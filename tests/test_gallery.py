import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.core.errors import AuthenticationRequired, NoAttachments, NoMedia
from internal.extractors.gallery import download_gallery, files_in, prefer_gallery
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
