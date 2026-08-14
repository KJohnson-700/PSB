#!/usr/bin/env python3
"""chop_gated_exit_replay — measure the STOP-vs-HOLD question, segmented by tape regime.

CONTEXT (2026-08-03 forensics, REALIZED): updown_stop_loss = 36 trades / 3% exit-WR / -92;
never_green_cut = 9 / 0% exit-WR / 56% would-have-held-won. The stops are the loss. Operator
wants regime-detection to live in the AI layer; this script does NOT gate anything live — it
MEASURES whether "hold instead of stop" helps specifically in CHOP vs TREND, so the AI layer
has a target to hit.

HONESTY (operator rule: shadow/counterfactual = HINT not truth):
  - REALIZED columns (exit_reason, exit-WR, realized_pnl) are hard facts.
  - HELD columns (held_pnl, held_win) are COUNTERFACTUAL — labeled [hint]. In THIS chop they
    look great because price mean-reverted; in a TREND they would invert. Do not ship "hold
    always" off this. The number that matters is the *gap* pattern, and it must be confirmed
    by a LIVE A/B before any behavior change.

Regime proxy at ENTRY (from tape_map.jsonl join): CHOP = tape FLAT or confidence < CONF_GATE;
TREND = directional call with conf >= CONF_GATE. (This proxy is itself a hint; the AI layer
replaces it.)

Reads:  data/calibration/trades_settled.jsonl  +  data/calibration/tape_map.jsonl
Prints: per-regime realized exit outcome + the counterfactual hold delta, per exit_reason.
Read-only. Run:  .venv/bin/python scripts/chop_gated_exit_replay.py [--conf-gate 0.6]
"""
from __future__ import annotations
import argparse
import bisect
import collections
import datetime
import json

SETTLED = "data/calibration/trades_settled.jsonl"
TAPE = "data/calibration/tape_map.jsonl"


def _load_tape():
    per = collections.defaultdict(list)
    for l in open(TAPE):
        try:
            d = json.loads(l)
        except Exception:
            continue
        ts = d.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        per[d.get("asset")].append((ts, str(d.get("direction") or "").upper(),
                                    float(d.get("confidence", 0) or 0)))
    for a in per:
        per[a].sort()
    return per


def _tape_at(per, asset, ts, tol=120):
    rows = per.get(asset) or []
    if not rows:
        return None, None
    tl = [r[0] for r in rows]
    j = bisect.bisect_left(tl, ts)
    best = None
    for k in (j - 1, j):
        if 0 <= k < len(rows) and abs(rows[k][0] - ts) <= tol:
            if best is None or abs(rows[k][0] - ts) < abs(rows[best][0] - ts):
                best = k
    if best is None:
        return None, None
    return rows[best][1], rows[best][2]


def _ts(r):
    t = r.get("ts")
    if isinstance(t, (int, float)):
        return t
    if isinstance(t, str):
        try:
            return datetime.datetime.fromisoformat(t).timestamp()
        except Exception:
            return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf-gate", type=float, default=0.6)
    args = ap.parse_args()

    tape = _load_tape()
    rows = [json.loads(l) for l in open(SETTLED) if l.strip()]

    # regime-tag each realized trade
    seg = {"CHOP": [], "TREND": [], "NO_TAPE": []}
    for r in rows:
        ts = _ts(r)
        d, conf = (_tape_at(tape, r.get("strategy"), ts) if ts else (None, None))
        if d is None:
            seg["NO_TAPE"].append(r)
        elif d == "FLAT" or (conf or 0) < args.conf_gate:
            seg["CHOP"].append(r)
        else:
            seg["TREND"].append(r)

    def won(r):
        return float(r.get("actual_pnl", 0) or 0) > 0

    print("=" * 78)
    print("CHOP-GATED EXIT REPLAY  (REALIZED facts | [hint]=counterfactual held)")
    print(f"regime proxy: CHOP = tape FLAT or conf<{args.conf_gate}   (n_settled={len(rows)})")
    print("=" * 78)
    for regime in ("CHOP", "TREND", "NO_TAPE"):
        rs = seg[regime]
        if not rs:
            continue
        realized = sum(float(r.get("actual_pnl", 0) or 0) for r in rs)
        held = sum(float(r.get("held_pnl", 0) or 0) for r in rs)
        wr = 100 * sum(won(r) for r in rs) / len(rs)
        print(f"\n── {regime}  n={len(rs)}  realized_WR={wr:.0f}%  "
              f"realized_pnl={realized:+.2f}   held_pnl[hint]={held:+.2f}   "
              f"gap[hint]={held - realized:+.2f}")
        # per exit_reason within regime
        er = collections.defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0, "held": 0.0, "regret": 0})
        for r in rs:
            x = er[r.get("exit_reason", "?")]
            x["n"] += 1
            x["w"] += won(r)
            x["pnl"] += float(r.get("actual_pnl", 0) or 0)
            x["held"] += float(r.get("held_pnl", 0) or 0)
            if r.get("held_win") and not won(r):
                x["regret"] += 1
        print(f'   {"exit_reason":<22} {"n":>3} {"exitWR":>6} {"realized":>9} {"held[hint]":>11} {"regret":>7}')
        for k in sorted(er, key=lambda k: er[k]["pnl"]):
            x = er[k]
            print(f'   {k:<22} {x["n"]:>3} {100*x["w"]/x["n"]:>5.0f}% '
                  f'{x["pnl"]:>+9.2f} {x["held"]:>+11.2f} {x["regret"]:>4}/{x["n"]:<2}')

    print("\nREAD: the ROBUST signal is realized exit-WR by reason per regime. The held[hint]")
    print("gap says how much a HOLD policy *might* recover IN THIS TAPE — confirm with a LIVE")
    print("A/B before any change; it inverts in a trend. Regime gating -> AI layer, not rules.")


if __name__ == "__main__":
    main()
