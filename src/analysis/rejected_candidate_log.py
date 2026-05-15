"""Ghost-trade log for candidates rejected at structural gates.

Appends one JSON object per rejection to ``data/calibration/rejected_candidates.jsonl``.
Purpose: answer the question "is my winning direction being unfairly blocked?" by
capturing enough information about each rejected candidate to later settle a
hypothetical outcome from market resolution data.

This module is write-only and best-effort: failures degrade silently with a warning
and never propagate into the strategy path. There is no behavior side-effect.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_REJECTED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates.jsonl"


def log_rejected_candidate(
    *,
    strategy: str,
    window: str,
    side: str,
    action: str,
    reason: str,
    market: Any,
    yes_price: float,
    est_prob_up: Optional[float],
    htf_bias: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    log_path: Optional[Path] = None,
) -> bool:
    """Append one ghost-trade record. Returns True on success.

    `market` should be the strategy's Market object (carries id, question, slug,
    end_date, token_ids, no_price). `context` is an optional dict of gate-specific
    telemetry — kept open-ended so callers can record the variables that drove the
    decision (e.g. macd_4h_histogram_rising) for later validation.
    """
    try:
        end_iso = ""
        end_dt = getattr(market, "end_date", None)
        if isinstance(end_dt, datetime):
            end_iso = end_dt.astimezone(timezone.utc).isoformat() if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc).isoformat()

        bias_token = (htf_bias or "unknown").lower()
        up_or_down = "up" if str(action).upper() == "BUY_YES" else "down"
        synthetic_lane = f"{strategy}|{window}|{up_or_down}|{bias_token}|rejected"

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 1,
            "strategy": strategy,
            "window": window,
            "side": side,
            "action": action,
            "reason": reason,
            "lane_id": synthetic_lane,
            "market_id": str(getattr(market, "id", "") or ""),
            "market_question": str(getattr(market, "question", "") or ""),
            "market_slug": str(getattr(market, "slug", "") or ""),
            "market_end_ts": end_iso,
            "token_id_yes": str(getattr(market, "token_id_yes", "") or ""),
            "token_id_no": str(getattr(market, "token_id_no", "") or ""),
            "yes_price": float(yes_price) if yes_price is not None else None,
            "no_price": float(getattr(market, "no_price", 0.0) or 0.0),
            "est_prob_up": float(est_prob_up) if est_prob_up is not None else None,
            "htf_bias": htf_bias,
            "context": context or {},
        }
    except (TypeError, ValueError, AttributeError) as exc:
        logger.warning("rejected_candidate_log build failed: %s", exc)
        return False

    path = Path(log_path) if log_path is not None else DEFAULT_REJECTED_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except OSError as exc:
        logger.warning("rejected_candidate_log append failed (%s): %s", path, exc)
        return False
    except (TypeError, ValueError) as exc:
        logger.warning("rejected_candidate_log serialize failed: %s; record=%r", exc, record)
        return False
