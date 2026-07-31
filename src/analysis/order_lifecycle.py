"""Live order-lifecycle records (2026-07-30, Codex paper-vs-live sweep, data-loop A).

The maker-first hybrid entry/exit path (clob_client.place_entry_order /
place_exit_order) makes several decisions per order — post as maker GTC, wait,
reconcile matched size, keep a partial, or cross the remainder to a FAK taker — that
were only visible in scattered log lines. This records ONE structured row per resolved
order to ``data/calibration/order_lifecycle.jsonl`` so the live maker/FAK path is
auditable: which path was taken, how much filled maker vs taker, submit→resolve
latency, and why a fallback fired.

LIVE-only in practice (paper uses the dry_run fresh-fill branch, not the maker legs).
Fail-safe: never raises, never affects execution. Gated by
``trading.order_lifecycle_log_enabled`` (default True).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG = _ROOT / "data" / "calibration" / "order_lifecycle.jsonl"


def _num(v: Any) -> Optional[float]:
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return None


def record(
    enabled: bool,
    *,
    kind: str,                       # "entry" | "exit"
    path: str,                       # maker_full | maker_partial_keep | maker_zero_cross_fak | ...
    token_id: str,
    side: str,
    requested_price: Any,
    requested_size: Any,
    order: Any = None,               # the resolved Order (or None when skipped)
    submit_monotonic: Optional[float] = None,
    matched: Any = None,             # reconciled matched share qty (maker leg)
    fallback_reason: Optional[str] = None,
    window: Optional[str] = None,
    market_id: Optional[str] = None,
    log_path: Path = DEFAULT_LOG,
) -> None:
    """Append one order-lifecycle row. No-op when ``enabled`` is false. Never raises."""
    if not enabled:
        return
    try:
        latency_ms = None
        if submit_monotonic is not None:
            latency_ms = round((time.monotonic() - submit_monotonic) * 1000.0, 1)
        order_id = getattr(order, "order_id", None) if order is not None else None
        filled_size = _num(getattr(order, "filled_size", None)) if order is not None else None
        # Order has no dedicated avg-fill field; the order price is the fill proxy
        # (limit for maker, marketable-limit for FAK). Prefer an explicit avg if present.
        filled_avg = None
        if order is not None:
            filled_avg = _num(getattr(order, "avg_fill_price", getattr(order, "price", None)))
        status = getattr(order, "status", None) if order is not None else None
        status = status.name if hasattr(status, "name") else (str(status) if status else None)
        _matched = _num(matched)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "path": path,
            "order_id": order_id,
            "token_id": str(token_id or ""),
            "side": str(side or ""),
            "window": window,
            "market_id": market_id,
            "requested_price": _num(requested_price),
            "requested_size": _num(requested_size),
            "matched_size": _matched,
            "filled_size": filled_size,
            "filled_avg_price": filled_avg,
            "status": status,
            "fallback_reason": fallback_reason,
            "latency_ms": latency_ms,
            "partial": (
                bool(filled_size is not None and requested_size is not None
                     and 0.0 < filled_size < float(requested_size) - 1e-6)
            ),
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception as exc:  # noqa: BLE001 — telemetry must never break execution
        logger.debug("order_lifecycle record failed (ignored): %s", exc)
