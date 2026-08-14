#!/usr/bin/env python3
"""Score every AI provider head-to-head vs tape_map — READ-ONLY. The improvement gauge.

Reads data/calibration/ai_direction_shadow.jsonl (paired provider calls + tape_map, same instant),
forward-joins each to the realized underlying move `horizon_min` later (price from tape_map.jsonl),
and scores each provider AND tape_map by PURE SIGN (how a Polymarket up/down market resolves) on the
SAME snapshots. Handles both the new multi-provider `decisions` rows and the legacy `ai_dir` rows.

A provider beats the deterministic champion only if its % > tape% on the same rows at n>=~30.
Usage: .venv/bin/python scripts/ai_direction_score.py [--conf 0.6]
"""
from __future__ import annotations
import argparse
import bisect
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHADOW = ROOT / "data/calibration/ai_direction_shadow.jsonl"
MAP = ROOT / "data/calibration/tape_map.jsonl"


def _price_series():
    per = defaultdict(list)
    for line in open(MAP):
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts, px = d.get("ts"), d.get("price")
        if isinstance(ts, (int, float)) and isinstance(px, (int, float)):
            per[d.get("asset")].append((ts, float(px)))
    for a in per:
        per[a].sort()
    return per


def _future_px(series, ts0, horizon_s):
    if not series:
        return None
    tss = [t for t, _ in series]
    target = ts0 + horizon_s
    j = bisect.bisect_left(tss, target)
    best = None
    for k in (j - 1, j, j + 1):
        if 0 <= k < len(series):
            dt = abs(series[k][0] - target)
            if dt <= max(60, horizon_s * 0.25) and (best is None or dt < best[0]):
                best = (dt, series[k][1])
    return best[1] if best else None


def _correct(direction, move):
    d = str(direction or "").upper()
    if d == "UP":
        return move > 0
    if d == "DOWN":
        return move < 0
    return None  # FLAT / unknown excluded from directional score


def _row_decisions(r):
    """Yield (provider_name, dir, conf) for a row — new `decisions` dict or legacy ai_dir."""
    dec = r.get("decisions")
    if isinstance(dec, dict) and dec:
        for name, d in dec.items():
            yield name, d.get("dir"), d.get("conf")
    elif r.get("ai_dir"):
        yield "minimax", r.get("ai_dir"), r.get("ai_conf")
    # champion always
    yield "tape_map", r.get("tape_dir"), r.get("tape_conf")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", type=float, default=0.6)
    args = ap.parse_args()
    if not SHADOW.exists() or SHADOW.stat().st_size == 0:
        print("ai_direction_shadow.jsonl empty — start the engine first.")
        return
    series = _price_series()
    allc = defaultdict(lambda: [0, 0])   # provider -> [correct,total] all conf
    hic = defaultdict(lambda: [0, 0])    # conf>=gate
    scored = pending = 0
    for line in open(SHADOW):
        try:
            r = json.loads(line)
        except Exception:
            continue
        fp = _future_px(series.get(r.get("asset"), []), r.get("ts"), r.get("horizon_min", 15) * 60)
        if fp is None or not r.get("price"):
            pending += 1
            continue
        move = (fp - r["price"]) / r["price"]
        scored += 1
        for name, dirc, conf in _row_decisions(r):
            ok = _correct(dirc, move)
            if ok is None:
                continue
            allc[name][0] += ok; allc[name][1] += 1
            try:
                if float(conf or 0) >= args.conf:
                    hic[name][0] += ok; hic[name][1] += 1
            except Exception:
                pass

    def pct(c):
        return f"{100*c[0]/c[1]:5.1f}% (n={c[1]})" if c[1] else "     n=0"
    print("=" * 66)
    print(f"AI PROVIDERS vs TAPE_MAP  (scored rows={scored}, pending fwd-price={pending})")
    print("=" * 66)
    names = sorted(allc, key=lambda k: (k == "tape_map", k))  # tape_map last
    print(f"  {'provider':10s}  {'all-conf':>18s}   {'conf>='+str(args.conf):>18s}")
    for name in names:
        marker = "  (champion)" if name == "tape_map" else ""
        print(f"  {name:10s}  {pct(allc[name]):>18s}   {pct(hic[name]):>18s}{marker}")
    print("\n  A provider wins only if its % > tape_map% on the same rows at n>=~30. Coin-flip=50%.")


if __name__ == "__main__":
    main()
