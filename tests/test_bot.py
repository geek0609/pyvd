import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from hydrogram import enums

from internal.core.queue import JobRegistry
from internal.bot.main import Bot, allowed, bot_commands, chat_kind, help_text
from internal.bot.chat import is_public_group
from internal.core.errors import AuthenticationRequired, MediaError, NoAttachments, NoMedia
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
        "start", "help", "extractors", "music", "cancel",
    ]
    assert [command.command for command in bot_commands(True)][-1] == "settings"


@pytest.mark.asyncio
async def test_cancel_remains_responsive_and_only_requester_can_cancel():
    replies = []
    started = asyncio.Event()
    cleaned = asyncio.Event()

    class Runner:
        jobs = JobRegistry()

        async def run(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

    class Message:
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=123, type=enums.ChatType.PRIVATE)

        def __init__(self, text, id, user, reply=None):
            self.text, self.id = text, id
            self.from_user = SimpleNamespace(id=user)
            self.reply_to_message_id = reply

        async def reply(self, text, **kwargs):
            replies.append(text)

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), Store())
    bot.runner = Runner()
    await asyncio.wait_for(bot.on_message(None, Message("https://youtu.be/YE7VzlLtp-4", 1, 7)), 1)
    await started.wait()
    await bot.on_message(None, Message("/cancel", 2, 8, 1))
    assert not cleaned.is_set()
    await bot.on_message(None, Message("/cancel", 3, 7, 1))
    await asyncio.wait_for(cleaned.wait(), 1)
    await bot.close()
    assert replies == ["No active download of yours was found for that message.", "Cancelling your download."]
    assert bot.runner.jobs.pending == 0
    assert not bot.job_tasks


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
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
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

    class Message:
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=-100, type=enums.ChatType.SUPERGROUP, username="publicgroup")
        from_user = SimpleNamespace(id=7)
        id = 10

        def __init__(self):
            self.text = text

        async def reply(self, text, **kwargs):
            raise AssertionError("plain links must not send a status message")

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

    class Runner:
        jobs = JobRegistry()
        async def run(self, request, chat, target_chat_id, **kwargs):
            assert "status" not in kwargs
            calls.append((
                request.extractor_id, kwargs["public_group"],
                kwargs["marked_nsfw"], kwargs["spoiler"],
            ))

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset(), admins=frozenset()), Store())
    bot.runner = Runner()
    await bot.on_message(None, Message())
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
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

    class Message:
        text = "https://vimeo.com/123456"
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=123, type=chat_type, username=None)
        from_user = SimpleNamespace(id=7) if has_user else None
        id = 10

        async def reply(self, text, **kwargs):
            raise AssertionError("plain links must not send a status message")

    class Store:
        async def chat(self, chat_id, requested_kind):
            assert requested_kind == kind
            return ChatSettings(chat_id, requested_kind, True, False, False, 10, False)

    class Runner:
        jobs = JobRegistry()
        async def run(self, request, chat, target_chat_id, **kwargs):
            calls.append((request.extractor_id, chat.kind, kwargs["public_group"]))

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), Store())
    bot.runner = Runner()
    await bot.on_message(None, Message())
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
    assert calls == [("vimeo", kind, False)]


@pytest.mark.asyncio
async def test_ignored_links_stay_silent_but_extractor_errors_reply() -> None:
    replies = []

    class Message:
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=-100, type=enums.ChatType.SUPERGROUP, username="publicgroup")
        from_user = SimpleNamespace(id=7)
        id = 10

        def __init__(self, text):
            self.text = text

        async def reply(self, text, **kwargs):
            replies.append(text)

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

    class Runner:
        jobs = JobRegistry()
        async def run(self, *args, **kwargs):
            raise NoMedia("No media was found at this link.")

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), Store())
    bot.runner = Runner()
    await bot.on_message(None, Message("https://x.com/username"))
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
    assert replies == []
    await bot.on_message(None, Message("https://t.me/example/123"))
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
    assert replies == []
    await bot.on_message(None, Message("https://www.reddit.com/gallery/abc123"))
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
    assert replies == ["⚠️ No media was found at this link."]


@pytest.mark.asyncio
async def test_download_error_sends_one_reply_without_temporary_status() -> None:
    replies = []

    class Message:
        text = "https://youtu.be/YE7VzlLtp-4"
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=123, type=enums.ChatType.PRIVATE)
        from_user = SimpleNamespace(id=7)
        id = 10

        async def reply(self, text, **kwargs):
            replies.append(text)

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, False)

    class Runner:
        jobs = JobRegistry()
        async def run(self, *args, **kwargs):
            raise MediaError("The file exceeds the 2 GB limit.")

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), Store())
    bot.runner = Runner()
    await bot.on_message(None, Message())
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
    assert replies == ["⚠️ The file exceeds the 2 GB limit."]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_type", "username"),
    [
        (enums.ChatType.PRIVATE, None),
        (enums.ChatType.SUPERGROUP, None),
        (enums.ChatType.SUPERGROUP, "publicgroup"),
    ],
)
@pytest.mark.parametrize(
    "error", [NoAttachments("No attachments"), NoMedia("Fetch failed"), AuthenticationRequired("Login required")],
)
async def test_text_only_posts_are_ignored_but_failures_reply(chat_type, username, error) -> None:
    replies = []

    class Message:
        text = "https://x.com/zhangqiaorjc/status/2105509406058463657"
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=123, type=chat_type, username=username)
        from_user = SimpleNamespace(id=7)
        id = 10

        async def reply(self, text, **kwargs):
            replies.append(text)

        async def delete(self):
            raise AssertionError("a text-only or failed source link must not be deleted")

    class Store:
        async def chat(self, chat_id, kind):
            return ChatSettings(chat_id, kind, True, False, False, 10, True)

    class Runner:
        jobs = JobRegistry()
        async def run(self, *args, **kwargs):
            raise error

    bot = Bot(SimpleNamespace(), SimpleNamespace(whitelist=frozenset()), Store())
    bot.runner = Runner()
    await bot.on_message(None, Message())
    if bot.job_tasks:
        await asyncio.gather(*bot.job_tasks)
    assert replies == ([] if isinstance(error, NoAttachments) else [f"⚠️ {error}"])


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
        "start", "help", "extractors", "music", "cancel",
    ]
    assert menus[0][1] == {}
    assert [command.command for command in menus[1][0]][-1] == "settings"
    assert menus[1][1]["scope"].type == "all_group_chats"
