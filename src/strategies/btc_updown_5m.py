"""Shared BTC 5m up/down quant math — live ``bitcoin`` and ``UpdownBacktestEngine`` must call this."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.analysis.btc_price_service import MACDResult, TrendSabreResult


def btc_5m_htf_boost(
    sabre: TrendSabreResult,
    macd_4h: MACDResult,
    *,
    full_mag: float = 0.04,
    partial_mag: float = 0.02,
) -> float:
    # 2026-07-20: magnitudes are config-driven (btc_5m_htf_full_boost/partial). The old
    # hardcoded 0.04 capped bull conviction so est topped ~0.62 even in a full bull, below
    # the bull-priced YES -> the lane bought cheap NO and faded every rally. Symmetric bear.
    if sabre.trend == 1 and macd_4h.above_zero:
        return full_mag
    if sabre.trend == 1 or macd_4h.above_zero:
        return partial_mag
    if sabre.trend == -1 and not macd_4h.above_zero:
        return -full_mag
    return -partial_mag


def btc_5m_4h_1h_hist_gate(
    macd_4h: MACDResult, macd_1h: MACDResult, allowed_side: str
) -> bool:
    if allowed_side == "LONG":
        if macd_4h.histogram_rising:
            return True
        return macd_1h.histogram_rising
    if not macd_4h.histogram_rising:
        return True
    return not macd_1h.histogram_rising


def btc_5m_hist_gate_reject_reason(
    macd_4h: MACDResult, macd_1h: MACDResult, allowed_side: str
) -> Optional[str]:
    if btc_5m_4h_1h_hist_gate(macd_4h, macd_1h, allowed_side):
        return None
    if allowed_side == "LONG":
        return "hist_gate_5m_long_reject"
    return "hist_gate_5m_short_reject"


def score_m5_direction(m5_direction: str, allowed_side: str) -> float:
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


def m5_candle_age_minutes(window_minutes: int, eval_minutes_left: float) -> float:
    """Minutes elapsed in the current 5m candle at entry eval (live ``m5_age_minutes``)."""
    return max(0.0, float(window_minutes) - float(eval_minutes_left))


def m5_in_prediction_window_at_age(m5_age_minutes: float) -> bool:
    return 3.0 <= float(m5_age_minutes) <= 4.0


def rsi_soft_adj_5m(rsi_14: float) -> float:
    if rsi_14 > 80:
        return -0.02
    if rsi_14 > 65:
        return -0.01
    if rsi_14 < 20:
        return 0.02
    if rsi_14 < 35:
        return 0.01
    return 0.0


@dataclass(frozen=True)
class Btc5mQuantResult:
    est_prob_up: float
    edge: float
    confidence: float
    htf_boost: float
    m5_adj: float
    hist_gate_allowed: bool
    rsi_blocked: bool


def compute_btc_5m_quant(
    *,
    sabre: TrendSabreResult,
    macd_4h: MACDResult,
    macd_1h: MACDResult,
    rsi_14: float,
    allowed_side: str,
    yes_price: float,
    m5_direction: str,
    m5_in_prediction_window: bool,
    htf_full_boost: float = 0.04,
    htf_partial_boost: float = 0.02,
    hard_hist_gate: bool = True,
) -> Btc5mQuantResult:
    """Raw quant path for BTC 5m (pre ``_calibrate_est_prob`` on live)."""
    est_prob_up = 0.50
    htf_boost = btc_5m_htf_boost(sabre, macd_4h, full_mag=htf_full_boost, partial_mag=htf_partial_boost)
    est_prob_up += htf_boost

    hist_ok = btc_5m_4h_1h_hist_gate(macd_4h, macd_1h, allowed_side)
    if not hist_ok and hard_hist_gate:
        return Btc5mQuantResult(
            est_prob_up=est_prob_up,
            edge=0.0,
            confidence=0.0,
            htf_boost=htf_boost,
            m5_adj=0.0,
            hist_gate_allowed=False,
            rsi_blocked=False,
        )

    if allowed_side == "LONG" and rsi_14 > 65:
        return Btc5mQuantResult(
            est_prob_up=est_prob_up,
            edge=0.0,
            confidence=0.0,
            htf_boost=htf_boost,
            m5_adj=0.0,
            hist_gate_allowed=True,
            rsi_blocked=True,
        )

    m5_adj = score_m5_direction(m5_direction, allowed_side)
    if allowed_side == "LONG":
        est_prob_up += m5_adj
    else:
        est_prob_up -= m5_adj

    if m5_in_prediction_window:
        if allowed_side == "LONG":
            est_prob_up += 0.02
        else:
            est_prob_up -= 0.02

    est_prob_up += rsi_soft_adj_5m(rsi_14)
    est_prob_up = max(0.10, min(0.90, est_prob_up))

    if allowed_side == "LONG":
        edge = est_prob_up - yes_price
    else:
        edge = yes_price - est_prob_up

    confidence = max(0.45, min(0.85, 0.50 + abs(htf_boost) * 2.5 + abs(m5_adj) * 1.5))
    return Btc5mQuantResult(
        est_prob_up=est_prob_up,
        edge=edge,
        confidence=confidence,
        htf_boost=htf_boost,
        m5_adj=m5_adj,
        hist_gate_allowed=True,
        rsi_blocked=False,
    )


def edge_for_action(*, estimated_prob: float, yes_price: float, action: str) -> float:
    if action == "BUY_YES":
        return estimated_prob - yes_price
    return (1.0 - estimated_prob) - (1.0 - yes_price)
