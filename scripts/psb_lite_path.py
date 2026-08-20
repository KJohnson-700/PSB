#!/usr/bin/env python3
"""PSB-lite — PRICE-PATH RECORDER (build tick 4, observe-only).

Tick 3 showed the naive favorite band loses (-$6.69/trade) and that the 0.70 hard stop
is what makes it pay -- but that claim is ARITHMETIC, not measurement, because the poller
only recorded the entry quote. To measure it we need the price PATH between entry and
resolution: did the quote actually touch 0.70, and when?

This records, for every market that quotes into the favorite band, a time series of the
quote until the window closes. With that, the 0.70 stop can be replayed exactly:
  - did it trigger?  - at what quote?  - what did the market resolve to afterwards?

Writes data/calibration/psb_lite_paths.jsonl (one row per observation).
Trades nothing, touches no config.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "calibration", "psb_lite_paths.jsonl")
GAMMA = "https://gamma-api.polymarket.com/markets"

FAV_MIN, FAV_MAX = 0.80, 0.93
STOP = 0.70
NEAR_MINS = 8.0
POLL_S = 8.0
RUN_MINS = float(os.environ.get("PSB_PATH_RUN_MINS", "115"))
ASSETS = ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp",
          "hype", "hyperliquid", "dogecoin", "doge", "bnb")


def _get(url, params=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "psb-lite-path/1.0"})
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


def main():
    # tracked[(market_id, side)] = {entry_quote, min_quote, stopped, stop_ts, n_obs}
    tracked = {}
    t_end = time.time() + RUN_MINS * 60.0
    polls = 0
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
                mins_left = (end - now).total_seconds() / 60.0
            except Exception:
                continue
            if mins_left > NEAR_MINS or mins_left < -1:
                continue
            try:
                ya = float(m.get("bestAsk"))
                yb = float(m.get("bestBid"))
            except (TypeError, ValueError):
                continue
            for side, quote, mark in (("YES", ya, ya), ("NO", round(1.0 - yb, 4), round(1.0 - yb, 4))):
                key = f"{m.get('id')}|{side}"
                st = tracked.get(key)
                if st is None:
                    # only start tracking when it first quotes INTO the favorite band
                    if not (FAV_MIN <= quote <= FAV_MAX):
                        continue
                    st = tracked[key] = {
                        "market_id": m.get("id"), "side": side,
                        "question": (m.get("question") or "")[:70],
                        "entry_quote": quote, "entry_mins_left": round(mins_left, 2),
                        "min_quote": quote, "stopped": False, "stop_mins_left": None,
                        "n_obs": 0, "end_date": m.get("endDate"),
                    }
                    _append({"ts": now_iso, "kind": "path_open", **{k: st[k] for k in
                             ("market_id", "side", "question", "entry_quote", "entry_mins_left", "end_date")}})
                st["n_obs"] += 1
                if mark < st["min_quote"]:
                    st["min_quote"] = mark
                if (not st["stopped"]) and mark <= STOP:
                    st["stopped"] = True
                    st["stop_mins_left"] = round(mins_left, 2)
                    _append({"ts": now_iso, "kind": "stop_touch", "market_id": st["market_id"],
                             "side": side, "entry_quote": st["entry_quote"], "quote": mark,
                             "mins_left": round(mins_left, 2)})
                    print(f"STOP-TOUCH {side} entry {st['entry_quote']:.3f} -> {mark:.3f} "
                          f"({mins_left:.1f}min left) {st['question'][:38]}", flush=True)
                _append({"ts": now_iso, "kind": "obs", "market_id": st["market_id"],
                         "side": side, "quote": mark, "mins_left": round(mins_left, 2)})
        time.sleep(POLL_S)

    for key, st in tracked.items():
        _append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": "path_close",
                 **{k: st[k] for k in ("market_id", "side", "question", "entry_quote",
                                       "entry_mins_left", "min_quote", "stopped",
                                       "stop_mins_left", "n_obs", "end_date")}})
    n_stop = sum(1 for s in tracked.values() if s["stopped"])
    print(f"psb_lite_path: polls={polls} tracked={len(tracked)} stop_touched={n_stop}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
