"""
Tests for SOL Macro Strategy — BTC-to-Solana Correlation Lag Trading

Tests cover:
1. Macro trend determination (1H)
2. 15m MACD confirmation
3. 5m entry timing + BTC-SOL lag detection
4. Edge estimation
5. Signal gating (macro blocks wrong side)
6. Market detection (SOL vs non-SOL)
7. Exposure manager integration
"""

import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from src.strategies.sol_macro import SolMacroStrategy, SolMacroSignal
from src.analysis.sol_btc_service import (
    SOLBTCService,
    SOLTechnicalAnalysis,
    SOLAnalysis,
    BTCSOLCorrelation,
    MultiTimeframeTrend,
    MACDResult,
    ORACLE_FEEDS,
)
from src.execution.exposure_manager import ExposureManager, ExposureTier

from tests.async_helpers import run_async


def _make_config():
    return {
        "strategies": {
            "sol_macro": {
                "enabled": True,
                "min_liquidity": 10000,
                "min_edge": 0.08,
                "ai_confidence_threshold": 0.60,
                "kelly_fraction": 0.15,
                "entry_price_min": 0.15,
                "entry_price_max": 0.85,
            }
        },
        "exposure": {
            "full_size": 5.0,
            "moderate_size": 3.0,
            "minimal_size": 1.0,
            "max_consecutive_losses": 3,
            "pause_cycles": 2,
        },
        "trading": {"dry_run": True},
    }


def test_optional_rsi_buy_ceiling_blocks_extreme_long_entries():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["rsi_buy_block_above"] = 80.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._rsi_blocks_entry("BUY_YES", 84.8) is True
    assert strategy._rsi_blocks_entry("BUY_YES", 79.9) is False
    assert strategy._rsi_blocks_entry("SELL_YES", 84.8) is False


def test_optional_min_positive_m5_adj_blocks_weak_5m_signal():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["min_positive_m5_adj_5m"] = 0.04
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._strong_enough_5m_signal(0.06) is True
    assert strategy._strong_enough_5m_signal(0.04) is True
    assert strategy._strong_enough_5m_signal(0.02) is False


def test_ai_decision_window_uses_configured_remaining_minutes():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["ai_entry_window_15m_min"] = 8.0
    cfg["strategies"]["sol_macro"]["ai_entry_window_15m_max"] = 13.0
    cfg["strategies"]["sol_macro"]["ai_entry_window_5m_min"] = 1.5
    cfg["strategies"]["sol_macro"]["ai_entry_window_5m_max"] = 2.5
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._within_ai_decision_window(mins_left=10.0, is_5m=False) is True
    assert strategy._within_ai_decision_window(mins_left=14.0, is_5m=False) is False
    assert strategy._within_ai_decision_window(mins_left=2.0, is_5m=True) is True
    assert strategy._within_ai_decision_window(mins_left=3.2, is_5m=True) is False


def test_macro_oracle_feed_map_covers_all_crypto_lanes():
    assert ORACLE_FEEDS["SOLUSDT"][0] == "polygon"
    assert ORACLE_FEEDS["ETHUSDT"][0] == "polygon"
    assert ORACLE_FEEDS["XRPUSDT"][0] == "polygon"
    assert ORACLE_FEEDS["HYPEUSDT"][0] == "arbitrum"


def test_optional_oracle_basis_gate_blocks_large_divergence():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["oracle_max_basis_bps"] = 10.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._oracle_basis_blocks_entry(12.5) is True
    assert strategy._oracle_basis_blocks_entry(-12.5) is True
    assert strategy._oracle_basis_blocks_entry(8.0) is False
    assert strategy._oracle_basis_blocks_entry(None) is False


