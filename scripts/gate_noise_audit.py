#!/usr/bin/env python3
"""Gate-noise audit — rank every reject gate by the EV of what it BLOCKS.

The complaint "we have too many gates / lane_min_edge is noise" is testable. A
gate is only worth keeping if the candidates it rejects would have LOST money. So
for every (strategy, reason-family, window, side) we settle the ghost-rejected
pool against the real outcome and score it on EV-per-$ (mean realized_pct), NOT
win-rate — because entry yes_price ~= P(YES) makes WR a tautology (a gate can
reject 80%-WR candidates that are still -EV because they're overpriced).

Verdict per gate:
  * PROTECTIVE  — blocked pool EV clearly < 0 (gate is saving you money; keep).
  * NOISE       — blocked pool EV >= ~0 (gate is rejecting break-even-or-better
                  trades; this is a frequency leak — candidate for removal).
  * TAUTOLOGY   — high WR but -EV (looks like it blocks winners, actually -EV;
                  the trap that makes good gates look like noise).

Usage:
    python scripts/gate_noise_audit.py --strategy bitcoin --since 2026-06-06
    python scripts/gate_noise_audit.py --min-n 50          # all strategies
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SETTLED = REPO / "data" / "calibration" / "rejected_candidates_settled.jsonl"

NOISE_EV = -0.01   # blocked-pool EV at/above this = not really protecting you
PROTECT_EV = -0.03  # clearly negative = protective
HIGH_WR = 0.60      # for the tautology flag


def _ts(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def _reason_family(reason: str) -> str:
    """Collapse a parametered reason to its gate family.

    'lane_min_edge>=0.120' -> 'lane_min_edge'; 'neutral_15m_min_edge=0.12' ->
    'neutral_15m_min_edge'. Splits on the first of = < > ( space.
    """
    r = str(reason or "").strip()
    for sep in ("=", "<", ">", "(", " "):
        i = r.find(sep)
        if i > 0:
            r = r[:i]
    return r or "(none)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--since", default="2026-06-06")
    ap.add_argument("--min-n", type=int, default=50)
    ap.add_argument("--by-lane", action="store_true",
                    help="split each gate by (window, side) too")
    ap.add_argument("--live-hours", type=float, default=None,
                    help="only report gate families that ALSO fired in the LIVE "
                         "reject log within this many hours (drops removed/dead "
                         "gates whose settled history is stale). Recommended: 48.")
    args = ap.parse_args()

    since = dt.datetime.fromisoformat(args.since).replace(
        tzinfo=dt.timezone.utc).timestamp()
    needle = f'"{args.strategy}"' if args.strategy else None

    # Optional live-recency filter: a gate is only actionable if it still fires.
    live_families = None
    if args.live_hours is not None:
        live_cut = dt.datetime.now(dt.timezone.utc).timestamp() - args.live_hours * 3600
        live_families = set()
        live_log = REPO / "data" / "calibration" / "rejected_candidates.jsonl"
        for line in open(live_log):
            if needle and needle not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if args.strategy and d.get("strategy") != args.strategy:
                continue
            ts = _ts(d.get("ts"))
            if ts is None or ts < live_cut:
                continue
            live_families.add((d.get("strategy"), _reason_family(d.get("reason"))))

    agg = defaultdict(lambda: {"n": 0, "w": 0, "rp": 0.0})
    for line in open(SETTLED):
        if needle and needle not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if args.strategy and d.get("strategy") != args.strategy:
            continue
        ts = _ts(d.get("ts"))
        if ts is None or ts < since:
            continue
        fam = _reason_family(d.get("reason"))
        key = (d.get("strategy"), fam)
        if args.by_lane:
            key = (d.get("strategy"), fam, d.get("window"), d.get("side"))
        g = agg[key]
        g["n"] += 1
        if d.get("win"):
            g["w"] += 1
        rp = d.get("realized_pct")
        if rp is not None:
            g["rp"] += rp

    rows = []
    for key, g in agg.items():
        if g["n"] < args.min_n:
            continue
        if live_families is not None and (key[0], key[1]) not in live_families:
            continue  # gate no longer fires live → removed/dead, not actionable
        wr = g["w"] / g["n"]
        ev = g["rp"] / g["n"]
        if ev >= NOISE_EV:
            verdict = "NOISE  -> blocks +EV, cut/loosen"
        elif ev <= PROTECT_EV:
            verdict = "PROTECTIVE -> keep"
        else:
            verdict = "marginal"
        if ev < NOISE_EV and wr >= HIGH_WR:
            verdict += " [TAUTOLOGY: high-WR but -EV]"
        rows.append((key, g["n"], wr, ev, verdict))

    # Sort: noisiest (highest blocked-pool EV = most wrongly blocked) first.
    rows.sort(key=lambda r: -r[3])
    scope = args.strategy or "ALL strategies"
    print(f"=== Gate-noise audit — {scope}, since {args.since}, n>={args.min_n} ===")
    print(f"(ranked by blocked-pool EV: top = most likely NOISE)\n")
    klen = 46 if args.by_lane else 30
    print(f"{'gate (strategy, reason[, window, side])':{klen}} {'n':>6} {'WR%':>6} {'EV/$':>7}  verdict")
    for key, n, wr, ev, verdict in rows:
        print(f"{str(key):{klen}} {n:>6} {100*wr:>6.1f} {ev:>+7.3f}  {verdict}")


if __name__ == "__main__":
    main()
