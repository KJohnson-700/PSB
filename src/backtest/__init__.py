"""Backtesting module for Polymarket strategies."""
from src.backtest.data_loader import PolymarketLoader, PolymarketDataLoader, LocalDataLoader
try:
    from src.backtest.engine import BacktestEngine, BacktestResult, BacktestTrade
except ImportError:
    BacktestEngine = BacktestResult = BacktestTrade = None

__all__ = [
    "PolymarketDataLoader",
    "LocalDataLoader",
    "BacktestEngine",
    "BacktestResult",
    "BacktestTrade",
]
