"""Rejected-candidate settlement and observability helpers.

The rejected-candidate tracker captures blocked entries that would otherwise
have been trades. This module closes that loop by:

1. Settling rejected candidates once their markets resolve.
2. Producing a compact runtime status snapshot for ops/dashboard surfaces.

The settled blocked-trade outcomes are intentionally kept separate from live lane
posteriors. They answer "what did the blocked trade do?" without polluting the
calibration stream for actually executed trades.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_REJECTED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates.jsonl"
DEFAULT_SETTLED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates_settled.jsonl"
DEFAULT_REGIME_LOG = DEFAULT_CALIBRATION_DIR / "market_regime.jsonl"
GAMMA_API = "https://gamma-api.polymarket.com"
RESOLVED_BUFFER_SEC = 90
REGIME_MATCH_MAX_AGE_SEC = 30 * 60
REGIME_FIELDS = (
    "price_regime",
    "polymarket_regime",
    "combined_regime",
    "regime_ts",
    "regime_match_age_sec",
    "regime_source",
)
REGIME_LABEL_FIELDS = ("price_regime", "polymarket_regime", "combined_regime")
REJECTED_COPY_FIELDS = (
    "btc_1h_regime",
    "context",
    "probe_variants",
    "policy_version",
    "feature_hash",
    "convergence_score",
    "convergence_probe_count",
    "convergence_pass_count",
    "convergence_fail_count",
    "convergence_narrow_pass_count",
    "convergence_strong_pass_count",
    "edge_quality",
    "component_mean_quality",
)


def ghost_id(rec: Dict[str, Any]) -> str:
    key = f"{rec.get('ts','')}|{rec.get('market_id','')}|{rec.get('reason','')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError as exc:
        logger.warning("rejected_candidate_tracker read failed (%s): %s", path, exc)


def _load_settled_ids(path: Path) -> set[str]:
    return {
        str(obj["ghost_id"])
        for obj in _iter_jsonl(path)
        if obj.get("ghost_id")
    }


def _load_rejected_by_id(path: Path) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in _iter_jsonl(path) or []:
        out[ghost_id(row)] = row
    return out


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_regime_snapshots(path: Path = DEFAULT_REGIME_LOG) -> List[Dict[str, Any]]:
    """Load market-regime snapshots sorted by timestamp."""
    snapshots: List[Dict[str, Any]] = []
    for row in _iter_jsonl(path) or []:
        ts = _parse_dt(row.get("ts"))
        if ts is None:
            continue
        snapshots.append({**row, "_parsed_ts": ts})
    snapshots.sort(key=lambda row: row["_parsed_ts"])
    return snapshots


def _nearest_regime_snapshot(
    target_ts: datetime,
    snapshots: Sequence[Dict[str, Any]],
    *,
    max_age_sec: float = REGIME_MATCH_MAX_AGE_SEC,
) -> Optional[Dict[str, Any]]:
    if not snapshots:
        return None

    lo = 0
    hi = len(snapshots)
    while lo < hi:
        mid = (lo + hi) // 2
        snap_ts = snapshots[mid].get("_parsed_ts")
        if not isinstance(snap_ts, datetime) or snap_ts < target_ts:
            lo = mid + 1
        else:
            hi = mid

    best: Optional[Dict[str, Any]] = None
    best_age: Optional[float] = None
    for idx in (lo - 1, lo):
        if idx < 0 or idx >= len(snapshots):
            continue
        snap = snapshots[idx]
        snap_ts = snap.get("_parsed_ts")
        if not isinstance(snap_ts, datetime):
            continue
        age = abs((target_ts - snap_ts).total_seconds())
        if age <= max_age_sec and (best_age is None or age < best_age):
            best = snap
            best_age = age
    return best


def enrich_with_regime(
    row: Dict[str, Any],
    snapshots: Sequence[Dict[str, Any]],
    *,
    max_age_sec: float = REGIME_MATCH_MAX_AGE_SEC,
    force: bool = False,
) -> Dict[str, Any]:
    """Return row with nearest market-regime labels for the ghost timestamp."""
    if not force and any(row.get(field) for field in REGIME_LABEL_FIELDS):
        return row

    out = dict(row)
    target_ts = _parse_dt(out.get("ts"))
    if target_ts is None:
        out.setdefault("regime_source", "missing_ts")
        return out

    snap = _nearest_regime_snapshot(target_ts, snapshots, max_age_sec=max_age_sec)
    if snap is None:
        out["regime_source"] = "unmatched"
        return out

    snap_ts = snap["_parsed_ts"]
    out["price_regime"] = snap.get("price_regime")
    out["polymarket_regime"] = snap.get("polymarket_regime")
    out["combined_regime"] = snap.get("combined_regime")
    out["regime_ts"] = snap.get("ts")
    out["regime_match_age_sec"] = round(abs((target_ts - snap_ts).total_seconds()), 3)
    out["regime_source"] = "market_regime"
    return out


def backfill_settled_regimes(
    *,
    input_path: Path = DEFAULT_SETTLED_LOG,
    output_path: Optional[Path] = None,
    regime_path: Path = DEFAULT_REGIME_LOG,
    rejected_path: Path = DEFAULT_REJECTED_LOG,
    max_age_sec: float = REGIME_MATCH_MAX_AGE_SEC,
    force: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Backfill regime labels onto settled ghost rows."""
    target_path = output_path or input_path
    summary = {
        "input_exists": input_path.exists(),
        "regime_exists": regime_path.exists(),
        "rows": 0,
        "matched": 0,
        "unmatched": 0,
        "already_labelled": 0,
        "rejected_metadata_copied": 0,
        "written": 0,
    }
    if not input_path.exists():
        return summary

    snapshots = load_regime_snapshots(regime_path)
    rejected_lookup = _load_rejected_by_id(rejected_path) if rejected_path.exists() else {}
    rows: List[Dict[str, Any]] = []
    for row in _iter_jsonl(input_path) or []:
        summary["rows"] += 1
        was_labelled = any(row.get(field) for field in REGIME_LABEL_FIELDS)
        rejected_src = rejected_lookup.get(str(row.get("ghost_id") or ""))
        metadata_copied = False
        enriched_row = dict(row)
        if rejected_src:
            for field in REJECTED_COPY_FIELDS:
                if force or not enriched_row.get(field):
                    if rejected_src.get(field) is not None:
                        enriched_row[field] = rejected_src.get(field)
                        metadata_copied = True
        if metadata_copied:
            summary["rejected_metadata_copied"] += 1
        if was_labelled and not force and not metadata_copied:
            summary["already_labelled"] += 1
            rows.append(enriched_row)
            continue
        enriched = enrich_with_regime(
            enriched_row,
            snapshots,
            max_age_sec=max_age_sec,
            force=force,
        )
        if enriched.get("regime_source") == "market_regime":
            summary["matched"] += 1
        else:
            summary["unmatched"] += 1
        rows.append(enriched)

    if dry_run:
        return summary

    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o644)
    try:
        for row in rows:
            clean = {k: v for k, v in row.items() if k != "_parsed_ts"}
            os.write(
                fd,
                (json.dumps(clean, separators=(",", ":")) + "\n").encode("utf-8"),
            )
            summary["written"] += 1
    finally:
        os.close(fd)
    os.replace(tmp_path, target_path)
    return summary


