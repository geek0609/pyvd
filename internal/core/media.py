"""Inspect downloaded files and prepare Telegram-compatible media."""

import asyncio
import json
import logging
from pathlib import Path

from internal.config.settings import Settings
from internal.core.errors import DurationTooLong, FileTooLarge, MediaError
from internal.models.media import Media, MediaItem


LOG = logging.getLogger(__name__)
CODECS = {
    "h264": "avc", "hevc": "hevc", "h265": "hevc", "vp9": "vp9",
    "vp8": "vp8", "av1": "av1", "aac": "aac", "mp3": "mp3",
    "opus": "opus", "vorbis": "vorbis", "flac": "flac",
}


def _check_size(path: Path, settings: Settings) -> int:
    size = path.stat().st_size
    if size > settings.max_file_size:
        raise FileTooLarge("The file exceeds the 2 GB limit.")
    return size


def _prepare_photo(item: MediaItem, settings: Settings) -> None:
    from PIL import Image, ImageOps

    assert item.path is not None
    source = item.path
    _check_size(source, settings)
    target = source.with_name(source.stem + "-telegram.jpg")
    if source.suffix.lower() in {".heic", ".heif"}:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError as exc:
            raise MediaError("HEIF image support is unavailable.") from exc
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((3200, 3200))
            if image.mode != "RGB":
                image = image.convert("RGB")
            image.save(target, "JPEG", quality=86, optimize=True)
    except (OSError, ValueError) as exc:
        raise MediaError("Could not prepare this image.") from exc
    item.path = target
    item.width, item.height = image.size
    item.size = _check_size(target, settings)
    if item.size > 10_000_000:
        raise FileTooLarge("The image is too large for a Telegram photo.")


async def _probe(path: Path) -> dict:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise MediaError("FFmpeg and ffprobe are required to process media.") from exc
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise MediaError("Media inspection timed out.") from exc
    if process.returncode:
        raise MediaError("Could not inspect the downloaded media file.")
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise MediaError("Invalid media metadata from ffprobe.") from exc


async def _thumbnail(item: MediaItem) -> None:
    assert item.path is not None
    target = item.path.with_name(item.path.stem + "-thumb.jpg")
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "1", "-i", str(item.path), "-frames:v", "1",
            "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
            "-q:v", "5", str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=60)
    except (FileNotFoundError, asyncio.TimeoutError):
        LOG.debug("thumbnail extraction unavailable for %s", item.path)
        return
    if process.returncode == 0 and target.is_file() and target.stat().st_size < 200_000:
        item.thumbnail = target


async def prepare(media: Media, settings: Settings) -> Media:
    if not media.items:
        raise MediaError("No media was found.")
    if len(media.items) > 20:
        raise MediaError("The post contains more than 20 media items.")
    for item in media.items:
        if item.path is None:
            raise MediaError("A downloaded media file is missing.")
        item.size = _check_size(item.path, settings)
        if item.kind == "photo":
            await asyncio.to_thread(_prepare_photo, item, settings)
            continue
        info = await _probe(item.path)
        streams = info.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if video:
            item.kind = "video"
            item.video_codec = CODECS.get(video.get("codec_name", ""), "")
            item.width = int(video.get("width") or item.width)
            item.height = int(video.get("height") or item.height)
        elif audio:
            item.kind = "audio"
        else:
            raise MediaError("The downloaded file has no usable media stream.")
        if audio:
            item.audio_codec = CODECS.get(audio.get("codec_name", ""), "")
        item.duration = int(float((info.get("format") or {}).get("duration") or item.duration))
        if item.duration > settings.max_duration:
            raise DurationTooLong("The media exceeds the duration limit.")
        item.bitrate = int((info.get("format") or {}).get("bit_rate") or item.bitrate)
        if item.kind == "video" and item.delivery_kind == "video":
            await _thumbnail(item)
    return media
