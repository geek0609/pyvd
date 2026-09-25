"""Configuration compatible with govd's environment and private directory."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values


TWO_GB = 2_000_000_000


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if value.lower() in {"true", "1", "yes", "on"}:
        return True
    if value.lower() in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


def _ids(value: str | None) -> frozenset[int]:
    return frozenset(int(part.strip()) for part in (value or "").split(",") if part.strip())


def _duration(value: str | None) -> int:
    """Parse the Go-style durations used in govd's .env, in seconds."""
    if not value:
        return 3600
    parts = list(re.finditer(r"(\d+)(h|m|s)", value))
    if not parts or "".join(m.group(0) for m in parts) != value:
        raise ValueError("MAX_DURATION must use h, m, or s units")
    return sum(int(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[m.group(2)] for m in parts)


@dataclass(frozen=True)
class SiteConfig:
    proxy: str = ""
    download_proxy: str = ""
    edge_proxy: str = ""
    disable_proxy: bool = False
    ignore_regex: tuple[re.Pattern[str], ...] = ()
    impersonate: bool = False
    disabled: bool = False
    instance: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "SiteConfig":
        if not isinstance(value, dict):
            raise ValueError("each private/config.yaml site must be a mapping")
        proxy = str(value.get("proxy") or "")
        edge_proxy = str(value.get("edge_proxy") or "")
        disable_proxy = bool(value.get("disable_proxy", False))
        if sum((bool(proxy), bool(edge_proxy), disable_proxy)) > 1:
            raise ValueError("proxy, edge_proxy and disable_proxy cannot be combined")
        return cls(
            proxy=proxy,
            download_proxy=str(value.get("download_proxy") or ""),
            edge_proxy=edge_proxy,
            disable_proxy=disable_proxy,
            ignore_regex=tuple(re.compile(str(item)) for item in value.get("ignore_regex", [])),
            impersonate=bool(value.get("impersonate", False)),
            disabled=bool(value.get("disabled", False)),
            instance=tuple(str(item) for item in value.get("instance", [])),
        )


@dataclass(frozen=True)
class Settings:
    root: Path
    api_id: int
    api_hash: str
    bot_token: str
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    downloads_dir: Path
    max_file_size: int
    max_duration: int
    concurrent_updates: int
    caching: bool
    proxy: str
    whitelist: frozenset[int]
    admins: frozenset[int]
    default_captions: bool
    default_silent: bool
    default_nsfw: bool
    default_media_album_limit: int
    default_delete_links: bool
    captions_header: str
    captions_description: str
    log_level: str
    metrics_port: int
    site_configs: dict[str, SiteConfig] = field(default_factory=dict)

    @property
    def private_dir(self) -> Path:
        return self.root / "private"

    def site(self, extractor_id: str) -> SiteConfig:
        return self.site_configs.get(extractor_id, SiteConfig())

    def cookie_path(self, extractor_id: str) -> Path:
        return self.private_dir / "cookies" / f"{extractor_id}.txt"


def load_settings(root: Path | None = None) -> Settings:
    root = (root or Path(__file__).resolve().parents[2]).resolve()
    values = {k: v for k, v in dotenv_values(root / ".env").items() if v is not None}
    values.update(os.environ)

    def get(key: str, default: str = "") -> str:
        return str(values.get(key) or default)

    required = ("API_ID", "API_HASH", "BOT_TOKEN", "DB_HOST", "DB_NAME", "DB_USER")
    missing = [key for key in required if not get(key)]
    if missing:
        raise ValueError(f"missing required settings: {', '.join(missing)}")

    # govd's MAX_FILE_SIZE is in MB. Telegram's requested cap is 2 GB decimal.
    max_size = min(int(get("MAX_FILE_SIZE", "2000")) * 1_000_000, TWO_GB)
    if max_size <= 0:
        raise ValueError("MAX_FILE_SIZE must be positive")
    site_config_path = root / "private" / "config.yaml"
    raw_sites = yaml.safe_load(site_config_path.read_text()) if site_config_path.exists() else {}
    if raw_sites is None:
        raw_sites = {}
    if not isinstance(raw_sites, dict):
        raise ValueError("private/config.yaml must contain a mapping")
    site_configs = {str(name): SiteConfig.from_mapping(value) for name, value in raw_sites.items()}
    admins = _ids(get("ADMINS"))
    whitelist = _ids(get("WHITELIST"))
    if whitelist:
        whitelist |= admins

    downloads_dir = Path(get("DOWNLOADS_DIR", "downloads"))
    if not downloads_dir.is_absolute():
        downloads_dir = root / downloads_dir

    return Settings(
        root=root,
        api_id=int(get("API_ID")),
        api_hash=get("API_HASH"),
        bot_token=get("BOT_TOKEN"),
        db_host=get("DB_HOST"),
        db_port=int(get("DB_PORT", "5432")),
        db_name=get("DB_NAME"),
        db_user=get("DB_USER"),
        db_password=get("DB_PASSWORD"),
        downloads_dir=downloads_dir,
        max_file_size=max_size,
        max_duration=_duration(get("MAX_DURATION", "1h")),
        concurrent_updates=max(1, int(get("CONCURRENT_UPDATES", "8"))),
        caching=_bool(get("CACHING", "true")),
        proxy=get("PROXY"),
        whitelist=whitelist,
        admins=admins,
        default_captions=_bool(get("DEFAULT_ENABLE_CAPTIONS"), True),
        default_silent=_bool(get("DEFAULT_ENABLE_SILENT")),
        default_nsfw=_bool(get("DEFAULT_ENABLE_NSFW")),
        default_media_album_limit=int(get("DEFAULT_MEDIA_ALBUM_LIMIT", "10")),
        default_delete_links=_bool(get("DEFAULT_DELETE_LINKS")),
        captions_header=get("CAPTIONS_HEADER", "<a href='{{url}}'>source</a> - @{{username}}"),
        captions_description=get("CAPTIONS_DESCRIPTION", "<blockquote expandable>{{text}}</blockquote>"),
        log_level=get("LOG_LEVEL", "info").upper(),
        metrics_port=int(get("METRICS_PORT", "0")),
        site_configs=site_configs,
    )
