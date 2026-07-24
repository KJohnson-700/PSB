"""Per-lane NO-SIGNAL entry gate (P0, 2026-07-03).

Consumes the enhanced_price_tracker snapshots (data/calibration/market_regime.jsonl,
15-min cron). Live-validated 2026-07-03 on n=3199 realized trades (5 weeks):
entries opened while combined_regime == "deadzone_confirmed" (7-asset spot vol
< 0.5% AND Polymarket 5m/15m odds clustered < 4pp around 0.50) realized
-$745.92 / WR 0.334 (n=1300) vs +$688.01 / WR 0.47 outside the condition.
9 lanes are POSITIVE inside the condition, so this gate blocks ONLY the lanes
listed in config — it is a per-lane conditional pause, not a lane cut. Lanes
resume automatically when the odds spread reopens.

Semantics:
- entry-only: exits/stops/settlement are never touched.
- hysteresis: `confirm_ticks` consecutive matching snapshots to enter the
  paused state, and the same count of clear snapshots to leave it (anti-flap).
- FAIL-OPEN: if the tracker file is missing, unparsable, or stale
  (> max_age_sec), the gate deactivates. A dead cron must never freeze entries.

Config (settings.yaml root):
no_signal_gate:
  enabled: true
  regime_file: data/calibration/market_regime.jsonl
  confirm_ticks: 2
  max_age_sec: 2100          # 35 min = 2 missed 15-min cron ticks
  cache_ttl_sec: 60
  blocked_lanes:             # "<strategy>|<window>|<up|down>"
    - hype_macro|5m|up
    - ...
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_PATH = _REPO_ROOT / "config" / "settings.yaml"

_NO_SIGNAL_LABEL = "deadzone_confirmed"  # legacy label written by enhanced_price_tracker

_cfg_cache: dict = {"ts": 0.0, "cfg": {}}
_state_cache: dict = {"ts": 0.0, "active": False, "logged_state": None}


def _load_cfg() -> dict:
    now = time.time()
    ttl = float(_cfg_cache["cfg"].get("cache_ttl_sec", 60) or 60) if _cfg_cache["cfg"] else 60.0
    if now - _cfg_cache["ts"] < ttl:
        return _cfg_cache["cfg"]
    cfg: dict = {}
    try:
        import yaml

        with open(_SETTINGS_PATH) as f:
            root = yaml.safe_load(f) or {}
        cfg = root.get("no_signal_gate") or {}
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception as e:  # fail-open
        logger.warning("no_signal_gate: config load failed (%s) — gate inactive", e)
        cfg = {}
    _cfg_cache["cfg"] = cfg
    _cfg_cache["ts"] = now
    return cfg


def _replay_hysteresis(regimes: list, confirm: int) -> bool:
    """Replay the tail of snapshot labels through the two-sided hysteresis."""
    active = False
    run_match = 0
    run_clear = 0
    for r in regimes:
        if r == _NO_SIGNAL_LABEL:
            run_match += 1
            run_clear = 0
        else:
            run_clear += 1
            run_match = 0
        if not active and run_match >= confirm:
            active = True
        elif active and run_clear >= confirm:
            active = False
    return active


def no_signal_active() -> bool:
    """True when the no-signal condition is confirmed (hysteresis applied)."""
    cfg = _load_cfg()
    if not cfg.get("enabled", False):
        return False
    now = time.time()
    if now - _state_cache["ts"] < float(cfg.get("cache_ttl_sec", 60) or 60):
        return _state_cache["active"]

    active = False
    try:
        path = Path(cfg.get("regime_file", "data/calibration/market_regime.jsonl"))
        if not path.is_absolute():
            path = _REPO_ROOT / path
        rows = []
        with open(path) as f:
            for line in f.readlines()[-48:]:
                try:
                    d = json.loads(line)
                    raw_ts = str(d["ts"]).replace("Z", "+00:00")
                    ts = datetime.fromisoformat(raw_ts)
                    if ts.tzinfo is None:  # tracker writes UTC; normalize naive
                        ts = ts.replace(tzinfo=timezone.utc)
                    rows.append((ts, d.get("combined_regime")))
                except Exception:
                    continue
        if rows:
            rows.sort()
            age = (datetime.now(timezone.utc) - rows[-1][0]).total_seconds()
            if age <= float(cfg.get("max_age_sec", 2100) or 2100):
                confirm = max(1, int(cfg.get("confirm_ticks", 2) or 2))
                active = _replay_hysteresis([r for _, r in rows], confirm)
            else:
                logger.warning(
                    "no_signal_gate: newest snapshot %.0fs old (> max_age) — gate FAIL-OPEN",
                    age,
                )
    except Exception as e:  # fail-open
        logger.warning("no_signal_gate: snapshot read failed (%s) — gate inactive", e)
        active = False

    if _state_cache["logged_state"] != active:
        logger.info("NO_SIGNAL_GATE state -> %s", "ACTIVE (paused lanes gated)" if active else "clear")
        _state_cache["logged_state"] = active
    _state_cache["active"] = active
    _state_cache["ts"] = now
    return active


_STRAT_SYMBOL = {
    "bitcoin": "BTCUSDT", "sol_macro": "SOLUSDT", "eth_macro": "ETHUSDT",
    "hype_macro": "HYPEUSDT", "xrp_macro": "XRPUSDT", "doge_macro": "DOGEUSDT",
    "bnb_macro": "BNBUSDT",
}


def _own_asset_trending(strategy: str) -> bool:
    """Per-asset override (operator order 2026-07-03): the global no-signal
    condition is built from xrp/hype/bnb odds + basket vol and gated BTC off
    other assets' books; if THIS lane's own asset reads trend on the fresh
    per-cycle P1 label, the lane trades regardless of the global condition.
    Missing/stale label => no override (global behavior)."""
    try:
        from src.analysis import asset_regime as _ar

        sym = _STRAT_SYMBOL.get(strategy)
        if not sym:
            return False
        st = _ar.get_state(sym)
        return bool(st and st.get("state") == "trend")
    except Exception:
        return False


def lane_blocked(strategy: str, window, action: str) -> bool:
    """True when this (strategy, window, action) lane is paused right now.

    PER-ASSET TRIGGER (operator order 2026-07-03): the pause is governed
    exclusively by THIS lane's own asset P1 label (asset_regime, per-cycle,
    hysteresis built in). The global tracker condition is NOT consulted —
    "per lane" applies to the trigger, not just the list.
    - own asset chop/dead  -> paused
    - own asset trend      -> trades
    - label missing/stale  -> trades (fail-open; frequency mandate)
    """
    cfg = _load_cfg()
    if not cfg.get("enabled", False):
        return False
    if action == "BUY_YES":
        side = "up"
    elif action == "BUY_NO":
        side = "down"
    else:  # unknown action: never block (entry gate only covers the two lanes)
        return False
    key = f"{strategy}|{window}|{side}"
    lanes = cfg.get("blocked_lanes") or []
    if key not in lanes:
        return False
    try:
        from src.analysis import asset_regime as _ar

        sym = _STRAT_SYMBOL.get(strategy)
        st = _ar.get_state(sym) if sym else None
    except Exception:
        st = None
    if st is None:
        return False  # no fresh own-asset label: fail-open
    if st.get("state") == "trend":
        return False
    return st.get("state") in ("chop", "dead")
