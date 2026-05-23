#!/usr/bin/env python3
"""Compute ghost-derived per-lane thresholds for the live calibrator.

Reads ``data/calibration/rejected_candidates_settled.jsonl``, buckets
ghost outcomes by translated live lane_id, and writes per-lane
recommendations to ``data/calibration/lane_thresholds.json``.

The bot itself can recompute on every ghost-settle cycle if
``lane_calibration.per_lane_thresholds.recompute_on_settle: true`` —
this script is for one-off review and dry-runs.

Examples:
    # Print the report without writing the file
    python3 scripts/compute_lane_thresholds.py --report

    # Compute + write with default thresholds (n>=100, WR<0.40 → veto)
    python3 scripts/compute_lane_thresholds.py --write

    # Tune thresholds
    python3 scripts/compute_lane_thresholds.py --report --min-n 200 --wr 0.35
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis.lane_thresholds import (
    DEFAULT_SETTLED_LOG,
    DEFAULT_THRESHOLDS_PATH,
    compute_lane_thresholds,
    summarize_thresholds,
    write_lane_thresholds,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--settled",
        type=Path,
        default=DEFAULT_SETTLED_LOG,
        help="path to rejected_candidates_settled.jsonl",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_THRESHOLDS_PATH,
        help="output path for lane_thresholds.json",
    )
    ap.add_argument(
        "--min-n",
        type=int,
        default=100,
        help="minimum bucket size to consider a lane (default 100)",
    )
    ap.add_argument(
        "--wr",
        type=float,
        default=0.40,
        help="ghost WR threshold below which to recommend veto (default 0.40)",
    )
    ap.add_argument(
        "--max-mean",
        type=float,
        default=0.40,
        help="recommended β floor to apply to overridden lanes (default 0.40)",
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help="write lane_thresholds.json (default: report only, no write)",
    )
    ap.add_argument(
        "--report",
        action="store_true",
        help="print summary to stdout",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="print full payload as JSON to stdout (overrides --report)",
    )
    args = ap.parse_args()

    payload = compute_lane_thresholds(
        settled_path=args.settled,
        min_bucket_n=args.min_n,
        wr_veto_threshold=args.wr,
        recommended_max_mean=args.max_mean,
    )

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.report or not args.write:
        print(summarize_thresholds(payload))

    if args.write:
        ok = write_lane_thresholds(payload, path=args.out)
        if ok:
            print(f"\nwrote {args.out}")
        else:
            print(f"\nWRITE FAILED: {args.out}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
