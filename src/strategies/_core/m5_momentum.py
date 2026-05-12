"""5m candle momentum scoring. Maps m5_direction to a probability adjustment.

Extracted from:
- BitcoinStrategy 5m updown path (bitcoin.py L1083-L1119)
- src/backtest/updown_engine._calc_m5_momentum (scoring half only)

NB: the producer (btc_price_service.calc_candle_momentum) emits only
SPIKE_UP/SPIKE_DOWN/DRIFT_UP/DRIFT_DOWN/empty for m5_direction. The
LEAN_UP/LEAN_DOWN entries in live BitcoinStrategy are dead code paths
(LEAN_* is a value of CandleMomentum.momentum_signal, not m5_direction).
We preserve them here in case a future producer change populates them.
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
