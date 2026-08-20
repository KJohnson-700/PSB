#!/usr/bin/env python3
"""Grade the near-resolution probe snapshots against REAL Gamma resolutions.

The question: as a contract approaches settlement, does ACCURACY climb faster than
BREAKEVEN does? Buying the favorite at quote p needs p + fee to be right just to break
even, so a 0.99 favorite must win 99.1% of the time. High win rate alone proves nothing —
this reports the MARGIN (actual WR minus required WR) per quote bucket per time mark.
"""

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPS = os.path.join(ROOT, "data", "calibration", "near_resolution_probe.jsonl")
CACHE = {}


def resolved_yes(mid):
    if mid in CACHE:
        return CACHE[mid]
    try:
        req = urllib.request.Request(f"https://gamma-api.polymarket.com/markets/{mid}",
                                     headers={"User-Agent": "psb-nrp-grade/1.0"})
        m = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        op = m.get("outcomePrices")
        if isinstance(op, str):
            op = json.loads(op)
        y = float(op[0]) if isinstance(op, list) and len(op) == 2 else None
        v = True if (y is not None and y >= 0.99) else (False if (y is not None and y <= 0.01) else None)
    except Exception:
        v = None
    CACHE[mid] = v
    time.sleep(0.12)
    return v


def bucket(p):
    if p >= 0.99:
        return "0.99+"
    if p >= 0.97:
        return "0.97-0.99"
    if p >= 0.95:
        return "0.95-0.97"
    if p >= 0.90:
        return "0.90-0.95"
    if p >= 0.80:
        return "0.80-0.90"
    return "<0.80"


ORDER = ["0.99+", "0.97-0.99", "0.95-0.97", "0.90-0.95", "0.80-0.90", "<0.80"]


def main():
    snaps = []
    for ln in open(SNAPS):
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("kind") == "snap":
            snaps.append(r)
    print(f"snapshots: {len(snaps)}")

    agg = defaultdict(lambda: [0, 0, 0.0, 0.0])   # n, wins, sum_quote, sum_pnl_per_$1
    pending = 0
    for r in snaps:
        yes = resolved_yes(r["market_id"])
        if yes is None:
            pending += 1
            continue
        won = (yes and r["fav_side"] == "YES") or ((not yes) and r["fav_side"] == "NO")
        p = float(r["fav_quote"])
        fee = float(r.get("fee_per_share") or 0.0)
        # per $1 of capital deployed at price p
        pnl = ((1 - p) - fee) / p if won else (-(p + fee)) / p
        k = (r["mark_secs"], bucket(p))
        a = agg[k]
        a[0] += 1
        a[1] += 1 if won else 0
        a[2] += p
        a[3] += pnl

    if not agg:
        print(f"nothing resolved yet ({pending} pending)")
        return 0
    print(f"graded {sum(a[0] for a in agg.values())}, pending {pending}\n")
    for mark in sorted({k[0] for k in agg}, reverse=True):
        print(f"=== T-{int(mark)}s ===")
        print(f"  {'quote bucket':14}{'n':>5}{'WR':>7}{'avg quote':>11}{'needs':>8}{'margin':>9}{'$/$1':>9}")
        tot_n = tot_pnl = 0
        for b in ORDER:
            a = agg.get((mark, b))
            if not a or a[0] == 0:
                continue
            n, w, sq, sp = a
            wr = w / n * 100
            avg_p = sq / n
            need = (avg_p + 0.07 * avg_p * (1 - avg_p)) * 100
            tot_n += n
            tot_pnl += sp
            print(f"  {b:14}{n:5}{wr:6.0f}%{avg_p:11.3f}{need:7.1f}%{wr-need:+9.1f}{sp/n:+9.4f}")
        if tot_n:
            print(f"  {'ALL':14}{tot_n:5}{'':>7}{'':>11}{'':>8}{'':>9}{tot_pnl/tot_n:+9.4f}")
        print()
    print("margin = actual WR minus the WR the quote REQUIRES. Positive => real edge.")
    print("$/$1   = net profit per dollar deployed, after the taker fee.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
