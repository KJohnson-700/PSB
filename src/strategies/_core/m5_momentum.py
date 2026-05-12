"""5m signal scoring.

- score_m5_direction(): BTC 5m path, based on CandleMomentum.m5_direction tier
  (SPIKE/DRIFT/LEAN).
- sol_m5_macd_adj():    SOL alt-5m path, based on 5m MACD crossover + histogram
  + macd-vs-signal. Live/backtest share both.

Extracted from:
- BitcoinStrategy 5m updown path
- src/backtest/updown_engine._calc_m5_momentum (scoring half only)

Producer tiers (btc_price_service.calc_candle_momentum, mirrored in
updown_engine._replay_candle_momentum and _calc_m5_momentum):
- |move| > 0.08% -> SPIKE_UP / SPIKE_DOWN
- |move| > 0.03% -> DRIFT_UP / DRIFT_DOWN
- |move| > 0.01% -> LEAN_UP  / LEAN_DOWN
- else           -> empty (treated as NONE)
"""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.btc_price_service import MACDResult


@dataclass(frozen=True)
class SolM5MacdResult:
    adj: float
    reason: str   # "" when nothing fired


def sol_m5_macd_adj(macd_5m: MACDResult, allowed_side: str) -> SolM5MacdResult:
    """SOL/alt 5m MACD-based probability adjustment.

    Tiered by signal strength:
      bull cross    : +0.06
      hist green+rising / red+falling (aligned): +0.04
      MACD vs signal aligned : +0.02
      against (cross or histogram opposite): -0.04
    """
    if allowed_side == "LONG":
        if macd_5m.crossover == "BULLISH_CROSS":
            return SolM5MacdResult(0.06, "5m MACD bull cross")
        if macd_5m.histogram_rising and macd_5m.histogram > 0:
            return SolM5MacdResult(0.04, "5m hist green+rising")
        if macd_5m.macd_line > macd_5m.signal_line:
            return SolM5MacdResult(0.02, "5m MACD>signal")
        if macd_5m.crossover == "BEARISH_CROSS" or macd_5m.histogram < 0:
            return SolM5MacdResult(-0.04, f"5m against ({macd_5m.crossover})")
        return SolM5MacdResult(0.0, "")
    # SHORT
    if macd_5m.crossover == "BEARISH_CROSS":
        return SolM5MacdResult(0.06, "5m MACD bear cross")
    if not macd_5m.histogram_rising and macd_5m.histogram < 0:
        return SolM5MacdResult(0.04, "5m hist red+falling")
    if macd_5m.macd_line < macd_5m.signal_line:
        return SolM5MacdResult(0.02, "5m MACD<signal")
    if macd_5m.crossover == "BULLISH_CROSS" or macd_5m.histogram > 0:
        return SolM5MacdResult(-0.04, f"5m against ({macd_5m.crossover})")
    return SolM5MacdResult(0.0, "")


def score_m5_direction(m5_direction: str, allowed_side: str) -> float:
    """Return the probability adjustment for a 5m candle direction.

    Live LONG: SPIKE_UP +0.06, DRIFT_UP +0.04, LEAN_UP +0.01,
               (SPIKE|DRIFT)_DOWN -0.04, LEAN_DOWN -0.01.
    SHORT: mirror, signs flipped wrt the side.
    """
    if allowed_side == "LONG":
        if m5_direction == "SPIKE_UP":
            return 0.06
        if m5_direction == "DRIFT_UP":
            return 0.04
        if m5_direction == "LEAN_UP":
            return 0.01
        if m5_direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
            return -0.04
        if m5_direction == "LEAN_DOWN":
            return -0.01
        return 0.0
    # SHORT
    if m5_direction == "SPIKE_DOWN":
        return 0.06
    if m5_direction == "DRIFT_DOWN":
        return 0.04
    if m5_direction == "LEAN_DOWN":
        return 0.01
    if m5_direction in ("SPIKE_UP", "DRIFT_UP"):
        return -0.04
    if m5_direction == "LEAN_UP":
        return -0.01
    return 0.0
