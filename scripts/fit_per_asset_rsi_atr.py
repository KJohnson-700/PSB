#!/usr/bin/env python3
"""Fit per-asset RSI/ATR bands for _estimate_probability from the settled ghost log.

The legacy bands (RSI 75/65/30/25/35/70, ATR% 0.03/0.01) were SOL-tuned and shared.
ATR% is an absolute scale that differs hugely per asset -> rescale by matching SOL's
percentile anchors. RSI is normalized (0-100) -> only rescale if an asset's distribution
diverges materially; also print realized-EV by RSI bucket x direction to sanity-check
that the overbought-penalty assumption even holds per asset (don't rescale a wrong sign).
"""
import json
from collections import defaultdict
from pathlib import Path
import numpy as np

REPO = Path(__file__).resolve().parents[1]
SETTLED = REPO / "data" / "calibration" / "rejected_candidates_settled.jsonl"
STRATS = {"sol_macro", "xrp_macro", "hype_macro", "bnb_macro", "doge_macro"}
SOL_RSI_ANCHORS = {"ob_strong": 75, "ob_mild": 65, "os_bounce": 30,
                   "os_strong": 25, "os_mild": 35, "ob_crash": 70}
SOL_ATR_ANCHORS = {"atr_high_pct": 0.03, "atr_low_pct": 0.01}


def main():
    rsi = defaultdict(list)
    atrp = defaultdict(list)
    # (strat, direction) -> list of (rsi, realized_pct)
    ev = defaultdict(list)
    pre = tuple(f'"{s}"' for s in STRATS)
    n = 0
    with SETTLED.open() as fh:
        for line in fh:
            if not any(p in line for p in pre):
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            s = r.get("strategy")
            if s not in STRATS:
                continue
            ctx = r.get("context") or {}
            rv = ctx.get("rsi_14"); av = ctx.get("atr_14"); sp = ctx.get("asset_spot")
            if rv is None or av is None or not sp:
                continue
            try:
                rv = float(rv); ap = float(av) / float(sp)
            except Exception:
                continue
            if not (0 < rv < 100) or not (0 < ap < 1):
                continue
            rsi[s].append(rv); atrp[s].append(ap)
            direction = "UP" if r.get("action") == "BUY_YES" else "DOWN"
            rp = r.get("realized_pct")
            if rp is not None:
                ev[(s, direction)].append((rv, float(rp)))
            n += 1

    # SOL percentile rank of each anchor
    sol_rsi = np.array(rsi["sol_macro"]); sol_atr = np.array(atrp["sol_macro"])
    def pctile_rank(arr, v):
        return float((arr < v).mean() * 100)
    rsi_anchor_pct = {k: pctile_rank(sol_rsi, v) for k, v in SOL_RSI_ANCHORS.items()}
    atr_anchor_pct = {k: pctile_rank(sol_atr, v) for k, v in SOL_ATR_ANCHORS.items()}

    print(f"rows={n}\nSOL anchor percentile ranks:")
    print("  RSI:", {k: f"{v:.0f}%" for k, v in rsi_anchor_pct.items()})
    print("  ATR:", {k: f"{v:.0f}%" for k, v in atr_anchor_pct.items()})

    print("\n=== RSI distribution (p10/p25/p50/p75/p90) ===")
    for s in ["sol_macro", "xrp_macro", "hype_macro", "bnb_macro", "doge_macro"]:
        a = np.array(rsi[s])
        if len(a) == 0:
            continue
        q = np.percentile(a, [10, 25, 50, 75, 90])
        print(f"{s:<11} n={len(a):>6} " + " ".join(f"{x:5.1f}" for x in q))

    print("\n=== ATR% distribution (p10/p25/p50/p75/p90) ===")
    for s in ["sol_macro", "xrp_macro", "hype_macro", "bnb_macro", "doge_macro"]:
        a = np.array(atrp[s])
        if len(a) == 0:
            continue
        q = np.percentile(a, [10, 25, 50, 75, 90])
        print(f"{s:<11} n={len(a):>6} " + " ".join(f"{x*100:6.3f}%" for x in q))

    print("\n=== APPLIED bands (alt value at SOL's anchor percentile) ===")
    hdr = ("asset      ob_strong ob_mild os_bounce os_strong os_mild ob_crash | "
           "atr_high atr_low")
    print(hdr); print("-" * len(hdr))
    applied = {}
    for s in ["sol_macro", "xrp_macro", "hype_macro", "bnb_macro", "doge_macro"]:
        ra = np.array(rsi[s]); aa = np.array(atrp[s])
        if len(ra) == 0:
            continue
        band = {k: float(np.percentile(ra, rsi_anchor_pct[k])) for k in SOL_RSI_ANCHORS}
        band["atr_high_pct"] = float(np.percentile(aa, atr_anchor_pct["atr_high_pct"]))
        band["atr_low_pct"] = float(np.percentile(aa, atr_anchor_pct["atr_low_pct"]))
        applied[s] = band
        print(f"{s:<11} {band['ob_strong']:8.1f} {band['ob_mild']:7.1f} "
              f"{band['os_bounce']:9.1f} {band['os_strong']:9.1f} {band['os_mild']:7.1f} "
              f"{band['ob_crash']:8.1f} | {band['atr_high_pct']*100:6.3f}% {band['atr_low_pct']*100:6.3f}%")

    print("\n=== outcome check: realized-EV by RSI bucket (mean realized_pct / n) ===")
    print("(validates whether high-RSI UP / low-RSI DOWN actually underperform per asset)")
    buckets = [("<30", 0, 30), ("30-50", 30, 50), ("50-65", 50, 65), ("65-75", 65, 75), (">75", 75, 100)]
    for s in ["sol_macro", "xrp_macro", "hype_macro", "bnb_macro", "doge_macro"]:
        for d in ("UP", "DOWN"):
            rows = ev.get((s, d), [])
            if not rows:
                continue
            cells = []
            for lbl, lo, hi in buckets:
                sub = [rp for rv, rp in rows if lo <= rv < hi]
                cells.append(f"{lbl}:{np.mean(sub):+.3f}/{len(sub)}" if sub else f"{lbl}:—")
            print(f"{s:<11} {d:<4} " + "  ".join(cells))

    print("\n--- YAML to paste under each strategies.<asset> block ---")
    for s, band in applied.items():
        print(f"  # {s}")
        print(f"  est_prob:")
        print(f"    rsi_overbought_strong: {band['ob_strong']:.1f}")
        print(f"    rsi_overbought_mild: {band['ob_mild']:.1f}")
        print(f"    rsi_oversold_bounce: {band['os_bounce']:.1f}")
        print(f"    rsi_oversold_strong: {band['os_strong']:.1f}")
        print(f"    rsi_oversold_mild: {band['os_mild']:.1f}")
        print(f"    rsi_overbought_crash: {band['ob_crash']:.1f}")
        print(f"    atr_high_pct: {band['atr_high_pct']:.4f}")
        print(f"    atr_low_pct: {band['atr_low_pct']:.4f}")


if __name__ == "__main__":
    main()
