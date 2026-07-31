"""Near-window trade management — telemetry + (later) in-memory watchlist.

2026-07-30 operator plan. PHASE 1 (this file, LOGGING ONLY — no trading behavior
change): every ``lane_entry_window`` rejection is recorded to
``data/calibration/window_watch.jsonl`` with enough context to answer "are 5m/15m
markets checked too early, too late, or correctly-but-blocked?" — i.e. whether the
broad 60s scanner loop is missing the eligibility window for near-window markets.

Later phases (scaffolded here, wired elsewhere): an in-memory WindowWatchItem
registry (P2), a 10–15s fast-recheck loop over near-window markets that reuses the
EXISTING strategy scan path (P3, dry-run behind ``fast_recheck_enabled``), conflict
buckets (P4), and OPS_JSON stats (P5).

Fail-safe by construction: any error here is swallowed — telemetry must NEVER break
the scan or change a trading decision.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG = _ROOT / "data" / "calibration" / "window_watch.jsonl"

_ASSET_BY_STRATEGY = {
    "bitcoin": "BTC",
    "sol_macro": "SOL",
    "eth_macro": "ETH",
    "xrp_macro": "XRP",
    "bnb_macro": "BNB",
    "doge_macro": "DOGE",
    "hype_macro": "HYPE",
}


def _asset_from_strategy(strategy: Optional[str]) -> str:
    s = str(strategy or "")
    return _ASSET_BY_STRATEGY.get(s, s.replace("_macro", "").upper() or "?")


def _cfg(config: Optional[dict]) -> dict:
    return ((config or {}).get("trading") or {}).get("window_watch") or {}


def is_enabled(config: Optional[dict]) -> bool:
    return bool(_cfg(config).get("enabled", False))


def _seconds_until_eligible(
    eval_mins_left: Optional[float],
    entry_window_min: Optional[float],
    entry_window_max: Optional[float],
) -> Optional[float]:
    """When (secs from now) should this market next be checked? Eligible band is
    ``entry_window_min <= mins_left <= entry_window_max``. If mins_left is still ABOVE
    the max, it becomes eligible as time-to-expiry decays to the max. If already BELOW
    the min, the window has passed (return None — nothing to wait for)."""
    if eval_mins_left is None or entry_window_max is None or entry_window_min is None:
        return None
    if eval_mins_left > entry_window_max:
        return round((eval_mins_left - entry_window_max) * 60.0, 1)
    if eval_mins_left < entry_window_min:
        return None  # past the window — missed / expired
    return 0.0  # currently inside the eligible band


def log_window_reject(
    config: Optional[dict],
    *,
    market: Any,
    strategy: Optional[str],
    window: Optional[str],
    side: Optional[str] = None,
    action: Optional[str] = None,
    side_source: Optional[str] = None,
    conflict_type: Optional[str] = None,
    eval_mins_left: Optional[float] = None,
    entry_window_min: Optional[float] = None,
    entry_window_max: Optional[float] = None,
    yes_price: Optional[float] = None,
    est_prob: Optional[float] = None,
    edge: Optional[float] = None,
    reason: str = "lane_entry_window",
    log_path: Path = DEFAULT_LOG,
) -> None:
    """PHASE 1: append one window_watch row for a near-window rejection. No-op unless
    ``trading.window_watch.enabled`` and this window+strategy are in the configured
    lists. Never raises."""
    try:
        ww = _cfg(config)
        if not ww.get("enabled", False):
            return
        win = str(window or "")
        windows = {str(x) for x in (ww.get("windows") or ["5m", "15m"])}
        if win not in windows:
            return
        strat = str(strategy or "")
        strategies = {str(x) for x in (ww.get("strategies") or [])}
        if strategies and strat not in strategies:
            return

        end_date = getattr(market, "end_date", None)
        market_end_ts = end_date.isoformat() if isinstance(end_date, datetime) else None
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market_id": str(getattr(market, "id", "") or ""),
            "market_question": str(getattr(market, "question", "") or "")[:120],
            "strategy": strat,
            "asset": _asset_from_strategy(strat),
            "window": win,
            "market_end_ts": market_end_ts,
            "eval_mins_left": (round(float(eval_mins_left), 3) if eval_mins_left is not None else None),
            "entry_window_min": (float(entry_window_min) if entry_window_min is not None else None),
            "entry_window_max": (float(entry_window_max) if entry_window_max is not None else None),
            "seconds_until_eligible": _seconds_until_eligible(
                eval_mins_left, entry_window_min, entry_window_max
            ),
            "last_reason": str(reason or ""),
            "side": (str(side) if side is not None else None),
            "action": (str(action) if action is not None else None),
            "side_source": (str(side_source) if side_source is not None else None),
            "conflict_type": (str(conflict_type) if conflict_type is not None else None),
            "price": (round(float(yes_price), 4) if yes_price is not None else None),
            "est_prob": (round(float(est_prob), 4) if est_prob is not None else None),
            "edge": (round(float(edge), 4) if edge is not None else None),
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # open-per-write O_APPEND is atomic for a single line; matches the reject logger.
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        # PHASE 2: also update the in-memory near-window registry (observability only —
        # NOT execution; the fast-recheck loop that consumes it stays behind
        # fast_recheck_enabled). No-op-safe.
        _registry_observe(ww, row)
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the scan
        logger.debug("window_watch log failed (ignored): %s", exc)


# ---------------------------------------------------------------------------
# PHASE 2: in-memory near-window registry (+ PHASE 5 stats). Module-level state so
# the strategy scan loop can populate it via log_window_reject() without threading a
# handle through every call site. Bounded by max_watch_items; swept for staleness.
# This is OBSERVABILITY: it answers "how many near-window markets are we tracking, and
# how long AFTER a market becomes eligible do we first re-check it?" (the key metric —
# a high delay means the broad 60s scan is missing live frequency). It does NOT execute.
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, dict] = {}
_ENTERED = 0  # watched markets later seen as entries (wired in P3/entry hook; 0 for now)


def _registry_observe(ww: dict, row: dict) -> None:
    """Add/update the registry from a near-window reject row. Rules (operator plan):
    track while ``eval_mins_left`` is still ABOVE the window (too-early); refresh
    last-seen each scan; the sweep drops items once they fall below the window (missed)
    or go stale (traded/expired → no longer rejected)."""
    mid = row.get("market_id")
    if not mid:
        return
    emax = row.get("entry_window_max")
    eml = row.get("eval_mins_left")
    # Only track markets that are still approaching the window (too-early). A reject with
    # eval already inside/below the band is a different problem (edge/side/tape), not timing.
    if emax is None or eml is None or eml <= float(emax):
        return
    max_items = int(ww.get("max_watch_items", 80) or 80)
    now = time.time()
    item = _REGISTRY.get(mid)
    if item is None:
        if len(_REGISTRY) >= max_items:
            return  # bounded — drop new items past the cap (swept items free slots)
        item = {
            "market_id": mid, "strategy": row.get("strategy"), "asset": row.get("asset"),
            "window": row.get("window"), "market_end_ts": row.get("market_end_ts"),
            "entry_window_min": row.get("entry_window_min"),
            "entry_window_max": emax, "first_seen_ts": now,
            "first_seen_mins_left": eml,
        }
        _REGISTRY[mid] = item
    item.update({
        "last_seen_ts": now,
        "last_eval_mins_left": eml,
        "seconds_until_eligible": row.get("seconds_until_eligible"),
        "last_reason": row.get("last_reason"),
        "last_side": row.get("side"),
        "last_action": row.get("action"),
    })


def registry_stats(config: Optional[dict], *, stale_after_sec: float = 60.0) -> dict:
    """PHASE 5: sweep the registry and return OPS_JSON stats. Removes items that fell
    below their window (missed) or went stale (no reject in ``stale_after_sec`` — market
    traded or resolved). Returns active/due_now/expired/last_reasons/avg_delay_sec."""
    global _REGISTRY
    ww = _cfg(config)
    buffer_sec = float(ww.get("near_window_buffer_sec", 90) or 90)
    now = time.time()
    expired = 0
    due_now = 0
    delays: list[float] = []
    reasons: dict[str, int] = {}
    survivors: dict[str, dict] = {}
    for mid, it in _REGISTRY.items():
        emin = it.get("entry_window_min")
        emax = it.get("entry_window_max")
        eml = it.get("last_eval_mins_left")
        stale = (now - float(it.get("last_seen_ts", now))) > stale_after_sec
        past = (emin is not None and eml is not None and eml < float(emin))
        if stale or past:
            expired += 1
            continue
        survivors[mid] = it
        # "due now" = within the fast-recheck band: mins_left in [emin, emax + buffer].
        if emin is not None and emax is not None and eml is not None:
            if float(emin) <= eml <= float(emax) + buffer_sec / 60.0:
                due_now += 1
        sue = it.get("seconds_until_eligible")
        if isinstance(sue, (int, float)):
            delays.append(float(sue))
        r = str(it.get("last_reason") or "?")
        reasons[r] = reasons.get(r, 0) + 1
    _REGISTRY = survivors
    return {
        "window_watch_active": len(survivors),
        "window_watch_due_now": due_now,
        "window_watch_entered": _ENTERED,
        "window_watch_expired": expired,
        "window_watch_last_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:6]),
        "window_watch_avg_secs_until_eligible": (round(sum(delays) / len(delays), 1) if delays else None),
    }


@dataclass
class WindowWatchItem:
    """PHASE 2 scaffold (not yet wired): one near-window market being tracked so the
    fast-recheck loop can re-evaluate it right as it becomes eligible, instead of
    waiting on the next broad 60s scan."""
    market_id: str
    strategy: str
    window: str
    market_question: str
    market_end_ts: Optional[datetime]
    token_id_yes: str
    token_id_no: str
    entry_window_min: float
    entry_window_max: float
    first_seen_ts: datetime
    first_seen_mins_left: float
    eligible_at_ts: Optional[datetime]
    last_checked_ts: Optional[datetime] = None
    last_reason: str = ""
    last_side: Optional[str] = None
    last_action: Optional[str] = None
