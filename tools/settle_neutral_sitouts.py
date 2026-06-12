"""Settle NEUTRAL sit-out shadow rows against actual Polymarket outcomes.

Reads ``data/calibration/neutral_sitout_shadow.jsonl`` (written log-only by
``SolMacroStrategy._shadow_log_neutral_sitout`` whenever an alt lane sits out
because its own timeframe had no usable bias) and produces
``data/calibration/neutral_sitout_settled.jsonl`` with the would-be win/loss of
following the *tape* (window-delta) side that scan recorded.

This answers: "when ETH / SOL-family lanes sat out on NEUTRAL, would following
the tape have won, and at what EV?" — the known-starvation counterfactual that
the rejected-*candidate* observer can't capture (a sit-out has no side).

Idempotent: a stable ghost_id (hash of ts|market_id|reason) skips already-settled
rows. Reuses the resolution + scoring helpers from settle_rejected_candidates so
the win/EV math is identical to the main ghost pipeline.

Usage:
    python tools/settle_neutral_sitouts.py
    python tools/settle_neutral_sitouts.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.settle_rejected_candidates import (  # noqa: E402
    compute_would_be,
    fetch_resolution,
    ghost_id,
    load_settled_ids,
)

SITOUT_LOG = REPO_ROOT / "data" / "calibration" / "neutral_sitout_shadow.jsonl"
SETTLED_LOG = REPO_ROOT / "data" / "calibration" / "neutral_sitout_settled.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Don't write settled records")
    ap.add_argument("--input", default=str(SITOUT_LOG))
    ap.add_argument("--output", default=str(SETTLED_LOG))
    ap.add_argument("--throttle", type=float, default=0.1, help="Sleep between API calls (s)")
    args = ap.parse_args()

    in_path, out_path = Path(args.input), Path(args.output)
    if not in_path.exists():
        print(f"No neutral-sitout log at {in_path}", file=sys.stderr)
        return 0

    settled_ids = load_settled_ids(out_path)
    cache: Dict[str, Optional[str]] = {}
    now = datetime.now(timezone.utc)

    skipped_settled = skipped_no_market_id = skipped_unresolved = settled_new = 0
    settle_records = []

    with open(in_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            gid = ghost_id(rec)
            if gid in settled_ids:
                skipped_settled += 1
                continue

            mid = rec.get("market_id", "")
            if not mid:
                skipped_no_market_id += 1
                continue

            outcome = fetch_resolution(mid, cache)
            time.sleep(args.throttle)
            if outcome is None:
                skipped_unresolved += 1
                continue

            wb = compute_would_be(
                action=rec.get("action", ""),
                yes_price=float(rec.get("yes_price") or 0.0),
                no_price=float(rec.get("no_price") or 0.0),
                outcome=outcome,
            )
            settle_records.append(
                {
                    "ghost_id": gid,
                    "settled_at": now.isoformat(),
                    "outcome": outcome,
                    **wb,
                    "ts": rec.get("ts"),
                    "strategy": rec.get("strategy"),
                    "window": rec.get("window"),
                    "action": rec.get("action"),
                    "reason": rec.get("reason"),
                    "market_id": mid,
                    "market_slug": rec.get("market_slug"),
                    "yes_price": rec.get("yes_price"),
                    "no_price": rec.get("no_price"),
                    "wd_prob": rec.get("wd_prob"),
                    "move_pct": rec.get("move_pct"),
                    "mins_left": rec.get("mins_left"),
                    "primary_htf_bias": rec.get("primary_htf_bias"),
                    "alt_1h_trend": rec.get("alt_1h_trend"),
                    "alt_15m_trend": rec.get("alt_15m_trend"),
                    "alt_5m_trend": rec.get("alt_5m_trend"),
                }
            )
            settled_new += 1

    if not args.dry_run and settle_records:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "a") as f:
            for r in settle_records:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")

    print(f"Processed {in_path}")
    print(f"  already settled : {skipped_settled}")
    print(f"  no market_id    : {skipped_no_market_id}")
    print(f"  unresolved/api  : {skipped_unresolved}")
    print(f"  newly settled   : {settled_new}{'  (dry-run, not written)' if args.dry_run else ''}")

    # Tape-EV diagnostic: would following the tape on sit-outs have won?
    if settle_records or out_path.exists():
        wins = losses = 0
        realized_sum = 0.0
        realized_n = 0
        by_window: Dict[str, Dict[str, float]] = {}
        with open(out_path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                w = r.get("win")
                if w is True:
                    wins += 1
                elif w is False:
                    losses += 1
                rp = r.get("realized_pct")
                if isinstance(rp, (int, float)):
                    realized_sum += rp
                    realized_n += 1
                key = r.get("window") or "?"
                s = by_window.setdefault(key, {"wins": 0.0, "losses": 0.0, "rp": 0.0, "n": 0.0})
                if w is True:
                    s["wins"] += 1
                elif w is False:
                    s["losses"] += 1
                if isinstance(rp, (int, float)):
                    s["rp"] += rp
                    s["n"] += 1
        if wins + losses:
            ev = (realized_sum / realized_n) if realized_n else float("nan")
            print(
                f"\nTape-side sit-out EV so far: {wins}W / {losses}L "
                f"({100*wins/(wins+losses):.1f}% WR, mean realized {ev:+.3f})"
            )
            print("  by window:")
            for win_label, s in sorted(by_window.items()):
                n = s["wins"] + s["losses"]
                if n:
                    mean_rp = (s["rp"] / s["n"]) if s["n"] else float("nan")
                    print(
                        f"    {win_label:4s}  {int(s['wins']):3d}W / {int(s['losses']):3d}L "
                        f"({100*s['wins']/n:5.1f}%, mean realized {mean_rp:+.3f})"
                    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
