"""Deterministic AI replay agent for backtest use (Option A step 2).

Reads decision records written by live (`data/ai_call_log/*.jsonl` via
`src.analysis.ai_call_log.append_record`) and exposes the same
`evaluate_trade_decision` contract as `AIAgent`, returning the recorded
decision when a match is found.

Design:
- Index keyed by `context_hash` (the same hash live computed at write time).
- Fallback secondary index by `(market_id, strategy_hint, quant_action)` so
  near-miss lookups can still find a decision if `edge`/`confidence` drifted
  slightly between live and replay (e.g. a strategy tweak post-recording).
- Misses are reported via a stats counter — never raise, never block the
  backtest.

What it does NOT do:
- Run any model. No API calls, no network IO.
- Synthesize decisions when no record exists. The backtest decides what to
  do on a miss (treat as "approved + matching quant action" for parity with
  pre-AI behavior, or treat as a skip, etc.).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from src.analysis import ai_call_log
from src.analysis.ai_agent import AIDecision

logger = logging.getLogger(__name__)


@dataclass
class ReplayStats:
    lookups: int = 0
    hits_by_hash: int = 0
    hits_by_window: int = 0
    hits_by_fallback: int = 0
    misses: int = 0
    records_loaded: int = 0


@dataclass
class ReplayRecord:
    """In-memory representation of one ai_call_log entry."""
    context_hash: str
    market_id: str
    strategy_hint: str
    quant_action: str
    approved: bool
    ai_action: str
    ai_confidence: float
    ai_estimated_probability: Optional[float]
    ai_edge: Optional[float]
    ai_reason: str
    ai_source: str
    window_minutes: Optional[int] = None
    window_open_utc: Optional[str] = None  # ISO-8601 string, normalized to minute

    @classmethod
    def from_json(cls, obj: dict) -> "ReplayRecord":
        wo = obj.get("window_open_utc")
        if wo:
            # Normalize to minute granularity for stable matching across float-second noise
            wo = wo[:16]  # "YYYY-MM-DDTHH:MM"
        return cls(
            context_hash=obj["context_hash"],
            market_id=obj["market_id"],
            strategy_hint=obj["strategy_hint"],
            quant_action=obj["quant_action"],
            approved=bool(obj["approved"]),
            ai_action=obj["ai_action"],
            ai_confidence=float(obj["ai_confidence"]),
            ai_estimated_probability=obj.get("ai_estimated_probability"),
            ai_edge=obj.get("ai_edge"),
            ai_reason=obj["ai_reason"],
            ai_source=obj["ai_source"],
            window_minutes=obj.get("window_minutes"),
            window_open_utc=wo,
        )

    def to_decision(self) -> AIDecision:
        return AIDecision(
            approved=self.approved,
            action=self.ai_action,
            confidence=self.ai_confidence,
            estimated_probability=self.ai_estimated_probability,
            edge=self.ai_edge,
            reason=f"replay:{self.ai_reason}",
            source=f"replay:{self.ai_source}",
        )


class AIReplayAgent:
    """Reads ai_call_log records and replays decisions deterministically."""

    def __init__(self, log_dir: Path | str = "data/ai_call_log") -> None:
        self.log_dir = Path(log_dir)
        self._by_hash: Dict[str, ReplayRecord] = {}
        self._by_window: Dict[Tuple[str, str, int, str], ReplayRecord] = {}
        self._by_fallback: Dict[Tuple[str, str, str], List[ReplayRecord]] = {}
        self.stats = ReplayStats()

    @property
    def records_loaded(self) -> int:
        return self.stats.records_loaded

    def load(self, days: Optional[Iterable[str]] = None) -> "AIReplayAgent":
        """Load records from the log dir. If `days` is None, load all *.jsonl files.

        `days` is a list of YYYY-MM-DD strings; only those files are read.
        Returns self for chaining.
        """
        if not self.log_dir.exists():
            logger.warning("AIReplayAgent: log dir %s does not exist", self.log_dir)
            return self

        if days is None:
            files = sorted(self.log_dir.glob("*.jsonl"))
        else:
            files = [self.log_dir / f"{d}.jsonl" for d in days]

        for path in files:
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        rec = ReplayRecord.from_json(obj)
                    except (json.JSONDecodeError, KeyError, ValueError) as exc:
                        logger.warning("AIReplayAgent skipping bad record in %s: %s", path, exc)
                        continue
                    self._by_hash[rec.context_hash] = rec
                    key = (rec.market_id, rec.strategy_hint, rec.quant_action)
                    self._by_fallback.setdefault(key, []).append(rec)
                    if rec.window_minutes is not None and rec.window_open_utc:
                        wkey = (
                            rec.strategy_hint, rec.quant_action,
                            int(rec.window_minutes), rec.window_open_utc,
                        )
                        # Last-writer-wins: replay picks the most recent decision for a
                        # window (in practice live re-decides only on transient retries).
                        self._by_window[wkey] = rec
                    self.stats.records_loaded += 1
        return self

    def lookup(
        self,
        *,
        market_question: str,
        market_id: str,
        strategy_hint: str,
        quant_action: str,
        quant_edge: float,
        quant_confidence: float,
        window_minutes: Optional[int] = None,
        window_open_utc: Optional[str] = None,
    ) -> Optional[ReplayRecord]:
        """Return the recorded decision for these inputs, or None on miss.

        Lookup priority:
        1. Exact context_hash match (live and replay see same market_id + quant)
        2. Window match: (strategy_hint, quant_action, window_minutes, window_open_utc[:16])
           — backtest doesn't know the real market_id, so this is the primary
           replay path for backtests
        3. Secondary by (market_id, strategy_hint, quant_action)
        """
        self.stats.lookups += 1
        h = ai_call_log.context_hash(
            market_question=market_question,
            market_id=market_id,
            strategy_hint=strategy_hint,
            quant_action=quant_action,
            quant_edge=quant_edge,
            quant_confidence=quant_confidence,
        )
        if h in self._by_hash:
            self.stats.hits_by_hash += 1
            return self._by_hash[h]

        if window_minutes is not None and window_open_utc:
            wkey = (strategy_hint, quant_action, int(window_minutes), window_open_utc[:16])
            if wkey in self._by_window:
                self.stats.hits_by_window += 1
                return self._by_window[wkey]

        fb = self._by_fallback.get((market_id, strategy_hint, quant_action))
        if fb:
            self.stats.hits_by_fallback += 1
            return fb[-1]
        self.stats.misses += 1
        return None

    def evaluate_sync(
        self,
        *,
        market_question: str = "",
        market_id: str = "",
        strategy_hint: str,
        quant_action: str,
        quant_edge: float,
        quant_confidence: float,
        window_minutes: Optional[int] = None,
        window_open_utc: Optional[str] = None,
    ) -> AIDecision:
        """Sync version for callers that don't have an event loop (backtest run loop).

        The async `evaluate_trade_decision` exists only to match the AIAgent
        interface signature; replay is pure in-memory lookup with no IO, so
        this sync facade is the right API when there's no asyncio context.
        """
        rec = self.lookup(
            market_question=market_question,
            market_id=market_id,
            strategy_hint=strategy_hint,
            quant_action=quant_action,
            quant_edge=quant_edge,
            quant_confidence=quant_confidence,
            window_minutes=window_minutes,
            window_open_utc=window_open_utc,
        )
        if rec is None:
            return AIDecision(
                approved=False, action="SKIP", confidence=0.0,
                estimated_probability=None, edge=None,
                reason="replay_miss", source="replay_miss",
            )
        return rec.to_decision()

    # -- AIAgent.evaluate_trade_decision-compatible facade ---------------------
    def is_available(self) -> bool:
        return self.records_loaded > 0

    def decision_layer_enabled(self) -> bool:
        return True

    async def evaluate_trade_decision(  # noqa: D401 — matches AIAgent signature
        self,
        *,
        market_question: str,
        market_description: str,  # unused at replay time
        current_yes_price: float,  # unused at replay time
        market_id: str,
        strategy_hint: str,
        quant_action: str,
        quant_edge: float,
        quant_confidence: float,
        quant_threshold: float,  # unused at replay time
        require_shadow_portfolio: Optional[bool] = None,  # unused
        window_minutes: Optional[int] = None,
        window_open_utc: Optional[str] = None,
    ) -> AIDecision:
        rec = self.lookup(
            market_question=market_question,
            market_id=market_id,
            strategy_hint=strategy_hint,
            quant_action=quant_action,
            quant_edge=quant_edge,
            quant_confidence=quant_confidence,
            window_minutes=window_minutes,
            window_open_utc=window_open_utc,
        )
        if rec is None:
            # No record exists for this context. Return a special "miss"
            # AIDecision so callers can treat it explicitly (e.g. skip the
            # trade, or fall through to quant-only behavior).
            return AIDecision(
                approved=False,
                action="SKIP",
                confidence=0.0,
                estimated_probability=None,
                edge=None,
                reason="replay_miss",
                source="replay_miss",
            )
        return rec.to_decision()
