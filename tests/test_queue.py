import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from internal.core.queue import FairQueue, JobCancelled, JobRegistry
from internal.core.tasks import Delivery, JobRunner
from internal.extractors.sites import Request
from internal.models.media import ChatSettings, Media, MediaItem


async def stop_tasks(tasks) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_downloads_rotate_between_chats_and_keep_each_chat_fifo() -> None:
    queue = FairQueue()
    starts = []
    started = {name: asyncio.Event() for name in ("a1", "a2", "a3", "b", "c", "d1", "d2", "e")}
    finished = {name: asyncio.Event() for name in started}
    running_chats = set()

    async def job(chat_id, name):
        async with queue.slot(chat_id):
            assert chat_id not in running_chats
            running_chats.add(chat_id)
            assert len(running_chats) <= 3
            starts.append(name)
            started[name].set()
            try:
                await finished[name].wait()
            finally:
                running_chats.remove(chat_id)

    tasks = [asyncio.create_task(job(chat, name)) for chat, name in ((1, "a1"), (2, "b"), (3, "c"))]
    try:
        await asyncio.wait_for(asyncio.gather(*(started[name].wait() for name in ("a1", "b", "c"))), 1)
        tasks.extend(asyncio.create_task(job(chat, name)) for chat, name in (
            (1, "a2"), (1, "a3"), (4, "d1"), (4, "d2"), (5, "e"),
        ))
        await asyncio.sleep(0)
        assert queue.active == 3 and queue.queued == 5
        assert starts == ["a1", "b", "c"]

        for ending, next_job in (("b", "d1"), ("d1", "e"), ("a1", "d2"), ("c", "a2"), ("a2", "a3")):
            finished[ending].set()
            await asyncio.wait_for(started[next_job].wait(), 1)
        assert starts == ["a1", "b", "c", "d1", "e", "d2", "a2", "a3"]
        for event in finished.values():
            event.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
    finally:
        await stop_tasks(tasks)
    assert queue.active == queue.queued == 0


@pytest.mark.asyncio
async def test_waiting_chat_runs_before_active_chats_next_download() -> None:
    queue = FairQueue(1)
    held = queue.slot(1)
    await held.__aenter__()
    starts = []

    async def job(chat_id, name):
        async with queue.slot(chat_id):
            starts.append(name)

    same_chat = asyncio.create_task(job(1, "a2"))
    other_chat = asyncio.create_task(job(2, "b1"))
    try:
        await asyncio.sleep(0)
        assert queue.queued == 2
        await held.__aexit__(None, None, None)
        await asyncio.wait_for(asyncio.gather(same_chat, other_chat), 1)
    finally:
        await stop_tasks((same_chat, other_chat))
    assert starts == ["b1", "a2"]
    assert queue.active == queue.queued == 0


@pytest.mark.asyncio
async def test_cancelling_a_queued_job_does_not_block_its_next_chat_job() -> None:
    queue = FairQueue(1)
    held = queue.slot(1)
    await held.__aenter__()
    started = asyncio.Event()

    async def job():
        async with queue.slot(1):
            started.set()

    cancelled = asyncio.create_task(job())
    next_job = asyncio.create_task(job())
    try:
        await asyncio.sleep(0)
        assert queue.queued == 2
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert queue.queued == 1
        await held.__aexit__(None, None, None)
        await asyncio.wait_for(next_job, 1)
    finally:
        await stop_tasks((cancelled, next_job))
    assert started.is_set()
    assert queue.active == queue.queued == 0


