"""Stop job subprocesses before their temporary files are removed."""

import asyncio
import os
import signal


async def finish_task(task: asyncio.Task):
    """Drain cleanup work even when the calling task is cancelled again."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


def kill_process_group(process) -> None:
    pid = getattr(process, "pid", None)
    try:
        if pid is not None:
            os.killpg(pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass


async def terminate_process(process) -> None:
    kill_process_group(process)
    if process.returncode is None:
        await finish_task(asyncio.create_task(process.wait()))


async def spawn_process(*command, **kwargs):
    kwargs["start_new_session"] = True
    task = asyncio.create_task(asyncio.create_subprocess_exec(*command, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        process = await finish_task(task)
        output_task = asyncio.create_task(process.communicate())
        await terminate_process(process)
        await finish_task(output_task)
        raise
