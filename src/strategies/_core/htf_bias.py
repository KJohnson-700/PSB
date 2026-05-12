"""4H higher-timeframe bias (BTC). Extracted from BitcoinStrategy._get_higher_tf_bias
and src/backtest/updown_engine._get_htf_bias, which were two hand-copied implementations.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.btc_price_service import TechnicalAnalysis


@dataclass(frozen=True)
class BtcHtfBiasResult:
    bias: str                # "BULLISH" | "BEARISH" | "NEUTRAL"
    raw_vote_bias: str       # Pre-conviction-gate bias ("BULLISH" | "BEARISH" | "NEUTRAL")
    downgraded_to_neutral: bool  # True iff a 2/3 vote was downgraded to NEUTRAL by the hist floor
    macd_4h_hist: float      # Echoed so callers can log without re-reading


def btc_htf_bias(ta: TechnicalAnalysis, *, min_hist_magnitude: float = 20.0) -> BtcHtfBiasResult:
    """3-vote 4H bias with conviction gate. THE LAW.

    Votes:
        1. Trend Sabre direction (trend == 1 or -1)
        2. Price position vs Sabre SMA(35)
        3. 4H MACD direction with three bull cases:
           a) MACD above zero
           b) Fresh BULLISH_CROSS while histogram rising (early bull)
           c) Histogram positive while still below zero (recovery)

    A 2/3 vote yields a directional bias only if |4H MACD hist| >= min_hist_magnitude;
    otherwise it downgrades to NEUTRAL (avoids 50/50 coin-flip entries on weak signals).
    """
    sabre = ta.trend_sabre
    macd_4h = ta.macd_4h
    price = ta.current_price

    bull_votes = 0
    bear_votes = 0

    if sabre.trend == 1:
        bull_votes += 1
    elif sabre.trend == -1:
        bear_votes += 1

    if price > sabre.ma_value:
        bull_votes += 1
    elif price < sabre.ma_value:
        bear_votes += 1

    early_bull = macd_4h.crossover == "BULLISH_CROSS" and macd_4h.histogram_rising
    early_bear = macd_4h.crossover == "BEARISH_CROSS" and not macd_4h.histogram_rising
    recovery = not macd_4h.above_zero and macd_4h.histogram > 0

    if early_bear:
        bear_votes += 1
    elif macd_4h.above_zero or early_bull or recovery:
        bull_votes += 1
    else:
        bear_votes += 1

    if bull_votes >= 2:
        raw = "BULLISH"
    elif bear_votes >= 2:
        raw = "BEARISH"
    else:
        return BtcHtfBiasResult(
            bias="NEUTRAL",
            raw_vote_bias="NEUTRAL",
            downgraded_to_neutral=False,
            macd_4h_hist=macd_4h.histogram,
        )

    if abs(macd_4h.histogram) < min_hist_magnitude:
        return BtcHtfBiasResult(
            bias="NEUTRAL",
            raw_vote_bias=raw,
            downgraded_to_neutral=True,
            macd_4h_hist=macd_4h.histogram,
        )

    return BtcHtfBiasResult(
        bias=raw,
        raw_vote_bias=raw,
        downgraded_to_neutral=False,
        macd_4h_hist=macd_4h.histogram,
    )
