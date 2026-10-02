import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.core.errors import AuthenticationRequired, FileTooLarge, MediaError, NoAttachments, NoMedia
from internal.extractors.extracted import save_extraction
from internal.extractors.gallery import download_gallery, files_in, prefer_gallery
from internal.extractors.gallery_worker import download_prepared, replay_messages
from internal.extractors.sites import Request


def test_photo_sites_and_tiktok_photo_use_gallery() -> None:
    assert prefer_gallery(Request("instagram", "abc", "https://instagram.com/p/abc"))
    assert prefer_gallery(Request("tiktok", "123", "https://tiktok.com/@x/photo/123"))
    assert not prefer_gallery(Request("tiktok", "123", "https://tiktok.com/@x/video/123"))
    assert not prefer_gallery(Request("youtube", "abc", "https://youtu.be/abc"))


def test_gallery_files_skip_metadata_and_partials(tmp_path: Path) -> None:
    (tmp_path / "album").mkdir()
    (tmp_path / "album" / "001.jpg").write_bytes(b"photo")
    (tmp_path / "album" / "001.jpg.json").write_text("{}")
    (tmp_path / "album" / "002.mp4.part").write_bytes(b"partial")
    assert [p.name for p in files_in(tmp_path)] == ["001.jpg"]


@pytest.mark.asyncio
async def test_instagram_login_redirect_is_reported(tmp_path: Path, monkeypatch) -> None:
    class Process:
        returncode = 4

        async def communicate(self):
            return b"", b"[instagram][error] HTTP redirect to login page"

    async def start(*args, **kwargs):
        assert "extractor.instagram.videos=merged" in args
        return Process()

    monkeypatch.setattr("internal.extractors.gallery.asyncio.create_subprocess_exec", start)
    settings = SimpleNamespace(
        max_file_size=2_000_000_000, proxy="",
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", proxy="", disable_proxy=False),
        cookie_path=lambda _: tmp_path / "missing.txt",
    )
    with pytest.raises(AuthenticationRequired, match="Refresh the Instagram cookies"):
        await download_gallery(
            Request("instagram", "DdzBEKugzsT", "https://www.instagram.com/reel/DdzBEKugzsT/"),
            settings, tmp_path,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("posts", "exit_code", "stderr", "text_only"),
    [
        ([{"tweet_id": 123, "count": 0}], 0, b"", True),
        ([], 0, b"", False),
        ([{"tweet_id": 456, "count": 0}], 0, b"", False),
        ([{"tweet_id": 123, "count": 1}], 0, b"", False),
        ([{"tweet_id": 123, "count": False}], 0, b"", False),
        ([{"tweet_id": 123, "count": 0}], 4, b"Login required", False),
        ([{"tweet_id": 123, "count": 0}], 0, b"Extractor error", False),
        ([{"tweet_id": 123, "count": 0, "quoted_id": 456}], 0, b"", False),
        ([{"tweet_id": 123, "count": 0, "quoted_id": 456}, {"tweet_id": 456, "count": 0}], 0, b"", True),
        ([{"tweet_id": 123, "count": 0, "quoted_id": 456}, {"tweet_id": 456, "count": 1}], 0, b"", False),
        ([{"tweet_id": 123, "count": 0, "quoted_id": 456}, {"tweet_id": 456, "count": 0, "quoted_id": 789}], 0, b"", False),
        ([{"tweet_id": 123, "count": 0, "quoted_id": 456}, {"tweet_id": 456, "count": 0, "quoted_id": 789}, {"tweet_id": 789, "count": 0}], 0, b"", True),
        (["invalid metadata"], 0, b"", False),
    ],
)
async def test_only_confirmed_text_posts_are_silent(
    tmp_path: Path, monkeypatch, posts: list, exit_code: int, stderr: bytes, text_only: bool,
) -> None:
    class Process:
        returncode = exit_code

        async def communicate(self):
            for index, post in enumerate(posts):
                (tmp_path / "gallery" / f"{index}.post.json").write_text(json.dumps(post))
            return b"", stderr

    async def start(*args, **kwargs):
        assert "extractor.twitter.text-tweets=true" in args
        assert "extractor.twitter.tweet-endpoint=rest" in args
        assert "metadata@post" in args
        return Process()

    monkeypatch.setattr("internal.extractors.gallery.asyncio.create_subprocess_exec", start)
    settings = SimpleNamespace(
        max_file_size=2_000_000_000, proxy="",
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", proxy="", disable_proxy=False),
        cookie_path=lambda _: tmp_path / "missing.txt",
    )
    with pytest.raises(NoAttachments if text_only else NoMedia):
        await download_gallery(Request("twitter", "123", "https://x.com/user/status/123"), settings, tmp_path)


