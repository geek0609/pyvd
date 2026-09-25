from pathlib import Path

from internal.extractors.gallery import files_in, prefer_gallery
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
