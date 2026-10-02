import asyncio
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.core.errors import MediaError
from internal.core.media import _probe, _thumbnail, extract_audio, prepare
from internal.extractors.gallery import download_gallery
from internal.extractors.sites import Request
from internal.models.media import Media, MediaItem
from internal.util.process import finish_task, spawn_process


def settings(tmp_path):
    return SimpleNamespace(
        max_file_size=2_000_000_000, max_duration=3600, proxy="",
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", proxy="", disable_proxy=False),
        cookie_path=lambda _: tmp_path / "missing.txt",
    )


async def capture_sleeping_process(monkeypatch, script="import time; time.sleep(60)"):
    original_start = asyncio.create_subprocess_exec
    processes = []
    started = asyncio.Event()

    async def start(*args, **kwargs):
        assert kwargs["start_new_session"]
        process = await original_start(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr("internal.util.process.asyncio.create_subprocess_exec", start)
    return processes, started


def assert_reaped(process):
    assert process.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["probe", "thumbnail", "audio"])
async def test_media_cancellation_reaps_child_before_return(tmp_path, monkeypatch, operation):
    processes, started = await capture_sleeping_process(monkeypatch)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    if operation == "probe":
        job = _probe(source)
    elif operation == "thumbnail":
        job = _thumbnail(MediaItem(kind="video", path=source))
    else:
        async def probe(_):
            return {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}

        monkeypatch.setattr("internal.core.media._probe", probe)
        job = extract_audio(source, tmp_path, settings(tmp_path), "Audio")
    task = asyncio.create_task(job)
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert_reaped(processes[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["probe", "thumbnail", "audio"])
async def test_media_timeouts_reap_child(tmp_path, monkeypatch, operation):
    processes, _ = await capture_sleeping_process(monkeypatch)
    original_wait_for = asyncio.wait_for

    async def short_timeout(awaitable, timeout):
        return await original_wait_for(awaitable, timeout=0.02)

    monkeypatch.setattr("internal.core.media.asyncio.wait_for", short_timeout)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    if operation == "thumbnail":
        await _thumbnail(MediaItem(kind="video", path=source))
    else:
        if operation == "probe":
            job = _probe(source)
        else:
            async def probe(_):
                return {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}

            monkeypatch.setattr("internal.core.media._probe", probe)
            job = extract_audio(source, tmp_path, settings(tmp_path), "Audio")
        with pytest.raises(MediaError, match="timed out"):
            await job
    assert_reaped(processes[0])


@pytest.mark.asyncio
async def test_gallery_cancellation_kills_subprocess_group_and_drains_output(tmp_path, monkeypatch):
    child_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time; "
        "from pathlib import Path; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"Path({str(child_file)!r}).write_text(str(child.pid)); "
        "sys.stderr.write('x' * 100000); sys.stderr.flush(); time.sleep(60)"
    )
    processes, started = await capture_sleeping_process(monkeypatch, script)
    task = asyncio.create_task(download_gallery(
        Request("instagram", "post", "https://instagram.com/reel/post/"),
        settings(tmp_path), tmp_path,
    ))
    await started.wait()
    async with asyncio.timeout(5):
        while not child_file.exists():
            await asyncio.sleep(0.01)
    child_pid = int(child_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert_reaped(processes[0])
    state = Path(f"/proc/{child_pid}/stat")
    assert not state.exists() or state.read_text().split()[2] == "Z"


@pytest.mark.asyncio
async def test_photo_cancellation_waits_for_file_writer_before_cleanup(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    target = tmp_path / "telegram.jpg"

    def prepare_photo(item, config):
        entered.set()
        assert release.wait(timeout=5)
        target.write_bytes(b"photo")
        item.path = target
        finished.set()

    monkeypatch.setattr("internal.core.media._prepare_photo", prepare_photo)
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo")
    media = Media("instagram", "post", "", items=[MediaItem(kind="photo", path=source)])
    task = asyncio.create_task(prepare(media, settings(tmp_path)))
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert finished.is_set() and target.is_file()
    target.unlink()
    await asyncio.sleep(0.01)
    assert not target.exists()


@pytest.mark.asyncio
async def test_cancelled_spawn_waits_for_spawn_then_reaps_process(monkeypatch):
    original_start = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    release = asyncio.Event()
    processes = []

    async def start(*args, **kwargs):
        entered.set()
        await release.wait()
        process = await original_start(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("internal.util.process.asyncio.create_subprocess_exec", start)
    task = asyncio.create_task(spawn_process("unused", stdout=asyncio.subprocess.PIPE))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert_reaped(processes[0])


@pytest.mark.asyncio
async def test_finish_task_drains_despite_repeated_cancellation():
    release = asyncio.Event()
    cleaned = asyncio.Event()

    async def cleanup():
        await release.wait()
        cleaned.set()
        return "done"

    inner = asyncio.create_task(cleanup())
    outer = asyncio.create_task(finish_task(inner))
    await asyncio.sleep(0)
    outer.cancel()
    await asyncio.sleep(0)
    outer.cancel()
    release.set()
    assert await outer == "done"
    assert cleaned.is_set()
