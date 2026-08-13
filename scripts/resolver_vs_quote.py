#!/usr/bin/env python3
"""Does the RESOLVER beat the QUOTE alone?

The bot's whole direction apparatus exists to answer "which way in the next
5/15/60 min". This asks whether that apparatus adds anything over a strategy
that never looks at a single indicator and just reads the price.

Every strategy below is scored on the SAME rows (paired), against the SAME real
Polymarket resolutions, so the comparison is apples-to-apples:

  resolver    take the side the resolver actually chose
  favorite    always take the side the market already favours (yes_price>0.5 -> UP)
  underdog    always take the cheap side
  always_up / always_down / coinflip   dumb baselines

Scored two ways, because accuracy and money are not the same thing here:
  ACC     how often the side matched the real outcome
  EV/$1   realised edge per dollar staked at the price you'd actually have paid:
            right -> (1-p)/p     wrong -> -1        (p = that side's price)
          A strategy can be 78% accurate and still lose money if p is 0.85.

Cost is applied as a per-share haircut on the entry price (0c / 1c / 2c), the
same convention scripts/ai_direction_exact_join.py uses.

Usage:
  .venv/bin/python scripts/resolver_vs_quote.py [--bytes 400000000] [--band 0.45 0.55]
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GHOST = os.path.join(_REPO, "data/calibration/rejected_candidates_settled.jsonl")
LIVE = os.path.join(_REPO, "data/calibration/trades.jsonl")

UP_WORDS = {"LONG", "BUY_YES", "YES", "UP", "BULLISH"}
DOWN_WORDS = {"SHORT", "BUY_NO", "NO", "DOWN", "BEARISH"}


def norm_side(s):
    s = (s or "").upper()
    if s in UP_WORDS:
        return "UP"
    if s in DOWN_WORDS:
        return "DOWN"
    return None


def price_for(side, yes_price):
    """What you pay per share to take `side`."""
    return yes_price if side == "UP" else 1.0 - yes_price


class Book:
    """Accumulates one strategy's record over the shared row set."""

    __slots__ = ("n", "right", "up", "ev", "ev1", "ev2", "staked")

    def __init__(self):
        self.n = self.right = self.up = 0
        self.ev = self.ev1 = self.ev2 = 0.0

    def add(self, side, truth, yes_price):
        if side is None:
            return
        p = price_for(side, yes_price)
        if not (0.01 <= p <= 0.99):      # unpriced / already-resolved rows carry no decision
            return
        won = side == truth
        self.n += 1
        self.right += won
        self.up += side == "UP"
        for cost, attr in ((0.0, "ev"), (0.01, "ev1"), (0.02, "ev2")):
            pc = min(0.99, p + cost)
            setattr(self, attr, getattr(self, attr) + ((1.0 - pc) / pc if won else -1.0))

    def row(self, name):
        n = self.n or 1
        acc = 100.0 * self.right / n
        # Wald 95% CI on accuracy — with n in the 100k's the interval is what tells
        # you whether a 1.7pt gap is a finding or a rounding error.
        se = 100.0 * math.sqrt(max(acc / 100.0 * (1 - acc / 100.0), 1e-12) / n)
        return (f"{name:12s} {self.n:8d} {acc:6.2f}% ±{1.96 * se:4.2f} {100.0 * self.up / n:7.1f}%"
                f" {self.ev / n:+8.4f} {self.ev1 / n:+8.4f} {self.ev2 / n:+8.4f}")


HEADER = (f"{'strategy':12s} {'n':>8s} {'acc':>7s} {'  95%':>6s} {'says_UP':>7s}"
          f" {'EV/$1':>8s} {'EV@1c':>8s} {'EV@2c':>8s}")


def report(title, books, order):
    print(f"\n--- {title} ---")
    print(HEADER)
    for k in order:
        if k in books and books[k].n:
            print(books[k].row(k))


def new_books():
    return collections.defaultdict(Book)


ORDER = ["resolver", "favorite", "underdog", "always_up", "always_down", "coinflip"]


