"""Reuse extracted audio by the source video's Telegram identity."""

import hashlib
from dataclasses import dataclass

from internal.core.errors import DurationTooLong, FileTooLarge, MediaError
from internal.models.media import ChatSettings, Media, MediaItem


MUSIC_EXTRACTOR = "telegram_audio"


@dataclass(frozen=True)
class MusicCacheResult:
    delivered: bool = False
    marked_nsfw: bool = False


def music_id(video) -> str:
    unique_id = getattr(video, "file_unique_id", None)
    if unique_id:
        return str(unique_id)
    file_id = getattr(video, "file_id", "")
    if not file_id:
        raise MediaError("This video has no reusable Telegram ID.")
    return hashlib.sha256(str(file_id).encode()).hexdigest()


def music_media(video, item: MediaItem, marked_nsfw: bool = False) -> Media:
    return Media(
        MUSIC_EXTRACTOR, music_id(video), "", nsfw=marked_nsfw, items=[item],
    )


def check_music_policy(
    marked_nsfw: bool, chat: ChatSettings, public_group: bool = False,
) -> None:
    if not marked_nsfw:
        return
    if chat.kind == "group" and not chat.nsfw:
        raise MediaError("Marked media is disabled in this group.")
    if public_group:
        raise MediaError(
            "Audio from marked videos is unavailable here. Use a DM or private group."
        )


async def send_cached_music(
    store, sender, settings, video, chat: ChatSettings,
    target_chat_id: int, reply_to: int, status=None,
    marked_nsfw: bool = False, public_group: bool = False,
) -> MusicCacheResult:
    """Send cached audio, preserving its marker when a file ID needs refreshing."""
    check_music_policy(marked_nsfw, chat, public_group)
    if not settings.caching:
        return MusicCacheResult(marked_nsfw=marked_nsfw)
    content_id = music_id(video)
    cached = await store.cached_media(MUSIC_EXTRACTOR, content_id)
    if cached is None:
        return MusicCacheResult(marked_nsfw=marked_nsfw)
    marked_nsfw = marked_nsfw or cached.nsfw
    if marked_nsfw and not cached.nsfw:
        await store.mark_media_nsfw(MUSIC_EXTRACTOR, content_id)
        cached.nsfw = True
    check_music_policy(marked_nsfw, chat, public_group)
    if len(cached.items) != 1 or cached.items[0].kind != "audio":
        return MusicCacheResult(marked_nsfw=marked_nsfw)
    item = cached.items[0]
    if item.size > settings.max_file_size:
        raise FileTooLarge("The file exceeds the 2 GB limit.")
    if item.duration > settings.max_duration:
        raise DurationTooLong("The media exceeds the duration limit.")
    if not item.file_id:
        return MusicCacheResult(marked_nsfw=marked_nsfw)
    try:
        await sender.send(
            target_chat_id, cached, "", reply_to=reply_to,
            silent=chat.silent, status=status,
        )
    except Exception as exc:
        code = str(exc).upper()
        if not any(part in code for part in (
            "FILE_ID_INVALID", "FILE_REFERENCE", "MEDIA_EMPTY", "FILE_ID",
        )):
            raise
        return MusicCacheResult(marked_nsfw=marked_nsfw)
    return MusicCacheResult(delivered=True, marked_nsfw=marked_nsfw)
