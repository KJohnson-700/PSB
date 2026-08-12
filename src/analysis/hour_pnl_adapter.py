"""ADAPTIVE per-hour sit-out driven by REALIZED pnl — hours self-flip in and out.

Replaces the static ``risk.blocked_pt_hours`` list with a measurement: each entry-hour (PT)
is scored on its rolling realized $/trade over the RECENT era, and an hour sits out only while
it is actually bleeding. When it recovers, it comes back on its own. No fixed trading schedule.

WHY THIS SHAPE (operator rules baked in):
  * REALIZED only. Scores come from closed trades in ``data/paper_trades/*/entries.jsonl``
    (ENTRY joined to EXIT by trade_id), never ghosts/EV.
  * RECENT ERA ONLY. Pooling stale sessions poisons the read (a dead-config era can brand a
    healthy hour as a bleeder). ``lookback_sessions`` keeps it to the newest N sessions.
  * BASELINE-RELATIVE. Hours are scored against the SAME-ERA overall $/trade, not an absolute
    line. Measured: with the bot broadly negative an absolute cut gated 13 of 24 hours (that is
    overall expectancy leaking into every bucket, not an hour effect). Relative scoring isolates
    true outliers and keeps working unchanged once overall expectancy turns positive.
  * HYSTERESIS. Block at ``sit_out_delta``, release at the looser ``recover_delta`` so an hour
    hovering at the line does not flap on/off every refresh.
  * FREQUENCY FLOOR. ``max_blocked_hours`` caps how much of the clock can ever sit out.
  * MIN SAMPLES. An hour with too few closes is never gated (fail-open), so a quiet hour is
    not mistaken for a bad one.
  * FAIL-OPEN EVERYWHERE. Any error => nothing blocked. This sits on the entry path and must
    never raise into a scan loop or silently starve the bot.

CONFIG (``config/settings.yaml`` -> ``risk.hour_adapter``; hot-reloadable, ``risk`` is a
hot-reload top-level key)::

    risk:
      blocked_pt_hours: []             # MANUAL pin list; unioned with the adaptive set
      hour_adapter:
        enabled: true
        lookback_sessions: 40   # recent-era window (newest N sessions with closes)
        min_samples: 25         # closes needed in an hour before it may be gated
        sit_out_delta: -1.50    # hour avg THIS FAR BELOW the same-era baseline => sit out
        recover_delta: -0.50    # ...stays out until it climbs back above this (hysteresis)
        max_blocked_hours: 4    # hard frequency floor (worst-first)
        refresh_sec: 900        # recompute cadence (cheap: tail of the journals)

``blocked_hours()`` returns the union of the manual list and the adaptive set, so the operator
can always pin an hour by hand without touching code.
"""

from __future__ import annotations

import glob
import json
import os
import time
from collections import defaultdict
from typing import Any, Dict, Set, Tuple

_JOURNAL_GLOB = "data/paper_trades/test_*/entries.jsonl"

# in-process cache: recompute at most once per refresh_sec
_cache: Dict[str, Any] = {"ts": 0.0, "blocked": frozenset(), "stats": {}}
# hours currently gated by the adapter (hysteresis state — an hour stays here until it recovers)
_gated: Set[int] = set()


def _pt_hour(iso_ts: str) -> int:
    """PT hour from a journal ISO timestamp (journals are UTC). -1 when unparseable."""
    try:
        # 'YYYY-MM-DDTHH:MM:SS...' -> UTC hour, then shift to PT (UTC-7)
        return (int(iso_ts[11:13]) - 7) % 24
    except (ValueError, TypeError, IndexError):
        return -1


def _collect(lookback_sessions: int) -> Dict[int, Tuple[int, float]]:
    """{pt_hour: (n_closes, net_pnl)} over the newest ``lookback_sessions`` session dirs."""
    paths = sorted(glob.glob(_JOURNAL_GLOB), key=lambda p: os.path.basename(os.path.dirname(p)))
    if lookback_sessions > 0:
        paths = paths[-lookback_sessions:]
    agg: Dict[int, Tuple[int, float]] = {}
    for path in paths:
        try:
            entries: Dict[str, str] = {}
            rows = []
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    ev = r.get("event")
                    if ev == "ENTRY":
                        tid = r.get("trade_id")
                        if tid:
                            entries[tid] = r.get("timestamp") or ""
                    elif ev == "EXIT":
                        rows.append(r)
            for x in rows:
                ts = entries.get(x.get("trade_id"))
                if not ts:
                    continue
                h = _pt_hour(ts)
                if h < 0:
                    continue
                try:
                    pnl = float(x.get("pnl") or 0.0)
                except (TypeError, ValueError):
                    continue
                n, net = agg.get(h, (0, 0.0))
                agg[h] = (n + 1, net + pnl)
        except OSError:
            continue
    return agg


