"""Tests for BTC 5m HTF boost and 4H/1H histogram gate."""

from src.analysis.btc_price_service import MACDResult, TrendSabreResult
from src.strategies._core import (
    btc_5m_4h_1h_hist_gate,
    btc_5m_htf_boost,
    btc_15m_htf_boost,
)


def _sabre(trend: int) -> TrendSabreResult:
    return TrendSabreResult(trend=trend)


def _macd(*, hist_rising: bool, above_zero: bool = False) -> MACDResult:
    return MACDResult(
        macd_line=0.0, signal_line=0.0,
        histogram=1.0 if above_zero else -1.0,
        prev_histogram=0.0,
        crossover="NONE",
        histogram_rising=hist_rising,
        above_zero=above_zero,
    )


# ── htf boost ────────────────────────────────────────────────────────────────

def test_strong_bull():
    assert btc_5m_htf_boost(_sabre(1), _macd(hist_rising=True, above_zero=True)) == 0.04


def test_partial_bull_sabre_only():
    assert btc_5m_htf_boost(_sabre(1), _macd(hist_rising=False, above_zero=False)) == 0.02


def test_partial_bull_macd_only():
    assert btc_5m_htf_boost(_sabre(0), _macd(hist_rising=True, above_zero=True)) == 0.02


def test_strong_bear():
    assert btc_5m_htf_boost(_sabre(-1), _macd(hist_rising=False, above_zero=False)) == -0.04


def test_partial_bear_default():
    # Pure neutral collapses to partial bear (matches live)
    assert btc_5m_htf_boost(_sabre(0), _macd(hist_rising=False, above_zero=False)) == -0.02


# ── 4H/1H histogram gate ─────────────────────────────────────────────────────

def test_gate_long_4h_rising_passes_primary():
    r = btc_5m_4h_1h_hist_gate(
        _macd(hist_rising=True), _macd(hist_rising=False), "LONG"
    )
    assert r.allowed and not r.fallback_used


def test_gate_long_4h_falling_1h_rising_passes_via_fallback():
    """Drift fix: pre-refactor backtest rejected this; live allows it."""
    r = btc_5m_4h_1h_hist_gate(
        _macd(hist_rising=False), _macd(hist_rising=True), "LONG"
    )
    assert r.allowed and r.fallback_used


def test_gate_long_both_falling_rejects():
    r = btc_5m_4h_1h_hist_gate(
        _macd(hist_rising=False), _macd(hist_rising=False), "LONG"
    )
    assert not r.allowed
    assert r.rejection_reason == "hist_gate_5m_long_reject"


def test_gate_short_4h_falling_passes():
    r = btc_5m_4h_1h_hist_gate(
        _macd(hist_rising=False), _macd(hist_rising=True), "SHORT"
    )
    assert r.allowed and not r.fallback_used


def test_gate_short_4h_rising_1h_falling_passes_via_fallback():
    r = btc_5m_4h_1h_hist_gate(
        _macd(hist_rising=True), _macd(hist_rising=False), "SHORT"
    )
    assert r.allowed and r.fallback_used


def test_gate_short_both_rising_rejects():
    r = btc_5m_4h_1h_hist_gate(
        _macd(hist_rising=True), _macd(hist_rising=True), "SHORT"
    )
    assert not r.allowed
    assert r.rejection_reason == "hist_gate_5m_short_reject"


# ── btc_15m_htf_boost ────────────────────────────────────────────────────────

def _sabre_t(trend: int, ma: float = 100.0) -> TrendSabreResult:
    return TrendSabreResult(trend=trend, ma_value=ma)


def test_15m_three_of_three_bull():
    # sabre=1, price>MA, MACD above_zero
    assert btc_15m_htf_boost(_sabre_t(1, 100), _macd(hist_rising=True, above_zero=True),
                             price=101, htf_bias="BULLISH") == 0.08


def test_15m_three_of_three_bear():
    assert btc_15m_htf_boost(_sabre_t(-1, 100),
                             _macd(hist_rising=False, above_zero=False),
                             price=99, htf_bias="BEARISH") == -0.08


def test_15m_two_of_three_bull():
    # sabre=1, price<MA, MACD above_zero -> +0.03 from lookup, BULLISH no further floor
    assert btc_15m_htf_boost(_sabre_t(1, 100),
                             _macd(hist_rising=True, above_zero=True),
                             price=99, htf_bias="BULLISH") == 0.03


def test_15m_mixed_no_floor_neutral():
    # Pure mixed votes, HTF=NEUTRAL -> 0.0 (no floor)
    assert btc_15m_htf_boost(_sabre_t(0, 100),
                             _macd(hist_rising=False, above_zero=False),
                             price=100, htf_bias="NEUTRAL") == 0.0


def test_15m_recovery_bull_vote_floor_drift_fix():
    """Drift fix: pre-refactor live had no floor here, so a BULLISH-by-recovery
    HTF vote could produce a NEGATIVE htf_boost. Floor +0.03 now applied."""
    # sabre=-1 (bear vote), price<MA (bear), but HTF=BULLISH via recovery case
    # Raw lookup: branch 2 fails (not all 3 bear, above_zero=False conflicts...)
    # Actually with sabre=-1 and not above_zero, branch 4 fires -> -0.03
    boost = btc_15m_htf_boost(
        _sabre_t(-1, 100),
        _macd(hist_rising=True, above_zero=False),  # below zero, hist > 0 (recovery)
        price=99,
        htf_bias="BULLISH",
    )
    # Without floor: -0.03. With floor: +0.03.
    assert boost == 0.03


def test_15m_recovery_bear_vote_floor():
    boost = btc_15m_htf_boost(
        _sabre_t(1, 100),
        _macd(hist_rising=False, above_zero=True),
        price=101,
        htf_bias="BEARISH",
    )
    # Raw lookup with sabre=1 and above_zero=True -> +0.08; floor pulls to -0.03
    assert boost == -0.03


def test_backtest_btc_5m_edge_uses_1h_fallback():
    """Pre-refactor _edge_5m_btc returned (0.0, 0.0) on 4H-falling LONG.
    Post-refactor it allows the entry via the 1H fallback."""
    from src.analysis.btc_price_service import TechnicalAnalysis
    from src.backtest.updown_engine import UpdownBacktestEngine
    import pandas as pd

    ta = TechnicalAnalysis(
        current_price=75_000.0,
        rsi_14=50.0,
        macd_4h=_macd(hist_rising=False, above_zero=True),
        macd_1h=_macd(hist_rising=True, above_zero=True),
        macd_15m=MACDResult(),
        trend_sabre=_sabre(1),
    )
    eng = UpdownBacktestEngine({"strategies": {}, "backtest": {}})
    edge, conf = eng._edge_5m_btc(
        ta, "LONG", pd.DataFrame(),
        pd.Timestamp("2026-01-01", tz="UTC"),
        ta.macd_4h,
    )
    assert (edge, conf) != (0.0, 0.0), "1H fallback should allow this entry"
