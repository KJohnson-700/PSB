"""Shared helpers for strategy config parsing."""

from __future__ import annotations

import logging
from typing import Any, Dict


def resolve_enabled_flag(
    strategy_key: str,
    strategy_config: Dict[str, Any],
    *,
    logger: logging.Logger,
) -> bool:
    """Fail closed when a strategy config omits ``enabled``.

    This prevents silent activation/deactivation on YAML key typos or partial blocks.
    """
    if "enabled" not in strategy_config:
        logger.warning(
            "Strategy '%s' missing required config key 'enabled' — defaulting to disabled",
            strategy_key,
        )
        return False
    return bool(strategy_config.get("enabled", False))


def resolve_tf_config_value(
    strategy_config: Dict[str, Any],
    *,
    tf: str,
    key: str,
    default: Any = None,
) -> Any:
    """Resolve a strategy config value with timeframe-scoped overrides.

    Precedence:
      strategies.<name>.by_tf.<tf>.<key>
      strategies.<name>.defaults.<key>
      strategies.<name>.<key>
      default
    """
    cfg = strategy_config if isinstance(strategy_config, dict) else {}
    tf_key = str(tf or "").strip().lower()

    by_tf = cfg.get("by_tf") or {}
    if isinstance(by_tf, dict):
        tf_cfg = by_tf.get(tf_key) or {}
        if isinstance(tf_cfg, dict) and key in tf_cfg:
            return tf_cfg[key]

    defaults = cfg.get("defaults") or {}
    if isinstance(defaults, dict) and key in defaults:
        return defaults[key]

    if key in cfg:
        return cfg[key]

    return default


def tf_config_override_snapshot(strategy_config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return explicit ``by_tf`` overrides for startup/operator logging."""
    cfg = strategy_config if isinstance(strategy_config, dict) else {}
    by_tf = cfg.get("by_tf") or {}
    if not isinstance(by_tf, dict):
        return {}
    return {
        str(tf): dict(values)
        for tf, values in by_tf.items()
        if isinstance(values, dict) and values
    }


# ---------------------------------------------------------------------------
# SIDE POLICY (2026-08-13) — who picks the side: the resolver, or the market?
# ---------------------------------------------------------------------------
# Measured on 171,380 settled ghost candidates (real Polymarket resolutions,
# Aug 10-13) in the 0.45-0.55 toss-up band, EV per $1 staked @1c:
#     favorite -0.0215  |  resolver -0.0525  |  ai_hist -0.0584  |  coinflip +0.0020
# The resolver loses to a coin flip at 95% CI +/-0.24pt. Only bnb_macro|1h has
# positive EV@1c under the resolver, so it is exempted via config.
#
# ONE implementation, called from sol_macro / eth_macro / bitcoin, so the policy
# cannot drift between the three side-determination paths. All values read from
# the live `direction` config at decision time => hot-reloadable (direction is in
# main._HOT_RELOAD_TOP_LEVEL_KEYS). Fail-open: any bad/missing config, or a
# missing price, returns the resolver's side untouched.

SIDE_POLICY_TAG = "market_favorite"


def resolve_side_policy(
    full_config: Dict[str, Any],
    *,
    lane_key: str,
    yes_price: Any,
    resolver_side: Any,
) -> Dict[str, Any]:
    """Decide who owns the side for this candidate.

    Returns a dict — the caller applies it, so this stays pure/testable:
        active   bool   the favorite policy owns the side (caller must go sticky)
        side     str    "LONG" | "SHORT" | None (None => sit this candidate out)
        skip     str    reason when side is None, for the reject log
        tag      str    suffix to append to side_source when active
        flat_edge float admission edge to use when active
        meta     dict   telemetry (resolver_side, favorite_side, band, deadband)

    `active` is False for exempt lanes, policy=resolver, or any failure — in which
    case the caller must behave byte-identically to before this existed.
    """
    out = {
        "active": False, "side": resolver_side, "skip": None,
        "tag": "", "flat_edge": 0.0,
        "meta": {"side_policy": "resolver", "resolver_side": resolver_side},
    }
    try:
        cfg = (full_config or {}).get("direction", {}) or {}
        policy = str(cfg.get("side_policy", "resolver") or "resolver").lower()
        if policy != "favorite":
            return out
        exempt = {str(x) for x in (cfg.get("side_policy_resolver_lanes") or [])}
        if lane_key in exempt:
            out["meta"] = {"side_policy": "favorite", "side_policy_exempt": True,
                           "resolver_side": resolver_side}
            return out
        price = float(yes_price)
        if not (0.0 < price < 1.0):
            return out
    except (TypeError, ValueError, AttributeError):
        return out

    try:
        dead = abs(float(cfg.get("side_policy_deadband", 0.01) or 0.0))
    except (TypeError, ValueError):
        dead = 0.01
    band = cfg.get("side_policy_price_band") or [0.0, 1.0]
    try:
        lo, hi = float(band[0]), float(band[1])
    except (TypeError, ValueError, IndexError):
        lo, hi = 0.0, 1.0
    try:
        flat = float(cfg.get("side_policy_flat_edge", 0.02) or 0.0)
    except (TypeError, ValueError):
        flat = 0.02

    fav = "LONG" if price > 0.5 else ("SHORT" if price < 0.5 else None)
    out["meta"] = {
        "side_policy": "favorite", "side_policy_exempt": False,
        "resolver_side": resolver_side, "favorite_side": fav,
        "yes_price": price, "deadband": dead, "price_band": [lo, hi],
    }
    out["active"] = True
    out["tag"] = SIDE_POLICY_TAG
    out["flat_edge"] = flat

    # Deadband first: at/near 0.50 the "favorite" is quote jitter, not a signal.
    if abs(price - 0.5) <= dead:
        out["side"] = None
        out["skip"] = "favorite_deadband"
        return out
    # Own price band — deliberately NOT the lane bands, which are tuned for the
    # regime this policy replaces and would choke frequency (Codex q3).
    if not (lo <= price <= hi):
        out["side"] = None
        out["skip"] = "favorite_price_band"
        return out
    out["side"] = fav
    return out
