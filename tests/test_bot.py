from types import SimpleNamespace

from hydrogram import enums

from internal.bot.main import allowed, chat_kind
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
