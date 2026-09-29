from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.core.errors import AuthenticationRequired
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
