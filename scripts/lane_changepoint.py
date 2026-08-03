#!/usr/bin/env python3
"""Change-point era guard (Script C) — don't let a dead era poison current stats.

A lane's lifetime win-rate is a lie if it blends a profitable era with a dead one
(the classic 06-28-poisons-06-29 mistake). This script segments each lane's
ordered realized-PnL series into homogeneous eras and reports the CURRENT era's
stats separately from the full history, so every downstream decision uses only
the era we are actually in.

Method (pure numpy, no ruptures dependency): Taylor CUSUM change-point detection
with bootstrap significance, applied recursively (binary segmentation). For a
series x, S_k = cumsum(x - mean); the candidate break is argmax|S_k|; its
significance is estimated by permuting x many times and comparing max|S| ranges.
Splits recurse while significant and segments stay above a min size. This finds
MEAN shifts in per-trade PnL — i.e. the trade index/date where the edge changed.
Breaks are printed with their calendar date so they can be cross-checked against
the timestamped config backups.

LIVE REALIZED only (data/calibration/trades.jsonl). Read-only, fail-safe.

Usage:
  python3 scripts/lane_changepoint.py                 # all history, per lane
  python3 scripts/lane_changepoint.py --min-seg 12 --conf 0.90
  python3 scripts/lane_changepoint.py --lane eth_macro|5m|BUY_YES
  python3 scripts/lane_changepoint.py --json
"""
from __future__ import annotations
import argparse, json, sys
from collections import defaultdict
from pathlib import Path

import numpy as np

TRADES = Path(__file__).resolve().parent.parent / "data" / "calibration" / "trades.jsonl"


def _f(r, k):
    v = r.get(k)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def load_trades(args):
    rows = []
    try:
        with open(TRADES) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        print(f"[changepoint] no trades log at {TRADES}", file=sys.stderr)
        return []
    if not args.all_sessions and args.sessions:
        sess = sorted({r.get("session_id", "") for r in rows if r.get("session_id")})
        keep = set(sess[-args.sessions:])
        rows = [r for r in rows if r.get("session_id") in keep]
    return rows


def _cusum_split(x, rng, n_boot=400, conf=0.90):
    """Return (index, confidence) of the strongest single change point, or None."""
    n = len(x)
    if n < 4:
        return None
    x = np.asarray(x, dtype=float)
    s = np.cumsum(x - x.mean())
    sdiff_obs = s.max() - s.min()
    if sdiff_obs <= 0:
        return None
    beat = 0
    for _ in range(n_boot):
        p = rng.permutation(x)
        sp = np.cumsum(p - p.mean())
        if (sp.max() - sp.min()) < sdiff_obs:
            beat += 1
    confidence = beat / n_boot
    if confidence < conf:
        return None
    idx = int(np.argmax(np.abs(s)))  # break AFTER position idx
    return (idx, confidence)


def find_breaks(x, min_seg, conf, rng):
    """Recursive binary segmentation. Returns sorted list of break indices."""
    breaks = []

    def rec(lo, hi):
        seg = x[lo:hi]
        if len(seg) < 2 * min_seg:
            return
        res = _cusum_split(seg, rng, conf=conf)
        if res is None:
            return
        idx, _ = res
        cut = lo + idx + 1
        if cut - lo < min_seg or hi - cut < min_seg:
            return
        breaks.append(cut)
        rec(lo, cut)
        rec(cut, hi)

    rec(0, len(x))
    return sorted(breaks)


def seg_stats(pnl):
    a = np.asarray(pnl, dtype=float)
    n = len(a)
    wins = int((a > 0).sum())
    return {"n": n, "wr": round(wins / n, 3) if n else 0.0,
            "pnl": round(float(a.sum()), 2),
            "avg": round(float(a.mean()), 3) if n else 0.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=None,
                    help="limit to last N sessions (default: all history)")
    ap.add_argument("--all-sessions", action="store_true", help="force full history")
    ap.add_argument("--min-seg", type=int, default=10, help="min trades per era")
    ap.add_argument("--conf", type=float, default=0.90, help="bootstrap confidence to split")
    ap.add_argument("--min-n", type=int, default=20, help="skip lanes below this n")
    ap.add_argument("--lane", type=str, default=None, help="one exact strategy|window|action")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_trades(args)
    if not rows:
        print("[changepoint] no trades"); return

    lanes = defaultdict(list)
    for r in rows:
        key = f"{r.get('strategy','?')}|{r.get('window','?')}|{r.get('action','?')}"
        ts = r.get("ts") or r.get("closed_at") or ""
        pnl = _f(r, "pnl")
        if pnl is None:
            continue
        lanes[key].append((ts, pnl))

    rng = np.random.default_rng(12345)  # fixed seed: reproducible eras
    report = []
    for key, seq in lanes.items():
        if args.lane and key != args.lane:
            continue
        if len(seq) < args.min_n:
            continue
        seq.sort(key=lambda t: t[0])
        ts = [t[0] for t in seq]
        pnl = [t[1] for t in seq]
        breaks = find_breaks(pnl, args.min_seg, args.conf, rng)
        bounds = [0] + breaks + [len(pnl)]
        eras = []
        for i in range(len(bounds) - 1):
            lo, hi = bounds[i], bounds[i + 1]
            st = seg_stats(pnl[lo:hi])
            st["start"] = ts[lo][:10]; st["end"] = ts[hi - 1][:10]
            eras.append(st)
        report.append({"lane": key, "n": len(pnl), "n_breaks": len(breaks),
                       "full": seg_stats(pnl), "current_era": eras[-1],
                       "eras": eras})

    report.sort(key=lambda d: d["n"], reverse=True)
    if args.json:
        print(json.dumps(report, indent=2)); return

    scope = f"last {args.sessions} sessions" if (args.sessions and not args.all_sessions) else "all history"
    print(f"\nCHANGE-POINT ERA GUARD  ·  {scope}  ·  min_seg={args.min_seg} conf={args.conf:.2f}")
    print("  full-history stats vs the CURRENT era (use current era for decisions)\n")
    print(f"  {'lane':30s} {'brk':>3}  {'full n/WR/pnl':>22}   {'CURRENT era n/WR/pnl · since':>34}")
    print("  " + "-" * 96)
    for d in report:
        f = d["full"]; c = d["current_era"]
        full_s = f"{f['n']:>4} {f['wr']:.2f} {f['pnl']:>+8.2f}"
        cur_s = f"{c['n']:>4} {c['wr']:.2f} {c['pnl']:>+8.2f} · {c['start']}"
        flag = ""
        if d["n_breaks"] > 0:
            # flag lanes whose current era disagrees in sign or by >15pt WR
            if (f["pnl"] > 0) != (c["pnl"] > 0) or abs(f["wr"] - c["wr"]) > 0.15:
                flag = "  <-- era differs from lifetime"
        print(f"  {d['lane']:30s} {d['n_breaks']:>3}  {full_s:>22}   {cur_s:>34}{flag}")
    print("\n  a break means the lane's edge shifted on that date — cross-check it")
    print("  against config/settings.yaml.bak_* timestamps before trusting it.\n")


if __name__ == "__main__":
    main()
