#!/usr/bin/env python3
"""
volume_regime_study.py — does LOW VOLUME actually hurt the bot? (operator question 2026-08-09)

For weeks we argued about "low volume" without measuring it, because trades never recorded the volume
at entry. But the bot's tape_map ALREADY logs a per-asset volume timeseries (data/calibration/tape_map.jsonl:
vol_pctile / vol_bucket, ~every 30s). This script JOINS each closed trade to the tape_map snapshot nearest
its entry time, then reports realized WR + payoff by volume regime — and splits it by entry-price band
(coin-flip near 0.50 vs favorite >=0.75) to test the hypothesis: low volume compresses the near-even MIDDLE
toward a coin-flip while leaving FAVORITES intact (an 85% favorite is 85% regardless of volume).

NO bot change, NO restart — pure join of two logs the bot already writes. LIVE realized outcomes only
(not ghost/EV). Answers: (1) is low-vol WR actually worse? (2) if so, WHERE — the middle, or everywhere?
That decides the fix: a volume-aware EDGE BAR (skip near-even in low vol, keep favorites) vs nothing vs a
blanket blackout (which we do NOT want — that's the trade-on-a-clock filter we killed in minimax).

Usage:
  python scripts/volume_regime_study.py                 # all August sessions
  python scripts/volume_regime_study.py --since 20260808 --join-tol 180
"""
import json, argparse, collections, bisect
from datetime import datetime


def load_tape_series():
    """asset -> (sorted ts[], parallel rows[]) for nearest-ts lookup."""
    ts_by = collections.defaultdict(list)
    row_by = collections.defaultdict(list)
    tmp = collections.defaultdict(list)
    with open("data/calibration/tape_map.jsonl") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            a = r.get("asset")
            t = r.get("ts")
            if a is None or t is None:
                continue
            tmp[a].append((float(t), r))
    for a, rows in tmp.items():
        rows.sort(key=lambda x: x[0])
        ts_by[a] = [x[0] for x in rows]
        row_by[a] = [x[1] for x in rows]
    return ts_by, row_by


def nearest_vol(ts_by, row_by, asset, entry_epoch, tol):
    tss = ts_by.get(asset)
    if not tss:
        return None
    i = bisect.bisect_right(tss, entry_epoch)
    cands = []
    if i > 0:
        cands.append(i - 1)
    if i < len(tss):
        cands.append(i)
    best = None
    for j in cands:
        d = abs(tss[j] - entry_epoch)
        if d <= tol and (best is None or d < best[0]):
            best = (d, row_by[asset][j])
    return best[1] if best else None


def band(entry_price):
    if entry_price is None:
        return "?"
    if entry_price >= 0.75:
        return "favorite(>=.75)"
    if abs(entry_price - 0.5) >= 0.10:   # 0.60-0.75 or 0.25-0.40
        return "edge(.10+)"
    return "coinflip(.40-.60)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="20260801", help="YYYYMMDD lower bound on session date")
    ap.add_argument("--join-tol", type=float, default=180.0, help="max secs between entry and tape snapshot")
    args = ap.parse_args()

    ts_by, row_by = load_tape_series()
    trades = []
    with open("data/calibration/trades.jsonl") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            s = str(r.get("session_id", ""))
            if not s.startswith("test_"):
                continue
            try:
                if s.split("_")[1] < args.since:
                    continue
            except Exception:
                continue
            trades.append(r)

    joined = 0
    by_vol = collections.defaultdict(lambda: [0, 0, 0.0])            # vol_bucket -> n,wins,pnl
    by_vol_band = collections.defaultdict(lambda: [0, 0, 0.0])       # (vol,band) -> n,wins,pnl
    by_pctile = collections.defaultdict(lambda: [0, 0])             # coarse pctile decile -> n,wins
    for r in trades:
        oa = r.get("opened_at")
        if not oa:
            continue
        try:
            ep_epoch = datetime.fromisoformat(oa).timestamp()
        except Exception:
            continue
        asset = r.get("strategy")
        vrow = nearest_vol(ts_by, row_by, asset, ep_epoch, args.join_tol)
        if vrow is None:
            continue
        joined += 1
        vb = vrow.get("vol_bucket", "?")
        pct = vrow.get("vol_pctile")
        w = 1 if r.get("win") else 0
        pnl = r.get("pnl", 0)
        bd = band(r.get("entry_price"))
        by_vol[vb][0] += 1; by_vol[vb][1] += w; by_vol[vb][2] += pnl
        by_vol_band[(vb, bd)][0] += 1; by_vol_band[(vb, bd)][1] += w; by_vol_band[(vb, bd)][2] += pnl
        if pct is not None:
            dec = round(pct, 1)
            by_pctile[dec][0] += 1; by_pctile[dec][1] += w

    print("=== VOLUME-REGIME STUDY (since %s, join tol %ds) ===" % (args.since, args.join_tol))
    print("trades joined to a tape-vol snapshot: %d / %d" % (joined, len(trades)))
    print()
    print("--- WR + payoff by VOLUME bucket (the headline) ---")
    print("vol_bucket   n     WR     pnl")
    for vb in ["low", "mid", "high", "?"]:
        if vb not in by_vol:
            continue
        n, w, p = by_vol[vb]
        print("  %-8s %4d   %3.0f%%  %8.2f" % (vb, n, 100 * w / n if n else 0, p))
    print()
    print("--- THE TEST: WR by volume bucket x entry band ---")
    print("(if low-vol hurts the MIDDLE but not favorites, that's a volume-aware edge bar, not a blackout)")
    print("vol      band                 n     WR     pnl")
    for vb in ["low", "mid", "high"]:
        for bd in ["coinflip(.40-.60)", "edge(.10+)", "favorite(>=.75)"]:
            n, w, p = by_vol_band[(vb, bd)]
            if n >= 4:
                print("  %-6s %-18s %4d   %3.0f%%  %8.2f" % (vb, bd, n, 100 * w / n, p))
    print()
    print("--- WR by vol_pctile decile (monotonic? = volume predicts WR) ---")
    for dec in sorted(by_pctile):
        n, w = by_pctile[dec]
        if n >= 8:
            print("  pctile~%.1f  n=%3d  WR=%3.0f%%" % (dec, n, 100 * w / n))


if __name__ == "__main__":
    main()
