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
            "fade": {
                "enabled": True,
                "consensus_threshold_lower": 0.80,
                "consensus_threshold_upper": 0.95,
                "ai_confidence_threshold": 0.60,
                "ipg_min": 0.10,
                "kelly_fraction": 0.10,
                "entry_price_min": 0.15,
                "entry_price_max": 0.45,
                "fee_buffer": 0.02,
            },
            "arbitrage": {
                "enabled": True,
                "min_edge": 0.10,
                "ai_confidence_threshold": 0.70,
                "fee_buffer": 0.02,
                "safety_margin": 0.03,
                "entry_price_min": 0.20,
                "entry_price_max": 0.40,
            },
            "neh": {
                "enabled": True,
                "max_yes_price": 0.15,
                "min_days_to_resolution": 30,
                "min_liquidity": 0,
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
        engine = BacktestEngine(base_config, strategy_name="fade")
        fill_price, slip_cost, fee_cost = engine._simulate_fill(
            price=0.50, size=100, side="buy", spread=0.02
        )
        # BUY should fill above midpoint (slippage makes us pay more)
        assert fill_price > 0.50
        assert slip_cost > 0
        assert fee_cost >= 0

    def test_sell_slippage_decreases_fill_price(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="fade")
        fill_price, slip_cost, fee_cost = engine._simulate_fill(
            price=0.50, size=100, side="sell", spread=0.02
        )
        # SELL should fill below midpoint (slippage makes us receive less)
        assert fill_price < 0.50
        assert slip_cost > 0

    def test_fees_applied_when_nonzero(self, base_config):
        base_config["backtest"]["fee_bps"] = 10
        engine = BacktestEngine(base_config, strategy_name="fade")
        _, _, fee_cost = engine._simulate_fill(
            price=0.50, size=100, side="buy", spread=0.02
        )
        # 10 bps on fill_price * size
        expected_fee = (10 / 10_000) * 0.50 * 100
        assert abs(fee_cost - expected_fee) < 0.01

    def test_size_dependent_slippage(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="fade")
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
        engine = BacktestEngine(base_config, strategy_name="fade")
        positions = [("BUY_YES", 100, 0.30, pd.Timestamp("2024-01-01"))]
        payout, fee = engine._settle_positions(positions, yes_won=True)
        assert payout == 100  # Full payout
        assert fee == 0  # Fee is 0 bps by default

    def test_buy_yes_loses(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="fade")
        positions = [("BUY_YES", 100, 0.30, pd.Timestamp("2024-01-01"))]
        payout, fee = engine._settle_positions(positions, yes_won=False)
        assert payout == 0

    def test_buy_no_wins(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="fade")
        positions = [("BUY_NO", 100, 0.70, pd.Timestamp("2024-01-01"))]
        payout, fee = engine._settle_positions(positions, yes_won=False)
        assert payout == 100

    def test_sell_yes_wins(self, base_config):
        engine = BacktestEngine(base_config, strategy_name="fade")
        positions = [("SELL_YES", 100, 0.90, pd.Timestamp("2024-01-01"))]
        payout, fee = engine._settle_positions(positions, yes_won=True)
        # Selling YES when YES wins = loss of size
        assert payout == -100

    def test_fees_on_settlement(self, base_config):
        base_config["backtest"]["fee_bps"] = 10
        engine = BacktestEngine(base_config, strategy_name="fade")
        positions = [("BUY_YES", 100, 0.30, pd.Timestamp("2024-01-01"))]
        payout, fee = engine._settle_positions(positions, yes_won=True)
        assert payout == 100
        assert fee > 0  # 10 bps on payout


class TestRunEngine:
    """Integration tests: run engine on simple price paths."""

    def test_ruin_cap(self, base_config):
        """Engine should not go below 0 bankroll."""
        engine = BacktestEngine(base_config, strategy_name="fade", initial_bankroll=100)
        # Very volatile prices that should drain bankroll
        prices = [0.95] * 50 + [0.05] * 50
        df = _make_price_df(prices)
        result = asyncio.run(engine.run(df, slug="test-ruin", resolution_outcome=True))
        assert result.final_bankroll >= 0

    def test_no_trades_no_change(self, base_config):
        """If no signals generated, bankroll should stay the same."""
        base_config["strategies"]["fade"]["enabled"] = True
        # Prices in no-signal zone (0.40-0.60)
        prices = [0.50] * 50
        df = _make_price_df(prices)
        engine = BacktestEngine(
            base_config, strategy_name="fade", initial_bankroll=1000
        )
        result = asyncio.run(engine.run(df, slug="test-noop", resolution_outcome=True))
        assert result.final_bankroll == 1000
        assert result.num_trades == 0

    def test_exit_strategy_time_and_target(self, base_config):
        """Test that time_and_target exit strategy works."""
        base_config["backtest"]["exit_strategy"] = "time_and_target"
        base_config["backtest"]["max_hold_hours"] = 3
        base_config["backtest"]["take_profit_pct"] = 0.10
        base_config["backtest"]["stop_loss_pct"] = 0.10
        # Prices: start at 0.88 (fade signal), then spike up (take profit), then crash
        prices = [0.88] * 2 + [0.98] * 10 + [0.10] * 10
        df = _make_price_df(prices)
        engine = BacktestEngine(
            base_config, strategy_name="fade", initial_bankroll=1000
        )
        result = asyncio.run(engine.run(df, slug="test-exit", resolution_outcome=True))
        # Should have exit trades in addition to entry trades
        exit_trades = [t for t in result.trades if t.action.startswith("EXIT_")]
        # If the fade generated a signal, exits should fire
        if result.num_trades > 1:
            assert len(exit_trades) > 0
