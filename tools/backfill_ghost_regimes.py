#!/usr/bin/env python3
"""Backfill market-regime labels onto settled ghost candidates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.analysis.ghost_calibration import (  # noqa: E402
    DEFAULT_REGIME_LOG,
    DEFAULT_SETTLED_LOG,
    REGIME_MATCH_MAX_AGE_SEC,
    backfill_settled_regimes,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill regime labels on rejected_candidates_settled.jsonl."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_SETTLED_LOG)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Defaults to in-place rewrite of --input.",
    )
    parser.add_argument("--regime-log", type=Path, default=DEFAULT_REGIME_LOG)
    parser.add_argument("--max-age-sec", type=float, default=REGIME_MATCH_MAX_AGE_SEC)
    parser.add_argument("--force", action="store_true", help="Recompute existing labels.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    summary = backfill_settled_regimes(
        input_path=args.input,
        output_path=args.output,
        regime_path=args.regime_log,
        max_age_sec=args.max_age_sec,
        force=args.force,
        dry_run=args.dry_run,
    )
    if args.json:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"Processed {args.input}")
    if args.output:
        print(f"  output          : {args.output}")
    print(f"  rows            : {summary['rows']}")
    print(f"  matched         : {summary['matched']}")
    print(f"  unmatched       : {summary['unmatched']}")
    print(f"  already labelled: {summary['already_labelled']}")
    print(f"  copied metadata : {summary['rejected_metadata_copied']}")
    print(f"  written         : {summary['written']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
