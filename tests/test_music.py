import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from hydrogram import enums

from internal.core.queue import JobRegistry
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
@pytest.mark.parametrize("marked", [False, True])
async def test_music_command_sends_audio_without_temporary_status(marked) -> None:
    events = []
    video = SimpleNamespace(file_id="video")

    class Message:
        text = "/music"
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=123, type=enums.ChatType.PRIVATE)
        from_user = SimpleNamespace(id=7)
        reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=42), video=video)
        reply_to_message_id = 5
        id = 6

        async def reply(self, text, **kwargs):
            raise AssertionError("successful /music must not send a status message")

    class Store:
        async def is_nsfw_file(self, file_id):
            return marked

        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

    class Runner:
        jobs = JobRegistry()
        async def run_music(self, source, chat, chat_id, reply_to, status, **kwargs):
            assert (source, chat.chat_id, chat_id, reply_to) == (video, 123, 123, 6)
            assert status is None
            assert kwargs == {"marked_nsfw": marked, "public_group": False}
            events.append("sent")

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), Store())
    bot.bot_id = 42
    bot.runner = Runner()
    await bot.on_message(None, Message())
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
    assert events == ["sent"]


@pytest.mark.asyncio
async def test_public_group_music_rejects_spoilered_video() -> None:
    replies = []

    class Message:
        text = "/music"
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=-100, type=enums.ChatType.SUPERGROUP, username="publicgroup")
        from_user = SimpleNamespace(id=7)
        reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=42), video=SimpleNamespace(file_id="video"),
            has_media_spoiler=True,
        )
        id = 6

        async def reply(self, text, **kwargs):
            replies.append(text)

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), SimpleNamespace())
    bot.bot_id = 42
    bot.runner = SimpleNamespace(jobs=JobRegistry())
    await bot.on_message(None, Message())
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
    assert replies == [
        "⚠️ Audio from spoilered videos is unavailable here. Use a DM or private group."
    ]


@pytest.mark.asyncio
async def test_group_music_rejects_cached_nsfw_video_when_disabled() -> None:
    replies = []

    class Message:
        text = "/music"
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=-100, type=enums.ChatType.SUPERGROUP, username=None)
        from_user = SimpleNamespace(id=7)
        reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=42), video=SimpleNamespace(file_id="video"),
        )
        id = 6

        async def reply(self, text, **kwargs):
            replies.append(text)

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

        async def is_nsfw_file(self, file_id):
            assert file_id == "video"
            return True

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), Store())
    bot.bot_id = 42
    bot.runner = SimpleNamespace(jobs=JobRegistry())
    await bot.on_message(None, Message())
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
    assert replies == ["⚠️ Marked media is disabled in this group."]


@pytest.mark.asyncio
async def test_music_rejects_oversize_video_before_download(tmp_path: Path) -> None:
    settings = SimpleNamespace(max_file_size=100, max_duration=3600, downloads_dir=tmp_path, caching=False)
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
    settings = SimpleNamespace(max_file_size=100, max_duration=3600, downloads_dir=tmp_path, caching=False)
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


@pytest.mark.asyncio
async def test_music_reuses_audio_across_chats_while_download_slots_are_busy(tmp_path, monkeypatch):
    import copy

    cached = {}
    downloads, sends = [], []

    class Store:
        async def cached_media(self, extractor, identity):
            return copy.deepcopy(cached.get((extractor, identity)))

        async def save_media(self, media):
            saved = copy.deepcopy(media)
            saved.items[0].path = None
            cached[(media.extractor_id, media.content_id)] = saved

    class Client:
        async def download_media(self, video, file_name, progress):
            downloads.append(video.file_id)
            path = Path(file_name)
            path.write_bytes(b"video")
            return path

    class Sender:
        async def send(self, chat_id, media, caption, **kwargs):
            sends.append((chat_id, kwargs["reply_to"]))
            if media.items[0].file_id:
                assert media.items[0].path is None
            else:
                media.items[0].file_id = "reusable-audio"

    async def extract(source, workdir, settings, title):
        return MediaItem("audio", path=source, size=5, duration=1, audio_codec="aac")

    monkeypatch.setattr("internal.core.tasks.extract_audio", extract)
    config = SimpleNamespace(caching=True, max_file_size=100, max_duration=60, downloads_dir=tmp_path)
    runner = JobRunner(Client(), config, Store(), "pyvd")
    runner.sender = Sender()
    video = SimpleNamespace(file_id="first-reference", file_unique_id="content-id", file_size=5,
                            duration=1, file_name="video.mp4")
    first = ChatSettings(1, "private", True, False, False, 10, False)
    await runner.run_music(video, first, 1, 10, None, marked_nsfw=True)
    video.file_id = "refreshed-reference"
    for _ in range(3):
        await runner.capacity.acquire()
    try:
        second = ChatSettings(2, "private", True, True, False, 10, False)
        await asyncio.wait_for(runner.run_music(video, second, 2, 20, None), 1)
    finally:
        for _ in range(3):
            runner.capacity.release()
    assert downloads == ["first-reference"]
    assert sends == [(1, 10), (2, 20)]
    assert list(cached) == [("telegram_audio", "content-id")]
    assert cached[("telegram_audio", "content-id")].nsfw
    group = ChatSettings(3, "group", True, False, False, 10, False)
    with pytest.raises(MediaError, match="disabled in this group"):
        await runner.run_music(video, group, 3, 30, None)
    assert len(sends) == 2
    assert not runner.locks and not list(tmp_path.iterdir())