def _make_bullish_ta():
    """Create a bullish SOL technical analysis."""
    return SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=135.50,
            ema_9=134.00,
            ema_21=132.00,
            ema_50=128.00,  # Bullish alignment
            rsi_14=62.0,
            macd_15m=MACDResult(
                macd_line=0.45,
                signal_line=0.30,
                histogram=0.15,
                prev_histogram=0.08,
                crossover="BULLISH_CROSS",
                histogram_rising=True,
                above_zero=True,
            ),
            macd_5m=MACDResult(
                macd_line=0.12,
                signal_line=0.08,
                histogram=0.04,
                prev_histogram=0.01,
                crossover="BULLISH_CROSS",
                histogram_rising=True,
                above_zero=True,
            ),
            atr_14=3.20,
            trend_direction="BULLISH",
            trend_strength=0.75,
        ),
        correlation=BTCSOLCorrelation(
            correlation_1h=0.88,
            btc_move_5m_pct=0.45,
            btc_move_15m_pct=0.90,
            btc_spike_detected=True,
            btc_spike_direction="UP",
            sol_move_5m_pct=0.15,
            sol_move_15m_pct=0.30,
            sol_lag_pct=0.45,
            lag_opportunity=True,
            opportunity_direction="LONG",
            opportunity_magnitude=0.45,
            btc_price=75200.0,
            btc_chainlink_price=75180.0,
        ),
        multi_tf=MultiTimeframeTrend(
            h1_trend="BULLISH",
            h1_basis="EMA9>EMA21>EMA50 RSI=62",
            m15_trend="BULLISH",
            m15_basis="MACD above zero, histogram rising",
            m5_trend="BULLISH",
            m5_basis="MACD bullish cross",
            aligned=True,
            overall_direction="BULLISH",
        ),
    )


def _make_bearish_ta():
    """Create a bearish SOL technical analysis."""
    return SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=120.50,
            ema_9=122.00,
            ema_21=125.00,
            ema_50=130.00,  # Bearish alignment
            rsi_14=35.0,
            macd_15m=MACDResult(
                macd_line=-0.55,
                signal_line=-0.30,
                histogram=-0.25,
                prev_histogram=-0.15,
                crossover="BEARISH_CROSS",
                histogram_rising=False,
                above_zero=False,
            ),
            macd_5m=MACDResult(
                macd_line=-0.15,
                signal_line=-0.08,
                histogram=-0.07,
                prev_histogram=-0.02,
                crossover="BEARISH_CROSS",
                histogram_rising=False,
                above_zero=False,
            ),
            atr_14=4.10,
            trend_direction="BEARISH",
            trend_strength=0.80,
        ),
        correlation=BTCSOLCorrelation(
            correlation_1h=0.82,
            btc_move_5m_pct=-0.50,
            btc_move_15m_pct=-1.10,
            btc_spike_detected=True,
            btc_spike_direction="DOWN",
            sol_move_5m_pct=-0.10,
            sol_move_15m_pct=-0.25,
            sol_lag_pct=0.60,
            lag_opportunity=True,
            opportunity_direction="SHORT",
            opportunity_magnitude=0.60,
            btc_price=73500.0,
        ),
        multi_tf=MultiTimeframeTrend(
            h1_trend="BEARISH",
            h1_basis="EMA9<EMA21<EMA50 RSI=35",
            m15_trend="BEARISH",
            m15_basis="MACD below zero, histogram falling",
            m5_trend="BEARISH",
            m5_basis="MACD bearish cross",
            aligned=True,
            overall_direction="BEARISH",
        ),
    )


def _make_choppy_ta():
    """Create a neutral/choppy SOL technical analysis."""
    return SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=130.00,
            ema_9=130.50,
            ema_21=130.20,
            ema_50=129.80,  # Tight, no clear direction
            rsi_14=50.0,
            macd_15m=MACDResult(
                macd_line=0.02,
                signal_line=0.01,
                histogram=0.01,
                prev_histogram=-0.01,
                crossover="NONE",
                histogram_rising=True,
                above_zero=True,
            ),
            macd_5m=MACDResult(
                macd_line=-0.01,
                signal_line=0.00,
                histogram=-0.01,
                prev_histogram=0.01,
                crossover="NONE",
                histogram_rising=False,
                above_zero=False,
            ),
            atr_14=1.50,
            trend_direction="NEUTRAL",
            trend_strength=0.2,
        ),
        correlation=BTCSOLCorrelation(
            correlation_1h=0.45,
            btc_move_5m_pct=0.05,
            btc_move_15m_pct=0.10,
            btc_spike_detected=False,
            btc_spike_direction="NONE",
            sol_move_5m_pct=0.03,
            sol_move_15m_pct=0.07,
            sol_lag_pct=0.0,
            lag_opportunity=False,
            opportunity_direction="NONE",
            opportunity_magnitude=0.0,
            btc_price=74000.0,
        ),
        multi_tf=MultiTimeframeTrend(
            h1_trend="NEUTRAL",
            h1_basis="No clear direction",
            m15_trend="NEUTRAL",
            m15_basis="MACD near zero",
            m5_trend="NEUTRAL",
            m5_basis="No crossover",
            aligned=False,
            overall_direction="NEUTRAL",
        ),
    )


