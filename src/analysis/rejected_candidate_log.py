"""Rejected-candidate tracker log for structural-gate skips.

Appends one JSON object per rejection to ``data/calibration/rejected_candidates.jsonl``.
Purpose: answer the question "is my winning direction being unfairly blocked?" by
capturing enough information about each rejected candidate to later settle a
hypothetical outcome from market resolution data.

Schema version 4 (2026-05-20): records now carry ``asset_spot``, ``btc_spot``,
``rsi_14``, and ``atr_14`` inside ``context`` when callers supply them via
``build_market_context``. Schema is additive — readers that ignore unknown keys
keep working against v3 records too.

This module is write-only and best-effort: failures degrade silently with a warning
and never propagate into the strategy path. There is no behavior side-effect.
"""

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from src.analysis.calibration_buckets import build_bucket_tags
from src.analysis.lane_identity import resolve_entry_family, resolve_lane_side

logger = logging.getLogger(__name__)

# Raise the soft file-descriptor limit on import. macOS defaults to 256 which is
# easily exhausted by upstream HTTP clients leaking connections (see CLOSE_WAIT
# sockets), and when that happens this logger is the first writer to surface
# EMFILE — silently dropping ghost-trade records needed for diagnosis.
try:
    import resource

    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if _soft < _hard or _hard == resource.RLIM_INFINITY:
        _target = 10240 if _hard == resource.RLIM_INFINITY else _hard
        if _soft < _target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (_target, _hard))
except (ImportError, ValueError, OSError) as _rlimit_exc:  # pragma: no cover
    logger.debug("rejected_candidate_log: could not raise RLIMIT_NOFILE: %s", _rlimit_exc)

# In-memory fallback buffer for records that failed to append (typically due to
# transient EMFILE). Flushed opportunistically on the next successful write.
_PENDING_BUFFER_MAX = 4096
_pending_buffer: Deque[tuple] = deque(maxlen=_PENDING_BUFFER_MAX)
_pending_lock = threading.Lock()


