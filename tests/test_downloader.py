from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.core.errors import AuthenticationRequired, FileTooLarge, NoAttachments, NoMedia, SessionCheckRequired
from internal.extractors import downloader
from internal.extractors.sites import Request
from internal.models.media import Media


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        max_file_size=100, max_duration=3600, proxy="",
        site=lambda _: SimpleNamespace(proxy="", download_proxy="", disable_proxy=False, edge_proxy=""),
        cookie_path=lambda _: tmp_path / "missing.txt",
    )


def test_downloaded_file_is_turned_into_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download):
            path = tmp_path / "001-video.mp4"
            path.write_bytes(b"video")
            return {"title": "A video", "requested_downloads": [{"filepath": str(path)}], "duration": 10}

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    media = downloader._download(Request("youtube", "video", "https://youtu.be/video"), _settings(tmp_path), tmp_path)
    assert media.items[0].path == tmp_path / "001-video.mp4"
    assert media.items[0].kind == "video"
    assert captured["format"].startswith("bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a][acodec^=mp4a]")
    assert "node" in captured["js_runtimes"]


def test_marked_playlist_entry_marks_the_post() -> None:
    assert downloader._marked_nsfw({"entries": [{"age_limit": 0}, {"age_limit": 18}]})
    assert downloader._marked_nsfw({"age_limit": 18, "entries": [{"age_limit": None}]})
    assert not downloader._marked_nsfw({"entries": [{"age_limit": 0}]})


def test_progress_stops_oversized_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download):
            self.opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 101})

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(FileTooLarge):
        downloader._download(Request("youtube", "video", "https://youtu.be/video"), _settings(tmp_path), tmp_path)


def test_additional_site_uses_its_own_cookie_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cookies = tmp_path / "private" / "cookies"
    cookies.mkdir(parents=True)
    original = cookies / "vimeo.txt"
    original.write_text("# Netscape HTTP Cookie File\n")
    captured = {}

    class FakeYDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download):
            path = tmp_path / "001-video.mp4"
            path.write_bytes(b"video")
            return {"requested_downloads": [{"filepath": str(path)}]}

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    settings = _settings(tmp_path)
    settings.cookie_path = lambda site_id: cookies / f"{site_id}.txt"
    downloader._download(
        Request("vimeo", "hash", "https://vimeo.com/123456"), settings, tmp_path,
    )
    assert Path(captured["cookiefile"]).read_bytes() == original.read_bytes()
    assert Path(captured["cookiefile"]) != original

    captured.clear()
    instagram_cookie = cookies / "instagram.txt"
    instagram_cookie.write_bytes(original.read_bytes())
    downloader._download(
        Request("instagram", "reel", "https://www.instagram.com/reel/reel/"),
        settings, tmp_path, use_cookies=False,
    )
    assert "cookiefile" not in captured
    assert captured["format"].startswith("b[ext=mp4]/bv[ext=mp4][vcodec^=avc1]")
    assert instagram_cookie.read_text() == "# Netscape HTTP Cookie File\n"


def test_instagram_prefers_merged_video_when_its_codec_metadata_is_missing() -> None:
    import yt_dlp

    with yt_dlp.YoutubeDL({
        "format": downloader.INSTAGRAM_FORMAT, "logger": downloader._SilentYtdlpLogger(),
        "quiet": True, "check_formats": False,
    }) as ydl:
        result = ydl.process_ie_result({
            "id": "reel", "title": "A reel", "formats": [
                {"format_id": "3", "ext": "mp4", "vcodec": None, "acodec": None,
                 "url": "https://example.com/merged.mp4"},
                {"format_id": "dash-video", "ext": "mp4", "vcodec": "vp09.00.40.08", "acodec": "none",
                 "width": 1080, "height": 1920, "url": "https://example.com/video.mp4"},
                {"format_id": "dash-audio", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.5",
                 "url": "https://example.com/audio.m4a"},
            ],
        }, download=False)
    assert result["format_id"] == "3"


