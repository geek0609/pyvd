"""Run one yt-dlp job in a process that can be stopped with its children."""

import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

from internal.config.settings import load_settings
from internal.core.errors import MediaError
from internal.extractors.downloader import _download
from internal.extractors.sites import Request


def main() -> int:
    logging.disable(logging.CRITICAL)
    job = json.loads(sys.stdin.readline())
    workdir = Path(job["workdir"])
    try:
        settings = load_settings(Path(job["root"]))
        request = Request(job["extractor_id"], job["content_id"], job["url"])
        media = _download(request, settings, workdir, job["use_cookies"])
        result = {"media": asdict(media)}
    except MediaError as exc:
        result = {"error": type(exc).__name__, "message": str(exc)}
    except Exception:
        result = {"error": "NoMedia", "message": "Could not download media from this link."}
    descriptor = os.open(workdir / "download-result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(result, output, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