@pytest.mark.asyncio
async def test_photo_only_tweet_downloads_its_attachments(tmp_path: Path, monkeypatch) -> None:
    class Process:
        returncode = 0

        async def communicate(self):
            directory = tmp_path / "gallery"
            (directory / "123_1.jpg").write_bytes(b"photo")
            (directory / "123.post.json").write_text(json.dumps({
                "tweet_id": 123, "count": 1, "content": "A photo", "sensitive": True,
            }))
            return b"", b""

    async def start(*args, **kwargs):
        return Process()

    monkeypatch.setattr("internal.extractors.gallery.asyncio.create_subprocess_exec", start)
    settings = SimpleNamespace(
        max_file_size=2_000_000_000, proxy="",
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", proxy="", disable_proxy=False),
        cookie_path=lambda _: tmp_path / "missing.txt",
    )
    media = await download_gallery(
        Request("twitter", "123", "https://x.com/user/status/123"), settings, tmp_path,
    )
    assert [item.kind for item in media.items] == ["photo"]
    assert media.caption == "A photo"
    assert media.nsfw


@pytest.mark.parametrize(
    "records",
    [
        None, [], [[2, {"count": 0}]],
        [[3, "https://cdn.example/video.mp4", {}]],
        [[2, {}], [-1, {"error": "AuthenticationError"}]],
        [[2, {}], [6, "https://www.instagram.com/p/other/", {}]],
        [[2, {}], [3, "ytdl:https://www.instagram.com/p/post/", {}]],
        [[2, {}]] + [[3, f"https://cdn.example/{index}.jpg", {}] for index in range(22)],
    ],
)
def test_incomplete_or_unsupported_gallery_results_are_not_replayed(records) -> None:
    assert replay_messages(records) is None


