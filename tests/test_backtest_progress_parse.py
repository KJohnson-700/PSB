"""Tests for crypto backtest stdout progress parsing."""

from __future__ import annotations

from src.dashboard.backtest_progress_parse import parse_crypto_progress_from_lines


def test_parse_empty():
    assert parse_crypto_progress_from_lines([]) == {
        "progress_pct": None,
        "progress_current": None,
        "progress_total": None,
    }


def test_parse_last_progress_wins():
    lines = [
        "  progress 1,000/10,000 windows (10.0%) trades=0 bankroll=$500.00",
        "noise",
        "  progress 2,500/10,000 windows (25.0%) trades=3 bankroll=$512.00",
    ]
    out = parse_crypto_progress_from_lines(lines)
    assert out["progress_pct"] == 25.0
    assert out["progress_current"] == 2500
    assert out["progress_total"] == 10000


def test_parse_no_commas():
    out = parse_crypto_progress_from_lines(["  progress 3/100 windows (3.0%) x"])
    assert out["progress_pct"] == 3.0
    assert out["progress_current"] == 3
    assert out["progress_total"] == 100
