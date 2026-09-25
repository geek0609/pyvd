"""Hydrogram bot entry point and message routing."""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from hydrogram import Client, enums, filters, idle, types

from internal.config.settings import Settings, load_settings
from internal.bot.admin import PERIODS, show_error, show_stats, stats_keyboard, stats_text
from internal.bot.settings import handle_callback, show_settings
from internal.core.errors import MediaError
from internal.core.tasks import JobRunner
from internal.database.store import Store
from internal.extractors.sites import SITE_NAMES, first_supported_url
from internal.logger.main import configure_logging
from internal.networking.proxy import hydrogram_proxy


LOG = logging.getLogger(__name__)
TAG_RE = re.compile(r"(?<!\w)#(skip|spoiler|nsfw)\b", re.IGNORECASE)


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


class Bot:
    def __init__(self, client: Client, settings: Settings, store: Store):
        self.client = client
        self.settings = settings
        self.store = store
        self.runner: JobRunner | None = None
        self.username = ""

    async def start(self) -> None:
        me = await self.client.get_me()
        self.username = me.username or "pyvd"
        self.runner = JobRunner(self.client, self.settings, self.store, self.username)
        LOG.info("started bot @%s", self.username)

    async def on_message(self, _: Client, message: types.Message) -> None:
        if not message.text or not message.from_user:
            return
        kind = chat_kind(message)
        if kind is None or not allowed(self.settings, message.chat.id, message.from_user.id):
            return
        sent_at = message.date
        if sent_at and sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        if sent_at and datetime.now(timezone.utc) - sent_at > timedelta(minutes=2):
            return
        text = message.text.strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        if command == "/start":
            await message.reply("Send me a media link and I’ll download it for you. Use /settings in a group to change its options.")
            return
        if command == "/extractors":
            await message.reply("Supported sites: " + ", ".join(sorted(SITE_NAMES.values())))
            return
        if command == "/settings":
            await show_settings(self.client, self.store, message)
            return
        if command == "/stats":
            if message.from_user.id in self.settings.admins:
                await show_stats(self.store, message)
            return
        if command == "/derr":
            if message.from_user.id in self.settings.admins:
                await show_error(self.store, message, text.partition(" ")[2].strip())
            return
        if command:
            return
        if "skip" in {tag.lower() for tag in TAG_RE.findall(text)}:
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
                spoiler=bool({"spoiler", "nsfw"} & {tag.lower() for tag in TAG_RE.findall(text)}),
                status=status,
            )
            await status.delete()
            if kind == "group" and chat.delete_links:
                try:
                    await message.delete()
                except Exception:
                    LOG.debug("could not delete source link", exc_info=True)
        except MediaError as exc:
            await status.edit_text(f"⚠️ {exc}", parse_mode=enums.ParseMode.DISABLED)
        except Exception as exc:
            LOG.exception("unexpected failure for %s", request.key)
            try:
                error_id = await self.store.log_error(exc)
                await status.edit_text(
                    f"⚠️ Download failed. Error ID: {error_id}",
                    parse_mode=enums.ParseMode.DISABLED,
                )
            except Exception:
                LOG.exception("could not report failure")

    async def on_callback(self, _: Client, query: types.CallbackQuery) -> None:
        if not allowed(
            self.settings, query.message.chat.id if query.message else None,
            query.from_user.id if query.from_user else None,
        ):
            await query.answer()
            return
        if await handle_callback(self.client, self.store, self.settings, query):
            return
        data = query.data or ""
        if data.startswith("stats:") and query.from_user.id in self.settings.admins:
            period = data.partition(":")[2]
            if period in {*PERIODS, "all"}:
                await query.answer()
                await query.edit_message_text(
                    await stats_text(self.store, period),
                    reply_markup=stats_keyboard(), parse_mode=enums.ParseMode.DISABLED,
                )
                return
        await query.answer()


async def run() -> None:
    settings = load_settings()
    configure_logging(settings)
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
    try:
        async with app:
            await bot.start()
            await idle()
    finally:
        await store.close()
