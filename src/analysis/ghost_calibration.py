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
from typing import Any, Dict, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_REJECTED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates.jsonl"
DEFAULT_SETTLED_LOG = DEFAULT_CALIBRATION_DIR / "rejected_candidates_settled.jsonl"
GAMMA_API = "https://gamma-api.polymarket.com"
RESOLVED_BUFFER_SEC = 90


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


def settle_rejected_candidates(
    *,
    input_path: Path = DEFAULT_REJECTED_LOG,
    output_path: Path = DEFAULT_SETTLED_LOG,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    throttle_sec: float = 0.0,
) -> Dict[str, Any]:
    """Settle any newly resolvable ghost candidates.

    Idempotent: previously settled rows are skipped via ``ghost_id``.
    """
    summary = {
        "input_exists": input_path.exists(),
        "already_settled": 0,
        "too_recent": 0,
        "no_market_id": 0,
        "unresolved_or_api": 0,
        "newly_settled": 0,
        "written": 0,
    }
    if not input_path.exists():
        return summary

    settled_ids = _load_settled_ids(output_path)
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
        settle_records.append(
            {
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
                "context": rec.get("context", {}),
                "probe_variants": rec.get("probe_variants", []),
                "policy_version": rec.get("policy_version"),
                "feature_hash": rec.get("feature_hash"),
            }
        )
        summary["newly_settled"] += 1

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
    }
