#!/usr/bin/env python3
"""Derive EXACT HYPE UP-side RSI momentum adjustments for _estimate_probability.

est_prob_up is P(market up). The right per-zone nudge = P(up | rsi zone) - baseline,
in probability points. Measure P(up)=mean(outcome=="YES") over HYPE candidates by
fine RSI bucket, all-time AND recent (regime-stability gate), plus realized-EV on the
UP side as a sign cross-check. Output the rsi_adj values for the three threshold zones
the code uses: rsi<30 (os_bounce), 65-75 (ob_mild), >75 (ob_strong); 30-65 stays 0.
"""
import json
import sys
from pathlib import Path
import numpy as np

SETTLED = Path("data/calibration/rejected_candidates_settled.jsonl")
RECENT = "2026-06-08"
STRAT = sys.argv[1] if len(sys.argv) > 1 else "hype_macro"
FINE = [(0,25),(25,30),(30,35),(35,45),(45,55),(55,65),(65,75),(75,85),(85,100)]
ZONES = {"rsi<30 (os_bounce)": (0,30), "30-65 (neutral)": (30,65),
         "65-75 (ob_mild)": (65,75), "rsi>75 (ob_strong)": (75,100)}


def load():
    rows = []  # (rsi, outcome_up:bool, realized, action, ts)
    for line in SETTLED.open():
        if f'"{STRAT}"' not in line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("strategy") != STRAT:
            continue
        ctx = r.get("context") or {}
        rv = ctx.get("rsi_14")
        if rv is None:
            continue
        try:
            rv = float(rv)
        except Exception:
            continue
        if not (0 < rv < 100):
            continue
        up = (r.get("outcome") == "YES")
        rp = r.get("realized_pct")
        rows.append((rv, up, (None if rp is None else float(rp)),
                     r.get("action"), r.get("ts", "")))
    return rows


def pud(rows, lo, hi):
    sub = [u for rv, u, _, _, _ in rows if lo <= rv < hi]
    return (np.mean(sub), len(sub)) if sub else (None, 0)


def up_ev(rows, lo, hi):
    sub = [rp for rv, _, rp, a, _ in rows if lo <= rv < hi and a == "BUY_YES" and rp is not None]
    return (np.mean(sub), len(sub)) if sub else (None, 0)


def report(rows, label):
    base, bn = np.mean([u for _, u, _, _, _ in rows]), len(rows)
    print(f"\n=== {label}  ({STRAT}, P(up) baseline={base:.3f}, n={bn}) ===")
    print(f"{'RSI bucket':<12} {'P(up)':>7} {'n':>6}   {'UP realizedEV':>13} {'n':>6}")
    for lo, hi in FINE:
        p, n = pud(rows, lo, hi)
        e, en = up_ev(rows, lo, hi)
        ps = f"{p:.3f}" if p is not None else "—"
        es = f"{e:+.3f}" if e is not None else "—"
        print(f"{lo:>3}-{hi:<7} {ps:>7} {n:>6}   {es:>13} {en:>6}")
    print(f"\n  derived UP rsi_adj = P(up|zone) - baseline  [zone -> adj, n]:")
    out = {}
    for zlabel, (lo, hi) in ZONES.items():
        p, n = pud(rows, lo, hi)
        adj = (p - base) if p is not None else None
        out[zlabel] = (adj, n)
        adjs = f"{adj:+.3f}" if adj is not None else "—"
        print(f"    {zlabel:<22} {adjs}  (n={n})")
    return out


def main():
    rows = load()
    allt = report(rows, "ALL-TIME")
    rec = [r for r in rows if r[4] >= RECENT]
    recent = report(rec, f"RECENT >= {RECENT}")
    print("\n=== STABILITY (all-time vs recent adj; must agree in sign) ===")
    for z in ZONES:
        a, _ = allt[z]; b, _ = recent[z]
        if a is None or b is None:
            continue
        flag = "OK" if (a >= 0) == (b >= 0) else "*** SIGN FLIP ***"
        print(f"  {z:<22} all={a:+.3f}  recent={b:+.3f}  {flag}")


if __name__ == "__main__":
    main()
