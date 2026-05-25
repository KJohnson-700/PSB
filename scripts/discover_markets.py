#!/usr/bin/env python3
"""
Discover Polymarket markets for backtesting.
Uses Polymarket Gamma API (free, no auth) to list events/markets.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src.env_bootstrap import load_project_dotenv

load_project_dotenv(Path(__file__).resolve().parent.parent, quiet=True)

GAMMA_BASE = "https://gamma-api.polymarket.com"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--search", default="", help="Search term (e.g. trump, fed, bitcoin)")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--validate", action="store_true", help="Check if first slug has price data")
    parser.add_argument("--start", default="2024-06-01", help="Start date for validation")
    parser.add_argument("--end", default="2024-10-31", help="End date for validation")
    args = parser.parse_args()

    params = {"limit": args.limit}
    r = requests.get(f"{GAMMA_BASE}/events", params=params, timeout=30)
    r.raise_for_status()
    events = r.json()
    if not events:
        print("No events found.")
        sys.exit(1)
    if args.search:
        events = [e for e in events if args.search.lower() in (e.get("slug") or "").lower()]
        if not events:
            print(f"No events matching '{args.search}'.")
            sys.exit(1)

    print(f"\nFound {len(events)} events:\n")
    for e in events[:20]:
        slug = e.get("slug", "")
        title = (e.get("title", "") or "")[:60]
        print(f"  {slug}")
        print(f"    {title}...")

    if args.validate and events:
        # --validate previously hit src/backtest/data_loader.PolymarketLoader to
        # preview price history. That loader was removed when the broken backtester
        # was deleted (2026-05-24). Validation should now go through the live
        # rejected_candidates_settled.jsonl ghost log instead.
        print("\n--validate is currently a no-op (backtest data_loader removed; "
              "use ghost log for slug coverage).")


if __name__ == "__main__":
    main()
