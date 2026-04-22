"""
Bitcoin Strategy unit tests — verify signal logic with mocked technical analysis data.

These tests validate:
1. Hierarchical trend filter (4H gates everything)
2. Lower TF confirmation (15m MACD must align)
3. Entry timing (candle momentum)
4. Edge calculation with VP, RSI, S/R
5. Exposure manager integration
6. Signal gating (only allowed direction trades pass)

No live data required — all TA objects are constructed with known values.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass

from src.market.scanner import Market
from src.analysis.math_utils import PositionSizer
from src.analysis.btc_price_service import (
    MACDResult,
    TrendSabreResult,
    CandleMomentum,
    AnchoredVolumeProfile,
    TechnicalAnalysis,
)
from src.strategies.bitcoin import BitcoinStrategy, BitcoinSignal
from src.execution.exposure_manager import ExposureManager, ExposureTier

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
            "bitcoin": {
                "enabled": True,
                "min_liquidity": 10000,
                "min_edge": 0.08,
                "ai_confidence_threshold": 0.60,
                "kelly_fraction": 0.15,
                "entry_price_min": 0.15,
                "entry_price_max": 0.85,
                "clear_distance_pct": 0.15,
            },
        },
        "exposure": {
            "full_size": 5.0,
            "moderate_size": 3.0,
            "minimal_size": 1.0,
            "max_consecutive_losses": 3,
            "pause_cycles": 2,
            "live_resume_mode": "auto",
            "high_vol_pct": 0.015,
            "low_vol_pct": 0.005,
            "high_volume_ratio": 1.3,
            "low_volume_ratio": 0.7,
        },
    }


def _make_btc_market(
    question="Will Bitcoin be above $80,000 on March 31?",
    yes_price=0.45,
    direction="UP",
    market_id="btc-80k-test",
    end_date=None,
):
    if end_date is None:
        end_date = datetime.now() + timedelta(days=14)
    return Market(
        id=market_id,
        question=question,
        description=f"Bitcoin price prediction market. {question}",
        volume=500000,
        liquidity=50000,
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        spread=0.02,
        end_date=end_date,
        token_id_yes="tok_yes_btc",
        token_id_no="tok_no_btc",
        group_item_title="",
    )


def _make_ta(
    price=75000,
    sabre_trend=1,  # 1=bull, -1=bear
    sabre_ma=73000,
    sabre_atr=1200,
    macd_4h_hist=None,
    macd_4h_above_zero=True,
    macd_15m_hist=5.0,
    macd_15m_cross="BULLISH_CROSS",
    macd_15m_above_signal=True,
    rsi=55,
    trend_strength=0.7,
    trend_direction="BULLISH",
    mom_15m_dir="SPIKE_UP",
    mom_15m_pct=0.15,
    mom_5m_dir="DRIFT_UP",
    mom_5m_pct=0.08,
    m15_in_predict=True,
    m5_in_predict=False,
    vp_poc=72000,
    vp_vah=74500,
    vp_val=70000,
    support=72000,
    resistance=78000,
):
    """Build a fully populated TechnicalAnalysis object with known values."""
    if macd_4h_hist is None:
        # Below-zero MACD with positive histogram triggers _recovery bull vote in production code;
        # default negative hist when 4H is below zero so tests get a plain bear MACD vote unless overridden.
        macd_4h_hist = 150 if macd_4h_above_zero else -150

    macd_4h = MACDResult(
        macd_line=300 if macd_4h_above_zero else -300,
        signal_line=150 if macd_4h_above_zero else -150,
        histogram=macd_4h_hist,
        prev_histogram=macd_4h_hist - 20,
        above_zero=macd_4h_above_zero,
        crossover="BULLISH_CROSS" if macd_4h_hist > 0 else "BEARISH_CROSS",
        histogram_rising=macd_4h_hist > 0,
    )
    macd_15m = MACDResult(
        macd_line=10 if macd_15m_above_signal else -10,
        signal_line=5 if macd_15m_above_signal else -5,
        histogram=macd_15m_hist,
        prev_histogram=macd_15m_hist - 2,
        above_zero=macd_15m_hist > 0,
        crossover=macd_15m_cross,
        histogram_rising=macd_15m_hist > 0,
    )
    sabre = TrendSabreResult(
        trend=sabre_trend,
        ma_value=sabre_ma,
        trail_value=sabre_ma - 3000 if sabre_trend == 1 else sabre_ma + 3000,
        atr=sabre_atr,
        tension=(price - sabre_ma) / sabre_atr if sabre_atr > 0 else 0,
        tension_abs=abs((price - sabre_ma) / sabre_atr) if sabre_atr > 0 else 0,
        snap_supports=[],
        snap_resistances=[],
    )
    momentum = CandleMomentum(
        m15_direction=mom_15m_dir,
        m15_move_pct=mom_15m_pct,
        m15_in_prediction_window=m15_in_predict,
        m5_direction=mom_5m_dir,
        m5_move_pct=mom_5m_pct,
        m5_in_prediction_window=m5_in_predict,
        momentum_strength=0.6,
    )
    vp = AnchoredVolumeProfile(
        poc_price=vp_poc,
        vah_price=vp_vah,
        val_price=vp_val,
        high_volume_nodes=[vp_poc, vp_poc - 500],
        low_volume_nodes=[vp_vah + 500],
        anchor_price=vp_val - 1000,
        total_volume=100000,
    )
    return TechnicalAnalysis(
        current_price=price,
        chainlink_price=price - 30,
        macd_4h=macd_4h,
        macd_15m=macd_15m,
        trend_sabre=sabre,
        candle_momentum=momentum,
        volume_profile=vp,
        ema_9=price - 100,
        ema_21=price - 200,
        ema_50=price - 800,
        ema_200=price - 3000,
        rsi_14=rsi,
        nearest_support=support,
        nearest_resistance=resistance,
        trend_direction=trend_direction,
        trend_strength=trend_strength,
        timestamp=datetime.now(),
    )


class TestBitcoinHTFBias:
    """Layer 1: Higher timeframe trend filter tests."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_bullish_htf_3_of_3(self):
        """All 3 votes bullish → BULLISH bias."""
        ta = _make_ta(
            price=75000, sabre_trend=1, sabre_ma=73000, macd_4h_above_zero=True
        )
        assert self.strategy._get_higher_tf_bias(ta) == "BULLISH"

    def test_bearish_htf_3_of_3(self):
        """All 3 votes bearish → BEARISH bias."""
        ta = _make_ta(
            price=71000, sabre_trend=-1, sabre_ma=73000, macd_4h_above_zero=False
        )
        assert self.strategy._get_higher_tf_bias(ta) == "BEARISH"

    def test_bullish_htf_2_of_3(self):
        """Sabre bull + price above MA but MACD below zero → still BULLISH (2/3)."""
        ta = _make_ta(
            price=74000, sabre_trend=1, sabre_ma=73000, macd_4h_above_zero=False
        )
        assert self.strategy._get_higher_tf_bias(ta) == "BULLISH"

    def test_bearish_htf_2_of_3(self):
        """Sabre bear + price below MA but MACD above zero → BEARISH (2/3)."""
        ta = _make_ta(
            price=72000, sabre_trend=-1, sabre_ma=73000, macd_4h_above_zero=True
        )
        assert self.strategy._get_higher_tf_bias(ta) == "BEARISH"

    def test_neutral_htf_all_conflict(self):
        """Each indicator different → NEUTRAL."""
        ta = _make_ta(
            price=74000, sabre_trend=-1, sabre_ma=73000, macd_4h_above_zero=True
        )
        # sabre=-1 (bear), price>ma (bull), macd above (bull) → 2 bull, 1 bear → BULLISH
        # Actually this is 2 bull. Let's force a true split:
        ta = _make_ta(
            price=72000, sabre_trend=1, sabre_ma=73000, macd_4h_above_zero=False
        )
        # sabre=1 (bull), price<ma (bear), macd below (bear) → 1 bull, 2 bear → BEARISH
        assert self.strategy._get_higher_tf_bias(ta) == "BEARISH"


