"""
Async-decoupled AI decision broker.

Removes the AI provider call from the trading cycle's critical path. Strategies
enqueue a PendingDecision during a scan, immediately bail that candidate, and a
background worker resolves it against the AI agent. On the next cycle, the
strategy looks up the resolved decision via ``get_resolved`` — which runs a
strict invalidation check (price drift, action flip, edge sign flip, position
now held, market closed, age) — and either consumes it to emit a signal or
re-enqueues.

Design notes
------------
* Single worker task. The AIAgent rate-limiter serialises provider calls anyway,
  so parallel workers buy nothing. Easier reasoning, no contention on the
  decisions dict.
* All state is in-memory and advisory. Decisions live <120s; losing them on
  restart is acceptable — strategies re-enqueue naturally on the next cycle.
* Persistence is audit-only via JSONL (``pending_ai_decisions.jsonl``).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DecisionKey = Tuple[str, str, str, str]  # (strategy, market_id, lane_id, action)


# State constants.
STATE_PENDING = "PENDING"
STATE_INFLIGHT = "IN_FLIGHT"
STATE_RESOLVED = "RESOLVED"
STATE_FAILED = "FAILED"
STATE_EXPIRED = "EXPIRED"
STATE_CONSUMED = "CONSUMED"


@dataclass
class PendingDecision:
    """Everything needed to (a) make the AI call later and (b) detect that the
    market context has drifted before acting on the resolved decision."""

    key: DecisionKey
    state: str
    created_at: float
    cycle_enqueued: int

    # Snapshot for invalidation.
    yes_price_at_enqueue: float
    edge_sign: int
    action: str

    # AI call payload.
    market_question: str
    market_description: str
    current_yes_price: float
    edge: float
    confidence: float
    estimated_prob: Optional[float]
    raw_est_prob: Optional[float]
    quant_threshold: float
    require_shadow_portfolio: bool

    # Marginal lane contract: when True the AI is veto-only (admit unless the AI
    # confidently opposes) — MUST be carried so the async path matches the
    # synchronous gate's veto_only=... and doesn't silently make the lane stricter.
    veto_only: bool = False

    # Materialization context — strategy reads these post-resolve to build the
    # signal without recomputing indicators.
    htf_bias: Optional[str] = None
    primary_htf_bias: Optional[str] = None
    macro_trend: Optional[str] = None
    btc_1h_regime: Optional[str] = None
    indicators: Dict[str, Any] = field(default_factory=dict)
    composite: Optional[Dict[str, Any]] = None
    oracle_validation: Optional[Dict[str, Any]] = None
    reason_parts: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    # Resolution.
    ai_decision: Optional[Any] = None  # AIDecision; untyped here to avoid import
    error: Optional[str] = None
    resolved_at: Optional[float] = None

    @property
    def strategy(self) -> str:
        return self.key[0]

    @property
    def market_id(self) -> str:
        return self.key[1]

    @property
    def lane_id(self) -> str:
        return self.key[2]

    def age_sec(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.created_at


@dataclass
class InvalidationResult:
    """Returned by get_resolved to communicate why a decision was rejected."""
    decision: Optional[Any]  # AIDecision when usable
    reason: Optional[str]    # set when decision is None for a non-trivial reason


class AIDecisionBroker:
    """In-memory broker decoupling AI calls from the trading cycle.

    Usage:
        broker = AIDecisionBroker(ai_agent=agent)
        await broker.start()
        # in strategy scan loop:
        resolved = broker.get_resolved(key, pending_snapshot)
        if resolved is None:
            broker.enqueue(pending_snapshot); continue
        # else build signal from resolved
        ...
        # in cycle teardown:
        broker.sweep_expired(open_position_ids)
        await broker.stop()  # on shutdown
    """

    def __init__(
        self,
        ai_agent: Any,
        *,
        max_decision_age_sec: float = 120.0,
        max_pending_decisions: int = 24,
        price_drift_threshold: float = 0.03,
        cycle_counter_ref: Optional[Callable[[], int]] = None,
        log_path: Optional[Path] = None,
        log_jsonl: bool = True,
    ) -> None:
        self.ai_agent = ai_agent
        self.max_decision_age_sec = float(max_decision_age_sec)
        self.max_pending_decisions = int(max_pending_decisions)
        self.price_drift_threshold = float(price_drift_threshold)
        self._cycle_counter_ref = cycle_counter_ref or (lambda: 0)
        self._log_path = log_path
        self._log_jsonl = bool(log_jsonl)

        self._decisions: Dict[DecisionKey, PendingDecision] = {}
        self._queue: collections.deque[DecisionKey] = collections.deque()
        self._signal_event: Optional[asyncio.Event] = None  # lazy
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False

        # Cumulative counters for stats / observability.
        self._counters: Dict[str, int] = {
            "enqueued": 0,
            "resolved": 0,
            "failed": 0,
            "consumed": 0,
            "expired": 0,
            "rejected_overflow": 0,
            "duplicate_enqueue_skipped": 0,
        }

    # ──────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._signal_event is None:
            self._signal_event = asyncio.Event()
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info(
            "AIDecisionBroker started (max_age=%.0fs, max_pending=%d, price_drift=%.3f)",
            self.max_decision_age_sec,
            self.max_pending_decisions,
            self.price_drift_threshold,
        )

    async def stop(self) -> None:
        self._running = False
        if self._signal_event is not None:
            self._signal_event.set()
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
        self._worker_task = None
        logger.info("AIDecisionBroker stopped")

    def worker_alive(self) -> bool:
        return self._worker_task is not None and not self._worker_task.done()

    # ──────────────────────────────────────────────────────────────
    # Public API: enqueue / get_resolved / sweep
    # ──────────────────────────────────────────────────────────────

    def enqueue(self, pending: PendingDecision) -> bool:
        """Enqueue a pending decision. Returns True if newly enqueued, False if
        skipped (duplicate or overflow)."""
        key = pending.key
        existing = self._decisions.get(key)
        if existing is not None and existing.state in (STATE_PENDING, STATE_INFLIGHT):
            self._counters["duplicate_enqueue_skipped"] += 1
            return False

        # Overflow: drop oldest PENDING (never IN_FLIGHT).
        if len(self._decisions) >= self.max_pending_decisions:
            self._evict_oldest_pending()

        pending.state = STATE_PENDING
        if pending.created_at <= 0:
            pending.created_at = time.time()
        pending.cycle_enqueued = self._cycle_counter_ref()
        self._decisions[key] = pending
        self._queue.append(key)
        self._counters["enqueued"] += 1
        self._log_transition(pending, "enqueued")

        if self._signal_event is not None:
            self._signal_event.set()
        return True

    def get_resolved(
        self,
        key: DecisionKey,
        *,
        current_yes_price: float,
        current_action: str,
        current_edge: float,
        open_position_ids: Optional[set[str]] = None,
        market_closed: bool = False,
    ) -> Optional[Any]:
        """Look up a resolved decision. Returns the AIDecision iff valid and
        not stale; otherwise returns None (caller should enqueue).

        Side effects: stale entries are removed; resolved entries are marked
        CONSUMED.
        """
        pd = self._decisions.get(key)
        if pd is None:
            return None

        # Terminal states.
        if pd.state == STATE_FAILED:
            self._log_transition(pd, "consumed_failed")
            del self._decisions[key]
            return None

        if pd.state in (STATE_PENDING, STATE_INFLIGHT):
            # Still in flight — nothing to consume.
            return None

        if pd.state != STATE_RESOLVED:
            # CONSUMED or EXPIRED already; treat as miss.
            return None

        now = time.time()
        age = pd.age_sec(now)
        reason: Optional[str] = None
        if age > self.max_decision_age_sec:
            reason = "age_exceeded"
        elif abs(current_yes_price - pd.yes_price_at_enqueue) > self.price_drift_threshold:
            reason = "price_drift"
        elif str(current_action) != pd.action:
            reason = "action_flip"
        else:
            cur_sign = 1 if current_edge >= 0 else -1
            if cur_sign != pd.edge_sign:
                reason = "edge_flip"
            elif open_position_ids and pd.market_id in open_position_ids:
                reason = "position_held"
            elif market_closed:
                reason = "market_closed"

        if reason is not None:
            pd.state = STATE_EXPIRED
            self._counters["expired"] += 1
            self._log_transition(pd, "expired", extra_reason=reason)
            del self._decisions[key]
            return None

        # Consume.
        pd.state = STATE_CONSUMED
        self._counters["consumed"] += 1
        self._log_transition(pd, "consumed")
        del self._decisions[key]
        return pd.ai_decision

    def sweep_expired(self, open_position_ids: Optional[set[str]] = None) -> None:
        """Once-per-cycle housekeeping. Removes EXPIRED entries (defensive — most
        are removed at get_resolved time), ages out stale RESOLVED entries, and
        invalidates PENDING entries whose markets are now held."""
        now = time.time()
        open_ids = open_position_ids or set()
        to_drop: List[DecisionKey] = []
        for key, pd in self._decisions.items():
            if pd.state == STATE_EXPIRED or pd.state == STATE_CONSUMED:
                to_drop.append(key)
                continue
            if pd.state == STATE_FAILED:
                # Drop if older than max age — give one cycle for the strategy
                # to read the failure and react.
                if pd.age_sec(now) > self.max_decision_age_sec:
                    to_drop.append(key)
                continue
            if pd.state == STATE_RESOLVED and pd.age_sec(now) > self.max_decision_age_sec:
                pd.state = STATE_EXPIRED
                self._counters["expired"] += 1
                self._log_transition(pd, "expired", extra_reason="age_sweep")
                to_drop.append(key)
                continue
            if pd.state == STATE_PENDING and pd.market_id in open_ids:
                pd.state = STATE_EXPIRED
                self._counters["expired"] += 1
                self._log_transition(pd, "expired", extra_reason="position_held_pre_resolve")
                to_drop.append(key)
        for key in to_drop:
            self._decisions.pop(key, None)

    def stats(self) -> Dict[str, Any]:
        now = time.time()
        pending = inflight = resolved = 0
        oldest_age = 0.0
        for pd in self._decisions.values():
            if pd.state == STATE_PENDING:
                pending += 1
            elif pd.state == STATE_INFLIGHT:
                inflight += 1
            elif pd.state == STATE_RESOLVED:
                resolved += 1
            age = pd.age_sec(now)
            if age > oldest_age:
                oldest_age = age
        return {
            "pending": pending,
            "inflight": inflight,
            "resolved_alive": resolved,
            "resolved_total": self._counters["resolved"],
            "consumed": self._counters["consumed"],
            "expired": self._counters["expired"],
            "failed": self._counters["failed"],
            "rejected_overflow": self._counters["rejected_overflow"],
            "duplicate_enqueue_skipped": self._counters["duplicate_enqueue_skipped"],
            "oldest_age_sec": round(oldest_age, 2),
            "worker_alive": self.worker_alive(),
        }

    def reset(self) -> None:
        """Drop all in-memory state. Worker is left running."""
        self._decisions.clear()
        self._queue.clear()

    # ──────────────────────────────────────────────────────────────
    # Worker
    # ──────────────────────────────────────────────────────────────

    async def _worker_loop(self) -> None:
        assert self._signal_event is not None
        while self._running:
            try:
                await self._signal_event.wait()
                self._signal_event.clear()
                while self._queue and self._running:
                    key = self._queue.popleft()
                    pd = self._decisions.get(key)
                    if pd is None or pd.state != STATE_PENDING:
                        continue
                    await self._process_one(pd)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AIDecisionBroker worker iteration crashed; sleeping 1s")
                await asyncio.sleep(1.0)

    async def _process_one(self, pd: PendingDecision) -> None:
        pd.state = STATE_INFLIGHT
        self._log_transition(pd, "inflight")
        start = time.time()
        try:
            # AIAgent's circuit breaker — bail fast on outage.
            is_available = True
            try:
                is_available = bool(self.ai_agent.is_available())
            except Exception:
                is_available = True  # fall through; agent will signal failure
            if not is_available:
                pd.ai_decision = None
                pd.error = "ai_unavailable"
                pd.state = STATE_FAILED
                self._counters["failed"] += 1
                pd.resolved_at = time.time()
                self._log_transition(pd, "failed", extra_reason="ai_unavailable",
                                     latency_ms=int((time.time() - start) * 1000))
                return

            ai_decision = await self.ai_agent.evaluate_trade_decision(
                market_question=pd.market_question,
                market_description=pd.market_description,
                current_yes_price=pd.current_yes_price,
                market_id=pd.market_id,
                strategy_hint=pd.strategy,
                lane_id=pd.lane_id,
                quant_action=pd.action,
                quant_edge=pd.edge,
                quant_confidence=pd.confidence,
                quant_threshold=pd.quant_threshold,
                raw_probability=pd.raw_est_prob,
                post_calibration_probability=pd.estimated_prob,
                require_shadow_portfolio=pd.require_shadow_portfolio,
                veto_only=pd.veto_only,
            )
            pd.ai_decision = ai_decision
            if ai_decision is None:
                pd.state = STATE_FAILED
                pd.error = "ai_returned_none"
                self._counters["failed"] += 1
            else:
                pd.state = STATE_RESOLVED
                self._counters["resolved"] += 1
        except asyncio.CancelledError:
            pd.state = STATE_FAILED
            pd.error = "worker_cancelled"
            self._counters["failed"] += 1
            raise
        except Exception as exc:
            pd.state = STATE_FAILED
            pd.error = f"{type(exc).__name__}: {exc}"
            self._counters["failed"] += 1
        finally:
            pd.resolved_at = time.time()
            transition = "resolved" if pd.state == STATE_RESOLVED else "failed"
            self._log_transition(
                pd,
                transition,
                latency_ms=int((time.time() - start) * 1000),
            )

    # ──────────────────────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────────────────────

    def _evict_oldest_pending(self) -> None:
        # Walk the FIFO; the first PENDING we find is the oldest.
        for key in list(self._queue):
            pd = self._decisions.get(key)
            if pd is None:
                # Stale queue entry — clean up.
                try:
                    self._queue.remove(key)
                except ValueError:
                    pass
                continue
            if pd.state == STATE_PENDING:
                pd.state = STATE_EXPIRED
                self._counters["rejected_overflow"] += 1
                self._log_transition(pd, "rejected_overflow")
                try:
                    self._queue.remove(key)
                except ValueError:
                    pass
                self._decisions.pop(key, None)
                return
        # Nothing to evict (all IN_FLIGHT or RESOLVED). The new enqueue will
        # push us over the cap but worker drains quickly; accept it rather than
        # silently dropping the new (likely more relevant) entry.

    def _log_transition(
        self,
        pd: PendingDecision,
        transition: str,
        *,
        extra_reason: Optional[str] = None,
        latency_ms: Optional[int] = None,
    ) -> None:
        if not self._log_jsonl or self._log_path is None:
            return
        record = {
            "ts_utc": datetime.utcnow().isoformat() + "Z",
            "cycle": pd.cycle_enqueued,
            "transition": transition,
            "strategy": pd.strategy,
            "market_id": pd.market_id,
            "lane_id": pd.lane_id,
            "action": pd.action,
            "age_ms": int(pd.age_sec() * 1000),
            "yes_price": pd.current_yes_price,
            "yes_price_at_enqueue": pd.yes_price_at_enqueue,
            "edge": pd.edge,
            "state": pd.state,
        }
        if extra_reason is not None:
            record["reason"] = extra_reason
        if pd.error is not None and transition in ("failed", "consumed_failed"):
            record["error"] = pd.error
        if latency_ms is not None:
            record["latency_ms"] = latency_ms
        if pd.ai_decision is not None and transition in ("resolved", "consumed"):
            ad = pd.ai_decision
            record["ai_approved"] = getattr(ad, "approved", None)
            record["ai_reason"] = getattr(ad, "reason", None)
            record["ai_action"] = getattr(ad, "action", None)
            record["ai_confidence"] = getattr(ad, "confidence", None)
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception:
            # Logging is advisory; never crash the trading loop on disk issues.
            logger.debug("ai_broker JSONL write failed", exc_info=True)
