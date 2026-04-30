#!/usr/bin/env python3
"""
Legacy general-market live scan entrypoint retired.
"""

import sys


def main() -> int:
    print(
        "scripts/live_strategy_scan.py was retired with fade/arbitrage/neh removal. "
        "Use the dashboard crypto watchlist and live strategy panels instead."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
