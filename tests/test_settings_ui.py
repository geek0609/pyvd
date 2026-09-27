from types import SimpleNamespace

import pytest
from hydrogram import enums

from internal.bot.settings import handle_callback, keyboard
from internal.models.media import ChatSettings


def test_settings_keyboard_uses_saved_values() -> None:
    chat = ChatSettings(-100, "group", True, False, False, 10, True)
    rows = keyboard(chat).inline_keyboard
    assert rows[0][0].text == "✅ Captions"
    assert rows[3][0].text == "✅ Delete source links"
    assert rows[4][0].text == "Album limit: 10"
    assert not any("site" in button.callback_data for row in rows for button in row)
    assert keyboard(chat).inline_keyboard[2][0].text == "❌ Allow age-restricted media"


@pytest.mark.asyncio
async def test_old_site_setting_callback_cannot_change_sites() -> None:
    class Client:
        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status=enums.ChatMemberStatus.ADMINISTRATOR)

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

        async def set_setting(self, *args):
            raise AssertionError("old site callback must not change settings")

    answers = []

    class Query:
        data = "s:site:youtube"
        from_user = SimpleNamespace(id=7)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-100, type=enums.ChatType.SUPERGROUP, username=None),
        )

        async def answer(self, text=None, **kwargs):
            answers.append(text)

    assert await handle_callback(Client(), Store(), Query())
    assert answers == ["Unknown setting."]
