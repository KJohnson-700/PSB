#!/usr/bin/env python3
"""Lane decision sheet — the calibration-overhaul gate (read-only).

WHY THIS EXISTS
---------------
Ghost EV (rejected_candidates_settled.jsonl) is HOLD-TO-RESOLUTION: no stops, no
take-profit, no time-decay, no slippage. Live P&L is governed by an exit layer the
ghost cannot see. Measured on taken trades (2026-06-02+), the ghost overstates live
EV by ~3.6pts on average and 15-20pts on the lanes it ranks highest. Tuning
admission on raw ghost EV therefore reliably admits lanes that bleed live.

This tool stops that. For each (strategy, window, side) lane it joins:
  1. ghost_EV      — mean realized_pct of REJECTED candidates (what loosening admits),
                     held-to-resolution.
  2. exit_delta    — mean(live_pct) - mean(held_pct) on TAKEN trades of that lane,
                     i.e. the systematic shift the exit layer imposes. Borrowed from
                     the (window, side) pool when the lane has too few taken trades.
  3. proj_EV       — ghost_EV + exit_delta : the ghost reject cohort PROJECTED through
                     this lane's real exit behavior. This is the number to act on.
  4. shrunk_EV     — proj_EV empirical-Bayes shrunk toward the pooled mean by taken n
                     (kills winner's-curse on the ranked extremes).
  5. stability     — sign agreement of live EV across the two time-halves.

VERDICT
  GO     : shrunk_EV > +margin, taken_n >= min_taken, two-half stable.    -> loosen.
  NO-GO  : shrunk_EV <= 0.                                                -> leave blocked / tighten.
  SHADOW : ghost_EV > 0 but taken_n < min_taken (exit behavior unproven)  -> instrument & forward-test,
           or unstable across halves.                                       do NOT ship blind.

USAGE
  python3 scripts/lane_decision_sheet.py [--since 2026-06-02] [--min-taken 20]
                                         [--margin 1.0] [--shrink-k 30]
"""
from __future__ import annotations
import argparse, json, math, collections, os

GHOST = "data/calibration/rejected_candidates_settled.jsonl"
TAKEN = "data/calibration/trades_settled.jsonl"

# ghost side vocab (LONG/SHORT) -> trades action vocab (BUY_YES/BUY_NO)
SIDE2ACTION = {"LONG": "BUY_YES", "SHORT": "BUY_NO"}


def _mid(ts: str) -> str:
    return ts


def load_ghost(since: str):
    """(strategy, window, action) -> list of realized_pct (%) for rejected candidates."""
    out = collections.defaultdict(list)
    if not os.path.exists(GHOST):
        return out
    for line in open(GHOST):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if (d.get("ts") or "") < since:
            continue
        rp = d.get("realized_pct")
        if rp is None:
            continue
        action = SIDE2ACTION.get(d.get("side"), d.get("side"))
        k = (d.get("strategy"), d.get("window"), action)
        out[k].append(float(rp) * 100.0)
    return out


