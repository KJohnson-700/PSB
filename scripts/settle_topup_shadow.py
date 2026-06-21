"""Settle final-window top-up SHADOW rows against real Polymarket outcomes.

Reads data/calibration/topup_shadow.jsonl, resolves each market's YES/NO outcome
(reusing the ghost settler's Gamma resolver), and scores each shadow top-up:

  win  = (detected_winning_side == outcome)
  unit P&L (only if a real ask <= cap was available, i.e. fillable):
         win  ->  (1.0 - topup_price)     # redeem at $1.00
         loss ->  -topup_price            # the asymmetric tail that kills naive farms

Reports the numbers that decide whether to advance to paper/live:
  - confirmation ACCURACY (did our winner gate pick real winners?)
  - fill availability (how often was an executable ask <= cap actually there?)
  - net EV per fillable top-up unit (must clear fees + a margin)
  - whether requiring oracle agreement (oracle_ok) improves accuracy

Usage:  .venv/bin/python scripts/settle_topup_shadow.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.settle_rejected_candidates import fetch_resolution  # noqa: E402

SHADOW_PATH = REPO_ROOT / "data" / "calibration" / "topup_shadow.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=str(SHADOW_PATH))
    ap.add_argument("--dry-run", action="store_true", help="resolve + report, do not rewrite file")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"no shadow log yet at {path}")
        return

    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    cache: dict = {}
    newly = 0
    for r in rows:
        if r.get("settled"):
            continue
        outcome = fetch_resolution(str(r.get("market_id", "")), cache)
        if outcome is None:
            continue  # not resolved yet; retry next run
        side = r.get("detected_winning_side", "YES")
        win = (outcome == side)
        tp = r.get("topup_price")
        if tp is not None:
            realized = (1.0 - float(tp)) if win else -float(tp)
        else:
            realized = None  # no executable top-up was available
        r.update({"settled": True, "outcome": outcome, "win": win, "realized_unit": realized})
        newly += 1

    if not args.dry_run and newly:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    settled = [r for r in rows if r.get("settled")]
    print(f"rows={len(rows)} newly_settled={newly} total_settled={len(settled)}")
    if not settled:
        print("(nothing resolved yet — re-run after markets settle)")
        return

    def report(label: str, sub: list) -> None:
        if not sub:
            print(f"  {label}: (none)")
            return
        n = len(sub)
        wins = sum(1 for r in sub if r.get("win"))
        fillable = [r for r in sub if r.get("realized_unit") is not None]
        ev = (sum(r["realized_unit"] for r in fillable) / len(fillable)) if fillable else float("nan")
        print(
            f"  {label}: n={n}  confirm_acc={100*wins/n:.1f}%  "
            f"fillable={len(fillable)}/{n} ({100*len(fillable)/n:.0f}%)  "
            f"EV/unit={ev:+.4f}"
        )

    print("Final-window top-up shadow — would-be performance:")
    report("ALL mark-confirmed", settled)
    report("oracle_ok=True only", [r for r in settled if r.get("oracle_ok")])

    # 2026-06-21 research-driven cohorts. The naive "buy the 0.95+ favorite" is not +EV;
    # the only documented edge is independent-confirm AND ask materially below fair. Score
    # THAT cohort, plus price buckets so we can see where (if anywhere) EV turns positive.
    print("Research cohorts (independent-confirm + below-fair):")
    report("below_fair (ask<=cap)", [r for r in settled if r.get("below_fair")])
    report("CONFIRMED EDGE (oracle_ok & below_fair)", [r for r in settled if r.get("confirmed_edge")])

    print("By executable top-up price bucket:")
    buckets = [(0.0, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 0.97), (0.97, 1.01)]
    for lo, hi in buckets:
        sub = [
            r for r in settled
            if r.get("topup_price") is not None and lo <= float(r["topup_price"]) < hi
        ]
        report(f"  ask [{lo:.2f},{hi:.2f})", sub)

    # break-even reference: at avg topup price p, EV>0 needs accuracy > p.
    fillable = [r for r in settled if r.get("realized_unit") is not None]
    if fillable:
        avg_p = sum(float(r["topup_price"]) for r in fillable) / len(fillable)
        print(f"  avg topup price={avg_p:.3f}  => need confirm_acc > {100*avg_p:.1f}% just to break even (pre-fee)")


if __name__ == "__main__":
    main()
