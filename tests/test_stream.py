import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from internal.core.errors import FileTooLarge
from internal.extractors import stream, stream_worker
from internal.extractors.sites import Request


def test_stream_formats_keep_h264_and_aac_quality() -> None:
    video = {"url": "https://example.com/video", "protocol": "https", "ext": "mp4", "vcodec": "avc1.640028"}
    audio = {"url": "https://example.com/audio", "protocol": "https", "ext": "m4a", "acodec": "mp4a.40.2"}
    assert stream_worker._formats({"requested_formats": [video, audio]}) == [video, audio]
    assert stream_worker._formats({"requested_formats": [{**video, "vcodec": "h265"}, audio]}) is None
    segmented = {**video, "protocol": "m3u8_native"}
    assert stream_worker._formats({"requested_formats": [segmented, audio]}) == [segmented, audio]


async def test_gallery_posts_keep_the_gallery_downloader(tmp_path: Path) -> None:
    for request in (
        Request("reddit", "post", "https://www.reddit.com/r/test/comments/post"),
        Request("tiktok", "123", "https://www.tiktok.com/@user/photo/123"),
        Request("soundcloud", "track", "https://soundcloud.com/user/track"),
    ):
        assert await stream.try_stream_upload(None, request, None, tmp_path) is None


async def test_edge_proxy_does_not_start_a_stream_worker(tmp_path: Path, monkeypatch) -> None:
    async def unexpected_worker(*args, **kwargs):
        raise AssertionError("an ineligible stream must skip worker startup")

    monkeypatch.setattr(stream.asyncio, "create_subprocess_exec", unexpected_worker)
    settings = SimpleNamespace(site=lambda _: SimpleNamespace(edge_proxy="https://edge.example"))
    assert await stream.try_stream_upload(
        None, Request("twitter", "123", "https://x.com/user/status/123"), settings, tmp_path,
    ) is None


@pytest.mark.parametrize("site,url", [
    ("youtube", "https://youtu.be/123"),
    ("twitter", "https://x.com/user/status/123"),
    ("instagram", "https://www.instagram.com/reel/123/"),
    ("vimeo", "https://vimeo.com/123"),
    ("dailymotion", "https://www.dailymotion.com/video/123"),
])
async def test_eligible_stream_still_starts_worker(tmp_path: Path, monkeypatch, site, url) -> None:
    starts = []
    process = SimpleNamespace(
        stdin=SimpleNamespace(write=lambda _: None, drain=AsyncMock(), close=lambda: None),
        stdout=SimpleNamespace(readline=AsyncMock(return_value=b'{"available":false}\n')),
        wait=AsyncMock(return_value=0), returncode=0,
    )

    async def start(*args, **kwargs):
        starts.append(args)
        return process

    monkeypatch.setattr(stream.asyncio, "create_subprocess_exec", start)
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n")
    settings = SimpleNamespace(
        root=tmp_path, site=lambda _: SimpleNamespace(edge_proxy=""),
        cookie_path=lambda _: cookie,
    )
    assert await stream.try_stream_upload(
        None, Request(site, "123", url), settings, tmp_path,
    ) is None
    assert len(starts) == 1


@pytest.mark.parametrize("length,expected", [
    (stream.PART_SIZE, [(0, 1, stream.PART_SIZE)]),
    (stream.PART_SIZE + 11, [(0, -1, stream.PART_SIZE), (1, 2, 11)]),
    (stream.PART_SIZE * 2, [(0, -1, stream.PART_SIZE), (1, 2, stream.PART_SIZE)]),
])
async def test_stream_parts_finalize_only_last_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, length: int, expected: list[tuple[int, int, int]],
) -> None:
    calls = []

    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            pass

        async def invoke(self, request):
            calls.append((request.file_part, request.file_total_parts, len(request.bytes)))
            return True

        async def stop(self):
            pass

    class Storage:
        async def dc_id(self):
            return 1

        async def auth_key(self):
            return b"key"

        async def test_mode(self):
            return False

    monkeypatch.setattr(stream, "Session", FakeSession)
    reader = asyncio.StreamReader()
    reader.feed_data(b"a" * length)
    reader.feed_eof()

    async def wait():
        return 0

    client = SimpleNamespace(storage=Storage(), save_file_semaphore=asyncio.Semaphore(1), rnd_id=lambda: 123)
    path = tmp_path / "video.mp4"
    handle = await stream._upload_parts(
        client, SimpleNamespace(stdout=reader, wait=wait), path,
        SimpleNamespace(max_file_size=2_000_000_000), None,
    )
    assert sorted(calls) == sorted(expected)
    assert handle.parts == len(expected)
    assert path.stat().st_size == length


