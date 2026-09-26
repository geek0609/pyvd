from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from hydrogram import enums

from internal.bot.main import Bot, allowed, bot_commands, chat_kind, help_text
from internal.networking.proxy import hydrogram_proxy


def test_whitelist_restricts_group_and_inline_users() -> None:
    settings = SimpleNamespace(whitelist=frozenset({123}))
    assert allowed(settings, 123, 999)
    assert not allowed(settings, -100, 123)
    assert allowed(settings, None, 123)


def test_chat_kind() -> None:
    assert chat_kind(SimpleNamespace(chat=SimpleNamespace(type=enums.ChatType.SUPERGROUP))) == "group"
    assert chat_kind(SimpleNamespace(chat=SimpleNamespace(type=enums.ChatType.PRIVATE))) == "private"


def test_hydrogram_proxy_url() -> None:
    assert hydrogram_proxy("socks5://user:pass@localhost:1080") == {
        "scheme": "socks5", "hostname": "localhost", "port": 1080,
        "username": "user", "password": "pass",
    }


def test_help_and_command_menus_describe_music_and_group_settings() -> None:
    assert "/music" in help_text("private", False)
    assert "/settings" not in help_text("private", False)
    assert "/settings" in help_text("group", False)
    assert "/stats" in help_text("private", True)
    assert [command.command for command in bot_commands(False)] == [
        "start", "help", "extractors", "music",
    ]
    assert [command.command for command in bot_commands(True)][-1] == "settings"


@pytest.mark.asyncio
async def test_help_command_replies_with_current_usage() -> None:
    replies = []

    class Message:
        text = "/help"
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=123, type=enums.ChatType.PRIVATE)
        from_user = SimpleNamespace(id=7)

        async def reply(self, text, **kwargs):
            replies.append((text, kwargs))

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset(), admins=frozenset()), SimpleNamespace())
    await bot.on_message(None, Message())
    assert len(replies) == 1
    assert "/music" in replies[0][0]
    assert replies[0][1]["parse_mode"] == enums.ParseMode.DISABLED


@pytest.mark.asyncio
async def test_start_registers_private_and_group_commands() -> None:
    menus = []

    class Client:
        async def get_me(self):
            return SimpleNamespace(id=42, username="pyvd")

        async def set_bot_commands(self, commands, **kwargs):
            menus.append((commands, kwargs))

    bot = Bot(Client(), SimpleNamespace(), SimpleNamespace())
    await bot.start()
    assert [command.command for command in menus[0][0]] == [
        "start", "help", "extractors", "music",
    ]
    assert menus[0][1] == {}
    assert [command.command for command in menus[1][0]][-1] == "settings"
    assert menus[1][1]["scope"].type == "all_group_chats"
