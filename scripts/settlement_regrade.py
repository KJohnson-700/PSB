#!/usr/bin/env python3
"""SETTLEMENT RE-GRADE (analysis only, 2026-08-19).

The question this answers: the band book (entry 0.45-0.55) shows +$2.22/trade on the
`updown_expired` exit -- but that exit sells at the LAST MARK (often ~0.49), it does not
settle the binary. If those positions had been settled honestly at 0/1, is the band
book still positive, or was its profit a marking artifact?

Method: for every closed trade, take its market_id and ask Gamma for the REAL resolution
(umaResolutionStatus / outcomePrices on the closed market). Recompute P&L as a true
binary settle from the entry price and side, and compare to what the journal recorded.

Writes data/calibration/settlement_regrade.jsonl + prints a summary. Changes nothing.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL = os.path.join(ROOT, "data", "paper_trades")
OUT = os.path.join(ROOT, "data", "calibration", "settlement_regrade.jsonl")
GAMMA = "https://gamma-api.polymarket.com/markets"
CACHE = {}
THROTTLE = 0.12


def gamma_market(mid):
    if mid in CACHE:
        return CACHE[mid]
    try:
        req = urllib.request.Request(f"{GAMMA}/{mid}",
                                     headers={"User-Agent": "psb-regrade/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            m = json.loads(r.read().decode())
    except Exception:
        m = None
    CACHE[mid] = m
    time.sleep(THROTTLE)
    return m


def resolved_yes(m):
    """True/False if the market resolved YES/NO, None if undetermined."""
    if not m:
        return None
    if not (m.get("closed") or m.get("archived")):
        return None
    op = m.get("outcomePrices")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except ValueError:
            op = None
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
    sessions = sys.argv[1:] or None
    trades = []
    for d in sorted(os.listdir(JOURNAL)):
        if not d.startswith("test_"):
            continue
        if sessions and d not in sessions:
            continue
        p = os.path.join(JOURNAL, d, "entries.jsonl")
        if not os.path.exists(p):
            continue
        ent = {}
        for ln in open(p):
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            ev = str(t.get("event"))
            if ev == "ENTRY":
                ent[t.get("trade_id")] = t
            elif ev == "EXIT":
                en = ent.get(t.get("trade_id"))
                if not en:
                    continue
                trades.append({
                    "session": d, "trade_id": t.get("trade_id"),
                    "market_id": en.get("market_id"),
                    "entry_price": float(en.get("entry_price") or 0),
                    "size": float(en.get("size") or 0),
                    "action": en.get("action"), "strategy": en.get("strategy"),
                    "reason": str(t.get("reason") or "")[:40],
                    "journal_pnl": t.get("pnl") or 0,
                    "exit_price": float(t.get("current_price") or 0),
                })
    print(f"loaded {len(trades)} closed trades", flush=True)

    agg = defaultdict(lambda: {"n": 0, "jp": 0.0, "sp": 0.0, "jw": 0, "sw": 0, "unres": 0})
    graded = 0
    with open(OUT, "a") as fh:
        for i, t in enumerate(trades):
            band = 0.45 <= t["entry_price"] <= 0.55
            fav = t["entry_price"] >= 0.80
            bucket = "BAND" if band else ("FAV" if fav else ("LOW" if t["entry_price"] < 0.45 else "MID"))
            key = (bucket, "expired" if "expired" in t["reason"] else
                   ("stop" if "stop" in t["reason"] else
                    ("tp" if ("take_profit" in t["reason"] or "profit" in t["reason"]) else "other")))
            a = agg[key]
            a["n"] += 1
            a["jp"] += t["journal_pnl"]
            if t["journal_pnl"] > 0:
                a["jw"] += 1
            yes = resolved_yes(gamma_market(t["market_id"]))
            if yes is None:
                a["unres"] += 1
                continue
            won = (yes and t["action"] == "BUY_YES") or ((not yes) and t["action"] == "BUY_NO")
            shares = t["size"]
            settled = shares * (1.0 - t["entry_price"]) if won else -shares * t["entry_price"]
            a["sp"] += settled
            if won:
                a["sw"] += 1
            graded += 1
            fh.write(json.dumps({
                "session": t["session"], "market_id": t["market_id"],
                "bucket": bucket, "exit_kind": key[1], "reason": t["reason"],
                "entry_price": t["entry_price"], "action": t["action"],
                "journal_pnl": round(t["journal_pnl"], 2),
                "settled_pnl": round(settled, 2),
                "resolved_yes": yes, "would_have_won": won,
            }, separators=(",", ":")) + "\n")
            if graded % 100 == 0:
                print(f"  graded {graded}...", flush=True)

    print(f"\ngraded {graded} of {len(trades)}\n")
    print(f"{'bucket/exit':22}{'n':>5}{'grd':>5}{'journalP&L':>12}{'settledP&L':>12}{'jWR':>6}{'sWR':>6}")
    for k in sorted(agg, key=lambda x: (x[0], x[1])):
        a = agg[k]
        g = a["n"] - a["unres"]
        if a["n"] < 5:
            continue
        print(f"{k[0]+'/'+k[1]:22}{a['n']:5}{g:5}{a['jp']:+12.2f}{a['sp']:+12.2f}"
              f"{a['jw']/a['n']*100:5.0f}%{(a['sw']/g*100 if g else 0):5.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
