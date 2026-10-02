import threading
import os
import time
import socket
from contextlib import contextmanager
from http.cookiejar import Cookie, CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from internal.extractors.source import PipeWriter, SourceCancelled, SourceProtocolError, _copy_http


class Output:
    def __init__(self):
        self.stopped = threading.Event()
        self.data = bytearray()

    @property
    def bytes_written(self):
        return len(self.data)

    def write(self, data):
        self.data.extend(data)
        return len(data)


@contextmanager
def server(respond):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            respond(self)

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}"
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_ranges_preserve_bytes_and_headers():
    data = b"0123456789abcdef"
    seen = []

    def respond(request):
        seen.append((request.headers["Range"], request.headers["Referer"], request.headers["Accept-Encoding"]))
        start, end = map(int, request.headers["Range"][6:].split("-"))
        end = min(end, len(data) - 1)
        request.send_response(206)
        request.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        request.send_header("Content-Length", str(end - start + 1))
        request.end_headers()
        request.wfile.write(data[start:end + 1])

    output = Output()
    with server(respond) as url:
        _copy_http({"url": url, "http_headers": {"Referer": "https://example.org/"},
                    "downloader_options": {"http_chunk_size": 5}}, output, "", CookieJar())
    assert output.data == data
    assert [row[0] for row in seen] == ["bytes=0-4", "bytes=5-9", "bytes=10-14", "bytes=15-15"]
    assert all(row[1:] == ("https://example.org/", "identity") for row in seen)


@pytest.mark.parametrize("chunk", [0, 5])
def test_unranged_server_uses_one_request(chunk):
    seen = []

    def respond(request):
        seen.append(request.path)
        request.send_response(200)
        request.send_header("Content-Length", "12")
        request.end_headers()
        request.wfile.write(b"hello world!")

    output = Output()
    with server(respond) as url:
        _copy_http({"url": url, "downloader_options": {"http_chunk_size": chunk}}, output, "", CookieJar())
    assert output.data == b"hello world!"
    assert len(seen) == 1


@pytest.mark.parametrize("status,range_header", [(206, "bytes 2-4/10"), (206, "garbage"), (403, "")])
def test_invalid_response_never_enters_media_pipe(status, range_header):
    def respond(request):
        request.send_response(status)
        request.send_header("Content-Range", range_header)
        request.end_headers()
        request.wfile.write(b"error or wrong bytes")

    output = Output()
    with server(respond) as url, pytest.raises(SourceProtocolError):
        _copy_http({"url": url, "downloader_options": {"http_chunk_size": 5}}, output, "", CookieJar())
    assert output.bytes_written == 0


def test_ignored_subsequent_range_never_duplicates_bytes():
    def respond(request):
        first = request.headers["Range"] == "bytes=0-4"
        request.send_response(206 if first else 200)
        request.send_header("Content-Range", "bytes 0-4/10")
        request.send_header("Content-Length", "5")
        request.end_headers()
        request.wfile.write(b"01234")

    output = Output()
    with server(respond) as url, pytest.raises(SourceProtocolError):
        _copy_http({"url": url, "downloader_options": {"http_chunk_size": 5}}, output, "", CookieJar())
    assert output.data == b"01234"


def test_redirect_body_is_not_media_and_cookies_keep_their_scope():
    seen = []

    def respond(request):
        seen.append(request.headers.get("Cookie", ""))
        if request.path == "/redirect":
            request.send_response(302)
            request.send_header("Location", "/media")
            request.end_headers()
            request.wfile.write(b"redirect body")
        else:
            request.send_response(200)
            request.send_header("Content-Length", "5")
            request.end_headers()
            request.wfile.write(b"media")

    jar = CookieJar()
    for domain, name in [("127.0.0.1", "local"), ("example.org", "private")]:
        jar.set_cookie(Cookie(0, name, "secret", None, False, domain, True, False, "/", True,
                              False, None, True, None, None, {}))
    output = Output()
    with server(respond) as url:
        _copy_http({"url": url + "/redirect"}, output, "", jar)
    assert output.data == b"media"
    assert all("local=secret" in cookie and "private" not in cookie for cookie in seen)


