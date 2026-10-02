import io
import json
import os
import threading
from contextlib import contextmanager
from http.cookiejar import Cookie, CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

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


def test_dash_keeps_the_exact_selected_video_and_audio_fragments() -> None:
    video = {
        "url": "https://cdn.example/video.mpd", "protocol": "http_dash_segments", "ext": "mp4",
        "vcodec": "avc1.640028", "height": 1080, "format_id": "selected-video",
        "fragment_base_url": "https://cdn.example/", "fragments": [{"path": "init.m4s"}, {"path": "video.m4s"}],
    }
    audio = {
        "url": "https://cdn.example/audio.mpd", "protocol": "http_dash_segments", "ext": "m4a",
        "acodec": "mp4a.40.2", "format_id": "selected-audio",
        "fragments": [{"url": "https://cdn.example/audio.m4s"}],
    }
    assert stream_worker._formats({"requested_formats": [video, audio]}) == [video, audio]
    assert stream_worker._formats({"requested_formats": [video, {**audio, "acodec": "opus"}]}) is None


@pytest.mark.parametrize("changes", [
    {"is_live": True}, {"has_drm": True}, {"live_status": "post_live"},
    {"is_from_start": True}, {"protocol": "http_dash_segments_generator"},
    {"fragments": "generator"}, {"fragments": iter([{"url": "https://cdn.example/part.m4s"}])},
    {"fragments": [{"url": "https://cdn.example/part.m4s", "decrypt_info": {"METHOD": "AES-128"}}]},
])
def test_live_drm_and_unbounded_dash_keep_completed_downloading(changes) -> None:
    source = {
        "url": "https://cdn.example/video.mpd", "protocol": "http_dash_segments", "ext": "mp4",
        "vcodec": "avc1.640028", "acodec": "none",
        "fragments": [{"url": "https://cdn.example/part.m4s"}],
    }
    assert stream_worker._formats({**source, **changes}) is None


@pytest.mark.parametrize("manifest", [
    "#EXTM3U\n#EXTINF:1,\nsegment.ts\n",  # No finite end marker.
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvariant.m3u8\n#EXT-X-ENDLIST\n",
    "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=key.bin\nsegment.ts\n#EXT-X-ENDLIST\n",
    "#EXTM3U\n  #EXT-X-KEY:METHOD=SAMPLE-AES,URI=key.bin\nsegment.ts\n#EXT-X-ENDLIST\n",
])
def test_unsafe_hls_playlists_do_not_stream(manifest) -> None:
    assert not stream_worker._hls_manifest(manifest, {})


@contextmanager
def media_server(payloads: dict[str, bytes]):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            body = payloads.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def copy_native(
    fmt: dict, tmp_path: Path, cookies: CookieJar | None = None,
    stopped: threading.Event | None = None,
):
    fifo = tmp_path / "segments.fifo"
    os.mkfifo(fifo)
    received = []

    def read():
        with fifo.open("rb") as stream:
            received.append(stream.read())

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    errors = []
    stream_worker._copy_source(fmt, fifo, "", cookies or CookieJar(), stopped or threading.Event(), errors)
    reader.join(timeout=3)
    assert not reader.is_alive()
    assert not list(tmp_path.glob("segments.fifo-Frag*"))
    return received[0], errors


@pytest.mark.parametrize("protocol", ["m3u8_native", "http_dash_segments"])
def test_native_sources_preserve_fragments_headers_and_scoped_cookies(tmp_path: Path, protocol: str) -> None:
    first = b"initialization and first fragment"
    second = b"second fragment"
    playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfirst\n#EXTINF:1,\nsecond\n#EXT-X-ENDLIST\n"
    cookies = CookieJar()
    for domain in ("127.0.0.1", "unrelated.example"):
        cookies.set_cookie(Cookie(
            version=0, name="session", value=domain, port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=False,
            path="/", path_specified=True, secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={},
        ))
    with media_server({"/media.m3u8": playlist, "/first": first, "/second": second}) as (base, requests):
        fmt = {"url": base + "/media.m3u8", "protocol": protocol, "ext": "mp4", "http_headers": {"X-Media-Test": "selected"}}
        if protocol == "http_dash_segments":
            fmt["fragments"] = [{"url": base + "/first"}, {"url": base + "/second"}]
        received, errors = copy_native(fmt, tmp_path, cookies)
    assert not errors
    assert received == first + second
    assert {path for path, _ in requests} == ({"/first", "/second", "/media.m3u8"} if protocol == "m3u8_native" else {"/first", "/second"})
    assert all(headers["X-Media-Test"] == "selected" for _, headers in requests)
    assert all(headers["Cookie"] == "session=127.0.0.1" for _, headers in requests)


@pytest.mark.parametrize("failure", ["missing", "oversized", "encrypted", "unbounded"])
def test_native_failure_closes_fifo_and_cleans_fragments(tmp_path: Path, monkeypatch, failure: str) -> None:
    playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfirst\n#EXTINF:1,\nsecond\n#EXT-X-ENDLIST\n"
    payloads = {"/first": b"first fragment", "/second": b"second fragment"}
    if failure == "missing":
        del payloads["/second"]
    elif failure == "oversized":
        monkeypatch.setattr(stream_worker, "MAX_FRAGMENT_SIZE", 4)
    elif failure == "encrypted":
        playlist = playlist.replace("#EXTINF:1,", "#EXT-X-KEY:METHOD=AES-128,URI=key.bin\n#EXTINF:1,", 1)
    else:
        playlist = playlist.replace("#EXT-X-ENDLIST\n", "")
    payloads["/media.m3u8"] = playlist.encode()
    with media_server(payloads) as (base, _requests):
        received, errors = copy_native({"url": base + "/media.m3u8", "protocol": "m3u8_native", "ext": "mp4"}, tmp_path)
    assert errors
    assert received == (b"first fragment" if failure == "missing" else b"")


def test_native_cancellation_stops_before_fetching_another_fragment(tmp_path: Path, monkeypatch) -> None:
    stopped = threading.Event()
    original_writer = stream_worker.PipeWriter

    class StopAfterFragment(original_writer):
        def write(self, data):
            result = super().write(data)
            stopped.set()
            return result

    monkeypatch.setattr(stream_worker, "PipeWriter", StopAfterFragment)
    playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfirst\n#EXTINF:1,\nsecond\n#EXT-X-ENDLIST\n"
    with media_server({"/media.m3u8": playlist, "/first": b"first", "/second": b"second"}) as (base, requests):
        received, errors = copy_native({"url": base + "/media.m3u8", "protocol": "m3u8_native", "ext": "mp4"}, tmp_path, stopped=stopped)
    assert errors and received == b"first"
    assert {path for path, _ in requests} == {"/media.m3u8", "/first"}
