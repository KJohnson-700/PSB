#!/usr/bin/env python3
"""Estimate how journaled est_prob aligns with realized YES outcomes."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
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


def _bucket_label(prob: float, width: float) -> str:
    floor = max(0.0, min(1.0, int(prob / width) * width))
    ceil = min(1.0, floor + width)
    return f"{floor:.2f}-{ceil:.2f}"


def _resolved_yes_outcome(exit_row: dict[str, Any]) -> int | None:
    extra = exit_row.get("extra") or {}
    outcome = str(extra.get("outcome_won") or "").upper()
    if outcome == "YES":
        return 1
    if outcome == "NO":
        return 0
    reason = str(exit_row.get("reason") or "")
    if "RESOLVED:YES" in reason:
        return 1
    if "RESOLVED:NO" in reason:
        return 0
    return None


def _load_calibration_rows(entries_path: Path, bucket_width: float) -> list[dict[str, Any]]:
    entries_by_id: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
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
                continue
            if row.get("event") != "EXIT" or not trade_id:
                continue
            entry = entries_by_id.get(trade_id)
            if not entry:
                continue
            extra = entry.get("extra") or {}
            est_prob = extra.get("raw_est_prob", extra.get("est_prob"))
            if est_prob is None:
                continue
            try:
                est_prob_f = float(est_prob)
            except (TypeError, ValueError):
                continue
            actual_yes = _resolved_yes_outcome(row)
            if actual_yes is None:
                continue
            side_source = str(extra.get("side_source") or "unknown")
            rows.append(
                {
                    "trade_id": trade_id,
                    "strategy": str(entry.get("strategy") or row.get("strategy") or "?"),
                    "action": str(entry.get("action") or row.get("action") or "?"),
                    "window_size": str(extra.get("window_size") or "?"),
                    "side_source": side_source,
                    "est_prob": est_prob_f,
                    "actual_yes": actual_yes,
                    "calibration_error": actual_yes - est_prob_f,
                    "bucket": _bucket_label(est_prob_f, bucket_width),
                    "oracle_basis_bps": extra.get("oracle_basis_bps"),
                    "reason": str(entry.get("reason") or ""),
                }
            )
    return rows


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {
            "trades": 0,
            "avg_est_prob": 0.0,
            "actual_yes_rate": 0.0,
            "avg_calibration_error": 0.0,
        }
    est_probs = [float(r["est_prob"]) for r in rows]
    actual_yes = [int(r["actual_yes"]) for r in rows]
    errs = [float(r["calibration_error"]) for r in rows]
    return {
        "trades": n,
        "avg_est_prob": round(statistics.mean(est_probs), 4),
        "actual_yes_rate": round(statistics.mean(actual_yes), 4),
        "avg_calibration_error": round(statistics.mean(errs), 4),
    }


def build_report(entries_path: Path, bucket_width: float = 0.05) -> dict[str, Any]:
    rows = _load_calibration_rows(entries_path, bucket_width)
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_side_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_strategy_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_bucket[row["bucket"]].append(row)
        by_strategy[row["strategy"]].append(row)
        by_side_source[row["side_source"]].append(row)
        by_strategy_source[f"{row['strategy']}|{row['side_source']}"].append(row)
    return {
        "entries_file": str(entries_path.resolve()),
        "eligible_trades": len(rows),
        "bucket_width": bucket_width,
        "overall": _summarize(rows),
        "buckets": {key: _summarize(group) for key, group in sorted(by_bucket.items())},
        "by_strategy": {key: _summarize(group) for key, group in sorted(by_strategy.items())},
        "by_side_source": {key: _summarize(group) for key, group in sorted(by_side_source.items())},
        "by_strategy_side_source": {
            key: _summarize(group) for key, group in sorted(by_strategy_source.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Journal est_prob calibration report.")
    parser.add_argument("--entries", type=Path, default=None)
    parser.add_argument("--bucket-width", type=float, default=0.05)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    entries = args.entries or _default_entries_file()
    if not entries or not entries.is_file():
        print("No entries.jsonl found. Pass --entries /path/to/entries.jsonl", file=sys.stderr)
        return 1

    report = build_report(entries, bucket_width=float(args.bucket_width))
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"File: {report['entries_file']}")
    print(f"Eligible resolved trades: {report['eligible_trades']}")
    print("| bucket | trades | avg est_prob | actual YES rate | avg error |")
    print("|---|---:|---:|---:|---:|")
    for bucket, stats in report["buckets"].items():
        print(
            f"| {bucket} | {stats['trades']} | {stats['avg_est_prob']:.3f} | "
            f"{stats['actual_yes_rate']:.3f} | {stats['avg_calibration_error']:+.3f} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
