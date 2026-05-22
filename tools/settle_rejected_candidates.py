"""Resolve ghost-trade rejections against actual Polymarket outcomes.

Reads ``data/calibration/rejected_candidates.jsonl`` and produces
``data/calibration/rejected_candidates_settled.jsonl`` with would-be win/loss
for each ghost candidate whose market has resolved.

Idempotent: re-running only adds newly resolvable rejections; previously settled
records are skipped via a stable ghost_id (hash of ts|market_id|reason).

Usage:
    python tools/settle_rejected_candidates.py
    python tools/settle_rejected_candidates.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
try:
    from src.analysis.lane_calibration import LaneCalibrator  # noqa: E402
    from src.analysis.ghost_calibration import (  # noqa: E402
        DEFAULT_REGIME_LOG,
        REGIME_MATCH_MAX_AGE_SEC,
        enrich_with_regime,
        load_regime_snapshots,
    )
except Exception:  # noqa: BLE001 — calibrator optional; settling still works
    LaneCalibrator = None  # type: ignore
    DEFAULT_REGIME_LOG = REPO_ROOT / "data" / "calibration" / "market_regime.jsonl"  # type: ignore
    REGIME_MATCH_MAX_AGE_SEC = 30 * 60  # type: ignore
    enrich_with_regime = None  # type: ignore
    load_regime_snapshots = None  # type: ignore
REJECTED_LOG = REPO_ROOT / "data" / "calibration" / "rejected_candidates.jsonl"
SETTLED_LOG = REPO_ROOT / "data" / "calibration" / "rejected_candidates_settled.jsonl"
GAMMA_API = "https://gamma-api.polymarket.com"
RESOLVED_BUFFER_SEC = 90  # don't query until window has been over for ≥90s


def ghost_id(rec: Dict) -> str:
    key = f"{rec.get('ts','')}|{rec.get('market_id','')}|{rec.get('reason','')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def load_settled_ids(path: Path) -> set:
    if not path.exists():
        return set()
    ids = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "ghost_id" in obj:
                    ids.add(obj["ghost_id"])
            except json.JSONDecodeError:
                continue
    return ids


def fetch_resolution(market_id: str, cache: Dict[str, Optional[str]]) -> Optional[str]:
    """Return 'YES' / 'NO' / None (unresolved). Caches to avoid repeat API hits."""
    if market_id in cache:
        return cache[market_id]
    try:
        resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=10)
        if resp.status_code != 200:
            cache[market_id] = None
            return None
        data = resp.json()
        if not data.get("closed", False):
            cache[market_id] = None
            return None
        # Direct resolution field first
        resolution = data.get("resolution")
        if isinstance(resolution, str) and resolution.upper() in ("YES", "NO"):
            cache[market_id] = resolution.upper()
            return cache[market_id]
        # Fallback: outcomePrices
        op = data.get("outcomePrices")
        if op:
            try:
                prices = json.loads(op) if isinstance(op, str) else op
                if len(prices) >= 2:
                    yp = float(prices[0])
                    if yp >= 0.99:
                        cache[market_id] = "YES"; return "YES"
                    if yp <= 0.01:
                        cache[market_id] = "NO"; return "NO"
            except (ValueError, json.JSONDecodeError):
                pass
        cache[market_id] = None
        return None
    except requests.RequestException:
        cache[market_id] = None
        return None


def compute_would_be(action: str, yes_price: float, no_price: float, outcome: str) -> Dict:
    """Return {win, realized_pct, hypothetical_payout, hypothetical_notional}."""
    action = (action or "").upper()
    if action == "BUY_YES":
        entry = yes_price
        won = (outcome == "YES")
    elif action == "BUY_NO":
        entry = no_price if no_price else ((1.0 - yes_price) if yes_price else None)
        won = (outcome == "NO")
    else:
        return {"win": None, "realized_pct": None, "hypothetical_payout": None, "hypothetical_notional": None}

    # Win/loss is fully determined by action+outcome and doesn't need price.
    # Backfilled records may lack price → return win but not realized_pct.
    if entry is None or entry <= 0 or entry >= 1:
        return {
            "win": bool(won),
            "realized_pct": None,
            "hypothetical_payout": 1.0 if won else 0.0,
            "hypothetical_notional": None,
        }

    realized = (1.0 - entry) / entry if won else -1.0
    return {
        "win": bool(won),
        "realized_pct": round(realized, 6),
        "hypothetical_payout": 1.0 if won else 0.0,
        "hypothetical_notional": round(entry, 6),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Don't write settled records")
    ap.add_argument("--input", default=str(REJECTED_LOG))
    ap.add_argument("--output", default=str(SETTLED_LOG))
    ap.add_argument("--regime-log", type=Path, default=DEFAULT_REGIME_LOG)
    ap.add_argument("--regime-max-age-sec", type=float, default=REGIME_MATCH_MAX_AGE_SEC)
    ap.add_argument("--throttle", type=float, default=0.1, help="Sleep between API calls (s)")
    args = ap.parse_args()

    in_path, out_path = Path(args.input), Path(args.output)
    if not in_path.exists():
        print(f"No rejected candidates log at {in_path}", file=sys.stderr)
        return 0

    settled_ids = load_settled_ids(out_path)
    regime_snapshots = load_regime_snapshots(args.regime_log) if load_regime_snapshots else []
    cache: Dict[str, Optional[str]] = {}
    now = datetime.now(timezone.utc)

    skipped_settled = 0
    skipped_too_recent = 0
    skipped_no_market_id = 0
    skipped_unresolved = 0
    settled_new = 0
    regime_matched = 0
    regime_unmatched = 0
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

            end_ts = rec.get("market_end_ts", "")
            if end_ts:
                try:
                    end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                    if (now - end_dt).total_seconds() < RESOLVED_BUFFER_SEC:
                        skipped_too_recent += 1
                        continue
                except ValueError:
                    pass

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

            settled_rec = {
                "ghost_id": gid,
                "settled_at": now.isoformat(),
                "outcome": outcome,
                **wb,
                # Carry forward fields needed for analysis (avoids re-joining)
                "ts": rec.get("ts"),
                "lane_id": rec.get("lane_id"),
                "strategy": rec.get("strategy"),
                "window": rec.get("window"),
                "side": rec.get("side"),
                "action": rec.get("action"),
                "reason": rec.get("reason"),
                "market_id": mid,
                "market_question": rec.get("market_question"),
                "yes_price": rec.get("yes_price"),
                "no_price": rec.get("no_price"),
                "est_prob_up": rec.get("est_prob_up"),
                "htf_bias": rec.get("htf_bias"),
                "btc_1h_regime": rec.get("btc_1h_regime"),
                "context": rec.get("context", {}),
                "convergence_score": rec.get("convergence_score"),
                "convergence_probe_count": rec.get("convergence_probe_count"),
                "convergence_pass_count": rec.get("convergence_pass_count"),
                "convergence_fail_count": rec.get("convergence_fail_count"),
                "convergence_narrow_pass_count": rec.get("convergence_narrow_pass_count"),
                "convergence_strong_pass_count": rec.get("convergence_strong_pass_count"),
                "edge_quality": rec.get("edge_quality"),
                "component_mean_quality": rec.get("component_mean_quality"),
            }
            if enrich_with_regime is not None:
                settled_rec = enrich_with_regime(
                    settled_rec,
                    regime_snapshots,
                    max_age_sec=args.regime_max_age_sec,
                )
                if settled_rec.get("regime_source") == "market_regime":
                    regime_matched += 1
                else:
                    regime_unmatched += 1
            settle_records.append(settled_rec)
            settled_new += 1

    if not args.dry_run and settle_records:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "a") as f:
            for r in settle_records:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")

        # Feed newly settled ghost outcomes into per-lane posteriors. Lane IDs
        # carry a `|rejected` suffix (set in rejected_candidate_log.py) so these
        # never mix with taken-trade lanes.
        if LaneCalibrator is not None:
            cal = LaneCalibrator(shadow_mode=True)  # shadow_mode is irrelevant for record()
            posterior_updates = 0
            for r in settle_records:
                lane = r.get("lane_id") or ""
                if not lane:
                    continue
                # Convert est_prob_up → stated prob of the side actually bet.
                # For BUY_NO, the bet wins when YES doesn't, so stated = 1 - est_prob_up.
                eu = r.get("est_prob_up")
                action = (r.get("action") or "").upper()
                if eu is None:
                    stated = None
                else:
                    try:
                        eu_f = float(eu)
                        stated = (1.0 - eu_f) if action == "BUY_NO" else eu_f
                    except (TypeError, ValueError):
                        stated = None
                try:
                    cal.record(
                        lane_id=lane,
                        stated_est_prob=stated,
                        realized_pct=float(r.get("realized_pct") or 0.0),
                        win=bool(r.get("win")),
                    )
                    posterior_updates += 1
                except Exception as _pe:  # noqa: BLE001 — telemetry only
                    print(f"  posterior update failed for {lane}: {_pe}", file=sys.stderr)
            print(f"  posterior updates: {posterior_updates}")

    # Summary
    print(f"Processed {in_path}")
    print(f"  already settled : {skipped_settled}")
    print(f"  too recent      : {skipped_too_recent}")
    print(f"  no market_id    : {skipped_no_market_id}")
    print(f"  unresolved/api  : {skipped_unresolved}")
    print(f"  newly settled   : {settled_new}{'  (dry-run, not written)' if args.dry_run else ''}")
    print(f"  regime matched  : {regime_matched}")
    print(f"  regime unmatched: {regime_unmatched}")

    # Quick diagnostic on what's been settled so far
    if settle_records or out_path.exists():
        wins, losses = 0, 0
        by_reason = {}
        with open(out_path) as f:
            for line in f:
                try: r = json.loads(line)
                except: continue
                if r.get("win") is True: wins += 1
                elif r.get("win") is False: losses += 1
                key = (r.get("reason"), r.get("action"))
                stats = by_reason.setdefault(key, {"wins": 0, "losses": 0})
                if r.get("win") is True: stats["wins"] += 1
                elif r.get("win") is False: stats["losses"] += 1
        if wins + losses:
            print(f"\nGhost outcomes so far: {wins}W / {losses}L  ({100*wins/(wins+losses):.1f}% would-be WR)")
            print(f"  by (gate_reason, action):")
            for (reason, action), s in sorted(by_reason.items()):
                n = s["wins"] + s["losses"]
                if n:
                    print(f"    {reason or '?':38s} {action or '?':8s}  {s['wins']:3d}W / {s['losses']:3d}L  ({100*s['wins']/n:5.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