def test_gallery_replay_downloads_album_without_fetching_post(tmp_path: Path, monkeypatch) -> None:
    import copy

    from gallery_dl import config
    from gallery_dl.extractor.instagram import InstagramExtractor, InstagramPostExtractor
    from gallery_dl.job import DownloadJob

    previous_config = copy.deepcopy(config._config)
    request = Request("instagram", "post", "https://www.instagram.com/p/post/")
    metadata = {
        "username": "test", "sidecar_media_id": "album", "count": 2,
        "caption": "Saved album caption", "is_nsfw": True,
    }
    records = [
        [2, metadata],
        [3, "https://cdn.example/video.mp4", {
            **metadata, "media_id": "video", "filename": "video", "extension": "mp4",
            "_http_headers": {"User-Agent": "saved-agent", "Referer": "https://www.instagram.com/"},
        }],
        [3, "https://cdn.example/photo.jpg", {
            **metadata, "media_id": "photo", "filename": "photo", "extension": "jpg",
        }],
    ]
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tupdated-job-session\n"
    )
    production_cookie = tmp_path / "original.txt"
    production_cookie.write_text("must remain unchanged")
    settings = SimpleNamespace(
        max_file_size=2_000_000_000, proxy="socks5://proxy.example:1080",
        site=lambda _: SimpleNamespace(download_proxy="", proxy="", disable_proxy=False),
        cookie_path=lambda _: production_cookie,
    )
    downloads = []

    def unexpected_post_request(*args, **kwargs):
        raise AssertionError("replay must not fetch the Instagram post")

    def download(job, url):
        assert job.extractor.cookies.get("sessionid", domain=".instagram.com") == "updated-job-session"
        downloads.append((url, job.pathfmt.kwdict.copy()))
        with job.pathfmt.open() as output:
            output.write(b"media")
        return True

    monkeypatch.setattr(InstagramExtractor, "login", unexpected_post_request)
    monkeypatch.setattr(InstagramPostExtractor, "posts", unexpected_post_request)
    monkeypatch.setattr(DownloadJob, "download", download)
    try:
        assert download_prepared(request, settings, tmp_path, replay_messages(records)) == 0
        assert config.get((), "file-range") == "1-21"
        assert config.get((), "filesize-max") == settings.max_file_size
        assert config.get((), "proxy") == settings.proxy
        assert config.get(("extractor", "instagram"), "videos") == "merged"
    finally:
        config.clear()
        config._config.update(previous_config)

    assert [url for url, _ in downloads] == ["https://cdn.example/video.mp4", "https://cdn.example/photo.jpg"]
    assert downloads[0][1]["_http_headers"]["User-Agent"] == "saved-agent"
    assert sorted(path.suffix for path in files_in(tmp_path / "gallery")) == [".jpg", ".mp4"]
    metadata_files = list((tmp_path / "gallery").rglob("*.json"))
    assert len(metadata_files) == 2
    assert all(json.loads(path.read_text())["caption"] == metadata["caption"] for path in metadata_files)
    assert production_cookie.read_text() == "must remain unchanged"


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_instagram_stream_fallback_reuses_gallery_results(
    tmp_path: Path, monkeypatch, success: bool,
) -> None:
    request = Request("instagram", "post", "https://www.instagram.com/reel/post/")
    save_extraction(tmp_path, request, "gallery", [
        [2, {"username": "test", "count": 1}],
        [3, "https://cdn.example/video.mp4", {"media_id": "video", "extension": "mp4"}],
    ])
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("updated job cookies")
    starts = []

    class Process:
        returncode = 0 if success else 4

        async def communicate(self, data=None):
            if data is None:
                assert not (tmp_path / "extracted.json").exists()
                return b"", b""
            assert json.loads(data)["content_id"] == request.content_id
            if success:
                (tmp_path / "gallery" / "video.mp4").write_bytes(b"video")
                (tmp_path / "gallery" / "video.mp4.json").write_text(json.dumps({
                    "caption": "Saved caption", "sensitive": True,
                }))
            return b"", b""

    async def start(*args, **kwargs):
        starts.append(args)
        if len(starts) == 1:
            assert args[1:] == ("-m", "internal.extractors.gallery_worker")
            assert "cookies" not in " ".join(args)
        else:
            assert args[1:3] == ("-m", "gallery_dl")
            assert args[args.index("--cookies") + 1] == str(cookie)
        return Process()

    def unexpected_cookie_copy(*args):
        raise AssertionError("replay must keep the cookiejar from extraction")

    monkeypatch.setattr("internal.extractors.gallery.asyncio.create_subprocess_exec", start)
    monkeypatch.setattr("internal.extractors.gallery.job_cookie_file", unexpected_cookie_copy)
    settings = SimpleNamespace(
        root=tmp_path, max_file_size=2_000_000_000, proxy="",
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", proxy="", disable_proxy=False),
    )
    if success:
        media = await download_gallery(request, settings, tmp_path)
        assert media.caption == "Saved caption" and media.nsfw
        assert len(media.items) == 1 and media.items[0].kind == "video"
    else:
        with pytest.raises(NoMedia, match="No media was found"):
            await download_gallery(request, settings, tmp_path)
    assert len(starts) == (1 if success else 2)
    assert cookie.read_text() == "updated job cookies"


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_failed_photo_album_replay_fetches_a_complete_fresh_album(
    tmp_path: Path, monkeypatch, partial: bool,
) -> None:
    request = Request("instagram", "album", "https://www.instagram.com/p/album/")
    save_extraction(tmp_path, request, "gallery", [
        [2, {"count": 2}],
        [3, "https://cdn.example/expired-1.jpg", {"extension": "jpg"}],
        [3, "https://cdn.example/expired-2.jpg", {"extension": "jpg"}],
    ])
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("updated job cookies")
    sentinel = tmp_path / "untouched.txt"
    sentinel.write_text("other job data")
    starts = []

    class Replay:
        returncode = 4

        async def communicate(self, data):
            assert json.loads(data)["content_id"] == request.content_id
            if partial:
                (tmp_path / "gallery" / "expired-1.jpg").write_bytes(b"partial")
                (tmp_path / "gallery" / "expired-1.jpg.json").write_text(json.dumps({
                    "caption": "Incomplete stale caption",
                }))
            return b"", b"Source download failed"

    class Fresh:
        returncode = 0

        async def communicate(self):
            for index in (1, 2):
                path = tmp_path / "gallery" / f"fresh-{index}.jpg"
                path.write_bytes(b"complete photo")
                path.with_suffix(".jpg.json").write_text(json.dumps({
                    "caption": "Fresh complete album", "sensitive": True,
                }))
            return b"", b""

    async def start(*args, **kwargs):
        starts.append(args)
        if len(starts) == 1:
            assert args[1:] == ("-m", "internal.extractors.gallery_worker")
            return Replay()
        assert len(starts) == 2
        assert args[1:3] == ("-m", "gallery_dl")
        assert list((tmp_path / "gallery").iterdir()) == []
        assert not (tmp_path / "extracted.json").exists()
        assert args[args.index("--cookies") + 1] == str(cookie)
        return Fresh()

    def unexpected_cookie_copy(*args):
        raise AssertionError("fresh retry must keep updated job cookies")

    monkeypatch.setattr("internal.extractors.gallery.asyncio.create_subprocess_exec", start)
    monkeypatch.setattr("internal.extractors.gallery.job_cookie_file", unexpected_cookie_copy)
    settings = SimpleNamespace(
        root=tmp_path, max_file_size=2_000_000_000, proxy="",
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", proxy="", disable_proxy=False),
    )
    media = await download_gallery(request, settings, tmp_path)
    assert [item.path.name for item in media.items] == ["fresh-1.jpg", "fresh-2.jpg"]
    assert all(item.kind == "photo" for item in media.items)
    assert media.caption == "Fresh complete album" and media.nsfw
    assert len(starts) == 2
    assert cookie.read_text() == "updated job cookies"
    assert sentinel.read_text() == "other job data"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["album", "disk"])
