import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from internal.core.errors import DurationTooLong, FileTooLarge, MediaError
from internal.core.tasks import JobRunner
from internal.extractors.sites import Request
from internal.models.media import ChatSettings, Media, MediaItem


@pytest.fixture
def job(tmp_path: Path, monkeypatch):
    request = Request("youtube", "video-id", "https://youtu.be/video-id")
    cached = Media(
        request.extractor_id, request.content_id, request.url,
        items=[MediaItem("video", file_id="cached-id", video_codec="avc", audio_codec="aac")],
    )
    settings = SimpleNamespace(
        caching=True, downloads_dir=tmp_path / "downloads",
        captions_header="{{url}}", captions_description="{{text}}",
        site=lambda _: SimpleNamespace(disabled=False, ignore_regex=()),
    )
    cache_checked = asyncio.Event()

    async def cached_media(*args):
        cache_checked.set()
        return cached

    store = SimpleNamespace(
        cached_media=AsyncMock(side_effect=cached_media),
        mark_media_nsfw=AsyncMock(), save_media=AsyncMock(),
    )
    runner = JobRunner(SimpleNamespace(), settings, store, "pyvd")
    sender = SimpleNamespace(send=AsyncMock(return_value=["message"]))
    runner.sender = sender
    stream = AsyncMock(return_value=None)
    downloaded = Media(
        request.extractor_id, request.content_id, request.url,
        items=[MediaItem("video", path=tmp_path / "video.mp4", video_codec="avc", audio_codec="aac")],
    )
    download = AsyncMock(return_value=downloaded)
    prepare = AsyncMock(side_effect=lambda media, _: media)
    monkeypatch.setattr("internal.core.tasks.try_stream_upload", stream)
    monkeypatch.setattr("internal.core.tasks.download", download)
    monkeypatch.setattr("internal.core.tasks.prepare", prepare)
    return SimpleNamespace(
        runner=runner, request=request, cached=cached, store=store,
        sender=sender, stream=stream, download=download, prepare=prepare,
        downloaded=downloaded, cache_checked=cache_checked,
        chat=ChatSettings(123, "private", True, False, False, 10, False),
    )


@pytest.mark.asyncio
async def test_cached_media_is_delivered_when_download_capacity_is_full(job) -> None:
    job.runner.capacity = asyncio.Semaphore(0)

    result = await asyncio.wait_for(
        job.runner.run(job.request, job.chat, 123), timeout=1,
    )

    assert result.media is job.cached
    assert result.messages == ["message"]
    assert job.runner.locks == {}
    job.download.assert_not_awaited()
    job.stream.assert_not_awaited()
    assert not job.runner.settings.downloads_dir.exists()


@pytest.mark.asyncio
async def test_same_post_cached_deliveries_still_hold_the_request_lock(job) -> None:
    first_send = asyncio.Event()
    release_first = asyncio.Event()

    async def send(*args, **kwargs):
        if not first_send.is_set():
            first_send.set()
            await release_first.wait()
        return ["message"]

    job.sender.send.side_effect = send
    job.runner.capacity = asyncio.Semaphore(0)
    first = asyncio.create_task(job.runner.run(job.request, job.chat, 123))
    second = None
    try:
        await asyncio.wait_for(first_send.wait(), timeout=1)
        second = asyncio.create_task(job.runner.run(job.request, job.chat, 456))
        await asyncio.sleep(0)
        job.store.cached_media.assert_awaited_once()
        assert not second.done()
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    finally:
        release_first.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)

    assert job.sender.send.await_count == 2
    assert job.runner.locks == {}


