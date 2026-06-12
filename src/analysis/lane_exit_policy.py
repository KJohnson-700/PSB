"""Self-maintaining per-lane EXIT policy recommender (shadow by default).

Tier 2 of the per-lane exits work. Mirrors the entry-side `per_lane_thresholds`
/ `lane_thresholds.json` pattern: a recompute job that reads settled trades,
classifies each lane's exit signature, and writes a recommendation file.

SAFETY: exit changes CANNOT be ghost-validated (the ghost log only covers
admission/side gates, not TP/stop/time-decay). Therefore this module is
RECOMMEND-ONLY. It never edits `config/settings.yaml` and never mutates live
exit params. It writes `data/calibration/lane_exit_policy.json` with, per lane:

  - measured held-WR / realized-WR / dollar gap
  - the recommended policy bucket (A/B/C) and params
  - the CURRENT live config for that lane (resolved from YAML)
  - a `drift` flag when the live config disagrees with the recommendation

A human reviews `drift` lanes and applies changes by hand (forward-test only).
Recommendations are emitted only once a lane clears `min_n_apply` (default 20)
so we don't churn exits on noise.

Usage:
    python -m src.analysis.lane_exit_policy            # write recommendations
    python -m src.analysis.lane_exit_policy --print    # also print a table
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from src.analysis.lane_exit_audit import SETTLED_PATH, _load, audit
from src.execution.updown_exit_shared import (
    parse_updown_exit_globals,
    resolve_updown_exit_params_for_position,
)

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "settings.yaml"
OUT_PATH = ROOT / "data" / "calibration" / "lane_exit_policy.json"

# Default trailing-floor params for Policy-A lanes (uniform until per-lane MFE
# has enough samples to tune; see lane_exit_audit MFE notes).
A_PARAMS = {
    "updown_hold_winners_to_resolution": True,
    "updown_trail_arm_pct": 0.10,
    "updown_trail_gap_pct": 0.15,
}
MIN_N_APPLY = 20


def _find_exit_cfg(d: Any) -> Dict[str, Any] | None:
    if isinstance(d, dict):
        if "take_profit_pct" in d and "updown_stop_loss_pct" in d:
            return d
        for v in d.values():
            r = _find_exit_cfg(v)
            if r:
                return r
    return None


def _recommended_params(policy: str, *, gap: float = 0.0) -> Dict[str, Any]:
    if policy.startswith("A"):
        return dict(A_PARAMS)
    if policy.startswith("-") and gap > 5.0:
        return dict(A_PARAMS)
    # B and C: no hold/trail — keep the tight global TP/SL.
    return {"updown_hold_winners_to_resolution": False}


def build(rows: List[Dict[str, Any]], min_n: int) -> Dict[str, Any]:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    g = parse_updown_exit_globals(_find_exit_cfg(cfg) or {})

    lanes: List[Dict[str, Any]] = []
    for key, n, hwr, rwr, hp, ap_, gap, pol in audit(rows, min_n):
        strat, window, action = key
        leg = "NO" if action == "BUY_NO" else "YES"
        resolved = resolve_updown_exit_params_for_position(
            g, strategy_name=strat, window_size=window,
            entry_leg=leg, outcome=("NO" if leg == "NO" else "YES"),
        )
        live_hold = bool(resolved.updown_hold_winners_to_resolution)
        rec = _recommended_params(pol, gap=gap)
        rec_hold = bool(rec.get("updown_hold_winners_to_resolution"))
        # Drift only matters once we trust the sample, and only on the
        # hold/trail dimension this module governs.
        drift = (n >= MIN_N_APPLY) and (live_hold != rec_hold)
        lanes.append({
            "lane": "|".join(str(x) for x in key),
            "strategy": strat, "window": window, "action": action,
            "n": n,
            "held_wr": round(hwr, 1), "realized_wr": round(rwr, 1),
            "held_pnl": round(hp, 2), "realized_pnl": round(ap_, 2),
            "gap": round(gap, 2),
            "policy": pol,
            "recommended": rec,
            "live_hold_winners": live_hold,
            "live_trail_arm": resolved.updown_trail_arm_pct,
            "applicable": n >= MIN_N_APPLY,
            "drift": drift,
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settled_n": len(rows),
        "min_n_apply": MIN_N_APPLY,
        "shadow_only": True,
        "lanes": lanes,
    }


def recompute(
    min_n: int = 5,
    since: Optional[str] = None,
    out_path: Path | str = OUT_PATH,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Load settled trades, rebuild the policy file, return (payload, drift_lanes).

    Safe to call from a worker thread (pure I/O + compute). Drift lanes are those
    where the live config disagrees with the data-recommended policy AND the lane
    has cleared ``MIN_N_APPLY`` settled trades.
    """
    rows = _load(SETTLED_PATH, since)
    payload = build(rows, min_n)
    Path(out_path).write_text(json.dumps(payload, indent=2))
    drift = [l for l in payload["lanes"] if l.get("drift")]
    return payload, drift


