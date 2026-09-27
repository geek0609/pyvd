"""Inline query placeholder and single-item replacement."""

import secrets
import time
from dataclasses import dataclass

from hydrogram import Client, enums, types

from internal.config.settings import Settings
from internal.core.send import format_caption, input_media
from internal.core.tasks import JobRunner, check_group_nsfw, stale_youtube_cache
from internal.database.store import Store
from internal.extractors.sites import (
    OTHER_SITE_ID, SITE_NAMES, Request, allowed_in_public_group, first_supported_url,
)


@dataclass
class Pending:
    user_id: int
    request: Request
    expires: float
    public_group: bool = False


class Inline:
    def __init__(self, client: Client, settings: Settings, store: Store):
        self.client = client
        self.settings = settings
        self.store = store
        self.pending: dict[str, Pending] = {}
        self.runner: JobRunner | None = None
        self.username = ""

    def add(self, user_id: int, request: Request, public_group: bool = False) -> str:
        now = time.monotonic()
        self.pending = {key: item for key, item in self.pending.items() if item.expires > now}
        task_id = secrets.token_hex(8)
        self.pending[task_id] = Pending(user_id, request, now + 300, public_group)
        return task_id

    def pop(self, task_id: str, user_id: int) -> Pending | None:
        item = self.pending.get(task_id)
        if item and item.user_id == user_id and item.expires > time.monotonic():
            del self.pending[task_id]
            return item
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
        group_context = query.chat_type not in {enums.ChatType.PRIVATE, enums.ChatType.BOT}
        if group_context and not allowed_in_public_group(request):
            await query.answer([], cache_time=0, is_personal=True)
            return
        task_id = self.add(query.from_user.id, request, public_group=group_context)
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
        pending = self.pop(chosen.result_id, chosen.from_user.id)
        if pending is None or not chosen.inline_message_id or self.runner is None:
            return
        request = pending.request
        user_id = chosen.from_user.id
        staged: list[types.Message] = []
        try:
            chat = await self.store.chat(user_id, "private")
            if self.settings.caching:
                cached = await self.store.cached_media(request.extractor_id, request.content_id)
                if cached and len(cached.items) == 1 and not stale_youtube_cache(cached):
                    cached.url = request.url
                    check_group_nsfw(cached, chat, pending.public_group)
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
                        pass
            delivery = await self.runner.run(
                request, chat, user_id, inline=True, public_group=pending.public_group,
            )
            staged = delivery.messages
            await self.client.edit_inline_media(
                chosen.inline_message_id,
                input_media(
                    delivery.media.items[0],
                    format_caption(delivery.media, chat, self.settings, self.username), False,
                ),
            )
        except Exception:
            try:
                await self.client.edit_inline_text(
                    chosen.inline_message_id,
                    "⚠️ This link is unavailable here. Send it to PyVD in a DM or private group.",
                    parse_mode=enums.ParseMode.DISABLED,
                )
            except Exception:
                pass
        finally:
            for message in staged:
                try:
                    await message.delete()
                except Exception:
                    pass
