#!/usr/bin/env python3
"""PSB-lite — NEAR-EXPIRY FAVORITE POLLER (build tick 2, observe-only).

Tick-1 finding that forced this file: the 0.80-0.93 favorite band only exists in the
final ~1-2 minutes of an up/down window. A 60s scan cycle structurally cannot see it.
So this polls fast and only where the band lives.

Every POLL_S seconds:
  - fetch live up/down markets (end_date_min pinned to now -- the un-pinned query
    returns DEAD 2025 markets, see tick-1 finding)
  - keep those with mins_left <= NEAR_MINS
  - log any side quoting inside [FAV_MIN, FAV_MAX] as a candidate, ONCE per
    (market_id, side) so a 10s cadence does not inflate n

Trades nothing. Writes data/calibration/psb_lite_candidates.jsonl. The companion
grader (psb_lite_grade.py) joins these to real Gamma resolutions -- the measured WR
of the band vs its breakeven WR is the single number the whole strategy lives on.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "calibration", "psb_lite_candidates.jsonl")
GAMMA = "https://gamma-api.polymarket.com/markets"

FAV_MIN, FAV_MAX = 0.80, 0.93
TAKER_RATE = 0.07
NOTIONAL = 60.0
NEAR_MINS = 6.0          # only poll windows this close to expiry
POLL_S = 10.0
RUN_MINS = float(os.environ.get("PSB_LITE_RUN_MINS", "115"))  # default just under 2h
ASSETS = ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp",
          "hype", "hyperliquid", "dogecoin", "doge", "bnb")


def _get(url, params=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "psb-lite-poll/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _append(row):
    try:
        with open(OUT, "a") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def is_updown_crypto(m):
    q = (m.get("question") or "").lower()
    return "up or down" in q and any(a in q for a in ASSETS)


def main():
    seen = set()
    t_end = time.time() + RUN_MINS * 60.0
    polls = cands = 0
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
            if not is_updown_crypto(m):
                continue
            try:
                end = datetime.fromisoformat(str(m.get("endDate")).replace("Z", "+00:00"))
                mins_left = (end - now).total_seconds() / 60.0
            except Exception:
                continue
            if mins_left > NEAR_MINS or mins_left < 0:
                continue
            try:
                yes_ask = float(m.get("bestAsk"))
                yes_bid = float(m.get("bestBid"))
            except (TypeError, ValueError):
                continue
            no_ask = round(1.0 - yes_bid, 4)
            for side, quote in (("YES", yes_ask), ("NO", no_ask)):
                if not (FAV_MIN <= quote <= FAV_MAX):
                    continue
                key = (m.get("id"), side)
                if key in seen:
                    continue
                seen.add(key)
                shares = NOTIONAL / quote
                fee = TAKER_RATE * quote * (1.0 - quote) * shares
                gross_win = (1.0 - quote) * shares
                loss_if_wrong = quote * shares
                be = (loss_if_wrong + fee) / (gross_win + loss_if_wrong)
                cands += 1
                _append({
                    "ts": now_iso, "kind": "candidate_v2",
                    "market_id": m.get("id"), "slug": m.get("slug"),
                    "question": (m.get("question") or "")[:80],
                    "end_date": m.get("endDate"), "mins_left": round(mins_left, 2),
                    "side": side, "quote": quote,
                    "yes_ask": yes_ask, "yes_bid": yes_bid,
                    "liquidity": m.get("liquidity"),
                    "shares": round(shares, 2), "notional": NOTIONAL,
                    "fee": round(fee, 3),
                    "gross_win": round(gross_win, 2),
                    "loss_if_wrong": round(loss_if_wrong, 2),
                    "breakeven_wr": round(be, 4),
                })
                print(f"CAND {side} @{quote:.3f} {mins_left:4.1f}min left | "
                      f"be_wr {be*100:.1f}% | {(m.get('question') or '')[:44]}", flush=True)
        time.sleep(POLL_S)
    _append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "kind": "poll_summary", "polls": polls, "candidates": cands,
             "run_mins": RUN_MINS})
    print(f"psb_lite_poller: polls={polls} candidates={cands}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
