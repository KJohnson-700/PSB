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


def btc_15m_htf_boost(
    sabre: TrendSabreResult,
    macd_4h: MACDResult,
    price: float,
    htf_bias: str,
) -> float:
    """Graduated 3-vote HTF boost for BTC 15m updown markets.

    Vote system (Sabre dir, price vs Sabre MA, 4H MACD above zero):
      3/3 aligned bull -> +0.08
      3/3 aligned bear -> -0.08
      2/3 bull (sabre=1 AND above_zero)   -> +0.03
      2/3 bear (sabre=-1 AND not above0)  -> -0.03
      else (mixed)                        ->  0.00

    Floor: if the HTF vote (btc_htf_bias) decided BULLISH via the
    early_bull / recovery cases (sabre=-1 with hist>0 but below zero,
    say), the raw vote-system lookup above may return 0 or a wrong-sign
    value — contradicting the HTF decision. Floor +0.03 / -0.03 so the
    edge calc respects the HTF vote.

    NB: pre-refactor only the backtest applied this floor; live BTC 15m
    had a latent inconsistency where a BULLISH-by-recovery vote could
    receive a negative htf_boost. Floor is now applied in both.
    """
    price_above_ma = price > sabre.ma_value
    if sabre.trend == 1 and price_above_ma and macd_4h.above_zero:
        htf_boost = 0.08
    elif sabre.trend == -1 and not price_above_ma and not macd_4h.above_zero:
        htf_boost = -0.08
    elif sabre.trend == 1 and macd_4h.above_zero:
        htf_boost = 0.03
    elif sabre.trend == -1 and not macd_4h.above_zero:
        htf_boost = -0.03
    else:
        htf_boost = 0.0

    if htf_bias == "BULLISH" and htf_boost < 0.03:
        htf_boost = 0.03
    elif htf_bias == "BEARISH" and htf_boost > -0.03:
        htf_boost = -0.03

    return htf_boost


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
