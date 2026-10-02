import json
import stat
from datetime import datetime, timezone
from http.cookiejar import Cookie, CookieJar, MozillaCookieJar
from pathlib import Path

import pytest

from internal.extractors.extracted import load_extraction, save_extraction
from internal.extractors.sites import Request


def test_extraction_is_private_and_bound_to_one_request(tmp_path: Path) -> None:
    request = Request("youtube", "video", "https://youtu.be/video")
    info = {"id": "video", "url": "https://media.example/video?token=private"}
    save_extraction(tmp_path, request, "yt-dlp", info)

    assert load_extraction(tmp_path, request, "yt-dlp") == info
    assert stat.S_IMODE((tmp_path / "extracted.json").stat().st_mode) == 0o600
    assert load_extraction(tmp_path, request, "gallery") is None
    assert load_extraction(tmp_path, Request("youtube", "other", request.url), "yt-dlp") is None
    assert load_extraction(tmp_path, Request("twitter", "video", request.url), "yt-dlp") is None
    assert load_extraction(tmp_path, Request("youtube", "video", "https://youtu.be/other"), "yt-dlp") is None
    assert not (tmp_path / "extracted.json.tmp").exists()


@pytest.mark.parametrize("data", ["not json", "[]", "null", "{}"])
def test_invalid_extraction_uses_normal_downloading(tmp_path: Path, data: str) -> None:
    (tmp_path / "extracted.json").write_text(data)
    assert load_extraction(tmp_path, Request("youtube", "video", "https://youtu.be/video"), "yt-dlp") is None


def test_gallery_dates_can_be_saved_without_exposing_private_details(tmp_path: Path) -> None:
    request = Request("instagram", "reel", "https://instagram.com/reel/reel/")
    data = [(2, {"date": datetime(2026, 10, 2, tzinfo=timezone.utc)})]
    save_extraction(tmp_path, request, "gallery", data)
    loaded = load_extraction(tmp_path, request, "gallery")
    assert loaded == [[2, {"date": "2026-10-02T00:00:00+00:00"}]]
    assert json.loads((tmp_path / "extracted.json").read_text())["backend"] == "gallery"


def test_unserializable_metadata_does_not_prevent_normal_downloading(tmp_path: Path) -> None:
    request = Request("youtube", "video", "https://youtu.be/video")
    save_extraction(tmp_path, request, "yt-dlp", {"unexpected": object()})
    assert load_extraction(tmp_path, request, "yt-dlp") is None
    assert not (tmp_path / "extracted.json.tmp").exists()


def test_refreshed_cookies_remain_scoped_and_private(tmp_path: Path) -> None:
    request = Request("instagram", "reel", "https://instagram.com/reel/reel/")
    cookies = CookieJar()
    cookies.set_cookie(Cookie(
        version=0, name="session", value="job-cookie", port=None, port_specified=False,
        domain=".instagram.com", domain_specified=True, domain_initial_dot=True,
        path="/", path_specified=True, secure=True, expires=None, discard=True,
        comment=None, comment_url=None, rest={"HttpOnly": None},
    ))
    save_extraction(tmp_path, request, "gallery", [[2, {}]], cookies)
    path = tmp_path / "cookies.txt"
    loaded = MozillaCookieJar(str(path))
    loaded.load(ignore_discard=True, ignore_expires=True)
    assert [(cookie.domain, cookie.path, cookie.secure, cookie.value) for cookie in loaded] == [
        (".instagram.com", "/", True, "job-cookie"),
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
