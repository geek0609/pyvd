"""Recognize the same site families and cache keys as govd."""

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit


SITE_NAMES = {
    "facebook": "Facebook", "instagram": "Instagram", "ninegag": "9GAG",
    "pinterest": "Pinterest", "reddit": "Reddit", "soundcloud": "SoundCloud",
    "threads": "Threads", "tiktok": "TikTok", "twitter": "X", "youtube": "YouTube",
}


@dataclass(frozen=True)
class Request:
    extractor_id: str
    content_id: str
    url: str

    @property
    def key(self) -> str:
        return f"{self.extractor_id}/{self.content_id}"


def _host_matches(host: str, *domains: str) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def identify(url: str) -> Request | None:
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return None
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return None
    except ValueError:
        return None

    path = parsed.path
    site = ""
    content_id = ""
    if _host_matches(host, "youtube.com", "youtube-nocookie.com", "youtu.be"):
        site = "youtube"
        content_id = (path.strip("/").split("/")[0] if _host_matches(host, "youtu.be")
                      else parse_qs(parsed.query).get("v", [""])[0])
        if not content_id:
            match = re.search(r"/(?:shorts|embed|v)/([\w-]{11})", path)
            content_id = match.group(1) if match else ""
    elif _host_matches(host, "instagram.com", "ddinstagram.com"):
        site = "instagram"
        match = re.search(r"/(?:p|reel|reels|tv|stories/[^/]+|share/[^/]+)/([^/?#]+)", path)
        content_id = match.group(1) if match else ""
    elif _host_matches(host, "tiktok.com"):
        site = "tiktok"
        match = re.search(r"/(?:video|photo|v|p)/(\d+)", path)
        content_id = match.group(1) if match else ""
    elif _host_matches(host, "twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com", "t.co"):
        site = "twitter"
        match = re.search(r"/status/(\d+)", path)
        content_id = match.group(1) if match else ""
    elif _host_matches(host, "facebook.com", "fb.watch"):
        site = "facebook"
        match = re.search(r"/(?:videos|reel|share/[rvp])/([^/?#]+)", path)
        content_id = match.group(1) if match else parse_qs(parsed.query).get("v", [""])[0]
    elif _host_matches(host, "reddit.com", "redd.it", "redditmedia.com"):
        site = "reddit"
        match = re.search(r"/(?:comments|s)/([^/?#]+)", path)
        content_id = match.group(1) if match else (path.strip("/").split("/")[0] if host == "redd.it" else "")
    elif _host_matches(host, "pinterest.com", "pin.it") or host.startswith("pinterest."):
        site = "pinterest"
        match = re.search(r"/pin/([\w-]+)", path)
        content_id = match.group(1) if match else (path.strip("/").split("/")[0] if host == "pin.it" else "")
    elif _host_matches(host, "soundcloud.com"):
        site = "soundcloud"
        parts = path.strip("/").split("/")
        content_id = parts[1] if len(parts) > 1 else ""
    elif _host_matches(host, "9gag.com"):
        site = "ninegag"
        match = re.search(r"/gag/([^/?#]+)", path)
        content_id = match.group(1) if match else ""
    elif _host_matches(host, "threads.net", "threads.com"):
        site = "threads"
        match = re.search(r"/(?:post|p)/([\w-]+)", path)
        content_id = match.group(1) if match else ""

    if not site:
        return None
    clean_url = urlunsplit(("https", host, parsed.path, parsed.query, ""))
    if not content_id:
        content_id = hashlib.sha256(clean_url.encode()).hexdigest()[:32]
    return Request(site, content_id[:150], clean_url)


URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def first_supported_url(text: str) -> Request | None:
    for match in URL_RE.finditer(text):
        request = identify(match.group(0).rstrip(".,!?)]}"))
        if request:
            return request
    return None
