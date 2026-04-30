import argparse
import asyncio
from datetime import datetime

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, BacktestVenueConfig, BacktestDataConfig, BacktestRunConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.identifiers import InstrumentId, Venue

from backtesting.adapters import PolyBotStrategyAdapter
from src.main import PolyBot


def main():
    """Main entry point for the backtest runner."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategy",
        type=str,
        required=True,
        help="The strategy to backtest (bitcoin, sol_macro, eth_macro, hype_macro, xrp_macro, weather).",
    )
    parser.add_argument("--market-slug", type=str, required=True, help="The Polymarket market slug (e.g., 'will-the-fed-cut-rates-in-june-2024').")
    parser.add_argument("--start-time", type=str, required=True, help="The backtest start time (YYYY-MM-DD HH:MM:SS)." )
    parser.add_argument("--end-time", type=str, required=True, help="The backtest end time (YYYY-MM-DD HH:MM:SS)." )
    args = parser.parse_args()

    # Initialize PolyBot to get access to strategies
    poly_bot = PolyBot()

    # Configure the backtest engine
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            venues=[
                BacktestVenueConfig(
                    name=Venue("POLYMARKET"),
                    oms_type="HEDGING",
                    account_type="CASH",
                    base_currency="USD",
                    starting_balances=["10000 USD"],
                ),
            ],
            data=[
                BacktestDataConfig(
                    instrument_id=InstrumentId.from_str(f"{args.market_slug}.POLYMARKET"),
                    start_time=dt_to_unix_nanos(datetime.strptime(args.start_time, "%Y-%m-%d %H:%M:%S")),
                    end_time=dt_to_unix_nanos(datetime.strptime(args.end_time, "%Y-%m-%d %H:%M:%S")),
                ),
            ],
        )
    )

    # Instantiate the strategy adapter
    strategy = PolyBotStrategyAdapter(
        poly_bot=poly_bot,
        strategy_name=args.strategy,
        instrument_id=InstrumentId.from_str(f"{args.market_slug}.POLYMARKET"),
    )

    # Run the backtest
    engine.add_strategy(strategy)
    asyncio.run(engine.run())

    # Print the results
    print(engine.trader.portfolio.performance_report())


if __name__ == "__main__":
    main()
