"""Run link jobs with bounded concurrency and per-post deduplication."""

import asyncio
import logging
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from hydrogram import Client, enums, types

from internal.config.settings import Settings
from internal.core.errors import MediaError
from internal.core.media import prepare
from internal.core.send import Sender, format_caption
from internal.database.store import Store
from internal.extractors.downloader import download
from internal.extractors.sites import Request
from internal.models.media import ChatSettings, Media


LOG = logging.getLogger(__name__)


@dataclass
class Delivery:
    media: Media
    messages: list[types.Message]


def stale_youtube_cache(media: Media) -> bool:
    return media.extractor_id == "youtube" and any(
        item.kind == "video" and item.delivery_kind == "document" for item in media.items
    )


class JobRunner:
    def __init__(self, client: Client, settings: Settings, store: Store, username: str):
        self.client = client
        self.settings = settings
        self.store = store
        self.username = username
        self.sender = Sender(client, settings)
        self.capacity = asyncio.Semaphore(3)
        self.locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def _lock(self, key: str):
        async with self.locks_guard:
            lock, count = self.locks.get(key, (asyncio.Lock(), 0))
            self.locks[key] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            async with self.locks_guard:
                _, count = self.locks[key]
                if count == 1:
                    del self.locks[key]
                else:
                    self.locks[key] = (lock, count - 1)

    async def _status(self, status: types.Message | None, text: str) -> None:
        if status:
            try:
                await status.edit_text(text, parse_mode=enums.ParseMode.DISABLED)
            except Exception:
                LOG.debug("could not update status message", exc_info=True)

    async def run(
        self, request: Request, chat: ChatSettings, target_chat_id: int,
        reply_to: int | None = None, spoiler: bool = False,
        status: types.Message | None = None, inline: bool = False,
    ) -> Delivery:
        site = self.settings.site(request.extractor_id)
        if site.disabled or request.extractor_id in chat.disabled_extractors:
            raise MediaError("This site is disabled in this chat.")
        if any(pattern.search(request.url) for pattern in site.ignore_regex):
            raise MediaError("This link is ignored by the site configuration.")
        async with self._lock(request.key), self.capacity:
            cached = await self.store.cached_media(request.extractor_id, request.content_id) if self.settings.caching else None
            if cached and stale_youtube_cache(cached):
                LOG.info("refreshing unsupported YouTube video format for %s", request.key)
                cached = None
            if cached and (not inline or len(cached.items) == 1):
                if chat.kind == "group" and len(cached.items) > chat.media_album_limit:
                    raise MediaError("This post exceeds this group's album limit.")
                if chat.kind == "group" and cached.nsfw and not chat.nsfw:
                    raise MediaError("NSFW media is disabled in this group.")
                try:
                    await self._status(status, "Sending cached media…")
                    messages = await self.sender.send(
                        target_chat_id, cached,
                        format_caption(cached, chat, self.settings, self.username),
                        reply_to=reply_to, silent=chat.silent, spoiler=spoiler, status=status,
                    )
                    return Delivery(cached, messages)
                except Exception as exc:
                    code = str(exc).upper()
                    if not any(part in code for part in ("FILE_ID_INVALID", "FILE_REFERENCE", "MEDIA_EMPTY", "FILE_ID")):
                        raise
                    LOG.warning("cached media ID rejected for %s: %s", request.key, exc)

            await self._status(status, "Downloading media…")
            self.settings.downloads_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="pyvd-", dir=self.settings.downloads_dir) as directory:
                workdir = Path(directory)
                media = await download(request, self.settings, workdir)
                if inline and len(media.items) != 1:
                    raise MediaError("Inline mode supports one media item per link.")
                if chat.kind == "group" and len(media.items) > chat.media_album_limit:
                    raise MediaError("This post exceeds this group's album limit.")
                if chat.kind == "group" and media.nsfw and not chat.nsfw:
                    raise MediaError("NSFW media is disabled in this group.")
                await self._status(status, "Preparing media…")
                media = await prepare(media, self.settings)
                await self._status(status, "Uploading media…")
                messages = await self.sender.send(
                    target_chat_id, media,
                    format_caption(media, chat, self.settings, self.username),
                    reply_to=reply_to, silent=chat.silent, spoiler=spoiler, status=status,
                )
                if self.settings.caching:
                    try:
                        await self.store.save_media(media)
                    except Exception:
                        LOG.exception("could not cache uploaded media for %s", request.key)
                return Delivery(media, messages)
