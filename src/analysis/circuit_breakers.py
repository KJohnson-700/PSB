"""Cross-strategy circuit breakers for crypto up/down entries.

These breakers halt *new entries* on one side of the book. They never force
exits, because the exit manager still owns position liquidation.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

STOP_EXIT_REASONS = frozenset({"updown_stop_loss", "stop_loss"})
BUY_SIDES = frozenset({"BUY_YES", "BUY_NO"})
CRYPTO_UPDOWN_STRATEGIES = frozenset(
    {
        "bitcoin",
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "doge_macro",
        "bnb_macro",
    }
)


@dataclass(frozen=True)
class BreakerDecision:
    allowed: bool
    reason: str = "OK"
    side: str = ""
    active_until: float = 0.0


class CircuitBreakerManager:
    """Side-specific halt manager for correlated crypto up/down risk."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.correlation_cfg = self.config.get("correlation_stop_halt") or {}
        self.reversal_cfg = self.config.get("reversal_halt") or {}
        self._stop_events: Deque[Tuple[float, str]] = deque()
        self._btc_prices: Deque[Tuple[float, float]] = deque()
        self._halts: Dict[str, Tuple[float, str]] = {}

    @staticmethod
    def normalize_action(action: Any) -> str:
        raw = str(action or "").upper().strip()
        if raw in BUY_SIDES:
            return raw
        if raw in {"YES", "BUY"}:
            return "BUY_YES"
        if raw in {"NO"}:
            return "BUY_NO"
        return raw

    @staticmethod
    def action_from_position(pos: Any) -> str:
        outcome = str(getattr(pos, "outcome", "") or "").upper().strip()
        if outcome == "NO":
            return "BUY_NO"
        if outcome == "YES":
            return "BUY_YES"
        entry_leg = str(getattr(pos, "entry_leg", "") or "").upper().strip()
        if entry_leg == "NO":
            return "BUY_NO"
        if entry_leg == "YES":
            return "BUY_YES"
        return ""

    def record_btc_price(self, btc_price: Optional[float], *, now: Optional[float] = None) -> None:
        if btc_price is None:
            return
        try:
            price = float(btc_price)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        ts = float(now if now is not None else time.time())
        self._btc_prices.append((ts, price))
        lookback_sec = float(self.reversal_cfg.get("lookback_sec", 300) or 300)
        max_age = max(lookback_sec * 3, 900.0)
        while self._btc_prices and ts - self._btc_prices[0][0] > max_age:
            self._btc_prices.popleft()

    def record_exit(
        self,
        *,
        reason: str,
        action: str,
        now: Optional[float] = None,
    ) -> Optional[BreakerDecision]:
        """Record a stop-loss exit and trigger same-side halts when thresholds hit."""
        if str(reason or "") not in STOP_EXIT_REASONS:
            return None
        side = self.normalize_action(action)
        if side not in BUY_SIDES:
            return None
        cfg = self.correlation_cfg
        if not bool(cfg.get("enabled", False)):
            return None

        ts = float(now if now is not None else time.time())
        self._stop_events.append((ts, side))
        fast_window = float(cfg.get("window_sec", 60) or 60)
        slow_window = float(cfg.get("slow_window_sec", 900) or 900)
        prune_window = max(fast_window, slow_window)
        while self._stop_events and ts - self._stop_events[0][0] > prune_window:
            self._stop_events.popleft()

        fast_threshold = int(cfg.get("stops_threshold", 3) or 3)
        slow_threshold = int(cfg.get("slow_stops_threshold", 6) or 6)
        pause_minutes = float(cfg.get("pause_minutes", 15) or 15)

        fast_count = self._count_stops(side, ts, fast_window)
        if fast_count >= fast_threshold:
            return self._halt(
                side,
                pause_minutes,
                f"correlation_stop_halt: {fast_count} {side} stops in {int(fast_window)}s",
                ts,
            )

        if bool(cfg.get("slow_mode_enabled", True)):
            slow_count = self._count_stops(side, ts, slow_window)
            if slow_count >= slow_threshold:
                return self._halt(
                    side,
                    pause_minutes,
                    f"correlation_stop_slow_halt: {slow_count} {side} stops in {int(slow_window)}s",
                    ts,
                )
        return None

    def can_enter(
        self,
        *,
        action: str,
        active_positions: Iterable[Any],
        btc_price: Optional[float] = None,
        now: Optional[float] = None,
    ) -> BreakerDecision:
        """Return whether a new entry on ``action`` is currently allowed."""
        side = self.normalize_action(action)
        if side not in BUY_SIDES:
            return BreakerDecision(True)
        ts = float(now if now is not None else time.time())
        self.record_btc_price(btc_price, now=ts)

        halt = self._active_halt(side, ts)
        if halt is not None:
            until, reason = halt
            return BreakerDecision(False, reason, side, until)

        triggered = self._maybe_trigger_reversal(side, active_positions, ts)
        if triggered is not None:
            return triggered

        return BreakerDecision(True)

    def snapshot(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        ts = float(now if now is not None else time.time())
        active = {
            side: {"until": until, "seconds_remaining": max(0.0, until - ts), "reason": reason}
            for side, (until, reason) in self._halts.items()
            if until > ts
        }
        return {"active_halts": active, "stop_events": len(self._stop_events), "btc_prices": len(self._btc_prices)}

    def _count_stops(self, side: str, ts: float, window_sec: float) -> int:
        same_side_only = bool(self.correlation_cfg.get("same_side_only", True))
        return sum(
            1
            for event_ts, event_side in self._stop_events
            if ts - event_ts <= window_sec and (event_side == side or not same_side_only)
        )

    def _active_halt(self, side: str, ts: float) -> Optional[Tuple[float, str]]:
        halt = self._halts.get(side)
        if halt is None:
            return None
        until, reason = halt
        if until <= ts:
            self._halts.pop(side, None)
            return None
        return halt

    def _halt(self, side: str, pause_minutes: float, reason: str, ts: float) -> BreakerDecision:
        until = ts + pause_minutes * 60.0
        old_until, _ = self._halts.get(side, (0.0, ""))
        if old_until > until:
            until = old_until
        self._halts[side] = (until, reason)
        logger.warning("CIRCUIT_BREAKER side=%s reason=%s pause_minutes=%.1f", side, reason, pause_minutes)
        return BreakerDecision(False, reason, side, until)

    def _maybe_trigger_reversal(
        self,
        side: str,
        active_positions: Iterable[Any],
        ts: float,
    ) -> Optional[BreakerDecision]:
        cfg = self.reversal_cfg
        if not bool(cfg.get("enabled", False)):
            return None
        position_threshold = int(cfg.get("position_threshold", 5) or 5)
        counts = {"BUY_YES": 0, "BUY_NO": 0}
        for pos in active_positions:
            strategy = str(getattr(pos, "strategy", "") or "").lower().strip()
            if strategy and strategy not in CRYPTO_UPDOWN_STRATEGIES:
                continue
            pos_side = self.action_from_position(pos)
            if pos_side in counts:
                counts[pos_side] += 1
        if counts.get(side, 0) < position_threshold:
            return None

        btc_return = self._btc_return(ts, float(cfg.get("lookback_sec", 300) or 300))
        if btc_return is None:
            return None
        threshold = float(cfg.get("btc_pct_threshold", 0.003) or 0.003)
        adverse = (side == "BUY_NO" and btc_return >= threshold) or (
            side == "BUY_YES" and btc_return <= -threshold
        )
        if not adverse:
            return None
        pause_minutes = float(cfg.get("pause_minutes", 10) or 10)
        return self._halt(
            side,
            pause_minutes,
            (
                f"reversal_halt: btc_return={btc_return:+.3%} over "
                f"{int(float(cfg.get('lookback_sec', 300) or 300))}s with {counts[side]} {side} positions"
            ),
            ts,
        )

    def _btc_return(self, ts: float, lookback_sec: float) -> Optional[float]:
        if len(self._btc_prices) < 2:
            return None
        latest_ts, latest_price = self._btc_prices[-1]
        if latest_ts > ts or latest_price <= 0:
            return None
        target_ts = ts - lookback_sec
        base_price: Optional[float] = None
        for event_ts, price in self._btc_prices:
            if event_ts <= target_ts:
                base_price = price
            else:
                break
        if base_price is None:
            return None
        if base_price <= 0:
            return None
        return (latest_price - base_price) / base_price
