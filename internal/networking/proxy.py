"""Convert govd's PROXY URL to Hydrogram's proxy settings."""

from urllib.parse import unquote, urlsplit


def hydrogram_proxy(value: str) -> dict | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "socks4", "socks5"} or not parsed.hostname or not parsed.port:
        raise ValueError("PROXY must be an http, socks4, or socks5 URL with a port")
    result = {"scheme": parsed.scheme, "hostname": parsed.hostname, "port": parsed.port}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result
