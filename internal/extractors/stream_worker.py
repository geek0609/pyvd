"""Extract a direct video source and remux it to a fragmented MP4 pipe."""

import copy
import json
import logging
import os
import re
import subprocess
import sys
import threading
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlsplit

from internal.config.settings import load_settings
from internal.extractors.cookies import job_cookie_file
from internal.extractors.downloader import DEFAULT_FORMAT, H264_FORMAT, _SilentYtdlpLogger, _entries
from internal.extractors.extracted import save_extraction
from internal.extractors.sites import Request
from internal.extractors.source import PipeWriter, copy_source as _copy_http_source


SEGMENT_PROTOCOLS = frozenset({"m3u8", "m3u8_native", "http_dash_segments"})
MAX_FRAGMENT_SIZE = 32 * 1024 * 1024
MAX_MANIFEST_SIZE = 2 * 1024 * 1024


def _finite(info: dict) -> bool:
    return not (
        info.get("is_live") or info.get("is_from_start") or info.get("has_drm")
        or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}
    )


def _source(fmt: dict, *, video: bool) -> bool:
    codec = fmt.get("vcodec" if video else "acodec") or ""
    if not (
        isinstance(fmt.get("url"), str)
        and urlsplit(fmt["url"]).scheme == "https"
        and fmt.get("ext") in {"mp4", "m4a"}
        and codec.startswith("avc1" if video else "mp4a")
        and _finite(fmt)
    ):
        return False
    protocol = fmt.get("protocol")
    if protocol in {"https", "http"}:
        return not fmt.get("fragments")
    if protocol in {"m3u8", "m3u8_native"}:
        return not fmt.get("hls_aes")
    if protocol == "http_dash_segments":
        fragments = fmt.get("fragments")
        return isinstance(fragments, list) and bool(fragments) and all(
            isinstance(fragment, dict) and (fragment.get("url") or fragment.get("path"))
            and not fragment.get("decrypt_info")
            for fragment in fragments
        )
    return False


def _formats(info: dict) -> list[dict] | None:
    if not _finite(info):
        return None
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
    if job.get("count_only"):
        options.update(socket_timeout=3, retries=0, extractor_retries=0)
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
    config.set(("extractor",), "metadata-path", False)
    config.set(("extractor",), "metadata-extractor", False)
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


def _hls_manifest(manifest: str, info: dict) -> bool:
    from yt_dlp.downloader.hls import HlsFD

    return (
        len(manifest.encode("utf-8")) <= MAX_MANIFEST_SIZE
        and _finite(info)
        and "#EXTM3U" in manifest and "#EXT-X-ENDLIST" in manifest
        and "#EXT-X-STREAM-INF" not in manifest and "#EXT-X-I-FRAME-STREAM-INF" not in manifest
        and not any(
            re.search(r"\bMETHOD\s*=\s*NONE\b", line) is None
            for line in map(str.strip, manifest.splitlines())
            if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:"))
        )
        and HlsFD.can_download(manifest, info)
    )


