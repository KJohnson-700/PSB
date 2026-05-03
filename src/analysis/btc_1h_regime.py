"""BTC 1H vs SMA(20) regime buckets for macro alt strategies.

BULL / RANGE / BEAR partition spot relative to a neutral band around SMA(20).
Used to scale min_edge (tighter = higher bar) and position size."""
from __future__ import annotations

from typing import Literal

Regime = Literal["BULL", "RANGE", "BEAR"]

# Defaults aligned with operator table (multipliers on base min_edge / Kelly sizing).
DEFAULT_MIN_EDGE_MULT: dict[str, float] = {"BULL": 1.0, "RANGE": 1.25, "BEAR": 1.40}
DEFAULT_SIZE_MULT: dict[str, float] = {"BULL": 1.0, "RANGE": 0.7, "BEAR": 0.5}


def classify_btc_1h_sma_regime(
    price: float,
    sma_20: float,
    range_band_pct: float,
) -> Regime:
    """Classify regime from last 1H close vs SMA(20).

    RANGE = price within ±range_band_pct of SMA (chop zone).
    BULL / BEAR = outside that band, by side.
    """
    if sma_20 <= 0 or price <= 0:
        return "RANGE"
    dist_pct = (price - sma_20) / sma_20
    if abs(dist_pct) <= range_band_pct:
        return "RANGE"
    return "BULL" if dist_pct > range_band_pct else "BEAR"


def regime_price(ta) -> float:
    """Spot price for regime: prefer explicit 1H close when present."""
    px = float(getattr(ta, "btc_1h_close", 0.0) or 0.0)
    if px > 0:
        return px
    return float(getattr(ta, "current_price", 0.0) or 0.0)
