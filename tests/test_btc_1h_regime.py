"""Tests for BTC 1H SMA(20) regime classification."""
from src.analysis.btc_1h_regime import classify_btc_1h_sma_regime


def test_regime_range_near_sma():
    band = 0.001
    sma = 100_000.0
    assert classify_btc_1h_sma_regime(sma * (1 + 0.0005), sma, band) == "RANGE"
    assert classify_btc_1h_sma_regime(sma * (1 - 0.0005), sma, band) == "RANGE"


def test_regime_bull_bear_outside_band():
    band = 0.001
    sma = 100_000.0
    assert classify_btc_1h_sma_regime(sma * (1 + 0.002), sma, band) == "BULL"
    assert classify_btc_1h_sma_regime(sma * (1 - 0.002), sma, band) == "BEAR"


def test_regime_invalid_falls_back_range():
    assert classify_btc_1h_sma_regime(100.0, 0.0, 0.001) == "RANGE"
