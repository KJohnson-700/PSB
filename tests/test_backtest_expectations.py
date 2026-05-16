"""Tests for backtest expectation key parsing and live trade matching."""

from src.execution.backtest_expectations import (
    live_trade_window_minutes,
    live_trades_for_expectation,
    parse_backtest_report_strategy,
)


def test_parse_backtest_report_strategy_window_suffix():
    base, wm = parse_backtest_report_strategy("bitcoin_30m")
    assert base == "bitcoin"
    assert wm == 30


def test_parse_backtest_report_strategy_plain():
    base, wm = parse_backtest_report_strategy("sol_macro")
    assert base == "sol_macro"
    assert wm is None


def test_live_trades_for_expectation_filters_window():
    live = [
        {"strategy": "bitcoin", "window_size": "5m", "event": "EXIT"},
        {"strategy": "bitcoin", "window_size": "30m", "event": "EXIT"},
        {"strategy": "sol_macro", "window_size": "30m", "event": "EXIT"},
    ]
    assert len(live_trades_for_expectation(live, "bitcoin_30m")) == 1
    assert live_trades_for_expectation(live, "bitcoin_30m")[0]["window_size"] == "30m"
    assert len(live_trades_for_expectation(live, "bitcoin")) == 2


def test_live_trade_window_minutes_normalizes():
    assert live_trade_window_minutes({"window": "15m"}) == 15
    assert live_trade_window_minutes({"window_size": "5"}) == 5
