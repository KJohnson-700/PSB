"""Discord notification policy: crypto execution only, no opportunity pings."""
import pytest

from unittest.mock import AsyncMock

from src.notifications.notification_manager import (
    NotificationManager,
    DISCORD_TRADE_STRATEGIES,
    _discord_trade_allowed,
    _polymarket_market_url_for_exit,
    merge_discord_webhook_from_env,
    format_discord_notifications_log_line,
)


@pytest.mark.asyncio
async def test_notify_trade_only_crypto_strategies():
    nm = NotificationManager({"enabled": True, "discord_webhook": "", "alert_on_trade": True})
    # No webhook — send_discord short-circuits; still verify unknown labels are denied.
    assert await nm.notify_trade({"strategy": "legacy_label", "side": "BUY"}) is False
    assert await nm.notify_trade({"strategy": "bitcoin", "side": "BUY"}) is False  # no webhook


@pytest.mark.asyncio
async def test_notify_exit_only_crypto_strategies():
    nm = NotificationManager({"enabled": True, "discord_webhook": "", "alert_on_exit": True})
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
    assert not _discord_trade_allowed("legacy_label")
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
    assert "Discord entry/fill posts disabled" in line
    assert "exit embeds ON" in line
    assert "errors OFF" in line


def test_polymarket_market_url_for_exit_placeholder_when_missing():
    s, ok = _polymarket_market_url_for_exit({})
    assert ok is False
    assert "missing market_id" in s
    assert "polymarket.com" not in s


def test_polymarket_market_url_for_exit_when_market_id():
    s, ok = _polymarket_market_url_for_exit({"market_id": " 0xabc123 "})
    assert ok is True
    assert s == "https://polymarket.com/market/0xabc123"


@pytest.mark.asyncio
async def test_notify_exit_embed_polymarket_url_when_market_id():
    nm = NotificationManager(
        {
            "enabled": True,
            "discord_webhook": "https://example.com/hook",
            "alert_on_exit": True,
        }
    )
    nm.send_discord = AsyncMock(return_value=True)
    await nm.notify_exit(
        {
            "strategy": "sol_macro",
            "pnl": 1.5,
            "question": "Test market?",
            "reason": "take_profit",
            "price": 0.9,
            "side": "SELL",
            "size": 10,
            "market_id": "cond-123",
            "entry_price": 0.5,
            "trade_id_tail": "tail-abc",
        }
    )
    nm.send_discord.assert_awaited_once()
    embed = nm.send_discord.await_args.args[1]
    polym = next(f["value"] for f in embed["fields"] if f["name"] == "Polymarket")
    assert "https://polymarket.com/market/cond-123" == polym


@pytest.mark.asyncio
async def test_notify_exit_embed_polymarket_placeholder_when_no_market_id():
    nm = NotificationManager(
        {
            "enabled": True,
            "discord_webhook": "https://example.com/hook",
            "alert_on_exit": True,
        }
    )
    nm.send_discord = AsyncMock(return_value=True)
    await nm.notify_exit(
        {
            "strategy": "eth_macro",
            "pnl": -0.5,
            "question": "Q?",
            "reason": "stop_loss",
            "price": 0.4,
            "side": "SELL",
            "size": 5,
            "market_id": "",
        }
    )
    embed = nm.send_discord.await_args.args[1]
    polym = next(f["value"] for f in embed["fields"] if f["name"] == "Polymarket")
    assert "polymarket.com" not in polym
    assert "missing market_id" in polym
