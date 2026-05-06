#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.underperformance_audit import (
    build_underperformance_report,
    discover_recent_sessions,
    load_backtest_action_summary,
    load_buy_no_skips,
    load_closed_trades,
    render_underperformance_markdown,
)


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    default_paper_root = repo_root / "data" / "paper_trades"
    default_reports_dir = repo_root / "data" / "backtest" / "reports"
    default_out_dir = repo_root / "docs" / "session_reports"

    ap = argparse.ArgumentParser(description="Diagnose live strategy underperformance.")
    ap.add_argument(
        "--baseline-session",
        default="test_20260504_034719",
        help="Single baseline session directory name under data/paper_trades.",
    )
    ap.add_argument(
        "--recent-sessions",
        default=None,
        help="Comma-separated recent session directory names. Default: auto-discover sessions after baseline with at least 5 exits.",
    )
    ap.add_argument(
        "--paper-root",
        type=Path,
        default=default_paper_root,
    )
    ap.add_argument(
        "--reports-dir",
        type=Path,
        default=default_reports_dir,
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=default_out_dir,
    )
    ap.add_argument(
        "--label",
        default="underperformance_diagnosis_20260505",
        help="Output stem written to docs/session_reports/<label>.{md,json}",
    )
    args = ap.parse_args()

    baseline_sessions = [args.baseline_session]
    if args.recent_sessions:
        recent_sessions = [s.strip() for s in args.recent_sessions.split(",") if s.strip()]
    else:
        recent_sessions = discover_recent_sessions(
            args.paper_root,
            after_session=args.baseline_session,
            min_exits=5,
        )
    if not recent_sessions:
        raise SystemExit("No recent sessions found for diagnosis.")

    baseline_rows = load_closed_trades(args.paper_root, baseline_sessions)
    recent_rows = load_closed_trades(args.paper_root, recent_sessions)
    skip_rows = load_buy_no_skips(args.paper_root, recent_sessions)
    backtests = load_backtest_action_summary(args.reports_dir)

    report = build_underperformance_report(
        baseline_rows=baseline_rows,
        recent_rows=recent_rows,
        skip_rows=skip_rows,
        backtests=backtests,
        baseline_sessions=baseline_sessions,
        recent_sessions=recent_sessions,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / f"{args.label}.json"
    md_path = args.out_dir / f"{args.label}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_underperformance_markdown(report), encoding="utf-8")
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
