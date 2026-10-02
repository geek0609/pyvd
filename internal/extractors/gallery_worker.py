"""Download previously extracted Instagram URLs without fetching the post again."""

import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlsplit

from internal.config.settings import Settings, load_settings
from internal.extractors.extracted import load_extraction
from internal.extractors.sites import Request


def replay_messages(info: object) -> list[tuple] | None:
    if not isinstance(info, list):
        return None
    messages = []
    have_directory = False
    urls = 0
    for record in info:
        if not isinstance(record, (list, tuple)):
            return None
        if len(record) == 2 and record[0] == 2 and isinstance(record[1], dict):
            messages.append((2, "", record[1].copy()))
            have_directory = True
        elif (
            len(record) == 3 and record[0] == 3 and have_directory
            and isinstance(record[1], str) and isinstance(record[2], dict)
        ):
            try:
                source = urlsplit(record[1])
            except ValueError:
                return None
            if source.scheme not in {"http", "https"} or not source.hostname:
                return None
            messages.append((3, record[1], record[2].copy()))
            urls += 1
        else:
            return None
    return messages if 1 <= urls <= 21 else None


def download_prepared(
    request: Request, settings: Settings, workdir: Path, messages: list[tuple],
) -> int:
    from gallery_dl import config, extractor
    from gallery_dl.job import DownloadJob

    config.clear()
    config.set((), "base-directory", str(workdir / "gallery"))
    config.set((), "file-range", "1-21")
    config.set((), "filesize-max", settings.max_file_size)
    config.set((), "postprocessors", ["metadata"])
    config.set(("extractor", "instagram"), "videos", "merged")
    config.set(("output",), "mode", False)
    config.set(("downloader", "http"), "progress", False)
    site = settings.site(request.extractor_id)
    proxy = site.download_proxy or site.proxy or settings.proxy
    if proxy and not site.disable_proxy:
        config.set((), "proxy", proxy)
    cookie = workdir / "cookies.txt"
    if cookie.is_file():
        config.set((), "cookies", str(cookie))
    original = extractor.find(request.url)
    if original is None or original.category != "instagram":
        return 1
    original.items = lambda: iter(messages)
    return DownloadJob(original).run()


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        job = json.loads(sys.stdin.readline())
        workdir = Path(job["workdir"])
        request = Request(job["extractor_id"], job["content_id"], job["url"])
        if request.extractor_id != "instagram":
            return 1
        messages = replay_messages(load_extraction(workdir, request, "gallery"))
        if messages is None:
            return 1
        settings = load_settings(Path(job["root"]))
        if settings.site(request.extractor_id).edge_proxy:
            return 1
        return download_prepared(request, settings, workdir, messages)
    except Exception:
        sys.stderr.write("Could not download prepared Instagram media.\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
