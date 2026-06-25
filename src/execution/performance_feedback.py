"""Runtime drift feedback: bounded min_edge / Kelly multipliers in memory only."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.analysis.ghost_calibration import DEFAULT_SETTLED_LOG
from src.execution.live_testing import PerformanceTracker

logger = logging.getLogger(__name__)


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def build_feedback_lane_id(
    strategy: str,
    *,
    window: str,
    side: str,
    regime: Optional[str],
) -> str:
    return (
        f"{str(strategy or '').strip().lower()}|"
        f"{str(window or '').strip().lower()}|"
        f"{str(side or '').strip().lower()}|"
        f"{str(regime or 'unknown').strip().lower()}"
    )


def _canonicalize_lane_id(lane_id: str) -> str:
    lane = str(lane_id or "").strip().lower()
    if lane.endswith("|rejected"):
        lane = lane[: -len("|rejected")]
    return lane


def check_overtight(
    config: Dict[str, Any],
    *,
    settled_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Find ghost-settled lanes whose min-edge gate looks overly restrictive."""
    pf = config.get("performance_feedback") or {}
    path = Path(settled_path) if settled_path is not None else DEFAULT_SETTLED_LOG
    if not path.exists():
        return []

    min_lane_sample = max(1, int(pf.get("overtight_min_lane_sample", 25)))
    min_pass_sample = max(1, int(pf.get("overtight_min_pass_sample", 12)))
    wr_threshold = float(pf.get("overtight_ghost_wr_threshold", 0.58))
    max_relax_delta = max(0.0, float(pf.get("overtight_max_relax_delta", 0.03)))
    mult_floor = float(pf.get("overtight_min_edge_mult_floor", 0.70))
    mult_ceil = float(pf.get("overtight_min_edge_mult_ceil", 1.0))
    allowed_reasons = {
        str(reason).strip()
        for reason in (pf.get("overtight_reasons") or ["lane_min_edge"])
        if str(reason).strip()
    }

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in _iter_jsonl(path) or []:
        # Shadow/instrumentation cohorts are observational only — never auto-loosen
        # off them, regardless of overtight_reasons config (foot-gun guard).
        if str(row.get("reason") or "").endswith("_shadow"):
            continue
        if allowed_reasons and str(row.get("reason") or "") not in allowed_reasons:
            continue
        if not isinstance(row.get("win"), bool):
            continue
        lane_id = _canonicalize_lane_id(str(row.get("lane_id") or ""))
        if not lane_id:
            continue
        context = row.get("context") or {}
        edge = _coerce_float(context.get("edge"))
        threshold = _coerce_float(context.get("effective_min_edge"))
        if edge is None or threshold is None:
            continue
        required_delta = max(0.0, threshold - edge)
        if required_delta <= 0:
            continue
        realized_pct = _coerce_float(row.get("realized_pct"))
        buckets.setdefault(lane_id, []).append(
            {
                "strategy": str(row.get("strategy") or ""),
                "window": str(row.get("window") or ""),
                "reason": str(row.get("reason") or ""),
                "win": bool(row.get("win")),
                "required_delta": required_delta,
                "baseline_threshold": threshold,
                "realized_pct": realized_pct,
            }
        )

    out: List[Dict[str, Any]] = []
    for lane_id, rows in buckets.items():
        lane_n = len(rows)
        lane_wins = sum(1 for row in rows if row["win"])
        lane_wr = lane_wins / lane_n if lane_n else 0.0
        if lane_n < min_lane_sample or lane_wr < wr_threshold:
            continue

        candidate_deltas = sorted(
            min(max_relax_delta, float(row["required_delta"]))
            for row in rows
        )
        recommendation: Optional[Dict[str, Any]] = None
        for delta in candidate_deltas:
            admitted = [
                row for row in rows if float(row["required_delta"]) <= (delta + 1e-9)
            ]
            admitted_n = len(admitted)
            admitted_wins = sum(1 for row in admitted if row["win"])
            admitted_wr = admitted_wins / admitted_n if admitted_n else 0.0
            if admitted_n < min_pass_sample or admitted_wr < wr_threshold:
                continue

            avg_baseline = sum(
                float(row["baseline_threshold"]) for row in admitted
            ) / admitted_n
            mult = 1.0
            if abs(avg_baseline) > 1e-9:
                mult = 1.0 - (float(delta) / avg_baseline)
            mult = max(mult_floor, min(mult, mult_ceil))

            realized_rows = [
                float(row["realized_pct"])
                for row in admitted
                if row["realized_pct"] is not None
            ]
            missed_ev = sum(max(val, 0.0) for val in realized_rows)
            protected_loss = sum(max(-val, 0.0) for val in realized_rows)

            recommendation = {
                "lane_id": lane_id,
                "strategy": rows[0]["strategy"],
                "window": rows[0]["window"],
                "ghost_n": lane_n,
                "ghost_wins": lane_wins,
                "ghost_win_rate": round(lane_wr, 4),
                "recommended_relax_delta": round(float(delta), 6),
                "recommended_min_edge_mult": round(mult, 6),
                "recommended_action": f"loosen min_edge by {float(delta):.4f}",
                "admitted_n": admitted_n,
                "admitted_wins": admitted_wins,
                "admitted_win_rate": round(admitted_wr, 4),
                "avg_baseline_threshold": round(avg_baseline, 6),
                "net_gate_value_pct": round(protected_loss - missed_ev, 6),
                "missed_ev_pct": round(missed_ev, 6),
                "protected_loss_pct": round(protected_loss, 6),
                "verdict": (
                    f"OVERTIGHT: ghost WR {lane_wr:.0%} on n={lane_n}; "
                    f"relax {float(delta):.4f} admits n={admitted_n} at WR {admitted_wr:.0%}"
                ),
            }
            break

        if recommendation is not None:
            out.append(recommendation)

    out.sort(
        key=lambda row: (
            float(row.get("ghost_win_rate", 0.0)),
            int(row.get("ghost_n", 0)),
        ),
        reverse=True,
    )
    return out


