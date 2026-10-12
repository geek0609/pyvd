"""Authorize group media deletion without retaining download ownership."""

import asyncio
import base64
import hashlib
import hmac
import struct

from hydrogram import Client, enums, types

from internal.bot.settings import is_group_admin


DELETE_PREFIX = "media:delete:"
DELETE_ALL_PREFIX = "da:"
DELETE_PREFIXES = (DELETE_PREFIX, DELETE_ALL_PREFIX)
CONTROL_TEXT = "Media sent. Use Delete all to remove all media from this link."


def _digest(token: str, payload: str) -> bytes:
    return hmac.new(token.encode(), payload.encode(), hashlib.sha256).digest()[:16]


def _signature(token: str, payload: str) -> str:
    digest = _digest(token, payload)
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


def _all_owner(token: str, chat_id: int, scope: bytes, user_id: int) -> bytes:
    return _digest(token, f"pyvd-delete-all-owner:v1:{chat_id}:{scope.hex()}:{user_id}")


def _all_signature(token: str, chat_id: int, scope: bytes, owner: bytes) -> bytes:
    return _digest(token, f"pyvd-delete-all-button:v1:{chat_id}:{scope.hex()}:{owner.hex()}")


def _deletion_scope(ids: list[int], source_id: int) -> bytes:
    """Pack exact message runs, or a source and bounds, into 13 bytes."""
    runs = []
    for message_id in ids:
        if runs and message_id == runs[-1][0] + runs[-1][1]:
            start, count = runs[-1]
            runs[-1] = (start, count + 1)
        else:
            runs.append((message_id, 1))
    # Two runs cover ordinary singles and albums, even with a busy chat between albums.
    # More scattered replies use their source and bounds to filter message lookups.
    if len(runs) <= 2 and len(ids) <= 20:
        first, count = runs[0]
        second, count2 = runs[1] if len(runs) == 2 else (0, 0)
        return b"\x01" + struct.pack(">III", first, second, count | (count2 << 8))
    return b"\x00" + struct.pack(">III", source_id, ids[0], ids[-1])


async def send_delete_control(
    client: Client, token: str, messages: list[types.Message], requester_id: int,
    source_id: int, silent: bool = False,
) -> None:
    chat_id = messages[0].chat.id
    ids = sorted(message.id for message in messages)
    if source_id <= 0 or ids[0] <= 0 or any(message.chat.id != chat_id for message in messages):
        raise ValueError("The media messages do not belong to one link in one chat")
    # A 13-byte scope and two 16-byte signatures fit the 64-byte callback limit.
    # The button retains no URLs or user IDs, and requires no local history.
    scope = _deletion_scope(ids, source_id)
    owner = _all_owner(token, chat_id, scope, requester_id)
    payload = scope + owner + _all_signature(token, chat_id, scope, owner)
    data = DELETE_ALL_PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")
    markup = types.InlineKeyboardMarkup([[
        types.InlineKeyboardButton("Delete all", callback_data=data),
    ]])
    await client.send_message(
        chat_id, CONTROL_TEXT, parse_mode=enums.ParseMode.DISABLED,
        reply_to_message_id=ids[-1],
        message_thread_id=getattr(messages[-1], "message_thread_id", None),
        disable_notification=silent, reply_markup=markup,
    )


def _control_scope(
    token: str, message: types.Message, data: str,
) -> tuple[bytes, bytes, list[int] | tuple[int, int, int]]:
    encoded = data[len(DELETE_ALL_PREFIX):]
    payload = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    if len(payload) != 45 or base64.urlsafe_b64encode(payload).decode().rstrip("=") != encoded:
        raise ValueError("Invalid Delete all data")
    scope, owner, signature = payload[:13], payload[13:29], payload[29:]
    if not hmac.compare_digest(
        signature, _all_signature(token, message.chat.id, scope, owner),
    ):
        raise ValueError("Invalid Delete all signature")
    fields = struct.unpack(">III", scope[1:])
    if scope[0] == 1:
        first, second, counts = fields
        count, count2 = counts & 255, counts >> 8
        if (
            not first or not 1 <= count <= 20 or not 0 <= count2 <= 20 - count
            or bool(second) != bool(count2) or (second and second < first + count)
        ):
            raise ValueError("Invalid media message runs")
        targets = list(range(first, first + count)) + list(range(second, second + count2))
    elif scope[0] == 0:
        source, first, last = fields
        if not source or not first or first > last:
            raise ValueError("Invalid media message bounds")
        targets = fields
    else:
        raise ValueError("Unknown Delete all scope")
    # Telegram assigns the control's ID after its keyboard is sent. Its stored
    # keyboard prevents callback data from being reused on another bot message.
    markup = message.reply_markup
    if not isinstance(markup, types.InlineKeyboardMarkup) or not any(
        button.callback_data == data for row in markup.inline_keyboard for button in row
    ):
        raise ValueError("The Delete all button belongs to another message")
    return scope, owner, targets


async def _link_media_ids(
    client: Client, chat_id: int, bot_id: int, source_id: int, first: int, last: int,
) -> list[int]:
    messages = []
    for start in range(first, last + 1, 200):
        page = await client.get_messages(
            chat_id, list(range(start, min(start + 200, last + 1))), replies=0,
        )
        messages.extend(item for item in page if (
            item and item.chat and item.chat.id == chat_id and item.from_user
            and item.from_user.id == bot_id and item.media and first <= item.id <= last
        ))
    # The signed endpoints also identify albums whose reply anchor was removed.
    related = [item for item in messages if (
        item.reply_to_message_id == source_id or item.id in {first, last}
    )]
    albums = {item.media_group_id for item in related if item.media_group_id}
    return sorted({item.id for item in messages if (
        item.reply_to_message_id == source_id or item.id in {first, last}
        or item.media_group_id in albums
    )})


async def handle_delete(
    client: Client, token: str, bot_id: int, query: types.CallbackQuery,
) -> bool:
    data = query.data or ""
    if not isinstance(data, str) or not data.startswith(DELETE_PREFIXES):
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
        all_media = None
        if data.startswith(DELETE_ALL_PREFIX):
            scope, owner_bytes, all_media = _control_scope(token, message, data)
            owner = base64.urlsafe_b64encode(owner_bytes).decode().rstrip("=")
            expected_owner = base64.urlsafe_b64encode(
                _all_owner(token, message.chat.id, scope, query.from_user.id),
            ).decode().rstrip("=")
            valid = True
        else:
            owner, signature = data[len(DELETE_PREFIX):].split(":")
            valid = (
                len(owner) == 22 and owner.isascii() and signature.isascii()
                and hmac.compare_digest(
                    signature, _button_signature(token, message.chat.id, message.id, owner),
                )
            )
            expected_owner = _owner_signature(token, message.chat.id, message.id, query.from_user.id)
    except ValueError:
        valid = False
    if not valid:
        await query.answer("This Delete button is invalid.", show_alert=True)
        return True
    if not hmac.compare_digest(owner, expected_owner):
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
        if all_media:
            async with asyncio.timeout(20):
                if isinstance(all_media, list):
                    wanted = set(all_media)
                    media = await client.get_messages(message.chat.id, all_media, replies=0)
                    ids = sorted({item.id for item in media if (
                        item and item.chat and item.chat.id == message.chat.id and item.from_user
                        and item.from_user.id == bot_id and item.media and item.id in wanted
                    )})
                else:
                    ids = await _link_media_ids(client, message.chat.id, bot_id, *all_media)
            ids.append(message.id)
        else:
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
