#!/usr/bin/env python3
"""HIGH-N clean right-side ledger — resolver DIRECTION quality across ALL scanned candidates.

Same exchange-truth method as clean_rightside_ledger.py, but over the full candidate stream
(data/calibration/rejected_candidates.jsonl) instead of only the 81 admitted trades. Each candidate
carries market_end_ts + window + the resolver's side pick (action/side/resolver_path) — so we get the
resolver's direction accuracy at real n, joined to Binance truth. No ghost, no exit, no sizing.

Dedup by market_id (a market is re-logged every scan cycle; one outcome per market). Keeps the LAST
side seen per market. Scores right-side per (strategy, window, action).

Usage: .venv/bin/python scripts/clean_rightside_candidates.py [--days N]  (default 4)
"""
import json, os, sys, time, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
from src.analysis.model_factory import _BINANCE_SYMBOL  # noqa: E402

CAND = os.path.join(_REPO, "data/calibration/rejected_candidates.jsonl")
WIN_MIN = {"5m": 5, "15m": 15, "1h": 60}


def _iso(s):
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return None


def _asset(strategy):
    a = str(strategy or "").replace("_macro", "").upper()
    return "BTC" if a == "BITCOIN" else a


def fetch_klines_range(symbol, start_ms, end_ms):
    out = {}
    cur = start_ms
    while cur < end_ms:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m"
               f"&startTime={cur}&endTime={end_ms}&limit=1000")
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                kl = json.load(r)
        except Exception as e:
            print(f"  fetch err {symbol}: {e}", file=sys.stderr); break
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
    return out


def price_at(cs, ts_ms):
    lo, hi, ans = 0, len(cs) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if cs[mid][0] <= ts_ms:
            ans = cs[mid][1]; lo = mid + 1
        else:
            hi = mid - 1
    return ans


