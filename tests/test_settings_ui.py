from types import SimpleNamespace

import pytest

from internal.bot.admin import stats_text
from internal.bot.settings import keyboard, sites_keyboard
from internal.models.media import ChatSettings


def test_settings_keyboard_uses_saved_values() -> None:
    chat = ChatSettings(-100, "group", True, False, False, 10, True, ("youtube",))
    rows = keyboard(chat).inline_keyboard
    assert rows[0][0].text == "✅ Captions"
    assert rows[3][0].text == "✅ Delete source links"
    assert rows[4][0].text == "Album limit: 10"
    site_rows = sites_keyboard(chat, SimpleNamespace(site=lambda _: SimpleNamespace(disabled=False))).inline_keyboard
    assert any("❌ YouTube" == button.text for row in site_rows for button in row)


@pytest.mark.asyncio
async def test_admin_stats_uses_govd_counts() -> None:
    class Store:
        async def stats(self, since):
            return {"private_chats": 2, "group_chats": 3, "downloads": 4, "bytes": 2_000_000_000}

    text = await stats_text(Store(), "7d")
    assert "Groups: 3" in text and "Total size: 2.00 GB" in text
