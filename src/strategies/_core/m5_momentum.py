"""5m candle momentum scoring. Maps m5_direction to a probability adjustment.

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
