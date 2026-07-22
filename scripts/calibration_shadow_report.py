#!/usr/bin/env python3
"""Calibration shadow report — is the quant's est_prob CALIBRATED vs realized outcomes?

Read-only. Joins taken-trade entries (which carry the quant's est_prob) to their
settled resolution outcome, per lane, and reports predicted-P(win) vs realized WR.
This is the truth-grounded "confidence meter": where the gap is large, the quant is
over/under-confident and true-Kelly is being fed a wrong win_prob.

Usage: python scripts/calibration_shadow_report.py [--session SUBSTR] [--min-n 8]
No bot interaction; safe to run any time.
"""
import json, glob, os, sys, argparse
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_jsonl(path):
    out = []
    try:
        with open(path) as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    try: out.append(json.loads(ln))
                    except Exception: pass
    except FileNotFoundError:
        pass
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None, help="filter settled rows to session_id substring")
    ap.add_argument("--min-n", type=int, default=8)
    args = ap.parse_args()

    # 1) est_prob per trade_id, from ALL session entries.jsonl (est_prob = P(up))
    est_by_tid = {}
    for f in glob.glob(os.path.join(REPO, "data/paper_trades/*/entries.jsonl")):
        for r in load_jsonl(f):
            if str(r.get("event")) != "ENTRY":
                continue
            tid = r.get("trade_id")
            ep = r.get("est_prob", (r.get("extra") or {}).get("est_prob"))
            if tid is None or ep is None:
                continue
            est_by_tid[tid] = {
                "est_prob_up": float(ep),
                "action": r.get("action"),
                "edge": float(r.get("edge") or 0.0),
                "strategy": r.get("strategy"),
                "window": (r.get("extra") or {}).get("window_size"),
            }

    # 2) settled outcomes (resolution truth) by trade_id
    settled = load_jsonl(os.path.join(REPO, "data/calibration/trades_settled.jsonl"))
    lanes = defaultdict(lambda: {"n": 0, "pred_sum": 0.0, "wins": 0, "edge_sum": 0.0,
                                 "brier": 0.0, "exit_wins": 0, "pnl": 0.0})
    joined = 0
    for s in settled:
        if args.session and args.session not in str(s.get("session_id", "")):
            continue
        tid = s.get("trade_id")
        e = est_by_tid.get(tid)
        if not e:
            continue
        # resolution truth: prefer held_win (side won at resolution); fall back to actual_pnl sign
        held_win = s.get("held_win")
        if held_win is None:
            won = 1 if float(s.get("actual_pnl") or 0) > 0 else 0
        else:
            won = 1 if held_win else 0
        # predicted P(this side wins) = est_prob_up if BUY_YES else 1-est_prob_up
        act = e["action"] or s.get("action") or ""
        p_side = e["est_prob_up"] if act == "BUY_YES" else (1.0 - e["est_prob_up"])
        strat = (e["strategy"] or s.get("strategy") or "?").replace("_macro", "")
        win = e["window"] or s.get("window") or "?"
        side = "up" if act == "BUY_YES" else "down"
        key = f"{strat}|{win}|{side}"
        d = lanes[key]
        d["n"] += 1
        d["pred_sum"] += p_side
        d["wins"] += won
        d["edge_sum"] += e["edge"]
        d["brier"] += (p_side - won) ** 2
        d["exit_wins"] += 1 if float(s.get("actual_pnl") or 0) > 0 else 0
        d["pnl"] += float(s.get("actual_pnl") or 0)
        joined += 1

    print(f"calibration_shadow_report | joined {joined} settled↔entry trades | "
          f"session={args.session or 'ALL'} | lanes>= n{args.min_n}\n")
    hdr = f"{'lane':<20} {'n':>4} {'predWR':>7} {'realWR':>7} {'gap':>6} {'brier':>6} {'exitWR':>7} {'pnl$':>8}"
    print(hdr); print("-" * len(hdr))
    rows = []
    for k, d in lanes.items():
        if d["n"] < args.min_n:
            continue
        pred = d["pred_sum"] / d["n"]
        real = d["wins"] / d["n"]
        rows.append((k, d["n"], pred, real, pred - real, d["brier"] / d["n"],
                     d["exit_wins"] / d["n"], d["pnl"]))
    # sort by abs calibration gap (worst-calibrated first)
    for k, n, pred, real, gap, brier, exitwr, pnl in sorted(rows, key=lambda r: -abs(r[4])):
        flag = " <-- OVERCONF" if gap > 0.10 else (" <-- underconf" if gap < -0.10 else "")
        print(f"{k:<20} {n:>4} {pred:>7.1%} {real:>7.1%} {gap:>+6.1%} {brier:>6.3f} {exitwr:>7.1%} {pnl:>8.2f}{flag}")
    if not rows:
        print("(no lanes at min-n yet)")
    print("\nRead: predWR=quant's mean P(side wins); realWR=settled resolution WR; "
          "gap>0=OVERconfident (feeds true-Kelly too-high win_prob→oversizes).")

if __name__ == "__main__":
    main()
