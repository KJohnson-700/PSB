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
