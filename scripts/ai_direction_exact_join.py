#!/usr/bin/env python
"""
EXACT market-id join between AI direction-provider calls and Polymarket resolutions.

Purpose
-------
An earlier *approximate* join (match each provider call to the NEXT settled market
for that asset whose resolution timestamp fell inside the call horizon +15m slack)
produced a striking asymmetry: every provider looked ~60% right when it said DOWN
and ~40% right when it said UP.

Two reviewers argued the join itself could manufacture that asymmetry via
  (a) regime-conditioned call cadence,
  (b) market-vintage mismatch between the UP-call and DOWN-call populations,
  (c) resolution-oracle aliasing.

This script replaces the approximate join with an EXACT one:

  * every provider call (asset, horizon, ts) is matched to the *specific*
    Polymarket market_id whose trading window CONTAINS the call timestamp, where
    the window boundaries are parsed from the market_question text
    ("Bitcoin Up or Down - August 8, 8:15PM-8:30PM ET"), and
  * the match is only accepted if the live scanner actually logged a ghost row
    for that market_id within +/- SLACK seconds of the call (proof the market was
    genuinely open and being scanned at that instant).

Unmatched calls are counted and reported, never silently dropped.

Outputs: a text report on stdout (and optionally --out <file>).

READ-ONLY: touches nothing but the calibration logs.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHADOW = os.path.join(REPO, "data/calibration/ai_direction_shadow.jsonl")
SETTLED = os.path.join(REPO, "data/calibration/rejected_candidates_settled.jsonl")

HORIZON_WINDOW = {5: "5m", 15: "15m", 60: "1h"}

# ---------------------------------------------------------------- time helpers

def _et_offset(dt_naive: datetime) -> timedelta:
    """US Eastern offset. Aug 2026 is EDT (-4). Handles DST crudely but the data
    window here is entirely inside DST; we still compute properly for safety."""
    y = dt_naive.year
    # DST: 2nd Sunday March 2am -> 1st Sunday Nov 2am
    def nth_sunday(month, n):
        d = datetime(y, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)  # first Sunday
        return d + timedelta(days=7 * (n - 1))
    start = nth_sunday(3, 2) + timedelta(hours=2)
    end = nth_sunday(11, 1) + timedelta(hours=2)
    return timedelta(hours=-4) if start <= dt_naive < end else timedelta(hours=-5)


MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}

# "Bitcoin Up or Down - August 8, 8:15PM-8:30PM ET"
RANGE_RE = re.compile(
    r"-\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{1,2})(?::(\d{2}))?\s*([AP]M)\s*-\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)\s*ET")
# "XRP Up or Down - August 8, 5AM ET"   (hourly market)
HOUR_RE = re.compile(
    r"-\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{1,2})(?::(\d{2}))?\s*([AP]M)\s*ET\s*$")


def _mk(year, month, day, hour12, minute, ampm):
    h = hour12 % 12
    if ampm == "PM":
        h += 12
    return datetime(year, month, day, h, minute)


def parse_window(question: str, year: int = 2026):
    """Return (start_epoch, end_epoch) UTC for a market question, or None."""
    if not question:
        return None
    m = RANGE_RE.search(question)
    if m:
        mon, day, h1, m1, ap1, h2, m2, ap2 = m.groups()
        month = MONTHS.get(mon)
        if not month:
            return None
        s = _mk(year, month, int(day), int(h1), int(m1 or 0), ap1)
        e = _mk(year, month, int(day), int(h2), int(m2 or 0), ap2)
        if e <= s:  # crosses midnight
            e += timedelta(days=1)
        off = _et_offset(s)
        return ((s - off).replace(tzinfo=timezone.utc).timestamp(),
                (e - off).replace(tzinfo=timezone.utc).timestamp())
    m = HOUR_RE.search(question)
    if m:
        mon, day, h1, m1, ap1 = m.groups()
        month = MONTHS.get(mon)
        if not month:
            return None
        s = _mk(year, month, int(day), int(h1), int(m1 or 0), ap1)
        e = s + timedelta(hours=1)
        off = _et_offset(s)
        return ((s - off).replace(tzinfo=timezone.utc).timestamp(),
                (e - off).replace(tzinfo=timezone.utc).timestamp())
    return None


def iso_epoch(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------- shadow calls

def load_calls(path=SHADOW):
    calls = []
    bad = 0
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            bad += 1
            continue
        ts = d.get("ts")
        asset = d.get("asset")
        hz = d.get("horizon_min")
        if ts is None or not asset or hz is None:
            bad += 1
            continue
        feats = d.get("features") or {}
        dec = d.get("decisions")
        rows = []
        if isinstance(dec, dict) and dec:
            for prov, v in dec.items():
                v = v or {}
                rows.append((prov, v.get("dir"), v.get("conf"), v.get("error")))
        else:  # legacy single-provider schema
            rows.append(("legacy", d.get("ai_dir"), d.get("ai_conf"), d.get("ai_error")))
        # tape_dir is a non-LLM baseline logged alongside; keep it as a pseudo-provider
        if d.get("tape_dir"):
            rows.append(("__tape_baseline", d.get("tape_dir"), d.get("tape_conf"), None))
        for prov, dr, conf, err in rows:
            if not dr:
                continue
            calls.append(dict(ts=float(ts), asset=asset, horizon=int(hz),
                              provider=prov, dir=str(dr).upper(),
                              conf=conf, error=err,
                              rsi=feats.get("rsi_14"), vol=feats.get("vol_pct"),
                              trend=feats.get("trend_dir_label"),
                              price=d.get("price")))
    return calls, bad


# ------------------------------------------------------- settled market tables

def build_markets(calls, cache_path, slack_pad=7200, force=False):
    """Stream the (large) settled ghost log once and build:
        meta[market_id] = dict(strategy, window, question, outcome, start, end)
        obs[market_id]  = sorted list of (ts, yes_price)
    Restricted to the time span covered by the provider calls (+/- pad)."""
    if cache_path and os.path.exists(cache_path) and not force:
        with open(cache_path, "rb") as fh:
            return pickle.load(fh)

    lo = min(c["ts"] for c in calls) - slack_pad
    hi = max(c["ts"] for c in calls) + slack_pad

    meta = {}
    obs = defaultdict(list)
    outcome_conflicts = 0
    n = 0
    for line in open(SETTLED):
        n += 1
        # cheap prefilter: the row ts is near the front of every record
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = iso_epoch(d.get("ts"))
        if ts is None or ts < lo or ts > hi:
            continue
        mid = d.get("market_id")
        if not mid:
            continue
        yp = d.get("yes_price")
        obs[mid].append((ts, yp))
        m = meta.get(mid)
        if m is None:
            w = parse_window(d.get("market_question") or "")
            meta[mid] = dict(strategy=d.get("strategy"), window=d.get("window"),
                             question=d.get("market_question"),
                             outcome=d.get("outcome"),
                             settled_at=iso_epoch(d.get("settled_at")),
                             start=(w[0] if w else None), end=(w[1] if w else None))
        else:
            if d.get("outcome") and m["outcome"] and d["outcome"] != m["outcome"]:
                outcome_conflicts += 1
    for mid in obs:
        obs[mid].sort()
    out = dict(meta=meta, obs=dict(obs), n_rows_scanned=n,
               outcome_conflicts=outcome_conflicts, lo=lo, hi=hi)
    if cache_path:
        with open(cache_path, "wb") as fh:
            pickle.dump(out, fh, protocol=4)
    return out


# ------------------------------------------------------------------ the join

def build_index(meta, obs):
    """(strategy, window) -> sorted list of (start, end, market_id)"""
    idx = defaultdict(list)
    for mid, m in meta.items():
        if m["start"] is None or not m["strategy"] or not m["window"]:
            continue
        idx[(m["strategy"], m["window"])].append((m["start"], m["end"], mid))
    for k in idx:
        idx[k].sort()
    return idx


def nearest_obs(series, ts, slack):
    """series: sorted [(ts, yes_price)]. Return (dt, yes_price) of nearest obs."""
    if not series:
        return None
    times = [t for t, _ in series]
    i = bisect.bisect_left(times, ts)
    best = None
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(series):
            dt = abs(series[j][0] - ts)
            if best is None or dt < best[0]:
                best = (dt, series[j][1])
    if best is None or best[0] > slack:
        return None
    return best


def join(calls, meta, obs, idx, slack=120, mode="containing"):
    """mode: 'containing' = the market whose window contains the call ts
             'next'       = the next market to OPEN after the call ts"""
    matched, excl = [], Counter()
    excl_detail = Counter()

    def drop(c, why):
        excl[why] += 1
        excl_detail[(c["provider"], c["dir"], why)] += 1

    for c in calls:
        w = HORIZON_WINDOW.get(c["horizon"])
        if w is None:
            drop(c, "no_window_for_horizon")
            continue
        lst = idx.get((c["asset"], w))
        if not lst:
            drop(c, "no_markets_for_asset_window")
            continue
        ts = c["ts"]
        cands = []
        if mode == "containing":
            # windows are non-overlapping per (asset,window); linear-ish scan via bisect
            starts = [s for s, _, _ in lst]
            i = bisect.bisect_right(starts, ts) - 1
            for j in (i - 1, i, i + 1):
                if 0 <= j < len(lst):
                    s, e, mid = lst[j]
                    if s <= ts < e:
                        cands.append(mid)
        else:
            starts = [s for s, _, _ in lst]
            i = bisect.bisect_right(starts, ts)
            if i < len(lst):
                cands.append(lst[i][2])
        if not cands:
            drop(c, "no_open_market_at_call_ts")
            continue
        if len(cands) > 1:
            drop(c, "ambiguous_multiple_open_markets")
            continue
        mid = cands[0]
        m = meta[mid]
        if m["outcome"] not in ("YES", "NO"):
            drop(c, "market_not_settled")
            continue
        no = nearest_obs(obs.get(mid, []), ts, slack)
        if no is None:
            drop(c, "no_scanner_obs_within_slack")
            continue
        dt_obs, yes_price = no
        matched.append(dict(c, market_id=mid, outcome=m["outcome"],
                            mkt_start=m["start"], mkt_end=m["end"],
                            question=m["question"], yes_price=yes_price,
                            obs_dt=dt_obs,
                            elapsed=ts - m["start"], remaining=m["end"] - ts,
                            frac=(ts - m["start"]) / max(1.0, m["end"] - m["start"])))
    return matched, excl, excl_detail


# ------------------------------------------------------------------- stats

def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"),) * 3
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, c - h, c + h


def acc_row(rows, direction):
    sub = [r for r in rows if r["dir"] == direction]
    n = len(sub)
    k = sum(1 for r in sub if (r["outcome"] == "YES") == (direction == "UP"))
    return k, n, wilson(k, n)


def two_prop_z(k1, n1, k2, n2):
    if n1 == 0 or n2 == 0:
        return float("nan")
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (p1 - p2) / se if se > 0 else float("nan")


def fmt_acc(k, n, ci):
    if n == 0:
        return "     n=0        "
    p, lo, hi = ci
    return f"{100*p:5.1f}% [{100*lo:4.1f},{100*hi:4.1f}] n={n}"


# ------------------------------------------------------------------ EV model

FEE = 0.07          # crypto up/down TAKER fee_rate, per src/execution/fill_sim.py


def ev_policies(rows, spread=0.015, fee=FEE, stake=1.0):
    """Return per-policy net EV/trade, using the repo's OWN fee formula
    (src/execution/fill_sim.py :: polymarket_taker_fee_usdc):

        fee_usdc = shares * fee_rate * p * (1 - p)

    With shares = stake/q this is exactly  fee = stake * 0.07 * (1 - q)  per $1
    staked -- i.e. 3.5c per $1 at q=0.50, 5.6c at q=0.20, 0.7c at q=0.90.
    Taker fee is charged on the entry fill only; settlement is free.
    Entry cost adds `spread` to the quoted price (crossing the book).
    Policies:
      straight : trade the called direction
      discard  : trade only DOWN calls (skip UP)
      fade     : trade DOWN calls straight, and UP calls inverted (buy NO)
    """
    def one(direction_to_trade, r):
        # direction_to_trade: 'UP' -> buy YES, 'DOWN' -> buy NO
        yp = r["yes_price"]
        if yp is None:
            return None
        if direction_to_trade == "UP":
            q = min(0.99, max(0.01, yp + spread))
            win = (r["outcome"] == "YES")
        else:
            q = min(0.99, max(0.01, (1.0 - yp) + spread))
            win = (r["outcome"] == "NO")
        shares = stake / q
        fee_usdc = shares * fee * q * (1.0 - q)
        if win:
            return shares * 1.0 - stake - fee_usdc
        return -stake - fee_usdc

    out = {}
    for pol in ("straight", "discard", "fade"):
        pnl, n, wins = 0.0, 0, 0
        for r in rows:
            d = r["dir"]
            if d not in ("UP", "DOWN"):
                continue
            if pol == "straight":
                trade = d                       # follow the call
            elif pol == "discard":
                if d == "UP":
                    continue                    # skip UP calls entirely
                trade = d
            else:                               # fade: invert UP calls only
                trade = "DOWN" if d == "UP" else d
            v = one(trade, r)
            if v is None:
                continue
            pnl += v
            n += 1
            wins += 1 if v > 0 else 0
        out[pol] = dict(n=n, ev=(pnl / n if n else float("nan")),
                        total=pnl, wr=(wins / n if n else float("nan")))
    return out


# --------------------------------------------------------------------- report

def hour_pt(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(
        timezone(timedelta(hours=-7))).hour


def price_bucket(p):
    if p is None:
        return "na"
    for hi in (0.2, 0.35, 0.45, 0.55, 0.65, 0.8):
        if p < hi:
            return f"<{hi}"
    return ">=0.8"


def report(matched, excl, calls, out=sys.stdout, excl_detail=None,
           meta=None, obs=None, idx=None):
    P = lambda *a: print(*a, file=out)
    providers = sorted({r["provider"] for r in matched})

    P("=" * 100)
    P("EXACT MARKET-ID JOIN — AI DIRECTION PROVIDERS vs POLYMARKET RESOLUTION")
    P("=" * 100)
    P(f"provider-calls in shadow log : {len(calls)}")
    P(f"matched to an exact market_id: {len(matched)}  "
      f"({100*len(matched)/max(1,len(calls)):.1f}%)")
    P("exclusions:")
    for k, v in excl.most_common():
        P(f"   {k:38s} {v:7d}  ({100*v/max(1,len(calls)):.1f}%)")

    # base rate
    mk = {}
    for r in matched:
        mk[r["market_id"]] = r["outcome"]
    up = sum(1 for v in mk.values() if v == "YES")
    P(f"\nrealized base rate over {len(mk)} DISTINCT matched markets: "
      f"UP {100*up/max(1,len(mk)):.1f}% / DOWN {100*(len(mk)-up)/max(1,len(mk)):.1f}%")

    P("\n" + "-" * 100)
    P("ACCURACY BY PROVIDER x CALLED DIRECTION  (95% Wilson CI)")
    P("-" * 100)
    P(f"{'provider':16s} {'said DOWN':32s} {'said UP':32s} {'UP-call rate':>13s} {'z(DOWN-UP)':>11s}")
    for p in providers:
        rows = [r for r in matched if r["provider"] == p]
        kd, nd, cid = acc_row(rows, "DOWN")
        ku, nu, ciu = acc_row(rows, "UP")
        nud = nu + nd
        z = two_prop_z(kd, nd, ku, nu)
        P(f"{p:16s} {fmt_acc(kd,nd,cid):32s} {fmt_acc(ku,nu,ciu):32s} "
          f"{(100*nu/nud if nud else float('nan')):12.1f}% {z:11.2f}")

    # FLAT accounting
    P("\nFLAT / other calls (not scored):")
    c = Counter((r["provider"], r["dir"]) for r in matched if r["dir"] not in ("UP", "DOWN"))
    for k, v in sorted(c.items()):
        P(f"   {k[0]:16s} {k[1]:8s} {v}")

    # strata
    def strat_block(title, keyfn, minn=60):
        P("\n" + "-" * 100)
        P(f"STRATIFIED: {title}")
        P("-" * 100)
        P(f"{'provider':14s} {'stratum':14s} {'DOWN acc':30s} {'UP acc':30s} {'gap':>7s}")
        for p in providers:
            rows = [r for r in matched if r["provider"] == p]
            keys = sorted({keyfn(r) for r in rows}, key=lambda x: (str(type(x)), x))
            for k in keys:
                sub = [r for r in rows if keyfn(r) == k]
                kd, nd, cid = acc_row(sub, "DOWN")
                ku, nu, ciu = acc_row(sub, "UP")
                if nd + nu < minn:
                    continue
                gap = (kd / nd - ku / nu) if nd and nu else float("nan")
                P(f"{p:14s} {str(k):14s} {fmt_acc(kd,nd,cid):30s} "
                  f"{fmt_acc(ku,nu,ciu):30s} {100*gap:6.1f}")

    strat_block("ASSET", lambda r: r["asset"])
    strat_block("HORIZON (min)", lambda r: r["horizon"])
    strat_block("ENTRY YES-PRICE BUCKET", lambda r: price_bucket(r["yes_price"]))
    strat_block("HOUR OF DAY (PT)", lambda r: hour_pt(r["ts"]), minn=40)
    strat_block("POSITION IN WINDOW (elapsed frac)",
                lambda r: f"frac{min(4,int(r['frac']*5))}")

    # reviewer alternative explanations
    P("\n" + "=" * 100)
    P("REVIEWER ALTERNATIVES: is the UP-call population a DIFFERENT population?")
    P("=" * 100)
    for p in providers:
        rows = [r for r in matched if r["provider"] == p and r["dir"] in ("UP", "DOWN")]
        if len(rows) < 200:
            continue
        P(f"\n{p}:")
        for d in ("DOWN", "UP"):
            sub = [r for r in rows if r["dir"] == d]
            if not sub:
                continue
            import statistics as st
            yp = [r["yes_price"] for r in sub if r["yes_price"] is not None]
            fr = [r["frac"] for r in sub]
            el = [r["elapsed"] for r in sub]
            ts = [r["ts"] for r in sub]
            P(f"  {d:5s} n={len(sub):6d}  yes_price med={st.median(yp) if yp else float('nan'):.3f} "
              f"mean={st.mean(yp) if yp else float('nan'):.3f} | "
              f"window-frac med={st.median(fr):.2f} | elapsed_s med={st.median(el):6.0f} | "
              f"call-date med={datetime.fromtimestamp(st.median(ts), timezone.utc).strftime('%m-%d %Hh')}")
        # vintage: are UP and DOWN calls drawn from the same days?
        byday = defaultdict(lambda: [0, 0])
        for r in rows:
            d = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%m-%d")
            byday[d][0 if r["dir"] == "UP" else 1] += 1
        P("   per-day UP/DOWN call counts: " +
          " ".join(f"{k}:{v[0]}/{v[1]}" for k, v in sorted(byday.items())))

    # CONTROL: within-market matched comparison — same market, both call dirs
    P("\n" + "-" * 100)
    P("CONTROL A: restrict to markets where the SAME provider made both an UP and a")
    P("DOWN call at some point in the window (removes market-vintage/selection).")
    P("-" * 100)
    for p in providers:
        rows = [r for r in matched if r["provider"] == p and r["dir"] in ("UP", "DOWN")]
        seen = defaultdict(set)
        for r in rows:
            seen[r["market_id"]].add(r["dir"])
        both = {m for m, s in seen.items() if len(s) == 2}
        sub = [r for r in rows if r["market_id"] in both]
        kd, nd, cid = acc_row(sub, "DOWN")
        ku, nu, ciu = acc_row(sub, "UP")
        P(f"{p:16s} markets={len(both):5d}  DOWN {fmt_acc(kd,nd,cid):30s} UP {fmt_acc(ku,nu,ciu)}")

    # CONTROL B: one call per market (first call) — removes cadence weighting
    P("\n" + "-" * 100)
    P("CONTROL B: ONE call per (provider, market_id) — the FIRST call. Removes")
    P("cadence weighting (a provider polling a market 30x inflates that market).")
    P("-" * 100)
    for p in providers:
        rows = sorted([r for r in matched if r["provider"] == p and r["dir"] in ("UP", "DOWN")],
                      key=lambda r: r["ts"])
        first, seen = [], set()
        for r in rows:
            if r["market_id"] in seen:
                continue
            seen.add(r["market_id"])
            first.append(r)
        kd, nd, cid = acc_row(first, "DOWN")
        ku, nu, ciu = acc_row(first, "UP")
        z = two_prop_z(kd, nd, ku, nu)
        P(f"{p:16s} DOWN {fmt_acc(kd,nd,cid):30s} UP {fmt_acc(ku,nu,ciu):30s} z={z:.2f}")

    # CONTROL C: price-conditioned. Does the call add anything over the market price?
    P("\n" + "-" * 100)
    P("CONTROL C: vs the MARKET PRICE at call time. 'mkt' = accuracy of simply")
    P("following the quote (yes_price>0.5 -> UP). If the provider only tracks price,")
    P("its edge is zero.")
    P("-" * 100)
    for p in providers:
        rows = [r for r in matched if r["provider"] == p and r["dir"] in ("UP", "DOWN")
                and r["yes_price"] is not None]
        if len(rows) < 100:
            continue
        for band in ("<0.45", "0.45-0.55", ">0.55"):
            def inband(r):
                q = r["yes_price"]
                return (q < 0.45) if band == "<0.45" else (
                    0.45 <= q <= 0.55 if band == "0.45-0.55" else q > 0.55)
            sub = [r for r in rows if inband(r)]
            kd, nd, cid = acc_row(sub, "DOWN")
            ku, nu, ciu = acc_row(sub, "UP")
            if nd + nu < 50:
                continue
            mk_k = sum(1 for r in sub if (r["yes_price"] > 0.5) == (r["outcome"] == "YES"))
            P(f"{p:14s} q{band:10s} DOWN {fmt_acc(kd,nd,cid):30s} UP {fmt_acc(ku,nu,ciu):30s} "
              f"| price-follow {100*mk_k/max(1,len(sub)):5.1f}% n={len(sub)}")

    # CONTROL D: when the provider DISAGREES with the quote, who is right?
    P("\n" + "-" * 100)
    P("CONTROL D: the only place a provider can add value — calls that DISAGREE with")
    P("the quote at call time (provider says UP while q<0.5, or DOWN while q>0.5).")
    P("Provider-right% below 50 means the quote beats the model.")
    P("-" * 100)
    P(f"{'provider':16s} {'disagree n':>11s} {'provider right':>28s} {'agree n':>9s} {'agree right':>26s}")
    for p in providers:
        rows = [r for r in matched if r["provider"] == p and r["dir"] in ("UP", "DOWN")
                and r["yes_price"] is not None and abs(r["yes_price"] - 0.5) > 1e-9]
        dis = [r for r in rows if (r["dir"] == "UP") != (r["yes_price"] > 0.5)]
        agr = [r for r in rows if (r["dir"] == "UP") == (r["yes_price"] > 0.5)]
        def rightness(sub):
            n = len(sub)
            k = sum(1 for r in sub if (r["outcome"] == "YES") == (r["dir"] == "UP"))
            return k, n, wilson(k, n)
        kd, nd, cd = rightness(dis)
        ka, na, ca = rightness(agr)
        P(f"{p:16s} {nd:11d} {fmt_acc(kd,nd,cd):28s} {na:9d} {fmt_acc(ka,na,ca)}")

    # CONTROL E: exclusion bias — are UP calls dropped at a different rate?
    if excl_detail:
        P("\n" + "-" * 100)
        P("CONTROL E: EXCLUSION BIAS. If the exact join drops UP and DOWN calls at")
        P("different rates, the surviving sample is selected. (matched / total per dir)")
        P("-" * 100)
        tot = Counter((c["provider"], c["dir"]) for c in calls)
        mat = Counter((r["provider"], r["dir"]) for r in matched)
        P(f"{'provider':16s} {'dir':6s} {'total':>8s} {'matched':>8s} {'match%':>8s}")
        for (p, d), t in sorted(tot.items()):
            if t < 50:
                continue
            P(f"{p:16s} {d:6s} {t:8d} {mat[(p,d)]:8d} {100*mat[(p,d)]/t:7.1f}%")

    # REPLICATION of the APPROXIMATE join that produced the disputed 60/40
    if meta is not None:
        P("\n" + "=" * 100)
        P("REPLICATION: the APPROXIMATE join (next settled market for the asset whose")
        P("RESOLUTION time falls inside the call horizon + 15m slack, ANY window size).")
        P("If this reproduces ~60/40 while the exact join does not, the join IS the")
        P("artifact.")
        P("=" * 100)
        ends = defaultdict(list)
        for mid, m in meta.items():
            if m["end"] is None or m["outcome"] not in ("YES", "NO"):
                continue
            ends[m["strategy"]].append((m["end"], mid))
        for k in ends:
            ends[k].sort()
        approx = []
        for c in calls:
            lst = ends.get(c["asset"])
            if not lst:
                continue
            ts = c["ts"]
            hi = ts + c["horizon"] * 60 + 15 * 60
            es = [e for e, _ in lst]
            i = bisect.bisect_left(es, ts)
            if i >= len(lst) or lst[i][0] > hi:
                continue
            approx.append(dict(c, market_id=lst[i][1], outcome=meta[lst[i][1]]["outcome"]))
        P(f"approx-join matched {len(approx)} / {len(calls)} calls "
          f"({100*len(approx)/max(1,len(calls)):.1f}%)")
        P(f"{'provider':16s} {'said DOWN':32s} {'said UP':32s} {'z':>8s}")
        for p in providers:
            rows = [r for r in approx if r["provider"] == p]
            kd, nd, cid = acc_row(rows, "DOWN")
            ku, nu, ciu = acc_row(rows, "UP")
            P(f"{p:16s} {fmt_acc(kd,nd,cid):32s} {fmt_acc(ku,nu,ciu):32s} "
              f"{two_prop_z(kd,nd,ku,nu):8.2f}")
        # how often does the approximate join pick a DIFFERENT market than exact?
        exact_by = {(r["provider"], r["ts"], r["asset"], r["horizon"]): r["market_id"]
                    for r in matched}
        same = diff = 0
        for r in approx:
            k = (r["provider"], r["ts"], r["asset"], r["horizon"])
            if k in exact_by:
                if exact_by[k] == r["market_id"]:
                    same += 1
                else:
                    diff += 1
        P(f"\nof calls matched by BOTH joins: same market_id {same}, "
          f"DIFFERENT market_id {diff} "
          f"({100*diff/max(1,same+diff):.1f}% of the overlap is a mismatched market)")

    # EV
    P("\n" + "=" * 100)
    P("PRICE-AWARE NET EV PER $1 STAKE  (buy-and-hold-to-resolution)")
    P("=" * 100)
    P("fee model = repo's own polymarket_taker_fee_usdc: shares*0.07*q*(1-q).")
    P("'gross' = zero spread, zero fee -> isolates raw signal from execution cost.")
    P(f"{'provider':16s} {'policy':16s} {'n':>7s} {'winrate':>9s} {'EV/$1':>9s} {'total$':>10s}")
    for p in providers:
        rows = [r for r in matched if r["provider"] == p]
        for label, spread, fee in (("gross", 0.0, 0.0), ("1c", 0.01, FEE), ("2c", 0.02, FEE)):
            pol = ev_policies(rows, spread=spread, fee=fee)
            for name in ("straight", "discard", "fade"):
                v = pol[name]
                P(f"{p:16s} {name+'@'+label:16s} {v['n']:7d} "
                  f"{100*v['wr']:8.1f}% {v['ev']:9.4f} {v['total']:10.1f}")
        P("")

    # EV restricted to the only band where the quote is uninformative
    P("-" * 100)
    P("EV restricted to the TOSS-UP band 0.45 <= yes_price <= 0.55 (the only band")
    P("where the quote itself carries no direction). If a model has real edge it")
    P("must show up here.")
    P("-" * 100)
    for p in providers:
        rows = [r for r in matched if r["provider"] == p and r["yes_price"] is not None
                and 0.45 <= r["yes_price"] <= 0.55]
        if len(rows) < 100:
            continue
        for label, spread, fee in (("gross", 0.0, 0.0), ("1c", 0.01, FEE)):
            pol = ev_policies(rows, spread=spread, fee=fee)
            for name in ("straight", "discard", "fade"):
                v = pol[name]
                P(f"{p:16s} {name+'@'+label:16s} {v['n']:7d} "
                  f"{100*v['wr']:8.1f}% {v['ev']:9.4f} {v['total']:10.1f}")
        P("")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slack", type=float, default=120.0,
                    help="max seconds between call ts and nearest scanner obs")
    ap.add_argument("--mode", default="containing", choices=["containing", "next"])
    ap.add_argument("--cache", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    calls, bad = load_calls()
    sys.stderr.write(f"loaded {len(calls)} provider-calls ({bad} bad lines)\n")
    cache = a.cache or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "psb_settled_markets.pkl")
    tbl = build_markets(calls, cache, force=a.force)
    sys.stderr.write(f"settled rows scanned={tbl['n_rows_scanned']} "
                     f"markets={len(tbl['meta'])} outcome_conflicts={tbl['outcome_conflicts']}\n")
    unparsed = sum(1 for m in tbl["meta"].values() if m["start"] is None)
    sys.stderr.write(f"markets with unparseable question window: {unparsed}\n")
    idx = build_index(tbl["meta"], tbl["obs"])
    matched, excl, excl_detail = join(calls, tbl["meta"], tbl["obs"], idx,
                                      slack=a.slack, mode=a.mode)
    sys.stderr.write(f"matched {len(matched)}\n")
    fh = open(a.out, "w") if a.out else sys.stdout
    report(matched, excl, calls, out=fh, excl_detail=excl_detail,
           meta=tbl["meta"], obs=tbl["obs"], idx=idx)
    if a.out:
        fh.close()


if __name__ == "__main__":
    main()
