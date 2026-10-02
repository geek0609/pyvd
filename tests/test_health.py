from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timezone

import pytest
from hydrogram import enums

from internal.bot.health import cookie_status, health_report, proxy_reachable
from internal.bot.main import Bot, bot_commands, help_text
from internal.config.settings import SiteConfig


def cookie(domain=".instagram.com", expiry="2000", name="sessionid", value="account-secret"):
    return f"{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}\n"


def settings(tmp_path, proxy="", sites=None):
    return SimpleNamespace(
        private_dir=tmp_path, proxy=proxy, site_configs=sites or {},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,private", [(True, True), (False, True), (True, False), (False, False)])
async def test_health_command_is_only_available_to_owner_in_dm(owner, private, monkeypatch):
    report = AsyncMock(return_value="PyVD health\nJobs: 2 active, 3 queued")
    monkeypatch.setattr("internal.bot.main.health_report", report)
    replies = []

    class Message:
        text = "/health"
        date = datetime.now(timezone.utc)
        chat = SimpleNamespace(id=123, type=enums.ChatType.PRIVATE if private else enums.ChatType.GROUP)
        from_user = SimpleNamespace(id=7 if owner else 8)

        async def reply(self, text, **kwargs):
            replies.append(text)
            assert kwargs["parse_mode"] == enums.ParseMode.DISABLED

    config = SimpleNamespace(whitelist=frozenset(), admins=frozenset({7}))
    bot = Bot(SimpleNamespace(), config, SimpleNamespace())
    bot.runner = SimpleNamespace(queue=SimpleNamespace(active=2, queued=3))
    await bot.on_message(None, Message())
    if owner and private:
        report.assert_awaited_once_with(config, 2, 3)
        assert replies == [report.return_value]
    else:
        report.assert_not_awaited()
        assert not replies


def test_health_help_is_owner_only_and_absent_from_shared_menus():
    assert "/health" not in help_text("private")
    assert "/health" not in help_text("group", owner=True)
    assert "/health" in help_text("private", owner=True)
    assert all(command.command != "health" for command in bot_commands(False) + bot_commands(True))


def test_cookie_status_counts_expiries_without_account_details(tmp_path):
    path = tmp_path / "instagram.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        + cookie(domain="#HttpOnly_.instagram.com")
        + cookie(expiry="500", name="ds_user_id", value="123456")
        + cookie(expiry="0", name="csrftoken", value="csrf-secret")
    )
    result = cookie_status(path, now=1000)
    assert result == "1 unexpired, 1 expired, 1 session"
    assert all(secret not in result for secret in ("account-secret", "123456", "csrf-secret", "sessionid"))


@pytest.mark.parametrize("text,result", [
    ("# Netscape HTTP Cookie File\n", "empty"),
    ("not a cookie\n", "invalid format"),
    (cookie(expiry="NaN"), "invalid format"),
    (cookie(expiry="inf"), "invalid format"),
    (cookie(expiry="2000.5"), "1 unexpired"),
])
def test_cookie_status_empty_and_invalid_formats(tmp_path, text, result):
    path = tmp_path / "cookies.txt"
    path.write_text(text)
    assert cookie_status(path, now=1000) == result


def test_cookie_status_unreadable_and_oversize(tmp_path):
    assert cookie_status(tmp_path / "missing") == "unreadable"
    path = tmp_path / "large"
    path.write_bytes(b"x" * 1_048_577)
    assert cookie_status(path) == "file too large to check"


@pytest.mark.asyncio
async def test_health_report_has_only_counts_and_safe_cookie_status(tmp_path, monkeypatch):
    (tmp_path / "cookies").mkdir()
    (tmp_path / "cookies" / "instagram.txt").write_text(cookie(expiry="9000000000"))
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr("internal.bot.health.proxy_reachable", probe)
    report = await health_report(settings(tmp_path), active=2, queued=7)
    assert "Jobs: 2 active, 7 queued" in report
    assert "instagram: 1 unexpired" in report
    assert "login validity is unverified" in report
    assert "No proxy configured" in report
    assert "account-secret" not in report and "sessionid" not in report
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_probes_distinct_proxies_once_without_printing_urls(tmp_path, monkeypatch):
    proxy = "http://proxy-user:proxy-password@proxy.example:9999"
    other_proxy = "socks5://other-user:other-password@other.example:1111"
    config = settings(tmp_path, proxy, {
        "youtube": SiteConfig(proxy=proxy),
        "instagram": SiteConfig(download_proxy=other_proxy),
        "reddit": SiteConfig(edge_proxy="https://secret-edge.example"),
    })
    probe = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr("internal.bot.health.proxy_reachable", probe)
    report = await health_report(config, active=0, queued=0)
    assert "default, youtube: reachable" in report
    assert "instagram downloads: unreachable" in report
    assert "Unsupported edge proxy configured: reddit" in report
    assert probe.await_count == 2
    for secret in (proxy, other_proxy, "proxy-password", "other-password", "secret-edge"):
        assert secret not in report


@pytest.mark.asyncio
async def test_proxy_probe_has_bounded_timeout_and_does_not_read_cookies(monkeypatch):
    calls = []

    class Session:
        def __init__(self, **kwargs):
            assert kwargs == {"trust_env": False}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def head(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=204)

    monkeypatch.setattr("internal.bot.health.AsyncSession", Session)
    assert await proxy_reachable("http://configured-proxy:1234")
    assert calls[0][1] == {
        "proxy": "http://configured-proxy:1234", "timeout": (3, 5),
        "allow_redirects": False, "discard_cookies": True,
    }


@pytest.mark.asyncio
async def test_proxy_probe_failure_does_not_expose_exception(monkeypatch):
    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("proxy-password account-secret")

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("internal.bot.health.AsyncSession", Session)
    assert not await proxy_reachable("secret")
