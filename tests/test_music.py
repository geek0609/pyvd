from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from hydrogram import enums

from internal.bot.main import Bot, replied_video
from internal.core.errors import FileTooLarge, MediaError
from internal.core.media import extract_audio
from internal.core.tasks import JobRunner
from internal.models.media import ChatSettings, MediaItem


def test_replied_video_accepts_only_pyvd_video() -> None:
    video = SimpleNamespace(file_id="video")
    document = SimpleNamespace(file_id="document", mime_type="video/x-matroska")
    assert replied_video(SimpleNamespace(from_user=SimpleNamespace(id=42), video=video), 42) is video
    assert replied_video(
        SimpleNamespace(from_user=SimpleNamespace(id=42), video=None, document=document), 42,
    ) is document
    with pytest.raises(MediaError, match="Reply to a video sent by PyVD"):
        replied_video(SimpleNamespace(from_user=SimpleNamespace(id=7), video=video), 42)
    with pytest.raises(MediaError, match="does not contain a video"):
        replied_video(
            SimpleNamespace(from_user=SimpleNamespace(id=42), video=None, document=None), 42,
        )


@pytest.mark.asyncio
async def test_music_command_routes_reply_and_removes_status() -> None:
    events = []
    video = SimpleNamespace(file_id="video")

    class Status:
        async def delete(self):
            events.append("deleted")

    class Message:
        text = "/music"
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=123, type=enums.ChatType.PRIVATE)
        from_user = SimpleNamespace(id=7)
        reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=42), video=video)
        reply_to_message_id = 5
        id = 6

        async def reply(self, text, **kwargs):
            assert text == "Queued…"
            return Status()

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

    class Runner:
        async def run_music(self, source, chat, chat_id, reply_to, status):
            assert (source, chat.chat_id, chat_id, reply_to) == (video, 123, 123, 6)
            events.append("sent")

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), Store())
    bot.bot_id = 42
    bot.runner = Runner()
    await bot.on_message(None, Message())
    assert events == ["sent", "deleted"]


@pytest.mark.asyncio
async def test_music_rejects_oversize_video_before_download(tmp_path: Path) -> None:
    settings = SimpleNamespace(max_file_size=100, max_duration=3600, downloads_dir=tmp_path)
    runner = JobRunner(SimpleNamespace(), settings, SimpleNamespace(), "pyvd")
    video = SimpleNamespace(file_size=101, duration=10)
    with pytest.raises(FileTooLarge):
        await runner.run_music(video, SimpleNamespace(), 1, 2, SimpleNamespace())


@pytest.mark.asyncio
async def test_music_downloads_and_sends_audio_as_reply(tmp_path: Path, monkeypatch) -> None:
    events = []

    class Client:
        async def download_media(self, video, file_name, progress):
            assert video.file_id == "video-id"
            path = Path(file_name)
            path.write_bytes(b"video")
            await progress(5, 5)
            return path

    class Status:
        async def edit_text(self, text, **kwargs):
            events.append(text)

    class Sender:
        async def send(self, chat_id, media, caption, **kwargs):
            assert chat_id == 123
            assert media.items[0].delivery_kind == "audio"
            assert media.items[0].title == "Audio"
            assert caption == ""
            assert kwargs["reply_to"] == 6
            assert kwargs["silent"]
            events.append("sent")

    async def fake_extract(source, workdir, settings, title):
        assert source.read_bytes() == b"video"
        assert title == "Audio"
        return MediaItem(kind="audio", path=source, size=5, audio_codec="aac", title=title)

    monkeypatch.setattr("internal.core.tasks.extract_audio", fake_extract)
    settings = SimpleNamespace(max_file_size=100, max_duration=3600, downloads_dir=tmp_path)
    runner = JobRunner(Client(), settings, SimpleNamespace(), "pyvd")
    runner.sender = Sender()
    video = SimpleNamespace(file_id="video-id", file_size=5, duration=1, file_name="streamed.mp4")
    chat = ChatSettings(123, "private", True, True, False, 10, False)
    await runner.run_music(video, chat, 123, 6, Status())
    assert events[-1] == "sent"


@pytest.mark.asyncio
async def test_music_rejects_video_without_audio(tmp_path: Path, monkeypatch) -> None:
    async def probe(_):
        return {"streams": [{"codec_type": "video"}], "format": {"duration": "10"}}

    monkeypatch.setattr("internal.core.media._probe", probe)
    with pytest.raises(MediaError, match="no audio track"):
        await extract_audio(
            tmp_path / "video", tmp_path,
            SimpleNamespace(max_duration=3600, max_file_size=100), "Audio",
        )
