#!/usr/bin/env python3
"""
Run Polymarket strategy backtest with live progress display.

Usage:
  python scripts/run_backtest.py --strategy fade --slug will-trump-win-2024 --start 2024-01-01 --end 2024-06-30
  python scripts/run_backtest.py --plan config/backtest_plan.yaml
  python scripts/run_backtest.py --strategy fade --csv data/backtest/sample.csv --no-ui

Requires POLYMARKETDATA_API_KEY in `.env` or config/secrets.env for API data.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yaml

from src.backtest.data_loader import PolymarketLoader, PolymarketDataLoader, LocalDataLoader
from src.backtest.engine import BacktestEngine, BacktestTrade
from src.env_bootstrap import load_project_dotenv

# Suppress verbose logging when using Rich UI
logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
)
logger = logging.getLogger(__name__)


def load_config():
    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_plan(plan_path: str) -> dict:
    path = Path(plan_path)
    if not path.exists():
        path = Path(__file__).resolve().parent.parent / plan_path
    with open(path) as f:
        return yaml.safe_load(f)


def discover_slug(loader: PolymarketDataLoader, search_terms: list, start: str, end: str) -> str:
    """Find first market with data in date range."""
    for term in search_terms:
        df = loader.fetch_markets(search=term, limit=20)
        if df.empty or "slug" not in df.columns:
            continue
        for _, row in df.iterrows():
            slug = row.get("slug")
            if not slug:
                continue
            data = loader.load_market_data(slug, start, end, "1h")
            if data is not None and len(data) >= 50:
                return slug
    return ""


def _generate_synthetic(start: str, end: str) -> pd.DataFrame:
    """Generate synthetic price data with a random walk and extreme spikes."""
    import numpy as np
    dates = pd.date_range(start, end, freq="1h")
    np.random.seed(42)
    prices = 0.5 + np.cumsum(np.random.randn(len(dates)) * 0.01)
    prices = np.clip(prices, 0.05, 0.95)
    n_extreme = min(20, len(prices) // 10)
    extreme_idx = np.random.choice(len(prices), n_extreme, replace=False)
    prices[extreme_idx] = np.random.choice([0.96, 0.97, 0.98, 0.04, 0.05], n_extreme)
    data = pd.DataFrame({"price": prices, "spread": 0.02}, index=pd.DatetimeIndex(dates, name="t"))
    data.index = pd.to_datetime(data.index, utc=True)
    return data


def main():
    parser = argparse.ArgumentParser(description="Run Polymarket backtest")
    parser.add_argument("--strategy", choices=["fade", "arbitrage", "neh"], default="fade")
    parser.add_argument("--slug", help="Market slug (for PolymarketData API)")
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-06-30", help="End date YYYY-MM-DD")
    parser.add_argument("--csv", help="Path to local CSV (optional, overrides API)")
    parser.add_argument("--plan", help="Load params from backtest_plan.yaml")
    parser.add_argument("--bankroll", type=float, default=10000.0)
    parser.add_argument("--resolution", default="1h", help="Data resolution (1m, 5m, 1h)")
    parser.add_argument("--no-ui", action="store_true", help="Plain text output, no Rich")
    parser.add_argument("--save-report", action="store_true", help="Save report to data/backtest/reports")
    parser.add_argument("--synthetic", action="store_true", help="Force synthetic data (skip API)")
    args = parser.parse_args()

    load_project_dotenv(Path(__file__).resolve().parent.parent, quiet=True)

    # Load plan if specified
    if args.plan:
        plan = load_plan(args.plan)
        args.strategy = plan.get("strategy", args.strategy)
        args.bankroll = plan.get("bankroll", args.bankroll)
        args.resolution = plan.get("data", {}).get("resolution", args.resolution)
        dates = plan.get("dates", {})
        args.start = dates.get("start", args.start)
        args.end = dates.get("end", args.end)
        if not args.slug and plan.get("market", {}).get("search_terms"):
            api_key = os.getenv("POLYMARKETDATA_API_KEY")
            if api_key:
                loader = PolymarketDataLoader(api_key=api_key)
                args.slug = discover_slug(
                    loader,
                    plan["market"]["search_terms"],
                    args.start,
                    args.end,
                )
                if args.slug:
                    print(f"Discovered market: {args.slug}")

    config = load_config()
    data = None
    slug = args.slug or "unknown"

    if args.csv:
        loader = LocalDataLoader()
        data = loader.load_from_csv(args.csv)
        if data is None:
            logger.error(f"Could not load CSV: {args.csv}")
            sys.exit(1)
        data_source = "LOCAL CSV"
    else:
        api_key = os.getenv("POLYMARKETDATA_API_KEY")
        data = None
        data_source = None

        if args.synthetic:
            print("Using SYNTHETIC data (--synthetic flag).")
            data = _generate_synthetic(args.start, args.end)
            data_source = "SYNTHETIC (fake)"
        elif args.slug:
            pm_loader = PolymarketLoader()
            data = pm_loader.load_market_data(args.slug, args.start, args.end, args.resolution)
            if data is not None and not data.empty:
                data_source = "REAL (Polymarket CLOB API)"
            elif api_key:
                loader = PolymarketDataLoader(api_key=api_key)
                data = loader.load_market_data(args.slug, args.start, args.end, args.resolution)
                data_source = "REAL (PolymarketData API)" if data is not None else None
            if data is None or data.empty:
                print("API returned no data. Falling back to SYNTHETIC.")
                data = _generate_synthetic(args.start, args.end)
                data_source = "SYNTHETIC (API failed)"
        else:
            print("--slug required for real data. Using SYNTHETIC.")
            data = _generate_synthetic(args.start, args.end)
            data_source = "SYNTHETIC (fake)"

    if data.empty:
        logger.error("No data to backtest")
        sys.exit(1)
    print(f"Data source: {data_source} | Market: {slug} | Bars: {len(data)}\n")

    engine = BacktestEngine(
        config=config,
        strategy_name=args.strategy,
        initial_bankroll=args.bankroll,
    )

    use_rich = not args.no_ui
    if use_rich:
        try:
            from rich.console import Console
            from rich.layout import Layout
            from rich.live import Live
            from rich.panel import Panel
            from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
            from rich.table import Table
        except ImportError:
            use_rich = False

    result = None
    recent_trades = []
    resolution_outcome = None
    if args.slug and slug != "unknown":
        resolution_outcome = PolymarketLoader().get_resolution_outcome(slug)

    def on_progress(current, total, trades_count, bankroll, last_trade):
        nonlocal result, recent_trades
        if last_trade:
            recent_trades.append(last_trade)
            recent_trades = recent_trades[-10:]

    if use_rich:
        console = Console()
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40),
            TaskProgressColumn(),
            TextColumn("•"),
            TextColumn("[green]{task.fields[trades]} trades"),
            TextColumn("•"),
            TextColumn("[yellow]${task.fields[bankroll]:,.0f}"),
            console=console,
        )
        task_id = progress.add_task(
            "Backtesting...",
            total=len(data),
            trades=0,
            bankroll=args.bankroll,
        )

        def update_progress(current, total, trades_count, bankroll, last_trade):
            on_progress(current, total, trades_count, bankroll, last_trade)
            progress.update(task_id, completed=current, trades=trades_count, bankroll=bankroll)

        with progress:
            result = asyncio.run(
                engine.run(data, slug=slug, on_progress=update_progress, resolution_outcome=resolution_outcome)
            )
    else:
        result = asyncio.run(engine.run(data, slug=slug, on_progress=on_progress, resolution_outcome=resolution_outcome))

    # Results
    net_cash_flow = result.final_bankroll - result.initial_bankroll

    if use_rich:
        console = Console()
        table = Table(title="Backtest Results")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Strategy", result.strategy)
        table.add_row("Period", f"{result.start_date} to {result.end_date}")
        table.add_row("Initial bankroll", f"${result.initial_bankroll:,.2f}")
        table.add_row("Net cash flow", f"${net_cash_flow:,.2f}")
        table.add_row("Cash remaining", f"${result.final_bankroll:,.2f}")
        table.add_row("Trades", str(result.num_trades))
        table.add_row("Blocked (spread)", str(result.blocked_trade_count))
        table.add_row("Execution cost (slippage+fees)", f"${result.execution_cost_total:,.2f}")

        trade_table = Table(title="Recent Trades")
        trade_table.add_column("Time", style="dim")
        trade_table.add_column("Action")
        trade_table.add_column("Size")
        trade_table.add_column("Price")
        trade_table.add_column("Edge")
        for t in result.trades[-8:]:
            trade_table.add_row(
                str(t.timestamp)[:19],
                t.action,
                f"${t.size:.0f}",
                f"{t.fill_price:.3f}",
                f"{t.edge:.2f}",
            )

        console.print(Panel(table, title="[bold]Summary"))
        if result.trades:
            console.print(Panel(trade_table, title="[bold]Trades"))
        console.print("[dim]Note: Full PnL requires resolution data.[/dim]")
    else:
        print("\n" + "=" * 60)
        print("BACKTEST RESULTS")
        print("=" * 60)
        print(f"Strategy: {result.strategy}")
        print(f"Period: {result.start_date} to {result.end_date}")
        print(f"Initial bankroll: ${result.initial_bankroll:,.2f}")
        print(f"Net cash flow: ${net_cash_flow:,.2f}")
        print(f"Cash remaining: ${result.final_bankroll:,.2f}")
        print(f"Trades: {result.num_trades}")
        print(f"Blocked: {result.blocked_trade_count}")
        print(f"Execution cost (slippage+fees): ${result.execution_cost_total:,.2f}")
        if result.trades:
            print("\nLast 5 trades:")
            for t in result.trades[-5:]:
                print(f"  {t.timestamp} | {t.action} | ${t.size:.0f} @ {t.fill_price:.3f}")

    if args.save_report and result:
        report_dir = Path(__file__).resolve().parent.parent / "data" / "backtest" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = report_dir / f"backtest_{result.strategy}_{ts}.json"
        report = {
            "strategy": result.strategy,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "initial_bankroll": result.initial_bankroll,
            "final_bankroll": result.final_bankroll,
            "execution_cost_total": result.execution_cost_total,
            "trades_count": result.num_trades,
            "blocked_count": result.blocked_trade_count,
            "trades": [
                {
                    "timestamp": str(t.timestamp),
                    "action": t.action,
                    "size": t.size,
                    "fill_price": t.fill_price,
                    "edge": t.edge,
                    "slippage_cost": getattr(t, "slippage_cost", 0),
                    "fee_cost": getattr(t, "fee_cost", 0),
                }
                for t in result.trades
            ],
        }
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport saved: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
