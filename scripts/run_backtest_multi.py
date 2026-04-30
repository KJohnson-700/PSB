#!/usr/bin/env python3
"""
Legacy multi-market backtest entrypoint retired.
"""

import sys


def main() -> int:
    print(
        "scripts/run_backtest_multi.py was retired with fade/arbitrage removal. "
        "Use scripts/run_backtest_crypto.py for active crypto strategies."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