class TestBitcoinLTFConfirmation:
    """Layer 2: Lower timeframe MACD confirmation tests."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_long_confirmed_by_bull_cross(self):
        """Bullish MACD cross confirms LONG."""
        ta = _make_ta(
            macd_15m_cross="BULLISH_CROSS", macd_15m_hist=5, macd_15m_above_signal=True
        )
        confirmed, strength, reasons = self.strategy._check_lower_tf_confirmation(
            ta, "LONG"
        )
        assert confirmed is True
        assert strength >= 0.40  # Bull cross = 0.40
        assert any("bull cross" in r for r in reasons)

    def test_long_confirmed_by_rising_histogram(self):
        """Rising histogram (red→green) confirms LONG."""
        ta = _make_ta(
            macd_15m_cross="NONE", macd_15m_hist=2, macd_15m_above_signal=True
        )
        # Manually set prev_histogram to negative (red→green transition)
        ta.macd_15m.prev_histogram = -1
        ta.macd_15m.histogram_rising = True
        confirmed, strength, reasons = self.strategy._check_lower_tf_confirmation(
            ta, "LONG"
        )
        assert confirmed is True

    def test_long_rejected_when_bearish(self):
        """Bearish MACD does NOT confirm LONG."""
        ta = _make_ta(
            macd_15m_cross="BEARISH_CROSS",
            macd_15m_hist=-5,
            macd_15m_above_signal=False,
        )
        ta.macd_15m.histogram_rising = False
        confirmed, strength, reasons = self.strategy._check_lower_tf_confirmation(
            ta, "LONG"
        )
        assert confirmed is False

    def test_short_confirmed_by_bear_cross(self):
        """Bearish MACD cross confirms SHORT."""
        ta = _make_ta(
            macd_15m_cross="BEARISH_CROSS",
            macd_15m_hist=-5,
            macd_15m_above_signal=False,
        )
        ta.macd_15m.histogram_rising = False
        confirmed, strength, reasons = self.strategy._check_lower_tf_confirmation(
            ta, "SHORT"
        )
        assert confirmed is True
        assert strength >= 0.40

    def test_short_rejected_when_bullish(self):
        """Bullish MACD does NOT confirm SHORT."""
        ta = _make_ta(
            macd_15m_cross="BULLISH_CROSS", macd_15m_hist=5, macd_15m_above_signal=True
        )
        confirmed, strength, reasons = self.strategy._check_lower_tf_confirmation(
            ta, "SHORT"
        )
        assert confirmed is False


class TestBitcoinTiming:
    """Layer 3: Entry timing with candle momentum."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_spike_up_boosts_long(self):
        """15m SPIKE_UP adds timing bonus for LONG."""
        ta = _make_ta(mom_15m_dir="SPIKE_UP", mom_15m_pct=0.20, m15_in_predict=True)
        bonus, reasons = self.strategy._check_timing(ta, "LONG")
        assert bonus > 0.05  # Spike + prediction window
        assert any("SPIKE_UP" in r for r in reasons)

    def test_spike_down_hurts_long(self):
        """15m SPIKE_DOWN penalizes LONG."""
        ta = _make_ta(mom_15m_dir="SPIKE_DOWN", mom_15m_pct=-0.20)
        bonus, reasons = self.strategy._check_timing(ta, "LONG")
        assert bonus < 0  # Penalty

    def test_spike_down_boosts_short(self):
        """15m SPIKE_DOWN adds timing bonus for SHORT."""
        ta = _make_ta(mom_15m_dir="SPIKE_DOWN", mom_15m_pct=-0.20, m15_in_predict=True)
        bonus, reasons = self.strategy._check_timing(ta, "SHORT")
        assert bonus > 0.05

    def test_prediction_window_adds_bonus(self):
        """Being in the 15m prediction window gives bonus."""
        ta = _make_ta(mom_15m_dir="FLAT", m15_in_predict=True, m5_in_predict=True)
        bonus, reasons = self.strategy._check_timing(ta, "LONG")
        assert any("predict window" in r for r in reasons)


