"""Extract a direct video source and remux it to a fragmented MP4 pipe."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from internal.config.settings import load_settings
from internal.extractors.cookies import job_cookie_file
from internal.extractors.downloader import DEFAULT_FORMAT, YOUTUBE_FORMAT


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
    if _source(info, video=True) and (info.get("acodec") or "").startswith("mp4a"):
        return [info]
    return None


def _copy_source(fmt: dict, fifo: Path, proxy: str, errors: list[str]) -> None:
    from curl_cffi import requests

    try:
        # Open first so FFmpeg cannot wait forever if the HTTP request fails.
        with fifo.open("wb", buffering=0) as output:
            with requests.Session() as session:
                response = session.get(
                    fmt["url"], headers=fmt.get("http_headers") or {},
                    proxy=proxy or None, stream=True, timeout=120,
                )
                try:
                    response.raise_for_status()
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            output.write(chunk)
                finally:
                    response.close()
    except Exception as exc:
        errors.append(type(exc).__name__)


def main() -> int:
    import yt_dlp

    job = json.loads(sys.stdin.readline())
    settings = load_settings(Path(job["root"]))
    site = settings.site(job["extractor_id"])
    workdir = Path(job["workdir"])
    if site.edge_proxy or job_cookie_file(settings, job["extractor_id"], workdir):
        print('{"available":false}', flush=True)
        return 0
    proxy = site.download_proxy or ("" if site.disable_proxy else site.proxy or settings.proxy)
    options = {
        "format": YOUTUBE_FORMAT if job["extractor_id"] == "youtube" else DEFAULT_FORMAT,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    if job["extractor_id"] == "youtube":
        options["js_runtimes"] = {"deno": {}, "node": {}}
    if proxy:
        options["proxy"] = proxy
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(job["url"], download=False)
    except Exception:
        print('{"available":false}', flush=True)
        return 0
    if not info or info.get("entries") is not None:
        print('{"available":false}', flush=True)
        return 0
    formats = _formats(info)
    if not formats or int(info.get("duration") or 0) > settings.max_duration:
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
    command.extend(["-map", "0:v:0", "-map", "1:a:0" if len(fifos) == 2 else "0:a:0"])
    command.extend(["-c", "copy", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"])
    errors: list[str] = []
    with subprocess.Popen(command, stdout=sys.stdout.buffer, stderr=subprocess.DEVNULL) as ffmpeg:
        workers = [threading.Thread(target=_copy_source, args=(fmt, fifo, proxy, errors), daemon=True)
                   for fmt, fifo in zip(formats, fifos)]
        for worker in workers:
            worker.start()
        result = ffmpeg.wait()
    return 0 if result == 0 and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
