#!/usr/bin/env python3
"""NEAR-RESOLUTION PROBE (observe-only) — the T-45s / T-60s question.

Operator's question: bots run BTC 5-minute up/down buying ~0.97-0.99 in the final seconds
and reportedly profit. This bot cannot: price_max 0.93 caps it, favorite_lane.windows
excludes 5m, and min_mins_left 3.0 refuses entry inside the last 3 minutes. So we have 13
lifetime trades above 0.95 and cannot answer whether the strategy is real FOR US.

The edge there is on the TIME axis, not the price axis: with seconds left the outcome is
nearly determined, and the fee collapses (0.07*p*(1-p) = 0.07% at p=0.99 vs 1.75% at 0.50).
The question is whether accuracy climbs FASTER than breakeven does as p rises.

This samples every live crypto up/down market and snapshots its quote at the T-60s and
T-45s marks, then a companion pass grades those snapshots against the REAL Gamma
resolution. Trades nothing, touches no config.

Writes data/calibration/near_resolution_probe.jsonl
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "calibration", "near_resolution_probe.jsonl")
GAMMA = "https://gamma-api.polymarket.com/markets"

MARKS = (60.0, 45.0)          # seconds-to-expiry snapshots the operator asked for
TOLERANCE = 6.0               # accept a sample within +/- this many seconds of a mark
POLL_S = 4.0
RUN_MINS = float(os.environ.get("PSB_NRP_RUN_MINS", "115"))
ASSETS = ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp",
          "hype", "hyperliquid", "dogecoin", "doge", "bnb")


def _get(url, params=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "psb-nrp/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _append(row):
    try:
        with open(OUT, "a") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def is_updown(m):
    q = (m.get("question") or "").lower()
    return "up or down" in q and any(a in q for a in ASSETS)


def window_of(q):
    q = (q or "").lower()
    if "-" in q and ("am-" in q or "pm-" in q):
        return "5m/15m"
    return "1h"


def main():
    taken = set()                 # (market_id, mark) already snapshotted
    t_end = time.time() + RUN_MINS * 60.0
    polls = snaps = 0
    while time.time() < t_end:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            markets = _get(GAMMA, {"active": "true", "closed": "false", "limit": 150,
                                   "order": "endDate", "ascending": "true",
                                   "end_date_min": now_iso})
        except Exception:
            time.sleep(POLL_S)
            continue
        polls += 1
        now = datetime.now(timezone.utc)
        for m in markets:
            if not is_updown(m):
                continue
            try:
                end = datetime.fromisoformat(str(m.get("endDate")).replace("Z", "+00:00"))
                secs_left = (end - now).total_seconds()
            except Exception:
                continue
            if secs_left < 0 or secs_left > MARKS[0] + TOLERANCE:
                continue
            try:
                yes_ask = float(m.get("bestAsk"))
                yes_bid = float(m.get("bestBid"))
            except (TypeError, ValueError):
                continue
            no_ask = round(1.0 - yes_bid, 4)
            for mark in MARKS:
                if abs(secs_left - mark) > TOLERANCE:
                    continue
                key = (m.get("id"), mark)
                if key in taken:
                    continue
                taken.add(key)
                snaps += 1
                # record BOTH sides; the "favorite" is whichever quotes higher
                fav_side, fav_quote = (("YES", yes_ask) if yes_ask >= no_ask else ("NO", no_ask))
                fee = 0.07 * fav_quote * (1 - fav_quote)
                _append({
                    "ts": now_iso, "kind": "snap",
                    "market_id": m.get("id"), "slug": m.get("slug"),
                    "question": (m.get("question") or "")[:80],
                    "end_date": m.get("endDate"),
                    "mark_secs": mark, "secs_left": round(secs_left, 1),
                    "window": window_of(m.get("question")),
                    "yes_ask": yes_ask, "yes_bid": yes_bid, "no_ask": no_ask,
                    "fav_side": fav_side, "fav_quote": fav_quote,
                    "fee_per_share": round(fee, 5),
                    "breakeven_wr": round(fav_quote + fee, 4),
                    "liquidity": m.get("liquidity"),
                })
                print(f"T-{int(mark)}s  {fav_side} @{fav_quote:.3f}  be={fav_quote+fee:.3f}  "
                      f"{(m.get('question') or '')[:44]}", flush=True)
        time.sleep(POLL_S)
    _append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "kind": "probe_summary", "polls": polls, "snaps": snaps})
    print(f"near_resolution_probe: polls={polls} snapshots={snaps}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
