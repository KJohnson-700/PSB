#!/usr/bin/env python3
"""lane_scoreboard.py — the calibration decision surface.

For each (strategy, window, side) lane it shows:
  * LIVE-realized WR / pnl / n   (the ONLY decision-grade signal — trades_settled.jsonl)
  * GHOST WR / n                 (context only, NOT decision-grade — rejected_candidates_settled.jsonl)
  * the LIVE-vs-GHOST gap        (flags lanes where the ghost lies, e.g. btc 5m long: ghost 69% / live 25%)
  * STATE                        (BLEED = trading & losing | STARVED = 0 live but ghosts exist | THIN = tiny n)

Usage:
  python3 scripts/lane_scoreboard.py                 # all lanes, all sessions
  python3 scripts/lane_scoreboard.py --recent 6      # only the last 6 sessions
  python3 scripts/lane_scoreboard.py --strategy bitcoin
  python3 scripts/lane_scoreboard.py --session test_20260801_011345
  python3 scripts/lane_scoreboard.py --min-n 3       # hide lanes below n live trades

Read-only. Never mutates anything.
"""
import argparse
import json
import os
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.join(BASE, "data/calibration/trades_settled.jsonl")
GHOST = os.path.join(BASE, "data/calibration/rejected_candidates_settled.jsonl")


def _load(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    return out


def _recent_sessions(rows, n):
    """Keep only rows from the n most-recent session_ids (by first-seen order in file)."""
    seen = []
    for r in rows:
        s = r.get("session_id")
        if s and s not in seen:
            seen.append(s)
    keep = set(seen[-n:]) if n else None
    return [r for r in rows if (keep is None or r.get("session_id") in keep)]


def _lane_key(r):
    return (r.get("strategy"), r.get("window"), r.get("action"))


def _ghost_win(r):
    """A ghost 'win' = the side we WOULD have taken resolved in our favor."""
    for k in ("would_win", "ghost_win", "hit", "correct"):
        if k in r:
            return bool(r[k])
    # fallback: compare our side token vs held_outcome
    side = r.get("action") or ""
    outcome = (r.get("held_outcome") or r.get("outcome") or "").upper()
    if outcome in ("YES", "NO"):
        return (side == "BUY_YES" and outcome == "YES") or (side == "BUY_NO" and outcome == "NO")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=0, help="only last N sessions")
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--session", default=None)
    ap.add_argument("--min-n", type=int, default=1, help="hide lanes below N live trades")
    args = ap.parse_args()

    live = _load(LIVE)
    ghost = _load(GHOST)
    if args.recent:
        live = _recent_sessions(live, args.recent)
    if args.session:
        live = [r for r in live if r.get("session_id") == args.session]
        ghost = [r for r in ghost if r.get("session_id") == args.session]

    L = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for r in live:
        if args.strategy and r.get("strategy") != args.strategy:
            continue
        k = _lane_key(r)
        L[k]["n"] += 1
        L[k]["w"] += 1 if (r.get("actual_pnl") or 0) > 0 else 0
        L[k]["pnl"] += r.get("actual_pnl") or 0.0

    G = defaultdict(lambda: {"n": 0, "w": 0})
    for r in ghost:
        if args.strategy and r.get("strategy") != args.strategy:
            continue
        k = _lane_key(r)
        gw = _ghost_win(r)
        if gw is None:
            continue
        G[k]["n"] += 1
        G[k]["w"] += 1 if gw else 0

    keys = set(L) | set(G)
    rows = []
    for k in keys:
        lv, gh = L[k], G[k]
        ln, lw, lpnl = lv["n"], lv["w"], lv["pnl"]
        gn, gw = gh["n"], gh["w"]
        lwr = (lw / ln * 100) if ln else None
        gwr = (gw / gn * 100) if gn else None
        if ln < args.min_n and ln > 0:
            state = "THIN"
        elif ln == 0 and gn > 0:
            state = "STARVED"
        elif lwr is not None and lpnl < 0:
            state = "BLEED"
        elif lwr is not None:
            state = "ok"
        else:
            state = "-"
        gap = (gwr - lwr) if (gwr is not None and lwr is not None) else None
        rows.append((k, ln, lwr, lpnl, gn, gwr, gap, state))

    # sort: bleeders first (most negative live pnl), then starved
    rows.sort(key=lambda x: (x[3] if x[2] is not None else 1e9))

    print(f"{'LANE':<34}{'live_n':>7}{'live_WR':>9}{'live_pnl':>10}{'ghost_n':>8}{'ghost_WR':>9}{'gap':>7}  STATE")
    print("-" * 100)
    for (strat, win, act), ln, lwr, lpnl, gn, gwr, gap, state in rows:
        if ln < args.min_n and state != "STARVED":
            continue
        lane = f"{strat}|{win}|{act}"
        lwr_s = f"{lwr:.0f}%" if lwr is not None else "-"
        gwr_s = f"{gwr:.0f}%" if gwr is not None else "-"
        gap_s = f"{gap:+.0f}" if gap is not None else "-"
        flag = "  <-- GHOST LIES" if (gap is not None and gap >= 25 and lpnl < 0) else ""
        print(f"{lane:<34}{ln:>7}{lwr_s:>9}{lpnl:>+10.2f}{gn:>8}{gwr_s:>9}{gap_s:>7}  {state}{flag}")
    print("-" * 100)
    print("BLEED=trading&losing  STARVED=0 live/ghosts exist  THIN=n<min  GHOST LIES=ghost>=25pt over live & live losing")


if __name__ == "__main__":
    main()