def refresh_performance_feedback(
    config: Dict[str, Any],
    *,
    journal_path: Optional[Path] = None,
    data_root: Optional[Path] = None,
    settled_path: Optional[Path] = None,
) -> None:
    """Run ``PerformanceTracker.check_drift`` and write ``config['_runtime_feedback']``."""
    pf = config.get("performance_feedback") or {}
    ts = datetime.now(timezone.utc).isoformat()
    if not bool(pf.get("enabled", False)):
        config["_runtime_feedback"] = {
            "enabled": False,
            "updated_at": ts,
            "by_strategy": {},
            "by_lane": {},
        }
        return

    # Backtest expectations removed (ghost calibration is primary validation system)
    expectations: Dict[str, Dict[str, float]] = {}
    min_sample = int(pf.get("min_live_sample", 15))
    drift = []
    if expectations:
        tracker = PerformanceTracker(
            str(journal_path) if journal_path is not None else None
        )
        drift = tracker.check_drift(expectations, min_live_sample=min_sample)
    else:
        logger.info(
            "performance_feedback: no expectations (backtest infrastructure removed); skip drift tighten"
        )

    diverge_mult = float(pf.get("diverge_min_edge_mult", 1.08))
    min_cap = float(pf.get("min_min_edge_mult", 1.0))
    max_cap = float(pf.get("max_min_edge_mult", 1.15))
    kelly_when = float(pf.get("kelly_mult_when_diverging", 1.0))
    k_min = float(pf.get("kelly_mult_min", 0.5))
    k_max = float(pf.get("kelly_mult_max", 1.0))

    by_strategy: Dict[str, Dict[str, Any]] = {}
    for r in drift:
        if r.is_diverging:
            mem = max(min_cap, min(diverge_mult, max_cap))
            if kelly_when < 1.0:
                km = max(k_min, min(kelly_when, k_max))
            else:
                km = 1.0
        else:
            mem = 1.0
            km = 1.0
        by_strategy[r.strategy] = {
            "min_edge_mult": round(mem, 4),
            "kelly_mult": round(km, 4),
            "is_diverging": r.is_diverging,
            "verdict": r.verdict,
            "live_sample_size": r.live_sample_size,
        }

    overtight = check_overtight(config, settled_path=settled_path)
    by_lane = {
        str(row["lane_id"]): {
            "min_edge_mult": float(row["recommended_min_edge_mult"]),
            "ghost_n": int(row["ghost_n"]),
            "ghost_wins": int(row["ghost_wins"]),
            "ghost_win_rate": float(row["ghost_win_rate"]),
            "admitted_n": int(row["admitted_n"]),
            "admitted_wins": int(row["admitted_wins"]),
            "admitted_win_rate": float(row["admitted_win_rate"]),
            "recommended_relax_delta": float(row["recommended_relax_delta"]),
            "avg_baseline_threshold": float(row["avg_baseline_threshold"]),
            "recommended_action": str(row["recommended_action"]),
            "verdict": str(row["verdict"]),
            "net_gate_value_pct": float(row["net_gate_value_pct"]),
        }
        for row in overtight
    }

    diverging_n = sum(1 for s in by_strategy.values() if s.get("is_diverging"))
    config["_runtime_feedback"] = {
        "enabled": True,
        "updated_at": ts,
        "expectations_empty": not bool(expectations),
        "strategies_evaluated": len(drift),
        "diverging_count": diverging_n,
        "overtight_count": len(by_lane),
        "by_strategy": by_strategy,
        "by_lane": by_lane,
    }
    tight = {k: v["min_edge_mult"] for k, v in by_strategy.items() if v["is_diverging"]}
    loose = {k: v["min_edge_mult"] for k, v in by_lane.items()}
    logger.info(
        "performance_feedback: evaluated=%d diverging=%d overtight=%d drift_mult=%s loosen_mult=%s",
        len(drift),
        diverging_n,
        len(by_lane),
        tight,
        loose,
    )


