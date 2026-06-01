"""Per-lane exit scorecard: held-to-resolution vs realized (TP/SL) outcomes.

This is the forward-test scorecard for the per-lane exit policy. Exit changes
CANNOT be ghost-validated (the ghost log only covers admission/side gates), so
this reads the taken-exit settler output `trades_settled.jsonl` and, for every
`(strategy, window, side)` lane with enough samples, compares:

  held-WR    — win rate if every position were held to binary resolution
  realized-WR— win rate actually achieved by the live exit (TP / stop / time)
  GAP        — held_pnl - actual_pnl  (dollars the exit gave up, + = exit hurt)

Interpretation drives the three exit policies in config `updown_overrides`:

  A  exit kills edge   held-WR >= 48% and GAP > +5 and realized << held
                       -> hold_winners_to_resolution + positive trailing floor
  B  exit is engine    realized_pnl > held_pnl (GAP < 0)
                       -> keep the tight global TP/SL; DO NOT add hold/trail
  C  entry-broken      held-WR < 42% and realized bad
                       -> not an exit problem; needs entry-side / suppression

Usage:
    python -m src.analysis.lane_exit_audit               # all-time
    python -m src.analysis.lane_exit_audit --min-n 8     # raise sample floor
    python -m src.analysis.lane_exit_audit --since 2026-05-29
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

SETTLED_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "calibration"
    / "trades_settled.jsonl"
)


def _load(path: Path, since: str | None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and str(r.get("ts") or "")[:10] < since:
            continue
        rows.append(r)
    return rows


def classify(held_wr: float, realized_wr: float, gap: float) -> str:
    """Assign the exit policy bucket from a lane's held-vs-realized signature."""
    if held_wr >= 48.0 and gap > 5.0 and realized_wr < held_wr - 8.0:
        return "A hold+trail (exit kills edge)"
    # Check entry-broken before "exit is engine": a lane can have a negative gap
    # (exit doing damage control) yet still be fundamentally broken at entry.
    # Both imply "don't add hold/trail", but C is the truer, more actionable
    # diagnosis (needs entry-side work / suppression, not an exit knob).
    if held_wr < 42.0 and realized_wr < 45.0:
        return "C entry-broken (not an exit fix)"
    if gap < 0:
        return "B keep tight TP/SL (exit is engine)"
    return "- neutral / watch"


def audit(rows: List[Dict[str, Any]], min_n: int) -> List[Tuple]:
    agg: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        agg[(r.get("strategy"), r.get("window"), r.get("action"))].append(r)
    out = []
    for key, rs in agg.items():
        n = len(rs)
        if n < min_n:
            continue
        held_w = sum(1 for r in rs if r.get("held_win"))
        real_w = sum(1 for r in rs if (r.get("actual_pnl") or 0) > 0)
        held_pnl = sum(r.get("held_pnl", 0) or 0 for r in rs)
        actual_pnl = sum(r.get("actual_pnl", 0) or 0 for r in rs)
        held_wr = 100.0 * held_w / n
        real_wr = 100.0 * real_w / n
        gap = held_pnl - actual_pnl
        out.append((key, n, held_wr, real_wr, held_pnl, actual_pnl, gap,
                    classify(held_wr, real_wr, gap)))
    return sorted(out, key=lambda x: -x[6])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-n", type=int, default=5, help="minimum settled trades per lane")
    ap.add_argument("--since", type=str, default=None, help="ISO date floor, e.g. 2026-05-29")
    ap.add_argument("--path", type=str, default=str(SETTLED_PATH))
    args = ap.parse_args()

    rows = _load(Path(args.path), args.since)
    if not rows:
        print(f"No settled rows in {args.path}" + (f" since {args.since}" if args.since else ""))
        return
    results = audit(rows, args.min_n)
    print(f"Per-lane exit scorecard — n={len(rows)} settled"
          + (f" since {args.since}" if args.since else "")
          + f" (min_n={args.min_n})\n")
    hdr = f"{'lane':32s} {'n':>3s} {'heldWR':>6s} {'realWR':>6s} {'heldPnL':>8s} {'realPnL':>8s} {'GAP':>8s}  policy"
    print(hdr)
    print("-" * len(hdr))
    for key, n, hwr, rwr, hp, ap_, gap, pol in results:
        lane = "|".join(str(x) for x in key)
        print(f"{lane:32s} {n:3d} {hwr:5.0f}% {rwr:5.0f}% {hp:8.1f} {ap_:8.1f} {gap:8.1f}  {pol}")


if __name__ == "__main__":
    main()
