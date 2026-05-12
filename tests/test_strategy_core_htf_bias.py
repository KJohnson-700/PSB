"""Parity + behavior tests for the shared BTC HTF-bias core."""

from __future__ import annotations

import pytest

from src.analysis.btc_price_service import (
    MACDResult,
    TechnicalAnalysis,
    TrendSabreResult,
)
from src.strategies._core import btc_htf_bias


def _ta(*, trend: int, ma_value: float, price: float,
        macd_hist: float, above_zero: bool,
        crossover: str = "NONE", hist_rising: bool = False) -> TechnicalAnalysis:
    return TechnicalAnalysis(
        current_price=price,
        rsi_14=50.0,
        macd_4h=MACDResult(
            macd_line=macd_hist,
            signal_line=0.0,
            histogram=macd_hist,
            prev_histogram=0.0,
            crossover=crossover,
            histogram_rising=hist_rising,
            above_zero=above_zero,
        ),
        macd_1h=MACDResult(),
        macd_15m=MACDResult(),
        trend_sabre=TrendSabreResult(
            trend=trend,
            ma_value=ma_value,
        ),
    )


def test_unanimous_bull_passes_conviction():
    ta = _ta(trend=1, ma_value=70_000, price=75_000, macd_hist=120.0, above_zero=True)
    r = btc_htf_bias(ta, min_hist_magnitude=20.0)
    assert r.bias == "BULLISH"
    assert r.raw_vote_bias == "BULLISH"
    assert r.downgraded_to_neutral is False


def test_unanimous_bear_passes_conviction():
    ta = _ta(trend=-1, ma_value=80_000, price=75_000, macd_hist=-120.0, above_zero=False)
    r = btc_htf_bias(ta, min_hist_magnitude=20.0)
    assert r.bias == "BEARISH"


def test_two_vote_bull_below_hist_floor_downgrades():
    # Sabre + price-above-MA vote bull; MACD weak -> 2/3 bull but hist too small.
    ta = _ta(trend=1, ma_value=70_000, price=75_000, macd_hist=5.0, above_zero=False)
    r = btc_htf_bias(ta, min_hist_magnitude=20.0)
    assert r.bias == "NEUTRAL"
    assert r.raw_vote_bias == "BULLISH"
    assert r.downgraded_to_neutral is True


def test_recovery_signal_gives_bull_vote_below_zero():
    # MACD below zero but histogram positive -> recovery vote bull.
    ta = _ta(trend=1, ma_value=70_000, price=75_000, macd_hist=80.0, above_zero=False)
    r = btc_htf_bias(ta, min_hist_magnitude=20.0)
    assert r.bias == "BULLISH"


def test_early_bear_cross_dominates():
    # Sabre+price bull, but BEARISH_CROSS not rising -> MACD votes bear, 2/3 bull still wins.
    ta = _ta(trend=1, ma_value=70_000, price=75_000, macd_hist=-50.0, above_zero=False,
             crossover="BEARISH_CROSS", hist_rising=False)
    r = btc_htf_bias(ta, min_hist_magnitude=20.0)
    assert r.bias == "BULLISH"  # 2 bull votes win conviction
    # Flip price/sabre to bear and the bear cross seals it
    ta2 = _ta(trend=-1, ma_value=80_000, price=75_000, macd_hist=-50.0, above_zero=False,
              crossover="BEARISH_CROSS", hist_rising=False)
    assert btc_htf_bias(ta2, min_hist_magnitude=20.0).bias == "BEARISH"


def test_split_vote_returns_neutral_without_downgrade():
    # Trend +1 (bull), price < MA (bear), MACD above zero (bull) -> 2 bull, 1 bear -> BULLISH
    ta = _ta(trend=1, ma_value=80_000, price=75_000, macd_hist=50.0, above_zero=True)
    assert btc_htf_bias(ta, min_hist_magnitude=20.0).bias == "BULLISH"
    # True 1-1-1 split (sabre neutral) -> NEUTRAL with no downgrade flag
    ta_split = _ta(trend=0, ma_value=80_000, price=80_000, macd_hist=50.0, above_zero=True)
    r = btc_htf_bias(ta_split, min_hist_magnitude=20.0)
    assert r.bias == "NEUTRAL"
    assert r.downgraded_to_neutral is False
    assert r.raw_vote_bias == "NEUTRAL"


def test_live_and_backtest_callers_match():
    """The wrappers in BitcoinStrategy and UpdownEngine must return identical
    bias strings for the same TA. This is the parity invariant."""
    from unittest.mock import MagicMock
    from src.strategies.bitcoin import BitcoinStrategy
    from src.backtest.updown_engine import UpdownBacktestEngine

    # Minimal mocked deps — we only exercise _get_higher_tf_bias / _get_htf_bias.
    cfg = {"min_4h_hist_magnitude": 20.0}
    btc = BitcoinStrategy(cfg, MagicMock(), MagicMock())

    cases = [
        _ta(trend=1, ma_value=70_000, price=75_000, macd_hist=120.0, above_zero=True),
        _ta(trend=-1, ma_value=80_000, price=75_000, macd_hist=-120.0, above_zero=False),
        _ta(trend=1, ma_value=70_000, price=75_000, macd_hist=5.0, above_zero=False),
        _ta(trend=1, ma_value=70_000, price=75_000, macd_hist=80.0, above_zero=False),
        _ta(trend=0, ma_value=80_000, price=80_000, macd_hist=50.0, above_zero=True),
    ]
    for ta in cases:
        live = btc._get_higher_tf_bias(ta)
        back = UpdownBacktestEngine._get_htf_bias(ta, min_hist=20.0)
        assert live == back, f"parity drift: live={live} back={back}"
