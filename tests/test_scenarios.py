"""
Scenario tests — realistic price paths with KNOWN outcomes.
Each scenario represents an actual market pattern (not random walks).
Tests that strategies would have been profitable on real dynamics.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from src.backtest.engine import BacktestEngine, BacktestResult

from tests.async_helpers import run_async


def _make_config():
    return {
        "polymarket": {"min_liquidity": 10000, "max_spread": 0.05},
        "trading": {
            "kelly_fraction": 0.25,
            "max_exposure_per_trade": 0.05,
            "default_position_size": 50,
            "max_position_size": 200,
        },
        "strategies": {
            "weather": {
                "enabled": True,
                "gap_min": 0.15,
                "min_ev": 0.05,
                "min_volume": 0,
                "min_hours_to_resolution": 0,
                "max_hours_to_resolution": 9999,
                "max_yes_price": 0.50,
                "kelly_fraction": 0.25,
                "max_position_size": 200,
            },
        },
        "backtest": {
            "exit_strategy": "time_and_target",
            "max_hold_hours": 72,
            "take_profit_pct": 0.20,
            "stop_loss_pct": 0.15,
            "slippage": {"default_bps": 25, "use_spread_when_available": True},
            "fee_bps": 0,
        },
    }


def _make_price_series(prices, freq="1h"):
    """Build a DataFrame from a list of prices."""
    dates = pd.date_range("2024-06-01", periods=len(prices), freq=freq, tz="UTC")
    return pd.DataFrame({"price": prices, "spread": 0.02}, index=pd.DatetimeIndex(dates, name="t"))
