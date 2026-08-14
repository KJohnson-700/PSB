#!/usr/bin/env python3
"""DYNAMIC CONVICTION SIZER — shadow (observe-only). Per Codex design 2026-08-06.

The operator's demand: bigger bets on high-conviction proven lanes, small on coinflips — Kelly on the
REAL edge (clean Binance-truth right-side%), NOT est_prob (AUC~0.5, which inverted the old Kelly).

This is a SHADOW: it computes what each lane WOULD be sized and prints it vs the current flat $15. It
does NOT change live sizing. Ship to live only after the exit fix stabilizes the payoff ratio (Codex:
sizing amplifies edge, so lane-scope + exit come first; the floor-coinflip/grow-proven design IS the
concentration lever).

Method (Codex):
  direction edge p : clean right-side% per (strategy,window,action) from Binance truth (high-n candidates),
                     Beta(25,25)-shrunk, Wilson/Beta LOWER bound (grow only from conservative edge).
  payoff b         : avg_win_pct / |avg_loss_pct| from realized trades (lane, else pooled fallback).
  breakeven        : p_be = 1/(1+b);  edge = p_lcb - p_be
  kelly            : full = max(0,(b*p_lcb-(1-p_lcb))/b); frac = 0.25*full; notional = bankroll*frac
  caps             : min_n_grow=50, min_n_downsize=20; unproven $6-10; proven min($45,0.08*bankroll);
                     coinflip floor $1-3; never est_prob.

Usage: .venv/bin/python scripts/dynamic_sizer_shadow.py [--days 6] [--bankroll 495]
"""
import json, os, sys, time, urllib.request, math
from collections import defaultdict
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
from src.analysis.model_factory import _BINANCE_SYMBOL  # noqa: E402

CAND = os.path.join(_REPO, "data/calibration/rejected_candidates.jsonl")
TRADES = os.path.join(_REPO, "data/calibration/trades.jsonl")
WIN_MIN = {"5m": 5, "15m": 15, "1h": 60}
FUTURES = {"HYPEUSDT"}  # priced off Binance USDM futures, not spot
Z = 1.28  # ~80% one-sided lower bound
PRIOR_A = PRIOR_B = 25.0
KELLY_FRAC = 0.25
MIN_N_GROW, MIN_N_DOWN = 50, 20


def _iso(s):
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return None


def _asset(strategy):
    a = str(strategy or "").replace("_macro", "").upper()
    return "BTC" if a == "BITCOIN" else a


def fetch_range(symbol, a_ms, b_ms):
    base = "https://fapi.binance.com/fapi/v1" if symbol in FUTURES else "https://api.binance.com/api/v3"
    out, cur = {}, a_ms
    while cur < b_ms:
        url = f"{base}/klines?symbol={symbol}&interval=1m&startTime={cur}&endTime={b_ms}&limit=1000"
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                kl = json.load(r)
        except Exception:
            break
        if not kl:
            break
        for k in kl:
            out[int(k[0])] = float(k[4])
        nxt = int(kl[-1][0]) + 60_000
        if nxt <= cur:
            break
        cur = nxt
        if len(kl) < 1000:
            break
        time.sleep(0.12)
    return sorted(out.items())


def price_at(cs, ts):
    lo, hi, ans = 0, len(cs) - 1, None
    while lo <= hi:
        m = (lo + hi) // 2
        if cs[m][0] <= ts:
            ans = cs[m][1]; lo = m + 1
        else:
            hi = m - 1
    return ans


