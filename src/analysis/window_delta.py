"""Window-delta probability — a model-independent direction signal.

PSB's ``est_prob_up`` is derived from a lagging-trend formula (and, on the
marginal path, an AI inference step). Its documented failure mode is shorting a
rising tape: a lagging ``htf_bias`` locks the side to BUY_NO while price has
actually risen since the window opened.

This module turns the single most basic ground truth — *which way has price
moved since this window opened* — into a probability of "ends up", entirely
independently of the model. It is used as a confirmation gate (block entries
whose chosen side disagrees with the delta), not blended into the edge math.

Pure functions only: no I/O, no strategy deps, stdlib only (no scipy).
"""

from __future__ import annotations

import math

# Output is clamped to this band so a near-resolution delta never produces a
# literal 0.0/1.0 (which would imply a degenerate, un-hedgeable certainty).
_PROB_FLOOR = 0.05
_PROB_CEIL = 0.95

# Floor on remaining-window sigma so the normal-CDF argument stays finite as
# mins_left -> 0. Expressed in the same %-move units as ``move_pct``.
_SIGMA_EPS = 1e-4

# Fallback logistic slope (per %-move) used when atr_pct is unavailable. Tuned
# so a ~0.1% move maps to a mild lean rather than a hard call.
_FALLBACK_LOGISTIC_K = 6.0


def window_delta_pct(current_price: float, window_open_price: float) -> float:
    """Percent move since the window opened.

    ``(current - open) / open * 100``. Mirrors the existing momentum formula in
    ``btc_price_service.calc_candle_momentum``. Returns 0.0 on a non-positive
    open price rather than raising — a missing/garbage open should read as
    "no information", not crash the scan loop.
    """
    if not window_open_price or window_open_price <= 0:
        return 0.0
    return (current_price - window_open_price) / window_open_price * 100.0


def _phi(x: float) -> float:
    """Standard normal CDF via stdlib erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def window_delta_prob(
    move_pct: float,
    mins_left: float,
    window_minutes: float,
    atr_pct: float | None,
) -> float:
    """Time-aware probability that the window ends "up", from the delta alone.

    Models the close as ``current + N(0, sigma_remaining)`` where the remaining
    volatility shrinks as the window runs out:

        sigma_remaining = atr_pct * sqrt(clamp(mins_left / window_minutes, 0, 1))
        P(up) = Phi(move_pct / sigma_remaining)

    Behaviour:
      * mins_left -> 0  : sigma_remaining -> 0, so P -> step (the delta *is* the
        answer near resolution).
      * full window left, zero delta : P -> 0.5 (maximally uncertain).
      * monotonically increasing in ``move_pct``.

    ``atr_pct`` is the per-window volatility scale (reuse ``atr_14`` as a percent
    of price). When it is missing/non-positive we fall back to a fixed-slope
    logistic on ``move_pct`` (time-insensitive, lower confidence) so the signal
    still produces a sane lean.

    Output is clamped to ``[0.05, 0.95]``.
    """
    if atr_pct is None or atr_pct <= 0 or window_minutes <= 0:
        # Fallback: time-insensitive logistic. Still monotonic in move_pct.
        prob = 1.0 / (1.0 + math.exp(-_FALLBACK_LOGISTIC_K * move_pct))
        return _clamp(prob, _PROB_FLOOR, _PROB_CEIL)

    frac_left = _clamp(mins_left / window_minutes, 0.0, 1.0)
    sigma_remaining = max(_SIGMA_EPS, atr_pct * math.sqrt(frac_left))
    prob = _phi(move_pct / sigma_remaining)
    return _clamp(prob, _PROB_FLOOR, _PROB_CEIL)


# Up/down window label -> minutes. Matches updown_timeframe_label() outputs.
WINDOW_MINUTES = {"5m": 5.0, "15m": 15.0, "1h": 60.0}


def evaluate_window_delta(asset_obj, tf: str, mins_left: float):
    """Compute (move_pct, delta_prob) for an asset's current window, or None.

    Reads ``window_open_<tf>``, ``current_price`` and ``atr_14`` off the asset
    analysis object by attribute name — works for both ``SOLAnalysis`` and the
    BTC ``TechnicalAnalysis`` (identical field names, no hard import here).

    Returns ``None`` when the inputs are unavailable (missing window open,
    unknown tf, zero price) so callers fail OPEN — a missing delta must never
    block a trade. ``atr_14`` is in price units and converted to a percent.
    """
    window_minutes = WINDOW_MINUTES.get(tf)
    open_px = float(getattr(asset_obj, f"window_open_{tf}", 0.0) or 0.0)
    cur = float(getattr(asset_obj, "current_price", 0.0) or 0.0)
    atr = float(getattr(asset_obj, "atr_14", 0.0) or 0.0)
    if not window_minutes or open_px <= 0 or cur <= 0:
        return None
    move = window_delta_pct(cur, open_px)
    atr_pct = (atr / cur * 100.0) if atr > 0 else None
    prob = window_delta_prob(move, mins_left, window_minutes, atr_pct)
    return move, prob


def delta_confirms_side(
    delta_prob: float,
    action: str,
    margin: float = 0.0,
) -> bool:
    """Does the window-delta agree with the chosen side?

    BUY_YES (long) needs ``delta_prob >= 0.5 + margin``; BUY_NO (short) needs
    ``delta_prob <= 0.5 - margin``. Any other action is treated as confirmed
    (the gate only adjudicates the two directional up/down sides).
    """
    if action == "BUY_YES":
        return delta_prob >= 0.5 + margin
    if action == "BUY_NO":
        return delta_prob <= 0.5 - margin
    return True
