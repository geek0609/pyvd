from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.core.send import Sender, batches, format_caption
from internal.models.media import ChatSettings, Media, MediaItem


def test_batches_keep_order_and_telegram_album_limits() -> None:
    items = [MediaItem(kind="photo") for _ in range(11)] + [
        MediaItem(kind="audio", audio_codec="mp3"), MediaItem(kind="video", video_codec="avc")
    ]
    assert [len(batch) for batch in batches(items)] == [10, 1, 1, 1]


def test_caption_escapes_remote_text() -> None:
    chat = ChatSettings(1, "private", True, False, False, 10, False)
    media = Media("youtube", "abc", "https://example.org/?x=1&y=2", caption="<script>")
    settings = SimpleNamespace(
        captions_header="<a href='{{url}}'>source</a> - @{{username}}",
        captions_description="{{text}}",
    )
    caption = format_caption(media, chat, settings, "bot")
    assert "&lt;script&gt;" in caption and "x=1&amp;y=2" in caption


@pytest.mark.asyncio
async def test_single_video_uses_hydrogram_upload(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    sent = SimpleNamespace(video=SimpleNamespace(file_id="cached-id"))

    class Client:
        async def send_video(self, chat_id, source, **kwargs):
            assert source == str(path)
            assert kwargs["supports_streaming"]
            return sent

    item = MediaItem(kind="video", path=path, size=5, video_codec="avc")
    media = Media("youtube", "abc", "https://youtu.be/abc", items=[item])
    sender = Sender(Client(), SimpleNamespace(max_file_size=2_000_000_000))
    assert await sender.send(1, media, "caption") == [sent]
    assert item.file_id == "cached-id"
