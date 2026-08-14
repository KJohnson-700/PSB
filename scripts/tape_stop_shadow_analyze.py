#!/usr/bin/env python3
"""tape-aware-stop SHADOW analyzer — READ-ONLY pass/fail read for the never-green exit lever.

Reads data/calibration/tape_stop_shadow.jsonl (written LIVE by live_testing.py when a LOSING
position has the tape turned against it) and joins each would-cut row to the trade's FINAL
outcome (trades_settled.jsonl actual_pnl, by trade_id). Per window it reports:

  WOULD-SAVE : the trade ended a LOSER worse than the would-cut mark -> cutting here helped.
  FALSE-CUT  : the trade RECOVERED to a win (or a smaller loss than the mark) -> cutting hurt.
  net verdict per window = would-save% ; a tape-stop is worth going live on a window only
  where would-save clearly beats false-cut (expect 1h favorable ~60%, 5m coin-flip ~49%).

The shadow row's `pnl_pct_at_shadow` is the counterfactual EXIT pnl% (had we cut there); the
settled `actual_pnl` (with `size`/`cost_basis`) is what actually happened. Empty until the
shadow has run live (restart-class), i.e. after the next restart. Usage:
  .venv/bin/python scripts/tape_stop_shadow_analyze.py
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHADOW = ROOT / "data/calibration/tape_stop_shadow.jsonl"
SETTLED = ROOT / "data/calibration/trades_settled.jsonl"


def main():
    if not SHADOW.exists() or SHADOW.stat().st_size == 0:
        print("tape_stop_shadow.jsonl is empty — the shadow is restart-class (live_testing.py).")
        print("It starts logging after the next restart; re-run this once would-cut rows accrue.")
        return

    # final outcome by trade_id
    outcome = {}
    for line in open(SETTLED):
        try:
            d = json.loads(line)
        except Exception:
            continue
        tid = d.get("trade_id")
        if tid is not None and d.get("actual_pnl") is not None:
            cb = d.get("cost_basis") or (float(d.get("entry_price") or 0) * float(d.get("size") or 0)) or 1.0
            outcome[tid] = {"pnl": float(d["actual_pnl"]),
                            "pnl_pct": float(d["actual_pnl"]) / cb if cb else 0.0}

    per_win = defaultdict(lambda: {"n": 0, "save": 0, "false": 0, "unjoined": 0,
                                   "save_$": 0.0, "false_$": 0.0})
    total = matched = 0
    for line in open(SHADOW):
        try:
            r = json.loads(line)
        except Exception:
            continue
        total += 1
        w = r.get("window_size") or "?"
        oc = outcome.get(r.get("trade_id"))
        b = per_win[w]
        b["n"] += 1
        if oc is None:
            b["unjoined"] += 1
            continue
        matched += 1
        cut_pct = float(r.get("pnl_pct_at_shadow", 0.0))   # where we WOULD have cut
        final_pct = oc["pnl_pct"]                          # where it actually ended
        # would-SAVE: final was WORSE (more negative) than the cut mark -> cutting avoided the extra loss.
        # FALSE-CUT: final was BETTER than the cut mark (recovered / smaller loss / win).
        if final_pct < cut_pct:
            b["save"] += 1
            b["save_$"] += (cut_pct - final_pct) * abs(oc["pnl"] / (final_pct or -1e-9))  # rough $ avoided
        else:
            b["false"] += 1
            b["false_$"] += (final_pct - cut_pct) * abs(oc["pnl"] / (final_pct or 1e-9))

    print("=" * 74)
    print(f"TAPE-AWARE-STOP SHADOW  (would-cut rows={total}, joined to outcome={matched})")
    print("=" * 74)
    print(f"{'window':7s} {'n':>4s} {'save':>5s} {'false':>6s} {'save%':>6s}   verdict")
    for w in sorted(per_win):
        b = per_win[w]
        j = b["save"] + b["false"]
        sp = (100.0 * b["save"] / j) if j else float("nan")
        verdict = "—"
        if j >= 10:
            verdict = "SHIP live" if sp >= 60 else ("marginal" if sp >= 52 else "DO NOT ship")
        print(f"{w:7s} {b['n']:>4d} {b['save']:>5d} {b['false']:>6d} {sp:>5.0f}%   {verdict}"
              + (f"  (unjoined {b['unjoined']})" if b["unjoined"] else ""))
    print("\n  save% = of joined would-cuts, how often cutting there beat riding to the end.")
    print("  Ship a tape-stop LIVE only on windows with save% >= 60 and n >= 10 (per-lane).")


if __name__ == "__main__":
    main()
