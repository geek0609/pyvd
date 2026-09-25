from pathlib import Path
from types import SimpleNamespace

from internal.extractors.cookies import job_cookie_file
from internal.extractors.sites import first_supported_url, identify


def test_govd_site_ids() -> None:
    cases = {
        "https://youtu.be/YE7VzlLtp-4": ("youtube", "YE7VzlLtp-4"),
        "https://www.youtube.com/watch?v=1qLvAoo33UQ": ("youtube", "1qLvAoo33UQ"),
        "https://www.youtube.com/shorts/Z9NxQdQ8Rpg": ("youtube", "Z9NxQdQ8Rpg"),
        "https://www.instagram.com/p/Cabc123/": ("instagram", "Cabc123"),
        "https://www.tiktok.com/@user/video/123456": ("tiktok", "123456"),
        "https://x.com/user/status/123456": ("twitter", "123456"),
        "https://reddit.com/r/test/comments/abc123/title/": ("reddit", "abc123"),
        "https://www.pinterest.com/pin/123456/": ("pinterest", "123456"),
        "https://9gag.com/gag/abc123": ("ninegag", "abc123"),
        "https://www.threads.net/@user/post/abc123": ("threads", "abc123"),
    }
    for url, expected in cases.items():
        request = identify(url)
        assert request is not None
        assert (request.extractor_id, request.content_id) == expected


def test_rejects_unrelated_or_local_urls() -> None:
    assert identify("https://notyoutube.com/watch?v=YE7VzlLtp-4") is None
    assert identify("http://127.0.0.1/video") is None
    assert identify("https://youtube.com:8443/watch?v=YE7VzlLtp-4") is None
    assert first_supported_url("hello https://www.instagram.com/p/Cabc123/ nice")


def test_cookies_are_copied_not_modified(tmp_path: Path) -> None:
    private = tmp_path / "private" / "cookies"
    private.mkdir(parents=True)
    source = private / "instagram.txt"
    source.write_text("# Netscape HTTP Cookie File\n")
    work = tmp_path / "work"
    work.mkdir()
    copied = job_cookie_file(SimpleNamespace(cookie_path=lambda _: source), "instagram", work)
    assert copied is not None and copied.read_bytes() == source.read_bytes()
    copied.write_text("changed")
    assert source.read_text() == "# Netscape HTTP Cookie File\n"