@pytest.mark.parametrize(
    ("body", "status", "expected"),
    [
        (b'{"message":"checkpoint_required"}', 400, SessionCheckRequired),
        (b'{"message":"challenge_required"}', 400, SessionCheckRequired),
        (b'{"message":"login_required"}', 401, AuthenticationRequired),
        (b'{"message":"feedback_required"}', 400, NoMedia),
        (b'{"message":"checkpoint_required"}', 500, NoMedia),
        (b'not JSON', 400, NoMedia),
        (b'[]', 400, NoMedia),
    ],
)
def test_instagram_http_errors_are_classified_without_exposing_response_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes, status: int, expected: type,
) -> None:
    import yt_dlp
    from yt_dlp.networking.common import Response
    from yt_dlp.networking.exceptions import HTTPError

    response = Response(BytesIO(body), "https://www.instagram.com/api/v1/media/123/info/", {}, status=status)
    cause = HTTPError(response)
    error = yt_dlp.utils.DownloadError("HTTP error", exc_info=(type(cause), cause, None))

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download):
            raise error

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    with pytest.raises(expected) as exc:
        downloader._download(
            Request("instagram", "reel", "https://www.instagram.com/reel/reel/"), _settings(tmp_path), tmp_path,
        )
    assert type(exc.value) is expected
    assert "https://" not in str(exc.value)
    assert "checkpoint_required" not in str(exc.value)


@pytest.mark.asyncio
async def test_instagram_auth_error_survives_failed_ytdlp_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = Request("instagram", "reel", "https://www.instagram.com/reel/reel/")
    auth_error = AuthenticationRequired("Refresh the Instagram cookies.")

    async def gallery(*args):
        raise auth_error

    def ytdlp(*args):
        raise NoMedia("Could not download media from this link.")

    monkeypatch.setattr(downloader, "download_gallery", gallery)
    monkeypatch.setattr(downloader, "_download_in_process", ytdlp)
    with pytest.raises(AuthenticationRequired) as exc:
        await downloader.download(request, _settings(tmp_path), tmp_path)
    assert exc.value is auth_error

    media = Media("instagram", "reel", request.url)
    monkeypatch.setattr(downloader, "_download_in_process", lambda *args: media)
    assert await downloader.download(request, _settings(tmp_path), tmp_path) is media


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["public", "private", "oversized"])
async def test_instagram_checkpoint_retries_without_cookies_and_preserves_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    request = Request("instagram", "reel", "https://www.instagram.com/reel/reel/")
    checkpoint = SessionCheckRequired("Complete the Instagram security check.")
    media = Media("instagram", "reel", request.url)
    calls = []

    async def gallery(*args):
        raise NoMedia("HTTP 400")

    def ytdlp(request, settings, workdir, use_cookies=True):
        calls.append(use_cookies)
        if use_cookies:
            raise checkpoint
        if outcome == "private":
            raise NoMedia("Login required")
        if outcome == "oversized":
            raise FileTooLarge("The file exceeds the 2 GB limit.")
        return media

    monkeypatch.setattr(downloader, "download_gallery", gallery)
    monkeypatch.setattr(downloader, "_download_in_process", ytdlp)
    if outcome == "public":
        assert await downloader.download(request, _settings(tmp_path), tmp_path) is media
    elif outcome == "private":
        with pytest.raises(SessionCheckRequired) as exc:
            await downloader.download(request, _settings(tmp_path), tmp_path)
        assert exc.value is checkpoint
    else:
        with pytest.raises(FileTooLarge):
            await downloader.download(request, _settings(tmp_path), tmp_path)
    assert calls == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["photo", "text", "error"])
async def test_twitter_no_video_uses_gallery_without_hiding_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str,
) -> None:
    request = Request("twitter", "123", "https://x.com/user/status/123")
    media = Media("twitter", "123", request.url)
    error = NoAttachments("No attachments") if result == "text" else NoMedia("Fetch failed")

    def ytdlp(*args):
        raise NoMedia("No video found")

    async def gallery(*args):
        if result == "photo":
            return media
        raise error

    monkeypatch.setattr(downloader, "_download_in_process", ytdlp)
    monkeypatch.setattr(downloader, "download_gallery", gallery)
    if result == "photo":
        assert await downloader.download(request, _settings(tmp_path), tmp_path) is media
    else:
        with pytest.raises(type(error)) as exc:
            await downloader.download(request, _settings(tmp_path), tmp_path)
        assert exc.value is error

    async def unexpected_gallery(*args):
        raise AssertionError("successful videos must not use the fallback")

    monkeypatch.setattr(downloader, "_download_in_process", lambda *args: media)
    monkeypatch.setattr(downloader, "download_gallery", unexpected_gallery)
    assert await downloader.download(request, _settings(tmp_path), tmp_path) is media
