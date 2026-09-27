from types import SimpleNamespace

import pytest

from internal.database.store import Store
from internal.models.media import Media, MediaItem


@pytest.mark.asyncio
async def test_private_chat_uses_defaults_without_database_write() -> None:
    settings = SimpleNamespace(
        default_captions=True, default_silent=False, default_nsfw=False,
        default_media_album_limit=10, default_delete_links=False,
    )
    store = Store(settings)
    chat = await store.chat(123, "private")
    assert chat.chat_id == 123 and chat.kind == "private"
    assert store.pool is None


@pytest.mark.asyncio
async def test_media_cache_does_not_store_request_url_or_user() -> None:
    writes = []

    class Db:
        def acquire(self):
            return self

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def fetchval(self, query, *args):
            writes.append((query, args))
            return 1

        async def execute(self, query, *args):
            writes.append((query, args))

    store = Store.__new__(Store)
    store.pool = Db()
    media = Media(
        "youtube", "video-id", "https://youtube.com/watch?v=video-id&si=private-share",
        items=[MediaItem(kind="video", file_id="telegram-id", video_codec="avc")],
    )
    await store.save_media(media)
    assert writes[0][1][:3] == ("video-id", "", "youtube")
    assert all("private-share" not in repr(args) for _, args in writes)


@pytest.mark.asyncio
async def test_cache_rejects_missing_file_ids() -> None:
    store = Store.__new__(Store)
    media = Media("youtube", "abc123", "https://youtube.com/watch?v=abc123")
    media.items.append(MediaItem(kind="video"))
    with pytest.raises(ValueError, match="file IDs"):
        await store.save_media(media)


@pytest.mark.asyncio
async def test_invalid_setting_cannot_reach_sql() -> None:
    store = Store.__new__(Store)
    with pytest.raises(ValueError):
        await store.set_setting(1, "language", "ru")
    with pytest.raises(ValueError):
        await store.set_setting(1, "media_album_limit", 99)
