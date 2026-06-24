"""Runtime lane-regime gate (the live half of the regime layer).

The bot reads ``config/settings.yaml`` only at startup, so static per-lane
``disable_buy_no_<tf>`` flags go stale as the regime shifts (e.g. the short side
is disabled across alts during a bear tape where its ghost edge has returned).
This module lets a separately-built map re-open such lanes *without a restart*:
it reads ``data/runtime/lane_regime_map.json`` (rebuilt every ~5 min by
``lane_regime_map.py``) fresh-per-scan and returns a decision the strategy hook
consults next to its YAML disables.

Design contract — FAIL SAFE. Any problem (missing file, stale map, corrupt JSON,
unexpected schema, exception) yields a *neutral* decision: it never overrides a
YAML disable, never forces a lane off, and leaves size unchanged. The static YAML
remains the hard operator kill-switch. This module must never raise into the
scan loop.

Precedence (highest first):
  1. manual ``force_off`` override            -> force_off=True
  2. manual ``force_on`` override             -> overrides_yaml_disable=True (size-capped)
     (manual overrides are intentional operator actions and apply *regardless* of
      map freshness — only the AUTO map entries below are gated on freshness.)
  3. stale / missing / corrupt map            -> neutral (YAML governs)
  4. fresh map entry with enabled=True        -> overrides_yaml_disable=True
  5. no entry                                 -> neutral (YAML governs)
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

# Runtime artifacts (written by lane_regime_map.py). Kept relative to repo root.
_MAP_PATH = os.environ.get(
    "LANE_REGIME_MAP_PATH", "data/runtime/lane_regime_map.json"
)
_OVERRIDES_PATH = os.environ.get(
    "LANE_REGIME_OVERRIDES_PATH", "data/runtime/lane_regime_overrides.json"
)

# force_on lanes are still held to a small paper size unless the override sets
# size_scalar explicitly higher.
_FORCE_ON_DEFAULT_SIZE_SCALAR = 0.25

_VALID_OVERRIDE_MODES = {"force_off", "force_on", "cap_size", "observe_only"}


@dataclass(frozen=True)
class LaneRegimeDecision:
    """Verdict for one (strategy, window, side) candidate at the current regime."""

    overrides_yaml_disable: bool = False
    force_off: bool = False
    size_scalar: float = 1.0
    enabled: Optional[bool] = None
    reason: str = "no_map_entry"
    sample_n: int = 0
    source: str = "neutral"
    stale: bool = False
    key: str = ""

    @property
    def is_neutral(self) -> bool:
        return (
            not self.overrides_yaml_disable
            and not self.force_off
            and abs(self.size_scalar - 1.0) < 1e-9
        )


def _neutral(key: str = "", reason: str = "no_map_entry", source: str = "neutral",
             stale: bool = False) -> LaneRegimeDecision:
    return LaneRegimeDecision(reason=reason, source=source, stale=stale, key=key)


class _JsonFileCache:
    """mtime-gated JSON loader. Re-reads only when the file changes; returns the
    last-good value on transient read/parse errors so a half-written file (the
    builder writes atomically, but be defensive) never blanks the cache."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._mtime: float = -1.0
        self._data: Optional[Dict[str, Any]] = None
        self._last_err: Optional[str] = None

    def get(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                st = os.stat(self._path)
            except OSError:
                # File absent -> treat as "no data" but keep any prior good copy
                # only if it still exists; absence is a real signal (stale/missing).
                self._data = None
                self._mtime = -1.0
                return None
            if st.st_mtime != self._mtime:
                try:
                    with open(self._path, "r") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, dict):
                        self._data = loaded
                        self._mtime = st.st_mtime
                        self._last_err = None
                except (OSError, ValueError) as exc:  # corrupt / mid-write
                    self._last_err = str(exc)
                    # keep prior self._data (last good)
            return self._data


_map_cache = _JsonFileCache(_MAP_PATH)
_ovr_cache = _JsonFileCache(_OVERRIDES_PATH)


def _norm_bias(bias: Optional[str]) -> str:
    b = str(bias or "").strip().upper()
    if b in ("BULL", "BULLISH"):
        return "BULLISH"
    if b in ("BEAR", "BEARISH"):
        return "BEARISH"
    if b in ("NEUTRAL", "RANGE", ""):
        return "NEUTRAL"
    return b


def _lane_key(strategy: str, window: str, side_action: str, bias: str) -> str:
    return f"{strategy}|{window}|{side_action}|{bias}"