def fetch_resolution(
    market_id: str,
    cache: Dict[str, Optional[str]],
    *,
    timeout: float = 10.0,
) -> Optional[str]:
    """Return ``YES`` / ``NO`` / ``None`` for unresolved or unavailable."""
    if market_id in cache:
        return cache[market_id]
    try:
        resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=timeout)
        if resp.status_code != 200:
            cache[market_id] = None
            return None
        data = resp.json()
        if not data.get("closed", False):
            cache[market_id] = None
            return None
        resolution = data.get("resolution")
        if isinstance(resolution, str) and resolution.upper() in {"YES", "NO"}:
            cache[market_id] = resolution.upper()
            return cache[market_id]
        outcome_prices = data.get("outcomePrices")
        if outcome_prices:
            try:
                prices = (
                    json.loads(outcome_prices)
                    if isinstance(outcome_prices, str)
                    else outcome_prices
                )
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    if yes_price >= 0.99:
                        cache[market_id] = "YES"
                        return "YES"
                    if yes_price <= 0.01:
                        cache[market_id] = "NO"
                        return "NO"
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        cache[market_id] = None
        return None
    except requests.RequestException:
        cache[market_id] = None
        return None


def compute_would_be(
    *,
    action: str,
    yes_price: Optional[float],
    no_price: Optional[float],
    outcome: str,
) -> Dict[str, Any]:
    action = (action or "").upper()
    if action == "BUY_YES":
        entry = yes_price
        won = outcome == "YES"
    elif action == "BUY_NO":
        entry = no_price if no_price else ((1.0 - yes_price) if yes_price else None)
        won = outcome == "NO"
    else:
        return {
            "win": None,
            "realized_pct": None,
            "hypothetical_payout": None,
            "hypothetical_notional": None,
        }

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