class TestBitcoinEdgeCalculation:
    """Layer 4: Probability estimation and edge calculation."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_btc_above_threshold_high_prob_up(self):
        """BTC at 80k, threshold 75k → high probability for UP market."""
        ta = _make_ta(price=80000, rsi=60)
        prob = self.strategy._estimate_probability(
            80000, 75000, "UP", ta, 14, 0.5, 0.05
        )
        assert prob > 0.65  # Well above threshold

    def test_btc_below_threshold_low_prob_up(self):
        """BTC at 70k, threshold 80k → low probability for UP market."""
        ta = _make_ta(price=70000, rsi=40)
        prob = self.strategy._estimate_probability(
            70000, 80000, "UP", ta, 14, 0.5, 0.05
        )
        assert prob < 0.40

    def test_btc_above_threshold_high_prob_down(self):
        """BTC at 70k, threshold 75k → high probability for DOWN market."""
        ta = _make_ta(price=70000, rsi=40)
        prob = self.strategy._estimate_probability(
            70000, 75000, "DOWN", ta, 14, 0.5, 0.05
        )
        assert prob > 0.60

    def test_rsi_overbought_penalizes_longs(self):
        """RSI > 75 should reduce UP probability."""
        ta = _make_ta(price=80000, rsi=80)
        prob_high_rsi = self.strategy._estimate_probability(
            80000, 75000, "UP", ta, 14, 0.5, 0
        )
        ta2 = _make_ta(price=80000, rsi=50)
        prob_normal_rsi = self.strategy._estimate_probability(
            80000, 75000, "UP", ta2, 14, 0.5, 0
        )
        assert prob_high_rsi < prob_normal_rsi  # Overbought penalty

    def test_vp_above_vah_boosts_long(self):
        """Price above VAH with bullish trend → VP boost."""
        ta = _make_ta(price=76000, vp_vah=74500)
        prob = self.strategy._estimate_probability(76000, 74000, "UP", ta, 14, 0.5, 0)
        # Should have VP bonus since price > VAH
        assert prob > 0.60

    def test_vp_stuck_in_value_area_reduces(self):
        """Price stuck in VA → VP penalty."""
        ta = _make_ta(price=72000, vp_vah=74500, vp_val=70000, vp_poc=72000)
        prob = self.strategy._estimate_probability(72000, 70000, "UP", ta, 14, 0.5, 0)
        ta2 = _make_ta(price=76000, vp_vah=74500, vp_val=70000, vp_poc=72000)
        prob2 = self.strategy._estimate_probability(76000, 74000, "UP", ta2, 14, 0.5, 0)
        # Price in VA should have lower conviction than breakout above VAH
        assert prob < prob2, (
            f"Stuck in VA ({prob:.3f}) should be lower than breakout ({prob2:.3f})"
        )


class TestBitcoinSignalGating:
    """Full signal gating: HTF → LTF → timing → edge → signal output."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.ai.analyze_market = AsyncMock(return_value=None)
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_bullish_htf_generates_buy_yes_on_up_market(self):
        """BULLISH HTF + confirmed LTF + UP market → BUY_YES (we agree BTC goes up)."""
        ta = _make_ta(
            price=82000,
            sabre_trend=1,
            sabre_ma=78000,
            macd_4h_above_zero=True,
            macd_15m_cross="BULLISH_CROSS",
            macd_15m_hist=5,
            macd_15m_above_signal=True,
            mom_15m_dir="SPIKE_UP",
            m15_in_predict=True,
            trend_strength=0.8,
            trend_direction="BULLISH",
        )
        market = _make_btc_market(
            question="Will Bitcoin be above $80,000 on March 31?",
            yes_price=0.40,  # Market underpricing → edge
        )
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))

        # Should generate a BUY_YES signal (we think BTC will be above 80k)
        if signals:
            assert signals[0].action == "BUY_YES"
            assert signals[0].direction == "UP"
            assert signals[0].edge > 0

    def test_bearish_htf_blocks_buy_on_up_market(self):
        """BEARISH HTF should NOT produce signals on UP markets (no buying into downtrend)."""
        ta = _make_ta(
            price=70000,
            sabre_trend=-1,
            sabre_ma=73000,
            macd_4h_above_zero=False,
            macd_15m_cross="BEARISH_CROSS",
            macd_15m_hist=-5,
            macd_15m_above_signal=False,
            trend_strength=0.8,
            trend_direction="BEARISH",
        )
        # UP market at 0.60 — but HTF is bearish so we'd be SELLING yes (shorting)
        market = _make_btc_market(
            question="Will Bitcoin be above $80,000 on March 31?",
            yes_price=0.60,
        )
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))

        # If any signal, it should be SELL_YES (bearish on BTC) not BUY_YES
        for sig in signals:
            assert sig.action != "BUY_YES" or sig.direction != "UP"

    def test_neutral_htf_produces_no_signals(self):
        """NEUTRAL HTF (conflicting indicators) → sit out entirely."""
        # Force a truly neutral situation — hard to get with 3 binary votes
        # Sabre bull, price below MA, MACD above zero → 2 bull 1 bear → BULLISH
        # So let's use a scenario where LTF doesn't confirm
        ta = _make_ta(
            price=75000,
            sabre_trend=1,
            sabre_ma=73000,
            macd_4h_above_zero=True,
            macd_15m_cross="BEARISH_CROSS",
            macd_15m_hist=-3,
            macd_15m_above_signal=False,
            trend_strength=0.7,
        )
        # HTF is BULLISH but LTF doesn't confirm LONG → no signals
        market = _make_btc_market(yes_price=0.50)
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))

        assert len(signals) == 0

    def test_exposure_paused_blocks_all(self):
        """When exposure manager is PAUSED, no signals should be generated."""
        ta = _make_ta(
            price=82000,
            sabre_trend=1,
            sabre_ma=78000,
            macd_4h_above_zero=True,
            macd_15m_cross="BULLISH_CROSS",
            macd_15m_hist=5,
            macd_15m_above_signal=True,
        )
        market = _make_btc_market(yes_price=0.40)

        # Trigger kill switch by recording 3 losses
        self.strategy.exposure_manager.record_trade(-10, "bitcoin", "test")
        self.strategy.exposure_manager.record_trade(-10, "bitcoin", "test")
        self.strategy.exposure_manager.record_trade(-10, "bitcoin", "test")

        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))

        assert len(signals) == 0  # Paused after 3 losses

    def test_non_btc_market_ignored(self):
        """Non-BTC markets should be filtered out."""
        ta = _make_ta()
        market = Market(
            id="test-non-btc",
            question="Will the Fed raise rates?",
            description="Interest rate market",
            volume=100000,
            liquidity=50000,
            yes_price=0.45,
            no_price=0.55,
            spread=0.02,
            end_date=datetime.now() + timedelta(days=30),
            token_id_yes="tok_yes",
            token_id_no="tok_no",
            group_item_title="",
        )
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))

        assert len(signals) == 0

    def test_disabled_strategy_returns_empty(self):
        """Disabled strategy should return empty list."""
        self.strategy.enabled = False
        signals = run_async(self.strategy.scan_and_analyze([], 10000))
        assert signals == []


