from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hydrogram import enums

from internal.bot.delete import add_delete_buttons, delete_keyboard, handle_delete
from internal.bot.main import Bot


TOKEN = "test-delete-token"
BOT_ID = 42


def media_message(id=100, album=None, user=BOT_ID, chat_id=-100, kind=enums.ChatType.SUPERGROUP):
    return SimpleNamespace(
        id=id, media_group_id=album, from_user=SimpleNamespace(id=user),
        chat=SimpleNamespace(id=chat_id, type=kind),
    )


def callback(message=None, requester=7, clicker=7):
    message = message or media_message()
    button = delete_keyboard(TOKEN, message.chat.id, message.id, requester).inline_keyboard[0][0]
    return SimpleNamespace(
        data=button.callback_data, message=message, from_user=SimpleNamespace(id=clicker),
        inline_message_id=None, answer=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP])
@pytest.mark.parametrize(("clicker", "status", "permitted"), [
    (7, enums.ChatMemberStatus.MEMBER, True),
    (8, enums.ChatMemberStatus.ADMINISTRATOR, True),
    (8, enums.ChatMemberStatus.OWNER, True),
    (8, enums.ChatMemberStatus.MEMBER, False),
    (8, enums.ChatMemberStatus.LEFT, False),
])
async def test_only_link_sender_and_current_group_admins_can_delete(kind, clicker, status, permitted):
    client = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status)),
        delete_messages=AsyncMock(), get_media_group=AsyncMock(),
    )
    query = callback(media_message(kind=kind), clicker=clicker)
    assert await handle_delete(client, TOKEN, BOT_ID, query)
    if permitted:
        client.delete_messages.assert_awaited_once_with(-100, [100], revoke=True)
        query.answer.assert_awaited_once_with("Deleted.")
    else:
        client.delete_messages.assert_not_awaited()
        assert query.answer.call_args.kwargs["show_alert"]
    if clicker == 7:
        client.get_chat_member.assert_not_awaited()
    else:
        client.get_chat_member.assert_awaited_once_with(-100, clicker)
    client.get_media_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_buttons_cover_singles_and_each_album_and_fit_callback_limit():
    client = SimpleNamespace(edit_message_reply_markup=AsyncMock())
    messages = [
        media_message(100), media_message(101, "first"), media_message(105, "first"),
        media_message(110, "second"), media_message(111, "second"),
    ]
    await add_delete_buttons(client, TOKEN, messages, 2**63 - 1)
    assert [call.args for call in client.edit_message_reply_markup.call_args_list] == [
        (-100, 100), (-100, 105), (-100, 111),
    ]
    for call in client.edit_message_reply_markup.call_args_list:
        button = call.kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.text == "Delete"
        assert len(button.callback_data.encode()) <= 64
        assert str(2**63 - 1) not in button.callback_data


@pytest.mark.asyncio
async def test_album_deletion_uses_exact_bot_message_ids_without_deleting_source_or_neighbors():
    query = callback(media_message(105, "album"))
    client = SimpleNamespace(
        get_media_group=AsyncMock(return_value=[
            media_message(101, "album"), media_message(105, "album"),
            media_message(102, "album", user=7), media_message(103, "other"),
            media_message(104, "album", chat_id=-200),
        ]), delete_messages=AsyncMock(),
    )
    assert await handle_delete(client, TOKEN, BOT_ID, query)
    client.get_media_group.assert_awaited_once_with(-100, 105)
    client.delete_messages.assert_awaited_once_with(-100, [101, 105], revoke=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "owner", "chat", "message", "sender", "signature", "unicode", "malformed", "inline", "dm",
])
async def test_forged_or_reused_delete_buttons_cannot_delete(change):
    query = callback()
    if change == "owner":
        changed = callback(requester=8).data.split(":")[-2]
        parts = query.data.split(":")
        parts[-2] = changed
        query.data = ":".join(parts)
        query.from_user.id = 8
    elif change == "chat":
        query.message.chat.id = -200
    elif change == "message":
        query.message.id += 1
    elif change == "sender":
        query.message.from_user.id = 7
    elif change == "signature":
        query.data += "x"
    elif change == "unicode":
        query.data = "media:delete:7:é"
    elif change == "malformed":
        query.data = "media:delete:invalid"
    elif change == "inline":
        query.inline_message_id = "inline-message"
    else:
        query.message.chat.type = enums.ChatType.PRIVATE
    client = SimpleNamespace(
        get_chat_member=AsyncMock(), get_media_group=AsyncMock(), delete_messages=AsyncMock(),
    )
    assert await handle_delete(client, TOKEN, BOT_ID, query)
    client.get_chat_member.assert_not_awaited()
    client.get_media_group.assert_not_awaited()
    client.delete_messages.assert_not_awaited()
    assert query.answer.call_args.kwargs["show_alert"]


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_anonymous_link_buttons_are_admin_only(admin):
    client = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status=(
            enums.ChatMemberStatus.ADMINISTRATOR if admin else enums.ChatMemberStatus.MEMBER
        ))), delete_messages=AsyncMock(),
    )
    assert await handle_delete(client, TOKEN, BOT_ID, callback(requester=0))
    assert client.delete_messages.await_count == int(admin)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["permissions", "album", "delete"])
async def test_api_errors_fail_closed_and_are_reported_in_callback(failure):
    client = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status=enums.ChatMemberStatus.ADMINISTRATOR)),
        get_media_group=AsyncMock(return_value=[media_message(100, "album")]),
        delete_messages=AsyncMock(),
    )
    query = callback(media_message(100, "album"), clicker=8)
    method = {"permissions": "get_chat_member", "album": "get_media_group", "delete": "delete_messages"}[failure]
    getattr(client, method).side_effect = RuntimeError("Telegram request failed")
    assert await handle_delete(client, TOKEN, BOT_ID, query)
    if failure != "delete":
        client.delete_messages.assert_not_awaited()
    assert query.answer.call_args.kwargs["show_alert"]
    assert "Could not" in query.answer.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["message", "chat", "sender", "clicker"])
async def test_unavailable_callback_messages_are_reported_without_deleting(missing):
    query = callback()
    if missing == "message":
        query.message = None
    elif missing == "chat":
        query.message.chat = None
    elif missing == "sender":
        query.message.from_user = None
    else:
        query.from_user = None
    client = SimpleNamespace(delete_messages=AsyncMock())
    bot = Bot(client, SimpleNamespace(whitelist=frozenset(), bot_token=TOKEN), SimpleNamespace())
    bot.bot_id = BOT_ID
    await bot.on_callback(None, query)
    client.delete_messages.assert_not_awaited()
    query.answer.assert_awaited_once_with("This Delete button is unavailable.", show_alert=True)


@pytest.mark.asyncio
async def test_bot_routes_delete_callback_without_saving_user_history():
    client = SimpleNamespace(delete_messages=AsyncMock())
    bot = Bot(client, SimpleNamespace(whitelist=frozenset(), bot_token=TOKEN), SimpleNamespace())
    bot.bot_id = BOT_ID
    query = callback()
    await bot.on_callback(None, query)
    client.delete_messages.assert_awaited_once_with(-100, [100], revoke=True)
    query.answer.assert_awaited_once_with("Deleted.")
