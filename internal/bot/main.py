"""Hydrogram bot entry point and message routing."""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from hydrogram import Client, enums, filters, idle, types

from internal.config.settings import Settings, load_settings
from internal.bot.chat import is_public_group
from internal.bot.settings import handle_callback, show_settings
from internal.bot.inline import Inline
from internal.core.errors import MediaError
from internal.core.tasks import JobRunner
from internal.database.store import Store
from internal.extractors.sites import PUBLIC_GROUP_SITE_NAMES, SITE_NAMES, first_supported_url, search_extractors
from internal.networking.proxy import hydrogram_proxy


TAG_RE = re.compile(r"(?<!\w)#(skip|spoiler|nsfw)\b", re.IGNORECASE)


def help_text(kind: str, public_group: bool = False) -> str:
    lines = [
        "Send a supported media link in a DM or group to download it (up to 2 GB). "
        "Use /extractors <name> to search supported sites.",
        "Reply to a video I sent with /music to receive its audio. "
        "Videos without an audio track cannot be converted.",
        "Use #skip to ignore a link, #spoiler to hide media, or #nsfw to mark it.",
    ]
    if kind == "group":
        lines.append(
            "Group admins can use /settings to change captions, silent delivery, "
            "album limits, link deletion, and whether to allow marked media. "
            "Allowed marked media is hidden by a spoiler in public groups."
        )
    if public_group:
        lines.append(
            "Public groups accept direct links from the listed site domains. "
            "For other links, use PyVD in a DM or private group."
        )
    return "\n\n".join(lines)


def bot_commands(group: bool) -> list[types.BotCommand]:
    commands = [
        types.BotCommand("start", "Introduction to PyVD"),
        types.BotCommand("help", "How to use PyVD"),
        types.BotCommand("extractors", "Search supported sites"),
        types.BotCommand("music", "Extract audio from a PyVD video"),
    ]
    if group:
        commands.append(types.BotCommand("settings", "Configure this group"))
    return commands


def allowed(settings: Settings, chat_id: int | None, user_id: int | None) -> bool:
    if not settings.whitelist:
        return True
    return (chat_id is not None and chat_id in settings.whitelist) or (
        chat_id is None and user_id is not None and user_id in settings.whitelist
    )


def chat_kind(message: types.Message) -> str | None:
    if message.chat.type in {enums.ChatType.PRIVATE, enums.ChatType.BOT}:
        return "private"
    if message.chat.type in {enums.ChatType.GROUP, enums.ChatType.SUPERGROUP}:
        return "group"
    return None


def replied_video(message: types.Message | None, bot_id: int) -> types.Video | types.Document:
    if message is None or not message.from_user or message.from_user.id != bot_id:
        raise MediaError("Reply to a video sent by PyVD with /music.")
    if message.video:
        return message.video
    if message.document and (message.document.mime_type or "").startswith("video/"):
        return message.document
    raise MediaError("That PyVD message does not contain a video.")