def _append_line(path: Path, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _flush_pending() -> None:
    """Best-effort drain of buffered records. Stops on first OSError."""
    while True:
        with _pending_lock:
            if not _pending_buffer:
                return
            path, line = _pending_buffer[0]
        try:
            _append_line(Path(path), line)
        except OSError:
            return
        with _pending_lock:
            if _pending_buffer and _pending_buffer[0] == (path, line):
                _pending_buffer.popleft()


# Drain any buffered (previously-failed) records on normal interpreter exit, so
# they aren't lost when no further append happens to trigger the inline flush.
atexit.register(_flush_pending)


DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_REJECTED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates.jsonl"


def _valid_lane_id(value: Any) -> str:
    lane_id = str(value or "").strip()
    return lane_id if len(lane_id.split("|")) >= 5 else ""


def _biases_from_live_lane(lane_id: str) -> Dict[str, str]:
    parts = lane_id.split("|")
    if len(parts) < 4:
        return {}
    bits = [bit for bit in parts[3].split("__") if bit]
    if len(bits) >= 3:
        return {
            "primary_htf_bias": bits[0].upper(),
            "alt_htf_bias": bits[1].upper(),
            "btc_htf_bias": bits[2].upper(),
        }
    if bits:
        return {"primary_htf_bias": bits[0].upper()}
    return {}

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


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _probe_quality(variant: Dict[str, Any]) -> Optional[float]:
    margin_raw = variant.get("margin")
    if margin_raw is None:
        return None
    try:
        margin = float(margin_raw)
    except (TypeError, ValueError):
        return None

    scale = 0.0
    try:
        if variant.get("threshold") is not None:
            scale = abs(float(variant.get("threshold") or 0.0))
        elif (
            variant.get("threshold_min") is not None
            and variant.get("threshold_max") is not None
        ):
            low = float(variant.get("threshold_min") or 0.0)
            high = float(variant.get("threshold_max") or 0.0)
            scale = abs(high - low)
    except (TypeError, ValueError):
        scale = 0.0
    scale = max(scale, 0.01)
    return _clamp01(0.5 + 0.5 * (margin / scale))


def compute_convergence_telemetry(
    *,
    probe_variants: Optional[List[Dict[str, Any]]] = None,
    edge: Optional[float] = None,
    effective_min_edge: Optional[float] = None,
    composite_score: Optional[float] = None,
    composite_components: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a compact convergence summary from gate margins or composite components."""
    baseline = [
        probe
        for probe in (probe_variants or [])
        if isinstance(probe, dict) and str(probe.get("kind") or "") == "baseline"
    ]
    qualities: List[float] = []
    pass_count = 0
    fail_count = 0
    narrow_pass_count = 0
    strong_pass_count = 0
    for probe in baseline:
        quality = _probe_quality(probe)
        if quality is None:
            continue
        qualities.append(quality)
        if bool(probe.get("would_pass")):
            pass_count += 1
            if quality >= 0.75:
                strong_pass_count += 1
            elif quality <= 0.60:
                narrow_pass_count += 1
        else:
            fail_count += 1

    edge_quality: Optional[float] = None
    try:
        if edge is not None and effective_min_edge is not None:
            denom = max(abs(float(effective_min_edge)), 0.0001)
            edge_quality = _clamp01(float(edge) / denom)
            qualities.append(edge_quality)
    except (TypeError, ValueError):
        edge_quality = None

    component_mean: Optional[float] = None
    if composite_components:
        vals: List[float] = []
        for value in composite_components.values():
            try:
                vals.append(_clamp01(float(value)))
            except (TypeError, ValueError):
                continue
        if vals:
            component_mean = round(sum(vals) / len(vals), 6)
            qualities.append(component_mean)

    score: Optional[float]
    if composite_score is not None:
        try:
            score = _clamp01(float(composite_score))
            qualities.append(score)
        except (TypeError, ValueError):
            score = None
    else:
        score = None

    convergence_score = round(sum(qualities) / len(qualities), 6) if qualities else None
    return {
        "convergence_score": convergence_score,
        "convergence_probe_count": len(baseline),
        "convergence_pass_count": pass_count,
        "convergence_fail_count": fail_count,
        "convergence_narrow_pass_count": narrow_pass_count,
        "convergence_strong_pass_count": strong_pass_count,
        "edge_quality": round(edge_quality, 6) if edge_quality is not None else None,
        "component_mean_quality": component_mean,
    }


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


def build_market_context(
    *,
    asset_spot: Optional[float] = None,
    btc_spot: Optional[float] = None,
    rsi_14: Optional[float] = None,
    atr_14: Optional[float] = None,
) -> Dict[str, Any]:
    """Standard market-condition fields for the rejection `context` dict.

    Merge the return value into the per-site `context` kwarg of
    `log_rejected_candidate` so every record carries asset spot, BTC spot,
    RSI(14), and ATR(14) at the moment of rejection. Skips fields whose
    value is None or non-numeric; never raises.
    """
    out: Dict[str, Any] = {}
    for key, val in (
        ("asset_spot", asset_spot),
        ("btc_spot", btc_spot),
        ("rsi_14", rsi_14),
        ("atr_14", atr_14),
    ):
        if val is None:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(num):
            continue
        out[key] = round(num, 6)
    return out


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
    btc_1h_regime: Optional[str] = None,
    convergence_score: Optional[float] = None,
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
        live_lane_id = _valid_lane_id(record_context.get("calibration_lane_id"))
        lane_biases = _biases_from_live_lane(live_lane_id)
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
        primary_bias = str(
            primary_htf_bias
            or lane_biases.get("primary_htf_bias")
            or htf_bias
            or ""
        ).strip()
        lane_family_text = str(lane_family or "").strip()
        if not lane_family_text and live_lane_id:
            parts = live_lane_id.split("|")
            if len(parts) >= 5:
                lane_family_text = str(parts[4] or "").strip()
        family_came_from_caller = bool(lane_family_text)
        if not lane_family_text:
            lane_family_text = resolve_entry_family(
                strategy=strategy,
                window_size=window,
                lane_side=resolve_lane_side(action=action, direction=up_or_down),
                side_source=side_source or record_context.get("side_source"),
                resolver_path=resolver_path or record_context.get("resolver_path"),
                ai_used=bool(record_context.get("ai_used")),
                reason=reason,
                signal_reason=record_context.get("signal_reason") or reason,
            )
        if (
            not family_came_from_caller
            and lane_family_text == "standard"
            and not (
                resolver_path
                or record_context.get("resolver_path")
                or side_source
                or record_context.get("side_source")
            )
        ):
            # Rejected before the direction resolver assigned a family
            # (e.g. iql_15m_reject, eth_15m_weak_confirm). resolve_entry_family
            # falls back to "standard" when given no signal, which pollutes the
            # real `*|standard` bucket. Tag distinctly so the populations
            # bucket separately for calibration.
            lane_family_text = "pre_resolver_reject"
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
            rsi=record_context.get("rsi_14"),
            atr=record_context.get("atr_14"),
            asset_spot=record_context.get("asset_spot"),
        )
        convergence = compute_convergence_telemetry(
            probe_variants=probe_variants,
            edge=record_context.get("edge"),
            effective_min_edge=effective_min_edge_val,
            composite_score=convergence_score,
            composite_components=record_context.get("composite_components"),
        )
        btc_1h_regime_text = str(
            btc_1h_regime
            or record_context.get("btc_1h_regime")
            or ""
        ).strip()

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 5,
            "strategy": strategy,
            "window": window,
            "side": side,
            "action": action,
            "reason": reason,
            "stage": stage,
            "lane_id": synthetic_lane,
            "ghost_lane_id": synthetic_lane,
            "live_lane_id": live_lane_id,
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
            "btc_1h_regime": btc_1h_regime_text or None,
            "context": record_context,
            # NOTE: raw `probe_variants` is intentionally NOT persisted. It is the
            # largest single field per row (~38% of the serialized row on recent
            # data) and is read by no consumer. Its information is distilled into the
            # `convergence_*` scalars below via compute_convergence_telemetry(), and
            # those ARE consumed downstream.
            "policy_version": str(policy_version or "").strip(),
            "feature_hash": str(feature_hash or "").strip(),
            "side_source": str(side_source or "").strip(),
            "resolver_path": str(resolver_path or side_source or "").strip(),
            "primary_htf_bias": primary_bias,
            "alt_htf_bias": lane_biases.get("alt_htf_bias", ""),
            "btc_htf_bias": lane_biases.get("btc_htf_bias", ""),
            "lane_family": lane_family_text,
            "entry_policy_snapshot": (
                entry_policy_snapshot if isinstance(entry_policy_snapshot, dict) else {}
            ),
            "effective_min_edge": effective_min_edge_val,
            "raw_est_prob": raw_est_prob_val,
            "calibrated_est_prob": calibrated_est_prob_val,
            "gate_reason": gate_reason_text,
            "gate_stage": gate_stage_text,
            **convergence,
            **bucket_tags,
        }
    except (TypeError, ValueError, AttributeError) as exc:
        logger.warning("rejected_candidate_log build failed: %s", exc)
        return False

    path = Path(log_path) if log_path is not None else DEFAULT_REJECTED_LOG
    try:
        line = json.dumps(record, separators=(",", ":")) + "\n"
    except (TypeError, ValueError) as exc:
        logger.warning("rejected_candidate_log serialize failed: %s; record=%r", exc, record)
        return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("rejected_candidate_log mkdir failed (%s): %s", path.parent, exc)

    # Opportunistically drain any records that failed previously.
    if _pending_buffer:
        _flush_pending()

    try:
        _append_line(path, line)
        return True
    except OSError as exc:
        with _pending_lock:
            dropped = len(_pending_buffer) >= _PENDING_BUFFER_MAX
            _pending_buffer.append((str(path), line))
            pending_n = len(_pending_buffer)
        logger.warning(
            "rejected_candidate_log append failed (%s): %s; buffered=%d%s",
            path,
            exc,
            pending_n,
            " (overflow: oldest dropped)" if dropped else "",
        )
        return False
