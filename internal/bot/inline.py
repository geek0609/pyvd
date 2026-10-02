"""Inline query placeholder and single-item replacement."""

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from urllib.request import Request as HttpRequest, urlopen

from hydrogram import Client, enums, types

from internal.config.settings import Settings
from internal.core.errors import MediaError, NoAttachments
from internal.core.send import format_caption
from internal.core.tasks import JobRunner, check_group_nsfw, stale_video_cache
from internal.database.store import Store
from internal.extractors.sites import (
    OTHER_SITE_ID, SITE_NAMES, Request, allowed_in_public_group, first_supported_url,
)
from internal.models.media import MediaItem
from internal.util.process import finish_task


def _edit_media(token: str, message_id: str, item: MediaItem, caption: str) -> None:
    """Replace an inline article with media already uploaded to Telegram."""
    if not item.file_id:
        raise MediaError("Telegram did not return a reusable media ID.")
    kind = item.delivery_kind
    media = {
        "type": kind,
        "media": item.file_id,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if kind == "video":
        media["supports_streaming"] = True
        media["duration"] = item.duration
        media["width"] = item.width
        media["height"] = item.height
    elif kind == "audio":
        media["duration"] = item.duration
        media["performer"] = item.artist
        media["title"] = item.title
    request = HttpRequest(
        f"https://api.telegram.org/bot{token}/editMessageMedia",
        data=json.dumps({"inline_message_id": message_id, "media": media}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get("ok") is not True or result.get("result") is not True:
        raise MediaError("Telegram did not replace the inline message.")


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
        self.tasks: set[asyncio.Task[None]] = set()
        self._expirations: dict[str, asyncio.TimerHandle] = {}
        self._closed = False
        self.runner: JobRunner | None = None
        self.username = ""

    def add(self, user_id: int, request: Request, public_group: bool = False) -> str:
        if self._closed:
            raise RuntimeError("Inline delivery is closed.")
        now = time.monotonic()
        for key, item in list(self.pending.items()):
            if item.expires <= now:
                self._discard(key)
        task_id = secrets.token_hex(8)
        self.pending[task_id] = Pending(user_id, request, now + 300, public_group)
        try:
            self._expirations[task_id] = asyncio.get_running_loop().call_later(
                300, self._discard, task_id,
            )
        except RuntimeError:
            pass
        return task_id

    def _discard(self, task_id: str) -> None:
        self.pending.pop(task_id, None)
        expiration = self._expirations.pop(task_id, None)
        if expiration is not None:
            expiration.cancel()

    def pop(self, task_id: str, user_id: int) -> Pending | None:
        item = self.pending.get(task_id)
        if item and item.expires <= time.monotonic():
            self._discard(task_id)
            return None
        if item and item.user_id == user_id and item.expires > time.monotonic():
            self._discard(task_id)
            return item
        return None

    def _start_delivery(self, pending: Pending, user_id: int, message_id: str) -> None:
        if self._closed:
            return
        task = asyncio.create_task(self._deliver(pending, user_id, message_id))
        self.tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        self._closed = True
        for task_id in list(self.pending):
            self._discard(task_id)
        tasks = list(self.tasks)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        if tasks:
            cleanup = asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await finish_task(cleanup)
                raise

    async def query(self, _: Client, query: types.InlineQuery) -> None:
        if self._closed or (
            self.settings.whitelist and query.from_user.id not in self.settings.whitelist
        ):
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
                "Preparing media… Tap Download if it does not start automatically.",
                parse_mode=enums.ParseMode.DISABLED,
                disable_web_page_preview=True,
            ),
            reply_markup=types.InlineKeyboardMarkup([[
                types.InlineKeyboardButton(
                    "Download", callback_data=f"inline:download:{task_id}",
                ),
            ]]),
        )
        await query.answer([result], cache_time=0, is_personal=True)

    async def chosen(self, _: Client, chosen: types.ChosenInlineResult) -> None:
        if not chosen.inline_message_id:
            return
        pending = self.pop(chosen.result_id, chosen.from_user.id)
        if pending is not None:
            self._start_delivery(pending, chosen.from_user.id, chosen.inline_message_id)

    async def callback(self, query: types.CallbackQuery) -> bool:
        data = query.data
        if not isinstance(data, str) or not data.startswith("inline:download:"):
            return False
        if not query.inline_message_id or not query.from_user:
            await query.answer("This inline result cannot be edited. Send the query again.", show_alert=True)
            return True
        task_id = data.removeprefix("inline:download:")
        pending = self.pop(task_id, query.from_user.id)
        if pending is None:
            await query.answer("This download has started or expired. Send the query again if needed.", show_alert=True)
            return True
        await query.answer("Downloading media…")
        self._start_delivery(pending, query.from_user.id, query.inline_message_id)
        return True

    async def _replace_media(self, message_id: str, item: MediaItem, caption: str) -> None:
        task = asyncio.create_task(asyncio.to_thread(
            _edit_media, self.settings.bot_token, message_id, item, caption,
        ))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await finish_task(task)
            except Exception:
                pass
            raise

    async def _deliver(self, pending: Pending, user_id: int, inline_message_id: str) -> None:
        if self.runner is None:
            return
        request = pending.request
        staged: list[types.Message] = []
        try:
            chat = await self.store.chat(user_id, "private")
            if self.settings.caching:
                cached = await self.store.cached_media(request.extractor_id, request.content_id)
                if cached and len(cached.items) == 1 and not stale_video_cache(cached):
                    cached.url = request.url
                    check_group_nsfw(cached, chat, pending.public_group)
                    try:
                        await self._replace_media(
                            inline_message_id, cached.items[0],
                            format_caption(cached, chat, self.settings, self.username),
                        )
                        return
                    except Exception:
                        pass
            delivery = await self.runner.run(
                request, chat, user_id, inline=True, public_group=pending.public_group,
            )
            staged = delivery.messages
            await self._replace_media(
                inline_message_id, delivery.media.items[0],
                format_caption(delivery.media, chat, self.settings, self.username),
            )
        except NoAttachments:
            try:
                await self.client.edit_inline_text(
                    inline_message_id, "This post has no attached media.",
                    parse_mode=enums.ParseMode.DISABLED,
                )
            except Exception:
                pass
        except Exception:
            try:
                await self.client.edit_inline_text(
                    inline_message_id,
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
