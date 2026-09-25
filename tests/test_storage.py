import pytest

from internal.database.store import Store
from internal.models.media import Media, MediaItem


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
