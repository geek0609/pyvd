"""Extract a direct video source and remux it to a fragmented MP4 pipe."""

import copy
import errno
import json
import logging
import os
import select
import subprocess
import sys
import threading
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlsplit

from internal.config.settings import load_settings
from internal.extractors.cookies import job_cookie_file
from internal.extractors.downloader import DEFAULT_FORMAT, H264_FORMAT, _SilentYtdlpLogger


def _source(fmt: dict, *, video: bool) -> bool:
    codec = fmt.get("vcodec" if video else "acodec") or ""
    return (
        isinstance(fmt.get("url"), str)
        and urlsplit(fmt["url"]).scheme == "https"
        and not fmt.get("fragments")
        and fmt.get("protocol") in {"https", "http"}
        and fmt.get("ext") in {"mp4", "m4a"}
        and codec.startswith("avc1" if video else "mp4a")
    )


def _formats(info: dict) -> list[dict] | None:
    selected = info.get("requested_formats")
    if selected:
        if len(selected) != 2 or not _source(selected[0], video=True) or not _source(selected[1], video=False):
            return None
        return selected
    if _source(info, video=True) and (
        (info.get("acodec") or "").startswith("mp4a") or info.get("acodec") == "none"
    ):
        return [info]
    return None


def _copy_cookies(cookies: CookieJar) -> CookieJar:
    copied = CookieJar()
    for cookie in cookies:
        copied.set_cookie(copy.copy(cookie))
    return copied


def _extract_ytdlp(job: dict, settings, cookie: Path | None, proxy: str) -> tuple[dict | None, CookieJar]:
    import yt_dlp

    options = {
        "format": H264_FORMAT if job["extractor_id"] == "youtube" else DEFAULT_FORMAT,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "logger": _SilentYtdlpLogger(),
    }
    if job["extractor_id"] == "youtube":
        options["js_runtimes"] = {"deno": {}, "node": {}}
    if cookie:
        options["cookiefile"] = str(cookie)
    if proxy:
        options["proxy"] = proxy
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(job["url"], download=False)
        return info, _copy_cookies(ydl.cookiejar)


def _extract_instagram(job: dict, cookie: Path | None, proxy: str) -> tuple[list, CookieJar]:
    from gallery_dl import config
    from gallery_dl.job import DataJob

    config.clear()
    config.set(("output",), "private", True)
    config.set(("extractor", "instagram"), "videos", "merged")
    if cookie:
        config.set(("extractor",), "cookies", str(cookie))
    if proxy:
        config.set(("extractor",), "proxy", proxy)
    extraction = DataJob(job["url"], file=None)
    extraction.run()
    if extraction.exception is not None or extraction.extractor.status:
        raise RuntimeError("Instagram media extraction failed")
    headers = dict(extraction.extractor.session.headers)
    for message in extraction.data:
        if message[0] == 3:
            metadata = message[2]
            metadata["_http_headers"] = {**headers, **(metadata.get("_http_headers") or {})}
    return extraction.data, _copy_cookies(extraction.extractor.session.cookies)


def _gallery_nsfw(value: object) -> bool:
    if isinstance(value, dict):
        if any(value.get(key) is True for key in ("over_18", "nsfw", "is_nsfw", "sensitive", "possibly_sensitive")):
            return True
        try:
            if float(value.get("age_limit") or 0) >= 18:
                return True
        except (TypeError, ValueError):
            pass
        return any(_gallery_nsfw(entry) for entry in value.values())
    if isinstance(value, (list, tuple)):
        return any(_gallery_nsfw(entry) for entry in value)
    return False


def _instagram_video(data: list) -> tuple[dict, list[dict]] | None:
    posts = [message for message in data if message[0] == 2]
    sources = [message for message in data if message[0] == 3]
    if len(posts) != 1 or len(sources) != 1 or any(message[0] not in {2, 3} for message in data):
        return None
    _, url, metadata = sources[0]
    if (
        not isinstance(url, str) or urlsplit(url).scheme != "https"
        or not urlsplit(url).path.lower().endswith(".mp4")
        or metadata.get("video_url") != url or metadata.get("_ytdl_manifest")
    ):
        return None
    info = {
        "description": metadata.get("description") or metadata.get("caption") or "",
        "title": metadata.get("title") or metadata.get("post_shortcode") or "",
        "uploader": metadata.get("username") or "",
        "duration": metadata.get("duration") or 0,
        "width": metadata.get("width") or 0,
        "height": metadata.get("height") or 0,
        "format_id": "merged",
        "age_limit": 18 if _gallery_nsfw(data) else 0,
    }
    source = {"url": url, "http_headers": metadata.get("_http_headers") or {}}
    return info, [source]


