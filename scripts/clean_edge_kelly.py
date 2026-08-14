#!/usr/bin/env python3
"""CLEAN-EDGE KELLY BACKTEST (2026-08-06) — the accurate Kelly input the removed backtests used to give.

The problem the operator named: Kelly needs an accurate per-trade/per-lane WIN-PROBABILITY. That came
from the backtests. They were removed (known-broken), so Kelly ran on est_prob (AUC~0.5 = coin-flip) and
sized LOSERS bigger. Flat sizing was a retreat to break-even, not a fix. This is the fix: a HONEST backtest
that replays every entry against the REAL Binance resolution (exchange truth, not ghost, not PM mid, not
where we exited) to estimate each lane's true win-prob, then sizes it by KELLY. Big money on the proven
58-60% lanes, ~nothing on the 50% coin-flips = concentrate capital on real edge = PROFIT, not break-even.

Valid NOW (wasn't before) because exits hold to resolution: a lane's realized result == its direction, so
the clean win-prob IS the realized edge. Kelly on a real 57% edge with ~symmetric binary payoff is +EV.

Method:
  win-prob p : clean right-side% per (strategy,window,action) from Binance truth, Beta(a,b)-shrunk +
               Wilson LOWER bound (grow only from a CONSERVATIVE edge — never the point estimate).
  payoff b   : from the lane's avg entry price c (binary: win pays $1, so b=(1-c)/c).
  kelly      : f = max(0, (p*b - (1-p)) / b);  size = bankroll * (KELLY_FRAC * f), capped/floored.
  A lane with p<=breakeven gets f=0 -> sits at the floor (or sit-out). No est_prob anywhere.

Usage: .venv/bin/python scripts/clean_edge_kelly.py [--days 6] [--bankroll 500] [--write]
  --write : also emit data/calibration/clean_edge_kelly_sizes.json (per-lane $ the live sizer can read)
"""
import json, os, sys, time, math
from collections import defaultdict
from datetime import datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

# 2026-08-06: data source = the SETTLED shadow log (rejected_candidates_settled.jsonl). Each row is a
# scanned candidate joined to the REAL market resolution (`win` = did our side match the outcome) + the
# entry prices we'd have paid (yes_price/no_price). This is the ground-truth win-prob AND payoff Kelly
# needs — the accurate inputs the removed backtests used to provide. The raw rejected_candidates.jsonl
# is auto-rotated to only ~hours of UNRESOLVED markets, which is why the first cut sized only 1 lane.
SETTLED = os.path.join(_REPO, "data/calibration/rejected_candidates_settled.jsonl")
OUT = os.path.join(_REPO, "data/calibration/clean_edge_kelly_sizes.json")
WIN_MIN = {"5m": 5, "15m": 15, "1h": 60}
Z = 1.28                       # ~80% one-sided lower bound (conservative growth)
PRIOR_A = PRIOR_B = 20.0       # Beta shrink toward 0.50
KELLY_FRAC = 0.30              # fractional Kelly (safety)
MIN_N = 50                     # unique MARKETS per lane (after dedup) before sizing on the edge
FLOOR_USD, CAP_USD = 11.0, 45.0  # no dust; hard risk cap (also bounded by 8% exposure downstream)


def _iso(s):
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return None


def wilson_lower(w, n, z=Z):
    if n == 0:
        return 0.0
    p = w / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (c - m) / d)


