#!/usr/bin/env python3
"""TAPE-CONFIRMED EXIT shadow — READ-ONLY. First step of the never-green exit-timing work.

Never-green losers split into two kinds:
  (1) wrong-side from entry (red throughout)  -> ENTRY fix (#1 oversold gate, (c) side-veto)
  (2) green-then-reversed (give-back)          -> EXIT fix: cut when the tape turns against you.

This measures the OPPORTUNITY for a tape-CONFIRMED exit on settled LOSERS: after entry, does
tape_map flip AGAINST the open position (short=BUY_NO -> map UP; long=BUY_YES -> map DOWN) with
confidence>=gate, BEFORE the market resolves? If so, a tape-confirmed exit could have cut the trade.
We can't price the Polymarket token mark per-tick, so we score DIRECTIONAL opportunity using the
underlying price path from tape_map.jsonl: was the underlying LESS adverse (vs the side) at the
flip than at resolution? If yes, exiting at the flip beats riding to the stop.

Join: trades_settled (loser, side, entry_price, actual_pnl) --trade_id--> trades.jsonl (entry_ts)
      --asset,[entry_ts, entry_ts+window]--> tape_map.jsonl (dir/conf/price path).

Reports coverage (% losers the tape flagged), median lead time, and % where the flip was a
GOOD exit signal (less adverse than resolution). Baseline gauge; appends nothing (pure read).
Usage: .venv/bin/python scripts/tape_confirmed_exit_shadow.py [--conf 0.6]
"""
from __future__ import annotations
import argparse
import bisect
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIN_SEC = {"5m": 300, "15m": 900, "1h": 3600}


def _parse_ts(ts):
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def _load_entries():
    ent = {}
    for line in open(ROOT / "data/calibration/trades.jsonl"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        tid = d.get("trade_id")
        if tid:
            ent[tid] = _parse_ts(d.get("ts"))
    return ent


def _load_tape():
    per = defaultdict(list)  # asset -> [(ts, dir, conf, price)]
    for line in open(ROOT / "data/calibration/tape_map.jsonl"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = d.get("ts"); px = d.get("price")
        if not isinstance(ts, (int, float)) or not isinstance(px, (int, float)):
            continue
        per[d.get("asset")].append((ts, str(d.get("direction") or "").upper(),
                                    float(d.get("confidence", 0.0) or 0.0), float(px)))
    for a in per:
        per[a].sort()
    return per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", type=float, default=0.6)
    args = ap.parse_args()

    ent = _load_entries()
    tape = _load_tape()

    losers = 0
    no_entry = 0
    no_path = 0
    flagged = 0            # tape flipped against before resolution
    good_signal = 0        # underlying less adverse at flip than at resolution
    lead_times = []
    per_lane = defaultdict(lambda: {"losers": 0, "flagged": 0, "good": 0})

    for line in open(ROOT / "data/calibration/trades_settled.jsonl"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        pnl = d.get("actual_pnl")
        if pnl is None or float(pnl) >= 0:
            continue
        losers += 1
        asset = d.get("strategy")               # tape_map keys are strategy names
        window = d.get("window")
        action = d.get("action")                # BUY_NO=short, BUY_YES=long
        tid = d.get("trade_id")
        e_ts = ent.get(tid)
        lane = (asset, window, action)
        per_lane[lane]["losers"] += 1
        if e_ts is None:
            no_entry += 1
            continue
        rows = tape.get(asset)
        if not rows:
            no_path += 1
            continue
        dur = WIN_SEC.get(window, 900)
        r_ts = e_ts + dur
        ts_list = [r[0] for r in rows]
        i0 = bisect.bisect_left(ts_list, e_ts)
        # entry + resolution underlying price (nearest snapshots)
        def _px_at(t):
            j = bisect.bisect_left(ts_list, t)
            best = None
            for k in (j - 1, j):
                if 0 <= k < len(rows):
                    dt = abs(rows[k][0] - t)
                    if best is None or dt < best[0]:
                        best = (dt, rows[k][3])
            return best[1] if best else None
        p_entry = _px_at(e_ts)
        p_resolve = _px_at(r_ts)
        if p_entry is None or p_resolve is None:
            no_path += 1
            continue
        against = "UP" if action == "BUY_NO" else "DOWN"
        flip_ts = flip_px = None
        for k in range(i0, len(rows)):
            ts, dr, conf, px = rows[k]
            if ts > r_ts:
                break
            if dr == against and conf >= args.conf:
                flip_ts, flip_px = ts, px
                break
        if flip_ts is None:
            continue
        flagged += 1
        per_lane[lane]["flagged"] += 1
        lead_times.append((r_ts - flip_ts) / 60.0)
        # adverse move vs the SIDE: for a short (BUY_NO) higher price = adverse; long = lower price
        if action == "BUY_NO":
            adv_flip = (flip_px - p_entry)
            adv_res = (p_resolve - p_entry)
        else:
            adv_flip = (p_entry - flip_px)
            adv_res = (p_entry - p_resolve)
        if adv_flip < adv_res:   # less adverse at flip than at resolution -> exiting at flip helps
            good_signal += 1
            per_lane[lane]["good"] += 1

    print("=" * 74)
    print(f"TAPE-CONFIRMED EXIT SHADOW  (settled losers, conf>={args.conf})")
    print("=" * 74)
    print(f"  losers analyzed:        {losers}  (no entry_ts join: {no_entry}, no price path: {no_path})")
    usable = losers - no_entry - no_path
    if usable <= 0:
        print("  no usable losers with a price path — need more overlap between settled + tape_map windows.")
        return
    print(f"  tape flipped AGAINST before resolution (coverage): {flagged}/{usable} = {100*flagged/usable:.0f}%")
    if flagged:
        lt = sorted(lead_times)
        print(f"    median lead time flip->resolution: {lt[len(lt)//2]:.1f} min")
        print(f"    of flagged, flip was LESS adverse than resolution (good exit): "
              f"{good_signal}/{flagged} = {100*good_signal/flagged:.0f}%")
        print(f"    -> a tape-confirmed exit would have improved ~{good_signal}/{usable} = "
              f"{100*good_signal/usable:.0f}% of all losers")
    print("\n  by lane (losers / flagged / good-exit):")
    for lane, s in sorted(per_lane.items(), key=lambda x: -x[1]["losers"]):
        if s["losers"] < 2:
            continue
        print(f"    {lane[0].replace('_macro',''):5s} {str(lane[1]):4s} {lane[2]:8s}: "
              f"{s['losers']:3d} / {s['flagged']:3d} / {s['good']:3d}")
    print("\n  (coverage = the never-green EXIT opportunity; good-exit% = how often the tape's")
    print("   against-flip preceded a worse resolution. Pairs with tape-map accuracy for sizing the fix.)")


if __name__ == "__main__":
    main()
