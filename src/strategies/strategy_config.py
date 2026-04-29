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
