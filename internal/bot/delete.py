"""Authorize group media deletion without retaining download ownership."""

import base64
import hashlib
import hmac

from hydrogram import Client, enums, types

from internal.bot.settings import is_group_admin


DELETE_PREFIX = "media:delete:"


def _signature(token: str, payload: str) -> str:
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _owner_signature(token: str, chat_id: int, message_id: int, user_id: int) -> str:
    return _signature(token, f"pyvd-delete-owner:v1:{chat_id}:{message_id}:{user_id}")


def _button_signature(token: str, chat_id: int, message_id: int, owner: str) -> str:
    return _signature(token, f"pyvd-delete-button:v1:{chat_id}:{message_id}:{owner}")


def delete_keyboard(
    token: str, chat_id: int, message_id: int, requester_id: int,
) -> types.InlineKeyboardMarkup:
    owner = _owner_signature(token, chat_id, message_id, requester_id)
    signature = _button_signature(token, chat_id, message_id, owner)
    return types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("Delete", callback_data=f"{DELETE_PREFIX}{owner}:{signature}"),
    ]])


async def add_delete_buttons(
    client: Client, token: str, messages: list[types.Message], requester_id: int,
) -> None:
    anchors = {}
    for message in messages:
        key = ("album", message.media_group_id) if message.media_group_id else ("single", message.id)
        anchors[key] = message
    for message in anchors.values():
        await client.edit_message_reply_markup(
            message.chat.id, message.id,
            reply_markup=delete_keyboard(token, message.chat.id, message.id, requester_id),
        )


async def handle_delete(
    client: Client, token: str, bot_id: int, query: types.CallbackQuery,
) -> bool:
    data = query.data or ""
    if not isinstance(data, str) or not data.startswith(DELETE_PREFIX):
        return False
    message = query.message
    if (
        query.inline_message_id or not message or not query.from_user or not message.chat
        or message.chat.type not in {enums.ChatType.GROUP, enums.ChatType.SUPERGROUP}
        or not message.from_user or message.from_user.id != bot_id
    ):
        await query.answer("This Delete button is unavailable.", show_alert=True)
        return True
    try:
        owner, signature = data[len(DELETE_PREFIX):].split(":")
        valid = (
            len(owner) == 22 and owner.isascii() and signature.isascii()
            and hmac.compare_digest(
                signature, _button_signature(token, message.chat.id, message.id, owner),
            )
        )
    except ValueError:
        valid = False
    if not valid:
        await query.answer("This Delete button is invalid.", show_alert=True)
        return True
    if not hmac.compare_digest(
        owner, _owner_signature(token, message.chat.id, message.id, query.from_user.id),
    ):
        try:
            admin = await is_group_admin(client, message.chat.id, query.from_user.id)
        except Exception:
            await query.answer(
                "Could not verify your group permissions. Please try again.", show_alert=True,
            )
            return True
        if not admin:
            await query.answer(
                "Only the link sender or group admins can delete this media.", show_alert=True,
            )
            return True
    try:
        messages = (
            await client.get_media_group(message.chat.id, message.id)
            if message.media_group_id else [message]
        )
        ids = [item.id for item in messages if (
            item.chat.id == message.chat.id and item.from_user and item.from_user.id == bot_id
            and item.media_group_id == message.media_group_id
        )]
        if message.id not in ids:
            raise ValueError("The media message is unavailable")
        await client.delete_messages(message.chat.id, ids, revoke=True)
    except Exception:
        await query.answer(
            "Could not delete this media. Check my group permissions and try again.", show_alert=True,
        )
        return True
    await query.answer("Deleted.")
    return True
