#!/usr/bin/env python3
"""Summarize closed paper trades by strategy/window/action for calibration."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _default_entries_file() -> Path | None:
    root = Path(__file__).resolve().parent.parent / "data" / "paper_trades"
    if not root.is_dir():
        return None
    for session in sorted(root.iterdir(), key=lambda x: x.name, reverse=True):
        entries = session / "entries.jsonl"
        if session.is_dir() and entries.is_file() and entries.stat().st_size > 0:
            return entries
    return None


def _load_closed(entries_path: Path) -> list[dict[str, Any]]:
    entries_by_id: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    with entries_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            trade_id = str(row.get("trade_id") or "")
            if row.get("event") == "ENTRY" and trade_id:
                entries_by_id[trade_id] = row
            elif row.get("event") == "EXIT":
                entry = entries_by_id.get(trade_id, {})
                extra = entry.get("extra") or {}
                closed.append(
                    {
                        "trade_id": trade_id,
                        "strategy": row.get("strategy") or entry.get("strategy") or "?",
                        "window": extra.get("window_size") or "?",
                        "lane_id": extra.get("lane_id"),
                        "lane_side": extra.get("lane_side"),
                        "lane_regime": extra.get("lane_regime"),
                        "entry_family": extra.get("entry_family"),
                        "promotion_state": extra.get("promotion_state"),
                        "ai_used": bool(extra.get("ai_used", False)),
                        "action": entry.get("action") or row.get("action") or "?",
                        "pnl": float(row.get("pnl") or 0.0),
                        "exit_reason": row.get("reason") or "?",
                        "entry_price": float(entry.get("entry_price") or row.get("entry_price") or 0.0),
                        "edge": float(entry.get("edge") or 0.0),
                        "confidence": float(entry.get("confidence") or 0.0),
                        "minutes_to_end": extra.get("minutes_to_market_end"),
                        "size": float(entry.get("size") or row.get("size") or 0.0),
                    }
                )
    return closed


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [r["pnl"] for r in rows if r["pnl"] > 0]
    losses = [r["pnl"] for r in rows if r["pnl"] < 0]
    returns = [(r["pnl"] / r["size"]) for r in rows if float(r.get("size") or 0.0) > 0]
    n = len(rows)
    avg_edge = statistics.mean([r["edge"] for r in rows]) if rows else 0.0
    avg_ret = statistics.mean(returns) if returns else 0.0
    return {
        "trades": n,
        "pnl": round(sum(r["pnl"] for r in rows), 4),
        "expectancy": round(sum(r["pnl"] for r in rows) / n, 4) if n else 0.0,
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "avg_win": round(statistics.mean(wins), 4) if wins else 0.0,
        "avg_loss": round(statistics.mean(losses), 4) if losses else 0.0,
        "avg_edge": round(avg_edge, 4),
        "avg_confidence": round(statistics.mean([r["confidence"] for r in rows]), 4) if rows else 0.0,
        "avg_entry_price": round(statistics.mean([r["entry_price"] for r in rows]), 4) if rows else 0.0,
        "avg_realized_return_on_notional": round(avg_ret, 4),
        "edge_realized_gap": round(avg_edge - avg_ret, 4) if returns else 0.0,
        "ai_trades": sum(1 for r in rows if r.get("ai_used")),
        "exit_reasons": dict(Counter(str(r["exit_reason"]) for r in rows)),
    }


def build_report(entries_path: Path) -> dict[str, Any]:
    rows = _load_closed(entries_path)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = str(row.get("lane_id") or "").strip() or f"{row['strategy']}|{row['window']}|{row['action']}"
        groups[key].append(row)
    return {
        "entries_file": str(entries_path.resolve()),
        "closed_trades": len(rows),
        "overall": _summary(rows),
        "lanes": {key: _summary(group) for key, group in sorted(groups.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Closed-trade lane calibration report.")
    parser.add_argument("--entries", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    entries = args.entries or _default_entries_file()
    if not entries or not entries.is_file():
        print("No entries.jsonl found. Pass --entries /path/to/entries.jsonl", file=sys.stderr)
        return 1

    report = build_report(entries)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"File: {report['entries_file']}")
    print(f"Closed trades: {report['closed_trades']}")
    print("| lane | trades | pnl | exp | WR | avg edge | avg ret | gap | ai | exits |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for lane, stats in report["lanes"].items():
        exits = ", ".join(f"{k}:{v}" for k, v in sorted(stats["exit_reasons"].items()))
        print(
            f"| {lane} | {stats['trades']} | {stats['pnl']:+.2f} | "
            f"{stats['expectancy']:+.2f} | {stats['win_rate']:.1%} | "
            f"{stats['avg_edge']:.3f} | {stats['avg_realized_return_on_notional']:.3f} | "
            f"{stats['edge_realized_gap']:+.3f} | {stats['ai_trades']} | {exits} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
