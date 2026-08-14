#!/usr/bin/env python3
"""CLEAN RIGHT-SIDE LEDGER — the un-muddled per-lane direction truth (2026-08-06).

The operator's standing objection: realized pnl per lane is MUDDLED (exit-leak + sizing +
wrong-side all collapse into one number) and ghost data is UNRELIABLE. Both true. This script
answers the one question those can't, using the ONLY source that is neither:

  For each ENTRY, did the underlying (Binance) actually move the way we bet, over the market's
  EXACT resolution window?  right_side = our side == real outcome.

Exchange price truth. Not ghost. Not PM mid. Not where we exited. Not what we sized. It isolates
DIRECTION quality per (asset, window, side) so we can finally see which lanes pick the right way —
independent of the exit leak and the sizing.

Window reconstruction (from real fills, no ghost):
  market_end   = closed_at + secs_to_expiry_at_exit    (both in the trade record)
  market_start = market_end - window_minutes
  outcome UP   = binance_close[market_end] > binance_close[market_start]

Usage: .venv/bin/python scripts/clean_rightside_ledger.py [--all]   (default: today's sessions)
"""
import json, os, sys, time, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
from src.analysis.model_factory import _BINANCE_SYMBOL  # noqa: E402

TRADES = os.path.join(_REPO, "data/calibration/trades.jsonl")
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
    """1m closes over [start,end], as {minute_ms: close}. Paginated public Binance REST."""
    out = {}
    cur = start_ms
    while cur < end_ms:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m"
               f"&startTime={cur}&endTime={end_ms}&limit=1000")
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                kl = json.load(r)
        except Exception as e:
            print(f"  fetch err {symbol}: {e}", file=sys.stderr)
            break
        if not kl:
            break
        for k in kl:
            out[int(k[0])] = float(k[4])  # open_time -> close
        nxt = int(kl[-1][0]) + 60_000
        if nxt <= cur:
            break
        cur = nxt
        if len(kl) < 1000:
            break
        time.sleep(0.15)
    return out


def price_at(closes_sorted, ts_ms):
    """Last 1m close at or before ts_ms."""
    lo, hi, ans = 0, len(closes_sorted) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if closes_sorted[mid][0] <= ts_ms:
            ans = closes_sorted[mid][1]; lo = mid + 1
        else:
            hi = mid - 1
    return ans


def main():
    allrows = "--all" in sys.argv
    import glob
    today = set(os.path.basename(d) for d in glob.glob(os.path.join(_REPO, "data/paper_trades/test_20260805_*")) +
                glob.glob(os.path.join(_REPO, "data/paper_trades/test_20260806_*")))
    rows = []
    for l in open(TRADES):
        if not l.strip().startswith("{"):
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        if not allrows and str(d.get("session_id")) not in today:
            continue
        rows.append(d)
    print(f"trades loaded: {len(rows)} ({'ALL' if allrows else 'today'})")

    # reconstruct each window; group time span per symbol for batched fetch
    recs = []
    span = {}
    for d in rows:
        w = str(d.get("window"))
        if w not in WIN_MIN:
            continue
        end = _iso(d.get("closed_at"))
        sxe = d.get("secs_to_expiry_at_exit")
        if end is None or sxe is None:
            continue
        m_end = end + float(sxe)
        m_start = m_end - WIN_MIN[w] * 60
        asset = _asset(d.get("strategy"))
        sym = _BINANCE_SYMBOL.get(asset)
        if not sym:
            continue
        rec = dict(strategy=d.get("strategy"), window=w, action=d.get("action"),
                   sym=sym, m_start=m_start, m_end=m_end, pnl=d.get("pnl") or 0.0)
        recs.append(rec)
        s = span.setdefault(sym, [m_start, m_end])
        s[0] = min(s[0], m_start); s[1] = max(s[1], m_end)

    # fetch klines per symbol once over its full span
    closes = {}
    for sym, (a, b) in span.items():
        print(f"  fetching {sym} {datetime.fromtimestamp(a,timezone.utc):%m-%d %H:%M}..{datetime.fromtimestamp(b,timezone.utc):%H:%M}")
        c = fetch_klines_range(sym, int(a * 1000) - 120_000, int(b * 1000) + 120_000)
        closes[sym] = sorted(c.items())

    # score each rec against Binance truth
    lanes = defaultdict(lambda: {"n": 0, "right": 0, "pnl": 0.0, "up": 0})
    scored = 0
    for r in recs:
        cs = closes.get(r["sym"])
        if not cs:
            continue
        p0 = price_at(cs, int(r["m_start"] * 1000))
        p1 = price_at(cs, int(r["m_end"] * 1000))
        if p0 is None or p1 is None or p0 == p1:
            continue
        outcome_up = p1 > p0
        bet_up = r["action"] == "BUY_YES"
        right = (bet_up == outcome_up)
        k = (r["strategy"], r["window"], r["action"])
        L = lanes[k]
        L["n"] += 1; L["right"] += int(right); L["pnl"] += r["pnl"]; L["up"] += int(outcome_up)
        scored += 1

    print(f"scored against Binance truth: {scored}\n")
    print(f"{'LANE (asset window side)':40} {'n':>3} {'RIGHT-SIDE%':>11} {'net$':>8} {'verdict'}")
    print("-" * 92)
    tot_n = tot_right = 0
    for k, L in sorted(lanes.items(), key=lambda kv: -(kv[1]["right"] / max(kv[1]["n"], 1))):
        n, rs = L["n"], L["right"] / max(L["n"], 1) * 100
        tot_n += n; tot_right += L["right"]
        # DIRECTION verdict is from right-side% ONLY (pnl shown for contrast = the muddle)
        v = "DIR-GOOD" if rs >= 55 else ("DIR-BAD" if rs <= 45 else "coinflip")
        contrast = ""
        if rs >= 55 and L["pnl"] < 0:
            contrast = "  <-- right side, LOSES money = EXIT/SIZING leak"
        if rs <= 45 and L["pnl"] > 0:
            contrast = "  <-- wrong side, MAKES money = exit got lucky"
        print(f"{str(k):40} {n:3} {rs:9.1f}%  {L['pnl']:+8.2f}  {v}{contrast}")
    if tot_n:
        print(f"\nOVERALL right-side%: {tot_right/tot_n*100:.1f}%  (n={tot_n})  <- the headline: is the bot picking the right direction at all?")


if __name__ == "__main__":
    main()
