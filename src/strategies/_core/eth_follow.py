"""ETH-follow scoring helpers: ETH 5m/15m MACD scoring + BTC follow signals.

Extracted from:
- EthMacroStrategy._btc_follow_5m_impulse_score
- EthMacroStrategy._eth_5m_macd_score
- EthMacroStrategy._eth_15m_follow_score
- updown_engine._eth_follow_btc_5m_impulse
- updown_engine._eth_follow_btc_15m_impulse_ok
- updown_engine._edge_5m_eth_follow_from_df / _edge_15m_eth_follow inline scoring

All matched byte-for-byte in logic across live and backtest. Now shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.analysis.btc_price_service import CandleMomentum, MACDResult


@dataclass(frozen=True)
class FollowScoreResult:
    score: float
    reasons: List[str] = field(default_factory=list)


def btc_follow_5m_impulse(
    momentum: CandleMomentum, allowed_side: str
) -> FollowScoreResult:
    """BTC 5m candle impulse used as a confirming signal for ETH follow entries.

    Aligned SPIKE: +0.06, aligned DRIFT: +0.04, against SPIKE/DRIFT: -0.05.
    Adds +0.02 when m5 is in the prediction window AND the impulse is positive.
    """
    direction = momentum.m5_direction
    reasons: List[str] = []
    score = 0.0

    if allowed_side == "LONG":
        if direction == "SPIKE_UP":
            score = 0.06
            reasons.append(f"BTC5m SPIKE_UP ({momentum.m5_move_pct:+.3f}%)")
        elif direction == "DRIFT_UP":
            score = 0.04
            reasons.append(f"BTC5m DRIFT_UP ({momentum.m5_move_pct:+.3f}%)")
        elif direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
            score = -0.05
            reasons.append(f"BTC5m against ({direction})")
    else:  # SHORT
        if direction == "SPIKE_DOWN":
            score = 0.06
            reasons.append(f"BTC5m SPIKE_DOWN ({momentum.m5_move_pct:+.3f}%)")
        elif direction == "DRIFT_DOWN":
            score = 0.04
            reasons.append(f"BTC5m DRIFT_DOWN ({momentum.m5_move_pct:+.3f}%)")
        elif direction in ("SPIKE_UP", "DRIFT_UP"):
            score = -0.05
            reasons.append(f"BTC5m against ({direction})")

    if momentum.m5_in_prediction_window and score > 0:
        score += 0.02
        reasons.append("BTC5m predict window")

    return FollowScoreResult(score=score, reasons=reasons)


def btc_follow_15m_impulse_ok(
    macd_15m: MACDResult, m15_direction: str, allowed_side: str, min_hist: float
) -> bool:
    """BTC 15m impulse confirmation (boolean) for ETH-follow gating.

    Passes when ANY of: matching crossover, histogram beyond min_hist with
    consistent rising/falling, OR m15 candle direction in matching tier.
    """
    if allowed_side == "LONG":
        return (
            macd_15m.crossover == "BULLISH_CROSS"
            or (macd_15m.histogram > min_hist and macd_15m.histogram_rising)
            or m15_direction in ("SPIKE_UP", "DRIFT_UP")
        )
    return (
        macd_15m.crossover == "BEARISH_CROSS"
        or (macd_15m.histogram < -min_hist and not macd_15m.histogram_rising)
        or m15_direction in ("SPIKE_DOWN", "DRIFT_DOWN")
    )


def eth_5m_macd_score(macd_5m: MACDResult, allowed_side: str) -> FollowScoreResult:
    """ETH 5m MACD scoring.

    Tiers (LONG; SHORT mirrors):
      bull cross           : +0.06
      hist > 0 and rising  : +0.04
      MACD > signal        : +0.02   (weak-bullish, matches sol_m5_macd_adj)
      bear cross or hist<0 : -0.05

    The 0.02 MACD>signal tier was missing prior to 2026-05-12, leaving
    the config knob `eth_follow_5m_min_adj: 0.02` (comment: "lowered
    from 0.04 — easier 5m entry in grindy tape") as a silent no-op:
    scores were always 0.04+ or 0/negative, so a 0.02 threshold filtered
    nothing additional. Result: ETH 5m backtests produced zero trades
    for windows where 5m was leaning bullish but not yet hist>0+rising.
    Adding the 0.02 tier honors the config's clear intent and mirrors
    SOL's scorer structure.
    """
    reasons: List[str] = []
    score = 0.0
    if allowed_side == "LONG":
        if macd_5m.crossover == "BULLISH_CROSS":
            score = 0.06
            reasons.append("ETH5m bull cross")
        elif macd_5m.histogram > 0 and macd_5m.histogram_rising:
            score = 0.04
            reasons.append("ETH5m green+rising")
        elif macd_5m.macd_line > macd_5m.signal_line:
            score = 0.02
            reasons.append("ETH5m MACD>signal")
        elif macd_5m.crossover == "BEARISH_CROSS" or macd_5m.histogram < 0:
            score = -0.05
            reasons.append("ETH5m against")
    else:
        if macd_5m.crossover == "BEARISH_CROSS":
            score = 0.06
            reasons.append("ETH5m bear cross")
        elif macd_5m.histogram < 0 and not macd_5m.histogram_rising:
            score = 0.04
            reasons.append("ETH5m red+falling")
        elif macd_5m.macd_line < macd_5m.signal_line:
            score = 0.02
            reasons.append("ETH5m MACD<signal")
        elif macd_5m.crossover == "BULLISH_CROSS" or macd_5m.histogram > 0:
            score = -0.05
            reasons.append("ETH5m against")
    return FollowScoreResult(score=score, reasons=reasons)


def eth_15m_follow_score(
    macd_15m: MACDResult, allowed_side: str, *, min_hist: float
) -> FollowScoreResult:
    """ETH 15m MACD scoring for follow entries."""
    reasons: List[str] = []
    score = 0.0
    if allowed_side == "LONG":
        if macd_15m.crossover == "BULLISH_CROSS":
            score = 0.06
            reasons.append("ETH15m bull cross")
        elif macd_15m.histogram >= min_hist and macd_15m.histogram_rising:
            score = 0.05
            reasons.append(f"ETH15m green+rising>{min_hist:.2f}")
        elif macd_15m.crossover == "BEARISH_CROSS" or macd_15m.histogram < 0:
            score = -0.05
            reasons.append("ETH15m against")
    else:
        if macd_15m.crossover == "BEARISH_CROSS":
            score = 0.06
            reasons.append("ETH15m bear cross")
        elif macd_15m.histogram <= -min_hist and not macd_15m.histogram_rising:
            score = 0.05
            reasons.append(f"ETH15m red+falling>{min_hist:.2f}")
        elif macd_15m.crossover == "BULLISH_CROSS" or macd_15m.histogram > 0:
            score = -0.05
            reasons.append("ETH15m against")
    return FollowScoreResult(score=score, reasons=reasons)
