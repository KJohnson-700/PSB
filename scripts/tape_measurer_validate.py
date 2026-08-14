#!/usr/bin/env python3
"""Tape-measurer predictive-validity check — READ-ONLY. The lockdown test.

"Locking down the tape measurer" (operator, 2026-08-03, top priority) starts with proving it
MEASURES SOMETHING REAL: when the map says UP, does the underlying actually rise next; when it
says DOWN, does it fall? This replays tonight's tape_map.jsonl — each row carries the asset's
own price + the direction label + a timestamp — and measures the FORWARD return at several
horizons, bucketed by the label. A trustworthy measurer shows monotonic separation:
mean_fwd(UP) > mean_fwd(FLAT) > mean_fwd(DOWN), and a directional hit-rate above 50%.

No bot touch, no trades — pure measurement of the measurer.
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

TAPE = Path(__file__).resolve().parent.parent / "data" / "calibration" / "tape_map.jsonl"
HORIZONS = (90, 180, 300, 600)  # seconds ahead


def _f(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def main():
    rows = defaultdict(list)  # asset -> [(ts, price, direction)]
    for l in open(TAPE):
        try:
            r = json.loads(l)
        except Exception:
            continue
        ts, px, d = _f(r.get("ts")), _f(r.get("price")), r.get("direction")
        if ts is not None and px not in (None, 0) and d:
            rows[r.get("asset")].append((ts, px, d))
    # Feed-sanity filter: drop rows whose price is a gross outlier vs the asset's median
    # (e.g. xrp logged a bogus 100.0 vs real ~1.07). A clean spot series is required to
    # measure the label's forward edge — garbage prices manufacture fake ±98% moves.
    dropped = 0
    for a in list(rows):
        ps = sorted(p for _, p, _ in rows[a])
        med = ps[len(ps) // 2] if ps else 0
        if med > 0:
            keep = [(t, p, d) for (t, p, d) in rows[a] if 0.2 * med <= p <= 5 * med]
            dropped += len(rows[a]) - len(keep)
            rows[a] = keep
    for a in rows:
        rows[a].sort(key=lambda x: x[0])
    if dropped:
        print(f"[validate] dropped {dropped} rows with out-of-range (bad-feed) prices\n")

    print("=" * 78)
    print("TAPE-MEASURER VALIDITY  ·  does the label predict the underlying's next move?")
    print("=" * 78)
    grand = defaultdict(lambda: defaultdict(lambda: {"n": 0, "sum": 0.0, "hit": 0}))
    for asset, seq in rows.items():
        n = len(seq)
        if n < 20:
            continue
        # index forward lookups
        print(f"\n{asset}  ({n} rows)")
        print(f"  {'horizon':>7} {'UP: n mean%':>18} {'DOWN: n mean%':>18} {'FLAT: n mean%':>18}  {'dir-hit%':>8} {'monotonic':>9}")
        for h in HORIZONS:
            buck = defaultdict(lambda: {"n": 0, "sum": 0.0, "hit": 0})
            j = 0
            for i in range(n):
                ts_i, px_i, d_i = seq[i]
                # advance j to first row at/after ts_i + h
                k = i
                while k < n and seq[k][0] < ts_i + h:
                    k += 1
                if k >= n:
                    break
                fwd = (seq[k][1] - px_i) / px_i
                b = buck[d_i]; b["n"] += 1; b["sum"] += fwd
                # directional hit: UP wants fwd>0, DOWN wants fwd<0 (FLAT excluded from hit)
                if d_i == "UP" and fwd > 0:
                    b["hit"] += 1
                elif d_i == "DOWN" and fwd < 0:
                    b["hit"] += 1
                g = grand[h][d_i]; g["n"] += 1; g["sum"] += fwd
                if (d_i == "UP" and fwd > 0) or (d_i == "DOWN" and fwd < 0):
                    g["hit"] += 1

            def cell(d):
                b = buck[d]
                if not b["n"]:
                    return f"{'-':>18}"
                return f"{b['n']:>5} {100*b['sum']/b['n']:>+9.3f}%"[:18].rjust(18)
            up, dn, fl = buck["UP"], buck["DOWN"], buck["FLAT"]
            hits = (up["hit"] + dn["hit"])
            dirn = (up["n"] + dn["n"])
            hitrate = 100 * hits / dirn if dirn else 0
            mono = ""
            if up["n"] and dn["n"]:
                um, dm = up["sum"] / up["n"], dn["sum"] / dn["n"]
                fm = (fl["sum"] / fl["n"]) if fl["n"] else (um + dm) / 2
                mono = "YES" if um > fm > dm else ("part" if um > dm else "NO")
            print(f"  {h:>7} {cell('UP')} {cell('DOWN')} {cell('FLAT')}  {hitrate:>7.1f}% {mono:>9}")

    print("\n" + "=" * 78)
    print("POOLED across assets (the headline: is the measurer directional at all?)")
    print(f"  {'horizon':>7} {'UP mean%':>12} {'DOWN mean%':>12} {'sep(UP-DOWN)':>13} {'dir-hit%':>9} {'n':>7}")
    for h in HORIZONS:
        up, dn = grand[h]["UP"], grand[h]["DOWN"]
        if not up["n"] or not dn["n"]:
            continue
        um, dm = 100 * up["sum"] / up["n"], 100 * dn["sum"] / dn["n"]
        hit = 100 * (up["hit"] + dn["hit"]) / (up["n"] + dn["n"])
        print(f"  {h:>7} {um:>+11.3f}% {dm:>+11.3f}% {um-dm:>+12.3f}% {hit:>8.1f}% {up['n']+dn['n']:>7}")
    print("\n  read: sep(UP-DOWN) > 0 and dir-hit% > 50 = the measurer has real directional")
    print("        edge. sep<=0 or hit<=50 = it is labeling noise — fix the measurer BEFORE")
    print("        any behavior hangs off it.\n")


if __name__ == "__main__":
    main()
