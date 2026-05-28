"""Ghost-driven per-lane threshold derivation.

The live ``LaneCalibrator`` applies global thresholds (β-veto floor 0.40,
min sample 30, α clamp [0.30, 2.50]) to every lane. The 180k+ records in
``rejected_candidates_settled.jsonl`` (would-have-been outcomes on every
candidate the live scanner rejected) carry enough information to compute
*per-lane* thresholds — some lanes are consistent 25% WR in specific
regimes and warrant a tighter floor than 0.40, others bounce around
50% and shouldn't be vetoed at all.

This module derives those per-lane overrides from the ghost log and
writes them to ``lane_thresholds.json``. The live calibrator can load
them at boot and consult them at admission time. **Off by default** —
gated by ``lane_calibration.per_lane_thresholds.enabled`` config; the
ghost data is the input but the operator decides when to flip the
override on.

Schema of ``lane_thresholds.json``::

    {
      "schema_version": 1,
      "computed_at": "2026-05-23T09:30:00+00:00",
      "min_bucket_n": 100,
      "wr_veto_threshold": 0.40,
      "thresholds": {
        "sol_macro|5m|down|bearish__bearish__bull|standard": {
          "ghost_n": 487,
          "ghost_wr": 0.269,
          "veto_recommended": true,
          "recommended_max_mean": 0.40
        },
        ...
      }
    }
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.analysis.lane_identity import build_lane_metadata, clean_lane_part, compose_lane_id

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_SETTLED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates_settled.jsonl"
DEFAULT_THRESHOLDS_PATH = DEFAULT_CALIBRATION_DIR / "lane_thresholds.json"

SCHEMA_VERSION = 1

# Defaults — operator overridable via config.
DEFAULT_MIN_BUCKET_N = 100
DEFAULT_WR_VETO_THRESHOLD = 0.40
# When a lane has enough ghost data AND ghost WR is below the threshold,
# recommend veto. The recommended_max_mean lets the operator dial how
# tight the override is — by default match the global floor.
DEFAULT_RECOMMENDED_MAX_MEAN = 0.40


@dataclass
class LaneBucket:
    """Counterfactual WR aggregate for one live lane_id."""

    n: int = 0
    wins: int = 0

    @property
    def losses(self) -> int:
        return self.n - self.wins

    @property
    def win_rate(self) -> Optional[float]:
        return (self.wins / self.n) if self.n > 0 else None


def _ghost_to_live_lane_id(rec: Dict[str, Any]) -> Optional[str]:
    """Mirror of ``ghost_calibration._ghost_to_live_lane_keys`` returning the
    first translated key (or None if metadata insufficient).

    Mirrors ghost settlement: prefer the exact lane id when present, otherwise
    rebuild the live lane from the rejected-record metadata so new entry-family
    taxonomy is preserved for threshold learning.
    """
    live_lane_id = str(rec.get("live_lane_id") or "").strip()
    if live_lane_id and len(live_lane_id.split("|")) >= 5:
        return live_lane_id
    context = rec.get("context")
    if isinstance(context, dict):
        context_lane_id = str(context.get("calibration_lane_id") or "").strip()
        if context_lane_id and len(context_lane_id.split("|")) >= 5:
            return context_lane_id
    else:
        context = {}

    lid = str(rec.get("lane_id") or "")
    parts = lid.split("|")
    if len(parts) < 3:
        return None
    strategy = str(rec.get("strategy") or parts[0]).strip()
    window = str(rec.get("window") or parts[1]).strip()
    direction = str(parts[2] or "").strip()
    if not strategy or not window or not direction:
        return None

    primary_bias = str(
        rec.get("primary_htf_bias")
        or context.get("primary_htf_bias")
        or rec.get("htf_bias")
        or context.get("htf_bias")
        or ""
    ).strip()
    alt_bias = str(
        rec.get("alt_htf_bias")
        or context.get("alt_htf_bias")
        or ""
    ).strip()
    btc_bias = str(
        rec.get("btc_htf_bias")
        or rec.get("btc_1h_regime")
        or context.get("btc_1h_regime")
        or ""
    ).strip()
    if strategy != "bitcoin" and primary_bias and not alt_bias:
        alt_bias = primary_bias

    lane_meta = build_lane_metadata(
        strategy=strategy,
        window_size=window,
        direction=direction,
        side_source=rec.get("side_source") or context.get("side_source"),
        resolver_path=rec.get("resolver_path") or context.get("resolver_path"),
        ai_used=bool(context.get("ai_used")),
        reason=rec.get("reason"),
        signal_reason=context.get("signal_reason") or rec.get("reason"),
        htf_bias=(primary_bias if strategy == "bitcoin" else None),
        primary_htf_bias=(None if strategy == "bitcoin" else primary_bias),
        alt_htf_bias=(None if strategy == "bitcoin" else alt_bias),
        btc_1h_regime=(None if strategy == "bitcoin" else btc_bias),
    )
    lane_regime = str(lane_meta.get("lane_regime") or "").strip() or "unclassified"
    lane_family = ""
    for candidate in (
        rec.get("lane_family"),
        context.get("lane_family"),
        context.get("entry_family"),
        parts[4] if len(parts) >= 5 and parts[4] != "rejected" else "",
    ):
        lane_family = clean_lane_part(candidate, default="")
        if lane_family:
            break
    if not lane_family:
        lane_family = str(lane_meta.get("entry_family") or "").strip() or "standard"
    return compose_lane_id(
        strategy=strategy,
        window_size=window,
        lane_side=direction,
        lane_regime=lane_regime,
        entry_family=lane_family,
    )


def _iter_settled(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("lane_thresholds: failed to read %s: %s", path, exc)
        return


def aggregate_ghost_buckets(
    settled_path: Path = DEFAULT_SETTLED_LOG,
) -> Dict[str, LaneBucket]:
    """Aggregate ghost outcomes by translated live lane_id."""
    buckets: Dict[str, LaneBucket] = defaultdict(LaneBucket)
    for rec in _iter_settled(settled_path):
        win = rec.get("win")
        if not isinstance(win, bool):
            continue
        live_id = _ghost_to_live_lane_id(rec)
        if not live_id:
            continue
        b = buckets[live_id]
        b.n += 1
        if win:
            b.wins += 1
    return buckets


def compute_lane_thresholds(
    settled_path: Path = DEFAULT_SETTLED_LOG,
    *,
    min_bucket_n: int = DEFAULT_MIN_BUCKET_N,
    wr_veto_threshold: float = DEFAULT_WR_VETO_THRESHOLD,
    recommended_max_mean: float = DEFAULT_RECOMMENDED_MAX_MEAN,
) -> Dict[str, Any]:
    """Compute per-lane threshold recommendations from ghost data.

    Returns a payload dict matching the ``lane_thresholds.json`` schema.
    A lane gets ``veto_recommended=True`` if its ghost WR is below
    ``wr_veto_threshold`` AND its bucket has at least ``min_bucket_n``
    settled records.
    """
    buckets = aggregate_ghost_buckets(settled_path)
    thresholds: Dict[str, Dict[str, Any]] = {}
    for lane_id, b in buckets.items():
        if b.n < min_bucket_n:
            continue
        wr = b.win_rate or 0.0
        veto = wr < wr_veto_threshold
        thresholds[lane_id] = {
            "ghost_n": b.n,
            "ghost_wr": round(wr, 4),
            "veto_recommended": bool(veto),
            "recommended_max_mean": float(recommended_max_mean),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "min_bucket_n": int(min_bucket_n),
        "wr_veto_threshold": float(wr_veto_threshold),
        "thresholds": thresholds,
    }


def write_lane_thresholds(
    payload: Dict[str, Any],
    *,
    path: Path = DEFAULT_THRESHOLDS_PATH,
) -> bool:
    """Atomic-ish write of lane_thresholds.json. Returns True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning("lane_thresholds: write failed: %s", exc)
        return False