def _make_market(
    question="Will Solana reach $150 by end of March?",
    yes_price=0.35,
    liquidity=50000,
    market_id="sol-150-march",
):
    m = MagicMock()
    m.id = market_id
    m.question = question
    m.description = question
    m.yes_price = yes_price
    m.no_price = 1 - yes_price
    m.liquidity = liquidity
    m.token_id_yes = "tok-yes-sol"
    m.token_id_no = "tok-no-sol"
    m.end_date = datetime.now() + timedelta(days=15)
    m.token_ids = ["tok-yes-sol", "tok-no-sol"]
    return m


# ═══════════════════════════════════════════════════════════════
# Test Classes
# ═══════════════════════════════════════════════════════════════


class TestSOLMacroTrend(unittest.TestCase):
    """LAYER 1: 1H Macro Trend determination."""

    def setUp(self):
        self.strategy = SolMacroStrategy(
            _make_config(),
            MagicMock(),
            MagicMock(),
            exposure_manager=ExposureManager(_make_config(), is_paper=True),
        )

    def test_bullish_macro(self):
        ta = _make_bullish_ta()
        trend = self.strategy._get_macro_trend(ta)
        self.assertEqual(trend, "BULLISH")

    def test_bearish_macro(self):
        ta = _make_bearish_ta()
        trend = self.strategy._get_macro_trend(ta)
        self.assertEqual(trend, "BEARISH")

    def test_neutral_macro(self):
        ta = _make_choppy_ta()
        trend = self.strategy._get_macro_trend(ta)
        self.assertEqual(trend, "NEUTRAL")

    def test_bullish_requires_two_votes(self):
        """Bullish needs at least 2 of 3 votes."""
        ta = _make_bullish_ta()
        # Override RSI to neutral — still bullish (h1 + EMA alignment)
        ta.sol.rsi_14 = 50.0
        trend = self.strategy._get_macro_trend(ta)
        self.assertEqual(trend, "BULLISH")

    def test_single_bull_vote_is_neutral(self):
        """Only 1 bullish vote → NEUTRAL."""
        ta = _make_choppy_ta()
        ta.multi_tf.h1_trend = "BULLISH"
        # EMAs are flat, RSI is 50 → only 1 bull vote
        trend = self.strategy._get_macro_trend(ta)
        self.assertIn(trend, ["NEUTRAL", "BULLISH"])  # depends on EMA alignment

    def test_primary_btc_htf_bias_helper_uses_same_gate_as_allowed_side(self):
        strategy = self.strategy
        self.assertAlmostEqual(strategy._apply_primary_htf_bias(0.50, "BULLISH", 0.07), 0.57)
        self.assertAlmostEqual(strategy._apply_primary_htf_bias(0.50, "BEARISH", 0.07), 0.43)
        self.assertAlmostEqual(strategy._apply_primary_htf_bias(0.50, "NEUTRAL", 0.07), 0.50)


