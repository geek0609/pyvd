"""Deliver prepared media through Hydrogram."""

import html
import time

from hydrogram import Client, enums, raw, types, utils
from hydrogram.errors import FilePartMissing

from internal.config.settings import Settings
from internal.core.errors import FileTooLarge, MediaError
from internal.models.media import ChatSettings, Media, MediaItem


def format_caption(media: Media, chat: ChatSettings, settings: Settings, username: str) -> str:
    def render(template: str, text: str) -> str:
        replacements = {
            "{{url}}": html.escape(media.url, quote=True),
            "{{username}}": html.escape(username, quote=True),
            "{{text}}": html.escape(text, quote=True),
        }
        for key, value in replacements.items():
            template = template.replace(key, value)
        return template

    header = render(settings.captions_header, "")
    if len(header) > 1024:
        raise MediaError("The configured caption header is too long.")
    if not chat.captions or not media.caption:
        return header
    text = media.caption[:600]
    while text:
        description = render(settings.captions_description, text)
        result = header + "\n" + description
        if len(result) <= 1024:
            return result
        text = text[: max(0, len(text) - 30)]
    return header


def message_file_id(message: types.Message) -> str:
    for field in ("video", "photo", "audio", "document", "animation"):
        value = getattr(message, field, None)
        if value and getattr(value, "file_id", None):
            return value.file_id
    raise MediaError("Telegram did not return a reusable media ID.")


def input_media(item: MediaItem, caption: str, spoiler: bool) -> types.InputMedia:
    source = item.file_id or str(item.path)
    if item.delivery_kind == "photo":
        return types.InputMediaPhoto(source, caption=caption, has_spoiler=spoiler)
    if item.delivery_kind == "video":
        return types.InputMediaVideo(
            source, caption=caption, duration=item.duration, width=item.width,
            height=item.height, thumb=str(item.thumbnail) if item.thumbnail else None,
            has_spoiler=spoiler, supports_streaming=True,
        )
    if item.delivery_kind == "audio":
        return types.InputMediaAudio(
            source, caption=caption, duration=item.duration,
            performer=item.artist, title=item.title,
        )
    return types.InputMediaDocument(source, caption=caption)


def batches(items: list[MediaItem]) -> list[list[MediaItem]]:
    result: list[list[MediaItem]] = []
    for item in items:
        category = "visual" if item.delivery_kind in {"photo", "video"} else item.delivery_kind
        if result:
            previous = result[-1]
            prev_kind = previous[0].delivery_kind
            prev_category = "visual" if prev_kind in {"photo", "video"} else prev_kind
            if category == prev_category and len(previous) < 10:
                previous.append(item)
                continue
        result.append([item])
    return result


