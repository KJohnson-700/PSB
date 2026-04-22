"""
Execution-driver tests: PolyBot._execute_*_signal_impl paths that place orders.

These catch Python scoping bugs (e.g. UnboundLocalError on `side`) and ordering bugs
(`can_sell_token` vs `side` assignment) without booting MarketScanner / WebSocket / full bot.

See vault note: Hermes `projects/psb/notes/2026-04-22-psb-execution-driver-tests.md`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.main import PolyBot
from src.strategies.bitcoin import BitcoinSignal
from src.strategies.sol_lag import SOLLagSignal
from src.strategies.xrp_dump_hedge import XRPDumpHedgeSignal


def _bare_polybot() -> PolyBot:
    """PolyBot instance without running __init__ (no scanner/ws)."""
    return object.__new__(PolyBot)


def _base_config() -> dict:
    return {
        "trading": {
            "dry_run": True,
            "default_position_size": 10,
            "max_position_size": 15,
        }
    }


def _attach_mocks(bot: PolyBot) -> None:
    bot.config = _base_config()
    bot.bankroll = 500.0
    bot.risk_manager = MagicMock()
    bot.risk_manager.can_trade = MagicMock(return_value=(True, "OK"))
    bot.risk_manager.evaluate_entry = MagicMock(return_value=(True, 15.0, "ok"))
    bot.risk_manager.add_position = MagicMock()
    bot.risk_manager.active_positions = {}
    bot.journal = MagicMock()
    bot.notifier = MagicMock()
    bot.notifier.notify_trade = AsyncMock()
    bot.clob_client = MagicMock()
    bot.clob_client.can_sell_token = AsyncMock(return_value=True)
    order = MagicMock()
    order.order_id = "ord_exec_driver_test"
    bot.clob_client.place_order = AsyncMock(return_value=order)


def _sol_like_signal(*, action: str, strategy_name: str = "hype_lag") -> SOLLagSignal:
    return SOLLagSignal(
        market_id="m_exec_drv_1",
        market_question="Hyperliquid Up or Down — test",
        action=action,
        price=0.5,
        size=10.0,
        confidence=0.6,
        edge=0.1,
        token_id_yes="0x" + "a" * 64,
        token_id_no="0x" + "b" * 64,
        end_date=datetime.now(timezone.utc) + timedelta(hours=1),
        direction="UP",
        strategy_name=strategy_name,
        reason="execution driver test",
    )


def _bitcoin_signal(*, action: str = "BUY_YES") -> BitcoinSignal:
    return BitcoinSignal(
        market_id="m_btc_1",
        market_question="Bitcoin Up or Down — test",
        action=action,
        price=0.5,
        size=10.0,
        confidence=0.6,
        edge=0.1,
        token_id_yes="0x" + "c" * 64,
        token_id_no="0x" + "d" * 64,
        end_date=datetime.now(timezone.utc) + timedelta(hours=1),
        direction="UP",
        htf_bias="BULLISH",
    )


def _xrp_signal(*, action: str = "BUY_YES") -> XRPDumpHedgeSignal:
    return XRPDumpHedgeSignal(
        market_id="m_xrp_1",
        market_question="XRP Up or Down — test",
        action=action,
        price=0.4,
        size=10.0,
        token_id_yes="0x" + "e" * 64,
        token_id_no="0x" + "f" * 64,
        end_date=datetime.now(timezone.utc) + timedelta(hours=1),
        leg="1",
    )


@pytest.mark.asyncio
async def test_execute_sol_lag_impl_hype_lag_buy_yes_no_unbound_side():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _sol_like_signal(action="BUY_YES", strategy_name="hype_lag")
    await bot._execute_sol_lag_signal_impl(sig)
    bot.clob_client.place_order.assert_called_once()
    kwargs = bot.clob_client.place_order.call_args.kwargs
    assert kwargs["side"] == "BUY"
    bot.journal.log_entry.assert_called_once()


@pytest.mark.asyncio
async def test_execute_sol_lag_impl_sell_yes():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _sol_like_signal(action="SELL_YES", strategy_name="sol_lag")
    await bot._execute_sol_lag_signal_impl(sig)
    assert bot.clob_client.place_order.call_args.kwargs["side"] == "SELL"


@pytest.mark.asyncio
async def test_execute_sol_lag_impl_unknown_action_returns_without_place():
    bot = _bare_polybot()
    _attach_mocks(bot)
    raw = _sol_like_signal(action="BUY_YES", strategy_name="sol_lag")
    sig = raw.model_copy(update={"action": "BUY_NO"})
    await bot._execute_sol_lag_signal_impl(sig)
    bot.clob_client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_bitcoin_impl_sets_side_before_order():
    bot = _bare_polybot()
    _attach_mocks(bot)
    await bot._execute_bitcoin_signal_impl(_bitcoin_signal(action="BUY_YES"))
    assert bot.clob_client.place_order.call_args.kwargs["side"] == "BUY"


@pytest.mark.asyncio
async def test_execute_xrp_dump_hedge_impl_buy_yes_leg():
    bot = _bare_polybot()
    _attach_mocks(bot)
    await bot._execute_xrp_dump_hedge_signal_impl(_xrp_signal(action="BUY_YES"))
    bot.clob_client.place_order.assert_called_once()
    assert bot.clob_client.place_order.call_args.kwargs["side"] == "BUY"
