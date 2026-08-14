#!/usr/bin/env python3
"""#105 tape_map UP-branch validator — READ-ONLY, no bot state touched.

The operator's #1 concern: a tape MAP that can only ever label DOWN/FLAT (never UP)
is the exact tape-blind, one-direction failure that breaks when the tape flips. This
session logged UP≈0 / DOWN-heavy, so we must distinguish:

  (A) GENUINE bear tape  — the machinery is symmetric, the inputs were just bearish, OR
  (B) ASYMMETRIC DEFECT  — the UP branch is structurally harder to reach than DOWN.

We cannot replay a past bull from disk (trade logs carry only *bucketed* htf/rsi, not raw
macd/ema/trend — the compute_tape_state inputs). So the decisive test is SYMMETRY BY
CONSTRUCTION: feed compute_tape_state a bullish input and its exact bearish mirror; a
correct, tape-agnostic classifier must return UP for one and DOWN for the other with the
same |dscore|. If every bull input mirrors a bear input, the branch is NOT biased — the
DOWN-heavy session is real tape (A). Any asymmetry is (B) and must be fixed before Phase-2.

Also: replays the live tape_map.jsonl dscore distribution as context.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.analysis.tape_map import compute_tape_state  # noqa: E402

MAP_PATH = ROOT / "data" / "calibration" / "tape_map.jsonl"


def _bull(**over):
    """A cleanly bullish input: price above a rising EMA stack, MACD up, trend UP."""
    base = dict(
        asset="test", current_price=105.0, atr_14=1.0,
        ema_9=104.0, ema_21=102.0, ema_50=100.0,   # cp>e21>e50 & e9>=e21 -> ema_dir +1
        macd_5m=1.0, macd_15m=1.0, macd_1h=1.0,     # macd_net +3 -> sign +1
        trend_direction="UP", trend_strength=0.8, rsi_14=60.0,
    )
    base.update(over)
    return base


def _mirror(inp):
    """Exact bearish mirror of a bull input: flip price/EMA order, MACD signs, trend."""
    m = dict(inp)
    cp = inp.get("current_price")
    # mirror the EMA stack around the price so the geometry is the exact inverse
    # (only when a real stack is present; the BTC aux_dir path has no EMAs).
    if cp is not None:
        for k in ("ema_9", "ema_21", "ema_50"):
            if inp.get(k) is not None:
                m[k] = cp + (cp - inp[k])
    for k in ("macd_5m", "macd_15m", "macd_1h"):
        if inp.get(k) is not None:
            m[k] = -inp[k]
    if inp.get("aux_dir") is not None:
        m["aux_dir"] = -inp["aux_dir"]
    td = str(inp.get("trend_direction") or "").upper()
    m["trend_direction"] = {"UP": "DOWN", "BULLISH": "BEARISH",
                            "DOWN": "UP", "BEARISH": "BULLISH"}.get(td, td)
    return m


# Battery of bull configs spanning: full-stack alt, MACD-only, EMA-only, trend-only,
# 2-of-3 combos, and the BTC aux_dir path (no EMA stack, MA-trend vote instead).
CASES = [
    ("full bull (macd+ema+trend)", _bull()),
    ("macd+trend, ema flat", _bull(ema_9=100.0, ema_21=100.0, ema_50=100.0)),
    ("macd+ema, trend none", _bull(trend_direction=None)),
    ("ema+trend, macd flat", _bull(macd_5m=0.0, macd_15m=0.0, macd_1h=0.0)),
    ("macd-only (2of3 needs another)", _bull(ema_9=100.0, ema_21=100.0, ema_50=100.0, trend_direction=None)),
    ("BTC aux_dir path (no ema stack)", _bull(current_price=None, ema_9=None, ema_21=None,
                                              ema_50=None, aux_dir=1.0)),
    ("BTC aux+macd, trend none", _bull(current_price=None, ema_9=None, ema_21=None,
                                       ema_50=None, aux_dir=1.0, trend_direction=None)),
]


def main():
    print("=" * 72)
    print("#105 TAPE-MAP SYMMETRY VALIDATOR  (is the UP branch reachable & unbiased?)")
    print("=" * 72)
    asym = 0
    up_fired = 0
    for name, bull in CASES:
        bs = compute_tape_state(**bull)
        ms = compute_tape_state(**_mirror(bull))
        bd, md = bs["dscore"], ms["dscore"]
        bdir, mdir = bs["direction"], ms["direction"]
        symmetric = (bd == -md) and (
            (bdir, mdir) in (("UP", "DOWN"), ("FLAT", "FLAT"), ("DOWN", "UP"))
        )
        if bdir == "UP":
            up_fired += 1
        flag = "OK " if symmetric else "!! ASYMMETRIC"
        if not symmetric:
            asym += 1
        print(f"  [{flag}] {name:34s} bull={bdir}({bd:+d})  mirror={mdir}({md:+d})")
    print("-" * 72)
    print(f"  UP fired in {up_fired}/{len(CASES)} bull cases; asymmetries: {asym}")
    verdict = ("SYMMETRIC — UP branch reachable, no directional bias. The DOWN-heavy "
               "session is REAL TAPE (A), not a defect."
               if asym == 0 and up_fired > 0 else
               "ASYMMETRY DETECTED (B) — UP branch is biased/dead; FIX before Phase-2.")
    print(f"  VERDICT: {verdict}")

    # Context: live dscore distribution from the shadow log.
    if MAP_PATH.exists():
        dc = {}
        mx = -9
        for l in MAP_PATH.open():
            try:
                d = json.loads(l).get("dscore")
            except Exception:
                continue
            if d is not None:
                dc[d] = dc.get(d, 0) + 1
                mx = max(mx, d)
        print("-" * 72)
        print("  live tape_map.jsonl dscore dist:", dict(sorted(dc.items())), "max=", mx)
        print("  (reachability on live data: UP requires dscore>=+2; DOWN<=-2)")


if __name__ == "__main__":
    main()
