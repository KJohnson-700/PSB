#!/usr/bin/env python3
"""hold_benefit_shadow — per-lane hold-to-resolution benefit tracker (OBSERVE-ONLY).

WHY: the hold-to-resolution audit (2026-08-03) showed hold-vs-exit benefit is
REGIME-DEPENDENT — sol total hold_minus_exit was NEGATIVE historically, then +44 in
a mean-reverting chop. A blanket "hold this lane" flag is a static-tape bet that inverts
with regime. This script does NOT flip any flag; it just SNAPSHOTS per-lane hold benefit
over time so we can tell a durable hold-winner (majority hold-better AND positive sum,
n>=MIN_N, across >=2 sessions) from a one-chop artifact.

A lane only earns a "SHIP-CANDIDATE" tag here when, on the CURRENT settled record:
  n >= MIN_N  AND  hold_better_frac >= MAJORITY  AND  sum_hme > 0  AND  spans >= 2 sessions.
Everything else is logged but tagged HOLD-SHADOW (do not ship). Decisions stay operator's.

Reads:  data/calibration/trades_settled.jsonl  (realized, settled — the truth record)
Appends:data/calibration/hold_benefit_shadow_log.jsonl  (one snapshot dict per run)
Prints: a ranked per-lane table + the ship-candidate shortlist.

Run:  .venv/bin/python scripts/hold_benefit_shadow.py
Daemonize (optional): nohup .venv/bin/python scripts/hold_benefit_shadow.py --loop 1800 \
    >> data/calibration/hold_benefit_shadow.log 2>&1 &
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import time
from datetime import datetime, timezone

SETTLED = "data/calibration/trades_settled.jsonl"
OUT = "data/calibration/hold_benefit_shadow_log.jsonl"

MIN_N = 20          # per-lane min settled trades before a hold flag is even considered
MAJORITY = 0.60     # >=60% of the lane's trades must be hold-better (not just positive sum)


def _rows(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def analyze(rows):
    # per (strategy, window, side): sum_hme, n, n_hold_better, sessions
    agg = collections.defaultdict(lambda: {"sum": 0.0, "n": 0, "better": 0, "sess": set()})
    total = {"sum": 0.0, "n": 0}
    for r in rows:
        hme = r.get("hold_minus_exit_pnl")
        if hme is None:
            continue
        try:
            hme = float(hme)
        except Exception:
            continue
        key = (r.get("strategy"), r.get("window"), r.get("action"))
        a = agg[key]
        a["sum"] += hme
        a["n"] += 1
        a["better"] += 1 if hme > 0 else 0
        a["sess"].add(r.get("session_id"))
        total["sum"] += hme
        total["n"] += 1
    lanes = []
    for key, a in agg.items():
        n = a["n"]
        frac = a["better"] / n if n else 0.0
        nsess = len(a["sess"])
        candidate = (n >= MIN_N and frac >= MAJORITY and a["sum"] > 0 and nsess >= 2)
        lanes.append({
            "lane": f'{(key[0] or "?").replace("_macro","")}|{key[1]}|{(key[2] or "?").replace("BUY_","")}',
            "n": n, "hold_better": a["better"], "hold_better_frac": round(frac, 3),
            "sum_hme": round(a["sum"], 2), "sessions": nsess,
            "verdict": "SHIP-CANDIDATE" if candidate else "HOLD-SHADOW",
        })
    lanes.sort(key=lambda x: -x["sum_hme"])
    return lanes, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="seconds between runs; 0 = one-shot")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    while True:
        rows = _rows(SETTLED)
        lanes, total = analyze(rows)
        ts = datetime.now(timezone.utc).isoformat()
        snap = {"ts": ts, "settled_rows": len(rows),
                "total_hold_minus_exit": round(total["sum"], 2), "total_n": total["n"],
                "min_n": MIN_N, "majority": MAJORITY, "lanes": lanes}
        with open(OUT, "a") as f:
            f.write(json.dumps(snap) + "\n")

        if not args.quiet:
            cands = [l for l in lanes if l["verdict"] == "SHIP-CANDIDATE"]
            print(f"[{ts}] settled={len(rows)} total_hold_minus_exit={total['sum']:+.2f} "
                  f"(n={total['n']})  ship-candidates={len(cands)}")
            print(f'  {"lane":<22} {"n":>3} {"better":>7} {"sum_hme":>8}  verdict')
            for l in lanes:
                print(f'  {l["lane"]:<22} {l["n"]:>3} {l["hold_better"]:>3}/{l["n"]:<3} '
                      f'{l["sum_hme"]:>+8.2f}  {l["verdict"]}')
            if cands:
                print("  SHIP-CANDIDATES (n>=%d, >=%.0f%% hold-better, +sum, >=2 sessions):"
                      % (MIN_N, MAJORITY * 100))
                for c in cands:
                    print(f'    -> {c["lane"]}  {c["hold_better"]}/{c["n"]}  {c["sum_hme"]:+.2f}')
            else:
                print("  (no lane clears the bar yet — all HOLD-SHADOW, keep accumulating)")

        if args.loop <= 0:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