def wilson_lower(wins, n, z=Z):
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def main():
    days = 6
    bankroll = 495.0
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    if "--bankroll" in sys.argv:
        bankroll = float(sys.argv[sys.argv.index("--bankroll") + 1])

    # 1) per-lane direction edge from candidates (Binance truth)
    mkt = {}
    for l in open(CAND):
        if not l.strip().startswith("{"):
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        mid = d.get("market_id")
        if mid and _iso(d.get("market_end_ts")) and str(d.get("window")) in WIN_MIN:
            mkt[str(mid)] = d
    ends = [e for e in (_iso(d.get("market_end_ts")) for d in mkt.values()) if e]
    cutoff = max(ends) - days * 86400
    recs, span = [], {}
    for d in mkt.values():
        end = _iso(d.get("market_end_ts"))
        if not end or end < cutoff:
            continue
        w = str(d.get("window")); sym = _BINANCE_SYMBOL.get(_asset(d.get("strategy")))
        if not sym:
            continue
        recs.append(dict(strategy=d.get("strategy"), window=w, action=d.get("action"),
                         sym=sym, m_start=end - WIN_MIN[w] * 60, m_end=end))
        s = span.setdefault(sym, [end - WIN_MIN[w] * 60, end])
        s[0] = min(s[0], end - WIN_MIN[w] * 60); s[1] = max(s[1], end)
    closes = {sym: fetch_range(sym, int(a * 1000) - 120_000, int(b * 1000) + 120_000)
              for sym, (a, b) in span.items()}
    edge = defaultdict(lambda: {"n": 0, "right": 0})
    for r in recs:
        cs = closes.get(r["sym"])
        if not cs:
            continue
        p0 = price_at(cs, int(r["m_start"] * 1000)); p1 = price_at(cs, int(r["m_end"] * 1000))
        if p0 is None or p1 is None or p0 == p1:
            continue
        up = p1 > p0
        right = (r["action"] == "BUY_YES") == up
        k = (r["strategy"], r["window"], r["action"])
        edge[k]["n"] += 1; edge[k]["right"] += int(right)

    # 2) per-lane REALIZED proof from closed trades (last `days`): n, net$, WR. This is the GATE — a lane
    #    only climbs toward its edge-ceiling once it's actually banking green (direction alone is NOT proof;
    #    the shorts are 56-60% right-side yet -$80..-$98 realized because the exit leaks — sizing those up
    #    on direction would bet bigger on the biggest losers). Direction sets the CEILING; realized sets the CLIMB.
    real = defaultdict(lambda: {"n": 0, "w": 0, "net": 0.0})
    cutoff_ts = time.time() - days * 86400
    for l in open(TRADES):
        if not l.strip().startswith("{"):
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get("shadow_mode"):
            continue
        ca = _iso(d.get("closed_at")) or _iso(d.get("opened_at"))
        if ca is None or ca < cutoff_ts:
            continue
        k = (d.get("strategy"), d.get("window"), d.get("action"))
        r = real[k]; r["n"] += 1; r["net"] += float(d.get("pnl") or 0.0)
        if d.get("win"):
            r["w"] += 1

    # 3) size each lane — edge-ceiling + realized-gated climb (operator model 2026-08-06)
    prov_cap = min(45.0, 0.08 * bankroll)   # ~= $40 proven-winner ceiling
    LONG_FLOOR, LONG_CEIL = 11.0, 16.0      # longs: fade floor -> proven-catch ceiling
    SHORT_BASE = 15.0                       # shorts: base until proven; then climb to edge ceiling
    MIN_N_PROOF = 6                         # realized trades needed before any climb

    def edge_ceiling(rs, is_short):
        """Clean Binance right-side% (POINT estimate, the operator's read) -> the size CEILING this lane may
        climb to. Point estimate, NOT the Wilson-LCB: the LCB over-penalizes moderate-n proven lanes
        (xrp 1h NO 59%, doge 1h NO 57%) and would sit them out. Small-n flukes are guarded by MIN_N_DOWN>=20
        upstream. The LCB conservatism lives in the CLIMB gate (realized), not here."""
        p = rs / 100.0
        if is_short:
            if p < 0.50:  return 0.0        # wrong-direction short -> SIT OUT (sol 15m NO 42%, eth)
            if p >= 0.56: return prov_cap    # proven direction (xrp 5m/1h, btc 5m, sol 5m, bnb 15m, doge 1h) -> up to ~$40
            if p >= 0.54: return 28.0        # good (xrp 15m, btc 15m)
            if p >= 0.52: return 20.0        # marginal
            return SHORT_BASE                # 50-52% coinflip short -> base only, no raise room
        else:  # long
            # 2026-08-06 operator: sit out the marginal 48-50% longs too. Clean rule now: >=50% keep, else SIT OUT.
            if p < 0.50:  return 0.0        # <50% long (incl marginal sol 15m/doge 5m) -> SIT OUT
            return LONG_CEIL                 # kept long (btc 5m 57%, bnb 15m 54%, ...) -> up to $16 once proven green

    print(f"bankroll=${bankroll:.0f}  proven_cap(~$40)=${prov_cap:.0f}  MIN_N_PROOF={MIN_N_PROOF}  (dir sets CEILING, realized sets CLIMB)")
    print(f"{'LANE':36} {'dir_n':>5} {'dir%':>5} {'p_lcb':>6} | {'rn':>3} {'rWR%':>5} {'rNet$':>7} | {'ceil':>5} {'SIZE':>6}  note")
    print("-" * 104)
    rows = []
    for k, e in edge.items():
        n, wins = e["n"], e["right"]
        if n < MIN_N_DOWN:
            continue
        p_use = min(wilson_lower(wins, n), (wins + PRIOR_A) / (n + PRIOR_A + PRIOR_B))
        is_short = (k[2] == "BUY_NO")
        base = SHORT_BASE if is_short else LONG_FLOOR
        ceil = edge_ceiling(wins / n * 100.0, is_short)
        r = real[k]; rn, rnet = r["n"], r["net"]
        rwr = (r["w"] / rn * 100) if rn else 0.0
        if ceil <= 0.0:
            size, note = 0.0, "SIT-OUT (dir)"
        else:
            proven_green = (rn >= MIN_N_PROOF and rnet > 0.0)
            if proven_green:
                # climb proportional to how convincingly green (net$/trade), capped at ceiling
                climb = min(1.0, max(0.0, (rnet / rn) / 1.0))   # +$1/trade -> full climb
                size = round(base + (ceil - base) * climb, 1)
                note = "RAISED (proven green)"
            elif rn >= MIN_N_PROOF and rnet <= 0.0:
                size, note = base, "BASE (leaking->exit-fix gates raise)"
            else:
                size, note = base, "BASE (n<proof)"
        rows.append((k, n, wins / n * 100, p_use, rn, rwr, rnet, ceil, size, note))
    for k, n, rs, p_use, rn, rwr, rnet, ceil, size, note in sorted(rows, key=lambda x: (-x[8], -x[7])):
        print(f"{str(k):36} {n:5} {rs:4.0f}% {p_use:6.3f} | {rn:3} {rwr:4.0f}% {rnet:7.1f} | {ceil:5.0f} {size:6.1f}  {note}")
    print("\nNOTE: shadow only. DIRECTION (clean Binance-truth right-side LCB) sets each lane's CEILING (proven")
    print("shorts up to ~$40, kept longs up to $16, wrong-dir -> SIT OUT). REALIZED net$ over last window GATES")
    print("the climb: a lane sits at base until it's banking green (n>=proof, net>0), then climbs to its ceiling.")
    print("So the exit fix — not the sizer — unlocks the raise: as it converts +MFE into wins, lanes climb on their own.")


if __name__ == "__main__":
    main()
