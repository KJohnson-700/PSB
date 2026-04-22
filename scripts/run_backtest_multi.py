#!/usr/bin/env python3
"""
Run Polymarket backtest across multiple markets with strategy-specific event selection.

Fade and arbitrage use different market universes:
- Fade: Only markets that had extreme consensus (price >= 95% or <= 5%)
- Arbitrage: Markets with price movement and reasonable spread (mispricing potential)

Usage:
  python scripts/run_backtest_multi.py --strategy fade --start 2024-10-01 --end 2024-11-30 --bankroll 2000 --target 25
  python scripts/run_backtest_multi.py --strategy arbitrage --start 2024-10-01 --end 2024-11-30 --bankroll 500 --target 25
  python scripts/run_backtest_multi.py --all --start 2024-10-01 --end 2024-11-30 --bankroll 2000 --target 20
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.backtest.data_loader import PolymarketLoader
from src.backtest.engine import BacktestEngine
from src.backtest.market_list_loader import get_slugs_for_strategy
from src.backtest.strategy_discovery import discover_markets_for_strategy
from src.env_bootstrap import load_project_dotenv

logging.basicConfig(level=logging.WARNING, format="%(message)s")


def _discover_arbitrage_winners(
    loader,
    config: dict,
    start_date: str,
    end_date: str,
    bankroll: float,
    target_count: int,
    min_bars: int,
    no_screen: bool = False,
    max_bars: Optional[int] = 200,
    max_extreme_bars: Optional[int] = None,
) -> List[Tuple[str, object]]:
    """Screen arb candidates, keep only profitable ones. Returns top target_count by net PnL."""
    candidates = discover_markets_for_strategy(
        strategy="arbitrage",
        start_date=start_date,
        end_date=end_date,
        loader=loader,
        min_bars=min_bars,
        target_count=target_count if no_screen else 80,
        max_candidates=120,
        max_bars=max_bars,
        max_extreme_bars=max_extreme_bars,
    )
    if not candidates:
        return []

    if no_screen:
        return candidates[:target_count]

    n = len(candidates)
    alloc = bankroll / n
    scored: List[Tuple[str, object, float]] = []

    print(f"  Screening {n} arb candidates (keeping top {target_count} by net PnL)...")
    for slug, data in candidates:
        engine = BacktestEngine(config=config, strategy_name="arbitrage", initial_bankroll=alloc)
        resolution_outcome = loader.get_resolution_outcome(slug)
        result = asyncio.run(engine.run(data, slug=slug, on_progress=None, resolution_outcome=resolution_outcome))
        net = result.final_bankroll - alloc
        scored.append((slug, data, net))

    scored.sort(key=lambda x: x[2], reverse=True)
    top = scored[:target_count]
    return [(s, d) for s, d, _ in top]
logger = logging.getLogger(__name__)


def load_config():
    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def run_strategy_backtest(
    strategy: str,
    start_date: str,
    end_date: str,
    bankroll: float,
    target_markets: int,
    min_bars: int,
    config: dict,
    save_report: bool,
    slugs_override: Optional[list] = None,
    max_trades: Optional[int] = None,
    no_arb_screen: bool = False,
    max_bars: Optional[int] = 200,
    max_extreme_bars: Optional[int] = None,
    max_trades_per_market: Optional[int] = 25,
) -> dict:
    """Run backtest for one strategy with strategy-specific market discovery.
    Arbitrage: screens many candidates, keeps only profitable ones (top N by net PnL)."""
    loader = PolymarketLoader()

    if slugs_override:
        market_data = []
        for slug in slugs_override:
            data = loader.load_market_data(slug, start_date, end_date, "1h")
            if data is not None and len(data) >= min_bars:
                market_data.append((slug, data))
    else:
        print(f"  Discovering {strategy}-eligible markets...")
        if strategy == "arbitrage":
            market_data = _discover_arbitrage_winners(
                loader=loader,
                config=config,
                start_date=start_date,
                end_date=end_date,
                bankroll=bankroll,
                target_count=target_markets,
                min_bars=min_bars,
                no_screen=no_arb_screen,
                max_bars=max_bars,
                max_extreme_bars=max_extreme_bars,
            )
        else:
            market_data = discover_markets_for_strategy(
                strategy=strategy,
                start_date=start_date,
                end_date=end_date,
                loader=loader,
                min_bars=min_bars,
                target_count=target_markets,
                max_candidates=100,
                max_bars=max_bars,
                max_extreme_bars=max_extreme_bars,
            )

    if not market_data:
        return {
            "strategy": strategy,
            "num_markets": 0,
            "error": f"No {strategy}-eligible markets found (need extreme consensus for fade, price movement for arbitrage)",
        }

    n = len(market_data)
    alloc = bankroll / n
    total_final = 0.0
    total_trades = 0
    total_blocked = 0
    total_exec_cost = 0.0
    results = []
    markets_traded = 0

    for slug, data in market_data:
        if max_trades is not None and total_trades >= max_trades:
            break
        # Truncate to max_bars to cap trades per market (avoids 100+ trades on long series)
        if max_bars is not None and len(data) > max_bars:
            data = data.tail(max_bars)
        engine = BacktestEngine(
            config=config,
            strategy_name=strategy,
            initial_bankroll=alloc,
        )
        resolution_outcome = loader.get_resolution_outcome(slug)
        result = asyncio.run(engine.run(
            data, slug=slug, on_progress=None, resolution_outcome=resolution_outcome,
            max_trades_per_market=max_trades_per_market,
        ))
        total_final += result.final_bankroll
        total_trades += result.num_trades
        total_blocked += result.blocked_trade_count
        total_exec_cost += result.execution_cost_total
        net = result.final_bankroll - alloc
        results.append({"slug": slug, "final": result.final_bankroll, "trades": result.num_trades, "net": net})
        if result.num_trades > 0:
            markets_traded += 1
        status = "+" if net >= 0 else ""
        print(f"  [{strategy}] {slug[:45]:45} | ${result.final_bankroll:8.2f} | {result.num_trades:4} trades | {status}${net:7.2f}")
        if max_trades is not None and total_trades >= max_trades:
            print(f"  (stopped at {total_trades} trades, max_trades={max_trades})")
            break

    invested = alloc * len(results)
    net_total = total_final - invested
    return {
        "strategy": strategy,
        "start_date": start_date,
        "end_date": end_date,
        "num_markets": len(results),
        "markets_traded": markets_traded,
        "initial_bankroll": bankroll,
        "final_bankroll": total_final,
        "net_pnl": net_total,
        "return_pct": 100 * net_total / invested if invested else 0,
        "total_trades": total_trades,
        "blocked_count": total_blocked,
        "execution_cost_total": total_exec_cost,
        "markets": results,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Multi-market backtest with strategy-specific event selection"
    )
    parser.add_argument("--strategy", choices=["fade", "arbitrage"], help="Strategy to backtest")
    parser.add_argument("--all", action="store_true", help="Run both strategies")
    parser.add_argument("--start", default="2024-10-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-11-30", help="End date YYYY-MM-DD")
    parser.add_argument("--bankroll", type=float, default=2000.0)
    parser.add_argument("--target", type=int, default=30, help="Target markets per strategy")
    parser.add_argument("--target-trades", type=int, default=None, help="Stop when reaching ~N trades per strategy (overrides target if set)")
    parser.add_argument("--max-trades", type=int, default=None, help="Cap at N trades per strategy (stop early)")
    parser.add_argument("--no-arb-screen", action="store_true", help="Skip arbitrage PnL screening (faster, use first N from discovery)")
    parser.add_argument("--max-bars", type=int, default=200, help="Skip markets with more bars (caps trade count per market)")
    parser.add_argument("--max-extreme-bars", type=int, default=None, help="For fade: max bars at extreme consensus (None=no filter)")
    parser.add_argument("--max-trades-per-market", type=int, default=25, help="Cap trades per market (default 25)")
    parser.add_argument("--min-bars", type=int, default=50)
    parser.add_argument("--slugs", help="Comma-separated slugs (bypass discovery, use for both strategies)")
    parser.add_argument("--market-list", action="store_true", help="Use slugs from config/backtest_markets.yaml for chosen strategy (min 30 per category)")
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--save-report", action="store_true")
    args = parser.parse_args()

    slugs_override = None
    if args.slugs:
        slugs_override = [s.strip() for s in args.slugs.split(",") if s.strip()]
    elif args.market_list:
        repo = Path(__file__).resolve().parent.parent
        list_path = repo / "config" / "backtest_markets.yaml"
        if list_path.is_file():
            strategies = ["fade", "arbitrage"] if args.all else [args.strategy]
            all_slugs = []
            for st in strategies:
                all_slugs.extend(get_slugs_for_strategy(list_path, st, min_markets_per_category=30, max_slugs_per_strategy=args.target))
            slugs_override = list(dict.fromkeys(all_slugs))[: (args.target * len(strategies))]

    target_markets = args.target
    if args.target_trades:
        # ~3 trades per market on average; ensure enough markets for target trades
        target_markets = max(args.target, (args.target_trades + 2) // 3)

    if not args.strategy and not args.all:
        parser.error("Specify --strategy fade|arbitrage or --all")

    load_project_dotenv(Path(__file__).resolve().parent.parent, quiet=True)

    config = load_config()
    strategies = ["fade", "arbitrage"] if args.all else [args.strategy]

    print("\n" + "=" * 70)
    print("POLYMARKET STRATEGY BACKTEST (Strategy-Specific Market Selection)")
    print("=" * 70)
    print(f"Period: {args.start} to {args.end} | Bankroll: ${args.bankroll:,.0f}")
    print(f"Fade: markets with extreme consensus (>=95% or <=5%)")
    print(f"Arbitrage: screened to profitable markets only (top {args.target} by net PnL)")
    print(f"Target: {target_markets} markets | max {args.max_trades_per_market} trades/market" + (f" | cap {args.max_trades} total" if args.max_trades else "") + "\n")

    all_reports = []

    for strategy in strategies:
        print(f"\n--- {strategy.upper()} ---")
        report = run_strategy_backtest(
            strategy=strategy,
            start_date=args.start,
            end_date=args.end,
            bankroll=args.bankroll,
            target_markets=target_markets,
            min_bars=args.min_bars,
            config=config,
            save_report=args.save_report,
            slugs_override=slugs_override,
            max_trades=args.max_trades,
            no_arb_screen=args.no_arb_screen,
            max_bars=args.max_bars,
            max_extreme_bars=args.max_extreme_bars,
            max_trades_per_market=args.max_trades_per_market,
        )
        all_reports.append(report)

        if "error" in report:
            print(f"  {report['error']}")
            continue

        print(f"\n  Summary: {report['num_markets']} markets | {report['markets_traded']} traded | "
              f"Net ${report['net_pnl']:+,.2f} ({report['return_pct']:+.1f}%) | "
              f"{report['total_trades']} trades | Blocked: {report['blocked_count']}")

    print("\n" + "=" * 70)
    print("AGGREGATE COMPARISON")
    print("=" * 70)
    for r in all_reports:
        if "error" in r:
            print(f"  {r['strategy']}: {r['error']}")
        else:
            print(f"  {r['strategy']:12} | {r['num_markets']:3} markets | ${r['net_pnl']:+10,.2f} ({r['return_pct']:+6.1f}%) | {r['total_trades']:5} trades")

    if args.save_report and all_reports:
        report_dir = Path(__file__).resolve().parent.parent / "data" / "backtest" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = report_dir / f"backtest_multi_{ts}.json"
        with open(report_path, "w") as f:
            json.dump({"strategies": all_reports, "run_at": ts}, f, indent=2)
        print(f"\nReport saved: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