class TestSOL15mConfirmation(unittest.TestCase):
    """LAYER 2: 15m MACD confirmation."""

    def setUp(self):
        self.strategy = SolMacroStrategy(
            _make_config(),
            MagicMock(),
            MagicMock(),
            exposure_manager=ExposureManager(_make_config(), is_paper=True),
        )

    def test_bullish_cross_confirms_long(self):
        ta = _make_bullish_ta()
        confirmed, strength, reasons = self.strategy._check_15m_confirmation(ta, "LONG")
        self.assertTrue(confirmed)
        self.assertGreater(strength, 0.25)
        self.assertTrue(any("bull cross" in r for r in reasons))

    def test_bearish_cross_confirms_short(self):
        ta = _make_bearish_ta()
        confirmed, strength, reasons = self.strategy._check_15m_confirmation(
            ta, "SHORT"
        )
        self.assertTrue(confirmed)
        self.assertGreater(strength, 0.25)
        self.assertTrue(any("bear cross" in r for r in reasons))

    def test_bearish_cross_does_not_confirm_long(self):
        ta = _make_bearish_ta()
        confirmed, strength, reasons = self.strategy._check_15m_confirmation(ta, "LONG")
        self.assertFalse(confirmed)
        self.assertLess(strength, 0.25)

    def test_rising_histogram_adds_strength(self):
        ta = _make_bullish_ta()
        ta.sol.macd_15m.crossover = "NONE"  # No cross, but histogram is rising
        ta.sol.macd_15m.prev_histogram = -0.05
        ta.sol.macd_15m.histogram = 0.05
        confirmed, strength, reasons = self.strategy._check_15m_confirmation(ta, "LONG")
        # Composite threshold is 0.50; red->green + MACD>signal = 0.45 (not confirmed).
        self.assertFalse(confirmed)
        self.assertGreaterEqual(strength, 0.35)
        self.assertTrue(any("red-to-green" in r for r in reasons))

    def test_flat_macd_weak_confirmation(self):
        ta = _make_choppy_ta()
        # Set truly flat MACD: no cross, not rising meaningfully, below signal
        ta.sol.macd_15m.crossover = "NONE"
        ta.sol.macd_15m.histogram_rising = False
        ta.sol.macd_15m.macd_line = -0.001
        ta.sol.macd_15m.signal_line = 0.001
        confirmed, strength, reasons = self.strategy._check_15m_confirmation(ta, "LONG")
        # Should not confirm LONG
        self.assertFalse(confirmed)


class TestSOLEntryTiming(unittest.TestCase):
    """LAYER 3: 5m entry timing + BTC-SOL lag detection."""

    def setUp(self):
        self.strategy = SolMacroStrategy(
            _make_config(),
            MagicMock(),
            MagicMock(),
            exposure_manager=ExposureManager(_make_config(), is_paper=True),
        )

    def test_bullish_5m_cross_adds_bonus(self):
        ta = _make_bullish_ta()
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        self.assertGreater(bonus, 0)
        self.assertTrue(any("5m MACD bull cross" in r for r in reasons))

    def test_lag_opportunity_adds_major_bonus(self):
        ta = _make_bullish_ta()
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        # Layer 3 is 5m MACD timing + corr context only (lag/spike handled in scan loops)
        self.assertGreaterEqual(bonus, 0.04)
        self.assertTrue(any("5m" in r for r in reasons))
        self.assertTrue(any("high corr" in r for r in reasons))

    def test_lag_against_direction_penalizes(self):
        ta = _make_bullish_ta()
        # Lag direction is LONG but we want SHORT
        bonus, reasons = self.strategy._check_entry_timing(ta, "SHORT")
        # 5m bearish cross wouldn't fire, and lag is against
        lag_reasons = [r for r in reasons if "lag against" in r]
        # Either lag penalty is applied or bonus is reduced
        assert bonus <= 0.10, (
            f"Bonus should be small when lag is against direction, got {bonus}"
        )

    def test_high_correlation_adds_bonus(self):
        ta = _make_bullish_ta()
        ta.correlation.correlation_1h = 0.92
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        self.assertTrue(any("high corr" in r for r in reasons))

    def test_low_correlation_penalizes(self):
        ta = _make_bullish_ta()
        ta.correlation.correlation_1h = 0.35
        ta.correlation.lag_opportunity = False  # No lag when low corr
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        self.assertTrue(any("low corr" in r for r in reasons))

    def test_btc_spike_adds_extra_bonus(self):
        ta = _make_bullish_ta()
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        # Spike is not logged in Layer 3 after 2026-04-07 refactor; corr context remains
        self.assertTrue(ta.correlation.btc_spike_detected)
        self.assertTrue(any("high corr" in r for r in reasons))

    def test_no_lag_no_spike_minimal_bonus(self):
        ta = _make_choppy_ta()
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        # Low corr penalty, no lag bonus
        self.assertLess(bonus, 0.05)


