"""Never-green fast-cut — SHADOW instrumentation (default) for the dominant exit leak.

Finding (2026-07-22, session test_20260722_043413): splitting exits by final MFE,
the NEVER-GREEN pool (a position whose peak pnl never cleared ~2%) was the ENTIRE
loss burden — 25 trades, −$98.45, 0% WR (zero ever recovered to a win), riding to
−36% avg (tails to −67/−74/−82%). The WENT-GREEN pool was +$171 at 69% WR. Winners
go green FAST (median hold 66s); never-green collapses ride 126s+. So a time-based
cut — "if a position is still not green after X seconds, exit" — separates losers
from winners by MFE-TIMING, orthogonal to entry direction (which has no skill:
est_prob ~0.50 AUC, conviction floor refuted). Critically, by construction such a
cut CANNOT hit a winner: winners are, by definition, not in the never-green pool.

CAVEAT this shadow measures: final-MFE 0% is suggestive but not conclusive for the
cut TIMING — a position still-not-green at 60s could recover by 90s and end a
winner (a "false-cut"). So this logs NEVER_GREEN_SHADOW would-cut events without
exiting, so we can forward-measure would-save (rode further down) vs false-cut
(recovered) before any live cut is ever enabled. Ghost-log pattern on the exit
path: config-gated, default off/shadow, never mutates trading state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class _PosState:
    peak_pnl_pct: float          # high-water mark of pnl% since entry
    fired: bool = False          # shadow event already emitted once


@dataclass
class NeverGreenCut:
    """Tracks whether each held position has 'gone green' and flags the ones that
    stay never-green past a hold threshold. Pure bookkeeping: ``observe`` returns a
    shadow event dict on the first qualifying tick, else None. Never touches
    positions, orders, or risk state.
    """

    green_threshold_pct: float = 0.02   # peak pnl below this == "never went green"
    cut_after_secs: float = 60.0        # flag if still-not-green past this hold time
    _state: Dict[str, _PosState] = field(default_factory=dict)

    def observe(
        self,
        *,
        position_id: str,
        hold_seconds: float,
        current_pnl_pct: float,
        cut_after_secs: Optional[float] = None,
    ) -> Optional[dict]:
        """Update state for one held position; return a shadow event the first time a
        still-never-green position crosses the hold threshold, else None. Pass
        ``cut_after_secs`` to override the default threshold per call (e.g. a longer
        window for 1h lanes, which develop slower than 5m); falls back to
        ``self.cut_after_secs`` when None."""
        thr = self.cut_after_secs if cut_after_secs is None else float(cut_after_secs)
        st = self._state.get(position_id)
        if st is None:
            st = _PosState(peak_pnl_pct=float(current_pnl_pct))
            self._state[position_id] = st
        else:
            st.peak_pnl_pct = max(st.peak_pnl_pct, float(current_pnl_pct))

        went_green = st.peak_pnl_pct >= self.green_threshold_pct
        if (
            not st.fired
            and not went_green
            and float(hold_seconds) >= thr
        ):
            st.fired = True
            return {
                "would_cut_pnl_pct": round(float(current_pnl_pct), 4),
                "peak_pnl_pct": round(st.peak_pnl_pct, 4),
                "hold_seconds": round(float(hold_seconds), 1),
                "cut_after_secs": round(float(thr), 1),
            }
        return None

    def drop(self, position_id: str) -> None:
        """Forget a closed position (call on exit to bound memory)."""
        self._state.pop(position_id, None)

    def active_count(self) -> int:
        return len(self._state)
