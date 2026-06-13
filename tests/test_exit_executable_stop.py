"""Executable-price stop trigger (trading.exit_rules.stop_use_executable_price).

A long-YES updown position whose YES MIDPOINT is only -10% (above the 15% stop) but
whose executable YES BID is -16% (below it). With the flag ON the stop must fire and
fill at the bid; with the flag OFF (midpoint marking) it must not fire at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

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


def test_no_stop_on_midpoint_when_disabled():
    # Same book: midpoint is only -10%, so the default (midpoint) stop must not fire.
    exits = _mgr(False).check_exits({"p1": _pos()}, _PRICES, _TOKENS, _LIQ)
    assert exits == []


def test_missing_book_falls_back_to_midpoint_no_fire():
    # Flag on but no liquidity snapshot -> fail-safe to midpoint -> no stop at -10%.
    exits = _mgr(True).check_exits({"p1": _pos()}, _PRICES, _TOKENS, None)
    assert exits == []
