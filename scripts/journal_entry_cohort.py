#!/usr/bin/env python3
"""Filter journal entries.jsonl to ENTRY events for clean signal-quality stats.

PRICE_UPDATE and other events inflate row counts and (historically) showed edge=0;
always restrict to event==ENTRY when analyzing strategy WR inputs vs executions."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def _default_entries_file() -> Path | None:
    root = Path(__file__).resolve().parent.parent / "data" / "paper_trades"
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir(), key=lambda x: x.name, reverse=True):
        if not d.is_dir():
            continue
        ef = d / "entries.jsonl"
        try:
            if ef.is_file() and ef.stat().st_size > 0:
                return ef
        except OSError:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Summarize ENTRY rows from entries.jsonl (optionally one strategy)."
    )
    ap.add_argument(
        "--entries",
        type=Path,
        default=None,
        help="Path to entries.jsonl (default: newest non-empty session under data/paper_trades)",
    )
    ap.add_argument(
        "--event",
        default="ENTRY",
        help="event field to keep (default: ENTRY)",
    )
    ap.add_argument(
        "--strategy",
        default=None,
        help="Only include this strategy (e.g. eth_macro)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Print one JSON object to stdout",
    )
    args = ap.parse_args()

    path = args.entries or _default_entries_file()
    if not path or not path.is_file():
        print("No entries.jsonl found. Pass --entries /path/to/entries.jsonl", file=sys.stderr)
        return 1

    rows: list[dict] = []
    bad = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if d.get("event") != args.event:
                continue
            if args.strategy and d.get("strategy") != args.strategy:
                continue
            rows.append(d)

    edges = [float(r.get("edge") or 0) for r in rows]
    confs = [float(r.get("confidence") or 0) for r in rows]
    empty_reason = sum(1 for r in rows if not (r.get("reason") or "").strip())

    out = {
        "entries_file": str(path.resolve()),
        "event_filter": args.event,
        "strategy_filter": args.strategy,
        "count": len(rows),
        "parse_errors": bad,
        "edge_min": min(edges) if edges else None,
        "edge_max": max(edges) if edges else None,
        "edge_mean": round(statistics.mean(edges), 6) if edges else None,
        "confidence_mean": round(statistics.mean(confs), 6) if confs else None,
        "empty_reason_count": empty_reason,
        "by_strategy": {},
    }
    for r in rows:
        s = str(r.get("strategy") or "?")
        out["by_strategy"][s] = out["by_strategy"].get(s, 0) + 1

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"File: {path.resolve()}")
    print(f"Filter: event={args.event!r}" + (f", strategy={args.strategy!r}" if args.strategy else ""))
    print(f"ENTRY rows: {len(rows)} (json parse errors in file: {bad})")
    if out["by_strategy"]:
        print("By strategy:")
        for k, v in sorted(out["by_strategy"].items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {k}: {v}")
    if edges:
        print(f"edge: min={out['edge_min']:.4f} max={out['edge_max']:.4f} mean={out['edge_mean']:.4f}")
    if confs:
        print(f"confidence mean: {out['confidence_mean']:.4f}")
    print(f"empty reason string: {empty_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
