"""Run link jobs with bounded concurrency and per-post deduplication."""

import asyncio
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from hydrogram import Client, enums, types

from internal.config.settings import Settings
from internal.core.errors import DurationTooLong, FileTooLarge, MediaError
from internal.core.media import extract_audio, prepare
from internal.core.music import check_music_policy, music_id, music_media, send_cached_music
from internal.core.queue import FairQueue, JobRegistry
from internal.core.send import Sender, format_caption
from internal.database.store import Store
from internal.extractors.downloader import download
from internal.extractors.sites import OTHER_SITE_ID, SITE_NAMES, Request, allowed_in_public_group
from internal.extractors.stream import try_stream_upload
from internal.models.media import ChatSettings, Media


@dataclass
class Delivery:
    media: Media
    messages: list[types.Message]


def stale_video_cache(media: Media) -> bool:
    return media.extractor_id in {"youtube", "instagram"} and any(
        item.kind == "video" and item.delivery_kind == "document" for item in media.items
    )


def check_group_nsfw(
    media: Media, chat: ChatSettings, public_group: bool, marked_nsfw: bool = False,
) -> None:
    if not (media.nsfw or marked_nsfw):
        return
    if public_group and chat.kind != "group":
        raise MediaError(
            "This link is unavailable in group inline mode. "
            "Send it as a regular group message instead."
        )
    if chat.kind == "group" and not chat.nsfw:
        raise MediaError("Marked media is disabled in this group.")


def delivery_spoiler(
    media: Media, chat: ChatSettings, public_group: bool,
    requested: bool, marked_nsfw: bool = False,
) -> bool:
    check_group_nsfw(media, chat, public_group, marked_nsfw)
    spoiler = requested or (public_group and (media.nsfw or marked_nsfw))
    if spoiler and any(item.delivery_kind not in {"photo", "video"} for item in media.items):
        raise MediaError("This media cannot be sent with a spoiler. Use a DM or private group.")
    return spoiler


