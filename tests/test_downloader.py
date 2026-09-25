from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.core.errors import FileTooLarge
from internal.extractors import downloader
from internal.extractors.sites import Request


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        max_file_size=100, max_duration=3600, proxy="",
        site=lambda _: SimpleNamespace(proxy="", download_proxy="", disable_proxy=False, edge_proxy=""),
        cookie_path=lambda _: tmp_path / "missing.txt",
    )


def test_downloaded_file_is_turned_into_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download):
            path = tmp_path / "001-video.mp4"
            path.write_bytes(b"video")
            return {"title": "A video", "requested_downloads": [{"filepath": str(path)}], "duration": 10}

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYDL)
    media = downloader._download(Request("youtube", "video", "https://youtu.be/video"), _settings(tmp_path), tmp_path)
    assert media.items[0].path == tmp_path / "001-video.mp4"
    assert media.items[0].kind == "video"


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

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYDL)
    with pytest.raises(FileTooLarge):
        downloader._download(Request("youtube", "video", "https://youtu.be/video"), _settings(tmp_path), tmp_path)
