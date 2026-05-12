"""Parity + behavior tests for the shared 15m LTF strength core.

Locks BitcoinStrategy._check_lower_tf_confirmation,
SolMacroStrategy._check_15m_confirmation, and both backtest wrappers to
identical output."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.analysis.btc_price_service import MACDResult
from src.strategies._core import (
    btc_ltf_strength_15m,
    passes_15m_iql,
    sol_ltf_strength_15m,
)


def _m(cross="NONE", hist=0.0, prev_hist=0.0, rising=False, above_signal=True) -> MACDResult:
    return MACDResult(
        macd_line=1.0 if above_signal else -1.0,
        signal_line=0.0,
        histogram=hist,
        prev_histogram=prev_hist,
        crossover=cross,
        histogram_rising=rising,
        above_zero=hist > 0,
    )


# ── btc_ltf_strength_15m ─────────────────────────────────────────────────────

def test_btc_long_bull_cross_alone_below_threshold():
    # Just a bull cross = 0.40, below threshold 0.50 -> not confirmed.
    # above_signal=False to isolate cross contribution.
    r = btc_ltf_strength_15m(_m(cross="BULLISH_CROSS", above_signal=False), "LONG")
    assert r.confirmed is False
    assert r.strength == 0.40


def test_btc_long_red_to_green_plus_above_signal_confirms():
    # red->green (+0.35) + MACD>signal (+0.15) = 0.50 -> confirmed
    r = btc_ltf_strength_15m(
        _m(hist=5.0, prev_hist=-3.0, rising=True, above_signal=True), "LONG"
    )
    assert r.confirmed is True
    assert abs(r.strength - 0.50) < 1e-9


def test_btc_short_bear_cross_plus_falling_confirms():
    # bear cross +0.40 + falling +0.20 + MACD<signal +0.15 = 0.75 -> confirmed
    r = btc_ltf_strength_15m(
        _m(cross="BEARISH_CROSS", hist=-2.0, prev_hist=-1.0,
           rising=False, above_signal=False), "SHORT"
    )
    assert r.confirmed is True
    assert abs(r.strength - 0.75) < 1e-9


# ── sol_ltf_strength_15m ─────────────────────────────────────────────────────

def test_sol_uses_different_weights_than_btc():
    """SOL: rising +0.15 vs BTC +0.20; MACD>signal +0.10 vs BTC +0.15."""
    m = _m(hist=4.0, prev_hist=2.0, rising=True, above_signal=True)
    btc = btc_ltf_strength_15m(m, "LONG")
    sol = sol_ltf_strength_15m(m, "LONG")
    # BTC: rising (+0.20) + above signal (+0.15) = 0.35
    # SOL: rising (+0.15) + above signal (+0.10) = 0.25
    assert abs(btc.strength - 0.35) < 1e-9
    assert abs(sol.strength - 0.25) < 1e-9


def test_sol_red_to_green_plus_macd_above_signal_below_threshold():
    # red->green +0.35 + MACD>signal +0.10 = 0.45 -> NOT confirmed (threshold 0.50)
    r = sol_ltf_strength_15m(
        _m(hist=4.0, prev_hist=-2.0, rising=True, above_signal=True), "LONG"
    )
    assert r.confirmed is False
    assert abs(r.strength - 0.45) < 1e-9


def test_sol_bull_cross_plus_red_to_green_confirms():
    r = sol_ltf_strength_15m(
        _m(cross="BULLISH_CROSS", hist=4.0, prev_hist=-2.0, rising=True), "LONG"
    )
    assert r.confirmed is True


# ── parity: live wrappers ↔ core ─────────────────────────────────────────────

def _btc_strategy():
    from src.strategies.bitcoin import BitcoinStrategy
    return BitcoinStrategy({"min_4h_hist_magnitude": 20.0}, MagicMock(), MagicMock())


def test_btc_live_wrapper_matches_core():
    btc = _btc_strategy()
    from src.analysis.btc_price_service import TechnicalAnalysis, TrendSabreResult

    cases = [
        _m(cross="BULLISH_CROSS", hist=5.0, prev_hist=-3.0, rising=True, above_signal=True),
        _m(cross="BEARISH_CROSS", hist=-5.0, prev_hist=2.0, rising=False, above_signal=False),
        _m(hist=1.0, prev_hist=0.5, rising=True, above_signal=False),
        _m(hist=-1.0, prev_hist=-0.5, rising=False, above_signal=True),
    ]
    for m in cases:
        ta = TechnicalAnalysis(
            current_price=75_000.0, macd_15m=m, trend_sabre=TrendSabreResult()
        )
        for side in ("LONG", "SHORT"):
            wrap_conf, wrap_str, _wrap_reasons = btc._check_lower_tf_confirmation(ta, side)
            core = btc_ltf_strength_15m(m, side)
            assert wrap_conf == core.confirmed, f"BTC parity drift on {side}: {m}"
            assert abs(wrap_str - core.strength) < 1e-9


def test_btc_backtest_wrapper_matches_core():
    from src.backtest.updown_engine import UpdownBacktestEngine
    from src.analysis.btc_price_service import TechnicalAnalysis, TrendSabreResult

    m = _m(cross="BULLISH_CROSS", hist=5.0, prev_hist=-3.0, rising=True, above_signal=True)
    ta = TechnicalAnalysis(
        current_price=75_000.0, macd_15m=m, trend_sabre=TrendSabreResult()
    )
    bt_conf, bt_str = UpdownBacktestEngine._ltf_strength(ta, "LONG")
    core = btc_ltf_strength_15m(m, "LONG")
    assert bt_conf == core.confirmed
    assert abs(bt_str - core.strength) < 1e-9


def test_sol_backtest_wrapper_matches_core():
    from src.backtest.updown_engine import UpdownBacktestEngine

    m = _m(cross="BULLISH_CROSS", hist=4.0, prev_hist=-2.0, rising=True, above_signal=True)
    bt_conf, bt_str = UpdownBacktestEngine._sol_ltf_strength_m(m, "LONG")
    core = sol_ltf_strength_15m(m, "LONG")
    assert bt_conf == core.confirmed
    assert abs(bt_str - core.strength) < 1e-9


# ── passes_15m_iql ───────────────────────────────────────────────────────────

def test_iql_passes_when_already_confirmed():
    # Bull cross + red-to-green + above signal -> SOL strength 0.85, confirmed
    m = _m(cross="BULLISH_CROSS", hist=3.0, prev_hist=-2.0, rising=True, above_signal=True)
    assert passes_15m_iql(m, "LONG", hist_floor=0.03) is True


def test_iql_relaxed_long_via_hist_floor():
    # Not confirmed (just rising +0.15) but histogram >= floor with rising -> pass
    m = _m(hist=0.05, prev_hist=0.02, rising=True, above_signal=False)
    assert passes_15m_iql(m, "LONG", hist_floor=0.03) is True


def test_iql_relaxed_long_via_cross_alone():
    m = _m(cross="BULLISH_CROSS", hist=-0.01, prev_hist=-0.05, rising=False, above_signal=False)
    # Bull cross alone in relaxed branch should pass
    assert passes_15m_iql(m, "LONG", hist_floor=0.03) is True


def test_iql_rejects_when_hist_below_floor():
    m = _m(hist=0.01, prev_hist=0.005, rising=True, above_signal=False)
    assert passes_15m_iql(m, "LONG", hist_floor=0.03) is False


def test_iql_short_mirror():
    m = _m(cross="BEARISH_CROSS", hist=-0.05, prev_hist=-0.02, rising=False, above_signal=False)
    assert passes_15m_iql(m, "SHORT", hist_floor=0.03) is True


def test_iql_live_backtest_parity():
    from src.backtest.updown_engine import UpdownBacktestEngine

    cases = [
        _m(cross="BULLISH_CROSS", hist=3.0, prev_hist=-2.0, rising=True, above_signal=True),
        _m(hist=0.05, prev_hist=0.02, rising=True, above_signal=False),
        _m(hist=0.01, prev_hist=0.005, rising=True, above_signal=False),
        _m(cross="BEARISH_CROSS", hist=-0.05, prev_hist=-0.02, rising=False, above_signal=False),
    ]
    for m in cases:
        for side in ("LONG", "SHORT"):
            core_r = passes_15m_iql(m, side, hist_floor=0.03)
            back_r = UpdownBacktestEngine._passes_15m_iql_macd(m, side, 0.03)
            assert core_r == back_r


def test_btc_backtest_now_matches_live_threshold():
    """Drift fix: pre-refactor backtest used threshold 0.35 for BTC, vs live 0.50.
    A signal that scored 0.40 used to confirm in backtest but not in live."""
    from src.backtest.updown_engine import UpdownBacktestEngine
    from src.analysis.btc_price_service import TechnicalAnalysis, TrendSabreResult

    # Pure bull cross only (macd<signal so +0.15 doesn't fire) -> 0.40 strength
    m = _m(cross="BULLISH_CROSS", above_signal=False)
    ta = TechnicalAnalysis(current_price=75_000.0, macd_15m=m, trend_sabre=TrendSabreResult())
    bt_conf, bt_str = UpdownBacktestEngine._ltf_strength(ta, "LONG")
    assert bt_str == 0.40
    # Post-refactor: not confirmed (matches live). Pre-refactor: would have been True.
    assert bt_conf is False
