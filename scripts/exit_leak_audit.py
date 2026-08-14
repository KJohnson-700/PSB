#!/usr/bin/env python3
"""EXIT LEAK AUDIT — the measurement that drives the exit fix (Codex 2026-08-06).

For every closed trade, compare what it REALIZED vs what it would have RESOLVED to (Binance truth over
the market's exact window). The leak = RIGHT direction at resolution but EXITED RED. Break it down by
exit_reason + lane + mfe (how far favorable it went before the exit killed it) so we can tell:
  - stop cut a right-direction trade that would've resolved a winner  -> hold-longer / wider stop
  - trade went far favorable (high mfe) then gave it all back           -> lock/trail the gain
vs correctly-lost (wrong direction) which the exit SHOULD cut.

No live change. Usage: .venv/bin/python scripts/exit_leak_audit.py [--days 3]
"""
import json, os, sys, time, urllib.request, math
from collections import defaultdict
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
from src.analysis.model_factory import _BINANCE_SYMBOL  # noqa: E402

TRADES = os.path.join(_REPO, "data/calibration/trades.jsonl")
WIN_SEC = {"5m": 300, "15m": 900, "1h": 3600}
FUTURES = {"HYPEUSDT"}


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


def main():
    days = 3
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    rows = []
    for l in open(TRADES):
        if not l.strip().startswith("{"):
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        w = str(d.get("window"))
        if w not in WIN_SEC:
            continue
        end = _iso(d.get("closed_at"))
        sxe = d.get("secs_to_expiry_at_exit")
        if end is not None and sxe is not None:
            m_end = end + float(sxe)
        else:
            op = _iso(d.get("opened_at"))
            if op is None:
                continue
            m_end = math.ceil(op / WIN_SEC[w]) * WIN_SEC[w]
        rows.append(dict(d, _m_end=m_end, _m_start=m_end - WIN_SEC[w]))
    if not rows:
        print("no trades"); return
    cutoff = max(r["_m_end"] for r in rows) - days * 86400
    rows = [r for r in rows if r["_m_end"] >= cutoff]
    span = {}
    for r in rows:
        sym = _BINANCE_SYMBOL.get(_asset(r.get("strategy")))
        if not sym:
            continue
        r["_sym"] = sym
        s = span.setdefault(sym, [r["_m_start"], r["_m_end"]])
        s[0] = min(s[0], r["_m_start"]); s[1] = max(s[1], r["_m_end"])
    closes = {sym: fetch_range(sym, int(a * 1000) - 120_000, int(b * 1000) + 120_000)
              for sym, (a, b) in span.items()}
    print(f"trades (last {days}d): {len(rows)}")

    cats = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    leak_by_reason = defaultdict(lambda: {"n": 0, "pnl": 0.0, "mfe": 0.0, "exitpct": 0.0})
    leak_by_lane = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    scored = 0
    for r in rows:
        cs = closes.get(r.get("_sym"))
        if not cs:
            continue
        p0 = price_at(cs, int(r["_m_start"] * 1000)); p1 = price_at(cs, int(r["_m_end"] * 1000))
        if p0 is None or p1 is None or p0 == p1:
            continue
        up = p1 > p0
        right = (r.get("action") == "BUY_YES") == up
        red = (r.get("pnl") or 0) < 0
        pnl = r.get("pnl") or 0
        cat = ("RIGHT_dir/RED (LEAK)" if (right and red) else
               "right/green (win)" if (right and not red) else
               "wrong/red (correct cut)" if (not right and red) else
               "wrong/green (lucky)")
        cats[cat]["n"] += 1; cats[cat]["pnl"] += pnl
        scored += 1
        if right and red:
            er = str(r.get("exit_reason") or "?")
            leak_by_reason[er]["n"] += 1; leak_by_reason[er]["pnl"] += pnl
            leak_by_reason[er]["mfe"] += abs(r.get("mfe_pct") or 0)
            leak_by_reason[er]["exitpct"] += abs(r.get("pnl_pct_at_exit") or 0)
            k = (r.get("strategy"), r.get("window"), r.get("action"))
            leak_by_lane[k]["n"] += 1; leak_by_lane[k]["pnl"] += pnl

    print(f"scored: {scored}\n")
    print("=== OUTCOME x EXIT categories ===")
    for c, v in sorted(cats.items(), key=lambda x: x[1]["pnl"]):
        print(f"  {c:26} n={v['n']:3}  pnl={v['pnl']:+8.2f}")
    leak_total = cats["RIGHT_dir/RED (LEAK)"]
    print(f"\n>>> THE LEAK (right direction, exited red): n={leak_total['n']}  ${leak_total['pnl']:+.2f} thrown away")
    print("\n=== leak by EXIT REASON (avg mfe = how far favorable before the exit killed it) ===")
    for er, v in sorted(leak_by_reason.items(), key=lambda x: x[1]["pnl"]):
        n = v["n"]
        print(f"  {er:26} n={n:3} ${v['pnl']:+8.2f}  avg_mfe={v['mfe']/n*100:5.1f}%  avg_exit={v['exitpct']/n*100:5.1f}%")
    print("\n=== leak by LANE ===")
    for k, v in sorted(leak_by_lane.items(), key=lambda x: x[1]["pnl"])[:12]:
        print(f"  {str(k):38} n={v['n']:2} ${v['pnl']:+7.2f}")
    print("\nREAD: high avg_mfe on a stop-reason leak = the trade went favorable then the stop cut it "
          "(hold-longer / trail). Low mfe = stopped early on noise before it resolved right (widen/defer stop).")


if __name__ == "__main__":
    main()
