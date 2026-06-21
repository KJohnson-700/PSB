"""Disk-backed exposure pause/resume overrides — the bridge that makes the split
dashboard's pause controls actually work.

The dashboard runs in its own process (``--dashboard-only``) and has no in-process
ExposureManager objects, so the old endpoints that called ``manager.manual_pause()``
returned "No bot instance" in split mode. Instead the dashboard now WRITES its
intent to ``data/runtime/exposure_overrides.json`` and the bot RECONCILES its
managers against that file every trading cycle (same pattern as ``data/KILL_SWITCH``).

Schema::

    {"global_paused": false,
     "paused_lanes": ["HYPE", "BNB"],
     "updated_at": "2026-06-21T..."}

Lane keys are the canonical ExposureManager ``lane_name`` (BTC/SOL/ETH/HYPE/XRP/
DOGE/BNB). ``normalize_lane`` accepts short/strategy forms (e.g. "hype",
"hype_macro", "bitcoin") and maps them to the canonical key.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_RUNTIME_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "runtime"
DEFAULT_PATH = _RUNTIME_DIR / "exposure_overrides.json"

CANONICAL_LANES = ("BTC", "SOL", "ETH", "HYPE", "XRP", "DOGE", "BNB")

_LANE_ALIASES = {
    "btc": "BTC", "bitcoin": "BTC",
    "sol": "SOL", "sol_macro": "SOL", "solana": "SOL",
    "eth": "ETH", "eth_macro": "ETH", "ethereum": "ETH",
    "hype": "HYPE", "hype_macro": "HYPE", "hyperliquid": "HYPE",
    "xrp": "XRP", "xrp_macro": "XRP", "ripple": "XRP",
    "doge": "DOGE", "doge_macro": "DOGE", "dogecoin": "DOGE",
    "bnb": "BNB", "bnb_macro": "BNB", "binance": "BNB",
}


def normalize_lane(lane: Any) -> Optional[str]:
    """Map any lane/strategy/alias string to a canonical lane key, or None."""
    if lane is None:
        return None
    s = str(lane).strip().lower()
    if s in _LANE_ALIASES:
        return _LANE_ALIASES[s]
    up = s.upper()
    return up if up in CANONICAL_LANES else None


def _default() -> Dict[str, Any]:
    return {"global_paused": False, "paused_lanes": [], "updated_at": None}


def read_overrides(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read overrides; fail-safe to an empty (nothing-paused) state."""
    p = Path(path) if path else DEFAULT_PATH
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return _default()
        lanes = [normalize_lane(x) for x in (data.get("paused_lanes") or [])]
        return {
            "global_paused": bool(data.get("global_paused", False)),
            "paused_lanes": sorted({x for x in lanes if x}),
            "updated_at": data.get("updated_at"),
        }
    except (OSError, ValueError, TypeError):
        return _default()


def _write(data: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else DEFAULT_PATH
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, p)
    return data


def set_global(paused: bool, path: Optional[Path] = None) -> Dict[str, Any]:
    cur = read_overrides(path)
    cur["global_paused"] = bool(paused)
    return _write(cur, path)


def set_lane(lane: Any, paused: bool, path: Optional[Path] = None) -> Dict[str, Any]:
    """Pause/resume a single lane. Returns the new state. Raises ValueError on bad lane."""
    key = normalize_lane(lane)
    if key is None:
        raise ValueError(f"unknown lane: {lane!r}")
    cur = read_overrides(path)
    lanes = set(cur.get("paused_lanes") or [])
    if paused:
        lanes.add(key)
    else:
        lanes.discard(key)
    cur["paused_lanes"] = sorted(lanes)
    return _write(cur, path)


def lane_is_paused(lane_name: str, overrides: Optional[Dict[str, Any]] = None,
                   path: Optional[Path] = None) -> bool:
    ov = overrides if overrides is not None else read_overrides(path)
    if ov.get("global_paused"):
        return True
    key = normalize_lane(lane_name)
    return key in set(ov.get("paused_lanes") or [])
