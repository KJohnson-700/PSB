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

from src.analysis.calibration_buckets import build_bucket_tags

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_REJECTED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates.jsonl"

# Fixed stage taxonomy for the `stage` field on rejection records. Downstream
# filters depend on these values — do not rename without coordinating the
# consumers (calibration dashboards, jq one-liners).
STAGE_LIQUIDITY = "liquidity"
STAGE_ORACLE = "oracle"
STAGE_CORR_FLOOR_5M = "corr_floor_5m"
STAGE_BTC_CATALYST_5M = "btc_catalyst_5m"
STAGE_SIGNAL_STRENGTH_5M = "signal_strength_5m"
STAGE_IQL_15M = "iql_15m"
STAGE_LOW_CORR_SUPPRESSED = "low_corr_suppressed"
STAGE_AI_VETO = "ai_veto"
STAGE_LANE_MIN_EDGE = "lane_min_edge"
STAGE_EDGE_CAP = "edge_cap"


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


def build_upper_cap_probe_variants(
    *,
    metric_name: str,
    observed_value: Optional[float],
    baseline_cap: Optional[float],
    relax_steps: Optional[List[float]] = None,
    tighten_steps: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """Return counterfactual variants for cap-style gates where ``observed <= cap`` passes."""
    try:
        observed = float(observed_value) if observed_value is not None else None
        baseline = float(baseline_cap) if baseline_cap is not None else None
    except (TypeError, ValueError):
        return []
    if observed is None or baseline is None:
        return []

    relax = [1.0, 2.0, 5.0] if relax_steps is None else list(relax_steps)
    tighten = [1.0, 2.0] if tighten_steps is None else list(tighten_steps)
    variants: List[Dict[str, Any]] = []

    def _append(kind: str, delta: float, cap: float) -> None:
        variants.append(
            {
                "probe": metric_name,
                "kind": kind,
                "delta": round(float(delta), 6),
                "threshold": round(float(cap), 6),
                "observed": round(observed, 6),
                "margin": round(float(cap) - observed, 6),
                "would_pass": bool(observed <= float(cap)),
            }
        )

    _append("baseline", 0.0, baseline)
    for delta in relax:
        if delta <= 0:
            continue
        _append("relax", delta, baseline + float(delta))
    for delta in tighten:
        if delta <= 0:
            continue
        _append("tighten", delta, max(0.0, baseline - float(delta)))
    return variants


def build_range_probe_variants(
    *,
    metric_name: str,
    observed_value: Optional[float],
    baseline_min: Optional[float],
    baseline_max: Optional[float],
    relax_steps: Optional[List[float]] = None,
    tighten_steps: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """Return counterfactual variants for band gates where ``min <= observed <= max`` passes."""
    try:
        observed = float(observed_value) if observed_value is not None else None
        lower = float(baseline_min) if baseline_min is not None else None
        upper = float(baseline_max) if baseline_max is not None else None
    except (TypeError, ValueError):
        return []
    if observed is None or lower is None or upper is None:
        return []
    if lower > upper:
        lower, upper = upper, lower

    relax = [0.01, 0.02, 0.05] if relax_steps is None else list(relax_steps)
    tighten = [0.01, 0.02] if tighten_steps is None else list(tighten_steps)
    variants: List[Dict[str, Any]] = []

    def _append(kind: str, delta: float, low: float, high: float) -> None:
        if low > high:
            low, high = high, low
        inside = low <= observed <= high
        margin = min(observed - low, high - observed) if inside else -min(abs(observed - low), abs(observed - high))
        variants.append(
            {
                "probe": metric_name,
                "kind": kind,
                "delta": round(float(delta), 6),
                "threshold_min": round(float(low), 6),
                "threshold_max": round(float(high), 6),
                "observed": round(observed, 6),
                "margin": round(float(margin), 6),
                "would_pass": bool(inside),
            }
        )

    _append("baseline", 0.0, lower, upper)
    for delta in relax:
        if delta <= 0:
            continue
        _append("relax", delta, lower - float(delta), upper + float(delta))
    for delta in tighten:
        if delta <= 0:
            continue
        _append("tighten", delta, lower + float(delta), upper - float(delta))
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
    stage: Optional[str] = None,
    side_source: Optional[str] = None,
    resolver_path: Optional[str] = None,
    primary_htf_bias: Optional[str] = None,
    lane_family: Optional[str] = None,
    entry_policy_snapshot: Optional[Dict[str, Any]] = None,
    effective_min_edge: Optional[float] = None,
    raw_est_prob: Optional[float] = None,
    calibrated_est_prob: Optional[float] = None,
    gate_reason: Optional[str] = None,
    gate_stage: Optional[str] = None,
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
        record_context = dict(context or {})
        effective_min_edge_val = (
            float(effective_min_edge)
            if effective_min_edge is not None
            else None
        )
        if effective_min_edge_val is None:
            try:
                if record_context.get("effective_min_edge") is not None:
                    effective_min_edge_val = float(record_context.get("effective_min_edge"))
            except (TypeError, ValueError):
                effective_min_edge_val = None
        raw_est_prob_val = float(raw_est_prob) if raw_est_prob is not None else None
        calibrated_est_prob_val = (
            float(calibrated_est_prob) if calibrated_est_prob is not None else None
        )
        if calibrated_est_prob_val is None and est_prob_up is not None:
            calibrated_est_prob_val = float(est_prob_up)
        if raw_est_prob_val is None:
            raw_est_prob_val = calibrated_est_prob_val
        primary_bias = str(primary_htf_bias or htf_bias or "").strip()
        gate_reason_text = str(gate_reason or reason or "").strip()
        gate_stage_text = str(gate_stage or stage or "").strip()
        correlation_value = None
        try:
            if record_context.get("corr_1h") is not None:
                correlation_value = float(record_context.get("corr_1h"))
            elif record_context.get("correlation_1h") is not None:
                correlation_value = float(record_context.get("correlation_1h"))
        except (TypeError, ValueError):
            correlation_value = None
        bucket_tags = build_bucket_tags(
            edge=record_context.get("edge"),
            yes_price=yes_price,
            correlation=correlation_value,
            side_source=side_source,
            regime_tag=primary_bias,
            gate_reason=gate_reason_text,
            gate_stage=gate_stage_text,
        )

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 3,
            "strategy": strategy,
            "window": window,
            "side": side,
            "action": action,
            "reason": reason,
            "stage": stage,
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
            "context": record_context,
            "probe_variants": probe_variants or [],
            "policy_version": str(policy_version or "").strip(),
            "feature_hash": str(feature_hash or "").strip(),
            "side_source": str(side_source or "").strip(),
            "resolver_path": str(resolver_path or side_source or "").strip(),
            "primary_htf_bias": primary_bias,
            "lane_family": str(lane_family or "").strip(),
            "entry_policy_snapshot": (
                entry_policy_snapshot if isinstance(entry_policy_snapshot, dict) else {}
            ),
            "effective_min_edge": effective_min_edge_val,
            "raw_est_prob": raw_est_prob_val,
            "calibrated_est_prob": calibrated_est_prob_val,
            "gate_reason": gate_reason_text,
            "gate_stage": gate_stage_text,
            **bucket_tags,
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
