"""Download supported posts with yt-dlp, preserving govd cookie files."""

import asyncio
import logging
from pathlib import Path
from typing import Any

from internal.config.settings import Settings
from internal.core.errors import DurationTooLong, FileTooLarge, MediaError, NoMedia
from internal.extractors.cookies import job_cookie_file
from internal.extractors.gallery import download_gallery, prefer_gallery
from internal.extractors.sites import Request
from internal.models.media import Media, MediaItem


LOG = logging.getLogger(__name__)


class _YtdlpLogger:
    def debug(self, message: str) -> None:
        LOG.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        LOG.info("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        LOG.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        LOG.error("yt-dlp: %s", message)


def _entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    if info.get("entries") is None:
        return [info]
    result: list[dict[str, Any]] = []
    for entry in info["entries"]:
        if entry:
            result.extend(_entries(entry))
    return result


def _paths(info: dict[str, Any], workdir: Path) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    seen: set[Path] = set()
    for entry in _entries(info):
        for download in entry.get("requested_downloads") or []:
            candidate = download.get("filepath") or download.get("_filename")
            if not candidate:
                continue
            path = Path(candidate).resolve()
            if path.is_file() and path.is_relative_to(workdir.resolve()) and path not in seen:
                found.append((path, entry))
                seen.add(path)
    if found:
        return found
    # Some extractors do not populate requested_downloads after postprocessing.
    for path in sorted(workdir.iterdir()):
        if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".json", ".txt"}:
            found.append((path, info))
    return found


def _kind(path: Path, site: str, entry: dict[str, Any]) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}:
        return "photo"
    if site == "soundcloud" or suffix in {".mp3", ".m4a", ".flac", ".ogg", ".opus"}:
        return "audio"
    if entry.get("vcodec") == "none" and entry.get("acodec") != "none":
        return "audio"
    return "video"


def _download(request: Request, settings: Settings, workdir: Path) -> Media:
    import yt_dlp

    site = settings.site(request.extractor_id)
    cookie = job_cookie_file(settings, request.extractor_id, workdir)

    def progress(update: dict[str, Any]) -> None:
        if update.get("status") == "downloading":
            expected = update.get("total_bytes") or 0
            current = update.get("downloaded_bytes") or 0
            if expected > settings.max_file_size or current > settings.max_file_size:
                raise FileTooLarge("The file exceeds the 2 GB limit.")

    def duration_filter(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
        duration = info.get("duration")
        if duration and duration > settings.max_duration:
            return "The media is longer than the configured duration limit."
        return None

    options: dict[str, Any] = {
        "outtmpl": str(workdir / "%(autonumber)03d-%(id).80B.%(ext)s"),
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlistend": 21,
        "max_filesize": settings.max_file_size,
        "match_filter": duration_filter,
        "progress_hooks": [progress],
        "logger": _YtdlpLogger(),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "ignoreerrors": False,
    }
    if request.extractor_id == "youtube":
        options["format"] = (
            "bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a]/"
            "b[ext=mp4][vcodec^=avc1]/"
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best"
        )
        options["js_runtimes"] = {"deno": {}, "node": {}}
    if cookie:
        options["cookiefile"] = str(cookie)
    proxy = site.proxy or settings.proxy
    if not site.disable_proxy and proxy:
        options["proxy"] = proxy
    if site.download_proxy:
        options["proxy"] = site.download_proxy
    if site.edge_proxy:
        raise MediaError("This site's edge proxy setting is not supported by PyVD.")
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(request.url, download=True)
    except FileTooLarge:
        raise
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc)
        if "larger than max-filesize" in message.lower():
            raise FileTooLarge("The file exceeds the 2 GB limit.") from exc
        if "longer than" in message.lower():
            raise DurationTooLong("The media exceeds the duration limit.") from exc
        raise NoMedia("Could not download media from this link.") from exc
    if not info:
        raise NoMedia("No media was found at this link.")
    paths = _paths(info, workdir)
    if not paths:
        raise NoMedia("No media file was downloaded.")
    if len(paths) > 20:
        raise MediaError("The post contains more than 20 media items.")
    media = Media(request.extractor_id, request.content_id, request.url)
    media.caption = str(info.get("description") or info.get("title") or "")
    media.nsfw = bool(info.get("age_limit") and info["age_limit"] >= 18)
    for path, entry in paths:
        size = path.stat().st_size
        if size > settings.max_file_size:
            raise FileTooLarge("The file exceeds the 2 GB limit.")
        duration = int(entry.get("duration") or 0)
        if duration > settings.max_duration:
            raise DurationTooLong("The media exceeds the duration limit.")
        media.items.append(MediaItem(
            kind=_kind(path, request.extractor_id, entry), path=path,
            format_id=str(entry.get("format_id") or "default"), size=size,
            duration=duration, width=int(entry.get("width") or 0),
            height=int(entry.get("height") or 0),
            title=str(entry.get("title") or ""),
            artist=str(entry.get("artist") or entry.get("uploader") or ""),
        ))
    return media


def _download_in_process(request: Request, settings: Settings, workdir: Path) -> Media:
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as pool:
        return pool.submit(_download, request, settings, workdir).result()


async def download(request: Request, settings: Settings, workdir: Path) -> Media:
    if prefer_gallery(request):
        try:
            return await download_gallery(request, settings, workdir)
        except NoMedia:
            LOG.info("gallery extraction unavailable for %s; trying yt-dlp", request.extractor_id)
    return await asyncio.to_thread(_download_in_process, request, settings, workdir)