def drift_signature(drift_lanes: List[Dict[str, Any]]) -> frozenset:
    """Stable identity of the current drift set, to suppress repeat alerts.

    Keyed on (lane, recommended-hold) so an alert re-fires only when a new lane
    drifts or an existing lane's recommendation flips — not every settle cycle.
    """
    return frozenset(
        (l["lane"], bool(l["recommended"].get("updown_hold_winners_to_resolution")))
        for l in drift_lanes
    )


def format_drift_message(drift_lanes: List[Dict[str, Any]]) -> str:
    """Human-readable Discord alert body for the current drift set."""
    if not drift_lanes:
        return ""
    lines = ["⚠️ **Exit-policy drift** — live config disagrees with settled data:"]
    for l in sorted(drift_lanes, key=lambda x: -abs(x.get("gap", 0))):
        want = ("hold+trail" if l["recommended"].get("updown_hold_winners_to_resolution")
                else "tight TP/SL")
        have = "hold+trail" if l["live_hold_winners"] else "tight TP/SL"
        lines.append(
            f"• `{l['lane']}` (n={l['n']}): held {l['held_wr']:.0f}% / "
            f"realized {l['realized_wr']:.0f}%, gap {l['gap']:+.1f} — "
            f"**data wants {want}**, live is {have}"
        )
    lines.append("_Review + apply by hand, then restart. Recommend-only; nothing auto-applied._")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-n", type=int, default=5)
    ap.add_argument("--since", type=str, default=None)
    ap.add_argument("--print", action="store_true", dest="show")
    ap.add_argument("--out", type=str, default=str(OUT_PATH))
    args = ap.parse_args()

    rows = _load(SETTLED_PATH, args.since)
    payload = build(rows, args.min_n)
    Path(args.out).write_text(json.dumps(payload, indent=2))

    drifts = [l for l in payload["lanes"] if l["drift"]]
    print(f"Wrote {args.out} — {len(payload['lanes'])} lanes, "
          f"{len(drifts)} drift (config disagrees with data, n>={MIN_N_APPLY}).")
    if drifts:
        print("\nDRIFT — review and apply by hand (forward-test only):")
        for l in drifts:
            want = "hold+trail" if l["recommended"].get("updown_hold_winners_to_resolution") else "tight TP/SL"
            have = "hold+trail" if l["live_hold_winners"] else "tight TP/SL"
            print(f"  {l['lane']:30s} n={l['n']:3d} gap={l['gap']:+7.1f}  "
                  f"data wants {want}, live is {have}  [{l['policy']}]")
    if args.show:
        print("\nAll lanes:")
        for l in sorted(payload["lanes"], key=lambda x: -x["gap"]):
            print(f"  {l['lane']:30s} n={l['n']:3d} held={l['held_wr']:4.0f}% "
                  f"real={l['realized_wr']:4.0f}% gap={l['gap']:+7.1f}  {l['policy']}")


if __name__ == "__main__":
    main()