class JobRunner:
    def __init__(self, client: Client, settings: Settings, store: Store, username: str):
        self.client = client
        self.settings = settings
        self.store = store
        self.username = username
        self.sender = Sender(client, settings)
        self.capacity = asyncio.Semaphore(3)
        self.queue = FairQueue(3)
        self.jobs = JobRegistry()
        self.locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def _slot(self, chat_id: int):
        async with self.queue.slot(chat_id), self.capacity:
            yield

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
                pass

    async def run_music(
        self, video: types.Video | types.Document, chat: ChatSettings,
        target_chat_id: int, reply_to: int, status: types.Message | None,
        marked_nsfw: bool = False, public_group: bool = False,
    ) -> None:
        if video.file_size and video.file_size > self.settings.max_file_size:
            raise FileTooLarge("The file exceeds the 2 GB limit.")
        if getattr(video, "duration", 0) > self.settings.max_duration:
            raise DurationTooLong("The media exceeds the duration limit.")
        async with self._lock("music:" + music_id(video)):
            cached = await send_cached_music(
                self.store, self.sender, self.settings, video, chat,
                target_chat_id, reply_to, status, marked_nsfw, public_group,
            )
            if cached.delivered:
                return
            marked_nsfw = cached.marked_nsfw
            async with self._slot(target_chat_id):
                self.settings.downloads_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="pyvd-music-", dir=self.settings.downloads_dir) as directory:
                    workdir = Path(directory)
                    last_update = 0.0
                    too_large = False

                    async def progress(current: int, total: int) -> None:
                        nonlocal last_update, too_large
                        if current > self.settings.max_file_size:
                            too_large = True
                            raise FileTooLarge("The file exceeds the 2 GB limit.")
                        now = time.monotonic()
                        if total and (now - last_update > 5 or current == total):
                            last_update = now
                            await self._status(status, f"Downloading video… {current * 100 // total}%")

                    await self._status(status, "Downloading video…")
                    downloaded = await self.client.download_media(
                        video, file_name=str(workdir / "video"), progress=progress,
                    )
                    if too_large:
                        raise FileTooLarge("The file exceeds the 2 GB limit.")
                    if downloaded is None:
                        raise MediaError("Could not download this video from Telegram.")
                    source = Path(downloaded)
                    downloaded_size = source.stat().st_size
                    if downloaded_size > self.settings.max_file_size:
                        raise FileTooLarge("The file exceeds the 2 GB limit.")
                    if video.file_size and downloaded_size != video.file_size:
                        raise MediaError("Telegram returned an incomplete video download.")
                    await self._status(status, "Extracting audio…")
                    title = Path(video.file_name or "Audio").stem or "Audio"
                    if title.lower() in {"streamed", "video"}:
                        title = "Audio"
                    item = await extract_audio(source, workdir, self.settings, title)
                    await self._status(status, "Uploading audio…")
                    check_music_policy(marked_nsfw, chat, public_group)
                    media = music_media(video, item, marked_nsfw)
                    await self.sender.send(
                        target_chat_id, media, "", reply_to=reply_to,
                        silent=chat.silent, status=status,
                    )
                    if self.settings.caching:
                        try:
                            await self.store.save_media(media)
                        except Exception:
                            pass

    async def run(
        self, request: Request, chat: ChatSettings, target_chat_id: int,
        reply_to: int | None = None, spoiler: bool = False,
        status: types.Message | None = None, inline: bool = False,
        public_group: bool = False, marked_nsfw: bool = False,
    ) -> Delivery:
        if public_group and not allowed_in_public_group(request):
            raise MediaError(
                "This domain is not in the public group allowlist. "
                "Send the link to PyVD in a DM or private group."
            )
        if marked_nsfw and chat.kind == "group" and not chat.nsfw:
            raise MediaError("Marked media is disabled in this group.")
        site = self.settings.site(request.extractor_id)
        other_disabled = (
            request.extractor_id not in SITE_NAMES
            and self.settings.site(OTHER_SITE_ID).disabled
        )
        if site.disabled or other_disabled:
            raise MediaError("This site is disabled by configuration.")
        if any(pattern.search(request.url) for pattern in site.ignore_regex):
            raise MediaError("This link is ignored by the site configuration.")
        async with self._lock(request.key):
            cached = await self.store.cached_media(request.extractor_id, request.content_id) if self.settings.caching else None
            if cached and stale_video_cache(cached):
                cached = None
            if cached and (not inline or len(cached.items) == 1):
                if marked_nsfw and not cached.nsfw:
                    await self.store.mark_media_nsfw(request.extractor_id, request.content_id)
                    cached.nsfw = True
                cached.url = request.url
                if chat.kind == "group" and len(cached.items) > chat.media_album_limit:
                    raise MediaError("This post exceeds this group's album limit.")
                send_spoiler = delivery_spoiler(
                    cached, chat, public_group, spoiler, marked_nsfw,
                )
                try:
                    await self._status(status, "Sending cached media…")
                    messages = await self.sender.send(
                        target_chat_id, cached,
                        format_caption(cached, chat, self.settings, self.username),
                        reply_to=reply_to, silent=chat.silent,
                        spoiler=send_spoiler, status=status,
                    )
                    return Delivery(cached, messages)
                except Exception as exc:
                    code = str(exc).upper()
                    if not any(part in code for part in ("FILE_ID_INVALID", "FILE_REFERENCE", "MEDIA_EMPTY", "FILE_ID")):
                        raise

            async with self._slot(target_chat_id):
                return await self._download_and_send(
                    request, chat, target_chat_id, reply_to, spoiler, status,
                    inline, public_group, marked_nsfw,
                )

    async def _download_and_send(
        self, request: Request, chat: ChatSettings, target_chat_id: int,
        reply_to: int | None, spoiler: bool, status: types.Message | None,
        inline: bool, public_group: bool, marked_nsfw: bool,
    ) -> Delivery:
        await self._status(status, "Downloading media…")
        self.settings.downloads_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pyvd-", dir=self.settings.downloads_dir) as directory:
            workdir = Path(directory)
            streamed = await try_stream_upload(self.client, request, self.settings, workdir, status)
            if streamed:
                media = streamed.media
                media.nsfw = media.nsfw or marked_nsfw
                check_group_nsfw(media, chat, public_group, marked_nsfw)
                await self._status(status, "Preparing media…")
                try:
                    media = await prepare(media, self.settings)
                except (DurationTooLong, FileTooLarge):
                    raise
                except MediaError:
                    pass
                else:
                    if media.items[0].delivery_kind == "video":
                        send_spoiler = delivery_spoiler(
                            media, chat, public_group, spoiler, marked_nsfw,
                        )
                        await self._status(status, "Sending media…")
                        message = await self.sender.send_preuploaded_video(
                            target_chat_id, media.items[0], streamed.file,
                            format_caption(media, chat, self.settings, self.username),
                            reply_to, chat.silent, send_spoiler,
                        )
                        if self.settings.caching:
                            try:
                                await self.store.save_media(media)
                            except Exception:
                                pass
                        return Delivery(media, [message])
                (workdir / "streamed.mp4").unlink(missing_ok=True)
            media = await download(request, self.settings, workdir)
            media.nsfw = media.nsfw or marked_nsfw
            if inline and len(media.items) != 1:
                raise MediaError("Inline mode supports one media item per link.")
            if chat.kind == "group" and len(media.items) > chat.media_album_limit:
                raise MediaError("This post exceeds this group's album limit.")
            check_group_nsfw(media, chat, public_group, marked_nsfw)
            await self._status(status, "Preparing media…")
            media = await prepare(media, self.settings)
            send_spoiler = delivery_spoiler(
                media, chat, public_group, spoiler, marked_nsfw,
            )
            await self._status(status, "Uploading media…")
            messages = await self.sender.send(
                target_chat_id, media,
                format_caption(media, chat, self.settings, self.username),
                reply_to=reply_to, silent=chat.silent,
                spoiler=send_spoiler, status=status,
            )
            if self.settings.caching:
                try:
                    await self.store.save_media(media)
                except Exception:
                    pass
            return Delivery(media, messages)
