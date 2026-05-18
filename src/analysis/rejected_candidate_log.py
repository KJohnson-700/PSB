"""Rejected-candidate tracker log for structural-gate skips.

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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_REJECTED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates.jsonl"


def build_threshold_probe_variants(
    *,
    metric_name: str,
    observed_value: Optional[float],
    baseline_threshold: Optional[float],
    relax_steps: Optional[List[float]] = None,
    tighten_steps: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """Return simple counterfactual pass/fail variants for a thresholded metric.

    ``observed_value >= threshold`` is treated as a pass condition. ``relax_steps``
    lower the threshold while ``tighten_steps`` raise it.
    """
    try:
        observed = float(observed_value) if observed_value is not None else None
        baseline = (
            float(baseline_threshold) if baseline_threshold is not None else None
        )
    except (TypeError, ValueError):
        return []
    if observed is None or baseline is None:
        return []

    relax = [0.0, 0.01, 0.02] if relax_steps is None else list(relax_steps)
    tighten = [0.01] if tighten_steps is None else list(tighten_steps)
    variants: List[Dict[str, Any]] = []

    def _append(kind: str, delta: float, threshold: float) -> None:
        variants.append(
            {
                "probe": metric_name,
                "kind": kind,
                "delta": round(float(delta), 6),
                "threshold": round(float(threshold), 6),
                "observed": round(observed, 6),
                "margin": round(observed - threshold, 6),
                "would_pass": bool(observed >= threshold),
            }
        )

    _append("baseline", 0.0, baseline)
    for delta in relax:
        if delta <= 0:
            continue
        _append("relax", delta, max(0.0, baseline - float(delta)))
    for delta in tighten:
        if delta <= 0:
            continue
        _append("tighten", delta, baseline + float(delta))
    return variants


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
    probe_variants: Optional[List[Dict[str, Any]]] = None,
    policy_version: Optional[str] = None,
    feature_hash: Optional[str] = None,
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
            "schema_version": 2,
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
            "probe_variants": probe_variants or [],
            "policy_version": str(policy_version or "").strip(),
            "feature_hash": str(feature_hash or "").strip(),
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
