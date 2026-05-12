"""Append-only log of AI decisions made by live trading.

Purpose: enable deterministic backtest replay of AI gates (Option A from the
2026-05-12 refactor planning session). Live writes one JSONL record per
AIAgent.evaluate_trade_decision call; backtest reads and looks up decisions
by (market_id, strategy_hint, quant_action).

File layout:
    data/ai_call_log/YYYY-MM-DD.jsonl

Record schema (one JSON object per line):
{
  "ts":              ISO-8601 UTC timestamp
  "market_id":       Polymarket market id
  "strategy_hint":   "bitcoin" | "sol_macro" | "eth_macro" | "xrp_macro" | "hype_macro"
  "quant_action":    "BUY_YES" | "BUY_NO"
  "quant_edge":      float
  "quant_confidence":float
  "quant_threshold": float
  "context_hash":    SHA-256 of (market_question + quant fields) — stable lookup key
  "window_minutes":  int | null  -- 5/15/30, when known (added 2026-05-12)
  "window_open_utc": ISO-8601 | null -- start of the up/down window (UTC), when known
  "approved":        bool
  "ai_action":       AIDecision.action
  "ai_confidence":   float
  "ai_estimated_probability": float | null
  "ai_edge":         float | null
  "ai_reason":       string (e.g. "direct_ai_hold", "approved")
  "ai_source":       AIDecision.source
}

Operator notes:
- The log writer is opt-in via `ai.call_log_enabled` in config (defaults to True).
- Writes are best-effort: any IO error is swallowed with a logger.warning so a
  log-disk issue can never block a live entry decision.
- Records are stable: pre-existing files are append-only; the backtest can
  safely replay any historical day.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path("data/ai_call_log")


def context_hash(
    *,
    market_question: str,
    market_id: str,
    strategy_hint: str,
    quant_action: str,
    quant_edge: float,
    quant_confidence: float,
) -> str:
    """Stable hash for backtest replay lookup. SHA-256 of normalized inputs.

    Deliberately excludes timestamps and floating-point fields beyond what
    identifies the decision context — `quant_edge`/`quant_confidence` are
    rounded to 4 dp before hashing so tiny float-noise doesn't break lookups.
    """
    payload = "|".join([
        market_id,
        strategy_hint,
        quant_action,
        f"{round(float(quant_edge), 4):.4f}",
        f"{round(float(quant_confidence), 4):.4f}",
        market_question.strip(),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def append_record(
    *,
    market_question: str,
    market_id: str,
    strategy_hint: str,
    quant_action: str,
    quant_edge: float,
    quant_confidence: float,
    quant_threshold: float,
    approved: bool,
    ai_action: str,
    ai_confidence: float,
    ai_estimated_probability: Optional[float],
    ai_edge: Optional[float],
    ai_reason: str,
    ai_source: str,
    window_minutes: Optional[int] = None,
    window_open_utc: Optional[datetime] = None,
    log_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> None:
    """Best-effort append of one decision record. Never raises."""
    try:
        dir_path = Path(log_dir) if log_dir is not None else _DEFAULT_DIR
        dir_path.mkdir(parents=True, exist_ok=True)
        ts = (now or datetime.now(timezone.utc)).isoformat()
        day = ts[:10]  # YYYY-MM-DD
        path = dir_path / f"{day}.jsonl"
        record: dict[str, Any] = {
            "ts": ts,
            "market_id": market_id,
            "strategy_hint": strategy_hint,
            "quant_action": quant_action,
            "quant_edge": float(quant_edge),
            "quant_confidence": float(quant_confidence),
            "quant_threshold": float(quant_threshold),
            "context_hash": context_hash(
                market_question=market_question,
                market_id=market_id,
                strategy_hint=strategy_hint,
                quant_action=quant_action,
                quant_edge=quant_edge,
                quant_confidence=quant_confidence,
            ),
            "window_minutes": int(window_minutes) if window_minutes is not None else None,
            "window_open_utc": (
                window_open_utc.astimezone(timezone.utc).isoformat()
                if window_open_utc is not None else None
            ),
            "approved": bool(approved),
            "ai_action": ai_action,
            "ai_confidence": float(ai_confidence),
            "ai_estimated_probability": (
                float(ai_estimated_probability) if ai_estimated_probability is not None else None
            ),
            "ai_edge": float(ai_edge) if ai_edge is not None else None,
            "ai_reason": ai_reason,
            "ai_source": ai_source,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as exc:  # noqa: BLE001 — never let logging block a trade
        logger.warning("ai_call_log append failed: %s", exc)