def load_lane_thresholds(
    path: Path = DEFAULT_THRESHOLDS_PATH,
) -> Dict[str, Dict[str, Any]]:
    """Load per-lane thresholds from disk. Returns empty dict if file
    is missing or unparseable — calibrator must fall back to global
    defaults in that case."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("lane_thresholds: load failed: %s", exc)
        return {}
    if not isinstance(blob, dict):
        return {}
    if blob.get("schema_version") != SCHEMA_VERSION:
        logger.warning(
            "lane_thresholds: schema mismatch (have %s expected %s) — ignoring",
            blob.get("schema_version"), SCHEMA_VERSION,
        )
        return {}
    thresholds = blob.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        return {}
    return thresholds


def summarize_thresholds(payload: Dict[str, Any]) -> str:
    """Human-readable summary for CLI / logs."""
    thresholds: Dict[str, Dict[str, Any]] = payload.get("thresholds", {})
    veto_rows = [
        (lid, info) for lid, info in thresholds.items() if info.get("veto_recommended")
    ]
    veto_rows.sort(key=lambda kv: (kv[1].get("ghost_wr") or 0))
    lines = [
        f"min_bucket_n={payload.get('min_bucket_n')}  "
        f"wr_veto_threshold={payload.get('wr_veto_threshold')}  "
        f"computed_at={payload.get('computed_at')}",
        f"total lanes with sufficient data: {len(thresholds)}",
        f"veto recommended: {len(veto_rows)}",
        "",
    ]
    if veto_rows:
        lines.append(f"{'lane_id':<60} {'n':>5} {'wr':>7}")
        for lid, info in veto_rows[:40]:
            lines.append(
                f"{lid:<60} {info.get('ghost_n',0):>5} {info.get('ghost_wr',0):>7.3f}"
            )
        if len(veto_rows) > 40:
            lines.append(f"  ... +{len(veto_rows)-40} more")
    return "\n".join(lines)
