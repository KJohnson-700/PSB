#!/usr/bin/env python3
"""EV-vs-WR re-cut of the settled ghost log.

Tests the report's claim that yes_price tails are a "credible signal".
WR stratified by price is mechanically driven by price (price ~= P(YES)),
so the real question is realized EV, which the settler already computes:

    realized_pct = (1-entry)/entry  if won   (entry = price actually paid)
                 = -1.0              if lost

mean(realized_pct) over a lane = expected return per dollar staked.

Usage:
    python scripts/ghost_ev_vs_wr_recut.py
    python scripts/ghost_ev_vs_wr_recut.py --since 2026-05-22
"""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SETTLED = REPO / "data" / "calibration" / "rejected_candidates_settled.jsonl"

# entry-price buckets (price actually paid for the chosen side)
BUCKETS = [(0.0, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 1.0)]

# Tier-1/Tier-2 lanes from the FINAL report, re-scored on EV.
# (label, strategy, window, side|None, est_prob_min|None)
REPORT_LANES = [
    ("T1 doge 1h est>=0.55",      "doge_macro", "1h", None,    0.55),
    ("T1 bnb 15m est>=0.55",      "bnb_macro",  "15m", None,   0.55),
    ("T1 bitcoin 5m (all)",       "bitcoin",    "5m", None,    None),
    ("T2 bitcoin 1h SHORT e>=.45","bitcoin",    "1h", "SHORT", 0.45),
    ("T2 xrp 1h SHORT e>=.60",    "xrp_macro",  "1h", "SHORT", 0.60),
    ("T2 bnb 1h e>=.60",          "bnb_macro",  "1h", None,    0.60),
    ("T2 doge 1h e>=.60",         "doge_macro", "1h", None,    0.60),
    ("T2 sol 1h e>=.60",          "sol_macro",  "1h", None,    0.60),
    ("T2 bitcoin 15m e>=.55",     "bitcoin",    "15m", None,   0.55),
    ("T2 bnb 1h LONG e>=.55",     "bnb_macro",  "1h", "LONG",  0.55),
]


def era_of(ts, holdout_after=None):
    """Split rows into in-sample vs holdout.

    With --holdout-after DATE: ts >= DATE is the fresh out-of-sample holdout,
    everything earlier is in-sample. Without it, fall back to the report's
    natural clusters (May = W19 holdout, June = W22-W23 in-sample).
    """
    if holdout_after:
        return "holdout(OOS)" if ts >= holdout_after else "in-sample"
    if ts.startswith("2026-05"):
        return "May(W19 holdout)"
    if ts.startswith("2026-06"):
        return "Jun(in-sample)"
    return "other"


def bucket_of(p):
    for lo, hi in BUCKETS:
        if lo <= p < hi or (hi == 1.0 and p == 1.0):
            return f"{lo:.2f}-{hi:.2f}"
    return None


def wilson_lo(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (c - m) / d


def entry_price(action, yes_price, no_price):
    if action == "BUY_YES":
        return yes_price
    if no_price:
        return no_price
    return (1.0 - yes_price) if yes_price is not None else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO date floor on ts, e.g. 2026-05-22")
    ap.add_argument("--window", default=None, help="filter to a window e.g. 15m")
    ap.add_argument("--min-n", type=int, default=200)
    ap.add_argument("--holdout-after", default=None,
                    help="ISO date; ts>=DATE = fresh OOS holdout, earlier = in-sample. "
                         "Omit to use the May/June report clusters.")
    args = ap.parse_args()
    if args.holdout_after:
        eras_order = ("ALL", "in-sample", "holdout(OOS)")
    else:
        eras_order = ("ALL", "Jun(in-sample)", "May(W19 holdout)")

    # keyed by (window, action, bucket)
    agg = defaultdict(lambda: {"n": 0, "wins": 0, "ev": 0.0})
    # report lanes, keyed by (label, era)
    lane = defaultdict(lambda: {"n": 0, "wins": 0, "ev": 0.0})

    n_rows = 0
    with open(SETTLED) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("ts") or ""
            if args.since and ts < args.since:
                continue
            action = d.get("action")
            yp = d.get("yes_price")
            np_ = d.get("no_price")
            rp = d.get("realized_pct")
            win = d.get("win")
            window = d.get("window")
            if action not in ("BUY_YES", "BUY_NO") or rp is None or win is None:
                continue
            if args.window and window != args.window:
                continue
            ep = entry_price(action, yp, np_)
            if ep is None or not (0 < ep < 1):
                continue
            b = bucket_of(ep)
            if b is None:
                continue
            n_rows += 1
            k = (window, action, b)
            agg[k]["n"] += 1
            agg[k]["wins"] += 1 if win else 0
            agg[k]["ev"] += rp

            # report lanes, split by era (May=W19 holdout vs June=in-sample)
            strat = d.get("strategy")
            est = d.get("est_prob_up")
            side = d.get("side")
            era = era_of(ts, args.holdout_after)
            for label, ls, lw, lside, lmin in REPORT_LANES:
                if strat != ls or window != lw:
                    continue
                if lside is not None and side != lside:
                    continue
                if lmin is not None and (est is None or est < lmin):
                    continue
                for ek in (era, "ALL"):
                    lane[(label, ek)]["n"] += 1
                    lane[(label, ek)]["wins"] += 1 if win else 0
                    lane[(label, ek)]["ev"] += rp

    print(f"\n=== rows used: {n_rows}  (since={args.since or 'ALL'}, window={args.window or 'ALL'}) ===")
    print("\nEntry-price-bucket lanes  (bucket = price PAID for the chosen side)")
    print(f"{'window':>6} {'action':>7} {'bucket':>11} {'n':>7} {'WR':>6} {'WR_lo':>6} {'EV/$':>8} {'verdict'}")
    for k in sorted(agg):
        v = agg[k]
        n = v["n"]
        if n < args.min_n:
            continue
        wr = v["wins"] / n
        ev = v["ev"] / n
        verdict = "PROFIT" if ev > 0.02 else ("flat" if ev > -0.02 else "LOSS")
        flag = "  <-- high WR, neg EV" if (wr >= 0.60 and ev < 0) else ""
        print(f"{k[0]:>6} {k[1]:>7} {k[2]:>11} {n:>7} {wr:>6.1%} {wilson_lo(v['wins'],n):>6.1%} {ev:>+8.3f} {verdict}{flag}")

    print("\nReport Tier-1/Tier-2 lanes re-scored on EV  (era split: May=W19 holdout, Jun=in-sample)")
    print(f"{'lane':>28} {'era':>17} {'n':>6} {'WR':>6} {'EV/$':>8} {'verdict'}")
    for label, _, _, _, _ in REPORT_LANES:
        for era in eras_order:
            v = lane[(label, era)]
            n = v["n"]
            if n == 0:
                vstr = f"{label:>28} {era:>17} {0:>6} {'—':>6} {'—':>8} no data"
                print(vstr)
                continue
            wr = v["wins"] / n
            ev = v["ev"] / n
            verdict = "PROFIT" if ev > 0.02 else ("flat" if ev > -0.02 else "LOSS")
            flag = "  <-- high WR, neg EV" if (wr >= 0.60 and ev < 0) else ""
            print(f"{label:>28} {era:>17} {n:>6} {wr:>6.1%} {ev:>+8.3f} {verdict}{flag}")
        print()


if __name__ == "__main__":
    main()