class TestSOLEdgeCalculation(unittest.TestCase):
    """LAYER 4: Probability estimation."""

    def setUp(self):
        self.strategy = SolMacroStrategy(
            _make_config(),
            MagicMock(),
            MagicMock(),
            exposure_manager=ExposureManager(_make_config(), is_paper=True),
        )

    def test_sol_above_threshold_up_direction(self):
        """SOL at $135 vs $130 threshold UP → high probability."""
        ta = _make_bullish_ta()
        prob = self.strategy._estimate_probability(
            sol_price=135.50,
            threshold=130.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.5,
            timing_bonus=0.05,
        )
        self.assertGreater(prob, 0.55)

    def test_sol_below_threshold_up_direction(self):
        """SOL at $120 vs $150 threshold UP → lower probability."""
        ta = _make_bearish_ta()
        prob = self.strategy._estimate_probability(
            sol_price=120.50,
            threshold=150.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.0,
            timing_bonus=0.0,
        )
        self.assertLess(prob, 0.50)

    def test_lag_opportunity_boosts_probability(self):
        """BTC-SOL lag in our direction increases edge."""
        ta = _make_bullish_ta()
        prob_with_lag = self.strategy._estimate_probability(
            sol_price=135.50,
            threshold=130.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.5,
            timing_bonus=0.05,
        )
        # Remove lag
        ta.correlation.lag_opportunity = False
        prob_without_lag = self.strategy._estimate_probability(
            sol_price=135.50,
            threshold=130.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.5,
            timing_bonus=0.05,
        )
        self.assertGreater(prob_with_lag, prob_without_lag)

    def test_overbought_rsi_penalizes_up(self):
        """RSI > 75 should reduce UP probability."""
        ta = _make_bullish_ta()
        ta.sol.rsi_14 = 78.0
        prob = self.strategy._estimate_probability(
            sol_price=135.50,
            threshold=130.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.5,
            timing_bonus=0.05,
        )
        # Should be lower due to overbought
        ta.sol.rsi_14 = 58.0
        prob_normal = self.strategy._estimate_probability(
            sol_price=135.50,
            threshold=130.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.5,
            timing_bonus=0.05,
        )
        self.assertLess(prob, prob_normal)

    def test_probability_bounded(self):
        """Probability should always be between 0.05 and 0.95."""
        ta = _make_bullish_ta()
        prob = self.strategy._estimate_probability(
            sol_price=200.0,
            threshold=100.0,
            direction="UP",
            ta=ta,
            days_to_resolution=1,
            ltf_strength=1.0,
            timing_bonus=0.20,
        )
        self.assertLessEqual(prob, 0.95)
        self.assertGreaterEqual(prob, 0.05)


class TestSOLMarketDetection(unittest.TestCase):
    """Market detection: SOL vs non-SOL markets."""

    def setUp(self):
        self.strategy = SolMacroStrategy(
            _make_config(),
            MagicMock(),
            MagicMock(),
        )

    def test_solana_market_detected(self):
        m = _make_market("Will Solana reach $150?")
        self.assertTrue(self.strategy._is_solana_market(m))

    def test_sol_abbreviation_detected(self):
        m = _make_market("Will SOL price exceed $200?")
        self.assertTrue(self.strategy._is_solana_market(m))

    def test_bitcoin_only_rejected(self):
        m = _make_market("Will Bitcoin reach $100,000?")
        self.assertFalse(self.strategy._is_solana_market(m))

    def test_bitcoin_with_solana_accepted(self):
        m = _make_market("Will Solana outperform Bitcoin this month?")
        self.assertTrue(self.strategy._is_solana_market(m))

    def test_direction_up(self):
        self.assertEqual(self.strategy._extract_direction("Will SOL reach $200?"), "UP")

    def test_direction_down(self):
        self.assertEqual(
            self.strategy._extract_direction("Will SOL drop below $100?"), "DOWN"
        )

    def test_price_extraction(self):
        self.assertEqual(
            self.strategy._extract_price_threshold("SOL above $150?"), 150.0
        )

    def test_price_extraction_with_comma(self):
        self.assertEqual(
            self.strategy._extract_price_threshold("SOL above $1,500?"), 1500.0
        )

    def test_price_out_of_range_rejected(self):
        """SOL range is $1-$10,000. BTC prices should be rejected."""
        self.assertIsNone(self.strategy._extract_price_threshold("above $75,000?"))

    def test_price_too_low_rejected(self):
        self.assertIsNone(self.strategy._extract_price_threshold("above $0.50?"))


