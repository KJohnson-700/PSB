"""Backfill derived historical beta-veto rows from live trade history.

Produces a reproducible dataset of rejected candidates that would have been
blocked by the global beta veto at the chosen ``(max_mean, min_n)`` setting.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.analysis.beta_veto_backfill import (  # noqa: E402
    build_beta_veto_backfill,
    write_json,
    write_jsonl,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--trades",
        type=Path,
        default=REPO_ROOT / "data" / "calibration" / "trades.jsonl",
    )
    ap.add_argument(
        "--rejected",
        type=Path,
        default=REPO_ROOT / "data" / "calibration" / "rejected_candidates.jsonl",
    )
    ap.add_argument(
        "--settled",
        type=Path,
        default=REPO_ROOT / "data" / "calibration" / "rejected_candidates_settled.jsonl",
    )
    ap.add_argument("--max-mean", type=float, required=True)
    ap.add_argument("--min-n", type=int, required=True)
    ap.add_argument(
        "--rows-out",
        type=Path,
        default=REPO_ROOT / "data" / "calibration" / "beta_veto_historical_rows.jsonl",
    )
    ap.add_argument(
        "--summary-out",
        type=Path,
        default=REPO_ROOT / "data" / "calibration" / "beta_veto_historical_summary.json",
    )
    args = ap.parse_args()

    rows, summary = build_beta_veto_backfill(
        trades_path=args.trades,
        rejected_path=args.rejected,
        settled_path=args.settled,
        beta_veto_max_mean=float(args.max_mean),
        beta_veto_min_n=int(args.min_n),
    )
    write_jsonl(args.rows_out, rows)
    write_json(args.summary_out, summary)
    print(
        f"wrote {len(rows)} rows to {args.rows_out} "
        f"and summary to {args.summary_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
