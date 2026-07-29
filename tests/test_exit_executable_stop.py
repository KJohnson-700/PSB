"""Executable-price stop trigger (trading.exit_rules.stop_use_executable_price).

A long-YES updown position whose YES MIDPOINT is only -10% (above the 15% stop) but
whose executable YES BID is -16% (below it). With the flag ON the stop must fire and
fill at the bid; with the flag OFF (midpoint marking) it must not fire at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.execution.live_testing import PositionExitManager


def _mgr(flag: bool) -> PositionExitManager:
    return PositionExitManager(
        {
            "trading": {
                "exit_rules": {
                    "enabled": True,
                    "take_profit_pct": 0.30,
                    "stop_loss_pct": 0.30,
                    "updown_stop_loss_pct": 0.15,
                    "stop_use_executable_price": flag,
                }
            }
        }
    )


def _pos() -> SimpleNamespace:
    return SimpleNamespace(
        market_id="m1",
        market_question="Ethereum Up or Down - test",
        outcome="YES",
        strategy="eth_macro",
        size=10.0,
        entry_price=0.50,
        entry_leg="YES",
        opened_at=datetime.now() - timedelta(minutes=3),
        end_date=None,
        window_size="15m",
    )


# YES midpoint 0.45 = -10% on a 0.50 entry (above the 15% stop); the bid 0.42 = -16%.
_PRICES = {"m1": 0.45}
_TOKENS = {"m1": ("YES_TOKEN", "NO_TOKEN")}
_LIQ = {"m1": {"best_bid": 0.42, "best_ask": 0.48}}


def test_executable_stop_fires_on_bid_when_enabled():
    exits = _mgr(True).check_exits({"p1": _pos()}, _PRICES, _TOKENS, _LIQ)
    assert len(exits) == 1
    d = exits[0]
    assert d.reason == "updown_stop_loss"
    assert d.action == "SELL"
    assert d.token_id == "YES_TOKEN"
    assert d.exit_price == 0.42  # filled at the executable bid, not the 0.45 midpoint
    assert d.marketable is True  # close must be placed FAK, not a resting limit


def test_no_stop_on_midpoint_when_disabled():
    # Same book: midpoint is only -10%, so the default (midpoint) stop must not fire.
    exits = _mgr(False).check_exits({"p1": _pos()}, _PRICES, _TOKENS, _LIQ)
    assert exits == []


def test_missing_book_falls_back_to_midpoint_no_fire():
    # Flag on but no liquidity snapshot -> fail-safe to midpoint -> no stop at -10%.
    exits = _mgr(True).check_exits({"p1": _pos()}, _PRICES, _TOKENS, None)
    assert exits == []


def test_realistic_paper_fill_uses_bid_ladder_vwap():
    # Long-YES TP at midpoint 0.65 (+30%), but the bid ladder VWAP for size 30 is
    # 0.60 -> recorded exit fills at 0.60, not the 0.65 mark.
    mgr = PositionExitManager(
        {
            "trading": {
                "exit_rules": {
                    "enabled": True,
                    "take_profit_pct": 0.30,
                    "stop_loss_pct": 0.30,
                    "updown_stop_loss_pct": 0.15,
                    "realistic_paper_fills": True,
                }
            }
        }
    )
    pos = SimpleNamespace(
        market_id="m1",
        market_question="Ethereum Up or Down - test",
        outcome="YES",
        strategy="eth_macro",
        size=30.0,
        entry_price=0.50,
        entry_leg="YES",
        opened_at=datetime.now() - timedelta(minutes=3),
        end_date=None,
        window_size="15m",
    )
    liq = {"m1": {"bids": [
        {"price": 0.62, "size": 10.0},
        {"price": 0.60, "size": 10.0},
        {"price": 0.58, "size": 10.0},
    ]}}
    exits = mgr.check_exits({"p1": pos}, {"m1": 0.65}, _TOKENS, liq)
    assert len(exits) == 1
    d = exits[0]
    assert d.reason == "take_profit"
    assert d.exit_price == pytest.approx(0.60)  # bid-ladder VWAP, not the 0.65 mark
    assert d.unrealized_pnl == pytest.approx(3.0)  # 30 * (0.60 - 0.50), not 4.5
    # Per-lane fill-quality telemetry: mark 0.65, slippage (3.0-4.5)/15 = -0.10.
    assert d.fill_mark_price == pytest.approx(0.65)
    assert d.fill_slippage_pct == pytest.approx(-0.10)


def _rpf_mgr() -> PositionExitManager:
    return PositionExitManager(
        {
            "trading": {
                "exit_rules": {
                    "enabled": True,
                    "take_profit_pct": 0.30,
                    "stop_loss_pct": 0.30,
                    "updown_stop_loss_pct": 0.15,
                    "realistic_paper_fills": True,
                }
            }
        }
    )


def _rpf_fee_mgr() -> PositionExitManager:
    return PositionExitManager(
        {
            "trading": {
                "exit_rules": {
                    "enabled": True,
                    "take_profit_pct": 0.30,
                    "stop_loss_pct": 0.30,
                    "updown_stop_loss_pct": 0.15,
                    "realistic_paper_fills": True,
                },
                "execution_fees": {
                    "enabled": True,
                    "crypto_updown_15m_taker_fee_rate": 0.07,
                },
            }
        }
    )


def test_realistic_fill_long_no_walks_mirrored_ask_ladder():
    # Long NO entered at 0.40. YES asks 0.33/0.35/0.37 -> NO bids 0.67/0.65/0.63;
    # selling 30 NO sweeps them -> VWAP 0.65. TP fires (NO price 0.65 vs entry 0.40).
    pos = SimpleNamespace(
        market_id="m1", market_question="Ethereum Up or Down - test",
        outcome="NO", strategy="eth_macro", size=30.0, entry_price=0.40,
        entry_leg="NO", opened_at=datetime.now() - timedelta(minutes=3),
        end_date=None, window_size="15m",
    )
    liq = {"m1": {"asks": [
        {"price": 0.33, "size": 10.0},
        {"price": 0.35, "size": 10.0},
        {"price": 0.37, "size": 10.0},
    ]}}
    # YES mid 0.35 -> NO mid 0.65, a +0.25 gain on a 0.40 entry = well past TP.
    exits = _rpf_mgr().check_exits({"p1": pos}, {"m1": 0.35}, _TOKENS, liq)
    assert len(exits) == 1
    d = exits[0]
    assert d.exit_price == pytest.approx(0.65)  # NO-space VWAP (1 - 0.35)
    assert d.unrealized_pnl == pytest.approx(30 * (0.65 - 0.40))


def test_realistic_fill_short_yes_walks_ask_ladder():
    # Short YES at 0.60 (outcome NO, not entry_leg NO): buy back YES by walking asks.
    pos = SimpleNamespace(
        market_id="m1", market_question="Ethereum Up or Down - test",
        outcome="NO", strategy="eth_macro", size=20.0, entry_price=0.60,
        entry_leg="YES", opened_at=datetime.now() - timedelta(minutes=3),
        end_date=None, window_size="15m",
    )
    liq = {"m1": {"asks": [{"price": 0.30, "size": 10.0}, {"price": 0.32, "size": 10.0}]}}
    # YES mid 0.31 -> short is deep in profit (entry 0.60) -> TP fires.
    exits = _rpf_mgr().check_exits({"p1": pos}, {"m1": 0.31}, _TOKENS, liq)
    assert len(exits) == 1
    d = exits[0]
    assert d.exit_price == pytest.approx(0.31)  # YES ask VWAP we buy back at
    assert d.unrealized_pnl == pytest.approx(20 * (0.60 - 0.31))


def test_15m_crypto_fee_is_subtracted_from_realistic_paper_exit():
    pos = SimpleNamespace(
        market_id="m1", market_question="Ethereum Up or Down - test",
        outcome="YES", strategy="eth_macro", size=100.0, entry_price=0.50,
        entry_leg="YES", opened_at=datetime.now() - timedelta(minutes=3),
        end_date=None, window_size="15m",
    )
    liq = {"m1": {"bids": [{"price": 0.50, "size": 100.0}]}}

    exits = _rpf_fee_mgr().check_exits({"p1": pos}, {"m1": 0.65}, _TOKENS, liq)

    assert len(exits) == 1
    d = exits[0]
    assert d.exit_price == pytest.approx(0.50)
    # Round-trip taker fee (marketable entries are taker too): entry + exit.
    # 100 * 0.07 * 0.50 * 0.50 = 1.75 per leg -> 3.50 round-trip.
    assert d.fill_fee_usdc == pytest.approx(3.50)
    assert d.fill_fee_rate == pytest.approx(0.07)
    assert d.unrealized_pnl == pytest.approx(-3.50)


def test_live_fee_metadata_overrides_config_fallback():
    pos = SimpleNamespace(
        market_id="m1", market_question="Ethereum Up or Down - test",
        outcome="YES", strategy="eth_macro", size=100.0, entry_price=0.50,
        entry_leg="YES", opened_at=datetime.now() - timedelta(minutes=3),
        end_date=None, window_size="15m",
    )
    liq = {"m1": {"bids": [{"price": 0.50, "size": 100.0}], "taker_fee_rate": 0.03}}

    exits = _rpf_fee_mgr().check_exits({"p1": pos}, {"m1": 0.65}, _TOKENS, liq)

    assert len(exits) == 1
    d = exits[0]
    # Live fee metadata (0.03) overrides config fallback; round-trip = 0.75 * 2.
    assert d.fill_fee_usdc == pytest.approx(1.50)
    assert d.fill_fee_rate == pytest.approx(0.03)
    assert d.unrealized_pnl == pytest.approx(-1.50)


def test_place_order_routes_order_type_to_post_order(monkeypatch):
    # Live-path bug fix: OrderArgs has no order_type field; the time-in-force goes to
    # post_order. FAK (marketable) for exits, post_only flag preserved.
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from py_clob_client_v2 import OrderType

    from src.execution.clob_client import CLOBClient

    c = CLOBClient({})
    monkeypatch.setattr(CLOBClient, "live_execution_supported", staticmethod(lambda: True))
    c.client = MagicMock()
    # 2026-07-27 entry-outage fix: FAK/FOK are MARKET orders on the CLOB (maker<=2dec,
    # taker<=4dec) and the bot routes them through create_market_order, NOT create_order
    # (the limit path built swapped precision and 400'd). GTC still uses create_order.
    c.client.create_market_order.return_value = "SIGNED_MKT_ORDER"
    c.client.create_order.return_value = "SIGNED_LIMIT_ORDER"
    c.client.post_order.return_value = {"order_id": "oid1"}
    c.ensure_fresh_credentials = AsyncMock(return_value=True)

    order = asyncio.run(
        c.place_order(
            token_id="t", side="SELL", price=0.42, size=10.0,
            dry_run=False, order_type="FAK",
        )
    )
    assert order is not None and order.order_id == "oid1"
    # FAK routed through the MARKET path; its signed order is what reaches post_order.
    c.client.create_market_order.assert_called_once()
    c.client.create_order.assert_not_called()
    args, _ = c.client.post_order.call_args
    assert args[0] == "SIGNED_MKT_ORDER"
    assert args[1] == OrderType.FAK      # time-in-force routed to post_order
    assert args[2] is False              # post_only flag preserved (default False)
