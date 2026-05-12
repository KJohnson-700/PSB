"""Small probability-adjustment helpers shared by BTC 5m and 15m paths.

Extracted from the inline RSI/Sabre-tension blocks in bitcoin.py
(both 5m and 15m updown branches) and updown_engine._edge_5m_btc /
_edge_15m. The behaviour is identical across both windows for the 15m
flavor; the 5m flavor uses tighter weights (commented inline).
"""

from __future__ import annotations

from src.analysis.btc_price_service import TrendSabreResult


def rsi_4_level_adj_15m(rsi_14: float) -> float:
    """RSI mean-reversion adjustment for 15m updown. Pure function.

    >80   -> -0.03 (very overbought, supports SHORT)
    >65   -> -0.02 (overbought zone)
    <20   -> +0.03 (very oversold, supports LONG)
    <35   -> +0.02 (oversold zone)
    """
    if rsi_14 > 80:
        return -0.03
    if rsi_14 > 65:
        return -0.02
    if rsi_14 < 20:
        return 0.03
    if rsi_14 < 35:
        return 0.02
    return 0.0


def rsi_4_level_adj_5m(rsi_14: float) -> float:
    """RSI mean-reversion adjustment for 5m updown. Lighter weights than 15m
    because the 5m noise floor is higher."""
    if rsi_14 > 80:
        return -0.02
    if rsi_14 > 65:
        return -0.01
    if rsi_14 < 20:
        return 0.02
    if rsi_14 < 35:
        return 0.01
    return 0.0


def sabre_tension_adj(sabre: TrendSabreResult, allowed_side: str,
                      *, threshold: float = 2.0, magnitude: float = 0.02) -> float:
    """Mean-reversion penalty/bonus when Sabre tension is stretched.

    When |tension| > threshold ATR, apply +/- magnitude in the
    direction of mean reversion (positive tension = stretched UP = LONG
    penalty / SHORT bonus, and vice versa).
    """
    if sabre.tension_abs <= threshold:
        return 0.0
    if allowed_side == "LONG":
        return -magnitude if sabre.tension > 0 else magnitude
    return magnitude if sabre.tension > 0 else -magnitude
