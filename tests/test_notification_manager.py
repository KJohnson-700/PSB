"""Discord notification policy: crypto execution only, no opportunity pings."""
import pytest

from src.notifications.notification_manager import (
    NotificationManager,
    DISCORD_TRADE_STRATEGIES,
    _discord_trade_allowed,
    merge_discord_webhook_from_env,
    format_discord_notifications_log_line,
)


@pytest.mark.asyncio
async def test_notify_trade_only_crypto_strategies():
    nm = NotificationManager({"enabled": True, "discord_webhook": "", "alert_on_trade": True})
    # No webhook — send_discord short-circuits; we still verify gate returns early for non-crypto
    assert await nm.notify_trade({"strategy": "consensus", "side": "BUY"}) is False
    assert await nm.notify_trade({"strategy": "weather", "side": "BUY"}) is False
    assert await nm.notify_trade({"strategy": "bitcoin", "side": "BUY"}) is False  # no webhook


@pytest.mark.asyncio
async def test_notify_exit_only_crypto_strategies():
    nm = NotificationManager({"enabled": True, "discord_webhook": "", "alert_on_exit": True})
    assert await nm.notify_exit({"strategy": "weather", "pnl": 1.0}) is False
    assert await nm.notify_exit({"strategy": "sol_macro", "pnl": 1.0}) is False


def test_discord_trade_allowlist():
    assert DISCORD_TRADE_STRATEGIES == frozenset(
        {
            "bitcoin",
            "sol_macro",
            "eth_macro",
            "hype_macro",
            "xrp_macro",
            "xrp_dump_hedge",
        }
    )
    assert _discord_trade_allowed("bitcoin")
    assert _discord_trade_allowed("hype_macro")
    assert _discord_trade_allowed("xrp_dump_hedge")
    assert not _discord_trade_allowed("consensus")
    assert not _discord_trade_allowed(None)


def test_merge_discord_webhook_from_env(monkeypatch):
    cfg = {"notifications": {"discord_webhook": ""}}
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.com/hook")
    merge_discord_webhook_from_env(cfg)
    assert cfg["notifications"]["discord_webhook"] == "https://example.com/hook"


def test_notification_manager_reload_from_config():
    nm = NotificationManager(
        {
            "notifications": {
                "enabled": True,
                "discord_webhook": "https://a.example/hook",
                "alert_on_exit": False,
            }
        }
    )
    nm.reload_from_config(
        {
            "notifications": {
                "enabled": False,
                "discord_webhook": "https://b.example/hook",
                "alert_on_exit": True,
            }
        }
    )
    assert nm.enabled is False
    assert nm.discord_webhook == "https://b.example/hook"
    assert nm.alert_on_exit is True


def test_format_discord_notifications_log_line():
    assert "no webhook" in format_discord_notifications_log_line({"notifications": {}})
    line = format_discord_notifications_log_line(
        {
            "notifications": {
                "enabled": True,
                "discord_webhook": "https://x.test/h",
                "alert_on_exit": True,
                "alert_on_error": False,
            }
        }
    )
    assert "webhook OK" in line
    assert "exit embeds ON" in line
    assert "errors OFF" in line
