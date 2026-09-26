import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.core.errors import FileTooLarge
from internal.extractors import stream, stream_worker
from internal.extractors.sites import Request


def test_stream_formats_keep_h264_and_aac_quality() -> None:
    video = {"url": "https://example.com/video", "protocol": "https", "ext": "mp4", "vcodec": "avc1.640028"}
    audio = {"url": "https://example.com/audio", "protocol": "https", "ext": "m4a", "acodec": "mp4a.40.2"}
    assert stream_worker._formats({"requested_formats": [video, audio]}) == [video, audio]
    assert stream_worker._formats({"requested_formats": [{**video, "vcodec": "h265"}, audio]}) is None
    assert stream_worker._formats({"requested_formats": [{**video, "protocol": "m3u8_native"}, audio]}) is None


async def test_gallery_posts_keep_the_gallery_downloader(tmp_path: Path) -> None:
    for request in (
        Request("instagram", "post", "https://www.instagram.com/p/post"),
        Request("tiktok", "123", "https://www.tiktok.com/@user/photo/123"),
    ):
        assert await stream.try_stream_upload(None, request, None, tmp_path) is None


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
