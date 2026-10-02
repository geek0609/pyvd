import errno
import io
import json
import os
import threading
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

import pytest

from internal.extractors import stream_worker
from internal.extractors.extracted import load_extraction
from internal.extractors.extracted import save_extraction
from internal.extractors.sites import Request as MediaRequest


def cookie_jar() -> CookieJar:
    jar = CookieJar()
    for domain in (".instagram.com", ".cdn.example"):
        jar.set_cookie(Cookie(
            version=0, name="session", value=domain, port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=True,
            path="/", path_specified=True, secure=True, expires=None, discard=True,
            comment=None, comment_url=None, rest={},
        ))
    return jar


def test_cookie_backed_extraction_keeps_cookiefile_and_format_selection(monkeypatch, tmp_path: Path) -> None:
    import yt_dlp

    captured = {}
    cookie = tmp_path / "copied-cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n")
    expected = {"url": "https://cdn.example/video.mp4"}

    class YoutubeDL:
        cookiejar = cookie_jar()

        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def extract_info(self, url, *, download):
            assert not download
            return expected

    monkeypatch.setattr(yt_dlp, "YoutubeDL", YoutubeDL)
    info, cookies = stream_worker._extract_ytdlp(
        {"extractor_id": "youtube", "url": "https://youtu.be/video"}, None, cookie, "http://proxy.example",
    )
    assert info is expected
    assert captured["format"] == stream_worker.H264_FORMAT
    assert captured["cookiefile"] == str(cookie)
    assert captured["proxy"] == "http://proxy.example"
    assert cookies is not YoutubeDL.cookiejar
    assert len(cookies) == 2


def instagram_data(*sources: tuple[str, dict]):
    return [(2, {"count": len(sources)}), *[(3, url, metadata) for url, metadata in sources]]


def test_instagram_uses_the_selected_merged_video_and_markers() -> None:
    url = "https://cdn.example/video.mp4?signature=example"
    metadata = {
        "video_url": url, "description": "Caption", "username": "uploader",
        "width": 720, "height": 1280, "_http_headers": {"User-Agent": "Mozilla/5.0"},
        "sensitive": True,
    }
    data = instagram_data((url, metadata))
    # Gallery counts soundtrack metadata even when audio=false excludes its URL.
    data[0][1]["count"] = 2
    info, sources = stream_worker._instagram_video(data)
    assert sources == [{"url": url, "http_headers": metadata["_http_headers"]}]
    assert info == {
        "description": "Caption", "title": "", "uploader": "uploader", "duration": 0,
        "width": 720, "height": 1280, "format_id": "merged", "age_limit": 18,
    }


@pytest.mark.parametrize("data", [
    instagram_data(("https://cdn.example/photo.jpg", {"display_url": "https://cdn.example/photo.jpg"})),
    instagram_data(
        ("https://cdn.example/video.mp4", {"video_url": "https://cdn.example/video.mp4"}),
        ("https://cdn.example/photo.jpg", {}),
    ),
    instagram_data(("ytdl:https://instagram.com/p/video", {"_ytdl_manifest": "dash"})),
    instagram_data(("http://cdn.example/video.mp4", {"video_url": "http://cdn.example/video.mp4"})),
    [(2, {}), (6, "https://instagram.com/p/other", {})],
    [(2, {}), (2, {})],
])
def test_instagram_photos_carousels_and_manifests_fall_back(data) -> None:
    assert stream_worker._instagram_video(data) is None


def test_instagram_extraction_keeps_all_records_headers_and_scoped_cookies(monkeypatch, tmp_path: Path) -> None:
    from gallery_dl import config, job

    configured = {}
    monkeypatch.setattr(config, "clear", lambda: None)
    monkeypatch.setattr(config, "set", lambda path, key, value: configured.__setitem__((path, key), value))
    data = instagram_data(
        ("https://cdn.example/video.mp4", {"_http_headers": {"User-Agent": "video"}}),
        ("https://cdn.example/photo.jpg", {}),
    )
    extraction = SimpleNamespace(
        data=data, exception=None, run=lambda: 0,
        extractor=SimpleNamespace(status=0, session=SimpleNamespace(
            headers={"User-Agent": "session", "Referer": "https://instagram.com/"}, cookies=cookie_jar(),
        )),
    )
    monkeypatch.setattr(job, "DataJob", lambda _url, *, file: extraction)
    cookie = tmp_path / "cookies.txt"
    result, cookies = stream_worker._extract_instagram(
        {"url": "https://www.instagram.com/p/post"}, cookie, "http://proxy.example",
    )
    assert result is data and len(result) == 3
    assert result[1][2]["_http_headers"] == {"User-Agent": "video", "Referer": "https://instagram.com/"}
    assert configured[(("extractor", "instagram"), "videos")] == "merged"
    assert configured[(("output",), "private")] is True
    assert configured[(("extractor",), "cookies")] == str(cookie)
    assert configured[(("extractor",), "proxy")] == "http://proxy.example"
    assert cookies is not extraction.extractor.session.cookies


