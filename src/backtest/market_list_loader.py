"""
Load backtest market slugs from config/backtest_markets.yaml (category-based, real data).

Supports:
- Slugs by strategy (arbitrage, fade, both)
- Filter by category and min_markets_per_category
- Cap per strategy for runtime control
"""

from pathlib import Path
from typing import Dict, List, Optional

import yaml


def load_backtest_markets_yaml(path: Path) -> dict:
    """Load and return the backtest_markets.yaml structure."""
    with open(path) as f:
        return yaml.safe_load(f)


def get_slugs_for_strategy(
    plan_path: Path,
    strategy: str,
    categories: Optional[List[str]] = None,
    min_markets_per_category: int = 30,
    max_slugs_per_strategy: Optional[int] = None,
) -> List[str]:
    """
    Return a list of market slugs for the given strategy from the pre-built market list.

    strategy: "arbitrage" | "fade" | "both"
    categories: If set, only include these category keys (e.g. ["both_elections", "both_macro"]).
                If None, include all categories for that strategy that meet min_markets_per_category.
    min_markets_per_category: Skip categories with fewer slugs than this (solid sample size).
    max_slugs_per_strategy: Cap total slugs returned (for runtime control).
    """
    data = load_backtest_markets_yaml(plan_path)
    by_strategy = data.get("by_strategy") or {}

    strategy_data = by_strategy.get(strategy)
    if not strategy_data:
        return []

    slugs = []
    seen = set()

    cat_keys = list(strategy_data.keys()) if categories is None else categories
    for cat_id in cat_keys:
        if cat_id not in strategy_data:
            continue
        cat_slugs = strategy_data[cat_id] or []
        if len(cat_slugs) < min_markets_per_category:
            continue
        for s in cat_slugs:
            if s and s not in seen:
                slugs.append(s)
                seen.add(s)
        if max_slugs_per_strategy is not None and len(slugs) >= max_slugs_per_strategy:
            break
    if max_slugs_per_strategy is not None:
        slugs = slugs[:max_slugs_per_strategy]
    return slugs