def main():
    days = 6; bankroll = 500.0; write = "--write" in sys.argv
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    if "--bankroll" in sys.argv:
        bankroll = float(sys.argv[sys.argv.index("--bankroll") + 1])

    # 1) per-lane win-rate + entry price from the SETTLED log (real Polymarket outcome), DEDUPED to ONE
    #    bet per market. The log is a per-SCAN ghost stream — a market is re-logged dozens of times (86x on
    #    some lanes), so counting raw rows inflates n AND biases the edge (Codex NO-GO: btc 15m NO flipped
    #    +0.032 -> -0.011 after dedup). Keep ONE row per (strategy,window,action,market_id): the LATEST
    #    valid-priced scan (closest to the entry decision). Recency filters on candidate scan `ts` (NOT
    #    settled_at — a backfill would make old regimes look recent). Price validated BEFORE counting so p
    #    and b come from the same sample; `win` must be a real bool.
    cutoff = time.time() - days * 86400
    seen = {}   # (strategy,window,action,market_id) -> (ts, win01, c)
    for l in open(SETTLED):
        if not l.strip().startswith("{"):
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        ts = _iso(d.get("ts"))
        if ts is None or ts < cutoff:
            continue
        win = d.get("win")
        if win not in (True, False) or str(d.get("window")) not in WIN_MIN:
            continue
        mid = d.get("market_id")
        if not mid:
            continue
        act = d.get("action")
        c = d.get("no_price") if act == "BUY_NO" else d.get("yes_price")  # outcome-leg price we pay
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if not (0.01 < c < 0.99):
            continue
        key = (d.get("strategy"), str(d.get("window")), act, str(mid))
        prev = seen.get(key)
        # keep the EARLIEST valid-priced scan per market: the bot enters early in the window (~0.50),
        # so the early price ~= the price it actually pays. The LATEST scan is near resolution (~0.65,
        # after the move is priced in) => wrong entry price, flips every lane artificially negative.
        if prev is None or ts < prev[0]:
            seen[key] = (ts, int(win is True), c)
    lane = defaultdict(lambda: {"n": 0, "w": 0, "cs": []})
    for (strat, win_tf, act, _mid), (_ts, w, c) in seen.items():
        a = lane[(strat, win_tf, act)]
        a["n"] += 1; a["w"] += w; a["cs"].append(c)

    # 2) per-lane Kelly on the CLEAN win-prob
    print(f"bankroll=${bankroll:.0f}  KELLY_FRAC={KELLY_FRAC}  MIN_N={MIN_N}  (win-prob = real Polymarket outcome, deduped per market, conservative; NO est_prob)")
    print(f"{'LANE':34} {'n':>4} {'raw%':>5} {'p_cons':>6} {'entry':>6} {'b':>5} {'edge':>6} {'kelly$':>7}")
    print("-" * 88)
    rows, sizes = [], {}
    for k, e in lane.items():
        n, w = e["n"], e["w"]
        if n < MIN_N:
            continue
        raw = w / n
        p = min(wilson_lower(w, n), (w + PRIOR_A) / (n + PRIOR_A + PRIOR_B))  # conservative
        c = (sum(e["cs"]) / len(e["cs"])) if e["cs"] else 0.5
        b = (1.0 - c) / c
        p_be = 1.0 / (1.0 + b)
        f = max(0.0, (p * b - (1.0 - p)) / b)
        size = bankroll * KELLY_FRAC * f
        if f <= 0:
            size = 0.0                      # no edge (LCB below breakeven) -> sit out / floor
        else:
            size = min(CAP_USD, max(FLOOR_USD, size))
        key = "%s|%s|%s" % k
        sizes[key] = round(size, 1)
        rows.append((key, n, raw * 100, p, c, b, p - p_be, size))
    for key, n, raw, p, c, b, edg, size in sorted(rows, key=lambda x: -x[7]):
        tag = "SIT-OUT" if size == 0 else ("BIG" if size >= 25 else "")
        print(f"{key:34} {n:4} {raw:4.0f}% {p:6.3f} {c:6.3f} {b:5.2f} {edg:+6.3f} {size:6.1f}  {tag}")
    print(f"\n{len([r for r in rows if r[7]>=25])} BIG-edge lanes (>=$25) get the capital; "
          f"{len([r for r in rows if r[7]==0])} no-edge lanes sit out. est_prob NEVER used.")
    if write:
        with open(OUT, "w") as f:
            json.dump({"generated_note": "clean-edge Kelly; win-prob from Binance truth, not est_prob",
                       "kelly_frac": KELLY_FRAC, "bankroll": bankroll, "sizes": sizes}, f, indent=2)
        print(f"\nwrote {OUT} ({len(sizes)} lanes)")


if __name__ == "__main__":
    main()
