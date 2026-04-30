#!/usr/bin/env python3
"""
Legacy scenario harness retired.
"""

import sys


def main() -> int:
    print(
        "scripts/run_scenario_tests.py was retired because it targeted removed "
        "arbitrage/neh strategy paths."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
