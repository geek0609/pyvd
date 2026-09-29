"""Photo and carousel fallback using gallery-dl."""

import asyncio
import json
import shutil
import sys
from pathlib import Path

from internal.config.settings import Settings
from internal.core.errors import AuthenticationRequired, FileTooLarge, MediaError, NoMedia
from internal.extractors.cookies import job_cookie_file
from internal.extractors.sites import Request
from internal.models.media import Media, MediaItem


GALLERY_SITES = {"instagram", "pinterest", "reddit", "threads", "ninegag"}
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".flac", ".ogg", ".opus"}
SKIP_SUFFIXES = {".part", ".json", ".txt", ".ytdl"}


def prefer_gallery(request: Request) -> bool:
    return request.extractor_id in GALLERY_SITES or (
        request.extractor_id == "tiktok" and "/photo/" in request.url
    )


def files_in(directory: Path) -> list[Path]:
    return sorted(
        (path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() not in SKIP_SUFFIXES),
        key=lambda path: str(path.relative_to(directory)),
    )


def _caption(directory: Path) -> str:
    for path in directory.rglob("*.json"):
        try:
            info = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(info, dict):
            for key in ("description", "caption", "title"):
                value = info.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return ""


def _marked_nsfw(directory: Path) -> bool:
    markers = {"over_18", "nsfw", "is_nsfw", "sensitive", "possibly_sensitive"}

    def marked(value: object) -> bool:
        if isinstance(value, dict):
            if any(value.get(key) is True for key in markers):
                return True
            limit = value.get("age_limit")
            if isinstance(limit, (int, float, str)) and not isinstance(limit, bool):
                try:
                    if float(limit) >= 18:
                        return True
                except ValueError:
                    pass
            return any(marked(item) for item in value.values() if isinstance(item, (dict, list)))
        if isinstance(value, list):
            return any(marked(item) for item in value)
        return False

    for path in directory.rglob("*.json"):
        try:
            if marked(json.loads(path.read_text())):
                return True
        except (OSError, ValueError):
            continue
    return False


async def download_gallery(request: Request, settings: Settings, workdir: Path) -> Media:
    site = settings.site(request.extractor_id)
    if site.edge_proxy:
        raise NoMedia("This site's edge proxy setting is not supported by PyVD.")
    gallery_dir = workdir / "gallery"
    gallery_dir.mkdir()
    cookie = job_cookie_file(settings, request.extractor_id, workdir)
    command = [
        sys.executable, "-m", "gallery_dl", "--config-ignore", "--quiet",
        "--destination", str(gallery_dir), "--range", "1-21",
        "--filesize-max", str(settings.max_file_size), "--write-metadata",
    ]
    if request.extractor_id == "instagram":
        command.extend(["-o", "extractor.instagram.videos=merged"])
    proxy = site.download_proxy or site.proxy or settings.proxy
    if proxy and not site.disable_proxy:
        command.extend(["--proxy", proxy])
    if cookie:
        command.extend(["--cookies", str(cookie)])
    command.append(request.url)
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    output_task = asyncio.create_task(process.communicate())
    try:
        while not output_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(output_task), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            for path in gallery_dir.rglob("*"):
                if path.is_file() and path.stat().st_size > settings.max_file_size:
                    process.kill()
                    await output_task
                    raise FileTooLarge("The file exceeds the 2 GB limit.")
            if shutil.disk_usage(workdir).free < 512_000_000:
                process.kill()
                await output_task
                raise NoMedia("Not enough free disk space for this download.")
        _, stderr = await output_task
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    paths = files_in(gallery_dir)
    if not paths:
        if request.extractor_id == "instagram" and b"redirect to login page" in stderr.lower():
            raise AuthenticationRequired(
                "Instagram redirected PyVD to login. Refresh the Instagram cookies."
            )
        raise NoMedia("No media was found by the gallery extractor.")
    if len(paths) > 20:
        raise MediaError("The post contains more than 20 media items.")
    media = Media(
        request.extractor_id, request.content_id, request.url,
        caption=_caption(gallery_dir), nsfw=_marked_nsfw(gallery_dir),
    )
    for path in paths:
        suffix = path.suffix.lower()
        kind = "photo" if suffix in PHOTO_SUFFIXES else "audio" if suffix in AUDIO_SUFFIXES else "video"
        size = path.stat().st_size
        if size > settings.max_file_size:
            raise FileTooLarge("The file exceeds the 2 GB limit.")
        media.items.append(MediaItem(kind=kind, path=path, size=size, title=path.stem))
    return media
