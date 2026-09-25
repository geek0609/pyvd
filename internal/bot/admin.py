"""Admin statistics and stored error lookup."""

from datetime import datetime, timedelta, timezone

from hydrogram import enums, types

from internal.database.store import Store


PERIODS = {"1d": 1, "7d": 7, "30d": 30}


def stats_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton(label, callback_data=f"stats:{label}")
        for label in (*PERIODS, "all")
    ]])


async def stats_text(store: Store, period: str) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=PERIODS.get(period, 36500))
    stats = await store.stats(since)
    return (
        f"Stats — {period if period in PERIODS else 'all time'}\n\n"
        f"Private chats: {stats['private_chats']}\n"
        f"Groups: {stats['group_chats']}\n"
        f"Downloads: {stats['downloads']}\n"
        f"Total size: {stats['bytes'] / 1_000_000_000:.2f} GB"
    )


async def show_stats(store: Store, message: types.Message) -> None:
    await message.reply(
        await stats_text(store, "all"), reply_markup=stats_keyboard(),
        parse_mode=enums.ParseMode.DISABLED,
    )


async def show_error(store: Store, message: types.Message, error_id: str) -> None:
    if len(error_id) != 8 or any(char not in "0123456789abcdef" for char in error_id.lower()):
        await message.reply("Usage: /derr <8-character error ID>")
        return
    detail = await store.error(error_id.lower())
    await message.reply(detail or "No error found for that ID.", parse_mode=enums.ParseMode.DISABLED)
