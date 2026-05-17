#!/usr/bin/env python3
"""Compare one paper-trading session slice to one crypto backtest report.

Focuses on the overlap we can measure today from repo artifacts:
- same strategy/window filter
- realized trade count, win rate, PnL, expectancy
- action mix, exit reasons, entry-price cohorts

This does not reconstruct the full candidate universe or scanner latency path.
It is an audit tool for live-vs-replay drift, not a full market-state replay.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.execution.trade_journal import JOURNAL_DIR, TradeJournal


def _load_report(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("split_mode") and isinstance(payload.get("test"), dict):
        section = dict(payload["test"])
        section["_split_mode"] = True
    else:
        section = dict(payload)
        section["_split_mode"] = False
    section["_raw"] = payload
    return section


def _coerce_window_size(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.endswith("m") or text.endswith("h"):
        return text
    return f"{text}m"


def _iter_session_exits(session_dir: Path) -> Iterable[Dict[str, Any]]:
    entries = session_dir / "entries.jsonl"
    if not entries.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with entries.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("event") == "EXIT":
                rows.append(payload)
    return rows


def _session_dir_from_arg(session: Optional[str]) -> Path:
    if session:
        candidate = Path(session)
        if candidate.is_dir():
            return candidate
        alt = JOURNAL_DIR / session
        if alt.is_dir():
            return alt
        raise FileNotFoundError(f"Session not found: {session}")
    chosen = TradeJournal.newest_resumable_session_dir()
    if chosen is None:
        raise FileNotFoundError("No resumable paper-trade session found")
    return chosen


def _infer_strategy(report: Dict[str, Any], explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    return str(report.get("strategy_base") or report.get("strategy") or "").strip()


def _infer_window(report: Dict[str, Any], explicit: Optional[str]) -> str:
    if explicit:
        return _coerce_window_size(explicit)
    return _coerce_window_size(report.get("window_minutes"))


def _bucket_entry_price(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value < 0.46:
        return "<0.46"
    if value <= 0.54:
        return "0.46-0.54"
    return ">0.54"


def _summarize_journal(rows: Iterable[Dict[str, Any]], strategy: str, window_size: str) -> Dict[str, Any]:
    filtered = []
    for row in rows:
        extra = row.get("extra") or {}
        if row.get("strategy") != strategy:
            continue
        row_window = _coerce_window_size(extra.get("window_size"))
        if window_size and row_window != window_size:
            continue
        filtered.append(row)
    wins = sum(1 for row in filtered if float(row.get("pnl") or 0.0) > 0)
    losses = sum(1 for row in filtered if float(row.get("pnl") or 0.0) < 0)
    pnls = [float(row.get("pnl") or 0.0) for row in filtered]
    edges = [
        float((row.get("extra") or {}).get("entry_edge") or row.get("edge") or 0.0)
        for row in filtered
    ]
    actions = Counter(str(row.get("action") or "") for row in filtered)
    exits = Counter(str(row.get("reason") or "unknown") for row in filtered)
    bands = Counter(_bucket_entry_price(float(row.get("entry_price"))) for row in filtered if row.get("entry_price") is not None)
    return {
        "trade_count": len(filtered),
        "wins": wins,
        "losses": losses,
        "net_pnl": round(sum(pnls), 4),
        "win_rate": round(wins / len(filtered), 4) if filtered else 0.0,
        "expectancy": round(sum(pnls) / len(filtered), 4) if filtered else 0.0,
        "avg_edge": round(sum(edges) / len(edges), 4) if edges else 0.0,
        "actions": dict(actions),
        "exit_reasons": dict(exits),
        "entry_price_bands": dict(bands),
    }


def _summarize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    trades = list(report.get("trades") or [])
    wins = int(report.get("wins", 0) or 0)
    losses = int(report.get("losses", 0) or 0)
    pnls = [float(t.get("pnl") or 0.0) for t in trades]
    actions = Counter(str(t.get("action") or "") for t in trades)
    exits = Counter(str(t.get("exit_reason") or "unknown") for t in trades)
    bands = Counter(
        _bucket_entry_price(float(t.get("entry_price")))
        for t in trades
        if t.get("entry_price") is not None
    )
    return {
        "trade_count": int(report.get("windows_entered", len(trades)) or 0),
        "wins": wins,
        "losses": losses,
        "net_pnl": round(float(report.get("net_pnl", 0.0) or 0.0), 4),
        "win_rate": round(float(report.get("win_rate", 0.0) or 0.0), 4),
        "expectancy": round(float(report.get("expectancy", 0.0) or 0.0), 4),
        "avg_edge": round(float(report.get("avg_edge", 0.0) or 0.0), 4),
        "actions": dict(actions),
        "exit_reasons": dict(exits),
        "entry_price_bands": dict(bands),
        "replay_assumptions": dict(report.get("replay_assumptions") or {}),
        "skip_counts": dict(report.get("skip_counts") or {}),
    }


def _diff_metrics(journal: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "trade_count_delta": journal["trade_count"] - report["trade_count"],
        "win_rate_delta": round(journal["win_rate"] - report["win_rate"], 4),
        "net_pnl_delta": round(journal["net_pnl"] - report["net_pnl"], 4),
        "expectancy_delta": round(journal["expectancy"] - report["expectancy"], 4),
        "avg_edge_delta": round(journal["avg_edge"] - report["avg_edge"], 4),
    }


def _print_markdown(
    *,
    session_dir: Path,
    report_path: Path,
    strategy: str,
    window_size: str,
    journal: Dict[str, Any],
    report: Dict[str, Any],
    diff: Dict[str, Any],
) -> None:
    print(f"## Backtest Sync Review")
    print(f"**Session:** `{session_dir.name}`")
    print(f"**Report:** `{report_path.name}`")
    print(f"**Filter:** `{strategy}` / `{window_size}`")
    print("")
    print("### Headline")
    print(
        f"- **Paper:** {journal['trade_count']} trades, WR {journal['win_rate']:.1%}, "
        f"PnL {journal['net_pnl']:+.2f}, expectancy {journal['expectancy']:+.2f}"
    )
    print(
        f"- **Backtest:** {report['trade_count']} trades, WR {report['win_rate']:.1%}, "
        f"PnL {report['net_pnl']:+.2f}, expectancy {report['expectancy']:+.2f}"
    )
    print(
        f"- **Delta:** trades {diff['trade_count_delta']:+d}, WR {diff['win_rate_delta']:+.1%}, "
        f"PnL {diff['net_pnl_delta']:+.2f}, expectancy {diff['expectancy_delta']:+.2f}, "
        f"avg_edge {diff['avg_edge_delta']:+.4f}"
    )
    print("")
    print("### Mix")
    print(f"- **Paper actions:** `{journal['actions']}`")
    print(f"- **Backtest actions:** `{report['actions']}`")
    print(f"- **Paper exit reasons:** `{journal['exit_reasons']}`")
    print(f"- **Backtest exit reasons:** `{report['exit_reasons']}`")
    print(f"- **Paper entry-price bands:** `{journal['entry_price_bands']}`")
    print(f"- **Backtest entry-price bands:** `{report['entry_price_bands']}`")
    print("")
    print("### Replay Assumptions")
    print(f"- **Assumptions:** `{report['replay_assumptions']}`")
    if report.get("skip_counts"):
        print(f"- **Top skip counts:** `{report['skip_counts']}`")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare one paper session slice to one backtest report.")
    parser.add_argument("--report", required=True, help="Path to backtest JSON report")
    parser.add_argument("--session", help="Session id or session directory path. Defaults to latest resumable session.")
    parser.add_argument("--strategy", help="Override strategy filter (e.g. bitcoin, sol_macro)")
    parser.add_argument("--window", help="Override window filter (e.g. 5m, 15m)")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of markdown")
    args = parser.parse_args()

    report_path = Path(args.report)
    session_dir = _session_dir_from_arg(args.session)
    report_section = _load_report(report_path)
    strategy = _infer_strategy(report_section, args.strategy)
    window_size = _infer_window(report_section, args.window)
    journal_rows = list(_iter_session_exits(session_dir))
    journal_summary = _summarize_journal(journal_rows, strategy, window_size)
    report_summary = _summarize_report(report_section)
    diff = _diff_metrics(journal_summary, report_summary)

    payload = {
        "session": str(session_dir),
        "report": str(report_path),
        "strategy": strategy,
        "window_size": window_size,
        "paper": journal_summary,
        "backtest": report_summary,
        "delta": diff,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_markdown(
            session_dir=session_dir,
            report_path=report_path,
            strategy=strategy,
            window_size=window_size,
            journal=journal_summary,
            report=report_summary,
            diff=diff,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
