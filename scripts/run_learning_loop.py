#!/usr/bin/env python3
"""Run the journal learning cycle once (stats + proposal JSON + vault append).

Uses ``config/settings.yaml`` ``learning_loop`` block. Does not start the bot.

Example:
  .venv/bin/python scripts/run_learning_loop.py
  .venv/bin/python scripts/run_learning_loop.py --no-archive
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import yaml

from src.analysis.journal_learning import (
    log_learning_summary_to_logger,
    run_learning_cycle,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--config",
        default=str(REPO / "config" / "settings.yaml"),
        help="Path to settings.yaml",
    )
    ap.add_argument(
        "--no-archive",
        action="store_true",
        help="Only scan data/paper_trades (skip paper_trades_archive)",
    )
    ap.add_argument(
        "--no-write",
        action="store_true",
        help="Compute payloads only; skip JSON + vault append",
    )
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    payload = run_learning_cycle(
        cfg,
        include_archive=not args.no_archive,
        write_files=not args.no_write,
    )
    log_learning_summary_to_logger(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