@pytest.mark.asyncio
async def test_cancellation_after_slot_grant_releases_capacity() -> None:
    queue = FairQueue(1)
    held = queue.slot(1)
    await held.__aenter__()

    async def job():
        async with queue.slot(2):
            raise AssertionError("cancelled job must not start")

    task = asyncio.create_task(job())
    await asyncio.sleep(0)
    await held.__aexit__(None, None, None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.active == queue.queued == 0
    async with queue.slot(3):
        assert queue.active == 1


@pytest.mark.asyncio
async def test_only_requester_can_cancel_exact_chat_and_message() -> None:
    registry = JobRegistry()
    started = asyncio.Event()
    cleaned = asyncio.Event()
    child_tasks = []

    async def operation():
        child_tasks.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()

    task = asyncio.create_task(registry.run(-100, 10, 7, operation()))
    await asyncio.wait_for(started.wait(), 1)
    assert registry.pending == 1
    assert child_tasks[0] is not task
    assert not registry.cancel(-100, 10, 8)
    assert not registry.cancel(-200, 10, 7)
    assert not registry.cancel(-100, 11, 7)
    assert not registry.cancel(-100, 10, None)
    assert registry.cancel(-100, 10, 7)
    assert not registry.cancel(-100, 10, 7)
    with pytest.raises(JobCancelled):
        await task
    assert cleaned.is_set()
    assert not task.cancelled()
    assert registry.pending == 0
    assert not registry.cancel(-100, 10, 7)


@pytest.mark.asyncio
async def test_background_job_registers_and_can_be_cancelled_before_its_first_turn() -> None:
    registry = JobRegistry()
    started = False

    async def operation():
        nonlocal started
        started = True
        await asyncio.Future()

    task = registry.start(-100, 10, 7, operation())
    assert registry.pending == 1
    assert not started
    assert registry.cancel(-100, 10, 7)
    with pytest.raises(JobCancelled):
        await task
    assert not started
    assert registry.pending == 0


@pytest.mark.asyncio
async def test_background_job_returns_immediately_and_forgets_its_finished_registration() -> None:
    registry = JobRegistry()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def operation():
        started.set()
        await finished.wait()
        return "delivered"

    task = registry.start(1, 10, 7, operation())
    duplicate = operation()
    try:
        assert registry.pending == 1 and not task.done()
        with pytest.raises(ValueError, match="already has a download job"):
            registry.start(1, 10, 8, duplicate)
        assert inspect.getcoroutinestate(duplicate) == inspect.CORO_CLOSED
        await asyncio.wait_for(started.wait(), 1)
        assert not task.done()
        finished.set()
        assert await task == "delivered"
    finally:
        await registry.close()
        await stop_tasks((task,))
    assert registry.pending == 0


@pytest.mark.asyncio
async def test_wrapper_cancelled_before_first_turn_cleans_its_background_child() -> None:
    registry = JobRegistry()
    cleaned = asyncio.Event()

    async def operation():
        try:
            await asyncio.Future()
        finally:
            cleaned.set()

    task = registry.start(1, 10, 7, operation())
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cleaned.wait(), 1)
        await asyncio.sleep(0)
        assert registry.pending == 0
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_shutdown_awaits_background_wrappers_and_children() -> None:
    registry = JobRegistry()
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def operation():
        started.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    task = registry.start(1, 10, 7, operation())
    await asyncio.wait_for(started.wait(), 1)
    await registry.close()
    assert cleaned.is_set() and task.done()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry.pending == 0


@pytest.mark.asyncio
async def test_cancelled_queue_registration_does_not_cancel_another_job() -> None:
    registry = JobRegistry()
    queue = FairQueue(1)
    started = [asyncio.Event(), asyncio.Event()]
    admitted = []
    finished = asyncio.Event()

    async def operation(index):
        started[index].set()
        async with queue.slot(1):
            admitted.append(index)
            await finished.wait()

    first = asyncio.create_task(registry.run(1, 10, 7, operation(0)))
    second = None
    try:
        await asyncio.wait_for(started[0].wait(), 1)
        second = asyncio.create_task(registry.run(1, 11, 7, operation(1)))
        await asyncio.wait_for(started[1].wait(), 1)
        assert queue.active == 1 and queue.queued == 1
        assert registry.cancel(1, 11, 7)
        with pytest.raises(JobCancelled):
            await second
        assert admitted == [0]
        assert registry.pending == 1
        assert queue.active == 1 and queue.queued == 0
        finished.set()
        await asyncio.wait_for(first, 1)
    finally:
        await stop_tasks([task for task in (first, second) if task is not None])
    assert registry.pending == queue.active == queue.queued == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_finished_jobs_are_forgotten_on_success_and_error(fails: bool) -> None:
    registry = JobRegistry()

    async def operation():
        if fails:
            raise RuntimeError("download failed")
        return "delivered"

    if fails:
        with pytest.raises(RuntimeError, match="download failed"):
            await registry.run(1, 10, 7, operation())
    else:
        assert await registry.run(1, 10, 7, operation()) == "delivered"
    assert registry.pending == 0
    assert not registry.cancel(1, 10, 7)


@pytest.mark.asyncio
async def test_caller_cancellation_still_propagates_and_unregisters() -> None:
    registry = JobRegistry()
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def operation():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()

    task = asyncio.create_task(registry.run(1, 10, 7, operation()))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set() and task.cancelled()
    assert registry.pending == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_running_and_queued_jobs_and_awaits_cleanup() -> None:
    registry = JobRegistry()
    queue = FairQueue(1)
    started = [asyncio.Event(), asyncio.Event()]
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    cleaned = []

    async def operation(index):
        started[index].set()
        try:
            async with queue.slot(1):
                await asyncio.Future()
        finally:
            if index == 0:
                cleanup_started.set()
                await finish_cleanup.wait()
            cleaned.append(index)

    tasks = [asyncio.create_task(registry.run(1, 10 + index, 7, operation(index))) for index in (0, 1)]
    closing = None
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 1)
        assert registry.pending == 2 and queue.queued == 1
        closing = asyncio.create_task(registry.close())
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert not closing.done()
        finish_cleanup.set()
        await asyncio.wait_for(closing, 1)
        assert sorted(cleaned) == [0, 1]
        assert registry.pending == queue.active == queue.queued == 0
        for task in tasks:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert all(task.cancelled() for task in tasks)
    finally:
        finish_cleanup.set()
        await stop_tasks([*tasks, *([closing] if closing is not None else [])])


