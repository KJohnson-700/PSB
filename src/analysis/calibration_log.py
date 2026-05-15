"""Phase 0 calibration log.

Appends one JSON object per closed trade to ``data/calibration/trades.jsonl``.
Purpose: build up the dataset that Phase 6 (per-lane probability calibration)
will read from. This module has no behavior side-effects — write-only — and
never throws into the caller; logging failures degrade silently with a warning.

The schema is intentionally additive: Phase 6 will populate
``calibrated_est_prob`` / ``alpha_used`` / ``posterior_*`` with real values.
Until then they default to ``stated_est_prob`` / ``1.0`` / ``None``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Centralised across-session log so calibration_report.py can aggregate easily.
DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_TRADES_LOG = DEFAULT_CALIBRATION_DIR / "trades.jsonl"


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        f = float(value)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _resolve_side(closed: Dict[str, Any]) -> str:
    """Return BUY_YES / BUY_NO from closed-trade record."""
    action = str(closed.get("action") or "").strip().upper()
    if action in ("BUY_YES", "BUY_NO"):
        return action
    leg = str(closed.get("entry_leg") or "").strip().upper()
    if leg == "NO":
        return "BUY_NO"
    return "BUY_YES"


def _resolve_lane_id(closed: Dict[str, Any]) -> str:
    """Pull lane_id from the closed trade's entry_signal (set at entry time)."""
    signal = closed.get("entry_signal") or {}
    lane_id = signal.get("lane_id") if isinstance(signal, dict) else None
    if isinstance(lane_id, str) and lane_id.strip():
        return lane_id.strip()
    # Fallback when an older position record lacks lane_id (e.g. restart-sync of
    # a position opened before lane_identity was wired). Use the coarse triple.
    strategy = str(closed.get("strategy") or "unknown")
    window = str(closed.get("window_size") or signal.get("window_size") or "unknown")
    side = "down" if _resolve_side(closed) == "BUY_NO" else "up"
    return f"{strategy}|{window}|{side}|unknown|fallback"


def build_record_from_closed_trade(
    closed: Dict[str, Any],
    *,
    session_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build one calibration-log row from a journal closed-trade dict.

    `closed` is the element appended to ``TradeJournal.closed_trades`` inside
    ``log_exit`` — it carries entry context (``entry_signal``, ``edge``,
    ``confidence``, ``entry_leg``, ``window_size``) plus exit fields
    (``closed_at``, ``exit_price``, ``pnl``, ``exit_reason``).
    """
    signal = closed.get("entry_signal") or {}
    if not isinstance(signal, dict):
        signal = {}

    entry_price = _coerce_float(closed.get("entry_price")) or 0.0
    exit_price = _coerce_float(closed.get("exit_price")) or 0.0
    size = _coerce_float(closed.get("size")) or 0.0
    pnl = _coerce_float(closed.get("pnl")) or 0.0
    notional = size * entry_price
    realized_pct = (pnl / notional) if notional else 0.0

    stated_edge = _coerce_float(closed.get("edge"))
    stated_est_prob = _coerce_float(signal.get("est_prob"))
    if stated_est_prob is None:
        stated_est_prob = _coerce_float(signal.get("raw_est_prob"))

    timestamp = (now or datetime.now(timezone.utc)).isoformat()

    return {
        "ts": timestamp,
        "session_id": str(session_id or ""),
        "trade_id": str(closed.get("trade_id") or ""),
        "lane_id": _resolve_lane_id(closed),
        "strategy": str(closed.get("strategy") or "unknown"),
        "window": str(closed.get("window_size") or signal.get("window_size") or ""),
        "side": _resolve_side(closed),
        "action": str(closed.get("action") or ""),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "size": size,
        "notional": round(notional, 6),
        "pnl": round(pnl, 6),
        "realized_pct": round(realized_pct, 6),
        "win": pnl > 0,
        "stated_edge": stated_edge,
        "stated_est_prob": stated_est_prob,
        # Phase 6 will overwrite the next two; Phase 0 logs them as identity.
        "calibrated_est_prob": stated_est_prob,
        "alpha_used": 1.0,
        "exit_reason": str(closed.get("exit_reason") or ""),
        "opened_at": str(closed.get("opened_at") or ""),
        "closed_at": str(closed.get("closed_at") or ""),
        "schema_version": 1,
    }


def append_calibration_record(
    record: Dict[str, Any],
    *,
    log_path: Optional[Path] = None,
) -> bool:
    """Append one calibration record as a single JSON line. Returns True on success.

    Failure never raises into the caller — calibration logging is best-effort
    telemetry. The trade execution and journaling paths must be unaffected.
    """
    path = Path(log_path) if log_path is not None else DEFAULT_TRADES_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":")) + "\n"
        # POSIX O_APPEND makes line-sized writes atomic across processes when the
        # payload is well under PIPE_BUF (typical 4 KiB). Our records are ~600 B.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except OSError as exc:
        logger.warning("calibration_log append failed (%s): %s", path, exc)
        return False
    except (TypeError, ValueError) as exc:
        logger.warning("calibration_log serialize failed: %s; record=%r", exc, record)
        return False
