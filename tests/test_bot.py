from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from hydrogram import enums

from internal.bot.main import Bot, allowed, bot_commands, chat_kind, help_text
from internal.bot.chat import is_public_group
from internal.core.errors import NoMedia
from internal.models.media import ChatSettings
from internal.networking.proxy import hydrogram_proxy


def test_whitelist_restricts_group_and_inline_users() -> None:
    settings = SimpleNamespace(whitelist=frozenset({123}))
    assert allowed(settings, 123, 999)
    assert not allowed(settings, -100, 123)
    assert allowed(settings, None, 123)


def test_chat_kind() -> None:
    assert chat_kind(SimpleNamespace(chat=SimpleNamespace(type=enums.ChatType.SUPERGROUP))) == "group"
    assert chat_kind(SimpleNamespace(chat=SimpleNamespace(type=enums.ChatType.PRIVATE))) == "private"


def test_public_group_requires_a_supergroup_username() -> None:
    assert is_public_group(SimpleNamespace(type=enums.ChatType.SUPERGROUP, username="publicgroup"))
    assert not is_public_group(SimpleNamespace(type=enums.ChatType.SUPERGROUP, username=None))
    assert not is_public_group(SimpleNamespace(type=enums.ChatType.GROUP, username=None))


def test_hydrogram_proxy_url() -> None:
    assert hydrogram_proxy("socks5://user:pass@localhost:1080") == {
        "scheme": "socks5", "hostname": "localhost", "port": 1080,
        "username": "user", "password": "pass",
    }


def test_help_and_command_menus_describe_music_and_group_settings() -> None:
    assert "/music" in help_text("private")
    assert "DM or group" in help_text("private")
    assert "/settings" not in help_text("private")
    assert "/settings" in help_text("group")
    assert "/download" not in help_text("group")
    assert "DM or private group" in help_text("group", public_group=True)
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
@pytest.mark.parametrize(
    ("text", "marked", "accepted"),
    [
        ("https://youtu.be/YE7VzlLtp-4", False, True),
        ("https://youtu.be/YE7VzlLtp-4 #nsfw", True, True),
        ("https://youtu.be/YE7VzlLtp-4 #skip", False, False),
        ("/download https://youtu.be/YE7VzlLtp-4", False, False),
        ("/unknown https://youtu.be/YE7VzlLtp-4", False, False),
        ("Just a conversation", False, False),
    ],
)
async def test_public_group_handles_plain_links_without_commands(
    text: str, marked: bool, accepted: bool,
) -> None:
    calls = []

    class Status:
        async def delete(self):
            pass

    class Message:
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=-100, type=enums.ChatType.SUPERGROUP, username="publicgroup")
        from_user = SimpleNamespace(id=7)
        id = 10

        def __init__(self):
            self.text = text

        async def reply(self, text, **kwargs):
            return Status()

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

    class Runner:
        async def run(self, request, chat, target_chat_id, **kwargs):
            calls.append((
                request.extractor_id, kwargs["public_group"],
                kwargs["marked_nsfw"], kwargs["spoiler"],
            ))

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset(), admins=frozenset()), Store())
    bot.runner = Runner()
    await bot.on_message(None, Message())
    assert calls == ([("youtube", True, marked, False)] if accepted else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_type", "has_user", "kind"),
    [
        (enums.ChatType.PRIVATE, True, "private"),
        (enums.ChatType.SUPERGROUP, True, "group"),
        (enums.ChatType.SUPERGROUP, False, "group"),
    ],
)
async def test_plain_links_download_in_dm_and_private_group(
    chat_type: enums.ChatType, has_user: bool, kind: str,
) -> None:
    calls = []

    class Status:
        async def delete(self):
            pass

    class Message:
        text = "https://vimeo.com/123456"
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=123, type=chat_type, username=None)
        from_user = SimpleNamespace(id=7) if has_user else None
        id = 10

        async def reply(self, text, **kwargs):
            return Status()

    class Store:
        async def chat(self, chat_id, requested_kind):
            assert requested_kind == kind
            return ChatSettings(chat_id, requested_kind, True, False, False, 10, False)

    class Runner:
        async def run(self, request, chat, target_chat_id, **kwargs):
            calls.append((request.extractor_id, chat.kind, kwargs["public_group"]))

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), Store())
    bot.runner = Runner()
    await bot.on_message(None, Message())
    assert calls == [("vimeo", kind, False)]


@pytest.mark.asyncio
async def test_non_media_links_leave_no_bot_reply() -> None:
    replies = []

    class Status:
        async def delete(self):
            replies.append("deleted")

        async def edit_text(self, text, **kwargs):
            replies.append(text)

    class Message:
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=-100, type=enums.ChatType.SUPERGROUP, username="publicgroup")
        from_user = SimpleNamespace(id=7)
        id = 10

        def __init__(self, text):
            self.text = text

        async def reply(self, text, **kwargs):
            replies.append(text)
            return Status()

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

    class Runner:
        async def run(self, *args, **kwargs):
            raise NoMedia("No media was found at this link.")

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), Store())
    bot.runner = Runner()
    await bot.on_message(None, Message("https://x.com/username"))
    assert replies == []
    await bot.on_message(None, Message("https://www.reddit.com/gallery/abc123"))
    assert replies == ["Queued…", "deleted"]


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
