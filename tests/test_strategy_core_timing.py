"""Tests for the 15m timing-bonus core helper."""

from src.analysis.btc_price_service import CandleMomentum
from src.strategies._core import btc_15m_timing_bonus


def _mom(*, m15="", m15_pct=0.0, m5="", m5_pct=0.0,
         m15_window=False, m5_window=False) -> CandleMomentum:
    return CandleMomentum(
        m15_direction=m15,
        m15_move_pct=m15_pct,
        m5_direction=m5,
        m5_move_pct=m5_pct,
        m15_in_prediction_window=m15_window,
        m5_in_prediction_window=m5_window,
    )


def test_long_m15_spike_up_full_bonus():
    r = btc_15m_timing_bonus(_mom(m15="SPIKE_UP"), "LONG")
    assert r.bonus == 0.08


def test_long_m15_drift_up_partial_bonus():
    r = btc_15m_timing_bonus(_mom(m15="DRIFT_UP"), "LONG")
    assert r.bonus == 0.04


def test_long_m15_against_penalty():
    r = btc_15m_timing_bonus(_mom(m15="SPIKE_DOWN"), "LONG")
    assert r.bonus == -0.05


def test_long_combined_m15_m5_bonuses_stack():
    r = btc_15m_timing_bonus(
        _mom(m15="SPIKE_UP", m5="DRIFT_UP", m15_window=True, m5_window=True),
        "LONG",
    )
    # 0.08 + 0.02 + 0.03 + 0.02 = 0.15
    assert abs(r.bonus - 0.15) < 1e-9
    assert "15m predict window" in r.reasons
    assert "5m predict window" in r.reasons


def test_short_mirrors_long():
    r = btc_15m_timing_bonus(_mom(m15="SPIKE_DOWN", m5="DRIFT_DOWN"), "SHORT")
    assert r.bonus == 0.08 + 0.02


def test_live_check_timing_delegates():
    """BitcoinStrategy._check_timing must return the same bonus as the core."""
    from unittest.mock import MagicMock
    from src.analysis.btc_price_service import TechnicalAnalysis, TrendSabreResult
    from src.strategies.bitcoin import BitcoinStrategy

    btc = BitcoinStrategy({"min_4h_hist_magnitude": 20.0}, MagicMock(), MagicMock())
    mom = _mom(m15="SPIKE_UP", m5="DRIFT_UP", m15_window=True)
    ta = TechnicalAnalysis(
        current_price=75_000.0,
        candle_momentum=mom,
        trend_sabre=TrendSabreResult(),
    )
    bonus, reasons = btc._check_timing(ta, "LONG")
    core = btc_15m_timing_bonus(mom, "LONG")
    assert bonus == core.bonus
    assert reasons == core.reasons
