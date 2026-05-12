"""Alt-strategy probability helpers shared between live sol_macro and the backtest engine.

Includes:
- apply_primary_htf_bias: signed boost from the resolved primary HTF vote
- alt_1h_hist_gate: relaxed 1H histogram gate (matches live sol_macro;
  the pre-refactor backtest used a hard reject that diverged from live)
- sol_rsi_extremes_adj: 75/25 RSI adjustment used by alt 15m + 5m paths
- btc_catalyst_boost: lag_opportunity / btc_spike_detected boost
"""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.btc_price_service import MACDResult


def apply_primary_htf_bias(est_prob_up: float, primary_htf_bias: str, weight: float) -> float:
    """Signed boost from the primary HTF vote.

    BULLISH -> +weight, BEARISH -> -weight, NEUTRAL/other -> unchanged.
    Matches SolMacroStrategy._apply_primary_htf_bias.
    """
    if primary_htf_bias == "BULLISH":
        return est_prob_up + weight
    if primary_htf_bias == "BEARISH":
        return est_prob_up - weight
    return est_prob_up


@dataclass(frozen=True)
class AltHistGateResult:
    allowed: bool
    rejection_reason: str = ""


def alt_1h_hist_gate(macd_1h: MACDResult, allowed_side: str) -> AltHistGateResult:
    """Relaxed 1H histogram gate for alt 15m updown entries.

    LONG passes when histogram is rising OR currently positive (even if decelerating).
    SHORT passes when histogram is falling OR currently negative.
    Blocks ONLY when histogram is actively against the trade direction.

    Pre-refactor the backtest had a strict "must be rising/falling" gate that
    rejected positive-but-decelerating histograms; live always used the
    relaxed form. This unifies them on the live behavior.
    """
    if allowed_side == "LONG":
        ok = macd_1h.histogram_rising or macd_1h.histogram > 0
        return AltHistGateResult(
            allowed=ok,
            rejection_reason="" if ok else "histogram_1h_blocks_long_15m",
        )
    # SHORT
    ok = (not macd_1h.histogram_rising) or macd_1h.histogram < 0
    return AltHistGateResult(
        allowed=ok,
        rejection_reason="" if ok else "histogram_1h_blocks_short_15m",
    )


def sol_rsi_extremes_adj(rsi_14: float, *, magnitude: float = 0.03) -> float:
    """RSI 75/25 mean-reversion adjustment used by alt 15m + 5m paths.

    >75 -> -magnitude  (overbought support for SHORT)
    <25 -> +magnitude  (oversold support for LONG)
    """
    if rsi_14 > 75:
        return -magnitude
    if rsi_14 < 25:
        return magnitude
    return 0.0


def btc_catalyst_boost(
    *, lag_opportunity: bool, lag_direction: str, lag_magnitude: float,
    btc_spike_detected: bool, allowed_side: str,
) -> float:
    """BTC catalyst boost applied to alt entries (live sol_macro 15m + 5m).

    Priority: lag opportunity (if direction matches), else BTC spike.
    Lag magnitude scaled into [0.02, 0.04].
    Sign flips for SHORT.
    """
    boost = 0.0
    if lag_opportunity and lag_direction == allowed_side:
        boost = min(0.04, max(0.02, abs(lag_magnitude) * 0.015))
    elif btc_spike_detected:
        boost = 0.03
    return boost if allowed_side == "LONG" else -boost