def get_drift_min_edge_mult(strategy: str, config: Dict[str, Any]) -> float:
    """Multiplier for effective min edge (1.0 when feature off or no row for strategy)."""
    pf = config.get("performance_feedback") or {}
    if not bool(pf.get("enabled", False)):
        return 1.0
    rf = config.get("_runtime_feedback") or {}
    if rf.get("enabled") is False:
        return 1.0
    key = strategy
    bys = rf.get("by_strategy") or {}
    row = bys.get(key) or bys.get(key.lower())
    if not row:
        return 1.0
    return float(row.get("min_edge_mult", 1.0) or 1.0)


def get_loosen_min_edge_mult(
    strategy: str,
    config: Dict[str, Any],
    *,
    window: str,
    side: str,
    regime: Optional[str],
) -> float:
    """Lane-level min-edge multiplier from settled ghost rejects (<1 = loosen)."""
    pf = config.get("performance_feedback") or {}
    if not bool(pf.get("enabled", False)):
        return 1.0
    rf = config.get("_runtime_feedback") or {}
    if rf.get("enabled") is False:
        return 1.0
    lane_id = build_feedback_lane_id(
        strategy,
        window=window,
        side=side,
        regime=regime,
    )
    row = (rf.get("by_lane") or {}).get(lane_id)
    if not row:
        return 1.0
    return float(row.get("min_edge_mult", 1.0) or 1.0)


def get_drift_kelly_mult(strategy: str, config: Dict[str, Any]) -> float:
    """Kelly fraction multiplier when diverging (1.0 default)."""
    pf = config.get("performance_feedback") or {}
    if not bool(pf.get("enabled", False)):
        return 1.0
    rf = config.get("_runtime_feedback") or {}
    if rf.get("enabled") is False:
        return 1.0
    bys = rf.get("by_strategy") or {}
    row = bys.get(strategy) or bys.get(strategy.lower())
    if not row:
        return 1.0
    return float(row.get("kelly_mult", 1.0) or 1.0)


def public_feedback_status(config: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitized snapshot for ``/api/status`` (no secrets)."""
    pf = config.get("performance_feedback") or {}
    rf = config.get("_runtime_feedback")
    base = {
        "feature_enabled": bool(pf.get("enabled", False)),
        "refresh_every_n_cycles": int(pf.get("refresh_every_n_cycles", 1)),
        "merge_learning_loop_expectations": bool(
            pf.get("merge_learning_loop_expectations", True)
        ),
        "overtight_contract": {
            "reasons": list(pf.get("overtight_reasons") or ["lane_min_edge"]),
            "min_lane_sample": int(pf.get("overtight_min_lane_sample", 25)),
            "min_pass_sample": int(pf.get("overtight_min_pass_sample", 12)),
            "ghost_wr_threshold": float(pf.get("overtight_ghost_wr_threshold", 0.58)),
            "max_relax_delta": float(pf.get("overtight_max_relax_delta", 0.03)),
            "min_edge_mult_floor": float(pf.get("overtight_min_edge_mult_floor", 0.70)),
            "min_edge_mult_ceil": float(pf.get("overtight_min_edge_mult_ceil", 1.0)),
        },
    }
    preview = check_overtight(config)
    base["overtight_preview"] = {
        "count": len(preview),
        "lanes": preview,
    }
    if isinstance(rf, dict) and rf:
        return {
            **base,
            "runtime": {
                "enabled": rf.get("enabled"),
                "updated_at": rf.get("updated_at"),
                "expectations_empty": rf.get("expectations_empty"),
                "diverging_count": rf.get("diverging_count"),
                "overtight_count": rf.get("overtight_count"),
                "strategies_evaluated": rf.get("strategies_evaluated"),
                "by_strategy": rf.get("by_strategy"),
                "by_lane": rf.get("by_lane"),
            },
        }
    return {**base, "runtime": None}
