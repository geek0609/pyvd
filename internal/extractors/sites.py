"""Recognize govd links and yt-dlp's named site extractors."""

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import parse_qs, urlsplit, urlunsplit


SITE_NAMES = {
    "facebook": "Facebook", "instagram": "Instagram", "ninegag": "9GAG",
    "pinterest": "Pinterest", "reddit": "Reddit", "soundcloud": "SoundCloud",
    "threads": "Threads", "tiktok": "TikTok", "twitter": "X", "youtube": "YouTube",
}
OTHER_SITE_ID = "ytdlp"
PUBLIC_GROUP_SITE_NAMES = {
    **SITE_NAMES,
    "kika": "KiKA",
    "lego": "LEGO",
    "nick.com": "Nickelodeon",
    "pbskids": "PBS Kids",
    "toggo": "TOGGO",
}
PUBLIC_GROUP_HOSTS = {
    "facebook": ("facebook.com", "fb.watch"),
    "instagram": ("instagram.com", "ddinstagram.com"),
    "kika": ("kika.de",),
    "lego": ("lego.com",),
    "ninegag": ("9gag.com",),
    "nick.com": ("nick.com",),
    "pbskids": ("pbskids.org",),
    "pinterest": ("pinterest.com", "pin.it"),
    "reddit": ("reddit.com", "redd.it", "redditmedia.com"),
    "soundcloud": ("soundcloud.com",),
    "threads": ("threads.net", "threads.com"),
    "tiktok": ("tiktok.com",),
    "toggo": ("toggo.de",),
    "twitter": ("twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com"),
    "youtube": ("youtube.com", "youtube-nocookie.com", "youtu.be"),
}


@lru_cache(maxsize=1)
def _named_extractors() -> tuple[type, ...]:
    from yt_dlp.extractor import gen_extractor_classes

    return tuple(
        extractor for extractor in gen_extractor_classes()
        if extractor.ie_key() != "Generic" and getattr(extractor, "_ENABLED", True) is not False
    )


def _site_id(extractor: type) -> str:
    name = extractor.IE_NAME.split(":", 1)[0].lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", name).strip("-._")
    if len(slug) > 30:
        slug = slug[:21] + "-" + hashlib.sha256(name.encode()).hexdigest()[:8]
    return slug


@lru_cache(maxsize=2048)
def _matching_extractor(url: str) -> type | None:
    for extractor in _named_extractors():
        if extractor.suitable(url):
            return extractor
    return None


@lru_cache(maxsize=1)
def _catalog() -> tuple[tuple[str, str], ...]:
    names = {}
    for extractor in _named_extractors():
        names.setdefault(_site_id(extractor), extractor.IE_NAME.split(":", 1)[0])
    names.update(SITE_NAMES)
    return tuple(sorted(names.items(), key=lambda item: item[1].casefold()))


def search_extractors(term: str, limit: int = 20) -> tuple[list[tuple[str, str]], int]:
    term = term.casefold().strip()
    matches = [item for item in _catalog() if term in item[0].casefold() or term in item[1].casefold()]
    return matches[:limit], len(matches)


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


def allowed_in_public_group(request: Request) -> bool:
    host = (urlsplit(request.url).hostname or "").lower()
    return _host_matches(host, *PUBLIC_GROUP_HOSTS.get(request.extractor_id, ()))


def identify(url: str) -> Request | None:
    try:
        url = url.strip()
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host or len(url) > 4096:
            return None
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return None
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
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
            match = re.search(r"/(?:shorts|embed|v|live)/([\w-]{11})(?:/|$)", path)
            content_id = match.group(1) if match else ""
        if not re.fullmatch(r"[\w-]{11}", content_id):
            return None
    elif _host_matches(host, "instagram.com", "ddinstagram.com"):
        site = "instagram"
        match = re.search(r"/(?:p|reel|reels|tv|stories/[^/]+|share/[^/]+)/([^/?#]+)", path)
        content_id = match.group(1) if match else ""
    elif _host_matches(host, "tiktok.com"):
        site = "tiktok"
        match = re.search(r"/(?:video|photo|v|p)/(\d+)", path)
        content_id = match.group(1) if match else ""
        if not content_id and host in {"vm.tiktok.com", "vt.tiktok.com"} and path.strip("/"):
            content_id = hashlib.sha256(url.encode()).hexdigest()[:32]
    elif _host_matches(host, "twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com", "t.co"):
        site = "twitter"
        match = re.search(r"/status/(\d+)", path)
        content_id = match.group(1) if match else ""
        if not content_id and host == "t.co" and path.strip("/"):
            content_id = hashlib.sha256(url.encode()).hexdigest()[:32]
    elif _host_matches(host, "facebook.com", "fb.watch"):
        site = "facebook"
        match = re.search(r"/(?:videos|reel|share/[rvp])/([^/?#]+)", path)
        content_id = match.group(1) if match else parse_qs(parsed.query).get("v", [""])[0]
        if not content_id and host == "fb.watch" and path.strip("/"):
            content_id = hashlib.sha256(url.encode()).hexdigest()[:32]
    elif _host_matches(host, "reddit.com", "redd.it", "redditmedia.com"):
        site = "reddit"
        match = re.search(r"/(?:comments|gallery|s)/([^/?#]+)", path)
        content_id = match.group(1) if match else (
            path.strip("/").split("/")[0] if host in {"redd.it", "www.redd.it"} else ""
        )
    elif _host_matches(host, "pinterest.com", "pin.it") or host.startswith("pinterest."):
        site = "pinterest"
        match = re.search(r"/pin/([\w-]+)", path)
        content_id = match.group(1) if match else (path.strip("/").split("/")[0] if host == "pin.it" else "")
    elif _host_matches(host, "soundcloud.com"):
        site = "soundcloud"
        parts = path.strip("/").split("/")
        content_id = parts[1] if len(parts) > 1 and parts[1] not in {
            "sets", "likes", "reposts", "tracks", "followers", "following",
        } else ""
    elif _host_matches(host, "9gag.com"):
        site = "ninegag"
        match = re.search(r"/gag/([^/?#]+)", path)
        content_id = match.group(1) if match else ""
    elif _host_matches(host, "threads.net", "threads.com"):
        site = "threads"
        match = re.search(r"/(?:post|p)/([\w-]+)", path)
        content_id = match.group(1) if match else ""

    if not site:
        extractor = _matching_extractor(url)
        if extractor is None:
            return None
        site = _site_id(extractor)
        if not site:
            return None
        clean_url = urlunsplit((parsed.scheme, parsed.netloc.lower(), path, parsed.query, parsed.fragment))
        try:
            native_id = extractor._match_id(clean_url)
        except (AttributeError, IndexError, ValueError):
            native_id = None
        content_id = (
            native_id if isinstance(native_id, str) and 0 < len(native_id) <= 50
            else hashlib.sha256(clean_url.encode()).hexdigest()[:32]
        )
        return Request(site, content_id, clean_url)
    if not content_id:
        return None
    clean_url = urlunsplit(("https", host, parsed.path, parsed.query, ""))
    return Request(site, content_id[:150], clean_url)


URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def first_supported_url(text: str) -> Request | None:
    for match in URL_RE.finditer(text):
        request = identify(match.group(0).rstrip(".,!?)]}"))
        if request:
            return request
    return None