def main():
    days = 4
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    now = None
    # dedup by market_id -> last candidate seen
    mkt = {}
    n = 0
    for l in open(CAND):
        if not l.strip().startswith("{"):
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        n += 1
        mid = d.get("market_id")
        end = _iso(d.get("market_end_ts"))
        w = str(d.get("window"))
        if mid is None or end is None or w not in WIN_MIN:
            continue
        mkt[str(mid)] = d  # last wins
    print(f"candidate rows read: {n}   distinct markets: {len(mkt)}")

    # cutoff to last `days`
    ends = [_iso(d.get("market_end_ts")) for d in mkt.values()]
    ends = [e for e in ends if e]
    cutoff = max(ends) - days * 86400
    recs, span = [], {}
    for d in mkt.values():
        end = _iso(d.get("market_end_ts"))
        if end is None or end < cutoff:
            continue
        w = str(d.get("window"))
        m_start = end - WIN_MIN[w] * 60
        asset = _asset(d.get("strategy"))
        sym = _BINANCE_SYMBOL.get(asset)
        if not sym:
            continue
        recs.append(dict(strategy=d.get("strategy"), window=w, action=d.get("action"),
                         side=d.get("side"), sym=sym, m_start=m_start, m_end=end,
                         htf=str(d.get("primary_htf_bias") or d.get("htf_bias") or "NA").upper()))
        s = span.setdefault(sym, [m_start, end])
        s[0] = min(s[0], m_start); s[1] = max(s[1], end)
    print(f"markets in last {days}d with reconstructable window: {len(recs)}")

    closes = {}
    for sym, (a, b) in span.items():
        print(f"  fetch {sym} {datetime.fromtimestamp(a,timezone.utc):%m-%d %H:%M}..{datetime.fromtimestamp(b,timezone.utc):%m-%d %H:%M}")
        c = fetch_klines_range(sym, int(a * 1000) - 120_000, int(b * 1000) + 120_000)
        closes[sym] = sorted(c.items())

    lanes = defaultdict(lambda: {"n": 0, "right": 0, "up": 0})
    lane_tape = defaultdict(lambda: {"n": 0, "right": 0})  # (strategy,window,action,BULL/BEAR/NEU)
    scored = 0
    for r in recs:
        cs = closes.get(r["sym"])
        if not cs:
            continue
        p0 = price_at(cs, int(r["m_start"] * 1000))
        p1 = price_at(cs, int(r["m_end"] * 1000))
        if p0 is None or p1 is None or p0 == p1:
            continue
        up = p1 > p0
        # side: prefer explicit side, else action
        side = str(r.get("side") or "").upper()
        if side in ("LONG", "SHORT"):
            bet_up = side == "LONG"
        else:
            bet_up = r["action"] == "BUY_YES"
        right = (bet_up == up)
        k = (r["strategy"], r["window"], r["action"])
        lanes[k]["n"] += 1; lanes[k]["right"] += int(right); lanes[k]["up"] += int(up)
        tape = "BULL" if r.get("htf") == "BULLISH" else ("BEAR" if r.get("htf") == "BEARISH" else "NEU")
        tk = (r["strategy"], r["window"], r["action"], tape)
        lane_tape[tk]["n"] += 1; lane_tape[tk]["right"] += int(right)
        scored += 1

    # per-strategy roll-up from the per-lane counts
    strat_roll_map = defaultdict(lambda: {"n": 0, "right": 0})
    for k, L in lanes.items():
        strat_roll_map[k[0]]["n"] += L["n"]
        strat_roll_map[k[0]]["right"] += L["right"]

    print(f"\nscored vs Binance truth: {scored}\n")
    print("=== PER-STRATEGY resolver right-side% (direction quality, high-n) ===")
    for st, S in sorted(strat_roll_map.items(), key=lambda kv: -(kv[1]["right"]/max(kv[1]["n"],1))):
        print(f"  {st:14} n={S['n']:5}  right-side {S['right']/max(S['n'],1)*100:5.1f}%")

    print("\n=== PER-LANE (asset window side), n>=15 ===")
    print(f"{'LANE':40} {'n':>5} {'right-side%':>11} {'up-rate%':>9}")
    print("-"*70)
    for k, L in sorted(lanes.items(), key=lambda kv: -(kv[1]["right"]/max(kv[1]["n"],1))):
        if L["n"] < 15:
            continue
        rs = L["right"]/L["n"]*100
        ur = L["up"]/L["n"]*100
        tag = " DIR-GOOD" if rs>=55 else (" DIR-BAD" if rs<=45 else " coinflip")
        print(f"{str(k):40} {L['n']:5} {rs:9.1f}%  {ur:7.1f}%{tag}")
    tn=sum(L['n'] for L in lanes.values()); tr=sum(L['right'] for L in lanes.values())
    print(f"\nOVERALL resolver right-side%: {tr/max(tn,1)*100:.1f}%  (n={tn})")

    # BULL vs BEAR tape split — the operator's sit-out question: which lanes win in bull vs
    # should sit out bull (but stay active in bear to catch fades/reverts).
    print("\n=== BULL vs BEAR TAPE (per lane, right-side%); SIT-OUT-BULL = bull<50 & bear>=53 ===")
    print(f"{'LANE':38} {'BULL n/rs%':>14}   {'BEAR n/rs%':>14}   verdict")
    print("-"*90)
    lanes_seen = sorted({(s, w, a) for (s, w, a, t) in lane_tape})
    for (s, w, a) in lanes_seen:
        b = lane_tape.get((s, w, a, "BULL"), {"n": 0, "right": 0})
        r = lane_tape.get((s, w, a, "BEAR"), {"n": 0, "right": 0})
        if b["n"] + r["n"] < 20:
            continue
        brs = b["right"]/b["n"]*100 if b["n"] else float("nan")
        rrs = r["right"]/r["n"]*100 if r["n"] else float("nan")
        verdict = ""
        if b["n"] >= 10 and r["n"] >= 10:
            if brs < 50 and rrs >= 53:
                verdict = "SIT-OUT-BULL (active in bear)"
            elif brs >= 53 and rrs >= 53:
                verdict = "keep both"
            elif brs >= 53 and rrs < 50:
                verdict = "bull-only"
            elif brs < 50 and rrs < 50:
                verdict = "weak both"
        bs = f"{b['n']:3}/{brs:4.0f}%" if b["n"] else "  -/  - "
        rs2 = f"{r['n']:3}/{rrs:4.0f}%" if r["n"] else "  -/  - "
        print(f"{str((s, w, a)):38} {bs:>14}   {rs2:>14}   {verdict}")


if __name__ == "__main__":
    main()
