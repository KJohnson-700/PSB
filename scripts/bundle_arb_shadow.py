#!/usr/bin/env python3
"""Bundle-arb SHADOW scanner (2026-08-19, operator GO) — observe-only, out-of-process.

Strategy candidate #1 from vault psb-new-research-2026-08: on a 2-outcome market,
buying BOTH sides for a combined cost < $1.00 locks the $1 resolution payout. The V2
threshold claim is combined < 0.9744 (1.56% resolution fee) — the fee number is
UNVERIFIED, so this logs GROSS edge and computes net under the claimed fee for later
grading; nothing trades off this file.

House method (shadow -> probe -> graduate): this is stage one. Every scan appends
observations to data/calibration/bundle_arb_shadow.jsonl:
  - every market whose best-ask YES + best-ask NO < LOG_BELOW (1.005) with fillable
    sizes, so we capture the near-misses too (the distribution matters, not just hits)
  - a per-scan summary row (markets scanned, hits, best combined seen)

Runs from the tooling daemon on a cadence. Public endpoints only (gamma + CLOB book),
throttled; no keys, no orders, no bot-process involvement.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "calibration", "bundle_arb_shadow.jsonl")

GAMMA = "https://gamma-api.polymarket.com/markets"
BOOK = "https://clob.polymarket.com/book"

# 2026-08-19 FEE MODEL VERIFIED against the live Gamma feeSchedule for crypto up/down
# (feeType crypto_fees_v2: rate 0.07, exponent 1, takerOnly, maker 0). The research's
# "1.56% resolution fee" claim is REFUTED for this class — the real bundle cost is the
# taker fee on BOTH legs at purchase: 0.07 * (p_yes*(1-p_yes) + p_no*(1-p_no)) per share
# (~3.5% near 50/50, tiny at the extremes).
TAKER_RATE = 0.07
CLAIMED_RESOLUTION_FEE = 0.0156   # kept for historical rows only; net math uses TAKER_RATE
LOG_BELOW = 1.005                 # log near-misses too; a hit is net_edge_claimed > 0
THROTTLE_S = 0.25
MAX_MARKETS = 120


def _get(url, params=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "psb-bundle-shadow/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def best_ask(token_id):
    """(price, size) of the cheapest ask, or None."""
    try:
        book = _get(BOOK, {"token_id": token_id})
    except Exception:
        return None
    asks = book.get("asks") or []
    try:
        lv = min(asks, key=lambda a: float(a["price"]))
        return float(lv["price"]), float(lv["size"])
    except (ValueError, TypeError, KeyError):
        return None


def scan():
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        markets = _get(GAMMA, {
            "active": "true", "closed": "false", "limit": MAX_MARKETS,
            # up/down crypto windows churn fastest; order by newest end date first
            "order": "endDate", "ascending": "true",
        })
    except Exception as exc:
        _append({"ts": now, "kind": "scan_error", "error": str(exc)[:200]})
        return 1

    scanned = hits = 0
    best_seen = None
    for m in markets:
        toks = m.get("clobTokenIds")
        if isinstance(toks, str):
            try:
                toks = json.loads(toks)
            except ValueError:
                continue
        if not toks or len(toks) != 2:
            continue
        yes = best_ask(toks[0])
        time.sleep(THROTTLE_S)
        no = best_ask(toks[1])
        time.sleep(THROTTLE_S)
        if yes is None or no is None:
            continue
        scanned += 1
        combined = yes[0] + no[0]
        if best_seen is None or combined < best_seen:
            best_seen = combined
        if combined < LOG_BELOW and yes[0] > 0.02 and no[0] > 0.02:
            fee = TAKER_RATE * (yes[0] * (1 - yes[0]) + no[0] * (1 - no[0]))
            net = 1.0 - combined - fee
            fillable = min(yes[1], no[1])
            hits += 1
            _append({
                "ts": now, "kind": "observation",
                "market_id": m.get("id"), "slug": m.get("slug"),
                "question": (m.get("question") or "")[:80],
                "end_date": m.get("endDate"),
                "yes_ask": yes[0], "no_ask": no[0], "combined": round(combined, 4),
                "yes_size": yes[1], "no_size": no[1], "fillable": fillable,
                "gross_edge": round(1.0 - combined, 4),
                "taker_fee_both_legs": round(fee, 4),
                "net_edge": round(net, 4),
                "expected_profit": round(fillable * net, 2),
            })
    _append({"ts": now, "kind": "scan_summary", "scanned": scanned, "hits": hits,
             "best_combined": round(best_seen, 4) if best_seen else None})
    print(f"bundle_arb_shadow: scanned={scanned} hits={hits} best={best_seen}")
    return 0


def _append(row):
    try:
        with open(OUT, "a") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(scan())