def _copy_segments(
    fmt: dict, fifo: Path, proxy: str, cookies: CookieJar,
    stopped: threading.Event, errors: list[str],
) -> None:
    import yt_dlp
    from yt_dlp.downloader.dash import DashSegmentsFD
    from yt_dlp.downloader.hls import HlsFD
    from yt_dlp.networking import Request as SourceRequest

    options = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "logger": _SilentYtdlpLogger(), "external_downloader": "native",
        "nopart": True, "continuedl": False, "_no_ytdl_file": True,
        "updatetime": False, "skip_unavailable_fragments": False,
        "concurrent_fragment_downloads": 1, "fragment_retries": 3, "retries": 3,
        "max_filesize": MAX_FRAGMENT_SIZE, "buffersize": 64 * 1024,
        "noresizebuffer": True, "socket_timeout": 30,
    }
    if proxy:
        options["proxy"] = proxy
    source = dict(fmt)
    source.setdefault("id", "source")
    try:
        # Opening before network requests guarantees FFmpeg receives EOF if extraction fails.
        with PipeWriter(fifo, stopped) as output, yt_dlp.YoutubeDL(options) as ydl:
            ydl.cookiejar = _copy_cookies(cookies)
            if fmt["protocol"] in {"m3u8", "m3u8_native"}:
                manifest = fmt.get("hls_media_playlist_data")
                if not manifest:
                    with ydl.urlopen(SourceRequest(fmt["url"], headers=fmt.get("http_headers") or {})) as response:
                        raw_manifest = response.read(MAX_MANIFEST_SIZE + 1)
                        if len(raw_manifest) > MAX_MANIFEST_SIZE:
                            raise RuntimeError("The media playlist is too large to stream")
                        manifest = raw_manifest.decode("utf-8", errors="replace")
                        source["url"] = response.url
                if not _hls_manifest(manifest, source):
                    raise RuntimeError("This playlist requires completed downloading")
                source["hls_media_playlist_data"] = manifest
                base = HlsFD
            else:
                base = DashSegmentsFD

            class NativePipe(base):
                def sanitize_open(self, filename, mode):
                    if filename == str(fifo):
                        return output, filename
                    return super().sanitize_open(filename, mode)

                def filesize_or_none(self, filename):
                    return output.bytes_written if filename == str(fifo) else super().filesize_or_none(filename)

                def _prepare_frag_download(self, context):
                    super()._prepare_frag_download(context)

                    def fragment_progress(update):
                        if stopped.is_set():
                            raise RuntimeError("Fragment copying stopped")
                        if max(update.get("downloaded_bytes") or 0, update.get("total_bytes") or 0) > MAX_FRAGMENT_SIZE:
                            raise RuntimeError("The fragment is too large to stream")

                    context["dl"].add_progress_hook(fragment_progress)

                def _read_fragment(self, context):
                    filename = context.get("fragment_filename_sanitized")
                    if filename and Path(filename).stat().st_size > MAX_FRAGMENT_SIZE:
                        raise RuntimeError("The fragment is too large to stream")
                    return super()._read_fragment(context)

                def _download_fragment(self, context, url, info, headers=None, request_data=None):
                    if stopped.is_set():
                        raise RuntimeError("Fragment copying stopped")
                    return super()._download_fragment(context, url, info, headers, request_data)

            downloader = NativePipe(ydl, options)
            result, _ = downloader.download(str(fifo), source)
            if not result:
                raise RuntimeError("A media fragment could not be downloaded")
    except Exception as exc:
        errors.append(type(exc).__name__)
    finally:
        for fragment in fifo.parent.glob(fifo.name + "-Frag*"):
            fragment.unlink(missing_ok=True)


def _copy_source(
    fmt: dict, fifo: Path, proxy: str, cookies: CookieJar,
    stopped: threading.Event, errors: list[str],
) -> None:
    copier = _copy_segments if fmt.get("protocol") in SEGMENT_PROTOCOLS else _copy_http_source
    copier(fmt, fifo, proxy, cookies, stopped, errors)


def _remux_command(fifos: list[Path], formats: list[dict]) -> list[str]:
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    for fifo in fifos:
        command.extend(["-i", str(fifo)])
    command.extend(["-map", "0:v:0", "-map", "1:a:0" if len(fifos) == 2 else "0:a:0?"])
    command.extend(["-c", "copy"])
    if any(fmt.get("protocol") in {"m3u8", "m3u8_native"} for fmt in formats):
        # AAC in transport-stream fragments needs its framing converted for MP4.
        command.extend(["-bsf:a", "aac_adtstoasc"])
    command.extend(["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"])
    return command


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
    request = Request(job["extractor_id"], job["content_id"], job["url"])
    if job.get("count_only"):
        count = None
        try:
            info, _ = _extract_ytdlp(job, settings, cookie, proxy)
            if info:
                count = min(20, len(_entries(info)))
        except Exception:
            pass
        print(json.dumps({"count": count}), flush=True)
        return 0
    try:
        if job["extractor_id"] == "instagram":
            gallery_data, cookies = _extract_instagram(job, cookie, proxy)
            save_extraction(workdir, request, "gallery", gallery_data, cookies)
            selected = _instagram_video(gallery_data)
            info, formats = selected if selected else (None, None)
        else:
            info, cookies = _extract_ytdlp(job, settings, cookie, proxy)
            if info:
                import yt_dlp

                save_extraction(workdir, request, "yt-dlp", yt_dlp.YoutubeDL.sanitize_info(info), cookies)
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

    command = _remux_command(fifos, formats)
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