class Bot:
    def __init__(self, client: Client, settings: Settings, store: Store):
        self.client = client
        self.settings = settings
        self.store = store
        self.runner: JobRunner | None = None
        self.inline = Inline(client, settings, store)
        self.username = ""
        self.bot_id = 0

    async def start(self) -> None:
        me = await self.client.get_me()
        self.bot_id = me.id
        self.username = me.username or "pyvd"
        self.runner = JobRunner(self.client, self.settings, self.store, self.username)
        self.inline.runner = self.runner
        self.inline.username = self.username
        try:
            await self.client.set_bot_commands(bot_commands(group=False))
            await self.client.set_bot_commands(
                bot_commands(group=True), scope=types.BotCommandScopeAllGroupChats(),
            )
        except Exception:
            pass

    async def on_chat_member_updated(self, _: Client, update: types.ChatMemberUpdated) -> None:
        old = update.old_chat_member
        new = update.new_chat_member
        if not new or new.user.id != self.bot_id:
            return
        if old and old.status not in {enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED}:
            return
        if new.status not in {enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR}:
            return
        if update.chat.type not in {enums.ChatType.GROUP, enums.ChatType.SUPERGROUP}:
            return
        if not allowed(self.settings, update.chat.id, None):
            return
        await self.store.chat(update.chat.id, "group")
        await self.client.send_message(
            update.chat.id,
            "Thanks for adding PyVD! Send a link, use /help "
            "for commands, or use /settings to configure this group.",
        )

    async def on_message(self, _: Client, message: types.Message) -> None:
        if not message.text:
            return
        kind = chat_kind(message)
        if kind is None or (kind == "private" and not message.from_user):
            return
        user_id = message.from_user.id if message.from_user else None
        if not allowed(self.settings, message.chat.id, user_id):
            return
        public_group = is_public_group(message.chat)
        sent_at = message.date
        if sent_at and sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        if sent_at and datetime.now(timezone.utc) - sent_at > timedelta(minutes=2):
            return
        text = message.text.strip()
        token = text.split(maxsplit=1)[0] if text.startswith("/") else ""
        name, _, mention = token.partition("@")
        command = name.lower() if not mention or mention.lower() == self.username.lower() else ""
        if token and not command:
            return
        if command and not message.from_user:
            return
        if command == "/start":
            await message.reply(
                "I’m PyVD. Send me a media link and I’ll download it. "
                "Reply to one of my videos with /music to extract its audio. "
                "Use /help for all commands."
            )
            return
        if command == "/help":
            await message.reply(
                help_text(kind, public_group),
                parse_mode=enums.ParseMode.DISABLED,
            )
            return
        if command == "/extractors":
            term = text.partition(" ")[2].strip()
            if public_group:
                names = [
                    name for site_id, name in sorted(
                        PUBLIC_GROUP_SITE_NAMES.items(), key=lambda item: item[1],
                    )
                    if term.casefold() in site_id.casefold() or term.casefold() in name.casefold()
                ]
                await message.reply(
                    ("Supported here: " + ", ".join(names) if names else "No matching site here.")
                    + "\nOther domains are outside the public group allowlist. "
                    "Use PyVD in a DM or private group.",
                    parse_mode=enums.ParseMode.DISABLED,
                )
                return
            if term:
                matches, total = search_extractors(term)
                lines = [f"{name} ({site_id})" for site_id, name in matches]
                result = "\n".join(lines) if lines else "No matching extractor."
                if total > len(matches):
                    result += f"\nShowing {len(matches)} of {total} matches."
                await message.reply(result, parse_mode=enums.ParseMode.DISABLED)
            else:
                await message.reply(
                    "PyVD supports " + ", ".join(sorted(SITE_NAMES.values()))
                    + ", plus yt-dlp's named site extractors. "
                    "Use /extractors <name> to search them. "
                    "Full list: https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md",
                    parse_mode=enums.ParseMode.DISABLED,
                )
            return
        if command == "/settings":
            await show_settings(self.client, self.store, message)
            return
        if command == "/music":
            status = await message.reply("Queued…", parse_mode=enums.ParseMode.DISABLED)
            try:
                replied = message.reply_to_message
                if replied is None and message.reply_to_message_id:
                    replied = await self.client.get_messages(
                        message.chat.id, message.reply_to_message_id,
                    )
                video = replied_video(replied, self.bot_id)
                if public_group and getattr(replied, "has_media_spoiler", False):
                    raise MediaError(
                        "Audio from spoilered videos is unavailable here. "
                        "Use a DM or private group."
                    )
                if self.runner is None:
                    raise RuntimeError("bot is not started")
                chat = await self.store.chat(message.chat.id, kind)
                await self.runner.run_music(video, chat, message.chat.id, message.id, status)
                await status.delete()
            except MediaError as exc:
                await status.edit_text(f"⚠️ {exc}", parse_mode=enums.ParseMode.DISABLED)
            except Exception:
                try:
                    await status.edit_text(
                        "⚠️ Audio extraction failed. Please try again later.",
                        parse_mode=enums.ParseMode.DISABLED,
                    )
                except Exception:
                    pass
            return
        if command:
            return
        tags = {tag.lower() for tag in TAG_RE.findall(text)}
        if "skip" in tags:
            return
        request = first_supported_url(text)
        if request is None:
            return
        chat = await self.store.chat(message.chat.id, kind)
        if self.runner is None:
            raise RuntimeError("bot is not started")
        status = await message.reply("Queued…", parse_mode=enums.ParseMode.DISABLED)
        try:
            await self.runner.run(
                request, chat, message.chat.id, reply_to=message.id,
                spoiler="spoiler" in tags, marked_nsfw="nsfw" in tags,
                status=status, public_group=public_group,
            )
            await status.delete()
            if kind == "group" and chat.delete_links:
                try:
                    await message.delete()
                except Exception:
                    pass
        except MediaError as exc:
            await status.edit_text(f"⚠️ {exc}", parse_mode=enums.ParseMode.DISABLED)
        except Exception:
            try:
                await status.edit_text(
                    "⚠️ Download failed. Please try again later.",
                    parse_mode=enums.ParseMode.DISABLED,
                )
            except Exception:
                pass

    async def on_callback(self, _: Client, query: types.CallbackQuery) -> None:
        if not allowed(
            self.settings, query.message.chat.id if query.message else None,
            query.from_user.id if query.from_user else None,
        ):
            await query.answer()
            return
        if await handle_callback(self.client, self.store, query):
            return
        data = query.data or ""
        if data == "inline:loading":
            await query.answer("Still processing this media.", show_alert=True)
            return
        await query.answer()


async def run() -> None:
    settings = load_settings()
    logging.disable(logging.CRITICAL)
    store = Store(settings)
    await store.open()
    app = Client(
        "pyvd", api_id=settings.api_id, api_hash=settings.api_hash,
        bot_token=settings.bot_token, in_memory=True,
        proxy=hydrogram_proxy(settings.proxy),
        workers=settings.concurrent_updates, max_concurrent_transmissions=3,
    )
    bot = Bot(app, settings, store)
    app.on_message(filters.text & ~filters.bot)(bot.on_message)
    app.on_callback_query()(bot.on_callback)
    app.on_inline_query()(bot.inline.query)
    app.on_chosen_inline_result()(bot.inline.chosen)
    app.on_chat_member_updated()(bot.on_chat_member_updated)
    try:
        async with app:
            await bot.start()
            await idle()
    finally:
        await store.close()