def refresh(config: Any, *, force: bool = False) -> Dict[str, Any]:
    """Recompute the adaptive set if stale. Returns the stats dict. Never raises."""
    try:
        get = config.get if hasattr(config, "get") else (lambda k, d=None: d)
        rcfg = (get("risk", {}) or {})
        acfg = (rcfg.get("hour_adapter") or {}) if isinstance(rcfg, dict) else {}
        if not bool(acfg.get("enabled", False)):
            _cache.update({"ts": time.time(), "blocked": frozenset(), "stats": {}})
            _gated.clear()
            return {}
        refresh_sec = float(acfg.get("refresh_sec", 900) or 900)
        now = time.time()
        if not force and (now - float(_cache.get("ts") or 0.0)) < refresh_sec:
            return _cache.get("stats") or {}

        lookback = int(acfg.get("lookback_sessions", 10) or 10)
        min_n = int(acfg.get("min_samples", 15) or 15)
        sit_out = float(acfg.get("sit_out_delta", -1.50))
        recover = float(acfg.get("recover_delta", -0.50))

        max_blocked = int(acfg.get("max_blocked_hours", 4) or 4)
        agg = _collect(lookback)

        # BASELINE-RELATIVE SCORING. Absolute $/trade thresholds are useless here: when the bot
        # is broadly negative, EVERY hour prints a negative avg and an absolute cut gates half
        # the clock (measured: 13/24 hours) — that is the bot's overall expectancy leaking into
        # each bucket, not an hour effect. Score each hour against the SAME-ERA baseline so we
        # isolate hours that are genuinely worse than the bot's own norm, and so the gate keeps
        # working unchanged once overall expectancy turns positive.
        _tot_n = sum(n for n, _ in agg.values())
        _tot_net = sum(net for _, net in agg.values())
        baseline = (_tot_net / _tot_n) if _tot_n else 0.0

        stats: Dict[str, Any] = {}
        eligible = []
        for h, (n, net) in sorted(agg.items()):
            avg = net / n if n else 0.0
            delta = avg - baseline
            stats[str(h)] = {"n": n, "net": round(net, 2), "avg": round(avg, 3),
                             "delta_vs_baseline": round(delta, 3)}
            if n < min_n:
                _gated.discard(h)      # not enough evidence -> never gate (fail-open)
                continue
            eligible.append((h, delta))

        # candidates: gated hours hold until they recover past the looser line (hysteresis)
        keep = {h for h, d in eligible if h in _gated and d <= recover}
        fresh = {h for h, d in eligible if h not in _gated and d <= sit_out}
        for h, _ in eligible:
            if h in _gated and h not in keep:
                _gated.discard(h)
        cand = keep | fresh

        # HARD FREQUENCY FLOOR: never sit out more than max_blocked_hours; keep only the worst.
        if len(cand) > max_blocked:
            worst = sorted(cand, key=lambda h: dict(eligible).get(h, 0.0))[:max_blocked]
            cand = set(worst)
        _gated.clear()
        _gated.update(cand)
        stats["_baseline"] = {"avg": round(baseline, 3), "n": _tot_n}
        _cache.update({"ts": now, "blocked": frozenset(_gated), "stats": stats})
        return stats
    except Exception:
        # fail-open: never let a scoring error gate the bot
        _cache.update({"ts": time.time(), "blocked": frozenset()})
        return {}


def blocked_hours(config: Any) -> Set[int]:
    """PT hours to sit out = MANUAL ``risk.blocked_pt_hours`` UNION the adaptive set."""
    manual: Set[int] = set()
    try:
        get = config.get if hasattr(config, "get") else (lambda k, d=None: d)
        rcfg = (get("risk", {}) or {})
        if isinstance(rcfg, dict):
            manual = {int(h) for h in (rcfg.get("blocked_pt_hours") or [])}
    except Exception:
        manual = set()
    try:
        refresh(config)
        return manual | set(_cache.get("blocked") or ())
    except Exception:
        return manual


def current_pt_hour(now: float | None = None) -> int:
    t = time.gmtime(now if now is not None else time.time())
    return (t.tm_hour - 7) % 24


def is_blocked_now(config: Any) -> bool:
    try:
        return current_pt_hour() in blocked_hours(config)
    except Exception:
        return False


def describe(config: Any) -> Dict[str, Any]:
    """Diagnostic snapshot for logs/CLI: what is gated and why."""
    stats = refresh(config, force=True)
    return {
        "adaptive_blocked": sorted(_cache.get("blocked") or ()),
        "manual_blocked": sorted(blocked_hours(config) - set(_cache.get("blocked") or ())),
        "effective_blocked": sorted(blocked_hours(config)),
        "current_pt_hour": current_pt_hour(),
        "per_hour": stats,
    }
