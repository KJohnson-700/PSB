"""Shared lane-specific entry policy resolution for macro strategies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class LaneEntryPolicy:
    enabled: bool
    min_edge: float
    hard_min_edge: float
    ai_override_min_edge: float
    entry_price_min: float
    entry_price_max: float
    entry_window_min: float
    entry_window_max: float
    size_multiplier: float


_ENTRY_POLICY_KEYS = frozenset(
    {
        "enabled",
        "min_edge",
        "hard_min_edge",
        "ai_override_min_edge",
        "entry_price_min",
        "entry_price_max",
        "entry_window_min",
        "entry_window_max",
        "size_multiplier",
    }
)


def _normalize_entry_policy_map(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in _ENTRY_POLICY_KEYS:
        if key not in raw:
            continue
        if key == "enabled":
            out[key] = bool(raw[key])
        else:
            out[key] = float(raw[key])
    return out


def resolve_entry_policy_side(*, direction: Optional[str], action: Optional[str]) -> str:
    """Return thesis side (`up`/`down`) for an entry.

    Strategy callers pass ``direction`` as the already-selected trade thesis
    (UP/DOWN), not the raw market question orientation. Keep BUY_NO aligned to
    that thesis so lane-specific down policies apply to BUY_NO/down trades.
    """
    dir_clean = str(direction or "").strip().upper()
    action_clean = str(action or "").strip().upper()

    if dir_clean == "DOWN":
        return "down"
    if dir_clean == "UP":
        return "up"

    if action_clean == "BUY_NO":
        return "down"
    if action_clean in {"BUY_YES", "SELL_YES"}:
        return "up"
    return "unknown"


def resolve_lane_entry_policy(
    *,
    strategy_name: str,
    window_size: str,
    side: str,
    full_config: Dict[str, Any],
    legacy_policy: Optional[Dict[str, Any]] = None,
) -> LaneEntryPolicy:
    """Resolve lane-specific entry policy for a strategy/window/side tuple."""
    params: Dict[str, Any] = {
        "enabled": True,
        "min_edge": 0.0,
        "hard_min_edge": 0.0,
        "ai_override_min_edge": 0.0,
        "entry_price_min": 0.0,
        "entry_price_max": 1.0,
        "entry_window_min": 0.0,
        "entry_window_max": 0.0,
        "size_multiplier": 1.0,
    }
    params.update(_normalize_entry_policy_map(legacy_policy or {}))

    top_entry_cfg = (full_config.get("entry_policy") or {}) if isinstance(full_config, dict) else {}
    params.update(_normalize_entry_policy_map(top_entry_cfg.get("defaults") or {}))

    strategies_cfg = (full_config.get("strategies") or {}) if isinstance(full_config, dict) else {}
    strategy_cfg = (strategies_cfg.get(strategy_name) or {}) if isinstance(strategies_cfg, dict) else {}
    strategy_entry_cfg = (strategy_cfg.get("entry_policy") or {}) if isinstance(strategy_cfg, dict) else {}
    params.update(_normalize_entry_policy_map(strategy_entry_cfg.get("defaults") or {}))

    side_cfg = (
        (((strategy_entry_cfg.get("window_side_overrides") or {}).get(str(window_size)) or {}).get(str(side))
        or {})
    )
    params.update(_normalize_entry_policy_map(side_cfg))

    return LaneEntryPolicy(
        enabled=bool(params["enabled"]),
        min_edge=float(params["min_edge"]),
        hard_min_edge=float(params["hard_min_edge"]),
        ai_override_min_edge=float(params["ai_override_min_edge"]),
        entry_price_min=float(params["entry_price_min"]),
        entry_price_max=float(params["entry_price_max"]),
        entry_window_min=float(params["entry_window_min"]),
        entry_window_max=float(params["entry_window_max"]),
        size_multiplier=float(params["size_multiplier"]),
    )


def entry_policy_to_dict(policy: LaneEntryPolicy, *, strategy_name: str, window_size: str, side: str) -> Dict[str, Any]:
    payload = asdict(policy)
    payload["strategy"] = str(strategy_name)
    payload["window_size"] = str(window_size)
    payload["side"] = str(side)
    return payload