async def test_gallery_replay_limits_do_not_retry(tmp_path: Path, monkeypatch, limit: str) -> None:
    request = Request("instagram", "album", "https://www.instagram.com/p/album/")
    save_extraction(tmp_path, request, "gallery", [
        [2, {"count": 1}], [3, "https://cdn.example/photo.jpg", {"extension": "jpg"}],
    ])
    starts = []

    class Process:
        returncode = 4

        async def communicate(self, data):
            for index in range(21 if limit == "album" else 1):
                (tmp_path / "gallery" / f"{index}.jpg").write_bytes(b"photo")
            return b"", b"Source download failed"

    async def start(*args, **kwargs):
        starts.append(args)
        return Process()

    monkeypatch.setattr("internal.extractors.gallery.asyncio.create_subprocess_exec", start)
    if limit == "disk":
        monkeypatch.setattr("internal.extractors.gallery.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    settings = SimpleNamespace(
        root=tmp_path, max_file_size=2_000_000_000, proxy="",
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", proxy="", disable_proxy=False),
    )
    with pytest.raises(MediaError, match="more than 20" if limit == "album" else "free disk space"):
        await download_gallery(request, settings, tmp_path)
    assert len(starts) == 1
    assert (tmp_path / "extracted.json").exists()


@pytest.mark.asyncio
async def test_incompatible_instagram_results_use_regular_gallery_cli(tmp_path: Path, monkeypatch) -> None:
    request = Request("instagram", "post", "https://www.instagram.com/p/post/")
    save_extraction(tmp_path, request, "gallery", [[6, "https://www.instagram.com/p/other/", {}]])

    class Process:
        returncode = 4

        async def communicate(self):
            return b"", b"HTTP redirect to login page"

    async def start(*args, **kwargs):
        assert args[1:3] == ("-m", "gallery_dl")
        return Process()

    monkeypatch.setattr("internal.extractors.gallery.asyncio.create_subprocess_exec", start)
    settings = SimpleNamespace(
        max_file_size=2_000_000_000, proxy="",
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", proxy="", disable_proxy=False),
        cookie_path=lambda _: tmp_path / "missing.txt",
    )
    with pytest.raises(AuthenticationRequired):
        await download_gallery(request, settings, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["completed", "running", "exiting"])
async def test_replayed_gallery_still_stops_oversize_files(tmp_path: Path, monkeypatch, state: str) -> None:
    request = Request("instagram", "post", "https://www.instagram.com/reel/post/")
    save_extraction(tmp_path, request, "gallery", [
        [2, {"count": 1}], [3, "https://cdn.example/video.mp4", {"extension": "mp4"}],
    ])
    finished = asyncio.Event()

    class Process:
        returncode = 0 if state == "completed" else None
        killed = False

        async def communicate(self, data):
            (tmp_path / "gallery" / "video.mp4").write_bytes(b"oversize")
            if state != "completed":
                await finished.wait()
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = 0 if state == "exiting" else -9
            finished.set()
            if state == "exiting":
                raise ProcessLookupError("process just exited")

    process = Process()

    async def start(*args, **kwargs):
        return process

    monkeypatch.setattr("internal.extractors.gallery.asyncio.create_subprocess_exec", start)
    settings = SimpleNamespace(
        root=tmp_path, max_file_size=5, proxy="",
        site=lambda _: SimpleNamespace(edge_proxy="", download_proxy="", proxy="", disable_proxy=False),
    )
    with pytest.raises(FileTooLarge):
        await download_gallery(request, settings, tmp_path)
    assert process.killed is (state != "completed")