def test_real_gallery_data_can_be_saved_for_replay_without_path_objects(monkeypatch, tmp_path: Path) -> None:
    import copy

    from gallery_dl import config, job
    from gallery_dl.extractor.common import Extractor

    previous_config = copy.deepcopy(config._config)
    request = MediaRequest("instagram", "post", "https://www.instagram.com/p/post/")

    class Post(Extractor):
        category = "instagram"
        subcategory = "post"
        pattern = r"https://www\.instagram\.com/p/(post)/"

        def items(self):
            yield 2, "", {"count": 1}
            yield 3, "https://cdn.example/video.mp4", {"video_url": "https://cdn.example/video.mp4"}

    original_job = job.DataJob
    monkeypatch.setattr(job, "DataJob", lambda url, **kwargs: original_job(Post.from_url(url), **kwargs))
    try:
        data, cookies = stream_worker._extract_instagram({"url": request.url}, None, "")
        save_extraction(tmp_path, request, "gallery", data, cookies)
        saved = load_extraction(tmp_path, request, "gallery")
        assert saved is not None
        assert "_path" not in saved[0][1]
        assert saved[1][2]["_http_headers"]
    finally:
        config.clear()
        config._config.update(previous_config)


def test_direct_sources_delegate_to_the_http_copier(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(stream_worker, "_copy_http_source", lambda *args: calls.append(args))
    fmt = {"url": "https://cdn.example/video.mp4", "http_headers": {"Referer": "https://instagram.com/"}}
    fifo = tmp_path / "source.fifo"
    cookies = cookie_jar()
    stopped = threading.Event()
    errors = []
    stream_worker._copy_source(fmt, fifo, "http://proxy.example", cookies, stopped, errors)
    assert calls == [(fmt, fifo, "http://proxy.example", cookies, stopped, errors)]


@pytest.mark.parametrize("source_fails", [False, True])
def test_worker_finishes_only_when_remux_and_sources_succeed(monkeypatch, tmp_path: Path, source_fails: bool) -> None:
    monkeypatch.setattr(stream_worker.logging, "disable", lambda _level: None)
    settings = SimpleNamespace(
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", disable_proxy=True),
        max_duration=3600,
    )
    job = {
        "root": str(tmp_path), "workdir": str(tmp_path), "extractor_id": "instagram",
        "content_id": "post", "url": "https://instagram.com/reel/post",
    }
    monkeypatch.setattr(stream_worker.sys, "stdin", io.StringIO(json.dumps(job)))
    monkeypatch.setattr(stream_worker, "load_settings", lambda _: settings)
    monkeypatch.setattr(stream_worker, "job_cookie_file", lambda *_args: None)
    url = "https://cdn.example/video.mp4"
    monkeypatch.setattr(stream_worker, "_extract_instagram", lambda *_args: (
        instagram_data((url, {"video_url": url})), CookieJar(),
    ))
    commands = []

    class FFmpeg:
        def __init__(self, command, **_kwargs):
            commands.append(command)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def wait(self):
            return 0

    def copy_source(_fmt, _fifo, _proxy, _cookies, _stopped, errors):
        if source_fails:
            errors.append("HTTPError")

    monkeypatch.setattr(stream_worker.subprocess, "Popen", FFmpeg)
    monkeypatch.setattr(stream_worker, "_copy_source", copy_source)
    assert stream_worker.main() == (1 if source_fails else 0)
    saved = load_extraction(tmp_path, MediaRequest("instagram", "post", job["url"]), "gallery")
    assert saved[1][1] == url  # The fallback can reuse details even after remux/source failure.
    assert "0:a:0?" in commands[0]
    assert commands[0][commands[0].index("-c") + 1] == "copy"
    assert not list(tmp_path.glob("*.fifo"))


@pytest.mark.parametrize("site", ["youtube", "instagram"])
def test_non_streamable_media_keeps_details_for_normal_downloading(monkeypatch, tmp_path: Path, site) -> None:
    monkeypatch.setattr(stream_worker.logging, "disable", lambda _level: None)
    settings = SimpleNamespace(
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", disable_proxy=True),
        max_duration=3600,
    )
    request = MediaRequest(site, "post", f"https://example.com/{site}/post")
    job = {
        "root": str(tmp_path), "workdir": str(tmp_path), "extractor_id": site,
        "content_id": request.content_id, "url": request.url,
    }
    monkeypatch.setattr(stream_worker.sys, "stdin", io.StringIO(json.dumps(job)))
    monkeypatch.setattr(stream_worker, "load_settings", lambda _: settings)
    monkeypatch.setattr(stream_worker, "job_cookie_file", lambda *_args: None)
    if site == "youtube":
        backend = "yt-dlp"
        monkeypatch.setattr(stream_worker, "_extract_ytdlp", lambda *_args: (
            {"id": "post", "vcodec": "vp9", "url": "https://cdn.example/video.webm"}, CookieJar(),
        ))
    else:
        backend = "gallery"
        monkeypatch.setattr(stream_worker, "_extract_instagram", lambda *_args: (
            instagram_data(("https://cdn.example/photo.jpg", {"extension": "jpg"})), CookieJar(),
        ))

    def unexpected_remux(*_args, **_kwargs):
        raise AssertionError("incompatible media should use the normal downloader")

    monkeypatch.setattr(stream_worker.subprocess, "Popen", unexpected_remux)
    assert stream_worker.main() == 0
    assert load_extraction(tmp_path, request, backend) is not None
    assert not list(tmp_path.glob("*.fifo"))
