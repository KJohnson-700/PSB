"""Tests for BTC 5m HTF boost and 4H/1H histogram gate."""

from src.analysis.btc_price_service import MACDResult, TrendSabreResult
from src.strategies._core import btc_5m_4h_1h_hist_gate, btc_5m_htf_boost


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
