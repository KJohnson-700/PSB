#!/usr/bin/env python3
"""tape_map FORWARD-ACCURACY tracker — READ-ONLY, the gauge for tape-map quality.

Operator ask (2026-08-03): "track the tape-map's % so we have a way to gauge improvement."
This is distinct from tape_map_validator.py (#105), which only tests SYMMETRY (is the UP
branch reachable). This scores whether each direction CALL was actually RIGHT against the
price that followed, by self-joining tape_map.jsonl on the same asset's later snapshots
(no external price feed needed — every snapshot carries `price`).

For each snapshot (asset, ts, direction, confidence, price):
  find the same asset's price ~H minutes later (nearest snapshot within tolerance);
  move = (future_price - price) / price;
  UP   correct if move >  +eps
  DOWN correct if move <  -eps
  FLAT correct if |move| <= eps         (eps default 0.10% of price)

Reports, per horizon H in {5,15,60} min:
  - DIRECTIONAL hit-rate at conf>=0.6, UP/DOWN only  <-- THE number the gates rely on
  - accuracy by asset, by direction, by confidence bucket
Appends a one-line summary to data/calibration/tape_map_accuracy_log.jsonl each run so the
trend is trackable over sessions (improvement gauge). Pass --no-log to skip the append.

Usage: .venv/bin/python scripts/tape_map_accuracy.py [--eps 0.001] [--no-log] [--since-ts N]
"""
from __future__ import annotations
import argparse
import bisect
import json
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = ROOT / "data" / "calibration" / "tape_map.jsonl"
LOG_PATH = ROOT / "data" / "calibration" / "tape_map_accuracy_log.jsonl"
HORIZONS_MIN = [5, 15, 60]
CONF_GATE = 0.6  # the threshold the live gates use


def _load(since_ts=None):
    per = defaultdict(list)  # asset -> list of (ts, dir, conf, price) sorted by ts
    for line in open(MAP_PATH):
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = d.get("ts")
        px = d.get("price")
        if not isinstance(ts, (int, float)) or not isinstance(px, (int, float)) or px <= 0:
            continue
        if since_ts and ts < since_ts:
            continue
        per[d.get("asset")].append((ts, str(d.get("direction") or "").upper(),
                                    float(d.get("confidence", 0.0) or 0.0), float(px)))
    for a in per:
        per[a].sort()
    return per


def _future_price(rows, ts_list, i, horizon_s, tol_s):
    """Nearest snapshot to ts+horizon within tol; None if the series ends first."""
    target = rows[i][0] + horizon_s
    j = bisect.bisect_left(ts_list, target)
    best = None
    for k in (j - 1, j, j + 1):
        if 0 <= k < len(rows):
            dt = abs(rows[k][0] - target)
            if dt <= tol_s and (best is None or dt < best[0]):
                best = (dt, rows[k][3])
    return best[1] if best else None


def score(per, eps, horizon_s):
    tol_s = max(45.0, horizon_s * 0.25)
    # buckets: (cut_key) -> [correct, total]
    by_asset = defaultdict(lambda: [0, 0])
    by_dir = defaultdict(lambda: [0, 0])
    dir_hi = [0, 0]   # UP/DOWN only, conf>=gate  <-- headline
    dir_all = [0, 0]  # UP/DOWN only, any conf
    for asset, rows in per.items():
        ts_list = [r[0] for r in rows]
        for i, (ts, dr, conf, px) in enumerate(rows):
            fp = _future_price(rows, ts_list, i, horizon_s, tol_s)
            if fp is None:
                continue
            move = (fp - px) / px
            # DIRECTIONAL calls (UP/DOWN) are scored by PURE SIGN — that is exactly how a
            # Polymarket up/down market resolves (sign of the move over the window), so any
            # correct-side move counts. `eps` is used ONLY as the FLAT dead-band; using it on
            # UP/DOWN would wrongly dump correct-but-small moves into "wrong" (5-15m crypto
            # moves are routinely < 0.1%). This was the initial-read artifact.
            if dr == "UP":
                ok = move > 0
            elif dr == "DOWN":
                ok = move < 0
            elif dr == "FLAT":
                ok = abs(move) <= eps
            else:
                continue
            by_asset[asset][0] += ok; by_asset[asset][1] += 1
            by_dir[dr][0] += ok; by_dir[dr][1] += 1
            if dr in ("UP", "DOWN"):
                dir_all[0] += ok; dir_all[1] += 1
                if conf >= CONF_GATE:
                    dir_hi[0] += ok; dir_hi[1] += 1
    return by_asset, by_dir, dir_hi, dir_all


def pct(cr):
    return (100.0 * cr[0] / cr[1]) if cr[1] else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eps", type=float, default=0.001, help="flat/dead band as frac of price (default 0.1%)")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--since-ts", type=float, default=None)
    args = ap.parse_args()

    per = _load(args.since_ts)
    tot = sum(len(v) for v in per.values())
    print("=" * 74)
    print(f"TAPE-MAP FORWARD ACCURACY  (snapshots={tot}, eps={args.eps*100:.2f}%, conf_gate={CONF_GATE})")
    print("=" * 74)
    headline = {}
    for hm in HORIZONS_MIN:
        by_asset, by_dir, dir_hi, dir_all = score(per, args.eps, hm * 60)
        print(f"\n── horizon {hm}m ──")
        print(f"  ★ DIRECTIONAL hit-rate (UP/DOWN only, conf>={CONF_GATE}): "
              f"{pct(dir_hi):5.1f}%  (n={dir_hi[1]})   [the gate-relevant number]")
        print(f"    directional, any conf: {pct(dir_all):5.1f}% (n={dir_all[1]})")
        print("    by direction:  " + "  ".join(
            f"{k}={pct(v):.0f}%(n={v[1]})" for k, v in sorted(by_dir.items())))
        print("    by asset:      " + "  ".join(
            f"{k.replace('_macro','')}={pct(v):.0f}%" for k, v in sorted(by_asset.items())))
        headline[str(hm)] = {"dir_conf60_pct": round(pct(dir_hi), 1), "n_conf60": dir_hi[1],
                             "dir_any_pct": round(pct(dir_all), 1)}
    # baseline reference: a coin flip on UP/DOWN = 50%; beating it = the map has edge
    print("\n  (coin-flip baseline = 50%. >55% at conf>=0.6 = usable directional edge.)")

    if not args.no_log:
        rec = {"ts": time.time(), "eps": args.eps, "snapshots": tot, "horizons": headline}
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"\n  appended to {LOG_PATH.name} (trend gauge)")


if __name__ == "__main__":
    main()