def _ghost_to_live_lane_keys(rec: Dict[str, Any]) -> List[str]:
    """Map a rejected/ghost record's lane_id to the live lane_id key(s) that
    self-healing should update.

    Ghost records carry lane_id like ``bitcoin|5m|down|bearish|rejected`` or
    ``sol_macro|5m|down|bearish|rejected`` (single-segment regime tag, family
    always ``rejected``). Live trades use ``bitcoin|5m|down|bearish|standard``
    (BTC: single-segment regime, varied family) or
    ``sol_macro|5m|down|bearish__bearish__bull|standard`` (alt: 3-segment
    regime, varied family).

    Returns the list of candidate live lane_ids the ghost outcome should be
    applied to. Family is always ``standard`` (the dominant admission path);
    BTC keeps the single-segment regime; alts get a 3-segment regime built
    from the record's ``htf_bias`` + ``btc_1h_regime``. If insufficient
    metadata, returns an empty list (skip the update).
    """
    lid = str(rec.get("lane_id") or "")
    parts = lid.split("|")
    if len(parts) < 5:
        return []
    strategy, window, direction, _ghost_regime, _ghost_family = parts[:5]
    if not strategy or not window or not direction:
        return []
    if strategy == "bitcoin":
        # BTC live keys: bitcoin|<window>|<direction>|<single_regime>|standard
        regime = (_ghost_regime or "unknown").lower()
        return [f"bitcoin|{window}|{direction}|{regime}|standard"]
    # Alts: need 3-segment regime tag <alt_1h>__<alt_4h>__<btc>
    htf = str(rec.get("htf_bias") or "").strip().lower()
    btc_regime_raw = rec.get("btc_1h_regime")
    btc = str(btc_regime_raw or "").strip().lower() if btc_regime_raw else ""
    if not htf or not btc:
        return []
    # Heuristic: assume alt 4H tracks alt 1H. Live keys we saw in trades:
    # sol_macro|5m|down|bearish__bearish__bull|standard — alt_1h=bearish,
    # alt_4h=bearish, btc=bull. Build same shape.
    composite = f"{htf}__{htf}__{btc}"
    return [f"{strategy}|{window}|{direction}|{composite}|standard"]


