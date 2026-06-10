"""Uncensored exit-excursion shadow logger (logging-only; never affects trading).

WHY
---
Live MFE/MAE (max favorable / adverse excursion) are CENSORED at our real exit:
once the TP or stop fires, the position is dropped from active_positions and we
stop sampling the market. So we never observe how much higher a winner would have
run (blocks "raise the TP?") or whether a stopped trade would have recovered to a
deeper-but-temporary dip level (blocks "what's the right stop WIDTH?").

This tracker keeps sampling each market's mark to window-close AFTER our exit and
records the TRUE full-path MFE/MAE alongside the censored exit values. Re-running
the bracket sim on the uncensored peaks/troughs answers both questions per lane,
without flying blind.

Pure instrumentation: it reads prices the fast-exit loop already fetches, writes one
jsonl row per trade at window-close, and is wrapped so it can never raise into the
trading loop. Mirrors live_testing.check_exits exactly: our-side token price is
(1 - yes) for a NO leg, else yes; excursion = (token_price - entry) / entry.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# Hard cap on how long we keep shadow-watching a market after our exit, in case an
# end_date is missing/implausible (defensive; real windows are 5m/15m/1h).
_MAX_WATCH_MINUTES = 75.0


class ExitExcursionShadow:
    def __init__(self, path: str = "data/calibration/exit_excursion_shadow.jsonl",
                 enabled: bool = True, max_watch: int = 2000):
        self.path = path
        self.enabled = enabled
        self.max_watch = max_watch
        self._watch: Dict[str, Dict[str, Any]] = {}

    # -- registration -------------------------------------------------------
    def register(self, *, trade_id: str, market_id: str, entry_price: float,
                 entry_leg: str, opened_at: Any, end_date: Any,
                 exit_mae: Optional[float], exit_mfe: Optional[float],
                 exit_pnl_pct: Optional[float], strategy: str = "", window: str = "",
                 action: str = "", yes_token: str = "") -> None:
        """Start shadow-watching a market after our real exit. Never raises."""
        if not self.enabled:
            return
        try:
            if not market_id or not trade_id or entry_price is None or entry_price <= 0:
                return
            if len(self._watch) >= self.max_watch:
                return  # backpressure; drop silently rather than grow unbounded
            leg = entry_leg if entry_leg in ("YES", "NO") else "YES"
            deadline = self._coerce_deadline(end_date)
            # Seed full excursions from the censored exit values so uncensored is
            # never tighter than what we already observed pre-exit.
            f_mfe = float(exit_mfe) if exit_mfe is not None else 0.0
            f_mae = float(exit_mae) if exit_mae is not None else 0.0
            self._watch[trade_id] = {
                "trade_id": trade_id, "market_id": market_id,
                "yes_token": yes_token or "",
                "entry_price": float(entry_price), "entry_leg": leg,
                "strategy": strategy, "window": window, "action": action,
                "exit_mae": exit_mae, "exit_mfe": exit_mfe,
                "exit_pnl_pct": exit_pnl_pct,
                "full_mfe": f_mfe, "full_mae": f_mae,
                "n_samples": 0, "deadline": deadline,
                "opened_at": self._iso(opened_at),
            }
        except Exception as e:  # pragma: no cover - never break the trade loop
            logger.debug("exit-excursion register failed: %s", e)

    # -- per-cycle update ---------------------------------------------------
    def watched_market_ids(self) -> Set[str]:
        return {w["market_id"] for w in self._watch.values()}

    def watched_tokens(self) -> Dict[str, str]:
        """market_id -> yes_token for markets we still shadow-watch (for price fetch)."""
        return {w["market_id"]: w["yes_token"] for w in self._watch.values() if w.get("yes_token")}

    def update(self, market_prices: Dict[str, float]) -> None:
        """Update full-path MFE/MAE from current YES prices. Never raises."""
        if not self.enabled or not self._watch:
            return
        try:
            for w in self._watch.values():
                yes = market_prices.get(w["market_id"])
                if yes is None:
                    continue
                token = (1.0 - float(yes)) if w["entry_leg"] == "NO" else float(yes)
                entry = w["entry_price"]
                if entry <= 0:
                    continue
                exc = (token - entry) / entry
                if exc > w["full_mfe"]:
                    w["full_mfe"] = exc
                if exc < w["full_mae"]:
                    w["full_mae"] = exc
                w["n_samples"] += 1
        except Exception as e:  # pragma: no cover
            logger.debug("exit-excursion update failed: %s", e)

    # -- flush expired ------------------------------------------------------
    def flush(self, now: Optional[datetime] = None) -> int:
        """Emit + drop watches past their window-close. Returns count flushed."""
        if not self.enabled or not self._watch:
            return 0
        now = now or datetime.now(timezone.utc)
        done = [tid for tid, w in self._watch.items() if now >= w["deadline"]]
        n = 0
        for tid in done:
            w = self._watch.pop(tid, None)
            if w is None:
                continue
            try:
                self._emit(w, now)
                n += 1
            except Exception as e:  # pragma: no cover
                logger.debug("exit-excursion emit failed: %s", e)
        return n

    # -- internals ----------------------------------------------------------
    def _emit(self, w: Dict[str, Any], now: datetime) -> None:
        rec = {
            "trade_id": w["trade_id"], "strategy": w["strategy"],
            "window": w["window"], "action": w["action"],
            "entry_price": w["entry_price"], "entry_leg": w["entry_leg"],
            # censored (at our real exit) vs uncensored (full path to window close)
            "exit_mae_pct": w["exit_mae"], "exit_mfe_pct": w["exit_mfe"],
            "exit_pnl_pct": w["exit_pnl_pct"],
            "full_mae_pct": round(w["full_mae"], 6),
            "full_mfe_pct": round(w["full_mfe"], 6),
            "n_shadow_samples": w["n_samples"],
            "opened_at": w["opened_at"],
            "settled_at": self._iso(now),
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    @staticmethod
    def _coerce_deadline(end_date: Any) -> datetime:
        # Anchor the watch horizon to registration time (UTC-aware now), NEVER to a
        # position timestamp — pos.opened_at/end_date can be naive LOCAL time, and
        # treating that as UTC put the deadline ~tz-offset hours in the past, which
        # flushed every watch on its first cycle (the 2026-06-10 1-sample bug).
        now = datetime.now(timezone.utc)
        cap = now + timedelta(minutes=_MAX_WATCH_MINUTES)
        ed = ExitExcursionShadow._aware(end_date)
        # Only trust end_date if it is in the FUTURE relative to now (a sane window
        # close); otherwise it's naive/stale — fall back to the now-based cap.
        if ed is None or ed <= now:
            return cap
        return min(ed, cap)

    @staticmethod
    def _aware(dt: Any) -> Optional[datetime]:
        if dt is None:
            return None
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except Exception:
                return None
        if isinstance(dt, datetime):
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return None

    @staticmethod
    def _iso(dt: Any) -> Optional[str]:
        a = ExitExcursionShadow._aware(dt)
        return a.isoformat() if a else None