def score_row(books, side, truth, yp, idx):
    fav = "UP" if yp > 0.5 else "DOWN"
    dog = "DOWN" if yp > 0.5 else "UP"
    books["resolver"].add(side, truth, yp)
    books["favorite"].add(fav, truth, yp)
    books["underdog"].add(dog, truth, yp)
    books["always_up"].add("UP", truth, yp)
    books["always_down"].add("DOWN", truth, yp)
    books["coinflip"].add("UP" if idx % 2 == 0 else "DOWN", truth, yp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bytes", type=int, default=400_000_000,
                    help="tail bytes of the settled ghost log to scan")
    ap.add_argument("--band", type=float, nargs=2, default=(0.45, 0.55),
                    help="toss-up band bounds")
    args = ap.parse_args()
    lo, hi = args.band

    allb, bandb = new_books(), new_books()
    bylane = collections.defaultdict(new_books)
    days = collections.Counter()

    sz = os.path.getsize(GHOST)
    f = open(GHOST, "rb")
    f.seek(max(0, sz - args.bytes))
    f.readline()
    idx = 0
    for line in f:
        if b'"outcome"' not in line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        out = r.get("outcome")
        if out not in ("YES", "NO"):
            continue
        try:
            yp = float(r.get("yes_price"))
        except (TypeError, ValueError):
            continue
        side = norm_side(r.get("side"))
        if side is None:
            continue
        truth = "UP" if out == "YES" else "DOWN"
        idx += 1
        days[str(r.get("ts"))[:10]] += 1
        score_row(allb, side, truth, yp, idx)
        lane = f"{r.get('strategy')}|{r.get('window')}"
        if lo <= yp <= hi:
            score_row(bandb, side, truth, yp, idx)
            score_row(bylane[lane], side, truth, yp, idx)

    print("=" * 92)
    print("RESOLVER vs QUOTE — settled ghost candidates (real Polymarket resolutions)")
    print(f"rows={idx}  dates {min(days) if days else '-'} -> {max(days) if days else '-'}")
    print("=" * 92)
    report("ALL PRICES (favorite/underdog are trivially separable here)", allb, ORDER)
    report(f"TOSS-UP BAND {lo}-{hi} — the quote has no opinion, so any acc>50% is real added value",
           bandb, ORDER)

    print(f"\n--- per lane, band only: does the resolver beat the favorite HERE? (n>=300) ---")
    print(f"{'lane':20s} {'n':>6s} {'resolver':>9s} {'favorite':>9s} {'delta':>7s} "
          f"{'res EV@1c':>10s} {'fav EV@1c':>10s}")
    rows = []
    for lane, bk in bylane.items():
        r_, f_ = bk["resolver"], bk["favorite"]
        if r_.n < 300:
            continue
        ra, fa = 100.0 * r_.right / r_.n, 100.0 * f_.right / max(1, f_.n)
        rows.append((ra - fa, lane, r_.n, ra, fa, r_.ev1 / r_.n, f_.ev1 / max(1, f_.n)))
    for d, lane, n, ra, fa, re_, fe in sorted(rows):
        print(f"{lane:20s} {n:6d} {ra:8.1f}% {fa:8.1f}% {d:+6.1f} {re_:+10.4f} {fe:+10.4f}")
    beat = sum(1 for d, *_ in rows if d > 0)
    print(f"\nlanes where the resolver beats the quote: {beat}/{len(rows)}")

    # ---- LIVE: the resolver actually acted here, so this is the money answer ----
    live = new_books()
    liveband = new_books()
    n_live = 0
    if os.path.exists(LIVE):
        for i, line in enumerate(open(LIVE)):
            try:
                r = json.loads(line)
            except Exception:
                continue
            side = norm_side(r.get("action") or r.get("side"))
            try:
                ep = float(r.get("entry_price"))
            except (TypeError, ValueError):
                continue
            if side is None or not r.get("exit_reason"):
                continue
            # Reconstruct the real outcome from the side taken + whether it won at
            # settlement. Only settled exits carry direction truth; a stopped trade
            # tells you nothing about which way the market finally went.
            if r.get("exit_reason") not in (
                    "updown_expired", "RESOLVED:YES (real)", "RESOLVED:NO (real)", "updown_resolved"):
                continue
            won = bool(r.get("win"))
            truth = side if won else ("DOWN" if side == "UP" else "UP")
            yp = ep if side == "UP" else 1.0 - ep
            n_live += 1
            score_row(live, side, truth, yp, i)
            if lo <= yp <= hi:
                score_row(liveband, side, truth, yp, i)
    print(f"\n{'=' * 92}\nLIVE REALISED (settled exits only — stops carry no direction truth)  n={n_live}")
    print("!! The `resolver` row here is CIRCULAR and must not be read as a score. Live truth is")
    print("!! reconstructed from `win`, so resolver-accuracy IS the win rate by construction, and")
    print("!! the sample is only trades that survived to settlement — the ones that went the wrong")
    print("!! way were stopped out and excluded. That is survivorship, not skill. The baselines")
    print("!! (favorite/always_up/coinflip) ARE valid here: they are independent of the side taken.")
    print("!! The clean resolver answer is the GHOST toss-up band above — no exits, no survivorship.")
    report("LIVE all prices", live, ORDER)
    report(f"LIVE toss-up band {lo}-{hi}", liveband, ORDER)


if __name__ == "__main__":
    main()
