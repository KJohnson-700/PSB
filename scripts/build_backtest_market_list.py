#!/usr/bin/env python3
"""
Build backtest market list from real Polymarket data.

Fetches closed events from Gamma API, assigns markets to categories using
config/backtest_market_categories.yaml, and writes config/backtest_markets.yaml
with up to 150 real market slugs per category.

Usage:
  python scripts/build_backtest_market_list.py
  python scripts/build_backtest_market_list.py --max-per-category 100 --max-events 3000
  python scripts/build_backtest_market_list.py --end-year 2024   # only events that ended in 2024 (better for 2024 backtests)
"""
import argparse
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GAMMA_BASE = "https://gamma-api.polymarket.com"


def load_categories(config_path: Path) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def fetch_closed_events(limit_per_page: int = 100, max_events: int = 8000) -> list:
    """Fetch closed events from Gamma API (paginated). Returns list of event dicts."""
    events = []
    offset = 0
    while len(events) < max_events:
        r = requests.get(
            f"{GAMMA_BASE}/events",
            params={
                "closed": "true",
                "limit": limit_per_page,
                "offset": offset,
            },
            timeout=60,
        )
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list) or not page:
            break
        events.extend(page)
        if len(page) < limit_per_page:
            break
        offset += limit_per_page
        if offset >= 10000:  # API sanity cap
            break
    return events[:max_events]


def filter_events_by_end_date(
    events: list,
    end_year: Optional[int] = None,
    end_date_min: Optional[str] = None,
    end_date_max: Optional[str] = None,
) -> list:
    """Keep only events whose endDate falls within the given year or date range."""
    if end_year is None and end_date_min is None and end_date_max is None:
        return events
    filtered = []
    for e in events:
        end_str = (e.get("endDate") or "")[:10]
        if not end_str or len(end_str) < 10:
            continue
        try:
            dt = datetime.strptime(end_str, "%Y-%m-%d")
        except ValueError:
            continue
        if end_year is not None and dt.year != end_year:
            continue
        if end_date_min is not None and end_str < end_date_min:
            continue
        if end_date_max is not None and end_str > end_date_max:
            continue
        filtered.append(e)
    return filtered


def extract_market_slugs(event: dict) -> list:
    """Return list of market slugs from an event. Uses market slug or event slug."""
    slugs = []
    seen = set()
    for m in event.get("markets") or []:
        s = (m.get("slug") or "").strip()
        if s and s not in seen:
            slugs.append(s)
            seen.add(s)
    if not slugs:
        ev_slug = (event.get("slug") or "").strip()
        if ev_slug and ev_slug not in seen:
            slugs.append(ev_slug)
            seen.add(ev_slug)
    return slugs


def event_text(event: dict) -> str:
    """Lowercased searchable text from event title, slug, description, category."""
    title = (event.get("title") or "")[:500]
    slug = event.get("slug") or ""
    desc = (event.get("description") or "")[:300]
    category = event.get("category") or ""
    tags = " ".join(
        (t.get("label") or t.get("slug") or "")
        for t in (event.get("tags") or [])
        if isinstance(t, dict)
    )
    return " ".join([title, slug, desc, category, tags]).lower()


def matches_category(text: str, keywords: list) -> bool:
    """True if any keyword appears in text (word or substring)."""
    for kw in keywords:
        if kw is None:
            continue
        kw_str = str(kw).lower()
        if kw_str and kw_str in text:
            return True
    return False


def build_market_lists(
    events: list,
    categories_config: dict,
    max_per_category: int,
) -> dict:
    """
    Assign each event's markets to matching categories. Returns:
    { "arbitrage_elections_president": [slug, ...], ... }
    """
    categories = categories_config.get("categories") or {}
    cap = max_per_category or categories_config.get("max_markets_per_category") or 150
    lists = {cat_id: [] for cat_id in categories}
    global_seen = set()  # optional: dedupe across categories

    for event in events:
        text = event_text(event)
        market_slugs = extract_market_slugs(event)
        if not market_slugs:
            continue

        for cat_id, spec in categories.items():
            if not isinstance(spec, dict):
                continue
            keywords = spec.get("keywords") or []
            if not keywords or not matches_category(text, keywords):
                continue
            current = lists.get(cat_id, [])
            if len(current) >= cap:
                continue
            for s in market_slugs:
                if s not in current:
                    current.append(s)
                    if len(current) >= cap:
                        break
            lists[cat_id] = current

    return lists


def main():
    parser = argparse.ArgumentParser(description="Build backtest market list from Polymarket Gamma API")
    parser.add_argument(
        "--max-per-category",
        type=int,
        default=None,
        help="Max markets per category (default from config)",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=8000,
        help="Max closed events to fetch (default 8000)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output YAML path (default config/backtest_markets.yaml)",
    )
    parser.add_argument(
        "--categories",
        default=None,
        help="Path to category config (default config/backtest_market_categories.yaml)",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=None,
        help="Only include events that ended in this year (e.g. 2024 for 2024 backtests)",
    )
    parser.add_argument(
        "--end-date-min",
        default=None,
        help="Only include events with endDate >= this (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date-max",
        default=None,
        help="Only include events with endDate <= this (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    categories_path = Path(args.categories or REPO_ROOT / "config" / "backtest_market_categories.yaml")
    output_path = Path(args.output or REPO_ROOT / "config" / "backtest_markets.yaml")

    if not categories_path.is_file():
        print(f"Categories config not found: {categories_path}", file=sys.stderr)
        sys.exit(1)

    categories_config = load_categories(categories_path)
    max_per = args.max_per_category or categories_config.get("max_markets_per_category") or 150

    print("Fetching closed events from Gamma API...")
    events = fetch_closed_events(max_events=args.max_events)
    print(f"Fetched {len(events)} events.")

    events = filter_events_by_end_date(
        events,
        end_year=args.end_year,
        end_date_min=args.end_date_min,
        end_date_max=args.end_date_max,
    )
    if args.end_year is not None or args.end_date_min is not None or args.end_date_max is not None:
        print(f"After end-date filter: {len(events)} events.")

    print("Assigning markets to categories...")
    lists = build_market_lists(events, categories_config, max_per)

    # Build output: group by strategy then category
    by_strategy = {"arbitrage": {}, "fade": {}, "both": {}}
    for cat_id, spec in (categories_config.get("categories") or {}).items():
        if not isinstance(spec, dict):
            continue
        strategy = spec.get("strategy") or "arbitrage"
        slugs = lists.get(cat_id, [])
        if strategy not in by_strategy:
            by_strategy[strategy] = {}
        by_strategy[strategy][cat_id] = slugs

    out = {
        "description": "Real Polymarket market slugs for backtesting, by strategy and category. Generated by scripts/build_backtest_market_list.py.",
        "max_markets_per_category": max_per,
        "source": "Polymarket Gamma API (closed events)",
        "by_strategy": by_strategy,
        "counts": {
            cat_id: len(slugs)
            for strategy, cats in by_strategy.items()
            for cat_id, slugs in cats.items()
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        yaml.safe_dump(out, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"Wrote {output_path}")
    total = sum(out["counts"].values())
    print(f"Total market slots: {total} (counts per category in YAML)")


if __name__ == "__main__":
    main()
