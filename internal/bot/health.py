"""Build an on-demand owner report without download or account details."""

import asyncio
import math
import re
import time
from pathlib import Path

from curl_cffi.requests import AsyncSession


PROBE_URL = "https://www.gstatic.com/generate_204"
PROBE_TIMEOUT = 6
SAFE_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")


def _name(value: str) -> str:
    return value if SAFE_NAME.fullmatch(value) else "configured site"


def cookie_status(path: Path, now: float | None = None) -> str:
    """Check Netscape cookie expiration locally; never report cookie contents."""
    now = time.time() if now is None else now
    try:
        if path.stat().st_size > 1_048_576:
            return "file too large to check"
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError):
        return "unreadable"
    unexpired = expired = session = invalid = 0
    for line in lines:
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            invalid += 1
            continue
        try:
            expiry = float(fields[4])
            if not math.isfinite(expiry):
                raise ValueError
        except ValueError:
            invalid += 1
            continue
        if expiry <= 0:
            session += 1
        elif expiry <= now:
            expired += 1
        else:
            unexpired += 1
    if not (unexpired or expired or session):
        return "invalid format" if invalid else "empty"
    parts = []
    if unexpired:
        parts.append(f"{unexpired} unexpired")
    if expired:
        parts.append(f"{expired} expired")
    if session:
        parts.append(f"{session} session")
    if invalid:
        parts.append(f"{invalid} invalid rows")
    return ", ".join(parts)


async def proxy_reachable(proxy: str) -> bool:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with AsyncSession(trust_env=False) as session:
                response = await session.head(
                    PROBE_URL, proxy=proxy, timeout=(3, 5),
                    allow_redirects=False, discard_cookies=True,
                )
                return 200 <= response.status_code < 300
    except Exception:
        return False


def _proxy_targets(settings) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}

    def add(proxy: str, label: str) -> None:
        if proxy:
            targets.setdefault(proxy, []).append(label)

    add(settings.proxy, "default")
    for site_id, site in sorted(settings.site_configs.items()):
        if not site.disable_proxy:
            add(site.proxy, _name(site_id))
        add(site.download_proxy, f"{_name(site_id)} downloads")
    return targets


async def health_report(settings, active: int, queued: int) -> str:
    lines = [
        "PyVD health", f"Jobs: {active} active, {queued} queued", "",
        "Cookies (local expiry check; login validity is unverified):",
    ]
    directory = settings.private_dir / "cookies"
    try:
        paths = sorted(directory.glob("*.txt"))
    except OSError:
        paths = []
    if paths:
        lines.extend(f"{_name(path.stem)}: {cookie_status(path)}" for path in paths)
    else:
        lines.append("No cookie files configured.")
    lines.extend(["", "Proxy connectivity:"])
    targets = _proxy_targets(settings)
    if targets:
        limit = asyncio.Semaphore(4)

        async def probe(proxy):
            async with limit:
                return await proxy_reachable(proxy)

        results = await asyncio.gather(*(probe(proxy) for proxy in targets))
        for labels, reachable in zip(targets.values(), results):
            lines.append(f"{', '.join(labels)}: {'reachable' if reachable else 'unreachable'}")
    else:
        lines.append("No proxy configured.")
    edge_sites = [
        _name(site_id) for site_id, site in sorted(settings.site_configs.items())
        if site.edge_proxy
    ]
    if edge_sites:
        lines.append(f"Unsupported edge proxy configured: {', '.join(edge_sites)}")
    return "\n".join(lines)
