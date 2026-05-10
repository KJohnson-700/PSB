"""Load backtest expectations for drift checks (reports + optional YAML merge)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_backtest_expectations(
    config: Dict[str, Any],
    *,
    data_root: Optional[Path] = None,
) -> Dict[str, Dict[str, float]]:
    """Build strategy -> {win_rate, avg_edge, trades_per_day} like ``/api/live/drift``.

    - Scans newest-first ``data/backtest/reports/backtest_*.json``; first-seen wins per strategy.
    - When ``performance_feedback.merge_learning_loop_expectations`` is true (default),
      merges ``learning_loop.backtest_expectations`` (partial keys override per strategy).
    """
    root = data_root if data_root is not None else (PROJECT_ROOT / "data")
    report_dir = root / "backtest" / "reports"
    expectations: Dict[str, Dict[str, float]] = {}
    if report_dir.exists():
        for f in sorted(
            report_dir.glob("backtest_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                with open(f, encoding="utf-8") as fp:
                    data = json.load(fp)
                strategy = data.get("strategy")
                if strategy and strategy not in expectations:
                    bt_trades = data.get("trades", [])
                    wins = sum(1 for t in bt_trades if t.get("pnl", 0) > 0)
                    edges = [
                        t.get("edge", 0) for t in bt_trades if t.get("edge") is not None
                    ]
                    expectations[str(strategy)] = {
                        "win_rate": wins / len(bt_trades) if bt_trades else 0,
                        "avg_edge": sum(edges) / len(edges) if edges else 0,
                        "trades_per_day": len(bt_trades)
                        / max(1, data.get("data_row_count", 1) / 24),
                    }
            except Exception:
                pass

    pf = config.get("performance_feedback") or {}
    merge_ll = bool(pf.get("merge_learning_loop_expectations", True))
    if merge_ll:
        yaml_exp = (config.get("learning_loop") or {}).get("backtest_expectations") or {}
        if isinstance(yaml_exp, dict):
            for strategy, row in yaml_exp.items():
                if not strategy or not isinstance(row, dict):
                    continue
                wr, ae, tpd = row.get("win_rate"), row.get("avg_edge"), row.get(
                    "trades_per_day"
                )
                if wr is None and ae is None and tpd is None:
                    continue
                cur = dict(expectations.get(str(strategy), {}))
                if wr is not None:
                    cur["win_rate"] = float(wr)
                if ae is not None:
                    cur["avg_edge"] = float(ae)
                if tpd is not None:
                    cur["trades_per_day"] = float(tpd)
                expectations[str(strategy)] = cur

    return expectations
