"""Media and chat data shared by extraction, storage, and Telegram delivery."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MediaItem:
    kind: str  # photo, video, or audio; unsupported codecs are sent as documents
    path: Path | None = None
    file_id: str = ""
    format_id: str = "default"
    size: int = 0
    duration: int = 0
    width: int = 0
    height: int = 0
    title: str = ""
    artist: str = ""
    audio_codec: str = ""
    video_codec: str = ""
    bitrate: int = 0
    thumbnail: Path | None = None

    @property
    def delivery_kind(self) -> str:
        if self.kind == "video" and (
            self.video_codec != "avc" or self.audio_codec not in {"", "aac", "mp3"}
        ):
            return "document"
        if self.kind == "audio" and self.audio_codec not in {"aac", "mp3"}:
            return "document"
        return self.kind


@dataclass
class Media:
    extractor_id: str
    content_id: str
    url: str
    caption: str = ""
    nsfw: bool = False
    items: list[MediaItem] = field(default_factory=list)


@dataclass
class ChatSettings:
    chat_id: int
    kind: str
    captions: bool
    silent: bool
    nsfw: bool
    media_album_limit: int
    delete_links: bool
