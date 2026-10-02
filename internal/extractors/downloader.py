"""Download supported posts with yt-dlp, preserving govd cookie files."""

import asyncio
import json
from pathlib import Path
from typing import Any

from internal.config.settings import Settings
from internal.core.errors import (
    AuthenticationRequired, DurationTooLong, FileTooLarge, MediaError, NoMedia, SessionCheckRequired,
)
from internal.extractors.cookies import job_cookie_file
from internal.extractors.gallery import download_gallery, prefer_gallery
from internal.extractors.sites import Request
from internal.models.media import Media, MediaItem


DEFAULT_FORMAT = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best"
H264_FORMAT = (
    "bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a]/"
    "b[ext=mp4][vcodec^=avc1]/"
    "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best"
)
INSTAGRAM_FORMAT = "b[ext=mp4]/" + H264_FORMAT


class _SilentYtdlpLogger:
    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    if info.get("entries") is None:
        return [info]
    result: list[dict[str, Any]] = []
    for entry in info["entries"]:
        if entry:
            result.extend(_entries(entry))
    return result


def _marked_nsfw(info: dict[str, Any]) -> bool:
    for entry in (info, *_entries(info)):
        try:
            if int(entry.get("age_limit") or 0) >= 18:
                return True
        except (TypeError, ValueError):
            continue
    return False


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


def _instagram_auth_error(error: Exception) -> AuthenticationRequired | None:
    from yt_dlp.networking.exceptions import HTTPError

    details = getattr(error, "exc_info", None)
    if not details:
        return None
    cause = details[1]
    if not isinstance(cause, HTTPError):
        cause = getattr(cause, "cause", None)
    if not isinstance(cause, HTTPError) or cause.status not in {400, 401, 403}:
        return None
    try:
        body = json.loads(cause.response.read(65_536))
    except Exception:
        return None
    message = body.get("message") if isinstance(body, dict) else None
    if message in ("checkpoint_required", "challenge_required"):
        return SessionCheckRequired(
            "Instagram requires an account security check. The bot owner must "
            "complete it and refresh the Instagram cookies."
        )
    if message == "login_required":
        return AuthenticationRequired("Instagram rejected PyVD's login session. Refresh the Instagram cookies.")
    return None


def _download(request: Request, settings: Settings, workdir: Path, use_cookies: bool = True) -> Media:
    import yt_dlp

    site = settings.site(request.extractor_id)
    cookie = job_cookie_file(settings, request.extractor_id, workdir) if use_cookies else None

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
        "format": DEFAULT_FORMAT,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlistend": 21,
        "max_filesize": settings.max_file_size,
        "match_filter": duration_filter,
        "progress_hooks": [progress],
        "logger": _SilentYtdlpLogger(),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "ignoreerrors": False,
    }
    if request.extractor_id == "instagram":
        options["format"] = INSTAGRAM_FORMAT
    if request.extractor_id == "youtube":
        options["format"] = H264_FORMAT
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
        if request.extractor_id == "instagram":
            auth_error = _instagram_auth_error(exc)
            if auth_error is not None:
                raise auth_error from exc
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
    media.nsfw = _marked_nsfw(info)
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


def _download_in_process(request: Request, settings: Settings, workdir: Path, use_cookies: bool = True) -> Media:
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as pool:
        return pool.submit(_download, request, settings, workdir, use_cookies).result()


async def download(request: Request, settings: Settings, workdir: Path) -> Media:
    auth_error: AuthenticationRequired | None = None
    if prefer_gallery(request):
        try:
            return await download_gallery(request, settings, workdir)
        except AuthenticationRequired as exc:
            auth_error = exc
        except NoMedia:
            pass
    try:
        return await asyncio.to_thread(_download_in_process, request, settings, workdir)
    except SessionCheckRequired as checkpoint:
        try:
            return await asyncio.to_thread(_download_in_process, request, settings, workdir, False)
        except NoMedia as exc:
            raise checkpoint from exc
    except NoMedia:
        if auth_error is not None:
            raise auth_error
        if request.extractor_id == "twitter":
            return await download_gallery(request, settings, workdir)
        raise