@pytest.mark.asyncio
async def test_shutdown_does_not_interrupt_an_existing_cancellation_cleanup() -> None:
    registry = JobRegistry()
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()

    async def operation():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await finish_cleanup.wait()

    task = asyncio.create_task(registry.run(1, 10, 7, operation()))
    closing = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert registry.cancel(1, 10, 7)
        await asyncio.wait_for(cleanup_started.wait(), 1)
        closing = asyncio.create_task(registry.close())
        await asyncio.sleep(0)
        assert not closing.done()
        finish_cleanup.set()
        await asyncio.wait_for(closing, 1)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert registry.pending == 0
    finally:
        finish_cleanup.set()
        await stop_tasks([task, *([closing] if closing is not None else [])])


@pytest.mark.asyncio
async def test_closed_registry_rejects_new_jobs_without_starting_them() -> None:
    registry = JobRegistry()
    await registry.close()

    async def operation():
        raise AssertionError("shutdown registry must not start a new download")

    coroutine = operation()
    with pytest.raises(asyncio.CancelledError):
        await registry.run(1, 10, 7, coroutine)
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert registry.pending == 0
    await registry.close()


@pytest.mark.asyncio
async def test_duplicate_registration_cannot_replace_a_running_job() -> None:
    registry = JobRegistry()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def operation():
        started.set()
        await finished.wait()
        return "original"

    first = asyncio.create_task(registry.run(1, 10, 7, operation()))
    duplicate = operation()
    try:
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(ValueError, match="already has a download job"):
            await registry.run(1, 10, 8, duplicate)
        assert inspect.getcoroutinestate(duplicate) == inspect.CORO_CLOSED
        assert registry.pending == 1
        assert not registry.cancel(1, 10, 8)
        finished.set()
        assert await first == "original"
    finally:
        await stop_tasks((first,))
    assert registry.pending == 0


def runner(monkeypatch, cached: Media | None = None) -> JobRunner:
    settings = SimpleNamespace(
        caching=True, captions_header="", captions_description="",
        site=lambda _: SimpleNamespace(disabled=False, ignore_regex=()),
    )
    store = SimpleNamespace(cached_media=AsyncMock(return_value=cached))
    job_runner = JobRunner(SimpleNamespace(), settings, store, "pyvd")
    job_runner.sender = SimpleNamespace(send=AsyncMock(return_value=[]))
    return job_runner


@pytest.mark.asyncio
async def test_cached_delivery_bypasses_a_full_fair_queue(monkeypatch) -> None:
    cached = Media("youtube", "cached", "https://youtu.be/cached", items=[
        MediaItem("video", file_id="cached-id", video_codec="avc", audio_codec="aac"),
    ])
    job_runner = runner(monkeypatch, cached)
    holds = [job_runner._slot(chat_id) for chat_id in (1, 2, 3)]
    for held in holds:
        await held.__aenter__()
    try:
        result = await asyncio.wait_for(job_runner.run(
            Request("youtube", "cached", cached.url),
            ChatSettings(1, "private", False, False, False, 10, False), 1,
        ), 1)
        assert result.media is cached
        assert job_runner.queue.active == 3 and job_runner.queue.queued == 0
        job_runner.sender.send.assert_awaited_once()
    finally:
        for held in holds:
            await held.__aexit__(None, None, None)
    assert job_runner.queue.active == 0 and job_runner.locks == {}


@pytest.mark.asyncio
async def test_cancelled_source_request_does_not_cancel_another_subscriber(monkeypatch) -> None:
    job_runner = runner(monkeypatch)
    request = Request("youtube", "same-post", "https://youtu.be/same-post")
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    starts = []

    async def download(request, chat, target_chat_id, *args):
        starts.append(target_chat_id)
        if target_chat_id == 1:
            first_started.set()
            await asyncio.Future()
        second_started.set()
        await release_second.wait()
        return Delivery(Media("youtube", "same-post", request.url), [])

    monkeypatch.setattr(job_runner, "_download_and_send", download)
    chat = ChatSettings(1, "private", False, False, False, 10, False)
    first = asyncio.create_task(job_runner.jobs.run(1, 10, 7, job_runner.run(request, chat, 1)))
    second = None
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        second = asyncio.create_task(job_runner.jobs.run(2, 11, 8, job_runner.run(request, chat, 2)))
        await asyncio.sleep(0)
        assert job_runner.jobs.cancel(1, 10, 7)
        with pytest.raises(JobCancelled):
            await first
        await asyncio.wait_for(second_started.wait(), 1)
        assert starts == [1, 2] and not second.done()
        release_second.set()
        await asyncio.wait_for(second, 1)
    finally:
        await stop_tasks([task for task in (first, second) if task is not None])
    assert job_runner.jobs.pending == job_runner.queue.active == job_runner.queue.queued == 0
    assert job_runner.locks == {}