@pytest.mark.asyncio
async def test_invalid_cached_file_waits_for_capacity_then_downloads(job) -> None:
    cached_send = asyncio.Event()

    async def send(chat_id, media, caption, **kwargs):
        if media is job.cached:
            cached_send.set()
            raise ValueError("FILE_REFERENCE_EXPIRED")
        return ["fresh-message"]

    job.sender.send.side_effect = send
    job.runner.capacity = asyncio.Semaphore(0)
    task = asyncio.create_task(job.runner.run(job.request, job.chat, 123))
    try:
        await asyncio.wait_for(cached_send.wait(), timeout=1)
        assert not task.done()
        job.download.assert_not_awaited()
        job.stream.assert_not_awaited()
        job.runner.capacity.release()
        result = await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert result.media is job.downloaded
    assert result.messages == ["fresh-message"]
    job.store.save_media.assert_awaited_once_with(job.downloaded)
    assert job.runner.locks == {}
    assert list(job.runner.settings.downloads_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_non_file_id_delivery_error_does_not_download_again(job) -> None:
    job.sender.send.side_effect = RuntimeError("CHAT_WRITE_FORBIDDEN")
    job.runner.capacity = asyncio.Semaphore(0)

    with pytest.raises(RuntimeError, match="CHAT_WRITE_FORBIDDEN"):
        await asyncio.wait_for(job.runner.run(job.request, job.chat, 123), timeout=1)

    job.download.assert_not_awaited()
    assert job.runner.locks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("extractor_id", ["youtube", "instagram"])
async def test_stale_cached_documents_use_download_capacity(job, extractor_id: str) -> None:
    request = Request(extractor_id, "video-id", f"https://example.com/{extractor_id}/video-id")
    job.cached.extractor_id = extractor_id
    job.cached.items[0].video_codec = "vp9"
    job.runner.capacity = asyncio.Semaphore(0)
    task = asyncio.create_task(job.runner.run(request, job.chat, 123))
    try:
        await asyncio.wait_for(job.cache_checked.wait(), timeout=1)
        assert not task.done()
        job.sender.send.assert_not_awaited()
        job.download.assert_not_awaited()
        job.runner.capacity.release()
        result = await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert result.media is job.downloaded
    job.download.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "nsfw", "album_limit", "public_group", "message"),
    [
        ("group", True, 10, False, "Marked media is disabled"),
        ("group", False, 1, False, "album limit"),
        ("private", True, 10, True, "group inline mode"),
    ],
)
async def test_cache_bypass_enforces_media_policy_before_delivery(
    job, kind: str, nsfw: bool, album_limit: int, public_group: bool, message: str,
) -> None:
    job.cached.nsfw = nsfw
    if album_limit == 1:
        job.cached.items.append(MediaItem("photo", file_id="photo-id"))
    chat = ChatSettings(-100, kind, True, False, False, album_limit, False)
    job.runner.capacity = asyncio.Semaphore(0)

    with pytest.raises(MediaError, match=message):
        await asyncio.wait_for(
            job.runner.run(job.request, chat, -100, public_group=public_group), timeout=1,
        )

    job.sender.send.assert_not_awaited()
    job.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_bypass_does_not_allow_other_domains_in_public_groups(job) -> None:
    job.runner.capacity = asyncio.Semaphore(0)

    with pytest.raises(MediaError, match="not in the public group allowlist"):
        await asyncio.wait_for(
            job.runner.run(
                Request("vimeo", "video-id", "https://vimeo.com/123"), job.chat, -100,
                public_group=True,
            ), timeout=1,
        )

    job.store.cached_media.assert_not_awaited()
    job.sender.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("public_group", [False, True])
async def test_marked_cached_video_keeps_group_spoiler_policy(job, public_group: bool) -> None:
    job.runner.capacity = asyncio.Semaphore(0)
    chat = ChatSettings(-100, "group", True, False, True, 10, False)

    await asyncio.wait_for(
        job.runner.run(
            job.request, chat, -100, public_group=public_group, marked_nsfw=True,
        ), timeout=1,
    )

    assert job.sender.send.call_args.kwargs["spoiler"] is public_group
    assert job.cached.nsfw
    job.store.mark_media_nsfw.assert_awaited_once_with("youtube", "video-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_x_inline_accepts_multiple_attachments_for_selection(job, cached) -> None:
    request = Request("twitter", "123", "https://x.com/user/status/123")
    job.runner.settings.caching = cached
    for media in (job.cached, job.downloaded):
        media.extractor_id = "twitter"
        media.content_id = "123"
        media.url = request.url
        media.items.append(MediaItem("video", file_id="second", video_codec="avc", audio_codec="aac"))
    result = await job.runner.run(request, job.chat, 123, inline=True, public_group=True)
    assert len(result.media.items) == 2
    job.sender.send.assert_awaited_once()
    if cached:
        job.download.assert_not_awaited()
    else:
        job.download.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_album_cache_downloads_under_capacity_and_enforces_single_item(job) -> None:
    job.cached.items.append(MediaItem("photo", file_id="photo-id"))
    job.downloaded.items.append(MediaItem("photo", path=Path("photo.jpg")))
    job.runner.capacity = asyncio.Semaphore(0)
    task = asyncio.create_task(job.runner.run(job.request, job.chat, 123, inline=True))
    try:
        await asyncio.wait_for(job.cache_checked.wait(), timeout=1)
        assert not task.done()
        job.sender.send.assert_not_awaited()
        job.runner.capacity.release()
        with pytest.raises(MediaError, match="Inline mode supports one media item"):
            await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    job.download.assert_awaited_once()
    job.sender.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [DurationTooLong("Too long"), FileTooLarge("Too large")])
async def test_streamed_video_limit_errors_do_not_download_again(job, error: MediaError) -> None:
    job.store.cached_media.return_value = None
    job.store.cached_media.side_effect = None
    job.stream.return_value = SimpleNamespace(media=job.downloaded, file="uploaded")
    job.prepare.side_effect = error

    with pytest.raises(type(error)) as exc:
        await job.runner.run(job.request, job.chat, 123)

    assert exc.value is error
    job.download.assert_not_awaited()
    job.sender.send.assert_not_awaited()
    job.store.save_media.assert_not_awaited()
    assert list(job.runner.settings.downloads_dir.iterdir()) == []
