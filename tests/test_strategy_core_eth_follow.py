"""Tests for the ETH-follow scoring core helpers."""

from src.analysis.btc_price_service import CandleMomentum, MACDResult
from src.strategies._core import (
    btc_follow_5m_impulse,
    btc_follow_15m_impulse_ok,
    eth_5m_macd_score,
    eth_15m_follow_score,
)


def _mom(direction: str = "", *, in_window: bool = False) -> CandleMomentum:
    return CandleMomentum(
        m5_direction=direction,
        m5_in_prediction_window=in_window,
    )


def _macd(*, cross="NONE", hist=0.0, prev_hist=0.0, rising=False) -> MACDResult:
    return MACDResult(
        macd_line=0.0, signal_line=0.0,
        histogram=hist, prev_histogram=prev_hist,
        crossover=cross, histogram_rising=rising,
        above_zero=hist > 0,
    )


# ── btc_follow_5m_impulse ────────────────────────────────────────────────────

def test_btc_follow_long_spike_up():
    r = btc_follow_5m_impulse(_mom("SPIKE_UP"), "LONG")
    assert r.score == 0.06


def test_btc_follow_long_drift_up():
    r = btc_follow_5m_impulse(_mom("DRIFT_UP"), "LONG")
    assert r.score == 0.04


def test_btc_follow_long_against_penalty():
    r = btc_follow_5m_impulse(_mom("SPIKE_DOWN"), "LONG")
    assert r.score == -0.05


def test_btc_follow_predict_window_bonus_when_positive():
    r = btc_follow_5m_impulse(_mom("SPIKE_UP", in_window=True), "LONG")
    assert r.score == 0.08  # 0.06 + 0.02


def test_btc_follow_predict_window_no_bonus_when_against():
    # Window doesn't add bonus when score is negative
    r = btc_follow_5m_impulse(_mom("SPIKE_DOWN", in_window=True), "LONG")
    assert r.score == -0.05


def test_btc_follow_short_mirrors():
    assert btc_follow_5m_impulse(_mom("SPIKE_DOWN"), "SHORT").score == 0.06
    assert btc_follow_5m_impulse(_mom("DRIFT_UP"), "SHORT").score == -0.05


# ── btc_follow_15m_impulse_ok ────────────────────────────────────────────────

def test_btc_follow_15m_long_cross_passes():
    assert btc_follow_15m_impulse_ok(
        _macd(cross="BULLISH_CROSS"), "", "LONG", min_hist=0.03
    )


def test_btc_follow_15m_long_strong_hist_passes():
    assert btc_follow_15m_impulse_ok(
        _macd(hist=0.10, rising=True), "", "LONG", min_hist=0.03
    )


def test_btc_follow_15m_long_via_candle_direction():
    assert btc_follow_15m_impulse_ok(
        _macd(), "SPIKE_UP", "LONG", min_hist=0.03
    )


def test_btc_follow_15m_long_rejects_nothing_aligned():
    assert not btc_follow_15m_impulse_ok(
        _macd(hist=0.01, rising=False), "NONE", "LONG", min_hist=0.03
    )


# ── eth_5m_macd_score ────────────────────────────────────────────────────────

def test_eth_5m_against_uses_minus_0_05():
    """Distinct from sol_m5_macd_adj which uses -0.04."""
    r = eth_5m_macd_score(_macd(hist=-0.05, rising=False), "LONG")
    assert r.score == -0.05


def test_eth_5m_no_macd_above_signal_tier():
    """ETH 5m has no '+0.02 macd>signal' tier (sol_m5 does)."""
    # macd_line>signal but hist<=0 and not rising -> falls through to against
    r = eth_5m_macd_score(_macd(hist=-0.01, rising=False), "LONG")
    assert r.score == -0.05  # against, not +0.02


# ── eth_15m_follow_score ─────────────────────────────────────────────────────

def test_eth_15m_strong_hist_tier():
    r = eth_15m_follow_score(_macd(hist=0.05, rising=True), "LONG", min_hist=0.03)
    assert r.score == 0.05


def test_eth_15m_against_penalty():
    r = eth_15m_follow_score(_macd(hist=-0.10, rising=False), "LONG", min_hist=0.03)
    assert r.score == -0.05


def test_eth_15m_below_min_hist_no_bonus():
    # hist=0.02 below min_hist=0.03 and not negative -> 0.0
    r = eth_15m_follow_score(_macd(hist=0.02, rising=True), "LONG", min_hist=0.03)
    assert r.score == 0.0


# ── parity: live wrappers delegate ───────────────────────────────────────────

def test_live_eth_macro_wrappers_delegate_to_core():
    from unittest.mock import MagicMock
    from src.strategies.eth_macro import ETHMacroStrategy

    cfg = {
        "strategies": {"eth_macro": {"eth_follow_15m_hist_min": 0.03}}
    }
    eth = ETHMacroStrategy(cfg, MagicMock(), MagicMock())

    mom = _mom("SPIKE_UP", in_window=True)
    score, _ = eth._btc_follow_5m_impulse_score(mom, "LONG")
    core = btc_follow_5m_impulse(mom, "LONG")
    assert score == core.score

    m5 = _macd(cross="BULLISH_CROSS")
    score, _ = eth._eth_5m_macd_score(m5, "LONG")
    assert score == eth_5m_macd_score(m5, "LONG").score

    m15 = _macd(hist=0.05, rising=True)
    score, _ = eth._eth_15m_follow_score(m15, "LONG")
    assert score == eth_15m_follow_score(m15, "LONG", min_hist=0.03).score
