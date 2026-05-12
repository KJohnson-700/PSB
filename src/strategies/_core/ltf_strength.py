"""15m MACD lower-timeframe confirmation strength. Extracted from:

- BitcoinStrategy._check_lower_tf_confirmation  (BTC weights)
- SolMacroStrategy._check_15m_confirmation      (SOL weights, shared by ETH/XRP/HYPE)
- src/backtest/updown_engine._ltf_strength      (was BTC-like but with threshold 0.35 — DRIFT)
- src/backtest/updown_engine._sol_ltf_strength_m

The threshold for both BTC and SOL is 0.50 in live. The backtest BTC variant
previously used 0.35, accepting more 15m entries than live ever would; this
extraction unifies them.

Weights:
- BTC: cross +0.40, hist red↔green +0.35 / just rising-and-bigger +0.20,
       MACD-vs-signal +0.15
- SOL: cross +0.40, hist red↔green +0.35 / just rising +0.15,
       MACD-vs-signal +0.10
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.analysis.btc_price_service import MACDResult


@dataclass(frozen=True)
class LtfStrengthResult:
    confirmed: bool
    strength: float           # 0.0 - 1.0 (clamped)
    reasons: List[str] = field(default_factory=list)


_LTF_CONFIRM_THRESHOLD = 0.50


def btc_ltf_strength_15m(macd_15m: MACDResult, allowed_side: str) -> LtfStrengthResult:
    """BTC weights & threshold. Matches BitcoinStrategy._check_lower_tf_confirmation."""
    reasons: List[str] = []
    s = 0.0
    if allowed_side == "LONG":
        if macd_15m.crossover == "BULLISH_CROSS":
            s += 0.40
            reasons.append("15m MACD bull cross")
        if macd_15m.histogram_rising and macd_15m.histogram > macd_15m.prev_histogram:
            if macd_15m.prev_histogram < 0 and macd_15m.histogram > 0:
                s += 0.35
                reasons.append("15m hist red->green")
            else:
                s += 0.20
                reasons.append("15m hist rising")
        if macd_15m.macd_line > macd_15m.signal_line:
            s += 0.15
            reasons.append("15m MACD>signal")
    elif allowed_side == "SHORT":
        if macd_15m.crossover == "BEARISH_CROSS":
            s += 0.40
            reasons.append("15m MACD bear cross")
        if not macd_15m.histogram_rising and macd_15m.histogram < macd_15m.prev_histogram:
            if macd_15m.prev_histogram > 0 and macd_15m.histogram < 0:
                s += 0.35
                reasons.append("15m hist green->red")
            else:
                s += 0.20
                reasons.append("15m hist falling")
        if macd_15m.macd_line < macd_15m.signal_line:
            s += 0.15
            reasons.append("15m MACD<signal")
    return LtfStrengthResult(
        confirmed=s >= _LTF_CONFIRM_THRESHOLD,
        strength=min(1.0, s),
        reasons=reasons,
    )


def passes_15m_iql_relaxed_rule(
    macd_15m: MACDResult, allowed_side: str, hist_floor: float
) -> bool:
    """Relaxed early-entry rule used when the 15m hasn't reached "confirmed" strength yet.

    Passes when EITHER matching crossover fires, OR histogram is beyond the floor in
    the matching direction with consistent histogram_rising.
    """
    hist = float(macd_15m.histogram)
    if allowed_side == "LONG":
        return macd_15m.crossover == "BULLISH_CROSS" or (
            hist >= hist_floor and macd_15m.histogram_rising
        )
    return macd_15m.crossover == "BEARISH_CROSS" or (
        hist <= -hist_floor and not macd_15m.histogram_rising
    )


def passes_15m_iql(macd_15m: MACDResult, allowed_side: str, hist_floor: float) -> bool:
    """15m Indicator Quality Layer for alt strategies (full check).

    Passes when EITHER:
      - sol_ltf_strength_15m says the 15m is "confirmed" (composite >= 0.50), OR
      - the relaxed early-entry rule passes.

    Extracted from SolMacroStrategy._passes_15m_iql and
    src/backtest/updown_engine._passes_15m_iql_macd (identical logic).
    """
    if sol_ltf_strength_15m(macd_15m, allowed_side).confirmed:
        return True
    return passes_15m_iql_relaxed_rule(macd_15m, allowed_side, hist_floor)


def sol_ltf_strength_15m(macd_15m: MACDResult, allowed_side: str) -> LtfStrengthResult:
    """SOL-family weights & threshold. Matches SolMacroStrategy._check_15m_confirmation.
    Used by SOL/ETH/XRP/HYPE alt strategies."""
    reasons: List[str] = []
    s = 0.0
    if allowed_side == "LONG":
        if macd_15m.crossover == "BULLISH_CROSS":
            s += 0.40
            reasons.append("15m MACD bull cross")
        if macd_15m.histogram_rising:
            if macd_15m.prev_histogram < 0 and macd_15m.histogram > 0:
                s += 0.35
                reasons.append("15m hist red-to-green")
            elif macd_15m.histogram > macd_15m.prev_histogram:
                s += 0.15
                reasons.append("15m hist rising")
        if macd_15m.macd_line > macd_15m.signal_line:
            s += 0.10
            reasons.append("15m MACD above signal")
    else:  # SHORT
        if macd_15m.crossover == "BEARISH_CROSS":
            s += 0.40
            reasons.append("15m MACD bear cross")
        if not macd_15m.histogram_rising:
            if macd_15m.prev_histogram > 0 and macd_15m.histogram < 0:
                s += 0.35
                reasons.append("15m hist green-to-red")
            elif macd_15m.histogram < macd_15m.prev_histogram:
                s += 0.15
                reasons.append("15m hist falling")
        if macd_15m.macd_line < macd_15m.signal_line:
            s += 0.10
            reasons.append("15m MACD below signal")
    return LtfStrengthResult(
        confirmed=s >= _LTF_CONFIRM_THRESHOLD,
        strength=min(1.0, s),
        reasons=reasons,
    )
