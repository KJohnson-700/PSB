"""RiskManager.position_entry_notional matches evaluate_entry cost semantics."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.execution.clob_client import RiskManager


def test_position_notional_buy_no_leg():
    p = MagicMock()
    p.entry_leg = "NO"
    p.outcome = "NO"
    p.size = 10.0
    p.entry_price = 0.55
    assert RiskManager.position_entry_notional(p) == 5.5


def test_position_notional_buy_yes():
    p = MagicMock()
    p.entry_leg = "YES"
    p.outcome = "YES"
    p.size = 10.0
    p.entry_price = 0.5
    assert RiskManager.position_entry_notional(p) == 5.0


def test_position_notional_short_yes_sell():
    p = MagicMock()
    p.entry_leg = "YES"
    p.outcome = "NO"
    p.size = 10.0
    p.entry_price = 0.5
    assert RiskManager.position_entry_notional(p) == 5.0
