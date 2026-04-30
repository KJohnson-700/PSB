#!/usr/bin/env python3
"""
Legacy backtest entrypoint retired.

The old rigorous runner targeted fade/arbitrage market universes that no longer
exist in this repo. Keeping the script importable but inert is safer than
leaving a crash-prone path in place.
"""

import sys


def main() -> int:
    print(
        "scripts/run_backtest_rigorous.py was retired with fade/arbitrage removal. "
        "Use scripts/run_backtest_crypto.py for BTC/SOL/ETH/HYPE/XRP backtests."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
