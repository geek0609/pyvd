from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from internal.core.errors import FileTooLarge
from internal.core.media import prepare
from internal.models.media import Media, MediaItem


@pytest.mark.asyncio
async def test_photo_converts_to_telegram_jpeg(tmp_path: Path) -> None:
    source = tmp_path / "picture.png"
    Image.new("RGBA", (120, 80), (255, 0, 0, 100)).save(source)
    item = MediaItem(kind="photo", path=source)
    media = Media("instagram", "post", "https://instagram.com/p/post", items=[item])
    await prepare(media, SimpleNamespace(max_file_size=2_000_000_000, max_duration=3600))
    assert item.path and item.path.suffix == ".jpg" and item.path.is_file()
    assert (item.width, item.height) == (120, 80)


@pytest.mark.asyncio
async def test_rejects_file_above_limit_before_processing(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"abcdef")
    media = Media("youtube", "id", "https://youtu.be/id", items=[MediaItem(kind="video", path=source)])
    with pytest.raises(FileTooLarge):
        await prepare(media, SimpleNamespace(max_file_size=5, max_duration=3600))


def test_unsupported_codec_is_document() -> None:
    assert MediaItem(kind="video", video_codec="hevc", audio_codec="aac").delivery_kind == "document"
    assert MediaItem(kind="video", video_codec="avc", audio_codec="aac").delivery_kind == "video"
