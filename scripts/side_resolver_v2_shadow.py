#!/usr/bin/env python3
"""side_resolver_v2 SHADOW scorer — champion vs v2 right-side% on Binance truth. Observe-only.

Reads rejected_candidates.jsonl (every scanned candidate carries the champion's resolver_path, which
encodes native/htf/quant/momentum sides), runs side_resolver_v2.resolve_side_v2 on the same features,
and scores BOTH the champion side and the v2 side against the real Binance outcome over each market's
window (same method as clean_rightside_candidates.py). Prints overall + the DISAGREEMENT subset (where
v2 != champion) — that subset is the only place v2 can help or hurt.

LIMITATION (honest): the candidate log does NOT carry tape_dir / tape_adapter, so v2's top two owners
(lane_tape_adapter, tape_map) can't fire in this shadow — v2 here reduces to quant/fresh/fade/native/
sit_out precedence. It tests the single-owner + sit-out-on-htf-disagreement hypothesis, NOT the adapter
owners (those need a live per-candidate emit = phase 2). Reported n makes the scope explicit.

Usage: .venv/bin/python scripts/side_resolver_v2_shadow.py [--days N]
"""
import json, os, sys, time, urllib.request, re
from collections import defaultdict
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
from src.analysis.model_factory import _BINANCE_SYMBOL  # noqa: E402
from src.analysis.side_resolver_v2 import SideResolverFeatures, resolve_side_v2  # noqa: E402

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


def _side_word(path, key):
    m = re.search(rf"{key}_(long|short)", str(path or ""))
    return {"long": "LONG", "short": "SHORT"}.get(m.group(1)) if m else None


def fetch_range(symbol, a_ms, b_ms):
    out = {}
    cur = a_ms
    while cur < b_ms:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m"
               f"&startTime={cur}&endTime={b_ms}&limit=1000")
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
    days = 4
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
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
        w = str(d.get("window"))
        asset = _asset(d.get("strategy"))
        sym = _BINANCE_SYMBOL.get(asset)
        if not sym:
            continue
        path = d.get("resolver_path")
        champ = "LONG" if d.get("action") == "BUY_YES" else "SHORT"
        feats = SideResolverFeatures(
            strategy=d.get("strategy"), asset=asset, window=w, market_id=str(d.get("market_id")),
            native_side=champ,  # champion's output ~ native (native owns 88%)
            htf_bias=str(d.get("primary_htf_bias") or d.get("htf_bias") or "NEUTRAL").upper(),
            htf_side=_side_word(path, "htf"),
            quant_side=_side_word(path, "quant"),
            momentum_side=_side_word(path, "momentum"),
            fresh_cross_side="LONG" if "fresh" in str(path) and "long" in str(path) else (
                "SHORT" if "fresh" in str(path) and "short" in str(path) else None),
            champion_side=champ, champion_resolver_path=path,
        )
        v2 = resolve_side_v2(feats)
        m_start = end - WIN_MIN[w] * 60
        recs.append(dict(strategy=d.get("strategy"), window=w, sym=sym, m_start=m_start, m_end=end,
                         champ=champ, v2=v2.side, owner=v2.owner))
        s = span.setdefault(sym, [m_start, end])
        s[0] = min(s[0], m_start); s[1] = max(s[1], end)
    print(f"markets: {len(recs)}  (v2 owners active: adapter/tape OFF in shadow — no tape fields logged)")

    closes = {sym: fetch_range(sym, int(a * 1000) - 120_000, int(b * 1000) + 120_000)
              for sym, (a, b) in span.items()}

    champ_n = champ_r = v2_n = v2_r = 0
    dis_n = dis_champ_r = dis_v2_r = 0
    abstain = 0
    owner_ct = defaultdict(int)
    for r in recs:
        cs = closes.get(r["sym"])
        if not cs:
            continue
        p0 = price_at(cs, int(r["m_start"] * 1000)); p1 = price_at(cs, int(r["m_end"] * 1000))
        if p0 is None or p1 is None or p0 == p1:
            continue
        up = p1 > p0
        c_right = (r["champ"] == "LONG") == up
        champ_n += 1; champ_r += int(c_right)
        owner_ct[r["owner"]] += 1
        if r["v2"] is None:
            abstain += 1
        else:
            v_right = (r["v2"] == "LONG") == up
            v2_n += 1; v2_r += int(v_right)
        if r["v2"] is not None and r["v2"] != r["champ"]:
            dis_n += 1
            dis_champ_r += int(c_right)
            dis_v2_r += int((r["v2"] == "LONG") == up)

    print(f"\n=== CHAMPION vs v2 (Binance truth) ===")
    print(f"champion right-side: {champ_r/max(champ_n,1)*100:.1f}%  (n={champ_n})")
    print(f"v2 right-side (non-abstain): {v2_r/max(v2_n,1)*100:.1f}%  (n={v2_n}); abstained {abstain}")
    print(f"\nDISAGREEMENT subset (v2 != champion, the only place v2 matters): n={dis_n}")
    if dis_n:
        print(f"  champion right-side here: {dis_champ_r/dis_n*100:.1f}%")
        print(f"  v2       right-side here: {dis_v2_r/dis_n*100:.1f}%   ({'v2 BETTER' if dis_v2_r>dis_champ_r else 'v2 worse/equal'})")
    print(f"\nv2 owner distribution: {dict(sorted(owner_ct.items(), key=lambda x:-x[1]))}")


if __name__ == "__main__":
    main()
