"""Per-lane directional circuit breaker (2026-08-05).

Shadow-proven (directional_breaker_shadow, 32 sessions / 2435 trades): a per-lane
consecutive-stop breaker is a WASH globally (+$8.80) but decisive PER LANE. This module
promotes it live for an operator-approved ENABLE-SET only: after `k` consecutive stop-outs
(updown_stop_loss / never_green_cut) on a lane, PAUSE new entries on that lane for
`cooldown_min` minutes (its edge has broken); auto-resume after cooldown; reset the counter
on any win (take_profit).

DESIGN
- State (consec-stop count + cooldown-until) persists to data/calibration/lane_breaker_state.json
  so a restart does NOT reset a live cooldown or a mid-streak count.
- OFF unless config trading.lane_breaker.enabled AND the lane is explicitly listed. A lane not
  in the config list is NEVER touched (the hard-exclude lanes — doge|5m|down etc. — simply
  aren't listed). Reversible (enabled:false or drop a lane).
- Fail-open everywhere: any error => not blocked, never breaks admission/exit.

Config (config/settings.yaml trading.lane_breaker):
  enabled: true
  default_k: 3
  default_cooldown_min: 45
  lanes:
    "xrp_macro|5m|BUY_NO":  {k: 3, cooldown_min: 45}
    ...
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = _ROOT / "data" / "calibration" / "lane_breaker_state.json"

# Exit reasons that count as a "stop" (edge-break signal). A win (take_profit) or any
# other benign close resets the streak.
_STOP_REASONS = {"updown_stop_loss", "never_green_cut", "updown_time_stop"}
_WIN_RESET_REASONS = {"take_profit"}

# In-memory mirror of the persisted state: {lane: {"consec": int, "cooldown_until": epoch}}.
_STATE: Dict[str, Dict[str, float]] = {}
_LOADED = False


def _lane_key(strategy: str, window: Any, action: str) -> str:
    return "%s|%s|%s" % (strategy, window, action)


def _cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return ((config or {}).get("trading") or {}).get("lane_breaker") or {}


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text())
            for lane, st in (data.get("lanes") or {}).items():
                _STATE[lane] = {
                    "consec": float(st.get("consec", 0) or 0),
                    "cooldown_until": float(st.get("cooldown_until", 0) or 0),
                }
    except Exception as e:  # fail-open: start empty
        logger.debug("lane_breaker load error: %s", e)
    _LOADED = True


def _persist() -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps({"lanes": _STATE}, indent=2))
        tmp.replace(STATE_PATH)
    except Exception as e:
        logger.debug("lane_breaker persist error: %s", e)


def _lane_params(config: Dict[str, Any], lane: str) -> Optional[Tuple[int, float]]:
    """Return (k, cooldown_sec) if the lane is enabled for the breaker, else None."""
    c = _cfg(config)
    if not bool(c.get("enabled", False)):
        return None
    lanes = c.get("lanes") or {}
    if lane not in lanes:
        return None
    ov = lanes.get(lane) or {}
    k = int(ov.get("k", c.get("default_k", 3)) or 3)
    cd_min = float(ov.get("cooldown_min", c.get("default_cooldown_min", 45)) or 45)
    return max(1, k), cd_min * 60.0


def record_exit(config: Dict[str, Any], *, strategy: str, window: Any, action: str,
                exit_reason: str, now: Optional[float] = None) -> None:
    """Update the lane's consecutive-stop streak from a settled exit.

    Only tracks lanes that are enabled for the breaker (others are ignored entirely).
    On the k-th consecutive stop, arm the cooldown. A win resets the streak + clears cooldown.
    """
    try:
        lane = _lane_key(strategy, window, action)
        params = _lane_params(config, lane)
        if params is None:
            return
        _load()
        k, cd_sec = params
        now = time.time() if now is None else now
        st = _STATE.setdefault(lane, {"consec": 0.0, "cooldown_until": 0.0})
        er = str(exit_reason or "")
        if er in _WIN_RESET_REASONS:
            st["consec"] = 0.0
            st["cooldown_until"] = 0.0
        elif er in _STOP_REASONS:
            st["consec"] = st.get("consec", 0.0) + 1.0
            if st["consec"] >= k:
                st["cooldown_until"] = now + cd_sec
                logger.info(
                    "LANE_BREAKER armed %s: %d consecutive stops >= k%d -> cooldown %.0fmin",
                    lane, int(st["consec"]), k, cd_sec / 60.0,
                )
        else:
            return  # benign exit (expired/flatten): neither stop nor win-reset
        _persist()
    except Exception as e:
        logger.debug("lane_breaker record_exit error: %s", e)


def is_blocked(config: Dict[str, Any], *, strategy: str, window: Any, action: str,
               now: Optional[float] = None) -> bool:
    """True if this lane is currently in breaker cooldown (skip the entry)."""
    try:
        lane = _lane_key(strategy, window, action)
        if _lane_params(config, lane) is None:
            return False
        _load()
        st = _STATE.get(lane)
        if not st:
            return False
        now = time.time() if now is None else now
        return float(st.get("cooldown_until", 0) or 0) > now
    except Exception as e:
        logger.debug("lane_breaker is_blocked error: %s", e)
        return False


def active_cooldowns(now: Optional[float] = None) -> Dict[str, float]:
    """Diagnostic: {lane: seconds_remaining} for lanes currently cooling down."""
    _load()
    now = time.time() if now is None else now
    out = {}
    for lane, st in _STATE.items():
        rem = float(st.get("cooldown_until", 0) or 0) - now
        if rem > 0:
            out[lane] = round(rem, 0)
    return out
