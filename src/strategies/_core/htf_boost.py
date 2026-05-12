"""HTF (4H) probability boosts and momentum gates for BTC updown markets.

Extracted from:
- BitcoinStrategy 5m updown HTF boost (bitcoin.py L1043-L1051) and 4H/1H
  histogram gate (L1058-L1081).
- src/backtest/updown_engine._edge_5m_btc HTF boost (L1185-L1188) and 4H
  histogram gate (L1192-L1193).

The backtest 4H gate was a hard reject; live has a 1H fallback that
allows entries when 4H is decelerating but 1H is building ("local
momentum recovery within larger trend structure"). This extraction
unifies them — backtest now matches live.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.btc_price_service import MACDResult, TrendSabreResult


def btc_5m_htf_boost(sabre: TrendSabreResult, macd_4h: MACDResult) -> float:
    """4-branch probability boost based on 4H Sabre trend and MACD position.

    Returns:
      +0.04 (strong bull) / +0.02 (partial bull) /
      -0.04 (strong bear) / -0.02 (partial bear, also covers fully neutral)
    """
    above_zero = macd_4h.above_zero
    if sabre.trend == 1 and above_zero:
        return 0.04
    if sabre.trend == 1 or above_zero:
        return 0.02
    if sabre.trend == -1 and not above_zero:
        return -0.04
    return -0.02


@dataclass(frozen=True)
class HistGateResult:
    allowed: bool         # True if entry passes the gate
    fallback_used: bool   # True if 1H momentum-recovery fallback saved the entry
    rejection_reason: str = ""  # e.g. "hist_gate_5m_long_reject" — empty when allowed


def btc_5m_4h_1h_hist_gate(
    macd_4h: MACDResult, macd_1h: MACDResult, allowed_side: str
) -> HistGateResult:
    """4H histogram gate with 1H momentum-recovery fallback.

    Primary: 4H histogram must be building in the trade direction.
    Fallback: if 4H is decelerating but 1H is building (LONG) / falling (SHORT),
              allow the entry — "local momentum recovery within larger trend".
    """
    if allowed_side == "LONG":
        if macd_4h.histogram_rising:
            return HistGateResult(allowed=True, fallback_used=False)
        if macd_1h.histogram_rising:
            return HistGateResult(allowed=True, fallback_used=True)
        return HistGateResult(
            allowed=False, fallback_used=False,
            rejection_reason="hist_gate_5m_long_reject",
        )
    # SHORT
    if not macd_4h.histogram_rising:
        return HistGateResult(allowed=True, fallback_used=False)
    if not macd_1h.histogram_rising:
        return HistGateResult(allowed=True, fallback_used=True)
    return HistGateResult(
        allowed=False, fallback_used=False,
        rejection_reason="hist_gate_5m_short_reject",
    )