def _clamp_size(x: Any, default: float = 1.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v <= 0.0:
        return 0.0
    return min(v, 1.0)


def _map_is_fresh(mp: Dict[str, Any], now: float) -> bool:
    """A map is usable only while inside its expires_at window."""
    exp = mp.get("expires_at_epoch")
    if isinstance(exp, (int, float)):
        return now <= float(exp)
    # Back-compat: ISO string expires_at.
    exp_iso = mp.get("expires_at")
    if isinstance(exp_iso, str):
        try:
            import datetime as _dt

            dt = _dt.datetime.fromisoformat(exp_iso.replace("Z", "+00:00"))
            return now <= dt.timestamp()
        except ValueError:
            return False
    return False  # no expiry => treat as stale (fail safe)


def _override_for(ovr: Dict[str, Any], strategy: str, window: str,
                  side_action: str, bias: str, now: float) -> Optional[Dict[str, Any]]:
    """Return the active override entry for this lane, if any (most specific
    key wins: full bias key, then bias-agnostic). Expired overrides ignored."""
    if not isinstance(ovr, dict):
        return None
    entries = ovr.get("overrides")
    if not isinstance(entries, dict):
        return None
    for key in (_lane_key(strategy, window, side_action, bias),
                f"{strategy}|{window}|{side_action}"):
        ent = entries.get(key)
        if not isinstance(ent, dict):
            continue
        exp = ent.get("expires_at_epoch")
        if isinstance(exp, (int, float)) and now > float(exp):
            continue
        if str(ent.get("mode", "")).strip() in _VALID_OVERRIDE_MODES:
            return ent
    return None


def evaluate_lane(
    strategy: str,
    window: str,
    side_action: str,
    asset_htf_bias: Optional[str],
    *,
    now: Optional[float] = None,
) -> LaneRegimeDecision:
    """Decide the regime-layer verdict for one candidate. Never raises.

    Args:
        strategy: e.g. "doge_macro".
        window: "5m" | "15m" | "1h".
        side_action: "BUY_NO" | "BUY_YES".
        asset_htf_bias: the live per-asset htf bias (BEARISH/BULLISH/NEUTRAL).
    """
    try:
        if now is None:
            now = time.time()
        bias = _norm_bias(asset_htf_bias)
        key = _lane_key(strategy, window, side_action, bias)

        ovr = _ovr_cache.get()
        ov_ent = _override_for(ovr, strategy, window, side_action, bias, now)

        # 1. Manual force_off always wins.
        if ov_ent is not None and ov_ent.get("mode") == "force_off":
            return LaneRegimeDecision(
                force_off=True, size_scalar=0.0, enabled=False,
                reason=str(ov_ent.get("reason", "manual_force_off")),
                source="override_force_off", key=key,
            )

        mp = _map_cache.get()

        # 2. Stale / missing / corrupt map -> neutral (YAML governs).
        if not isinstance(mp, dict) or not _map_is_fresh(mp, now):
            # ...but a manual force_on still applies even with no fresh map.
            if ov_ent is not None and ov_ent.get("mode") == "force_on":
                return LaneRegimeDecision(
                    overrides_yaml_disable=True, enabled=True,
                    size_scalar=_clamp_size(
                        ov_ent.get("size_scalar"), _FORCE_ON_DEFAULT_SIZE_SCALAR),
                    reason=str(ov_ent.get("reason", "manual_force_on")),
                    source="override_force_on", key=key,
                )
            return _neutral(key=key, reason="map_stale_or_missing",
                            source="stale_fallback", stale=True)

        lanes = mp.get("lanes")
        entry = lanes.get(key) if isinstance(lanes, dict) else None

        # 3. Manual force_on (with a fresh map present).
        if ov_ent is not None and ov_ent.get("mode") == "force_on":
            size = _clamp_size(ov_ent.get("size_scalar"), _FORCE_ON_DEFAULT_SIZE_SCALAR)
            return LaneRegimeDecision(
                overrides_yaml_disable=True, enabled=True, size_scalar=size,
                reason=str(ov_ent.get("reason", "manual_force_on")),
                sample_n=int((entry or {}).get("sample_n", 0) or 0),
                source="override_force_on", key=key,
            )

        # cap_size override: don't change enable, just cap (applied on top of map).
        cap = None
        if ov_ent is not None and ov_ent.get("mode") == "cap_size":
            cap = _clamp_size(ov_ent.get("size_scalar"), 1.0)

        # 4. Fresh map entry that enables this YAML-disabled lane.
        if isinstance(entry, dict) and bool(entry.get("enabled")):
            size = _clamp_size(entry.get("size_scalar"), 1.0)
            if cap is not None:
                size = min(size, cap)
            return LaneRegimeDecision(
                overrides_yaml_disable=True, enabled=True, size_scalar=size,
                reason=str(entry.get("reason", "regime_map_enable")),
                sample_n=int(entry.get("sample_n", 0) or 0),
                source="map_enable", key=key,
            )

        # 5. Map present but this lane not enabled -> neutral (YAML governs).
        return _neutral(key=key, reason="map_entry_not_enabled", source="map_neutral")
    except Exception:  # absolute fail-safe: never break the scan loop
        return _neutral(reason="evaluate_error", source="error")