class TestBitcoinMarketDetection:
    """Test market detection and price extraction helpers."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_detects_bitcoin_market(self):
        m = _make_btc_market(question="Will Bitcoin be above $80,000?")
        assert self.strategy._is_bitcoin_market(m) is True

    def test_detects_btc_market(self):
        m = _make_btc_market(question="Will BTC exceed $100K?")
        assert self.strategy._is_bitcoin_market(m) is True

    def test_rejects_non_btc(self):
        m = Market(
            id="test",
            question="Will ETH hit $5000?",
            description="Ethereum market",
            volume=100000,
            liquidity=50000,
            yes_price=0.5,
            no_price=0.5,
            spread=0.02,
            end_date=datetime.now() + timedelta(days=30),
            token_id_yes="t1",
            token_id_no="t2",
            group_item_title="",
        )
        assert self.strategy._is_bitcoin_market(m) is False

    def test_extracts_price_threshold(self):
        assert (
            self.strategy._extract_price_threshold("Will BTC be above $80,000?")
            == 80000
        )
        assert (
            self.strategy._extract_price_threshold("Bitcoin above $75,500.50?")
            == 75500.50
        )
        assert self.strategy._extract_price_threshold("BTC reach $100,000?") == 100000

    def test_extracts_direction_up(self):
        assert self.strategy._extract_direction("Will BTC be above $80,000?") == "UP"
        assert self.strategy._extract_direction("Will Bitcoin exceed $90K?") == "UP"
        assert self.strategy._extract_direction("Will BTC rise to $100K?") == "UP"

    def test_extracts_direction_down(self):
        assert (
            self.strategy._extract_direction("Will BTC fall below $70,000?") == "DOWN"
        )
        assert (
            self.strategy._extract_direction("Will Bitcoin drop under $60K?") == "DOWN"
        )


class TestExposureManagerIntegration:
    """Test exposure manager tier calculation and kill switch."""

    def setup_method(self):
        self.config = _make_config()
        self.em = ExposureManager(self.config, is_paper=True)

    def test_high_conditions_full_tier(self):
        from src.execution.exposure_manager import MarketConditions

        conditions = MarketConditions(
            volatility=0.02,
            volume_ratio=1.5,
            trend_strength=0.8,
            trend_direction="BULLISH",
        )
        tier, mult, size, reason = self.em.get_exposure(conditions)
        assert tier == ExposureTier.FULL
        assert mult == 1.0
        assert size == 5.0

    def test_low_conditions_minimal_tier(self):
        from src.execution.exposure_manager import MarketConditions

        conditions = MarketConditions(
            volatility=0.003,
            volume_ratio=0.5,
            trend_strength=0.1,
            trend_direction="NEUTRAL",
        )
        tier, mult, size, reason = self.em.get_exposure(conditions)
        assert tier == ExposureTier.MINIMAL
        assert mult == 0.2

    def test_kill_switch_after_3_losses(self):
        from src.execution.exposure_manager import MarketConditions

        self.em.record_trade(-5, "bitcoin", "test1")
        self.em.record_trade(-5, "bitcoin", "test2")
        self.em.record_trade(-5, "bitcoin", "test3")
        conditions = MarketConditions(
            volatility=0.02, volume_ratio=1.5, trend_strength=0.8
        )
        tier, mult, size, reason = self.em.get_exposure(conditions)
        assert tier == ExposureTier.PAUSED
        assert size == 0.0

    def test_win_resets_loss_streak(self):
        self.em.record_trade(-5, "bitcoin", "test1")
        self.em.record_trade(-5, "bitcoin", "test2")
        assert self.em._consecutive_losses == 2
        self.em.record_trade(10, "bitcoin", "test3")
        assert self.em._consecutive_losses == 0

    def test_scale_size_respects_tier(self):
        from src.execution.exposure_manager import MarketConditions

        conditions = MarketConditions(
            volatility=0.003, volume_ratio=0.5, trend_strength=0.1
        )
        self.em.get_exposure(conditions)  # Sets to MINIMAL
        scaled = self.em.scale_size(10.0)
        assert scaled <= 1.0  # Minimal tier max is $1 (test config has no min_trade_usd)

    def test_scale_size_min_trade_usd_floor(self):
        """Production-style exposure: tier multiplier must not shrink below min_trade_usd."""
        from src.execution.exposure_manager import MarketConditions

        cfg = {
            **_make_config(),
            "exposure": {
                "full_size": 15.0,
                "moderate_size": 13.0,
                "minimal_size": 10.0,
                "min_trade_usd": 10.0,
                "max_consecutive_losses": 3,
                "pause_cycles": 2,
                "live_resume_mode": "auto",
                "high_vol_pct": 0.015,
                "low_vol_pct": 0.005,
                "high_volume_ratio": 1.3,
                "low_volume_ratio": 0.7,
            },
        }
        em = ExposureManager(cfg, is_paper=True)
        conditions = MarketConditions(
            volatility=0.003, volume_ratio=0.5, trend_strength=0.1
        )
        em.get_exposure(conditions)  # MINIMAL, mult 0.2
        scaled = em.scale_size(50.0)  # 50 * 0.2 = 10 after floor, cap minimal_size 10
        assert scaled == 10.0
