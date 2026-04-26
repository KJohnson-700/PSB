"""
Unit tests for BacktestEngine: verify entry/exit, fees, slippage, and settlement logic.
"""

import asyncio
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from src.backtest.engine import BacktestEngine


def _make_price_df(prices, start="2024-01-01", freq="1h"):
    """Create a simple DataFrame with given prices."""
    idx = pd.date_range(start=start, periods=len(prices), freq=freq, tz="UTC")
    return pd.DataFrame({"price": prices, "spread": 0.02}, index=idx)


@pytest.fixture
def base_config():
    return {
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
        "trading": {
            "kelly_fraction": 0.25,
            "max_exposure_per_trade": 0.05,
            "default_position_size": 50,
            "max_position_size": 200,
        },
        "backtest": {
            "exit_strategy": "hold_to_settlement",
            "slippage": {"default_bps": 25, "use_spread_when_available": True},
            "fee_bps": 0,
        },
    }


class TestSimulateFill:
    """Test fill simulation with slippage and fees."""

    def test_buy_slippage_increases_fill_price(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="weather")
        fill_price, slip_cost, fee_cost = engine._simulate_fill(
            price=0.50, size=100, side="buy", spread=0.02
        )
        # BUY should fill above midpoint (slippage makes us pay more)
        assert fill_price > 0.50
        assert slip_cost > 0
        assert fee_cost >= 0

    def test_sell_slippage_decreases_fill_price(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="weather")
        fill_price, slip_cost, fee_cost = engine._simulate_fill(
            price=0.50, size=100, side="sell", spread=0.02
        )
        # SELL should fill below midpoint (slippage makes us receive less)
        assert fill_price < 0.50
        assert slip_cost > 0

    def test_fees_applied_when_nonzero(self, base_config):
        base_config["backtest"]["fee_bps"] = 10
        engine = BacktestEngine(base_config, strategy_name="weather")
        _, _, fee_cost = engine._simulate_fill(
            price=0.50, size=100, side="buy", spread=0.02
        )
        # 10 bps on fill_price * size
        expected_fee = (10 / 10_000) * 0.50 * 100
        assert abs(fee_cost - expected_fee) < 0.01

    def test_size_dependent_slippage(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="weather")
        # Small size
        _, slip_small, _ = engine._simulate_fill(
            price=0.50, size=50, side="buy", spread=0.02
        )
        # Large size (should have more slippage due to sqrt scaling)
        _, slip_large, _ = engine._simulate_fill(
            price=0.50, size=500, side="buy", spread=0.02
        )
        # Large size should have proportionally more slippage
        assert slip_large > slip_small


class TestSettlement:
    """Test settlement logic with fees."""

    def test_buy_yes_wins(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="weather")
        positions = [("BUY_YES", 100, 0.30, pd.Timestamp("2024-01-01"))]
        payout, fee = engine._settle_positions(positions, yes_won=True)
        assert payout == 100  # Full payout
        assert fee == 0  # Fee is 0 bps by default

    def test_buy_yes_loses(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="weather")
        positions = [("BUY_YES", 100, 0.30, pd.Timestamp("2024-01-01"))]
        payout, fee = engine._settle_positions(positions, yes_won=False)
        assert payout == 0

    def test_buy_no_wins(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="weather")
        positions = [("BUY_NO", 100, 0.70, pd.Timestamp("2024-01-01"))]
        payout, fee = engine._settle_positions(positions, yes_won=False)
        assert payout == 100

    def test_sell_yes_wins(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="weather")
        positions = [("SELL_YES", 100, 0.90, pd.Timestamp("2024-01-01"))]
        payout, fee = engine._settle_positions(positions, yes_won=True)
        # Selling YES when YES wins = loss of size
        assert payout == -100

    def test_fees_on_settlement(self, base_config):
        base_config["backtest"]["fee_bps"] = 10
        engine = BacktestEngine(base_config, strategy_name="weather")
        positions = [("BUY_YES", 100, 0.30, pd.Timestamp("2024-01-01"))]
        payout, fee = engine._settle_positions(positions, yes_won=True)
        assert payout == 100
        assert fee > 0  # 10 bps on payout
