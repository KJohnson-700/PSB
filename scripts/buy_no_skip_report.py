#!/usr/bin/env python3
"""Summarize BUY_NO suppression events from paper-trade journals."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _default_entries_files() -> list[Path]:
    root = Path(__file__).resolve().parent.parent / "data" / "paper_trades"
    if not root.is_dir():
        return []
    return [
        session / "entries.jsonl"
        for session in sorted(root.iterdir(), key=lambda p: p.name, reverse=True)
        if session.is_dir() and (session / "entries.jsonl").is_file()
    ][:5]


def _load_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") != "BUY_NO_SKIP":
                    continue
                extra = row.get("extra") or {}
                rows.append(
                    {
                        "session": path.parent.name,
                        "strategy": row.get("strategy") or extra.get("strategy") or "?",
                        "window": extra.get("window_size") or "?",
                        "reason": row.get("reason") or extra.get("skip_reason") or "?",
                        "yes_price": float(extra.get("yes_price") or 0.0),
                        "edge": float(row.get("edge") or extra.get("edge") or 0.0),
                        "min_edge": float(extra.get("effective_min_edge") or 0.0),
                        "rsi": float(extra.get("rsi") or 0.0),
                        "htf_bias": extra.get("htf_bias") or "",
                        "alt_1h_trend": extra.get("alt_1h_trend") or "",
                    }
                )
    return rows


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "count": 0,
            "avg_edge": 0.0,
            "avg_min_edge": 0.0,
            "avg_gap": 0.0,
            "avg_yes_price": 0.0,
        }
    gaps = [r["min_edge"] - r["edge"] for r in rows]
    return {
        "count": len(rows),
        "avg_edge": round(statistics.mean(r["edge"] for r in rows), 4),
        "avg_min_edge": round(statistics.mean(r["min_edge"] for r in rows), 4),
        "avg_gap": round(statistics.mean(gaps), 4),
        "avg_yes_price": round(statistics.mean(r["yes_price"] for r in rows), 4),
        "near_miss_count": sum(1 for gap in gaps if 0 <= gap <= 0.02),
    }


def build_report(paths: list[Path]) -> dict[str, Any]:
    rows = _load_rows(paths)
    by_strategy_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_strategy_window: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_strategy_reason[f"{row['strategy']}|{row['reason']}"].append(row)
        by_strategy_window[f"{row['strategy']}|{row['window']}"].append(row)
    return {
        "entries_files": [str(p.resolve()) for p in paths],
        "total_buy_no_skips": len(rows),
        "sessions": dict(Counter(r["session"] for r in rows)),
        "by_strategy_reason": {
            key: _stats(group) for key, group in sorted(by_strategy_reason.items())
        },
        "by_strategy_window": {
            key: _stats(group) for key, group in sorted(by_strategy_window.items())
        },
        "latest_samples": rows[-10:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=Path, action="append", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    paths = args.entries or _default_entries_files()
    paths = [p for p in paths if p.is_file()]
    report = build_report(paths)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"Files: {len(paths)}")
    print(f"BUY_NO skips: {report['total_buy_no_skips']}")
    print("\nBy strategy/reason")
    print("| strategy/reason | count | near misses | avg edge | avg min | avg gap | avg YES |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for key, stats in report["by_strategy_reason"].items():
        print(
            f"| {key} | {stats['count']} | {stats.get('near_miss_count', 0)} | "
            f"{stats['avg_edge']:.4f} | {stats['avg_min_edge']:.4f} | "
            f"{stats['avg_gap']:.4f} | {stats['avg_yes_price']:.4f} |"
        )
    print("\nBy strategy/window")
    print("| strategy/window | count | near misses | avg edge | avg min | avg gap | avg YES |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for key, stats in report["by_strategy_window"].items():
        print(
            f"| {key} | {stats['count']} | {stats.get('near_miss_count', 0)} | "
            f"{stats['avg_edge']:.4f} | {stats['avg_min_edge']:.4f} | "
            f"{stats['avg_gap']:.4f} | {stats['avg_yes_price']:.4f} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
