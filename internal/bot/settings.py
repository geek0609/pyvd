"""English-only group settings backed by govd's settings table."""

from hydrogram import Client, enums, types

from internal.config.settings import Settings
from internal.database.store import Store
from internal.extractors.sites import OTHER_SITE_ID, OTHER_SITE_NAME, SITE_NAMES
from internal.models.media import ChatSettings


TOGGLES = {
    "captions": "Captions",
    "silent": "Silent mode",
    "nsfw": "Allow marked 18+ media",
    "delete_links": "Delete source links",
}
LIMITS = (1, 5, 10, 15, 20)


def keyboard(chat: ChatSettings) -> types.InlineKeyboardMarkup:
    rows = [
        [types.InlineKeyboardButton(
            f"{'✅' if getattr(chat, key) else '❌'} {label}",
            callback_data=f"s:toggle:{key}",
        )]
        for key, label in TOGGLES.items()
    ]
    rows.append([types.InlineKeyboardButton(
        f"Album limit: {chat.media_album_limit}", callback_data="s:limits",
    )])
    rows.append([types.InlineKeyboardButton("Supported sites", callback_data="s:sites")])
    rows.append([types.InlineKeyboardButton("Close", callback_data="s:close")])
    return types.InlineKeyboardMarkup(rows)


def limit_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup([
        [types.InlineKeyboardButton(str(value), callback_data=f"s:limit:{value}") for value in LIMITS],
        [types.InlineKeyboardButton("Back", callback_data="s:home")],
    ])


def sites_keyboard(chat: ChatSettings, settings: Settings) -> types.InlineKeyboardMarkup:
    names = {**SITE_NAMES, OTHER_SITE_ID: OTHER_SITE_NAME}
    buttons = [
        types.InlineKeyboardButton(
            f"{'❌' if site_id in chat.disabled_extractors else '✅'} {name}",
            callback_data=f"s:site:{site_id}",
        )
        for site_id, name in sorted(names.items(), key=lambda pair: pair[1])
        if not settings.site(site_id).disabled
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.append([types.InlineKeyboardButton("Back", callback_data="s:home")])
    return types.InlineKeyboardMarkup(rows)


async def is_group_admin(client: Client, chat_id: int, user_id: int) -> bool:
    member = await client.get_chat_member(chat_id, user_id)
    return member.status in {enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR}


async def show_settings(client: Client, store: Store, message: types.Message) -> None:
    if message.chat.type not in {enums.ChatType.GROUP, enums.ChatType.SUPERGROUP}:
        await message.reply("Settings are available in groups.")
        return
    if not await is_group_admin(client, message.chat.id, message.from_user.id):
        await message.reply("Only group administrators can change settings.")
        return
    chat = await store.chat(message.chat.id, "group")
    await message.reply("Group settings", reply_markup=keyboard(chat))


async def handle_callback(
    client: Client, store: Store, settings: Settings, query: types.CallbackQuery,
) -> bool:
    data = query.data or ""
    if not data.startswith("s:"):
        return False
    if query.message is None or query.message.chat.type not in {
        enums.ChatType.GROUP, enums.ChatType.SUPERGROUP,
    }:
        await query.answer("Group settings only.", show_alert=True)
        return True
    chat_id = query.message.chat.id
    if not await is_group_admin(client, chat_id, query.from_user.id):
        await query.answer("Only group administrators can change settings.", show_alert=True)
        return True
    chat = await store.chat(chat_id, "group")
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    if action == "toggle" and value in TOGGLES:
        await store.set_setting(chat_id, value, not getattr(chat, value))
        chat = await store.chat(chat_id, "group")
        action = "home"
    elif action == "limit" and value.isdecimal() and int(value) in LIMITS:
        await store.set_setting(chat_id, "media_album_limit", int(value))
        chat = await store.chat(chat_id, "group")
        action = "home"
    elif action == "site" and value in {*SITE_NAMES, OTHER_SITE_ID}:
        await store.set_extractor_enabled(chat_id, value, value in chat.disabled_extractors)
        chat = await store.chat(chat_id, "group")
        action = "sites"
    if action == "close":
        await query.answer()
        await query.message.delete()
        return True
    if action == "home":
        text, markup = "Group settings", keyboard(chat)
    elif action == "limits":
        text, markup = "Choose the maximum number of items in a post:", limit_keyboard()
    elif action == "sites":
        text, markup = "Tap a site to enable or disable it:", sites_keyboard(chat, settings)
    else:
        await query.answer("Unknown setting.", show_alert=True)
        return True
    await query.answer()
    await query.edit_message_text(text, reply_markup=markup, parse_mode=enums.ParseMode.DISABLED)
    return True
