"""Schedule downloads fairly and keep cancellable jobs only while they run."""

import asyncio
from collections import deque
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypeVar


T = TypeVar("T")


class FairQueue:
    def __init__(self, limit: int = 3):
        if limit < 1:
            raise ValueError("The download limit must be positive.")
        self.limit = limit
        self._active: set[int] = set()
        self._waiting: dict[int, deque[asyncio.Future]] = {}
        self._rotation: deque[int] = deque()

    @property
    def active(self) -> int:
        return len(self._active)

    @property
    def queued(self) -> int:
        return sum(len(waiters) for waiters in self._waiting.values())

    def _schedule(self) -> None:
        while len(self._active) < self.limit and self._rotation:
            for _ in range(len(self._rotation)):
                chat_id = self._rotation.popleft()
                if chat_id in self._active:
                    self._rotation.append(chat_id)
                    continue
                waiters = self._waiting[chat_id]
                while waiters and waiters[0].cancelled():
                    waiters.popleft()
                if not waiters:
                    del self._waiting[chat_id]
                    continue
                waiter = waiters.popleft()
                if waiters:
                    self._rotation.append(chat_id)
                else:
                    del self._waiting[chat_id]
                self._active.add(chat_id)
                waiter.set_result(None)
                break
            else:
                break

    @asynccontextmanager
    async def slot(self, chat_id: int):
        waiter = asyncio.get_running_loop().create_future()
        if chat_id not in self._waiting:
            self._waiting[chat_id] = deque()
            self._rotation.append(chat_id)
        self._waiting[chat_id].append(waiter)
        self._schedule()
        try:
            await waiter
            yield
        finally:
            if waiter.done() and not waiter.cancelled():
                self._active.discard(chat_id)
                if chat_id in self._waiting:
                    self._rotation.remove(chat_id)
                    self._rotation.append(chat_id)
            else:
                waiters = self._waiting.get(chat_id)
                if waiters and waiter in waiters:
                    waiters.remove(waiter)
                    if not waiters:
                        del self._waiting[chat_id]
                        self._rotation.remove(chat_id)
            self._schedule()


class JobCancelled(Exception):
    """The requester cancelled this job."""


@dataclass
class _RegisteredJob:
    user_id: int | None
    task: asyncio.Task
    cancelled: bool = False
    waiter: asyncio.Task | None = None


class JobRegistry:
    def __init__(self):
        self._jobs: dict[tuple[int, int], _RegisteredJob] = {}
        self._closed = False

    @property
    def pending(self) -> int:
        return len(self._jobs)

    def start(
        self, chat_id: int, message_id: int, user_id: int | None,
        operation: Coroutine[Any, Any, T],
    ) -> asyncio.Task[T]:
        key = (chat_id, message_id)
        if self._closed:
            operation.close()
            raise asyncio.CancelledError
        if key in self._jobs:
            operation.close()
            raise ValueError("This message already has a download job.")
        job = _RegisteredJob(user_id, asyncio.create_task(operation))
        self._jobs[key] = job
        job.waiter = asyncio.create_task(self._wait(key, job))
        job.waiter.add_done_callback(lambda task: self._wait_finished(key, job, task))
        return job.waiter

    async def run(
        self, chat_id: int, message_id: int, user_id: int | None,
        operation: Coroutine[Any, Any, T],
    ) -> T:
        return await self.start(chat_id, message_id, user_id, operation)

    async def _wait(self, key: tuple[int, int], job: _RegisteredJob) -> Any:
        try:
            return await job.task
        except asyncio.CancelledError:
            if job.cancelled and not asyncio.current_task().cancelling():
                raise JobCancelled from None
            raise
        finally:
            self._forget(key, job)

    def _forget(self, key: tuple[int, int], job: _RegisteredJob) -> None:
        if self._jobs.get(key) is job:
            del self._jobs[key]
        if job.task.done() and not job.task.cancelled():
            job.task.exception()

    def _wait_finished(self, key: tuple[int, int], job: _RegisteredJob, waiter: asyncio.Task) -> None:
        # A task cancelled before its first turn never enters its finally block.
        if waiter.cancelled() and not job.task.done():
            if not job.task.cancelling():
                job.task.cancel()
            job.task.add_done_callback(lambda _: self._forget(key, job))
        else:
            self._forget(key, job)

    async def close(self) -> None:
        self._closed = True
        jobs = list(self._jobs.items())
        for _, job in jobs:
            job.cancelled = False
            if not job.task.done() and not job.task.cancelling():
                job.task.cancel()
        await asyncio.gather(
            *(task for _, job in jobs for task in (job.task, job.waiter) if task is not None),
            return_exceptions=True,
        )
        for key, job in jobs:
            if self._jobs.get(key) is job:
                del self._jobs[key]

    def cancel(self, chat_id: int, message_id: int, requester_id: int | None) -> bool:
        job = self._jobs.get((chat_id, message_id))
        if (
            job is None or requester_id is None or requester_id != job.user_id
            or job.task.done() or job.task.cancelling()
        ):
            return False
        job.cancelled = True
        return job.task.cancel()