def load_taken(since: str):
    """(strategy, window, action) -> list of (ts, held_pct, live_pct)."""
    out = collections.defaultdict(list)
    if not os.path.exists(TAKEN):
        return out
    for line in open(TAKEN):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if (d.get("ts") or "") < since:
            continue
        cb = d.get("cost_basis") or 0.0
        hp = d.get("held_realized_pct")
        ap = d.get("actual_pnl")
        if cb <= 0 or hp is None or ap is None:
            continue
        held_pct = float(hp) * 100.0
        live_pct = float(ap) / float(cb) * 100.0
        k = (d.get("strategy"), d.get("window"), d.get("action"))
        out[k].append((d.get("ts") or "", held_pct, live_pct))
    return out


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-02")
    ap.add_argument("--min-taken", type=int, default=20)
    ap.add_argument("--margin", type=float, default=1.0, help="min shrunk EV %% to call GO")
    ap.add_argument("--shrink-k", type=float, default=30.0, help="EB pseudo-count")
    ap.add_argument("--min-ghost", type=int, default=30)
    args = ap.parse_args()

    ghost = load_ghost(args.since)
    taken = load_taken(args.since)

    # pooled exit_delta by (window, action) for borrowing when a lane is thin
    pool_delta = collections.defaultdict(list)
    for k, rows in taken.items():
        _, w, a = k
        for (_, h, l) in rows:
            pool_delta[(w, a)].append(l - h)

    # pooled live EV across all taken trades -> EB shrink target
    all_live = [l for rows in taken.values() for (_, _, l) in rows]
    pooled_live = mean(all_live)

    keys = set(ghost) | set(taken)
    out_rows = []
    for k in keys:
        s, w, side = k
        g = ghost.get(k, [])
        t = taken.get(k, [])
        ghost_ev = mean([x for x in g]) if g else None
        taken_n = len(t)

        # exit_delta: lane-measured if enough taken, else (window,side) pooled
        if taken_n >= args.min_taken:
            exit_delta = mean([l - h for (_, h, l) in t])
            delta_src = "lane"
        else:
            pd = pool_delta.get((w, side), [])
            exit_delta = mean(pd) if pd else 0.0
            delta_src = "pool" if pd else "none"

        if ghost_ev is None:
            continue  # nothing to loosen if no reject cohort

        proj_ev = ghost_ev + exit_delta

        # EB shrinkage toward pooled live EV, weighted by taken n (confidence in exits)
        n_eff = taken_n
        shrunk = (n_eff * proj_ev + args.shrink_k * pooled_live) / (n_eff + args.shrink_k)

        # two-half stability on live EV (needs taken trades)
        stable = None
        if taken_n >= args.min_taken:
            ts_sorted = sorted(t, key=lambda r: r[0])
            mid = len(ts_sorted) // 2
            h1 = mean([l for (_, _, l) in ts_sorted[:mid]])
            h2 = mean([l for (_, _, l) in ts_sorted[mid:]])
            stable = (h1 > 0) == (h2 > 0)
            half = (h1, h2)
        else:
            half = (None, None)

        # verdict
        if ghost_ev <= 0 and proj_ev <= 0:
            verdict = "NO-GO"
        elif taken_n < args.min_taken:
            verdict = "SHADOW" if ghost_ev > 0 else "NO-GO"
        elif shrunk > args.margin and stable:
            verdict = "GO"
        elif shrunk <= 0:
            verdict = "NO-GO"
        else:
            verdict = "SHADOW"  # +EV but marginal / unstable -> forward-test

        out_rows.append({
            "lane": f"{s} {w} {side}", "ghost_n": len(g), "taken_n": taken_n,
            "ghost_ev": ghost_ev, "exit_delta": exit_delta, "delta_src": delta_src,
            "proj_ev": proj_ev, "shrunk": shrunk, "stable": stable,
            "h1": half[0], "h2": half[1], "verdict": verdict,
        })

    order = {"GO": 0, "SHADOW": 1, "NO-GO": 2}
    out_rows.sort(key=lambda r: (order[r["verdict"]], -r["shrunk"]))

    print(f"LANE DECISION SHEET  since={args.since}  min_taken={args.min_taken}  "
          f"margin={args.margin}%  shrink_k={args.shrink_k}  pooled_live={pooled_live:+.2f}%")
    print(f"{'lane':<22}{'gN':>6}{'tN':>5}{'ghostEV':>9}{'exitΔ':>8}{'src':>5}"
          f"{'projEV':>8}{'shrunk':>8}{'stbl':>5}  verdict")
    print("-" * 92)
    for r in out_rows:
        st = "" if r["stable"] is None else ("y" if r["stable"] else "N")
        print(f"{r['lane']:<22}{r['ghost_n']:>6}{r['taken_n']:>5}{r['ghost_ev']:>+9.1f}"
              f"{r['exit_delta']:>+8.1f}{r['delta_src']:>5}{r['proj_ev']:>+8.1f}"
              f"{r['shrunk']:>+8.1f}{st:>5}  {r['verdict']}")

    c = collections.Counter(r["verdict"] for r in out_rows)
    print("-" * 92)
    print(f"GO={c['GO']}  SHADOW={c['SHADOW']}  NO-GO={c['NO-GO']}   "
          f"(GO=loosen now · SHADOW=instrument+forward-test · NO-GO=leave/tighten)")


if __name__ == "__main__":
    main()
