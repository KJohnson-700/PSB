#!/usr/bin/env python3
"""Aggregate EXIT rows from paper `entries.jsonl` by session folder prefix.

Example:

    .venv/bin/python scripts/slice_paper_exits_by_session_prefix.py --prefix test_20260511

Exit reasons match `PositionExitManager` / journal: `updown_stop_loss` is the **%** stop
(`updown_stop_loss_pct`); `updown_time_stop` is the **late-window cents** stop
(`updown_stop_cents` + `updown_exit_window_mins`). See `docs/session_reports/may11_2026_exit_reason_reconciliation.md`.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


def _window_from_question(q: str) -> str:
    m = re.search(
        r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
        q or "",
        re.IGNORECASE,
    )
    if not m:
        return "unknown"

    def to24(h: str, mi: str, ap: str) -> int:
        h_i = int(h)
        mi_i = int(mi)
        ap_u = ap.upper()
        if ap_u == "AM" and h_i == 12:
            h_i = 0
        elif ap_u == "PM" and h_i != 12:
            h_i += 12
        return h_i * 60 + mi_i

    a = to24(m.group(1), m.group(2), m.group(3))
    b = to24(m.group(4), m.group(5), m.group(6))
    d = b - a
    if d <= 0:
        d += 24 * 60
    if d <= 6:
        return "5m"
    if d >= 23:
        return "30m"
    return "15m"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--paper-root",
        type=Path,
        default=Path("data/paper_trades"),
        help="Root containing test_* session folders",
    )
    ap.add_argument(
        "--prefix",
        required=True,
        help="Session directory name prefix (e.g. test_20260511)",
    )
    args = ap.parse_args()
    root: Path = args.paper_root
    prefix: str = args.prefix

    sessions = sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefix))
    if not sessions:
        print(f"No sessions under {root} matching prefix {prefix!r}")
        raise SystemExit(1)

    by_reason: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "pnl": 0.0, "sessions": set()}
    )
    by_reason_win: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"n": 0, "pnl": 0.0})
    )
    rows_out: list[dict[str, object]] = []

    for sess in sessions:
        path = sess / "entries.jsonl"
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                if e.get("event") != "EXIT":
                    continue
                reason = (e.get("reason") or "").strip()
                ex = (e.get("extra") or {}).get("exit_reason")
                if ex:
                    reason = str(ex).strip()
                pnl = float(e.get("pnl") or 0)
                strat = e.get("strategy") or ""
                q = e.get("market_question") or ""
                win = _window_from_question(q)
                agg = by_reason[reason]
                agg["n"] = int(agg["n"]) + 1
                agg["pnl"] = float(agg["pnl"]) + pnl
                cast_sessions = agg["sessions"]
                assert isinstance(cast_sessions, set)
                cast_sessions.add(sess.name)
                wr = by_reason_win[reason][win]
                wr["n"] += 1
                wr["pnl"] += pnl
                rows_out.append(
                    {
                        "session": sess.name,
                        "strategy": strat,
                        "action": e.get("action"),
                        "reason": reason,
                        "pnl": pnl,
                        "window": win,
                        "question": (q[:72] + "…") if len(q) > 72 else q,
                    }
                )

    print(f"Sessions scanned: {len(sessions)}")
    print(f"Total EXIT rows: {len(rows_out)}")
    if not rows_out:
        print("(No EXIT rows in entries.jsonl for these sessions.)")
        return

    print("\nBy exit_reason (journal `reason` / `extra.exit_reason`):")
    for reason, agg in sorted(by_reason.items(), key=lambda kv: -int(kv[1]["n"])):
        sess_set = agg["sessions"]
        assert isinstance(sess_set, set)
        print(
            f"  {reason!r}: n={int(agg['n'])}, pnl=${float(agg['pnl']):.2f}, "
            f"sessions={sorted(sess_set)}"
        )

    print("\nPer exit_reason × window (ET range minutes):")
    for reason in sorted(by_reason_win.keys()):
        print(f"  [{reason}]")
        for win, wagg in sorted(by_reason_win[reason].items()):
            print(f"    {win}: n={int(wagg['n'])}, pnl=${wagg['pnl']:.2f}")


if __name__ == "__main__":
    main()
