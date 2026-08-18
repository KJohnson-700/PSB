"""Shared deep-merge for settings.yaml updates (dashboard + live bot apply)."""

from typing import Any, Dict

ALLOWED_TOP_KEYS = frozenset(
    {
        "trading",
        "strategies",
        "risk",
        "term_risk",
        "exposure",
        "backtest",
        "logging",
        "dashboard",
        "notifications",
        "ai",
        "polymarket",
        # 2026-08-12 VALIDATOR SYNC: these two are in main._HOT_RELOAD_TOP_LEVEL_KEYS but were
        # missing here, so their hot-reloads always failed ("Unknown config key") — the bug that
        # made lane_management restart-class and silently kept direction.apply_windows at ['1h']
        # in the running bot while disk said ['1h','15m'] (operator-directed 15m AI routing).
        "direction",
        "lane_management",
        # 2026-08-18: favorite_lane was restart-class, so respect_ai_direction edits sat
        # dormant on disk while the lane was 100% strangled by the benched AI driver —
        # the same silent-key class as the 08-12 validator-sync bug two lines up.
        "favorite_lane",
    }
)


def deep_merge_config(
    base: Dict[str, Any],
    updates: Dict[str, Any],
    *,
    _top_level: bool = True,
) -> Dict[str, Any]:
    """Recursively merge updates into base dict. Rejects unknown keys only at top level."""
    for key, val in updates.items():
        if _top_level and key not in ALLOWED_TOP_KEYS:
            raise ValueError(
                f"Unknown config key: '{key}'. Allowed: {sorted(ALLOWED_TOP_KEYS)}"
            )
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            deep_merge_config(base[key], val, _top_level=False)
        else:
            base[key] = val
    return base
