#!/usr/bin/env python3
"""Replay the LaneTapeAdapter over historical session journals.

Proves (or refutes) that the adapter would have de-sized the collapse lanes
BEFORE they bled, while leaving the good session's winners at full size.

For each session it walks EXITs in time order, and for each close asks the
adapter (in LIVE mode) what multiplier it WOULD have applied to THAT trade's
notional GIVEN ONLY the lane's prior closes (no look-ahead) — then updates the
adapter with the close. It reports actual vs adapted session P&L, per lane.

Usage: python3 scripts/replay_lane_tape_adapter.py
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.analysis.lane_tape_adapter import LaneTapeAdapter, lane_key  # noqa: E402

SESSIONS = [
    ("test_20260722_200518", "GOOD +562"),
    ("test_20260725_185351", "COLLAPSE -95 (convmax)"),
    ("test_20260725_193644", "COLLAPSE -48"),
    ("test_20260726_013503", "-34"),
    ("test_20260726_084253", "CURRENT"),
]

CFG = {
    "mode": "live",
    "window_closes": 5,
    "green_arm_pct": 0.08,
    "min_samples": 2,
    "min_mult": 0.25,
    "max_mult": 1.0,        # de-size only (no upsize yet); upsize is a gated v2 stage
    "loss_ref_dollars": 4.0,
    "green_keep_rate": 0.5,  # net-losing lanes only de-size once green_rate < this
    "recency_ramp": 2.0,
}


def load_exits(sid):
    f = f"data/paper_trades/{sid}/entries.jsonl"
    if not os.path.exists(f):
        return []
    rows = [json.loads(l) for l in open(f) if l.strip()]
    exits = [r for r in rows if r.get("event") == "EXIT"]
    exits.sort(key=lambda r: r.get("timestamp", ""))
    return exits


def replay(sid):
    ad = LaneTapeAdapter(CFG)
    exits = load_exits(sid)
    actual_total = 0.0
    adapted_total = 0.0
    lane_actual = defaultdict(float)
    lane_adapted = defaultdict(float)
    lane_desized_before_loss = defaultdict(lambda: [0, 0])  # [de-sized losers, total losers]
    for e in exits:
        extra = e.get("extra") or {}
        asset = e.get("strategy", "")
        win = extra.get("window_size", "?")
        side = e.get("action", "")
        pnl = float(e.get("pnl") or 0.0)
        mfe = float(extra.get("mfe_pct") or 0.0)
        key = lane_key(asset, win, side)
        # multiplier from PRIOR closes only (no look-ahead)
        mult = ad.size_multiplier(asset, win, side)
        adapted_pnl = pnl * mult
        actual_total += pnl
        adapted_total += adapted_pnl
        lane_actual[key] += pnl
        lane_adapted[key] += adapted_pnl
        if pnl < 0:
            lane_desized_before_loss[key][1] += 1
            if mult < 0.999:
                lane_desized_before_loss[key][0] += 1
        # now learn from this close
        ad.record_close(asset, win, side, mfe_pct=mfe, pnl=pnl)
    return actual_total, adapted_total, lane_actual, lane_adapted, lane_desized_before_loss


def main():
    print(f"LaneTapeAdapter replay  cfg={CFG}\n")
    print(f"{'SESSION':<26} {'actual':>9} {'adapted':>9} {'delta':>8}  {'saved%':>6}")
    grand_a = grand_ad = 0.0
    per_lane_delta = defaultdict(lambda: [0.0, 0.0])
    desize = defaultdict(lambda: [0, 0])
    for sid, label in SESSIONS:
        a, ad, la, lad, dz = replay(sid)
        grand_a += a
        grand_ad += ad
        saved = ((ad - a) / abs(a) * 100) if a else 0.0
        print(f"{label:<26} {a:>9.2f} {ad:>9.2f} {ad-a:>+8.2f}  {saved:>5.0f}%")
        for k in la:
            per_lane_delta[k][0] += la[k]
            per_lane_delta[k][1] += lad[k]
        for k in dz:
            desize[k][0] += dz[k][0]
            desize[k][1] += dz[k][1]
    print(f"{'TOTAL':<26} {grand_a:>9.2f} {grand_ad:>9.2f} {grand_ad-grand_a:>+8.2f}")
    print("\n=== Per-lane (all sessions): actual -> adapted, and de-size hit-rate on losers ===")
    print(f"{'lane':<22} {'actual':>9} {'adapted':>9} {'delta':>8}  desized_losers")
    for k in sorted(per_lane_delta, key=lambda k: per_lane_delta[k][0]):
        a, ad = per_lane_delta[k]
        d = desize[k]
        hit = f"{d[0]}/{d[1]}" if d[1] else "-"
        flag = ""
        if a < -5 and ad > a + 1:
            flag = "  <-- bleed reduced"
        if a > 20 and abs(ad - a) < abs(a) * 0.15:
            flag = "  <-- winner preserved"
        print(f"{k:<22} {a:>9.2f} {ad:>9.2f} {ad-a:>+8.2f}  {hit:>10}{flag}")


if __name__ == "__main__":
    main()
