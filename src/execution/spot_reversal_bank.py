"""Spot-reversal bank — SHADOW instrumentation (default) for the exit gap-through leak.

Problem (2026-07-22): on hold-to-resolution updown lanes, when the underlying
reverses the Polymarket binary book reprices in a discrete jump *faster* than
the 3s fast-exit cadence, so a winning position gives back its peak straight
through the trail floor. Observed: doge 5m down peaked +35.6% (trail floor
locked +25.6%), the book gapped past it to -11% between two 3s ticks. A stop
that reads only the CLOB book cannot see the reversal coming; the underlying
spot leads the reprice.

This module tracks each held position's FAVORABLE underlying-spot extreme and,
when an in-profit position's spot reverses past a threshold, flags a
"would-bank" event. In SHADOW mode it ONLY logs (never exits) so we can
forward-measure whether banking at that moment beats the actual realized exit
before ever touching live trading. It is the ghost-log pattern applied to the
exit path: config-gated, default shadow, fully inert when off, and it never
mutates any trading/risk state.

Live mode (spot_reversal_bank_mode: live) is intentionally NOT wired to an
actual exit here — flipping to live requires the caller to act on the returned
event, gated on forward-proven shadow data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

# Strategy name -> Binance spot symbol. HYPE trades on Hyperliquid (no Binance
# spot symbol in the WS feed) -> skipped in v1 (returns None -> caller no-ops).
_STRAT_SYMBOL = {
    "bitcoin": "BTCUSDT",
    "eth_macro": "ETHUSDT",
    "sol_macro": "SOLUSDT",
    "xrp_macro": "XRPUSDT",
    "doge_macro": "DOGEUSDT",
    "bnb_macro": "BNBUSDT",
}


def symbol_for_strategy(strategy: str) -> Optional[str]:
    """Binance spot symbol for a strategy, or None if it has no spot feed."""
    return _STRAT_SYMBOL.get(str(strategy or "").strip())


@dataclass
class _PosState:
    down_bet: bool            # True = position profits when spot FALLS (NO / short YES)
    fav_spot: float           # favorable spot extreme (min for down-bet, max for up-bet)
    peak_pnl_pct: float = 0.0
    fired: bool = False       # shadow event already emitted once for this position


@dataclass
class SpotReversalBank:
    """Tracks per-position underlying-spot extremes and flags in-profit reversals.

    Pure bookkeeping: ``observe`` returns a shadow event dict on the first
    reversal-while-in-profit that crosses the threshold, else None. It never
    touches positions, orders, or risk state.
    """

    arm_pct: float = 0.12            # only consider once peak pnl clears this (matches trail arm)
    reversal_pct: float = 0.003      # spot reversal from favorable extreme to flag (0.3%)
    _state: Dict[str, _PosState] = field(default_factory=dict)

    def observe(
        self,
        *,
        position_id: str,
        down_bet: bool,
        current_spot: Optional[float],
        current_pnl_pct: float,
    ) -> Optional[dict]:
        """Update state for one held position; return a shadow event when a
        reversal-while-in-profit first crosses the threshold, else None.

        current_spot may be None/<=0 (feed miss) -> treated as no-op for the tick.
        """
        if current_spot is None or current_spot <= 0:
            return None
        st = self._state.get(position_id)
        if st is None:
            self._state[position_id] = _PosState(
                down_bet=bool(down_bet),
                fav_spot=float(current_spot),
                peak_pnl_pct=float(current_pnl_pct),
            )
            return None

        if st.down_bet:
            st.fav_spot = min(st.fav_spot, float(current_spot))
            reversal = (float(current_spot) - st.fav_spot) / st.fav_spot if st.fav_spot > 0 else 0.0
        else:
            st.fav_spot = max(st.fav_spot, float(current_spot))
            reversal = (st.fav_spot - float(current_spot)) / st.fav_spot if st.fav_spot > 0 else 0.0

        st.peak_pnl_pct = max(st.peak_pnl_pct, float(current_pnl_pct))

        if (
            not st.fired
            and st.peak_pnl_pct >= self.arm_pct
            and reversal >= self.reversal_pct
            and float(current_pnl_pct) < st.peak_pnl_pct
        ):
            st.fired = True
            return {
                "reversal_pct": round(reversal, 5),
                "peak_pnl_pct": round(st.peak_pnl_pct, 4),
                "current_pnl_pct": round(float(current_pnl_pct), 4),
                "giveback_pct": round(st.peak_pnl_pct - float(current_pnl_pct), 4),
            }
        return None

    def drop(self, position_id: str) -> None:
        """Forget a closed position (call on exit to bound memory)."""
        self._state.pop(position_id, None)

    def active_count(self) -> int:
        return len(self._state)