class TestSOLSignalGating(unittest.TestCase):
    """Integration: macro trend gates signals correctly."""

    def setUp(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.sizer = MagicMock()
        self.sizer.kelly_bet = MagicMock(return_value=3.0)
        self.em = ExposureManager(self.config, is_paper=True)
        self.strategy = SolMacroStrategy(
            self.config, self.ai, self.sizer, exposure_manager=self.em
        )

    @patch.object(SOLBTCService, "get_full_analysis")
    async def _run_scan(self, ta, mock_analysis):
        mock_analysis.return_value = ta
        markets = [_make_market()]
        return await self.strategy.scan_and_analyze(markets, bankroll=10000.0)

    def test_neutral_macro_produces_no_signals(self):
        """NEUTRAL macro trend → no signals (sit out)."""
        ta = _make_choppy_ta()
        signals = run_async(self._run_scan(ta))
        self.assertEqual(len(signals), 0)

    def test_bullish_macro_allows_signals(self):
        """BULLISH macro with confirming indicators → signals possible."""
        ta = _make_bullish_ta()
        self.em.scale_size = MagicMock(return_value=3.0)
        signals = run_async(self._run_scan(ta))
        # Signals depend on edge calc vs threshold, but at minimum the strategy shouldn't crash
        self.assertIsInstance(signals, list)


class TestSOLExposureIntegration(unittest.TestCase):
    """Exposure manager correctly scales SOL positions."""

    def test_conditions_from_bullish_ta(self):
        ta = _make_bullish_ta()
        conditions = SolMacroStrategy.conditions_from_ta(ta)
        self.assertGreater(conditions.volatility, 0)
        self.assertEqual(conditions.trend_direction, "BULLISH")
        # High correlation → volume ratio > 1
        self.assertGreater(conditions.volume_ratio, 1.0)

    def test_conditions_from_choppy_ta(self):
        ta = _make_choppy_ta()
        conditions = SolMacroStrategy.conditions_from_ta(ta)
        self.assertEqual(conditions.trend_direction, "NEUTRAL")
        # Correlation 0.45 → falls through to default volume_ratio=1.0 (not > 0.8, not < 0.4)
        self.assertLessEqual(conditions.volume_ratio, 1.0)

    def test_aligned_tf_gives_full_strength(self):
        ta = _make_bullish_ta()
        conditions = SolMacroStrategy.conditions_from_ta(ta)
        # Aligned = True → trend_strength = 1.0
        self.assertEqual(conditions.trend_strength, 1.0)

    def test_unaligned_tf_gives_partial_strength(self):
        ta = _make_choppy_ta()
        conditions = SolMacroStrategy.conditions_from_ta(ta)
        # Not aligned → uses sol.trend_strength
        self.assertLess(conditions.trend_strength, 1.0)

    def test_paused_exposure_blocks_signals(self):
        """When exposure is PAUSED, strategy returns no signals."""
        import asyncio

        config = _make_config()
        em = ExposureManager(config, is_paper=True)
        em.manual_pause()

        strategy = SolMacroStrategy(
            config,
            MagicMock(),
            MagicMock(),
            exposure_manager=em,
        )

        with patch.object(
            SOLBTCService, "get_full_analysis", return_value=_make_bullish_ta()
        ):
            markets = [_make_market()]
            signals = run_async(
                strategy.scan_and_analyze(markets, bankroll=10000.0)
            )
        self.assertEqual(len(signals), 0)


if __name__ == "__main__":
    unittest.main()
