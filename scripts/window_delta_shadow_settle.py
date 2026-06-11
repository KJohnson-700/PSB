#!/usr/bin/env python3
"""Settle the window-delta shadow log against real Polymarket outcomes.

Closes the loop on the "window-delta as the PRIMARY 5m/15m entry signal" thesis.
An OFFLINE reconstruction is impossible (no sub-15-minute spot is retained), so
``sol_macro._shadow_log_window_delta`` logs, decision-neutrally, the window-delta
implied ``wd_prob`` (= P(up)) and the live ``yes_price`` for EVERY up/down
candidate at decision time. This script joins those rows to the settled outcome
and answers the only question that matters:

    When the window-delta signal BEAT the market price (by margin m), what was
    the realized win-rate and EV-per-$ of trading the side the tape pointed to?

For the chosen ``action`` the signal-vs-market edge is:
    BUY_YES (LONG):  wd_prob - yes_price          (tape P(up)  vs market P(up))
    BUY_NO  (SHORT): yes_price - wd_prob           (tape P(down) vs market P(down))

EV-per-$ uses the price actually paid for the chosen side:
    win  ->  (1 - paid) / paid
    loss ->  -1.0

Usage:
    python scripts/window_delta_shadow_settle.py
    python scripts/window_delta_shadow_settle.py --window 5m --min-n 20
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHADOW = REPO / "data" / "calibration" / "window_delta_shadow.jsonl"
RESOLUTION = REPO / "data" / "calibration" / "_market_resolution_cache.json"
SETTLED_GHOST = REPO / "data" / "calibration" / "rejected_candidates_settled.jsonl"

# Signal-vs-market edge buckets (how much the tape beat the market price by).
EDGE_BUCKETS = [
    (-1.0, 0.0, "tape WORSE than mkt"),
    (0.0, 0.05, "edge 0.00-0.05"),
    (0.05, 0.10, "edge 0.05-0.10"),
    (0.10, 0.20, "edge 0.10-0.20"),
    (0.20, 1.0, "edge 0.20+"),
]


def _load_resolutions(needed_ids: set) -> dict:
    """market_id -> "YES"/"NO". The bot's ``_market_resolution_cache.json`` can go
    stale (it has lagged ~22h), so the LIVE settled ghost log
    (``rejected_candidates_settled.jsonl``, refreshed continuously) is the primary
    source; the cache is a fast fallback for ids the ghost scan misses. Only ids in
    ``needed_ids`` (the shadow log's markets) are retained, so the 685MB ghost scan
    stays cheap on memory and can stop early once everything needed is found.
    """
    out: dict = {}
    if SETTLED_GHOST.exists() and needed_ids:
        with open(SETTLED_GHOST) as fh:
            for line in fh:
                # cheap pre-filter before json.loads on a huge file
                if '"outcome"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mid = str(d.get("market_id") or "")
                if mid not in needed_ids:
                    continue
                oc = d.get("outcome")
                if oc in ("YES", "NO"):
                    out[mid] = oc  # last write wins; outcome is market-level
                    if len(out) >= len(needed_ids):
                        break
    missing = needed_ids - out.keys()
    if missing and RESOLUTION.exists():
        with open(RESOLUTION) as fh:
            cache = json.load(fh)
        for mid in missing:
            oc = (cache.get(mid) or {}).get("outcome_won")
            if oc in ("YES", "NO"):
                out[mid] = oc
    return out


def _settle(row: dict, resolutions: dict):
    """Return (won: bool, ev_per_dollar: float, signal_edge: float) or None."""
    mid = str(row.get("market_id") or "")
    outcome = resolutions.get(mid)
    if outcome not in ("YES", "NO"):
        return None
    action = row.get("action")
    yes_price = row.get("yes_price")
    wd_prob = row.get("wd_prob")
    if action not in ("BUY_YES", "BUY_NO") or yes_price is None or wd_prob is None:
        return None
    yes_price = float(yes_price)
    if action == "BUY_YES":
        won = outcome == "YES"
        paid = yes_price
        signal_edge = wd_prob - yes_price
    else:  # BUY_NO
        won = outcome == "NO"
        paid = 1.0 - yes_price
        signal_edge = yes_price - wd_prob
    if paid <= 0.0 or paid >= 1.0:
        return None
    ev = (1.0 - paid) / paid if won else -1.0
    return won, ev, signal_edge


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default=None, help="filter to 5m/15m/1h")
    ap.add_argument("--strategy", default=None, help="filter to one strategy")
    ap.add_argument("--min-n", type=int, default=10)
    args = ap.parse_args()

    if not SHADOW.exists():
        print(f"No shadow log yet at {SHADOW} — start the bot (restart picks up "
              "the logger) and let 5m/15m candidates accumulate, then re-run.")
        return

    # Pass 1: load shadow rows (small) and collect the market ids we must resolve.
    rows = []
    needed_ids = set()
    with open(SHADOW) as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if args.window and row.get("window") != args.window:
                continue
            if args.strategy and row.get("strategy") != args.strategy:
                continue
            rows.append(row)
            mid = row.get("market_id")
            if mid is not None:
                needed_ids.add(str(mid))

    resolutions = _load_resolutions(needed_ids)
    by_edge = defaultdict(lambda: {"n": 0, "w": 0, "ev": 0.0})
    by_lane = defaultdict(lambda: {"n": 0, "w": 0, "ev": 0.0})
    total = settled = 0

    for row in rows:
            total += 1
            out = _settle(row, resolutions)
            if out is None:
                continue
            won, ev, edge = out
            settled += 1
            for lo, hi, label in EDGE_BUCKETS:
                if lo <= edge < hi:
                    g = by_edge[label]
                    g["n"] += 1
                    g["w"] += int(won)
                    g["ev"] += ev
                    break
            lane = (row.get("strategy"), row.get("window"), row.get("action"),
                    "FLIP" if row.get("flipped") else "native")
            g = by_lane[lane]
            g["n"] += 1
            g["w"] += int(won)
            g["ev"] += ev

    print(f"shadow rows={total}  settled={settled}  "
          f"(unsettled = market not yet resolved / not in cache)\n")
    if settled == 0:
        print("Nothing settled yet — let more windows resolve, then re-run.")
        return

    print("=== Realized outcome by SIGNAL-vs-MARKET edge (the thesis test) ===")
    print(f"{'bucket':22} {'n':>6} {'WR%':>6} {'EV/$':>8}")
    for _, _, label in EDGE_BUCKETS:
        g = by_edge[label]
        if g["n"] == 0:
            continue
        print(f"{label:22} {g['n']:>6} {100*g['w']/g['n']:>6.1f} "
              f"{g['ev']/g['n']:>+8.3f}")

    print(f"\n=== By lane (strategy, window, action, native/FLIP), n>={args.min_n} ===")
    print(f"{'lane':46} {'n':>6} {'WR%':>6} {'EV/$':>8}")
    for k in sorted(by_lane, key=lambda k: -by_lane[k]["n"]):
        g = by_lane[k]
        if g["n"] < args.min_n:
            continue
        print(f"{str(k):46} {g['n']:>6} {100*g['w']/g['n']:>6.1f} "
              f"{g['ev']/g['n']:>+8.3f}")


if __name__ == "__main__":
    main()
