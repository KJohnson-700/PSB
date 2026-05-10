"""Runtime drift feedback: bounded min_edge / Kelly multipliers in memory only."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.execution.backtest_expectations import load_backtest_expectations
from src.execution.live_testing import PerformanceTracker

logger = logging.getLogger(__name__)


def refresh_performance_feedback(
    config: Dict[str, Any],
    *,
    journal_path: Optional[Path] = None,
    data_root: Optional[Path] = None,
) -> None:
    """Run ``PerformanceTracker.check_drift`` and write ``config['_runtime_feedback']``."""
    pf = config.get("performance_feedback") or {}
    ts = datetime.now(timezone.utc).isoformat()
    if not bool(pf.get("enabled", False)):
        config["_runtime_feedback"] = {
            "enabled": False,
            "updated_at": ts,
            "by_strategy": {},
        }
        return

    expectations = load_backtest_expectations(config, data_root=data_root)
    if not expectations:
        config["_runtime_feedback"] = {
            "enabled": True,
            "updated_at": ts,
            "expectations_empty": True,
            "by_strategy": {},
        }
        logger.info("performance_feedback: no expectations (reports/YAML empty); skip tighten")
        return

    min_sample = int(pf.get("min_live_sample", 15))
    tracker = PerformanceTracker(
        str(journal_path) if journal_path is not None else None
    )
    drift = tracker.check_drift(expectations, min_live_sample=min_sample)

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

    diverging_n = sum(1 for s in by_strategy.values() if s.get("is_diverging"))
    config["_runtime_feedback"] = {
        "enabled": True,
        "updated_at": ts,
        "expectations_empty": False,
        "strategies_evaluated": len(drift),
        "diverging_count": diverging_n,
        "by_strategy": by_strategy,
    }
    tight = {k: v["min_edge_mult"] for k, v in by_strategy.items() if v["is_diverging"]}
    logger.info(
        "performance_feedback: evaluated=%d diverging=%d min_edge_mult %s",
        len(drift),
        diverging_n,
        tight,
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
    }
    if isinstance(rf, dict) and rf:
        return {
            **base,
            "runtime": {
                "enabled": rf.get("enabled"),
                "updated_at": rf.get("updated_at"),
                "expectations_empty": rf.get("expectations_empty"),
                "diverging_count": rf.get("diverging_count"),
                "strategies_evaluated": rf.get("strategies_evaluated"),
                "by_strategy": rf.get("by_strategy"),
            },
        }
    return {**base, "runtime": None}
