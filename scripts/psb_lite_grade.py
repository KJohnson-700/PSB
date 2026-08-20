#!/usr/bin/env python3
"""PSB-lite — CANDIDATE GRADER (build tick 3).

Joins every favorite candidate logged by psb_lite_poller.py to the REAL Gamma
resolution of its market, and answers the one question the whole strategy rests on:

    does the 0.80-0.93 favorite band win MORE than its breakeven win rate?

Breakeven at quote p (taker, crypto_fees_v2 fee = 0.07*p*(1-p) per share) is
    be = (p + fee) / 1.0
i.e. you must win often enough to cover the price paid plus the entry fee.

Grades observe-only candidates — nothing was ever traded. Read-only apart from
appending its own verdict rows to data/calibration/psb_lite_graded.jsonl.
"""

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAND = os.path.join(ROOT, "data", "calibration", "psb_lite_candidates.jsonl")
OUT = os.path.join(ROOT, "data", "calibration", "psb_lite_graded.jsonl")
GAMMA = "https://gamma-api.polymarket.com/markets"
CACHE = {}


def gamma(mid):
    if mid in CACHE:
        return CACHE[mid]
    try:
        req = urllib.request.Request(f"{GAMMA}/{mid}",
                                     headers={"User-Agent": "psb-lite-grade/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            m = json.loads(r.read().decode())
    except Exception:
        m = None
    CACHE[mid] = m
    time.sleep(0.12)
    return m


def resolved_yes(m):
    if not m or not (m.get("closed") or m.get("archived")):
        return None
    op = m.get("outcomePrices")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except ValueError:
            return None
    if isinstance(op, list) and len(op) == 2:
        try:
            y = float(op[0])
        except (TypeError, ValueError):
            return None
        if y >= 0.99:
            return True
        if y <= 0.01:
            return False
    return None


def main():
    cands = []
    seen = set()
    for ln in open(CAND):
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("kind") != "candidate_v2":
            continue
        key = (r.get("market_id"), r.get("side"))
        if key in seen:
            continue
        seen.add(key)
        cands.append(r)
    print(f"candidates to grade: {len(cands)}")

    graded = pending = 0
    wins = 0
    gross = fees = 0.0
    by_band = defaultdict(lambda: [0, 0, 0.0])
    with open(OUT, "a") as fh:
        for r in cands:
            yes = resolved_yes(gamma(r["market_id"]))
            if yes is None:
                pending += 1
                continue
            won = (yes and r["side"] == "YES") or ((not yes) and r["side"] == "NO")
            p = float(r["quote"])
            shares = float(r["shares"])
            fee = 0.07 * p * (1 - p) * shares
            pnl = (shares * (1 - p) - fee) if won else (-shares * p - fee)
            graded += 1
            wins += 1 if won else 0
            gross += pnl
            fees += fee
            b = "0.80-0.85" if p < 0.855 else ("0.85-0.90" if p < 0.905 else "0.90-0.93")
            by_band[b][0] += 1
            by_band[b][1] += 1 if won else 0
            by_band[b][2] += pnl
            fh.write(json.dumps({
                "market_id": r["market_id"], "side": r["side"], "quote": p,
                "mins_left": r["mins_left"], "resolved_yes": yes, "won": won,
                "pnl": round(pnl, 2), "breakeven_wr": r["breakeven_wr"],
            }, separators=(",", ":")) + "\n")

    if not graded:
        print(f"nothing resolved yet ({pending} pending)")
        return 0
    wr = wins / graded * 100
    be = sum(float(c["breakeven_wr"]) for c in cands) / len(cands) * 100
    print(f"\n=== PSB-LITE FAVORITE BAND — GRADED vs REAL RESOLUTIONS ===")
    print(f"  graded         : {graded}   (pending {pending})")
    print(f"  WIN RATE       : {wr:.1f}%")
    print(f"  BREAKEVEN WR   : {be:.1f}%")
    print(f"  margin         : {wr - be:+.1f} points")
    print(f"  net P&L        : ${gross:+.2f}  (${gross/graded:+.2f}/trade, fees ${fees:.2f})")
    print(f"  VERDICT        : {'EDGE CONFIRMED' if wr > be else 'NO EDGE — below breakeven'}")
    print(f"\n  {'quote band':14}{'n':>5}{'WR':>7}{'breakeven':>11}{'$/trade':>9}")
    for b in sorted(by_band):
        v = by_band[b]
        if not v[0]:
            continue
        mid = {"0.80-0.85": 0.825, "0.85-0.90": 0.875, "0.90-0.93": 0.915}[b]
        bwr = (mid + 0.07 * mid * (1 - mid)) * 100
        print(f"  {b:14}{v[0]:5}{v[1]/v[0]*100:6.0f}%{bwr:10.1f}%{v[2]/v[0]:+9.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
