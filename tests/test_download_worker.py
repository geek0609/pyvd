import io
import json
import logging
import stat
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from internal.core.errors import (
    AuthenticationRequired, DurationTooLong, FileTooLarge, MediaError, NoMedia,
    SessionCheckRequired,
)
from internal.extractors import download_worker
from internal.extractors.downloader import _download_in_process
from internal.extractors.sites import Request
from internal.models.media import Media, MediaItem


ERRORS = [
    MediaError, NoMedia, FileTooLarge, DurationTooLong,
    AuthenticationRequired, SessionCheckRequired,
]


def request():
    return Request("youtube", "video-id", "https://youtu.be/video-id")


def job(tmp_path, use_cookies=True):
    return {
        "root": str(tmp_path), "workdir": str(tmp_path),
        "extractor_id": "youtube", "content_id": "video-id",
        "url": "https://youtu.be/video-id", "use_cookies": use_cookies,
    }


def run_worker(tmp_path, monkeypatch, download, use_cookies=True):
    monkeypatch.setattr(download_worker.sys, "stdin", io.StringIO(json.dumps(job(tmp_path, use_cookies)) + "\n"))
    monkeypatch.setattr(download_worker, "load_settings", lambda _: SimpleNamespace())
    monkeypatch.setattr(download_worker, "_download", download)
    previous_level = logging.root.manager.disable
    try:
        assert download_worker.main() == 0
    finally:
        logging.disable(previous_level)
    return tmp_path / "download-result.json"


def test_worker_result_is_private_and_contains_media_paths(tmp_path, monkeypatch, capsys):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"media")

    def download(source, settings, workdir, use_cookies):
        assert source == request() and workdir == tmp_path
        assert use_cookies is False
        return Media("youtube", "video-id", source.url, items=[MediaItem(kind="video", path=video)])

    result = run_worker(tmp_path, monkeypatch, download, use_cookies=False)
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    payload = json.loads(result.read_text())
    assert payload["media"]["items"][0]["path"] == str(video)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("error", ERRORS)
def test_worker_preserves_known_error_type_and_safe_message(tmp_path, monkeypatch, error, capsys):
    def download(*args):
        raise error("Safe user-facing error")

    result = run_worker(tmp_path, monkeypatch, download)
    assert json.loads(result.read_text()) == {
        "error": error.__name__, "message": "Safe user-facing error",
    }
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    assert capsys.readouterr() == ("", "")


def test_worker_hides_unexpected_exception_details(tmp_path, monkeypatch, capsys):
    def download(*args):
        raise RuntimeError("sessionid=private-cookie-value proxy-password=secret")

    result = run_worker(tmp_path, monkeypatch, download)
    assert json.loads(result.read_text()) == {
        "error": "NoMedia", "message": "Could not download media from this link.",
    }
    assert "private-cookie" not in result.read_text()
    assert capsys.readouterr() == ("", "")


def test_worker_does_not_overwrite_existing_result_or_follow_symlink(tmp_path, monkeypatch):
    sentinel = tmp_path / "untouched.txt"
    sentinel.write_text("keep existing data")
    (tmp_path / "download-result.json").symlink_to(sentinel)
    with pytest.raises(FileExistsError):
        run_worker(
            tmp_path, monkeypatch,
            lambda *args: Media("youtube", "video-id", "", items=[]),
        )
    assert sentinel.read_text() == "keep existing data"


def fake_worker(tmp_path, monkeypatch, payload, exit_code=0):
    class Process:
        returncode = exit_code

        async def communicate(self, data):
            assert json.loads(data) == job(tmp_path)
            (tmp_path / "download-result.json").write_text(json.dumps(payload, default=str))
            return b"", b""

    async def start(*args, **kwargs):
        assert args[1:] == ("-m", "internal.extractors.download_worker")
        assert kwargs["stdout"] is not None and kwargs["stderr"] is not None
        return Process()

    monkeypatch.setattr("internal.extractors.downloader.spawn_process", start)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ERRORS)
async def test_parent_reconstructs_known_worker_error_and_removes_result(tmp_path, monkeypatch, error):
    fake_worker(tmp_path, monkeypatch, {"error": error.__name__, "message": "Safe user-facing error"})
    with pytest.raises(error, match="Safe user-facing error"):
        await _download_in_process(request(), SimpleNamespace(root=tmp_path), tmp_path)
    assert not (tmp_path / "download-result.json").exists()


@pytest.mark.asyncio
async def test_parent_reconstructs_media_and_removes_result(tmp_path, monkeypatch):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    thumbnail = tmp_path / "thumbnail.jpg"
    thumbnail.write_bytes(b"photo")
    media = Media(
        "youtube", "video-id", "", nsfw=True,
        items=[MediaItem(kind="video", path=source, thumbnail=thumbnail)],
    )
    fake_worker(tmp_path, monkeypatch, {"media": asdict(media)})
    result = await _download_in_process(request(), SimpleNamespace(root=tmp_path), tmp_path)
    assert result == media
    assert not (tmp_path / "download-result.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,symlink", [("path", False), ("thumbnail", False), ("path", True)])
async def test_parent_rejects_worker_paths_outside_job_directory(tmp_path, monkeypatch, field, symlink):
    workdir = tmp_path / "job"
    workdir.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private file")
    source = workdir / "video.mp4"
    if symlink:
        source.symlink_to(outside)
    else:
        source.write_bytes(b"video")
    item = asdict(MediaItem(kind="video", path=source))
    item[field] = source if symlink else outside
    media = asdict(Media("youtube", "video-id", ""))
    media["items"] = [item]
    fake_worker(workdir, monkeypatch, {"media": media})
    with pytest.raises(NoMedia, match="unavailable"):
        await _download_in_process(request(), SimpleNamespace(root=workdir), workdir)
    assert not (workdir / "download-result.json").exists()
    assert outside.read_bytes() == b"private file"