class Sender:
    def __init__(self, client: Client, settings: Settings):
        self.client = client
        self.settings = settings

    async def send_preuploaded_video(
        self, chat_id: int, item: MediaItem, file: raw.types.InputFileBig,
        caption: str, reply_to: int | None, silent: bool, spoiler: bool,
    ) -> types.Message:
        if item.path is None or not item.path.is_file():
            raise MediaError("A streamed video is missing before delivery.")
        thumbnail = await self.client.save_file(str(item.thumbnail)) if item.thumbnail else None
        media = raw.types.InputMediaUploadedDocument(
            file=file, mime_type="video/mp4", thumb=thumbnail, spoiler=spoiler or None,
            attributes=[
                raw.types.DocumentAttributeVideo(
                    supports_streaming=True, duration=item.duration,
                    w=item.width, h=item.height,
                ),
                raw.types.DocumentAttributeFilename(file_name=item.path.name),
            ],
        )
        while True:
            try:
                result = await self.client.invoke(raw.functions.messages.SendMedia(
                    peer=await self.client.resolve_peer(chat_id), media=media,
                    silent=silent or None,
                    reply_to=utils.get_reply_head_fm(None, reply_to),
                    random_id=self.client.rnd_id(),
                    **await utils.parse_text_entities(
                        self.client, caption, enums.ParseMode.HTML, None,
                    ),
                ))
            except FilePartMissing as exc:
                await self.client.save_file(str(item.path), file_id=file.id, file_part=exc.value)
                continue
            for update in result.updates:
                if isinstance(update, (
                    raw.types.UpdateNewMessage, raw.types.UpdateNewChannelMessage,
                    raw.types.UpdateNewScheduledMessage,
                )):
                    sent = await types.Message._parse(
                        client=self.client, message=update.message,
                        users={user.id: user for user in result.users},
                        chats={chat.id: chat for chat in result.chats},
                        is_scheduled=isinstance(update, raw.types.UpdateNewScheduledMessage),
                    )
                    item.file_id = message_file_id(sent)
                    return sent
            raise MediaError("Telegram did not confirm the streamed video.")

    async def _single(
        self, chat_id: int, item: MediaItem, caption: str, reply_to: int | None,
        silent: bool, spoiler: bool, status: types.Message | None,
    ) -> types.Message:
        if item.size > self.settings.max_file_size:
            raise FileTooLarge("The file exceeds the 2 GB limit.")
        common = {
            "caption": caption, "parse_mode": enums.ParseMode.HTML,
            "disable_notification": silent, "reply_to_message_id": reply_to,
        }
        if item.file_id:
            sent = await self.client.send_cached_media(chat_id, item.file_id, **common)
        else:
            if item.path is None or not item.path.is_file():
                raise MediaError("A file is missing before upload.")
            if item.path.stat().st_size > self.settings.max_file_size:
                raise FileTooLarge("The file exceeds the 2 GB limit.")
            last_update = 0.0

            async def progress(current: int, total: int) -> None:
                nonlocal last_update
                now = time.monotonic()
                if status and (now - last_update > 5 or current == total):
                    last_update = now
                    try:
                        await status.edit_text(f"Uploading… {current * 100 // max(total, 1)}%")
                    except Exception:
                        pass

            source = str(item.path)
            kind = item.delivery_kind
            if kind == "photo":
                sent = await self.client.send_photo(
                    chat_id, source, has_spoiler=spoiler, progress=progress, **common,
                )
            elif kind == "video":
                sent = await self.client.send_video(
                    chat_id, source, duration=item.duration, width=item.width,
                    height=item.height, thumb=str(item.thumbnail) if item.thumbnail else None,
                    supports_streaming=True, has_spoiler=spoiler, progress=progress, **common,
                )
            elif kind == "audio":
                sent = await self.client.send_audio(
                    chat_id, source, duration=item.duration, performer=item.artist,
                    title=item.title, progress=progress, **common,
                )
            else:
                sent = await self.client.send_document(
                    chat_id, source, progress=progress, **common,
                )
        if sent is None:
            raise MediaError("Telegram stopped the upload.")
        item.file_id = message_file_id(sent)
        return sent

    async def send(
        self, chat_id: int, media: Media, caption: str, reply_to: int | None = None,
        silent: bool = False, spoiler: bool = False, status: types.Message | None = None,
    ) -> list[types.Message]:
        if not media.items:
            raise MediaError("No media was found.")
        sent: list[types.Message] = []
        caption_pending = caption
        for batch in batches(media.items):
            if len(batch) == 1:
                message = await self._single(
                    chat_id, batch[0], caption_pending, reply_to, silent, spoiler, status,
                )
                sent.append(message)
            else:
                for item in batch:
                    if item.size > self.settings.max_file_size:
                        raise FileTooLarge("The file exceeds the 2 GB limit.")
                    if item.path and item.path.stat().st_size > self.settings.max_file_size:
                        raise FileTooLarge("The file exceeds the 2 GB limit.")
                inputs = [
                    input_media(item, caption_pending if i == 0 else "", spoiler)
                    for i, item in enumerate(batch)
                ]
                messages = await self.client.send_media_group(
                    chat_id, inputs, reply_to_message_id=reply_to,
                    disable_notification=silent,
                )
                if len(messages) != len(batch):
                    raise MediaError("Telegram returned an incomplete album.")
                for item, message in zip(batch, messages):
                    item.file_id = message_file_id(message)
                sent.extend(messages)
            caption_pending = ""
        return sent
