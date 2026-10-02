"""Keep extracted source details only for the lifetime of one download job."""

import json
import os
from datetime import date, datetime
from http.cookiejar import CookieJar, MozillaCookieJar
from pathlib import Path

from internal.extractors.sites import Request


def _json_value(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("Unsupported extraction metadata")


def save_extraction(
    workdir: Path, request: Request, backend: str, info: object,
    cookies: CookieJar | None = None,
) -> None:
    payload = {
        "extractor_id": request.extractor_id, "content_id": request.content_id,
        "url": request.url, "backend": backend, "info": info,
    }
    temporary = workdir / "extracted.json.tmp"
    try:
        data = json.dumps(payload, default=_json_value)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(data)
        if cookies is not None:
            cookie_path = workdir / "cookies.txt"
            jar = MozillaCookieJar(str(cookie_path))
            for cookie in cookies:
                jar.set_cookie(cookie)
            descriptor = os.open(cookie_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(descriptor)
            jar.save(ignore_discard=True, ignore_expires=True)
        temporary.replace(workdir / "extracted.json")
    except (OSError, TypeError, ValueError):
        # Metadata reuse is optional; ordinary downloading remains available.
        temporary.unlink(missing_ok=True)


def load_extraction(workdir: Path, request: Request, backend: str) -> object | None:
    try:
        payload = json.loads((workdir / "extracted.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in (
            ("extractor_id", request.extractor_id), ("content_id", request.content_id),
            ("url", request.url), ("backend", backend),
        )
    ):
        return None
    info = payload.get("info")
    if backend == "yt-dlp":
        return info if isinstance(info, dict) else None
    if backend == "gallery":
        return info if isinstance(info, list) else None
    return None
