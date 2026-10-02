"""Copy HTTP media into a pipe with bounded buffering and validated ranges."""

import copy
import errno
import os
import re
import select
import threading
from http.cookiejar import CookieJar
from pathlib import Path


class SourceCancelled(Exception):
    pass


class SourceProtocolError(Exception):
    pass


class _HTTPError(SourceProtocolError):
    def __init__(self, status: int):
        self.status = status


RETRIES = 3
RETRY_DELAY = 0.25


class PipeWriter:
    def __init__(self, path: Path, stopped: threading.Event):
        self.name = str(path)
        self.stopped = stopped
        self.bytes_written = 0
        self.closed = True
        self._fd: int | None = None

    def __enter__(self):
        while not self.stopped.is_set():
            try:
                self._fd = os.open(self.name, os.O_WRONLY | os.O_NONBLOCK)
                self.closed = False
                return self
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise
                self.stopped.wait(0.05)
        raise SourceCancelled

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        while view:
            if self.stopped.is_set():
                raise SourceCancelled
            try:
                written = os.write(self._fd, view)
            except BlockingIOError:
                select.select([], [self._fd], [], 0.1)
                continue
            self.bytes_written += written
            view = view[written:]
        return len(data)

    def flush(self) -> None:
        pass

    def tell(self) -> int:
        return self.bytes_written

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.closed = True

    def __exit__(self, *_):
        self.close()


class _Response:
    def __init__(self, offset: int, end: int | None, total: int | None, validator: str | None = None):
        self.offset, self.end, self.total = offset, end, total
        self.status = 0
        self.headers: dict[str, str] = {}
        self.expected: int | None = None
        self.error: Exception | None = None
        self.validated = False
        self.range_supported = False
        self.validator = validator

    def header(self, line: bytes) -> int:
        decoded = line.decode("latin-1").strip()
        if decoded.startswith("HTTP/"):
            self.status = int(decoded.split()[1])
            self.headers.clear()
        elif ":" in decoded:
            key, value = decoded.split(":", 1)
            self.headers[key.lower()] = value.strip()
        return len(line)

    def validate(self) -> None:
        if self.validated:
            return
        if self.status not in {200, 206}:
            raise _HTTPError(self.status)
        if self.headers.get("content-encoding", "identity").lower() != "identity":
            raise SourceProtocolError("Encoded media cannot be ranged safely")
        validator = self.headers.get("etag")
        if not validator or validator.startswith("W/"):
            validator = self.headers.get("last-modified")
        if self.validator and validator and validator != self.validator:
            raise SourceProtocolError("The source changed during the media transfer")
        self.validator = self.validator or validator
        if self.status == 200 and self.offset == 0:
            length = self.headers.get("content-length")
            self.expected = int(length) if length else None
            self.total = self.expected
            self.validated = True
            return
        if self.status != 206:
            raise SourceProtocolError("The source did not honor the media range")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", self.headers.get("content-range", ""))
        if not match:
            raise SourceProtocolError("The source returned an invalid media range")
        start, end, total = map(int, match.groups())
        if (
            start != self.offset or end < start or end >= total
            or (self.end is not None and end > self.end)
            or (self.total is not None and total != self.total)
        ):
            raise SourceProtocolError("The source changed the media range")
        self.expected, self.total = end - start + 1, total
        self.range_supported = True
        self.validated = True


def _copy_http(fmt: dict, output: PipeWriter, proxy: str, cookies: CookieJar) -> None:
    from curl_cffi import CurlOpt, requests

    jar = CookieJar()
    for cookie in cookies:
        jar.set_cookie(copy.copy(cookie))
    headers = {key: value for key, value in (fmt.get("http_headers") or {}).items() if key.lower() != "accept-encoding"}
    headers["Accept-Encoding"] = "identity"
    chunk = int((fmt.get("downloader_options") or {}).get("http_chunk_size") or 0)
    chunk = min(max(chunk, 0), 16 * 1024 * 1024)
    total = None
    validator = None
    failures = 0
    range_supported = False
    state: _Response

    def header(line: bytes) -> int:
        return state.header(line)

    with requests.Session(
        cookies=jar, trust_env=False,
        curl_options={CurlOpt.HEADERFUNCTION: header, CurlOpt.CONNECTTIMEOUT_MS: 30_000},
    ) as session:
        while not output.stopped.is_set():
            offset = output.bytes_written
            end = offset + chunk - 1 if chunk else None
            if total is not None and end is not None:
                end = min(end, total - 1)
            state = _Response(offset, end, total, validator)
            request_headers = dict(headers)
            if chunk or offset:
                request_headers["Range"] = f"bytes={offset}-{end if end is not None else ''}"
            if offset and validator:
                request_headers["If-Range"] = validator

            def write(data: bytes) -> int:
                if 300 <= state.status < 400:
                    return len(data)
                try:
                    state.validate()
                    if state.expected is not None and output.bytes_written - offset + len(data) > state.expected:
                        raise SourceProtocolError("The source exceeded the media range")
                    return output.write(data)
                except Exception as exc:
                    state.error = exc
                    return 0

            try:
                response = session.get(
                    fmt["url"], headers=request_headers, proxy=proxy or None,
                    content_callback=write, timeout=None,
                )
                try:
                    if state.error is not None:
                        raise state.error
                    state.validate()
                    response.raise_for_status()
                finally:
                    response.close()
            except Exception as exc:
                error = state.error or exc
                transient = (
                    isinstance(error, _HTTPError) and error.status in {408, 429, 500, 502, 503, 504}
                ) or (
                    isinstance(error, requests.exceptions.RequestException)
                    and getattr(error, "code", None) in {5, 6, 7, 18, 28, 35, 52, 55, 56, 92}
                )
                resumable = output.bytes_written == 0 or range_supported or state.range_supported or (
                    state.validated and state.validator
                    and state.headers.get("accept-ranges", "").lower() == "bytes"
                    and state.total is not None
                )
                if not transient or not resumable or failures >= RETRIES:
                    raise error from None
                if state.validated:
                    total, validator = state.total, state.validator
                    range_supported = range_supported or state.range_supported or bool(
                        state.validator and state.headers.get("accept-ranges", "").lower() == "bytes"
                    )
                # A disconnect after every promised byte needs no replay.
                if total is not None and output.bytes_written == total:
                    return
                if output.stopped.wait(RETRY_DELAY * 2 ** failures):
                    raise SourceCancelled from None
                failures += 1
                continue
            state.validate()
            if state.expected is not None and output.bytes_written - offset != state.expected:
                raise SourceProtocolError("The source returned incomplete media")
            total = state.total
            validator = state.validator
            range_supported = state.range_supported
            failures = 0
            if not state.range_supported or output.bytes_written == total:
                return
        raise SourceCancelled


def copy_source(
    fmt: dict, fifo: Path, proxy: str, cookies: CookieJar,
    stopped: threading.Event, errors: list[str],
) -> None:
    try:
        with PipeWriter(fifo, stopped) as output:
            _copy_http(fmt, output, proxy, cookies)
    except SourceCancelled:
        pass
    except Exception as exc:
        errors.append(type(exc).__name__)
