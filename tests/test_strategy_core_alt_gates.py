"""Tests for the alt-strategy primitives (sol_macro / backtest)."""

from src.analysis.btc_price_service import MACDResult
from src.strategies._core import (
    alt_1h_hist_gate,
    anti_ltf_gate_skip_reason,
    apply_primary_htf_bias,
    btc_catalyst_boost,
    sol_rsi_extremes_adj,
)


def _macd(*, hist: float, rising: bool) -> MACDResult:
    return MACDResult(
        macd_line=0.0, signal_line=0.0,
        histogram=hist, prev_histogram=0.0,
        crossover="NONE", histogram_rising=rising,
        above_zero=hist > 0,
    )


# ── apply_primary_htf_bias ───────────────────────────────────────────────────

def test_apply_bias_bullish():
    assert abs(apply_primary_htf_bias(0.50, "BULLISH", 0.07) - 0.57) < 1e-9


def test_apply_bias_bearish():
    assert abs(apply_primary_htf_bias(0.50, "BEARISH", 0.07) - 0.43) < 1e-9


def test_apply_bias_neutral_unchanged():
    assert apply_primary_htf_bias(0.50, "NEUTRAL", 0.07) == 0.50


# ── alt_1h_hist_gate (the drift fix) ─────────────────────────────────────────

def test_long_gate_passes_when_rising():
    r = alt_1h_hist_gate(_macd(hist=0.1, rising=True), "LONG")
    assert r.allowed


def test_long_gate_passes_when_positive_but_decelerating():
    """Drift fix: pre-refactor backtest rejected this; live always allowed it."""
    r = alt_1h_hist_gate(_macd(hist=0.1, rising=False), "LONG")
    assert r.allowed, "positive histogram should pass even if decelerating"


def test_long_gate_blocks_when_negative_and_falling():
    r = alt_1h_hist_gate(_macd(hist=-0.05, rising=False), "LONG")
    assert not r.allowed
    assert r.rejection_reason == "histogram_1h_blocks_long_15m"


def test_short_gate_passes_when_negative_but_rising():
    r = alt_1h_hist_gate(_macd(hist=-0.1, rising=True), "SHORT")
    assert r.allowed


def test_short_gate_blocks_when_positive_and_rising():
    r = alt_1h_hist_gate(_macd(hist=0.05, rising=True), "SHORT")
    assert not r.allowed


# ── sol_rsi_extremes_adj ─────────────────────────────────────────────────────

def test_rsi_extremes_overbought():
    assert sol_rsi_extremes_adj(76.0) == -0.03


def test_rsi_extremes_oversold():
    assert sol_rsi_extremes_adj(20.0) == 0.03


def test_rsi_extremes_neutral():
    assert sol_rsi_extremes_adj(50.0) == 0.0


def test_rsi_extremes_5m_lighter_magnitude():
    assert sol_rsi_extremes_adj(76.0, magnitude=0.02) == -0.02


# ── btc_catalyst_boost ───────────────────────────────────────────────────────

def test_catalyst_lag_aligned_long():
    b = btc_catalyst_boost(
        lag_opportunity=True, lag_direction="LONG", lag_magnitude=2.0,
        btc_spike_detected=False, allowed_side="LONG",
    )
    assert abs(b - 0.03) < 1e-9  # min(0.04, max(0.02, 2.0*0.015)) = 0.03


def test_catalyst_lag_magnitude_floor():
    b = btc_catalyst_boost(
        lag_opportunity=True, lag_direction="LONG", lag_magnitude=0.1,
        btc_spike_detected=False, allowed_side="LONG",
    )
    assert b == 0.02  # clamped to floor


def test_catalyst_lag_magnitude_ceiling():
    b = btc_catalyst_boost(
        lag_opportunity=True, lag_direction="LONG", lag_magnitude=10.0,
        btc_spike_detected=False, allowed_side="LONG",
    )
    assert b == 0.04


def test_catalyst_lag_direction_mismatch_falls_to_spike():
    b = btc_catalyst_boost(
        lag_opportunity=True, lag_direction="SHORT", lag_magnitude=2.0,
        btc_spike_detected=True, allowed_side="LONG",
    )
    assert b == 0.03


def test_catalyst_short_flips_sign():
    b = btc_catalyst_boost(
        lag_opportunity=False, lag_direction="", lag_magnitude=0.0,
        btc_spike_detected=True, allowed_side="SHORT",
    )
    assert b == -0.03


# ── anti_ltf_gate_skip_reason ────────────────────────────────────────────────

def test_anti_ltf_default_skips_when_confirmed():
    assert anti_ltf_gate_skip_reason(
        ltf_confirmed=True, require_ltf_confirmation=False,
        anti_ltf_gate_enabled=True,
    ) == "anti_ltf_confirmed_15m"


def test_anti_ltf_default_passes_when_unconfirmed():
    assert anti_ltf_gate_skip_reason(
        ltf_confirmed=False, require_ltf_confirmation=False,
        anti_ltf_gate_enabled=True,
    ) == ""


def test_require_ltf_inverts_policy():
    # unconfirmed -> skip
    assert anti_ltf_gate_skip_reason(
        ltf_confirmed=False, require_ltf_confirmation=True,
        anti_ltf_gate_enabled=True,
    ) == "ltf_required_unconfirmed_15m"
    # confirmed -> pass
    assert anti_ltf_gate_skip_reason(
        ltf_confirmed=True, require_ltf_confirmation=True,
        anti_ltf_gate_enabled=True,
    ) == ""


def test_anti_ltf_disabled_passes_through():
    assert anti_ltf_gate_skip_reason(
        ltf_confirmed=True, require_ltf_confirmation=False,
        anti_ltf_gate_enabled=False,
    ) == ""


def test_eth_follow_exception_overrides_default_anti_ltf():
    assert anti_ltf_gate_skip_reason(
        ltf_confirmed=True, require_ltf_confirmation=False,
        anti_ltf_gate_enabled=True, eth_15m_follow_exception=True,
    ) == ""


# ── parity: live wrappers use the core ───────────────────────────────────────

def test_live_sol_macro_apply_bias_uses_core():
    from unittest.mock import MagicMock
    from src.strategies.sol_macro import SolMacroStrategy

    s = SolMacroStrategy(
        {"strategies": {"sol_macro": {}}}, MagicMock(), MagicMock(),
    )
    assert s._apply_primary_htf_bias(0.50, "BULLISH", 0.10) == 0.60
    assert s._apply_primary_htf_bias(0.50, "BEARISH", 0.10) == 0.40
    assert s._apply_primary_htf_bias(0.50, "NEUTRAL", 0.10) == 0.50
