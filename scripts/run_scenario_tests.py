#!/usr/bin/env python3
"""Run realistic scenario backtests for all three strategies."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import yaml

from src.backtest.engine import BacktestEngine


def load_config():
    with open(Path(__file__).resolve().parent.parent / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


def make_series(prices):
    dates = pd.date_range("2024-06-01", periods=len(prices), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"price": prices, "spread": 0.02}, index=pd.DatetimeIndex(dates, name="t")
    )


def deep_copy_config(config):
    return yaml.safe_load(yaml.dump(config))


def run_single(config, strategy, prices, slug, resolution, bankroll=10000):
    engine = BacktestEngine(config, strategy_name=strategy, initial_bankroll=bankroll)
    return asyncio.run(engine.run(make_series(prices), slug=slug, resolution_outcome=resolution))


def print_result(r, show_trades=0):
    print(f"  Trades: {r.num_trades}")
    print(f"  PnL: ${r.net_pnl:+,.2f}")
    print(f"  Final: ${r.final_bankroll:,.2f}")
    print(f"  Return: {r.total_return_pct:+.1f}%")
    print(f"  Execution costs: ${r.execution_cost_total:,.2f}")
    if show_trades:
        for t in r.trades[:show_trades]:
            print(f"    {t.action:12s} | size=${t.size:.0f} @ {t.fill_price:.3f} | edge={t.edge:.2f}")


def run_arbitrage(config):
    print("=" * 70)
    print("ARBITRAGE STRATEGY - REALISTIC SCENARIO TESTS")
    print("=" * 70)

    cfg = deep_copy_config(config)
    cfg["backtest"]["exit_strategy"] = "hold_to_settlement"

    # Scenario 1
    print("\n--- Scenario 1: YES at 0.30, resolves YES (arb should WIN) ---")
    r = run_single(cfg, "arbitrage", [0.30] * 60, "arb-win", True)
    print_result(r, show_trades=3)

    # Scenario 2
    print("\n--- Scenario 2: YES at 0.30, resolves NO (arb should LOSE) ---")
    r = run_single(cfg, "arbitrage", [0.30] * 60, "arb-loss", False)
    print_result(r)

    # Scenario 3
    print("\n--- Scenario 3: YES at 0.70 (NO underpriced at 0.30), resolves NO ---")
    r = run_single(cfg, "arbitrage", [0.70] * 60, "arb-no-win", False)
    print_result(r, show_trades=3)

    # Scenario 4: Early exit
    print("\n--- Scenario 4: YES at 0.25 drifts to 0.55 (early exit on profit) ---")
    cfg_exit = deep_copy_config(config)
    cfg_exit["backtest"]["exit_strategy"] = "time_and_target"
    cfg_exit["backtest"]["take_profit_pct"] = 0.20
    prices = list(np.linspace(0.25, 0.55, 80))
    r = run_single(cfg_exit, "arbitrage", prices, "arb-early", True)
    exits = [t for t in r.trades if "EXIT" in t.action]
    print_result(r)
    print(f"  Early exits: {len(exits)} (profit/stop/time)")

    # Scenario 5: Portfolio
    print("\n--- Scenario 5: Portfolio - 8 markets, mixed outcomes ---")
    scenarios = [
        (0.25, True, "YES cheap, resolves YES"),
        (0.30, True, "YES cheap, resolves YES"),
        (0.35, True, "YES cheap, resolves YES"),
        (0.25, False, "YES cheap, resolves NO"),
        (0.30, False, "YES cheap, resolves NO"),
        (0.70, False, "NO cheap, resolves NO"),
        (0.75, False, "NO cheap, resolves NO"),
        (0.70, True, "NO cheap, resolves YES"),
    ]
    total_pnl = 0
    wins = 0
    for i, (price, outcome, desc) in enumerate(scenarios):
        r = run_single(cfg, "arbitrage", [price] * 60, f"arb-p{i}", outcome)
        total_pnl += r.net_pnl
        tag = "WIN" if r.net_pnl > 0 else "LOSS"
        if r.net_pnl > 0:
            wins += 1
        print(f"  Mkt {i+1}: {tag:4s} | PnL: ${r.net_pnl:+8,.2f} | {desc}")
    print(f"  ----------------------------------------")
    print(f"  PORTFOLIO TOTAL PnL: ${total_pnl:+,.2f}")
    print(f"  Win rate: {wins}/{len(scenarios)} ({100*wins/len(scenarios):.0f}%)")


def run_neh(config):
    print("\n")
    print("=" * 70)
    print("NEH (NOTHING EVER HAPPENS) STRATEGY - REALISTIC SCENARIO TESTS")
    print("=" * 70)

    cfg = deep_copy_config(config)
    cfg["backtest"]["exit_strategy"] = "hold_to_settlement"

    # Scenario 1: Low YES decays, resolves NO
    print("\n--- Scenario 1: YES at 0.08, decays to 0.01, resolves NO (NEH should WIN) ---")
    prices = list(np.linspace(0.08, 0.01, 100))
    r = run_single(cfg, "neh", prices, "neh-win", False)
    print_result(r, show_trades=3)

    # Scenario 2: Low YES, black swan, resolves YES
    print("\n--- Scenario 2: YES at 0.08, spikes to 0.90 (black swan, NEH LOSES) ---")
    prices = [0.08] * 20 + list(np.linspace(0.08, 0.90, 30))
    r = run_single(cfg, "neh", prices, "neh-loss", True)
    print_result(r)

    # Scenario 3: YES at 0.05 stays flat, resolves NO
    print("\n--- Scenario 3: YES at 0.05, flat, resolves NO (premium collection) ---")
    prices = [0.05] * 80
    r = run_single(cfg, "neh", prices, "neh-flat", False)
    print_result(r, show_trades=3)

    # Scenario 4: YES at 0.12 (near threshold), resolves NO
    print("\n--- Scenario 4: YES at 0.12 (near max_yes_price=0.15), resolves NO ---")
    prices = [0.12] * 80
    r = run_single(cfg, "neh", prices, "neh-near", False)
    print_result(r)

    # Scenario 5: YES at 0.20 (above threshold, should NOT trade)
    print("\n--- Scenario 5: YES at 0.20 (above max_yes_price=0.15, should NOT trade) ---")
    prices = [0.20] * 80
    r = run_single(cfg, "neh", prices, "neh-no-trade", False)
    print_result(r)

    # Scenario 6: Portfolio
    print("\n--- Scenario 6: Portfolio - 10 markets, 8 resolve NO, 2 black swans ---")
    total_pnl = 0
    wins = 0
    for i in range(10):
        is_swan = i >= 8  # 2 black swans
        price = 0.05 + i * 0.01  # 0.05 to 0.14
        if price > 0.15:
            continue
        if is_swan:
            prices = [price] * 20 + list(np.linspace(price, 0.85, 30))
            outcome = True
        else:
            prices = list(np.linspace(price, max(0.01, price - 0.03), 80))
            outcome = False
        r = run_single(cfg, "neh", prices, f"neh-p{i}", outcome)
        total_pnl += r.net_pnl
        tag = "WIN" if r.net_pnl > 0 else "LOSS"
        if r.net_pnl > 0:
            wins += 1
        print(f"  Mkt {i+1}: {tag:4s} | PnL: ${r.net_pnl:+8,.2f} | YES={price:.2f} | {'BLACK SWAN' if is_swan else 'resolves NO'}")
    print(f"  ----------------------------------------")
    print(f"  PORTFOLIO TOTAL PnL: ${total_pnl:+,.2f}")
    print(f"  Win rate: {wins}/10 ({100*wins/10:.0f}%)")
    print(f"  Key insight: NEH profits on many small wins, loses big on rare events")


if __name__ == "__main__":
    config = load_config()
    run_arbitrage(config)
    run_neh(config)
