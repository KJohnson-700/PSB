"""Live calibration-correction apply hook (2026-07-22).

Reads the per-lane est_prob correction map produced (shadow) by
``scripts/calibration_correction.py`` and returns the ``delta_p_side`` for a
lane so the sizing path can shrink an over-confident lane's win-probability
before the true-Kelly size is computed.

Design contract (must match the shadow exactly, or the apply diverges from the
numbers that earned the apply-gate):
  - Correction is applied to ``win_prob`` (== p_side) at SIZING time ONLY.
    It does NOT re-gate admission — the shadow re-sizes already-filled trades,
    it never re-runs the edge gate. Applying it to admission would reject
    trades the shadow never modeled.
  - corrected_p_side = clamp(raw_p_side - delta_p_side, 0.02, 0.98)
  - Lane key = ``f"{strategy_no_macro}|{window}|{'up'|'down'}"`` (e.g. eth|15m|up).

Fail-safe by construction: any missing file / parse error / unknown lane
returns 0.0 (no correction), so this can NEVER raise into the trade path or
size a position it couldn't price. Flag-gated by the caller.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, Optional

_MAP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "calibration", "est_prob_correction_map.json",
)

_lock = threading.Lock()
_cache: Dict[str, float] = {}
_cached_mtime: Optional[float] = None
_loaded_path: Optional[str] = None


def _refresh_locked(path: str) -> None:
    """Reload the map iff its mtime changed. CALLER HOLDS _lock."""
    global _cache, _cached_mtime, _loaded_path
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        # No map yet -> empty (every lookup returns 0.0 = no correction).
        _cache = {}
        _cached_mtime = None
        _loaded_path = path
        return
    if mtime == _cached_mtime and path == _loaded_path:
        return
    try:
        with open(path) as fh:
            doc = json.load(fh)
        lanes = doc.get("lanes", {}) if isinstance(doc, dict) else {}
        parsed: Dict[str, float] = {}
        for k, v in lanes.items():
            try:
                parsed[str(k)] = float(v.get("delta_p_side", 0.0) or 0.0)
            except (TypeError, ValueError, AttributeError):
                continue
        _cache = parsed
    except (OSError, ValueError, json.JSONDecodeError):
        # Corrupt/partial write -> keep last good cache, do not raise.
        return
    _cached_mtime = mtime
    _loaded_path = path


def get_correction_delta(lane_key: str, path: Optional[str] = None) -> float:
    """delta_p_side for ``lane_key`` (0.0 if unknown / no map / any error)."""
    if not lane_key:
        return 0.0
    p = path or _MAP_PATH
    try:
        with _lock:
            _refresh_locked(p)
            return float(_cache.get(lane_key, 0.0))
    except Exception:
        return 0.0


def corrected_win_prob(win_prob: float, lane_key: str, path: Optional[str] = None) -> float:
    """clamp(win_prob - delta_p_side, 0.02, 0.98). Identity if no correction."""
    delta = get_correction_delta(lane_key, path)
    if not delta:
        return win_prob
    return min(0.98, max(0.02, float(win_prob) - delta))
