#!/usr/bin/env python3
"""lane_live.py — REAL-TIME per-lane truth from the session journal (no settler lag).

The offline settler (trades_settled.jsonl) lags, esp. on slow-resolving 1h markets — that's
what made the 1h audit hard. But every EXIT record in entries.jsonl ALREADY carries the full
lane context in `extra` (window_size, lane_id, entry_family, signal_reason, exit_reason). This
reads that directly, so per-lane WR / payoff / exit-mix is live the moment a trade closes.

Usage:
  python3 scripts/lane_live.py                         # newest session
  python3 scripts/lane_live.py --session test_2026...  # a specific session
  python3 scripts/lane_live.py --strategy bitcoin
  python3 scripts/lane_live.py --exit-mix              # also break down exit reasons per lane
"""
import argparse, glob, json, os
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PT = os.path.join(BASE, "data/paper_trades")


def newest_session():
    ds = sorted(glob.glob(os.path.join(PT, "test_*")), key=os.path.getmtime)
    return os.path.basename(ds[-1]) if ds else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--exit-mix", action="store_true")
    args = ap.parse_args()

    sess = args.session or newest_session()
    if not sess:
        print("no session found"); return
    path = os.path.join(PT, sess, "entries.jsonl")
    if not os.path.exists(path):
        print(f"no entries.jsonl for {sess}"); return

    lane = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0, "ws": 0.0, "ls": 0.0,
                                 "wn": 0.0, "ln": 0.0, "exits": defaultdict(int)})
    for ln in open(path):
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("event") != "EXIT":
            continue
        strat = d.get("strategy")
        if args.strategy and strat != args.strategy:
            continue
        ex = d.get("extra") or {}
        win = ex.get("window_size") or "?"
        act = d.get("action")
        pnl = d.get("pnl") or 0.0
        notl = (d.get("entry_price") or 0) * (d.get("size") or 0)
        k = f"{strat}|{win}|{act}"
        v = lane[k]
        v["n"] += 1
        v["pnl"] += pnl
        v["exits"][ex.get("exit_reason") or d.get("reason") or "?"] += 1
        if pnl > 0:
            v["w"] += 1; v["ws"] += pnl; v["wn"] += notl
        else:
            v["ls"] += pnl; v["ln"] += notl

    print(f"session {sess}  (live journal, real-time)\n")
    print(f"{'LANE':<26}{'n':>3}{'W':>3}{'L':>3}{'WR':>6}{'pnl':>9}{'avgW':>7}{'avgL':>7}{'payoff':>7}{'Wnotl':>7}{'Lnotl':>7}")
    for k in sorted(lane, key=lambda x: lane[x]["pnl"]):
        v = lane[k]; n, w = v["n"], v["w"]; l = n - w
        aw = v["ws"] / w if w else 0
        al = v["ls"] / l if l else 0
        po = (aw / abs(al)) if al else 0
        wn = v["wn"] / w if w else 0
        ln_ = v["ln"] / l if l else 0
        print(f"{k:<26}{n:>3}{w:>3}{l:>3}{w/n*100:>5.0f}%{v['pnl']:>+9.2f}{aw:>+7.2f}{al:>+7.2f}{po:>7.2f}{wn:>7.1f}{ln_:>7.1f}")
    if args.exit_mix:
        print("\n--- exit-reason mix per lane ---")
        for k in sorted(lane):
            mix = ", ".join(f"{r}:{c}" for r, c in sorted(lane[k]["exits"].items(), key=lambda x: -x[1]))
            print(f"  {k}: {mix}")


if __name__ == "__main__":
    main()
