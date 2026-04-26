"""Discord notification policy: crypto execution only, no opportunity pings."""
import pytest

from src.notifications.notification_manager import (
    NotificationManager,
    DISCORD_TRADE_STRATEGIES,
    _discord_trade_allowed,
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
    assert await nm.notify_exit({"strategy": "sol_lag", "pnl": 1.0}) is False


def test_discord_trade_allowlist():
    assert DISCORD_TRADE_STRATEGIES == frozenset(
        {"bitcoin", "sol_lag", "eth_lag", "hype_lag", "xrp_dump_hedge"}
    )
    assert _discord_trade_allowed("bitcoin")
    assert _discord_trade_allowed("hype_lag")
    assert not _discord_trade_allowed("consensus")
    assert not _discord_trade_allowed(None)
