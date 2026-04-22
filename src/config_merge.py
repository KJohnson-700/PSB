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
