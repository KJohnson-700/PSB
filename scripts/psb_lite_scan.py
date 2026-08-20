#!/usr/bin/env python3
"""PSB-lite — FAVORITE SCANNER (build tick 1, observe-only).

Spec: vault 2026-08-19-FRESH-BOT-SPEC-v0-favorite-first.md

The whole entry model, in one rule: on a crypto up/down binary, take the side whose
best-ask sits in [0.80, 0.93] and hold to resolution. No bias, no momentum, no AI,
no calibration. The price IS the model.

Why this rule (measured on the operator's OWN sessions, real closes only):
  winning sessions: 44% of entries were >= 0.80, up to $70.8 notional, WR 55-69%
  bleeding sessions: 9% favorites, max $25.3 notional, WR 31-41%
Why the 0.93 ceiling: fee = 0.07*p*(1-p) per share (crypto_fees_v2, taker-only,
verified live). At 0.93 the gross win is +7.5% and the fee 0.45% — above 0.93 the
payoff stops covering fee + slippage.

This script TRADES NOTHING. It scans, applies the rule, and appends what it WOULD
have taken to data/calibration/psb_lite_candidates.jsonl so the rule can be graded
against real Gamma resolutions before a single dollar (paper or otherwise) moves.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "calibration", "psb_lite_candidates.jsonl")

GAMMA = "https://gamma-api.polymarket.com/markets"
BOOK = "https://clob.polymarket.com/book"

FAV_MIN = 0.80          # below this the WR is not favorite-grade
FAV_MAX = 0.93          # above this the payoff stops covering fee + slippage
TAKER_RATE = 0.07       # crypto_fees_v2, verified live: fee = rate * p * (1-p)
NOTIONAL = 60.0         # winning-era scale (median $15 / mean $36 / max $71)
THROTTLE_S = 0.2
MAX_MARKETS = 150
ASSETS = ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp",
          "hype", "dogecoin", "doge", "bnb")


def _get(url, params=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "psb-lite-scan/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def best_ask(token_id):
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


def is_updown_crypto(m):
    q = (m.get("question") or "").lower()
    if "up or down" not in q:
        return False
    return any(a in q for a in ASSETS)


def scan():
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # 2026-08-19 QUERY BUG FOUND: {active,closed:false,order:endDate,ascending} returns
    # long-DEAD markets (endDate in 2025, bestAsk 1 / bestBid 0 / liquidity 0) that are
    # still flagged active. Must pin end_date_min to NOW or every scan reads dead books.
    # NOTE: scripts/bundle_arb_shadow.py has the SAME bug — its "no free money" result
    # was measured on dead books and is void until re-run.
    _now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        markets = _get(GAMMA, {"active": "true", "closed": "false",
                               "limit": MAX_MARKETS, "order": "endDate",
                               "ascending": "true", "end_date_min": _now_iso})
    except Exception as exc:
        print(f"psb_lite_scan: gamma error {exc}")
        return 1

    scanned = hits = 0
    rows = []
    for m in markets:
        if not is_updown_crypto(m):
            continue
        toks = m.get("clobTokenIds")
        if isinstance(toks, str):
            try:
                toks = json.loads(toks)
            except ValueError:
                continue
        if not toks or len(toks) != 2:
            continue
        scanned += 1
        # Gamma carries the live top-of-book (bestAsk/bestBid) and it agrees with the CLOB;
        # the per-token /book call fails often enough to silently drop candidates, so quote
        # from gamma and only reach for the book to size the fill.
        try:
            _yes_ask = float(m.get("bestAsk"))
            _yes_bid = float(m.get("bestBid"))
        except (TypeError, ValueError):
            continue
        _no_ask = round(1.0 - _yes_bid, 4)   # NO ask is the complement of the YES BID
        for side, tok, quote in (("YES", toks[0], _yes_ask), ("NO", toks[1], _no_ask)):
            if not (FAV_MIN <= quote <= FAV_MAX):
                continue
            ba = best_ask(tok)
            time.sleep(THROTTLE_S)
            price = quote
            size = ba[1] if ba else 0.0
            if ba and FAV_MIN <= ba[0] <= FAV_MAX:
                price = ba[0]
            if not (FAV_MIN <= price <= FAV_MAX):
                continue
            shares = NOTIONAL / price
            fillable = min(shares, size)
            fee = TAKER_RATE * price * (1.0 - price) * fillable
            gross_win = (1.0 - price) * fillable
            loss_if_wrong = price * fillable
            # breakeven WR for this exact entry, after fee
            be_wr = (loss_if_wrong + fee) / (gross_win + loss_if_wrong) if (gross_win + loss_if_wrong) else 1.0
            hits += 1
            rows.append({
                "ts": now, "kind": "candidate",
                "market_id": m.get("id"), "slug": m.get("slug"),
                "question": (m.get("question") or "")[:80],
                "end_date": m.get("endDate"),
                "side": side, "ask": price, "ask_size": size,
                "shares": round(fillable, 2),
                "notional": round(fillable * price, 2),
                "fee": round(fee, 3),
                "gross_win": round(gross_win, 2),
                "loss_if_wrong": round(loss_if_wrong, 2),
                "breakeven_wr": round(be_wr, 4),
            })
    rows.append({"ts": now, "kind": "scan_summary", "updown_scanned": scanned,
                 "candidates": hits})
    with open(OUT, "a") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    print(f"psb_lite_scan: up/down markets={scanned} favorite candidates={hits}")
    for r in rows:
        if r.get("kind") != "candidate":
            continue
        print(f"  {r['side']:3} @{r['ask']:.3f} x{r['shares']:6.1f}sh "
              f"(${r['notional']:.0f}) win +${r['gross_win']:.2f} / lose -${r['loss_if_wrong']:.2f} "
              f"fee ${r['fee']:.2f} | breakeven WR {r['breakeven_wr']*100:.1f}% | {r['question'][:46]}")
    return 0


if __name__ == "__main__":
    sys.exit(scan())
