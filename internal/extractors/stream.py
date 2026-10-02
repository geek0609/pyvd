"""Upload MP4 parts to Telegram as a source is downloaded and remuxed."""

import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from hydrogram import Client, raw, types
from hydrogram.session import Session

from internal.config.settings import Settings
from internal.core.errors import FileTooLarge
from internal.extractors.gallery import prefer_gallery
from internal.extractors.sites import Request
from internal.models.media import Media, MediaItem


PART_SIZE = 512 * 1024
STREAM_SITES = frozenset({"youtube", "tiktok", "twitter", "facebook"})


@dataclass
class UploadedVideo:
    media: Media
    file: raw.types.InputFileBig


async def _upload_parts(
    client: Client, process: asyncio.subprocess.Process, path: Path,
    settings: Settings, status: types.Message | None,
) -> raw.types.InputFileBig:
    file_id = client.rnd_id()
    session = Session(
        client, await client.storage.dc_id(), await client.storage.auth_key(),
        await client.storage.test_mode(), is_media=True,
    )
    tasks: set[asyncio.Task] = set()
    part_number = 0
    total_bytes = 0
    buffered = bytearray()
    last_update = 0.0

    async def send_part(number: int, chunk: bytes, total_parts: int) -> None:
        result = await session.invoke(raw.functions.upload.SaveBigFilePart(
            file_id=file_id, file_part=number, file_total_parts=total_parts, bytes=chunk,
        ))
        if result is not True:
            raise RuntimeError("Telegram rejected a video part")

    async def check_tasks() -> None:
        nonlocal tasks
        done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()

    async with client.save_file_semaphore:
        await session.start()
        try:
            with path.open("wb") as output:
                while True:
                    chunk = await asyncio.wait_for(process.stdout.read(64 * 1024), timeout=180)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > settings.max_file_size:
                        raise FileTooLarge("The file exceeds the 2 GB limit.")
                    output.write(chunk)
                    buffered.extend(chunk)
                    # Keep one part back; only the last part carries the final count.
                    while len(buffered) > PART_SIZE:
                        part = bytes(buffered[:PART_SIZE])
                        del buffered[:PART_SIZE]
                        tasks.add(asyncio.create_task(send_part(part_number, part, -1)))
                        part_number += 1
                        if len(tasks) >= 4:
                            await check_tasks()
                    now = time.monotonic()
                    if status and now - last_update >= 5:
                        last_update = now
                        try:
                            await status.edit_text(f"Downloading and uploading… {total_bytes / 1_000_000:.1f} MB")
                        except Exception:
                            pass
            if not buffered:
                raise RuntimeError("The video stream was empty")
            if await asyncio.wait_for(process.wait(), timeout=30):
                raise RuntimeError("The video remux failed")
            while tasks:
                await check_tasks()
            await send_part(part_number, bytes(buffered), part_number + 1)
            return raw.types.InputFileBig(id=file_id, parts=part_number + 1, name=path.name)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await session.stop()


async def try_stream_upload(
    client: Client, request: Request, settings: Settings, workdir: Path,
    status: types.Message | None = None,
) -> UploadedVideo | None:
    if request.extractor_id not in STREAM_SITES or prefer_gallery(request):
        return None
    if settings.site(request.extractor_id).edge_proxy:
        return None
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "internal.extractors.stream_worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    completed = False
    try:
        job = {
            "root": str(settings.root), "workdir": str(workdir),
            "extractor_id": request.extractor_id, "url": request.url,
        }
        process.stdin.write((json.dumps(job) + "\n").encode())
        await process.stdin.drain()
        process.stdin.close()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=180)
        header = json.loads(line)
        if not header.get("available"):
            await process.wait()
            completed = True
            return None
        path = workdir / "streamed.mp4"
        handle = await _upload_parts(client, process, path, settings, status)
        media = Media(request.extractor_id, request.content_id, request.url)
        media.caption = header["caption"]
        media.nsfw = header["nsfw"]
        media.items.append(MediaItem(
            kind="video", path=path, format_id=header["format_id"],
            size=path.stat().st_size, duration=header["duration"],
            width=header["width"], height=header["height"],
            title=header["title"], artist=header["artist"],
        ))
        completed = True
        return UploadedVideo(media, handle)
    except FileTooLarge:
        raise
    except Exception:
        (workdir / "streamed.mp4").unlink(missing_ok=True)
        return None
    finally:
        if not completed and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