def test_pipe_wait_and_backpressure_can_be_cancelled(tmp_path):
    fifo = tmp_path / "source.fifo"
    os.mkfifo(fifo, 0o600)
    stopped = threading.Event()
    outcome = []

    def copy():
        try:
            with PipeWriter(fifo, stopped) as writer:
                writer.write(b"x" * 1024 * 1024)
        except SourceCancelled:
            outcome.append("cancelled")

    thread = threading.Thread(target=copy)
    thread.start()
    time.sleep(0.1)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        time.sleep(0.1)
        stopped.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert outcome == ["cancelled"]
    finally:
        stopped.set()
        os.close(reader)
        thread.join(timeout=2)


def test_pipe_open_without_reader_can_be_cancelled(tmp_path):
    fifo = tmp_path / "source.fifo"
    os.mkfifo(fifo, 0o600)
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(SourceCancelled):
        with PipeWriter(fifo, stopped):
            pytest.fail("The pipe must stay closed")


def test_interrupted_range_resumes_from_emitted_bytes(monkeypatch):
    monkeypatch.setattr("internal.extractors.source.RETRY_DELAY", 0)
    data = b"0123456789abcdef"
    seen = []

    def respond(request):
        seen.append((request.headers["Range"], request.headers.get("If-Range")))
        start, end = map(int, request.headers["Range"][6:].split("-"))
        end = min(end, len(data) - 1)
        request.send_response(206)
        request.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        request.send_header("Content-Length", str(end - start + 1))
        request.send_header("ETag", '"media-version"')
        request.end_headers()
        payload = data[start:end + 1]
        request.wfile.write(payload[:3] if len(seen) == 1 else payload)
        request.wfile.flush()
        if len(seen) == 1:
            request.connection.shutdown(socket.SHUT_WR)

    output = Output()
    with server(respond) as url:
        _copy_http({"url": url, "downloader_options": {"http_chunk_size": 8}}, output, "", CookieJar())
    assert output.data == data
    assert seen[1] == ("bytes=3-10", '"media-version"')


def test_transient_status_retries_are_bounded_and_never_write_error_body(monkeypatch):
    monkeypatch.setattr("internal.extractors.source.RETRY_DELAY", 0)
    seen = []

    def respond(request):
        seen.append(request.path)
        request.send_response(503)
        request.end_headers()
        request.wfile.write(b"temporary error")

    output = Output()
    with server(respond) as url, pytest.raises(SourceProtocolError):
        _copy_http({"url": url}, output, "", CookieJar())
    assert output.bytes_written == 0
    assert len(seen) == 4


@pytest.mark.parametrize("changed", ["etag", "length"])
def test_changed_media_after_interruption_is_rejected(monkeypatch, changed):
    monkeypatch.setattr("internal.extractors.source.RETRY_DELAY", 0)
    seen = []

    def respond(request):
        seen.append(request.path)
        start, end = map(int, request.headers["Range"][6:].split("-"))
        request.send_response(206)
        total = 20 if len(seen) > 1 and changed == "length" else 10
        request.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        request.send_header("Content-Length", str(end - start + 1))
        request.send_header("ETag", '"changed"' if len(seen) > 1 and changed == "etag" else '"original"')
        request.end_headers()
        request.wfile.write(b"ab" if len(seen) == 1 else b"x" * (end - start + 1))
        request.wfile.flush()
        request.connection.shutdown(socket.SHUT_WR)

    output = Output()
    with server(respond) as url, pytest.raises(SourceProtocolError):
        _copy_http({"url": url, "downloader_options": {"http_chunk_size": 5}}, output, "", CookieJar())
    assert output.data == b"ab"
    assert len(seen) == 2


def test_interrupted_unranged_source_is_not_replayed(monkeypatch):
    monkeypatch.setattr("internal.extractors.source.RETRY_DELAY", 0)
    seen = []

    def respond(request):
        seen.append(request.path)
        request.send_response(200)
        request.send_header("Content-Length", "10")
        request.end_headers()
        request.wfile.write(b"ab")
        request.wfile.flush()
        request.connection.shutdown(socket.SHUT_WR)

    output = Output()
    with server(respond) as url, pytest.raises(Exception):
        _copy_http({"url": url}, output, "", CookieJar())
    assert output.data == b"ab"
    assert len(seen) == 1
