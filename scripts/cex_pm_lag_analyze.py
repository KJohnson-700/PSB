#!/usr/bin/env python3
"""Read-out for the CEX->PM lag shadow. Joins cex_pm_lag_shadow.jsonl (Binance move + PM mid per
market_id) to rejected_candidates_settled.jsonl (market_id -> outcome) and answers the ONE question:

  does the fresh BINANCE direction predict the PM outcome, and — the tradeable part — does it beat
  the PM mid (i.e. is the mid mispriced when it lags a fresh Binance move)?

Pure analysis, no side effects. Run periodically; verdict firms up as rows settle (5m/15m markets
resolve within minutes). `binance follows outcome` > 53% AND an edge-over-mid > fees = a real lag edge.
"""
import json
import os
import sys
from collections import defaultdict

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAG = os.path.join(_REPO, "data/calibration/cex_pm_lag_shadow.jsonl")
SETTLED = os.path.join(_REPO, "data/calibration/rejected_candidates_settled.jsonl")


def load_outcomes(max_bytes=120_000_000):
    """market_id -> outcome(UP=1/DOWN=0) from the settled ghost log (tail for speed)."""
    out = {}
    if not os.path.exists(SETTLED):
        return out
    size = os.path.getsize(SETTLED)
    with open(SETTLED) as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            o = str(d.get("outcome") or "").upper()
            mkt = d.get("market_id")
            if mkt and o in ("YES", "NO", "UP", "DOWN"):
                out[str(mkt)] = 1 if o in ("YES", "UP") else 0
    return out


def main():
    outcomes = load_outcomes()
    rows = []
    if os.path.exists(LAG):
        for line in open(LAG):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    joined = [(r, outcomes[str(r["market_id"])]) for r in rows
              if str(r.get("market_id")) in outcomes]
    print(f"lag rows={len(rows)}  settled-joinable={len(joined)}  (outcomes loaded={len(outcomes)})")
    if len(joined) < 20:
        print("insufficient settled joins yet — let it accumulate (5m/15m resolve in minutes).")
        return

    def acc(pred_up, y):
        return sum(1 for p, yy in zip(pred_up, y) if int(p) == yy) / len(y)

    # 1) does fresh Binance direction predict the outcome? (only non-FLAT)
    nf = [(r, y) for r, y in joined if r.get("binance_dir") in ("UP", "DOWN")]
    if nf:
        bpred = [1 if r["binance_dir"] == "UP" else 0 for r, _ in nf]
        by = [y for _, y in nf]
        b_acc = acc(bpred, by)
        # 2) the PM mid's own accuracy on the same set (the bar to beat — you PAY the mid)
        mpred = [1 if r["pm_mid"] > 0.5 else 0 for r, _ in nf]
        m_acc = acc(mpred, by)
        print(f"\nBinance-dir predicts outcome: {b_acc:.3f}  (n={len(nf)})")
        print(f"PM-mid    predicts outcome: {m_acc:.3f}  (the efficient-price bar you pay)")
        print(f"  -> Binance edge OVER the mid: {b_acc - m_acc:+.3f}  (>0 and >fees = tradeable lag)")

    # 3) the sharp subset: mid still ~even while Binance moved (mid lags)
    lag = [(r, y) for r, y in joined if r.get("mid_lags_binance")]
    if lag:
        lpred = [1 if r["binance_dir"] == "UP" else 0 for r, _ in lag]
        ly = [y for _, y in lag]
        print(f"\nMID-LAGS-BINANCE subset (mid ~even, Binance moved): follow-Binance acc="
              f"{acc(lpred, ly):.3f}  n={len(lag)}  <- the money subset if >0.55")

    # 4) by window
    print("\nby window (Binance-dir acc):")
    byw = defaultdict(list)
    for r, y in nf:
        byw[r.get("window")].append((1 if r["binance_dir"] == "UP" else 0, y))
    for w, v in sorted(byw.items()):
        print(f"  {w}: {acc([p for p, _ in v], [y for _, y in v]):.3f}  n={len(v)}")


if __name__ == "__main__":
    main()
