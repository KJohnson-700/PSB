"""Load backtest expectations for drift checks (reports + optional YAML merge)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_REPORT_STRATEGY_RE = re.compile(r"^(.+)_(\d+)m$", re.IGNORECASE)


def parse_backtest_report_strategy(strategy: str) -> Tuple[str, Optional[int]]:
    """Split report keys like ``bitcoin_30m`` into (``bitcoin``, 30)."""
    raw = str(strategy or "").strip()
    if not raw:
        return "", None
    m = _REPORT_STRATEGY_RE.match(raw)
    if m:
        return m.group(1).lower(), int(m.group(2))
    return raw.lower(), None


def live_trade_window_minutes(trade: Dict[str, Any]) -> Optional[int]:
    """Normalize journal EXIT row window to integer minutes when present."""
    for key in ("window_size", "window", "window_minutes"):
        raw = trade.get(key)
        if raw is None or raw == "":
            continue
        s = str(raw).strip().lower().removesuffix("m")
        try:
            return int(float(s))
        except (TypeError, ValueError):
            continue
    return None


def live_trades_for_expectation(
    live_trades: list[Dict[str, Any]], expectation_key: str
) -> list[Dict[str, Any]]:
    """Match journal EXIT rows to a backtest expectation key (base or base_Nm)."""
    base, window_m = parse_backtest_report_strategy(expectation_key)
    if not base:
        return []
    out: list[Dict[str, Any]] = []
    for t in live_trades:
        if str(t.get("strategy", "")).lower() != base:
            continue
        if window_m is None:
            out.append(t)
            continue
        tw = live_trade_window_minutes(t)
        if tw is not None and tw == window_m:
            out.append(t)
    return out


def _metrics_from_report_data(data: Dict[str, Any]) -> Dict[str, float]:
    bt_trades = data.get("trades") or []
    if not bt_trades and isinstance(data.get("test"), dict):
        bt_trades = data["test"].get("trades") or []
    wins = sum(1 for t in bt_trades if t.get("pnl", 0) > 0)
    edges = [t.get("edge", 0) for t in bt_trades if t.get("edge") is not None]
    scanned = int(data.get("windows_scanned") or data.get("total_windows") or 0)
    entered = int(data.get("windows_entered") or len(bt_trades) or 0)
    days = max(1.0, scanned / max(1, int(data.get("window_minutes") or 15)) / (24 * 60 / 15))
    if data.get("start_date") and data.get("end_date"):
        try:
            from datetime import datetime

            s = datetime.strptime(str(data["start_date"])[:10], "%Y-%m-%d")
            e = datetime.strptime(str(data["end_date"])[:10], "%Y-%m-%d")
            days = max(1.0, (e - s).days + 1)
        except ValueError:
            pass
    return {
        "win_rate": wins / len(bt_trades) if bt_trades else float(data.get("win_rate") or 0),
        "avg_edge": sum(edges) / len(edges) if edges else float(data.get("avg_edge") or 0),
        "trades_per_day": (
            len(bt_trades) / days
            if bt_trades
            else entered / days if entered else 0.0
        ),
    }


def load_backtest_expectations(
    config: Dict[str, Any],
    *,
    data_root: Optional[Path] = None,
) -> Dict[str, Dict[str, float]]:
    """Build strategy -> {win_rate, avg_edge, trades_per_day} like ``/api/live/drift``.

    - Scans newest-first ``data/backtest/reports/backtest_*.json``; first-seen wins per key.
    - Report keys may be ``bitcoin_30m`` (window-specific) or legacy ``bitcoin``.
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
                if not strategy:
                    sym = data.get("symbol")
                    wm = data.get("window_minutes")
                    base = {
                        "BTC": "bitcoin",
                        "SOL": "sol_macro",
                        "ETH": "eth_macro",
                        "XRP": "xrp_macro",
                        "HYPE": "hype_macro",
                    }.get(str(sym or "").upper())
                    if base and wm:
                        strategy = f"{base}_{int(wm)}m"
                if strategy and strategy not in expectations:
                    expectations[str(strategy)] = _metrics_from_report_data(data)
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
