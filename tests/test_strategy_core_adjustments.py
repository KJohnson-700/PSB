"""Tests for the small probability-adjustment helpers."""

from src.analysis.btc_price_service import TrendSabreResult
from src.strategies._core import (
    rsi_4_level_adj_5m,
    rsi_4_level_adj_15m,
    sabre_tension_adj,
)


# ── RSI 15m ──────────────────────────────────────────────────────────────────

def test_rsi_15m_very_overbought():
    assert rsi_4_level_adj_15m(82.0) == -0.03


def test_rsi_15m_overbought():
    assert rsi_4_level_adj_15m(70.0) == -0.02


def test_rsi_15m_neutral():
    assert rsi_4_level_adj_15m(50.0) == 0.0


def test_rsi_15m_oversold():
    assert rsi_4_level_adj_15m(30.0) == 0.02


def test_rsi_15m_very_oversold():
    assert rsi_4_level_adj_15m(18.0) == 0.03


def test_rsi_15m_boundary_65_no_adj():
    # >65 fires, ==65 does not
    assert rsi_4_level_adj_15m(65.0) == 0.0
    assert rsi_4_level_adj_15m(65.5) == -0.02


# ── RSI 5m (lighter weights) ─────────────────────────────────────────────────

def test_rsi_5m_uses_lighter_weights():
    assert rsi_4_level_adj_5m(82.0) == -0.02
    assert rsi_4_level_adj_5m(70.0) == -0.01
    assert rsi_4_level_adj_5m(18.0) == 0.02
    assert rsi_4_level_adj_5m(30.0) == 0.01


# ── Sabre tension ────────────────────────────────────────────────────────────

def _sabre(tension: float) -> TrendSabreResult:
    return TrendSabreResult(tension=tension, tension_abs=abs(tension))


def test_sabre_tension_under_threshold_zero():
    assert sabre_tension_adj(_sabre(1.5), "LONG") == 0.0


def test_sabre_tension_long_overstretched_penalty():
    assert sabre_tension_adj(_sabre(2.5), "LONG") == -0.02


def test_sabre_tension_long_overcompressed_bonus():
    assert sabre_tension_adj(_sabre(-2.5), "LONG") == 0.02


def test_sabre_tension_short_mirror():
    assert sabre_tension_adj(_sabre(2.5), "SHORT") == 0.02
    assert sabre_tension_adj(_sabre(-2.5), "SHORT") == -0.02
