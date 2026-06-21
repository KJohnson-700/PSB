"""Final-window winner top-up — SHADOW logger (logging-only; never affects trading).

WHY (the shadow-before-paper rationale)
---------------------------------------
Operator wallet evidence shows a real Polymarket microstructure edge: on BTC 5m
Up/Down markets the functionally-winning token still trades at ~0.97-0.99 in the
final window (resolution latency), so buying it there and redeeming at $1.00 is a
near-riskless few cents. That proves the *market opportunity* exists — it proves
NOTHING about whether OUR code can (a) correctly identify the winning side in the
final window without false positives, or (b) actually get filled at 0.97-0.99.

This stage validates (a) only, truthfully and offline:
  - It logs, for each open BTC 5m position entering the final window, whether our
    independent winner-confirmation gate fired, the side it called, the REAL price
    we could have topped up at (best ask on our side — so fill-availability is
    honest, not assumed), the oracle basis, and seconds-to-resolution.
  - A settler then scores each shadow top-up against the real settled outcome.

We do NOT go to paper next-for-edge: paper fills at the requested price with ~100%
fill, so on this asymmetric payoff (+~0.02 win vs -~0.98 loss) paper would show a
fake near-100% win rate and hide the only real risks (no-fill + wrong-confirm tail).
Paper is for accounting plumbing later; live smoke is the real fill-quality gate.

Pure instrumentation: reads prices/book the fast-exit loop already fetched, writes
one jsonl row per (position, market) at most once, wrapped so it can never raise
into the trading loop.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class FinalWindowTopupShadow:
    """Shadow-log would-be final-window top-ups on open BTC 5m positions.

    Default-OFF. Enable via config: ``final_window_topup.shadow_enabled: true``.
    """

    def __init__(
        self,
        *,
        path: str = "data/calibration/topup_shadow.jsonl",
        enabled: bool = False,
        # Gate params (conservative defaults; the whole point is to MEASURE these).
        final_window_max_mins: float = 1.0,   # only inside the last N minutes
        winner_mark_threshold: float = 0.90,  # our-side mark must be >= this to "confirm winning"
        max_oracle_basis_bps: float = 25.0,   # oracle must agree (basis within bound)
        max_topup_price: float = 0.99,        # never top up above this
        below_fair_max: float = 0.90,         # 2026-06-21: research says the only +EV
                                              # cohort is independent-confirm AND the ask
                                              # still materially below fair (~<=0.90), not
                                              # 0.95+. Flag rows at/below this for analysis.
        only_strategy: str = "bitcoin",
        only_window: str = "5m",
    ) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self.final_window_max_mins = float(final_window_max_mins)
        self.winner_mark_threshold = float(winner_mark_threshold)
        self.max_oracle_basis_bps = float(max_oracle_basis_bps)
        self.max_topup_price = float(max_topup_price)
        self.below_fair_max = float(below_fair_max)
        self.only_strategy = only_strategy
        self.only_window = only_window
        self._logged: set = set()  # (trade_id) already shadow-logged this process

    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]]) -> "FinalWindowTopupShadow":
        c = (cfg or {}).get("final_window_topup", {}) or {}
        return cls(
            path=str(c.get("shadow_path", "data/calibration/topup_shadow.jsonl")),
            enabled=bool(c.get("shadow_enabled", False)),
            final_window_max_mins=float(c.get("final_window_max_mins", 1.0) or 1.0),
            winner_mark_threshold=float(c.get("winner_mark_threshold", 0.90) or 0.90),
            max_oracle_basis_bps=float(c.get("max_oracle_basis_bps", 25.0) or 25.0),
            max_topup_price=float(c.get("max_topup_price", 0.99) or 0.99),
            below_fair_max=float(c.get("below_fair_max", 0.90) or 0.90),
        )

    def observe(
        self,
        *,
        trade_id: str,
        market_id: str,
        strategy: str,
        window: str,
        entry_leg: str,            # "YES" or "NO" — the side we hold
        entry_price: float,
        yes_mark: float,           # CLOB /midpoint yes price (the exit-loop ruler)
        best_ask_yes: Optional[float],
        best_bid_yes: Optional[float],
        mins_left: Optional[float],
        oracle_basis_bps: Optional[float],
        market_end_at: Any = None,
    ) -> None:
        """Evaluate the top-up trigger for one open position and shadow-log it once.

        Never raises into the trading loop.
        """
        if not self.enabled:
            return
        try:
            if not trade_id or trade_id in self._logged:
                return
            if strategy != self.only_strategy or window != self.only_window:
                return
            if mins_left is None or mins_left > self.final_window_max_mins or mins_left < 0:
                return
            if yes_mark is None:
                return

            leg = "NO" if str(entry_leg).upper() == "NO" else "YES"
            # Our-side mark + the executable top-up ask on OUR side.
            if leg == "YES":
                our_mark = float(yes_mark)
                # to top up a YES position we buy YES at its ask
                our_ask = best_ask_yes
            else:
                our_mark = 1.0 - float(yes_mark)
                # NO ask = 1 - (YES bid): buying NO lifts the YES bid side
                our_ask = (1.0 - best_bid_yes) if best_bid_yes is not None else None

            # Winner confirmation. The SHADOW gate fires on mark-confirmation alone
            # (our-side mark deep in-the-money, inside the final window) and merely
            # RECORDS whether the oracle also agreed — so the settler can tell us, from
            # real outcomes, both (a) mark-only accuracy and (b) whether adding the
            # oracle-basis check improves it. The LIVE stage will hard-require both.
            mark_confirms = our_mark >= self.winner_mark_threshold
            oracle_ok = (
                oracle_basis_bps is not None
                and abs(float(oracle_basis_bps)) <= self.max_oracle_basis_bps
            )
            confirmed = bool(mark_confirms)

            # Fill availability is part of the truth: is there an executable ask
            # at/below our cap? (no_fill rows matter — they show when the edge
            # evaporates because the book already ran to 1.00.)
            topup_price = None
            fillable = False
            if our_ask is not None and 0.0 < float(our_ask) <= self.max_topup_price:
                topup_price = round(float(our_ask), 4)
                fillable = True

            if not confirmed:
                return  # only record rows where our gate would have fired a top-up

            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "trade_id": trade_id,
                "market_id": market_id,
                "strategy": strategy,
                "window": window,
                "held_leg": leg,
                "entry_price": round(float(entry_price), 4) if entry_price is not None else None,
                "yes_mark": round(float(yes_mark), 4),
                "our_side_mark": round(our_mark, 4),
                "detected_winning_side": leg,  # we top up the side we hold, iff it's winning
                "oracle_basis_bps": round(float(oracle_basis_bps), 3) if oracle_basis_bps is not None else None,
                "oracle_ok": oracle_ok,
                "mins_left": round(float(mins_left), 3),
                "topup_price": topup_price,       # real best-ask on our side (None if no fill <= cap)
                "fillable": fillable,             # was an executable top-up actually available?
                "winner_mark_threshold": self.winner_mark_threshold,
                "market_end_at": str(market_end_at) if market_end_at is not None else None,
                "settled": False,                 # filled in by the settler
            }
            self._write(row)
            self._logged.add(trade_id)
        except Exception:  # never break the exit loop
            logger.debug("final_window_topup_shadow.observe failed (ignored)", exc_info=True)

    def observe_market(
        self,
        *,
        market_id: str,
        strategy: str,
        window: str,
        yes_mark: float,           # REAL /midpoint (NOT the scanner 0.5-placeholder)
        best_ask_yes: Optional[float],
        best_bid_yes: Optional[float],
        mins_left: Optional[float],
        oracle_basis_bps: Optional[float] = None,
        oracle_price: Optional[float] = None,
        spot_price: Optional[float] = None,
        oracle_age_s: Optional[float] = None,
        market_end_at: Any = None,
    ) -> None:
        """Standalone capture (NOT tied to a held position): for ANY 5m market in its
        final window, detect the winning side from the mark and shadow-log the would-be
        capture. This is the WIDE version — feed it real /midpoint marks from the
        dedicated final-window sampler, never the scanner's 0.5 placeholder.

        Winning side: YES if yes_mark >= threshold, NO if yes_mark <= 1-threshold,
        else ambiguous (no capture — those near-0.5-at-the-wire markets are the tail
        that kills naive farms, so we deliberately DON'T claim them).
        Never raises.
        """
        if not self.enabled:
            return
        try:
            if strategy != self.only_strategy or window != self.only_window:
                return
            if mins_left is None or mins_left > self.final_window_max_mins or mins_left < 0:
                return
            if yes_mark is None:
                return
            ym = float(yes_mark)
            if ym >= self.winner_mark_threshold:
                leg = "YES"
            elif ym <= (1.0 - self.winner_mark_threshold):
                leg = "NO"
            else:
                return  # ambiguous at the wire — do not claim a winner
            key = f"{market_id}|{leg}"
            if key in self._logged:
                return
            our_ask = best_ask_yes if leg == "YES" else (
                (1.0 - best_bid_yes) if best_bid_yes is not None else None
            )
            our_mark = ym if leg == "YES" else (1.0 - ym)
            oracle_ok = (
                oracle_basis_bps is not None
                and abs(float(oracle_basis_bps)) <= self.max_oracle_basis_bps
            )
            topup_price = None
            fillable = False
            if our_ask is not None and 0.0 < float(our_ask) <= self.max_topup_price:
                topup_price = round(float(our_ask), 4)
                fillable = True
            # 2026-06-21 research-driven dimensions (see docs/Polymarket_5m_Final_Window_*):
            # the only +EV cohort is independent-feed-confirmed AND ask materially below
            # fair. Log the discount (gross upside if right), the below-fair flag, and the
            # combined confirmed-edge cohort so the settler can score THAT, not naive 0.95+.
            gross_win_cents = round(1.0 - topup_price, 4) if topup_price is not None else None
            below_fair = bool(fillable and topup_price is not None and topup_price <= self.below_fair_max)
            confirmed_edge = bool(oracle_ok and below_fair)
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "trade_id": None,            # standalone — not a held position
                "mode": "standalone",
                "market_id": market_id,
                "strategy": strategy,
                "window": window,
                "held_leg": None,
                "yes_mark": round(ym, 4),
                "our_side_mark": round(our_mark, 4),
                "detected_winning_side": leg,
                "oracle_basis_bps": round(float(oracle_basis_bps), 3) if oracle_basis_bps is not None else None,
                "oracle_ok": oracle_ok,
                "oracle_price": round(float(oracle_price), 4) if oracle_price is not None else None,
                "spot_price": round(float(spot_price), 4) if spot_price is not None else None,
                "oracle_age_s": round(float(oracle_age_s), 2) if oracle_age_s is not None else None,
                "mins_left": round(float(mins_left), 3),
                "topup_price": topup_price,
                "fillable": fillable,
                "gross_win_cents": gross_win_cents,
                "below_fair_max": self.below_fair_max,
                "below_fair": below_fair,
                "confirmed_edge": confirmed_edge,
                "winner_mark_threshold": self.winner_mark_threshold,
                "market_end_at": str(market_end_at) if market_end_at is not None else None,
                "settled": False,
            }
            self._write(row)
            self._logged.add(key)
        except Exception:
            logger.debug("final_window_topup_shadow.observe_market failed (ignored)", exc_info=True)

    def _write(self, row: Dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:
            logger.debug("final_window_topup_shadow write failed (ignored)", exc_info=True)
