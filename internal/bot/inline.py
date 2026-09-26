"""Inline query placeholder and single-item replacement."""

import logging
import secrets
import time
from dataclasses import dataclass

from hydrogram import Client, enums, types

from internal.config.settings import Settings
from internal.core.send import Sender, format_caption, input_media
from internal.core.tasks import JobRunner, stale_youtube_cache
from internal.database.store import Store
from internal.extractors.sites import OTHER_SITE_ID, SITE_NAMES, Request, first_supported_url


LOG = logging.getLogger(__name__)


@dataclass
class Pending:
    user_id: int
    request: Request
    expires: float


class Inline:
    def __init__(self, client: Client, settings: Settings, store: Store):
        self.client = client
        self.settings = settings
        self.store = store
        self.pending: dict[str, Pending] = {}
        self.runner: JobRunner | None = None
        self.username = ""

    def add(self, user_id: int, request: Request) -> str:
        now = time.monotonic()
        self.pending = {key: item for key, item in self.pending.items() if item.expires > now}
        task_id = secrets.token_hex(8)
        self.pending[task_id] = Pending(user_id, request, now + 300)
        return task_id

    def pop(self, task_id: str, user_id: int) -> Request | None:
        item = self.pending.get(task_id)
        if item and item.user_id == user_id and item.expires > time.monotonic():
            del self.pending[task_id]
            return item.request
        return None

    async def query(self, _: Client, query: types.InlineQuery) -> None:
        if self.settings.whitelist and query.from_user.id not in self.settings.whitelist:
            await query.answer([], cache_time=0, is_personal=True)
            return
        request = first_supported_url(query.query or "")
        if request is None or self.settings.site(request.extractor_id).disabled:
            await query.answer([], cache_time=0, is_personal=True)
            return
        if request.extractor_id not in SITE_NAMES and self.settings.site(OTHER_SITE_ID).disabled:
            await query.answer([], cache_time=0, is_personal=True)
            return
        chat = await self.store.chat(query.from_user.id, "private")
        if request.extractor_id in chat.disabled_extractors or (
            request.extractor_id not in SITE_NAMES and OTHER_SITE_ID in chat.disabled_extractors
        ):
            await query.answer([], cache_time=0, is_personal=True)
            return
        task_id = self.add(query.from_user.id, request)
        result = types.InlineQueryResultArticle(
            title="Share media", id=task_id,
            input_message_content=types.InputTextMessageContent(
                "Preparing media…", parse_mode=enums.ParseMode.DISABLED,
                disable_web_page_preview=True,
            ),
            reply_markup=types.InlineKeyboardMarkup([[
                types.InlineKeyboardButton("…", callback_data="inline:loading"),
            ]]),
        )
        await query.answer([result], cache_time=0, is_personal=True)

    async def chosen(self, _: Client, chosen: types.ChosenInlineResult) -> None:
        request = self.pop(chosen.result_id, chosen.from_user.id)
        if request is None or not chosen.inline_message_id or self.runner is None:
            return
        user_id = chosen.from_user.id
        staged: list[types.Message] = []
        try:
            chat = await self.store.chat(user_id, "private")
            if self.settings.caching:
                cached = await self.store.cached_media(request.extractor_id, request.content_id)
                if cached and len(cached.items) == 1 and not stale_youtube_cache(cached):
                    try:
                        await self.client.edit_inline_media(
                            chosen.inline_message_id,
                            input_media(
                                cached.items[0],
                                format_caption(cached, chat, self.settings, self.username), False,
                            ),
                        )
                        return
                    except Exception:
                        LOG.info("cached inline media ID could not be reused for %s", request.key)
            delivery = await self.runner.run(request, chat, user_id, inline=True)
            staged = delivery.messages
            await self.client.edit_inline_media(
                chosen.inline_message_id,
                input_media(
                    delivery.media.items[0],
                    format_caption(delivery.media, chat, self.settings, self.username), False,
                ),
            )
        except Exception:
            LOG.exception("inline media failed for %s", request.key)
            try:
                await self.client.edit_inline_text(
                    chosen.inline_message_id,
                    "⚠️ Could not share this media. Start the bot in private chat and try again.",
                    parse_mode=enums.ParseMode.DISABLED,
                )
            except Exception:
                LOG.debug("could not update failed inline result", exc_info=True)
        finally:
            for message in staged:
                try:
                    await message.delete()
                except Exception:
                    LOG.debug("could not delete inline staging message", exc_info=True)