def settle_rejected_candidates(
    *,
    input_path: Path = DEFAULT_REJECTED_LOG,
    output_path: Path = DEFAULT_SETTLED_LOG,
    regime_path: Path = DEFAULT_REGIME_LOG,
    regime_max_age_sec: float = REGIME_MATCH_MAX_AGE_SEC,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    throttle_sec: float = 0.0,
    calibrator: Optional[Any] = None,
    ghost_weight: float = 0.5,
) -> Dict[str, Any]:
    """Settle any newly resolvable ghost candidates.

    Idempotent: previously settled rows are skipped via ``ghost_id``.

    When ``calibrator`` is supplied, each newly-settled record's would-have-been
    outcome is fed to ``calibrator.record_ghost(lane_id, win, weight=ghost_weight)``
    so β-vetoed lanes can self-heal from observation alone. Default weight is
    0.5x of a live trade — ghost outcomes lack slippage/fills, so they
    shouldn't dominate β 1:1 with real trades.
    """
    summary = {
        "input_exists": input_path.exists(),
        "already_settled": 0,
        "too_recent": 0,
        "no_market_id": 0,
        "unresolved_or_api": 0,
        "newly_settled": 0,
        "regime_matched": 0,
        "regime_unmatched": 0,
        "written": 0,
        "ghost_beta_updates": 0,
    }
    if not input_path.exists():
        return summary

    settled_ids = _load_settled_ids(output_path)
    regime_snapshots = load_regime_snapshots(regime_path)
    cache: Dict[str, Optional[str]] = {}
    settle_records: List[Dict[str, Any]] = []
    ts_now = now or datetime.now(timezone.utc)

    for rec in _iter_jsonl(input_path):
        gid = ghost_id(rec)
        if gid in settled_ids:
            summary["already_settled"] += 1
            continue

        market_id = str(rec.get("market_id") or "")
        if not market_id:
            summary["no_market_id"] += 1
            continue

        end_ts = str(rec.get("market_end_ts") or "")
        if end_ts:
            try:
                end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                if (ts_now - end_dt).total_seconds() < RESOLVED_BUFFER_SEC:
                    summary["too_recent"] += 1
                    continue
            except ValueError:
                pass

        outcome = fetch_resolution(market_id, cache)
        if throttle_sec > 0:
            time.sleep(throttle_sec)
        if outcome is None:
            summary["unresolved_or_api"] += 1
            continue

        wb = compute_would_be(
            action=str(rec.get("action") or ""),
            yes_price=(
                float(rec["yes_price"])
                if rec.get("yes_price") not in (None, "")
                else None
            ),
            no_price=(
                float(rec["no_price"])
                if rec.get("no_price") not in (None, "")
                else None
            ),
            outcome=outcome,
        )
        settled_rec = {
            "ghost_id": gid,
            "settled_at": ts_now.isoformat(),
            "outcome": outcome,
            **wb,
            "ts": rec.get("ts"),
            "lane_id": rec.get("lane_id"),
            "strategy": rec.get("strategy"),
            "window": rec.get("window"),
            "side": rec.get("side"),
            "action": rec.get("action"),
            "reason": rec.get("reason"),
            "market_id": market_id,
            "market_question": rec.get("market_question"),
            "yes_price": rec.get("yes_price"),
            "no_price": rec.get("no_price"),
            "est_prob_up": rec.get("est_prob_up"),
            "htf_bias": rec.get("htf_bias"),
            "btc_1h_regime": rec.get("btc_1h_regime"),
            "context": rec.get("context", {}),
            "probe_variants": rec.get("probe_variants", []),
            "policy_version": rec.get("policy_version"),
            "feature_hash": rec.get("feature_hash"),
            "convergence_score": rec.get("convergence_score"),
            "convergence_probe_count": rec.get("convergence_probe_count"),
            "convergence_pass_count": rec.get("convergence_pass_count"),
            "convergence_fail_count": rec.get("convergence_fail_count"),
            "convergence_narrow_pass_count": rec.get("convergence_narrow_pass_count"),
            "convergence_strong_pass_count": rec.get("convergence_strong_pass_count"),
            "edge_quality": rec.get("edge_quality"),
            "component_mean_quality": rec.get("component_mean_quality"),
        }
        settled_rec = enrich_with_regime(
            settled_rec,
            regime_snapshots,
            max_age_sec=regime_max_age_sec,
        )
        if settled_rec.get("regime_source") == "market_regime":
            summary["regime_matched"] += 1
        else:
            summary["regime_unmatched"] += 1
        settle_records.append(settled_rec)
        summary["newly_settled"] += 1

        # Self-healing: feed ghost outcome into calibrator β at reduced weight,
        # translated to the LIVE lane_id key(s) so the veto check can actually
        # see the updates. Lets β-vetoed lanes climb back above the veto
        # threshold if their would-have-been WR recovers, without re-exposing
        # capital. Translation is heuristic (alt 4H assumed == alt 1H) — when
        # metadata is missing, the update is skipped.
        if calibrator is not None:
            try:
                win_val = wb.get("win") if isinstance(wb, dict) else None
                if isinstance(win_val, bool):
                    live_keys = _ghost_to_live_lane_keys(rec)
                    for live_lane_id in live_keys:
                        calibrator.record_ghost(
                            live_lane_id, win_val, weight=ghost_weight
                        )
                        summary["ghost_beta_updates"] += 1
            except Exception as _gpe:  # noqa: BLE001 — telemetry must not block settle
                logger.warning("ghost calibrator update skipped: %s", _gpe)

    if not dry_run and settle_records:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(output_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                for rec in settle_records:
                    os.write(
                        fd,
                        (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8"),
                    )
                    summary["written"] += 1
            finally:
                os.close(fd)
        except OSError as exc:
            logger.warning("rejected_candidate_tracker append failed (%s): %s", output_path, exc)

    return summary


def build_ghost_calibration_status(
    *,
    rejected_path: Path = DEFAULT_REJECTED_LOG,
    settled_path: Path = DEFAULT_SETTLED_LOG,
) -> Dict[str, Any]:
    """Return a compact status block for OPS_JSON / dashboard consumers."""
    rejected = list(_iter_jsonl(rejected_path) or [])
    settled = list(_iter_jsonl(settled_path) or [])
    total_rejected = len(rejected)
    total_settled = len(settled)
    unresolved = max(0, total_rejected - total_settled)

    wins = 0
    losses = 0
    by_reason_action: Dict[str, Dict[str, int]] = {}
    last_settled_at = ""
    for rec in settled:
        if rec.get("win") is True:
            wins += 1
        elif rec.get("win") is False:
            losses += 1
        reason = str(rec.get("reason") or "?")
        action = str(rec.get("action") or "?")
        key = f"{reason}|{action}"
        bucket = by_reason_action.setdefault(key, {"wins": 0, "losses": 0, "n": 0})
        if rec.get("win") is True:
            bucket["wins"] += 1
        elif rec.get("win") is False:
            bucket["losses"] += 1
        bucket["n"] += 1
        settled_at = str(rec.get("settled_at") or "")
        if settled_at and settled_at > last_settled_at:
            last_settled_at = settled_at

    ordered = sorted(
        by_reason_action.items(),
        key=lambda kv: kv[1]["n"],
        reverse=True,
    )[:10]
    total_decided = wins + losses

    # BTC 1H regime breakdown
    regime_stats: Dict[str, Dict[str, Any]] = {}
    for rec in settled:
        regime = str(rec.get("btc_1h_regime") or "UNKNOWN")
        bucket = regime_stats.setdefault(regime, {"wins": 0, "losses": 0, "n": 0})
        bucket["n"] += 1
        if rec.get("win") is True:
            bucket["wins"] += 1
        elif rec.get("win") is False:
            bucket["losses"] += 1
    regime_breakdown = {}
    for regime, stats in sorted(regime_stats.items()):
        decided = stats["wins"] + stats["losses"]
        regime_breakdown[regime] = {
            "n": stats["n"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "win_rate": round(stats["wins"] / decided, 4) if decided else None,
        }

    # Convergence score breakdown
    conv_buckets = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for rec in settled:
        score = rec.get("convergence_score")
        if score is None:
            conv_buckets["none"] += 1
        elif score >= 0.6:
            conv_buckets["high"] += 1
        elif score >= 0.4:
            conv_buckets["medium"] += 1
        else:
            conv_buckets["low"] += 1

    return {
        "rejected_log_exists": rejected_path.exists(),
        "settled_log_exists": settled_path.exists(),
        "total_rejected": total_rejected,
        "total_settled": total_settled,
        "unresolved": unresolved,
        "wins": wins,
        "losses": losses,
        "settled_win_rate": round(wins / total_decided, 4) if total_decided else None,
        "last_settled_at": last_settled_at or None,
        "top_reason_action": {
            key: value for key, value in ordered
        },
        "btc_1h_regime_breakdown": regime_breakdown,
        "convergence_score_breakdown": conv_buckets,
    }