def _copy_source(
    fmt: dict, fifo: Path, proxy: str, cookies: CookieJar,
    stopped: threading.Event, errors: list[str],
) -> None:
    from curl_cffi import requests

    descriptor = None
    try:
        while not stopped.is_set():
            try:
                descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise
                stopped.wait(0.05)
        if descriptor is None:
            return

        def write(chunk: bytes) -> int:
            pending = memoryview(chunk)
            while pending:
                if stopped.is_set():
                    raise RuntimeError("Video source copying stopped")
                try:
                    written = os.write(descriptor, pending)
                except BlockingIOError:
                    select.select([], [descriptor], [], 0.1)
                else:
                    pending = pending[written:]
            return len(chunk)

        # A callback applies pipe backpressure directly without buffering the full response.
        with requests.Session(cookies=_copy_cookies(cookies), trust_env=False) as session:
            response = session.get(
                fmt["url"], headers=fmt.get("http_headers") or {},
                proxy=proxy or None, content_callback=write, timeout=None,
            )
            try:
                response.raise_for_status()
            finally:
                response.close()
    except Exception as exc:
        errors.append(type(exc).__name__)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main() -> int:
    job = json.loads(sys.stdin.readline())
    settings = load_settings(Path(job["root"]))
    site = settings.site(job["extractor_id"])
    workdir = Path(job["workdir"])
    if site.edge_proxy:
        print('{"available":false}', flush=True)
        return 0
    logging.disable(logging.CRITICAL)
    cookie = job_cookie_file(settings, job["extractor_id"], workdir)
    proxy = site.download_proxy or ("" if site.disable_proxy else site.proxy or settings.proxy)
    try:
        if job["extractor_id"] == "instagram":
            gallery_data, cookies = _extract_instagram(job, cookie, proxy)
            selected = _instagram_video(gallery_data)
            info, formats = selected if selected else (None, None)
        else:
            info, cookies = _extract_ytdlp(job, settings, cookie, proxy)
            formats = _formats(info) if info and info.get("entries") is None else None
    except Exception:
        print('{"available":false}', flush=True)
        return 0
    if not info or not formats or int(info.get("duration") or 0) > settings.max_duration:
        print('{"available":false}', flush=True)
        return 0

    header = {
        "available": True,
        "caption": str(info.get("description") or info.get("title") or "")[:4000],
        "nsfw": bool(info.get("age_limit") and info["age_limit"] >= 18),
        "duration": int(info.get("duration") or 0),
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "title": str(info.get("title") or "")[:500],
        "artist": str(info.get("artist") or info.get("uploader") or "")[:500],
        "format_id": str(info.get("format_id") or "default")[:100],
    }
    fifos = [workdir / f"source-{index}.fifo" for index in range(len(formats))]
    for fifo in fifos:
        os.mkfifo(fifo, 0o600)
    print(json.dumps(header), flush=True)

    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    for fifo in fifos:
        command.extend(["-i", str(fifo)])
    command.extend(["-map", "0:v:0", "-map", "1:a:0" if len(fifos) == 2 else "0:a:0?"])
    command.extend(["-c", "copy", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"])
    errors: list[str] = []
    stopped = threading.Event()
    workers = []
    result = 1
    try:
        with subprocess.Popen(command, stdout=sys.stdout.buffer, stderr=subprocess.DEVNULL) as ffmpeg:
            workers = [threading.Thread(
                target=_copy_source, args=(fmt, fifo, proxy, cookies, stopped, errors), daemon=True,
            ) for fmt, fifo in zip(formats, fifos)]
            for worker in workers:
                worker.start()
            result = ffmpeg.wait()
            if result:
                stopped.set()
            for worker in workers:
                worker.join(timeout=5)
    finally:
        stopped.set()
        for worker in workers:
            worker.join(timeout=1)
        for fifo in fifos:
            fifo.unlink(missing_ok=True)
    return 0 if result == 0 and not errors and not any(worker.is_alive() for worker in workers) else 1


if __name__ == "__main__":
    raise SystemExit(main())
