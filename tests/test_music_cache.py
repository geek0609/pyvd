from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from internal.core.errors import DurationTooLong, FileTooLarge, MediaError
from internal.core.music import MUSIC_EXTRACTOR, music_id, music_media, send_cached_music
from internal.models.media import ChatSettings, Media, MediaItem


def chat(kind="private", nsfw=False):
    return ChatSettings(123, kind, True, True, nsfw, 10, False)


def settings(caching=True):
    return SimpleNamespace(caching=caching, max_file_size=100, max_duration=60)


def media(nsfw=False, **item):
    return Media(
        MUSIC_EXTRACTOR, "same-video", "", nsfw=nsfw,
        items=[MediaItem(kind="audio", file_id="audio-id", audio_codec="aac", **item)],
    )


async def deliver(cached, *, video=None, group=None, config=None, marked=False, public=False, error=None):
    video = video or SimpleNamespace(file_unique_id="same-video", file_id="video-id")
    store = SimpleNamespace(
        cached_media=AsyncMock(return_value=cached), mark_media_nsfw=AsyncMock(),
    )
    sender = SimpleNamespace(send=AsyncMock(side_effect=error))
    result = await send_cached_music(
        store, sender, config or settings(), video, group or chat(), 456, 789,
        marked_nsfw=marked, public_group=public,
    )
    return result, store, sender


def test_music_key_ignores_chat_message_and_user_identity():
    first = SimpleNamespace(file_unique_id="same-video", file_id="old-ref", chat_id=123)
    second = SimpleNamespace(file_unique_id="same-video", file_id="new-ref", chat_id=456)
    assert music_id(first) == music_id(second) == "same-video"
    extracted = music_media(first, MediaItem(kind="audio"), marked_nsfw=True)
    assert extracted.extractor_id == MUSIC_EXTRACTOR
    assert extracted.content_id == "same-video"
    assert extracted.url == "" and extracted.nsfw


def test_music_key_without_unique_id_hashes_file_identity():
    first = SimpleNamespace(file_id="private-reference")
    second = SimpleNamespace(file_id="private-reference", user_id=456)
    assert music_id(first) == music_id(second)
    assert "private-reference" not in music_id(first)
    with pytest.raises(MediaError, match="reusable Telegram ID"):
        music_id(SimpleNamespace())


@pytest.mark.asyncio
async def test_music_cache_hit_reuses_audio_file_id():
    cached = media()
    result, store, sender = await deliver(cached)
    assert result.delivered
    store.cached_media.assert_awaited_once_with(MUSIC_EXTRACTOR, "same-video")
    sender.send.assert_awaited_once_with(
        456, cached, "", reply_to=789, silent=True, status=None,
    )


@pytest.mark.asyncio
async def test_disabled_music_cache_avoids_database_lookup():
    result, store, sender = await deliver(media(), config=settings(caching=False))
    assert not result.delivered
    store.cached_media.assert_not_awaited()
    sender.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["FILE_ID_INVALID", "FILE_REFERENCE_EXPIRED", "MEDIA_EMPTY"])
async def test_stale_audio_falls_back_and_retains_cached_marker(error):
    result, _, _ = await deliver(media(nsfw=True), error=RuntimeError(error))
    assert not result.delivered and result.marked_nsfw


@pytest.mark.asyncio
async def test_unrelated_send_error_is_not_hidden():
    with pytest.raises(RuntimeError, match="CHAT_WRITE_FORBIDDEN"):
        await deliver(media(), error=RuntimeError("CHAT_WRITE_FORBIDDEN"))


@pytest.mark.asyncio
@pytest.mark.parametrize("public,enabled", [(False, False), (True, True)])
async def test_cached_marker_enforces_group_policy(public, enabled):
    with pytest.raises(MediaError, match="disabled|Audio from marked"):
        await deliver(media(nsfw=True), group=chat("group", enabled), public=public)


@pytest.mark.asyncio
async def test_new_source_marker_updates_cached_audio_before_delivery():
    cached = media()
    result, store, _ = await deliver(cached, marked=True)
    assert result.delivered and result.marked_nsfw and cached.nsfw
    store.mark_media_nsfw.assert_awaited_once_with(MUSIC_EXTRACTOR, "same-video")


@pytest.mark.asyncio
@pytest.mark.parametrize("limits,error", [({"size": 101}, FileTooLarge), ({"duration": 61}, DurationTooLong)])
async def test_cached_audio_obeys_current_limits(limits, error):
    with pytest.raises(error):
        await deliver(media(**limits))


@pytest.mark.asyncio
async def test_cache_miss_retains_source_marker():
    result, _, sender = await deliver(None, marked=True)
    assert not result.delivered and result.marked_nsfw
    sender.send.assert_not_awaited()
