"""Tests for the shared m5 direction → probability adjustment."""

from src.strategies._core import score_m5_direction


# ── LONG side ────────────────────────────────────────────────────────────────

def test_long_spike_up_full_bonus():
    assert score_m5_direction("SPIKE_UP", "LONG") == 0.06


def test_long_drift_up_partial_bonus():
    assert score_m5_direction("DRIFT_UP", "LONG") == 0.04


def test_long_spike_down_full_penalty():
    assert score_m5_direction("SPIKE_DOWN", "LONG") == -0.04


def test_long_drift_down_full_penalty():
    assert score_m5_direction("DRIFT_DOWN", "LONG") == -0.04


def test_long_lean_up_weak_bonus():
    # Dead in production (producer never emits LEAN for m5_direction)
    # but locked here so a future producer change behaves predictably.
    assert score_m5_direction("LEAN_UP", "LONG") == 0.01


def test_long_lean_down_weak_penalty():
    assert score_m5_direction("LEAN_DOWN", "LONG") == -0.01


def test_long_none_or_empty_zero():
    assert score_m5_direction("NONE", "LONG") == 0.0
    assert score_m5_direction("", "LONG") == 0.0


# ── SHORT side ───────────────────────────────────────────────────────────────

def test_short_mirrors_long():
    assert score_m5_direction("SPIKE_DOWN", "SHORT") == 0.06
    assert score_m5_direction("DRIFT_DOWN", "SHORT") == 0.04
    assert score_m5_direction("LEAN_DOWN", "SHORT") == 0.01
    assert score_m5_direction("SPIKE_UP", "SHORT") == -0.04
    assert score_m5_direction("DRIFT_UP", "SHORT") == -0.04
    assert score_m5_direction("LEAN_UP", "SHORT") == -0.01


# ── parity: backtest _calc_m5_momentum delegates here ────────────────────────

def test_backtest_m5_calc_delegates_to_core():
    """UpdownBacktestEngine._calc_m5_momentum should now return the same scoring
    that score_m5_direction produces. This is a structural assertion; the
    direction-classification half of _calc_m5_momentum still reads bars."""
    from src.backtest.updown_engine import UpdownBacktestEngine
    import pandas as pd

    # Build a 1m bar slice that produces a SPIKE_UP (>0.08% move) in the early window
    rows = [
        {"open_time": pd.Timestamp("2026-01-01 00:00", tz="UTC"), "open": 100.0,
         "high": 100.2, "low": 99.9, "close": 100.1, "volume": 1.0},
        {"open_time": pd.Timestamp("2026-01-01 00:01", tz="UTC"), "open": 100.1,
         "high": 100.3, "low": 100.0, "close": 100.2, "volume": 1.0},
    ]
    df = pd.DataFrame(rows)
    direction, adj = UpdownBacktestEngine._calc_m5_momentum(
        df, pd.Timestamp("2026-01-01 00:00", tz="UTC"), "LONG"
    )
    assert direction == "SPIKE_UP"
    assert adj == score_m5_direction("SPIKE_UP", "LONG") == 0.06
