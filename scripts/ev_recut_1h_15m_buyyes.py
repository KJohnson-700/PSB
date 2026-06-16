#!/usr/bin/env python3
"""Net-of-fee EV recut for the four 1h/15m BUY_YES pools the verdict cited
(+ ETH 1h BUY_YES as the negative control).

Why: ghost yes_price ~= P(YES), so WR-by-price on BUY_YES is near-tautological
(a high-WR long pool just means it bought cheap favorites). Rank on realized
EV net of the crypto fee, NOT on WR.

realized_pct (from settler) = (1-entry)/entry if won, else -1.0  -> per $ staked.
Crypto fee per the code: fee = shares*rate*p*(1-p), rate=0.072.
As a fraction of cash staked (notional = shares*p):  fee/notional = 0.072*(1-p).
Held-to-resolution exit fee -> 0 (p(1-p)=0 at 0/1), so entry fee ~= round-trip here.

Time-split: all-time vs --since to check regime stability (prior session flagged
the +EV pockets as regime-unstable).
"""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SETTLED = REPO / "data" / "calibration" / "rejected_candidates_settled.jsonl"
RATE = 0.072  # crypto fee rate

# (label, strategy, window) — all BUY_YES / LONG
LANES = [
    ("SOL 1h BUY_YES",  "sol_macro",  "1h"),
    ("HYPE 1h BUY_YES", "hype_macro", "1h"),
    ("XRP 15m BUY_YES", "xrp_macro",  "15m"),
    ("DOGE 15m BUY_YES","doge_macro", "15m"),
    ("ETH 1h BUY_YES",  "eth_macro",  "1h"),  # control: verdict says don't loosen
]
KEY = {(s, w): label for label, s, w in LANES}


def wilson_lo(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return (c - m) / d


def summarize(rows):
    n = len(rows)
    if n == 0:
        return None
    wins = sum(1 for r in rows if r["win"])
    gross = sum(r["realized_pct"] for r in rows) / n
    fees = [RATE * (1.0 - r["yes_price"]) for r in rows]
    mean_fee = sum(fees) / n
    net = [r["realized_pct"] - f for r, f in zip(rows, fees)]
    net_mean = sum(net) / n
    prices = sorted(r["yes_price"] for r in rows)
    med_price = prices[n // 2]
    return {
        "n": n, "wr": wins / n, "gross_ev": gross, "mean_fee": mean_fee,
        "net_ev": net_mean, "wilson_wr": wilson_lo(wins, n), "med_price": med_price,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO date; recent-era split")
    args = ap.parse_args()

    # quick substring prefilter to skip JSON parse on irrelevant lines
    want_strats = {s for _, s, _ in LANES}
    prefilter = tuple(f'"{s}"' for s in want_strats)

    buckets = defaultdict(lambda: {"all": [], "recent": []})
    bad = 0
    with SETTLED.open() as fh:
        for line in fh:
            if not any(p in line for p in prefilter):
                continue
            if '"action": "BUY_YES"' not in line and '"action":"BUY_YES"' not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                bad += 1
                continue
            k = (r.get("strategy"), r.get("window"))
            if k not in KEY:
                continue
            if r.get("action") != "BUY_YES":
                continue
            if r.get("win") is None or r.get("realized_pct") is None:
                continue
            yp = r.get("yes_price")
            if yp is None or yp <= 0 or yp >= 1:
                continue
            rec = {"win": bool(r["win"]), "realized_pct": float(r["realized_pct"]), "yes_price": float(yp)}
            ts = r.get("ts", "")
            buckets[KEY[k]]["all"].append(rec)
            if args.since and ts >= args.since:
                buckets[KEY[k]]["recent"].append(rec)

    # entry-price band breakdown (all-time) — shows where any +EV lives
    BANDS = [(0.0, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 1.0)]
    print("\n=== ALL-TIME by entry-price band (netEV / n) ===")
    bhdr = f"{'lane':<18} " + " ".join(f"{lo:.2f}-{hi:.2f}".rjust(14) for lo, hi in BANDS)
    print(bhdr)
    print("-" * len(bhdr))
    for label, *_ in LANES:
        rows = buckets[label]["all"]
        cells = []
        for lo, hi in BANDS:
            sub = [r for r in rows if lo <= r["yes_price"] < hi or (hi == 1.0 and r["yes_price"] >= lo)]
            s = summarize(sub)
            cells.append(f"{s['net_ev']:+.3f}/{s['n']}".rjust(14) if s else "—".rjust(14))
        print(f"{label:<18} " + " ".join(cells))

    hdr = f"{'lane':<18} {'n':>6} {'WR':>6} {'grossEV':>8} {'fee':>7} {'netEV':>8} {'medPx':>6}"
    print("\n=== ALL-TIME ===")
    print(hdr)
    print("-" * len(hdr))
    for label, *_ in LANES:
        s = summarize(buckets[label]["all"])
        if s:
            print(f"{label:<18} {s['n']:>6} {s['wr']*100:>5.1f}% {s['gross_ev']:>+8.4f} "
                  f"{s['mean_fee']:>7.4f} {s['net_ev']:>+8.4f} {s['med_price']:>6.2f}")
    if args.since:
        print(f"\n=== RECENT (ts >= {args.since}) ===")
        print(hdr)
        print("-" * len(hdr))
        for label, *_ in LANES:
            s = summarize(buckets[label]["recent"])
            if s:
                print(f"{label:<18} {s['n']:>6} {s['wr']*100:>5.1f}% {s['gross_ev']:>+8.4f} "
                      f"{s['mean_fee']:>7.4f} {s['net_ev']:>+8.4f} {s['med_price']:>6.2f}")
    if bad:
        print(f"\n(skipped {bad} unparseable lines)")
    print("\nRank on netEV. WR is near-tautological on BUY_YES (yes_price ~= P(YES)).")


if __name__ == "__main__":
    main()
