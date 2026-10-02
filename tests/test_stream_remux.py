import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from functools import partial
from http.cookiejar import CookieJar
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from internal.extractors import stream_worker


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg and ffprobe are required for remux integration tests",
)


def _ffmpeg(*arguments: str) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *arguments],
        check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def _probe(path: Path) -> dict:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True, timeout=15, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    return json.loads(output)


def _video_frames(path: Path) -> list[str]:
    output = _ffmpeg("-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "framemd5", "-")
    return [line.rsplit(",", 1)[-1].strip() for line in output.decode().splitlines()
            if line and not line.startswith("#")]


def _audio_packets(path: Path) -> list[str]:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_packets",
         "-show_data_hash", "sha256", "-show_entries", "packet=data_hash", "-of", "json", str(path)],
        check=True, timeout=15, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    return [packet["data_hash"] for packet in json.loads(output)["packets"]]


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@contextmanager
def _serve(directory: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(scope="module")
def originals(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-remux-originals")
    paths = {}
    for audio in (False, True):
        path = root / f"video-{audio}.mp4"
        command = ["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24"]
        if audio:
            command.extend(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"])
        command.extend(["-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "24"])
        if audio:
            command.extend(["-c:a", "aac"])
        command.extend(["-movflags", "+faststart", str(path)])
        _ffmpeg(*command)
        paths[audio] = path
    return paths


@pytest.mark.parametrize("segment_type", ["mpegts", "fmp4"])
@pytest.mark.parametrize("audio", [False, True], ids=["silent", "aac"])
def test_hls_native_remux_preserves_media(originals, tmp_path: Path, segment_type: str, audio: bool) -> None:
    original = originals[audio]
    media = tmp_path / "media"
    media.mkdir()
    _ffmpeg("-i", str(original), "-c", "copy", "-f", "hls", "-hls_time", "1",
            "-hls_playlist_type", "vod", "-hls_segment_type", segment_type, str(media / "index.m3u8"))
    fifo = tmp_path / "source.fifo"
    os.mkfifo(fifo, 0o600)
    stopped, errors = threading.Event(), []
    output = tmp_path / "remux.mp4"
    with _serve(media) as base:
        formats = [{"url": base + "/index.m3u8", "protocol": "m3u8_native", "ext": "mp4"}]
        with output.open("wb") as destination:
            process = subprocess.Popen(
                stream_worker._remux_command([fifo], formats), stdout=destination, stderr=subprocess.PIPE,
            )
            worker = threading.Thread(
                target=stream_worker._copy_segments,
                args=(formats[0], fifo, "", CookieJar(), stopped, errors), daemon=True,
            )
            worker.start()
            try:
                _, stderr = process.communicate(timeout=30)
                worker.join(timeout=5)
                assert process.returncode == 0, stderr.decode(errors="replace")
                assert not errors and not worker.is_alive()
                assert not list(tmp_path.glob("source.fifo-Frag*"))
            finally:
                stopped.set()
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
                worker.join(timeout=3)
                fifo.unlink(missing_ok=True)
    expected, actual = _probe(original)["streams"], _probe(output)["streams"]
    signature = lambda streams: [(entry["codec_name"], entry.get("width"), entry.get("height")) for entry in streams]
    assert signature(actual) == signature(expected)
    assert actual[0]["codec_name"] == "h264" and (actual[0]["width"], actual[0]["height"]) == (320, 180)
    frames = _video_frames(original)
    assert len(frames) == 48 and _video_frames(output) == frames
    if audio:
        assert actual[1]["codec_name"] == "aac"
        packets = _audio_packets(original)
        assert packets and _audio_packets(output) == packets