async def test_stream_stops_at_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    class Storage:
        async def dc_id(self):
            return 1

        async def auth_key(self):
            return b"key"

        async def test_mode(self):
            return False

    monkeypatch.setattr(stream, "Session", FakeSession)
    reader = asyncio.StreamReader()
    reader.feed_data(b"a" * 101)
    reader.feed_eof()
    client = SimpleNamespace(storage=Storage(), save_file_semaphore=asyncio.Semaphore(1), rnd_id=lambda: 123)
    with pytest.raises(FileTooLarge):
        await stream._upload_parts(client, SimpleNamespace(stdout=reader), tmp_path / "video.mp4",
                                   SimpleNamespace(max_file_size=100), None)


def upload_client():
    storage = SimpleNamespace(
        dc_id=AsyncMock(return_value=1), auth_key=AsyncMock(return_value=b"key"),
        test_mode=AsyncMock(return_value=False),
    )
    return SimpleNamespace(storage=storage, save_file_semaphore=asyncio.Semaphore(1), rnd_id=lambda: 123)


async def test_rejected_part_cannot_complete_upload(tmp_path: Path, monkeypatch) -> None:
    session = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), invoke=AsyncMock(return_value=False))
    monkeypatch.setattr(stream, "Session", lambda *_args, **_kwargs: session)
    reader = asyncio.StreamReader()
    reader.feed_data(b"a" * 100)
    reader.feed_eof()
    with pytest.raises(RuntimeError, match="rejected"):
        await stream._upload_parts(
            upload_client(), SimpleNamespace(stdout=reader, wait=AsyncMock(return_value=0)),
            tmp_path / "video.mp4", SimpleNamespace(max_file_size=1000), None,
        )
    session.stop.assert_awaited_once()


async def test_cancelled_upload_stops_pending_parts(tmp_path: Path, monkeypatch) -> None:
    invoked = asyncio.Event()
    cancelled = []

    async def invoke(request):
        invoked.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.append(request.file_part)
            raise

    session = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), invoke=invoke)
    monkeypatch.setattr(stream, "Session", lambda *_args, **_kwargs: session)
    reader = asyncio.StreamReader()
    reader.feed_data(b"a" * (stream.PART_SIZE * 5))
    task = asyncio.create_task(stream._upload_parts(
        upload_client(), SimpleNamespace(stdout=reader), tmp_path / "video.mp4",
        SimpleNamespace(max_file_size=10_000_000), None,
    ))
    await asyncio.wait_for(invoked.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(cancelled) == [0, 1, 2, 3]
    session.stop.assert_awaited_once()


async def test_cancelled_session_start_closes_partial_connection(tmp_path: Path, monkeypatch) -> None:
    connected = asyncio.Event()
    session = SimpleNamespace(connection=None, stop=AsyncMock())

    async def start():
        session.connection = object()
        connected.set()
        await asyncio.Future()

    session.start = start
    monkeypatch.setattr(stream, "Session", lambda *_args, **_kwargs: session)
    client = upload_client()
    task = asyncio.create_task(stream._upload_parts(
        client, SimpleNamespace(), tmp_path / "video.mp4",
        SimpleNamespace(max_file_size=10_000_000), None,
    ))
    await asyncio.wait_for(connected.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    session.stop.assert_awaited_once()
    assert not client.save_file_semaphore.locked()
    assert not (tmp_path / "video.mp4").exists()


async def test_cancelled_stream_terminates_worker_and_removes_partial_file(tmp_path: Path, monkeypatch) -> None:
    started = asyncio.Event()
    process = SimpleNamespace(
        stdin=SimpleNamespace(write=lambda _: None, drain=AsyncMock(), close=lambda: None),
        stdout=SimpleNamespace(readline=AsyncMock(return_value=b'{"available":true}\n')),
        wait=AsyncMock(return_value=0), communicate=AsyncMock(return_value=(b"", b"")),
        returncode=None, pid=4321,
    )
    monkeypatch.setattr(stream.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    killed = []
    monkeypatch.setattr(stream.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    async def upload(*_args):
        (tmp_path / "streamed.mp4").write_bytes(b"partial video")
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(stream, "_upload_parts", upload)
    settings = SimpleNamespace(root=tmp_path, site=lambda _: SimpleNamespace(edge_proxy=""))
    task = asyncio.create_task(stream.try_stream_upload(
        None, Request("instagram", "123", "https://www.instagram.com/reel/123/"), settings, tmp_path,
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [(4321, stream.signal.SIGTERM)]
    process.communicate.assert_awaited_once()
    assert not (tmp_path / "streamed.mp4").exists()
