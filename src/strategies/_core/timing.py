"""15m updown entry-timing bonus from candle momentum + prediction windows.

Extracted from:
- BitcoinStrategy._check_timing (bitcoin.py L416-L455)
- src/backtest/updown_engine._edge_15m inline timing block

The two implementations were structurally identical apart from the
reasons-list bookkeeping that lives only in the live wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.analysis.btc_price_service import CandleMomentum


@dataclass(frozen=True)
class TimingBonusResult:
    bonus: float
    reasons: List[str] = field(default_factory=list)


def btc_15m_timing_bonus(mom: CandleMomentum, allowed_side: str) -> TimingBonusResult:
    """m15 + m5 candle direction + prediction-window contributions.

    Per-tier weights (LONG; SHORT mirrors):
      m15 SPIKE aligned  : +0.08
      m15 DRIFT aligned  : +0.04
      m15 SPIKE/DRIFT opp: -0.05
      m5  SPIKE aligned  : +0.04
      m5  DRIFT aligned  : +0.02
      m15 in prediction window: +0.03
      m5  in prediction window: +0.02
    """
    bonus = 0.0
    reasons: List[str] = []

    if allowed_side == "LONG":
        if mom.m15_direction in ("SPIKE_UP", "DRIFT_UP"):
            bonus += 0.08 if "SPIKE" in mom.m15_direction else 0.04
            reasons.append(f"15m early {mom.m15_direction} ({mom.m15_move_pct:+.3f}%)")
        elif mom.m15_direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
            bonus -= 0.05
            reasons.append(f"15m early AGAINST ({mom.m15_direction})")
        if mom.m5_direction in ("SPIKE_UP", "DRIFT_UP"):
            bonus += 0.04 if "SPIKE" in mom.m5_direction else 0.02
            reasons.append(f"5m early {mom.m5_direction}")
    else:  # SHORT
        if mom.m15_direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
            bonus += 0.08 if "SPIKE" in mom.m15_direction else 0.04
            reasons.append(f"15m early {mom.m15_direction} ({mom.m15_move_pct:+.3f}%)")
        elif mom.m15_direction in ("SPIKE_UP", "DRIFT_UP"):
            bonus -= 0.05
            reasons.append(f"15m early AGAINST ({mom.m15_direction})")
        if mom.m5_direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
            bonus += 0.04 if "SPIKE" in mom.m5_direction else 0.02
            reasons.append(f"5m early {mom.m5_direction}")

    if mom.m15_in_prediction_window:
        bonus += 0.03
        reasons.append("15m predict window")
    if mom.m5_in_prediction_window:
        bonus += 0.02
        reasons.append("5m predict window")

    return TimingBonusResult(bonus=bonus, reasons=reasons)
