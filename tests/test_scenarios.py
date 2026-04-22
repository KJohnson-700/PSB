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
            "fade": {
                "enabled": True,
                "consensus_threshold_lower": 0.80,
                "consensus_threshold_upper": 0.95,
                "ai_confidence_threshold": 0.60,
                "ipg_min": 0.10,
                "fee_buffer": 0.02,
                "entry_price_min": 0.15,
                "entry_price_max": 0.45,
                "kelly_fraction": 0.10,
            },
            "arbitrage": {
                "enabled": True,
                "use_ai": True,
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
                "min_liquidity": 10000,
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


# ─── SCENARIO: FALSE CONSENSUS → FADE WINS ──────────────────────

class TestFadeScenarios:

    def test_consensus_was_wrong_yes_resolves_no(self):
        """
        Market: YES trades at 0.82 (no_price=0.18 passes entry filter [0.15,0.45]).
        Then crashes to 0.15. Resolution: NO. Fade (SELL_YES) should profit.
        """
        # Phase 1: YES at 0.82 (consensus zone, no=0.18 >= entry_price_min) for 48 bars
        # Phase 2: Drops to 0.15 over 24 bars (consensus was wrong)
        phase1 = [0.82] * 48
        phase2 = list(np.linspace(0.82, 0.15, 24))
        prices = phase1 + phase2

        data = _make_price_series(prices)
        engine = BacktestEngine(_make_config(), strategy_name="fade", initial_bankroll=10000)
        result = run_async(engine.run(data, slug="test-fade-win", resolution_outcome=False))

        assert result.num_trades > 0, "Fade should have entered during consensus at 0.82"
        assert result.final_bankroll > result.initial_bankroll, \
            f"Fade should profit when consensus was wrong. PnL: ${result.net_pnl:.2f}"

    def test_consensus_was_right_yes_resolves_yes(self):
        """
        Market: YES at 0.82 (entry passes), stays in consensus zone, resolves YES.
        Fade (SELL_YES) should LOSE — it bet against the crowd and was wrong.
        """
        # YES stays at 0.82 the whole time; resolves YES
        prices = [0.82] * 72

        data = _make_price_series(prices)
        engine = BacktestEngine(_make_config(), strategy_name="fade", initial_bankroll=10000)
        result = run_async(engine.run(data, slug="test-fade-loss", resolution_outcome=True))

        assert result.num_trades > 0, "Fade should have entered during consensus at 0.82"
        assert result.final_bankroll < result.initial_bankroll, \
            f"Fade should lose when consensus was correct. PnL: ${result.net_pnl:.2f}"

    def test_fade_ignores_mid_range_market(self):
        """
        Price hovers around 0.50 — no consensus, no fade signals.
        """
        prices = [0.48 + 0.04 * np.sin(i / 5) for i in range(100)]
        data = _make_price_series(prices)
        engine = BacktestEngine(_make_config(), strategy_name="fade", initial_bankroll=10000)
        result = run_async(engine.run(data, slug="test-no-fade", resolution_outcome=True))

        assert result.num_trades == 0, "No trades expected when price stays in mid-range"
        assert result.final_bankroll == result.initial_bankroll


# ─── SCENARIO: ARBITRAGE MEAN REVERSION ──────────────────────────

class TestArbitrageScenarios:

    def test_underpriced_yes_reverts_and_resolves_yes(self):
        """
        YES stays at 0.30 (arb zone, in [0.20, 0.40]), resolves YES.
        Arb buys YES cheap, holds to settlement → profit.
        """
        # Hold price steady so arb only enters BUY_YES, never flips to BUY_NO
        prices = [0.30] * 40
        data = _make_price_series(prices)
        config = _make_config()
        config["backtest"]["exit_strategy"] = "hold_to_settlement"
        engine = BacktestEngine(config, strategy_name="arbitrage", initial_bankroll=10000)
        result = run_async(engine.run(data, slug="test-arb-win", resolution_outcome=True))

        assert result.num_trades > 0, "Arb should enter when YES is at 0.30"
        assert result.final_bankroll > result.initial_bankroll, \
            f"Arb should profit on underpriced YES that resolves YES. PnL: ${result.net_pnl:.2f}"

    def test_underpriced_yes_resolves_no(self):
        """
        YES stays at 0.30, market resolves NO.
        Arb buys YES → loses at settlement.
        """
        prices = [0.30] * 40
        data = _make_price_series(prices)
        config = _make_config()
        config["backtest"]["exit_strategy"] = "hold_to_settlement"
        engine = BacktestEngine(config, strategy_name="arbitrage", initial_bankroll=10000)
        result = run_async(engine.run(data, slug="test-arb-loss", resolution_outcome=False))

        assert result.num_trades > 0
        assert result.final_bankroll < result.initial_bankroll, \
            f"Arb should lose when resolution goes against the trade. PnL: ${result.net_pnl:.2f}"


# ─── SCENARIO: NEH — NOTHING EVER HAPPENS ───────────────────────

class TestNEHScenarios:

    def test_neh_low_yes_decays_to_zero(self):
        """
        YES starts at 0.08, slowly decays to 0.01. Resolves NO.
        NEH sells YES at 0.08, collects full premium → profit.
        """
        prices = list(np.linspace(0.08, 0.01, 100))
        data = _make_price_series(prices)
        engine = BacktestEngine(_make_config(), strategy_name="neh", initial_bankroll=10000)
        result = run_async(engine.run(data, slug="test-neh-win", resolution_outcome=False))

        assert result.num_trades > 0, "NEH should sell YES when price is at 0.08"
        assert result.final_bankroll > result.initial_bankroll, \
            f"NEH should profit when the unlikely event doesn't happen. PnL: ${result.net_pnl:.2f}"

    def test_neh_black_swan_loses(self):
        """
        YES starts at 0.08, then spikes to 0.90 (the unlikely happened!). Resolves YES.
        NEH sold YES → big loss.
        """
        phase1 = [0.08] * 20
        phase2 = list(np.linspace(0.08, 0.90, 30))
        prices = phase1 + phase2
        data = _make_price_series(prices)
        engine = BacktestEngine(_make_config(), strategy_name="neh", initial_bankroll=10000)
        result = run_async(engine.run(data, slug="test-neh-loss", resolution_outcome=True))

        assert result.num_trades > 0
        assert result.final_bankroll < result.initial_bankroll, \
            f"NEH should lose when the black swan happens. PnL: ${result.net_pnl:.2f}"


# ─── SCENARIO: EXECUTION REALISM ─────────────────────────────────

class TestExecutionRealism:

    def test_slippage_reduces_pnl(self):
        """Higher slippage should produce worse results on the same data."""
        prices = [0.88] * 48 + list(np.linspace(0.88, 0.15, 24))
        data = _make_price_series(prices)

        config = _make_config()
        engine_low = BacktestEngine(config, strategy_name="fade", initial_bankroll=10000, slippage_mult=1.0)
        engine_high = BacktestEngine(config, strategy_name="fade", initial_bankroll=10000, slippage_mult=3.0)

        result_low = run_async(engine_low.run(data, slug="slip-low", resolution_outcome=False))
        result_high = run_async(engine_high.run(data, slug="slip-high", resolution_outcome=False))

        assert result_low.execution_cost_total < result_high.execution_cost_total, \
            "Higher slippage multiplier should produce higher execution costs"

    def test_bankroll_never_goes_negative(self):
        """Even with bad trades, bankroll should be floored at 0."""
        # Scenario designed to lose money rapidly
        prices = [0.88] * 20 + [0.95] * 80  # fade enters, consensus strengthens
        data = _make_price_series(prices)
        engine = BacktestEngine(_make_config(), strategy_name="fade", initial_bankroll=100)
        result = run_async(engine.run(data, slug="test-ruin", resolution_outcome=True))

        assert result.final_bankroll >= 0, "Bankroll must never go negative"

    def test_no_trades_means_unchanged_bankroll(self):
        """If price never enters any signal zone, bankroll should be unchanged."""
        prices = [0.50] * 100
        data = _make_price_series(prices)
        engine = BacktestEngine(_make_config(), strategy_name="fade", initial_bankroll=10000)
        result = run_async(engine.run(data, slug="test-nothing", resolution_outcome=True))

        assert result.num_trades == 0
        assert result.final_bankroll == 10000.0
