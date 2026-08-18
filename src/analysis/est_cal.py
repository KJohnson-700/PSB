"""est_prob calibration CONSUMER — the sizing hook Phase A never had (2026-08-18).

Phase A (scripts/est_calibration_report.py) fits the hierarchical shrink

    p_cal = price + k * (claimed - price)

and the tooling daemon refits data/calibration/est_prob_calibration.json on the
settle tick — but NOTHING in the bot ever read that state (verified: zero src/
consumers on 2026-08-18 while the file refit on cadence). This module is the
missing consumer, wired into the three true-Kelly sizing sites (sol_macro
family, eth_macro, bitcoin). SIZING ONLY — admission edge keeps the raw claim;
this file is imported after the entry has already passed its gates.

SELF-ARMING GRADUATION (pre-registered in the Phase A tool, not invented here):
the shrink is applied only while the POOLED walk-forward record proves it —
calibrated Brier must beat BOTH raw and market on >= MIN_WALKFORWARD_N
out-of-sample graded trades (n-weighted across walkforward_history batches).
Until the record clears the bar, sized_win_prob returns the raw prob untouched
and reports why. No human flip, no feature shipped OFF: the gate arms itself
the moment the evidence exists, and DISARMS itself if the record decays.

Fail-open everywhere: any read/parse problem returns the raw prob. This sits
on the entry path and must never raise into a scan loop.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

STATE_PATH = os.path.join("data", "calibration", "est_prob_calibration.json")
MIN_WALKFORWARD_N = 150   # graduation gate C from the Phase A pre-registration
_RECHECK_SEC = 60.0       # state file mtime poll cadence

_cache: Dict[str, Any] = {"checked": 0.0, "mtime": None, "state": None, "gate": None}
_logged_status: Dict[str, bool] = {}


def _load_state() -> Optional[Dict[str, Any]]:
    now = time.time()
    if now - _cache["checked"] < _RECHECK_SEC:
        return _cache["state"]
    _cache["checked"] = now
    try:
        mtime = os.stat(STATE_PATH).st_mtime
    except OSError:
        _cache.update({"state": None, "gate": None})
        return None
    if mtime == _cache["mtime"] and _cache["state"] is not None:
        return _cache["state"]
    try:
        with open(STATE_PATH) as fh:
            state = json.load(fh)
    except Exception:
        return _cache["state"]  # keep last good state on a torn read
    _cache.update({"mtime": mtime, "state": state, "gate": _gate(state)})
    return state


def _gate(state: Dict[str, Any]) -> Dict[str, Any]:
    """Pooled walk-forward verdict: {'armed': bool, 'n': int, 'why': str}."""
    hist = state.get("walkforward_history") or []
    n = 0
    mkt = raw = cal = 0.0
    for batch in hist:
        try:
            bn = int(batch.get("n") or 0)
            if bn <= 0:
                continue
            mkt += float(batch["market"]) * bn
            raw += float(batch["raw"]) * bn
            cal += float(batch["calibrated"]) * bn
            n += bn
        except (KeyError, TypeError, ValueError):
            continue
    if n < MIN_WALKFORWARD_N:
        return {"armed": False, "n": n,
                "why": f"walkforward n={n} < {MIN_WALKFORWARD_N}"}
    mkt, raw, cal = mkt / n, raw / n, cal / n
    if cal < raw and cal < mkt:
        return {"armed": True, "n": n,
                "why": f"walkforward n={n}: cal {cal:.4f} beats raw {raw:.4f} and market {mkt:.4f}"}
    return {"armed": False, "n": n,
            "why": f"walkforward n={n}: cal {cal:.4f} does NOT beat raw {raw:.4f} / market {mkt:.4f}"}


def _leaf_k(groups: Dict[str, Any], strategy: str, window: str, action: str) -> float:
    for key in (f"{strategy}|{window}|{action}", f"{strategy}|{window}", strategy, "GLOBAL"):
        g = groups.get(key)
        if isinstance(g, dict) and g.get("k") is not None:
            try:
                return max(0.0, min(1.0, float(g["k"])))
            except (TypeError, ValueError):
                continue
    return 1.0  # unknown family: k=1 == raw claim == no-op


def gate_status() -> Dict[str, Any]:
    """For probation/diagnostics: current arm state without touching sizing."""
    state = _load_state()
    if state is None:
        return {"armed": False, "n": 0, "why": "state file missing/unreadable"}
    return dict(_cache.get("gate") or {"armed": False, "n": 0, "why": "no gate computed"})


def sized_win_prob(win_prob: float, our_price: float, strategy: str,
                   window: str, action: str) -> Tuple[float, str]:
    """(possibly-calibrated win_prob, status) for the Kelly sizing call.

    status: 'applied k=..' | 'gated: <why>' | 'off: <why>'. Never raises.
    """
    try:
        state = _load_state()
        if state is None:
            return win_prob, "off: no state"
        gate = _cache.get("gate") or {}
        if not gate.get("armed"):
            why = str(gate.get("why") or "ungraduated")
            if not _logged_status.get("gated"):
                _logged_status["gated"] = True
                logger.info("[est-cal] sizing consumer LOADED but GATED — %s", why)
            return win_prob, f"gated: {why}"
        k = _leaf_k(state.get("groups") or {}, str(strategy), str(window), str(action))
        p = float(our_price) + k * (float(win_prob) - float(our_price))
        p = min(0.99, max(0.01, p))
        if not _logged_status.get("armed"):
            _logged_status["armed"] = True
            logger.warning("[est-cal] sizing consumer ARMED — %s", gate.get("why"))
        return p, f"applied k={k:.3f}"
    except Exception as exc:  # fail-open: sizing must never break on calibration
        return win_prob, f"off: {type(exc).__name__}"
