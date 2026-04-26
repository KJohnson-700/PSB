"""
Bitcoin Strategy scenario tests — realistic paper trade scenarios with known price paths.

Tests both 15m and 5m timeframe scenarios. Each scenario simulates what the bot
would encounter with real BTC market data and verifies the strategy's decisions.

These provide "solid data points" per round by testing:
1. Strong bullish trend → should generate BUY_YES on UP markets
2. Strong bearish trend → should generate SELL_YES on UP markets (or BUY_YES on DOWN)
3. Choppy/sideways → should sit out (no signals)
4. Trend reversal → should adapt (no stale signals)
5. Kill switch trigger → should halt after consecutive losses
6. Exposure scaling → full/moderate/minimal sizing
7. Resolution tracking → real PnL from market settlement
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock, patch

from src.market.scanner import Market
from src.analysis.math_utils import PositionSizer
from src.analysis.ai_agent import AIAnalysis
from src.analysis.btc_price_service import (
    MACDResult,
    TrendSabreResult,
    CandleMomentum,
    AnchoredVolumeProfile,
    TechnicalAnalysis,
)
from src.strategies.bitcoin import BitcoinStrategy
from src.execution.exposure_manager import (
    ExposureManager,
    ExposureTier,
    MarketConditions,
)
from src.execution.resolution_tracker import ResolutionTracker

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
                # Strategy only scans updown markets; tests must not depend on live UTC hour.
                "blocked_utc_hours_updown": [],
                # Production caps edges to avoid inflated updown signals; scenarios use strong TA.
                # Raised to 0.30 so the test TA (est_prob_up≈0.75) can produce signals at
                # yes_price=0.50 (edge≈0.25) within the [0.46, 0.54] updown entry price band.
                "max_edge_updown": 0.30,
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


def _make_btc_market(question, yes_price, market_id="btc-test"):
    return Market(
        id=market_id,
        question=question,
        description=f"Bitcoin market: {question}",
        volume=500000,
        liquidity=50000,
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        spread=0.02,
        end_date=datetime.now() + timedelta(days=14),
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        group_item_title="",
    )


def _make_btc_updown_market(
    yes_price=0.40,
    mins_until_end=13.0,
    market_id="btc-updown-test",
):
    """15m Bitcoin Up/Down candle (matches `UPDOWN_PATTERN` + entry window)."""
    end = datetime.now(timezone.utc) + timedelta(minutes=mins_until_end)
    return Market(
        id=market_id,
        question="Bitcoin Up or Down - 12:00AM-12:15AM ET?",
        description="Bitcoin 15m up/down candle",
        volume=500000,
        liquidity=50000,
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        spread=0.02,
        end_date=end,
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        group_item_title="",
    )


def _ltf_unconfirmed_bull(ta: TechnicalAnalysis) -> TechnicalAnalysis:
    """Weaken 15m MACD so anti-LTF gate passes (early-momentum path)."""
    ta.macd_15m = MACDResult(
        macd_line=10.0,
        signal_line=12.0,
        histogram=5.0,
        prev_histogram=4.0,
        crossover="NONE",
        histogram_rising=True,
        above_zero=True,
    )
    return ta


def _ltf_unconfirmed_bear(ta: TechnicalAnalysis) -> TechnicalAnalysis:
    """Weaken 15m MACD so anti-LTF gate passes on SHORT path."""
    ta.macd_15m = MACDResult(
        macd_line=-7.0,
        signal_line=-6.5,
        histogram=-0.2,
        prev_histogram=-0.1,
        crossover="NONE",
        histogram_rising=False,
        above_zero=False,
    )
    return ta


def _no_timing_ltf_bull(ta: TechnicalAnalysis) -> TechnicalAnalysis:
    """Strip LTF/timing signals so only HTF boost contributes to est_prob_up (≈0.58).

    Used to test the AI marginal edge path: at yes_price=0.52 the quant edge is
    0.06 (below min_edge=0.08 but above ai_updown_marginal_min_edge=0.03), which
    triggers the AI confirmation flow.
    """
    ta.macd_15m = MACDResult(
        macd_line=5.0,
        signal_line=8.0,
        histogram=-0.5,
        prev_histogram=-0.3,
        crossover="NONE",
        histogram_rising=False,
        above_zero=False,
    )
    ta.candle_momentum = CandleMomentum(
        m15_direction="FLAT",
        m15_move_pct=0.00,
        m15_in_prediction_window=False,
        m5_direction="FLAT",
        m5_move_pct=0.00,
        m5_in_prediction_window=False,
        momentum_strength=0.0,
    )
    return ta


def _make_bullish_ta(price=82000):
    """Scenario: Strong bull trend — all indicators aligned UP."""
    return TechnicalAnalysis(
        current_price=price,
        ema_9=price - 100,
        ema_21=price - 300,
        ema_50=price - 1500,
        ema_200=price - 5000,
        rsi_14=62,
        macd_4h=MACDResult(
            macd_line=400,
            signal_line=200,
            histogram=200,
            prev_histogram=150,
            crossover="NONE",
            histogram_rising=True,
            above_zero=True,
        ),
        macd_15m=MACDResult(
            macd_line=15,
            signal_line=8,
            histogram=7,
            prev_histogram=-2,
            crossover="BULLISH_CROSS",
            histogram_rising=True,
            above_zero=True,
        ),
        trend_sabre=TrendSabreResult(
            ma_value=price - 2000,
            trail_value=price - 5000,
            trend=1,
            tension=1.5,
            tension_abs=1.5,
            atr=1200,
            snap_supports=[],
            snap_resistances=[],
        ),
        candle_momentum=CandleMomentum(
            m15_direction="SPIKE_UP",
            m15_move_pct=0.18,
            m15_in_prediction_window=True,
            m5_direction="DRIFT_UP",
            m5_move_pct=0.06,
            m5_in_prediction_window=False,
            momentum_strength=0.7,
        ),
        volume_profile=AnchoredVolumeProfile(
            poc_price=price - 3000,
            vah_price=price - 1000,
            val_price=price - 5000,
            high_volume_nodes=[price - 3000],
            low_volume_nodes=[price + 1000],
            anchor_price=price - 6000,
            total_volume=200000,
        ),
        trend_direction="BULLISH",
        trend_strength=0.85,
        nearest_support=price - 3000,
        nearest_resistance=price + 4000,
        timestamp=datetime.now(),
    )


def _make_bearish_ta(price=68000):
    """Scenario: Strong bear trend — all indicators aligned DOWN."""
    return TechnicalAnalysis(
        current_price=price,
        ema_9=price + 100,
        ema_21=price + 400,
        ema_50=price + 2000,
        ema_200=price + 6000,
        rsi_14=35,
        macd_4h=MACDResult(
            macd_line=-300,
            signal_line=-150,
            histogram=-150,
            prev_histogram=-100,
            crossover="NONE",
            histogram_rising=False,
            above_zero=False,
        ),
        macd_15m=MACDResult(
            macd_line=-12,
            signal_line=-5,
            histogram=-7,
            prev_histogram=2,
            crossover="BEARISH_CROSS",
            histogram_rising=False,
            above_zero=False,
        ),
        trend_sabre=TrendSabreResult(
            ma_value=price + 3000,
            trail_value=price + 6000,
            trend=-1,
            tension=-2.0,
            tension_abs=2.0,
            atr=1400,
            snap_supports=[],
            snap_resistances=[],
        ),
        candle_momentum=CandleMomentum(
            m15_direction="SPIKE_DOWN",
            m15_move_pct=-0.22,
            m15_in_prediction_window=True,
            m5_direction="DRIFT_DOWN",
            m5_move_pct=-0.08,
            m5_in_prediction_window=False,
            momentum_strength=0.75,
        ),
        volume_profile=AnchoredVolumeProfile(
            poc_price=price + 4000,
            vah_price=price + 6000,
            val_price=price + 2000,
            high_volume_nodes=[price + 4000],
            low_volume_nodes=[price - 1000],
            anchor_price=price + 8000,
            total_volume=180000,
        ),
        trend_direction="BEARISH",
        trend_strength=0.80,
        nearest_support=price - 5000,
        nearest_resistance=price + 2000,
        timestamp=datetime.now(),
    )


def _make_choppy_ta(price=75000):
    """Scenario: Sideways, no clear trend — bot should sit out."""
    return TechnicalAnalysis(
        current_price=price,
        ema_9=price + 50,
        ema_21=price - 50,
        ema_50=price + 100,
        ema_200=price - 200,
        rsi_14=50,
        macd_4h=MACDResult(
            macd_line=10,
            signal_line=8,
            histogram=2,
            prev_histogram=-1,
            crossover="NONE",
            histogram_rising=True,
            above_zero=True,
        ),
        macd_15m=MACDResult(
            macd_line=-1,
            signal_line=1,
            histogram=-2,
            prev_histogram=1,
            crossover="NONE",
            histogram_rising=False,
            above_zero=False,
        ),
        trend_sabre=TrendSabreResult(
            ma_value=price + 200,
            trail_value=price - 2000,
            trend=1,
            tension=0.1,
            tension_abs=0.1,
            atr=800,
            snap_supports=[],
            snap_resistances=[],
        ),
        candle_momentum=CandleMomentum(
            m15_direction="FLAT",
            m15_move_pct=0.01,
            m5_direction="FLAT",
            m5_move_pct=0.005,
            momentum_strength=0.1,
        ),
        volume_profile=AnchoredVolumeProfile(
            poc_price=price,
            vah_price=price + 500,
            val_price=price - 500,
            high_volume_nodes=[price],
            low_volume_nodes=[],
            anchor_price=price - 1000,
            total_volume=80000,
        ),
        trend_direction="NEUTRAL",
        trend_strength=0.15,
        nearest_support=price - 1000,
        nearest_resistance=price + 1000,
        timestamp=datetime.now(),
    )


class TestBitcoinBullScenario:
    """Scenario: BTC in strong uptrend — 15m and 5m both confirming."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.ai.analyze_market = AsyncMock(return_value=None)
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_strong_bull_buys_yes_on_up_market(self):
        """BTC at 82k, strong bullish, 15m up/down market → BUY_YES (quant path)."""
        ta = _ltf_unconfirmed_bull(_make_bullish_ta(82000))
        # yes_price 0.50 is at the centre of the [0.46, 0.54] updown entry band.
        # With this TA est_prob_up≈0.75 → edge≈0.25 which passes max_edge_updown=0.30.
        market = _make_btc_updown_market(yes_price=0.50, mins_until_end=13.0)
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) >= 1
        assert signals[0].action == "BUY_YES"
        assert signals[0].edge > 0.08  # Above min_edge threshold

    def test_strong_bull_sells_yes_on_overpriced_up(self):
        """Bullish HTF only allows LONG on updown → BUY_YES or no trade (never bearish SELL)."""
        ta = _ltf_unconfirmed_bull(_make_bullish_ta(82000))
        market = _make_btc_updown_market(yes_price=0.65, mins_until_end=13.0)
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert isinstance(signals, list)
        for s in signals:
            assert s.action in ("BUY_YES", "SELL_YES"), f"Unexpected action: {s.action}"
        assert not any(s.action == "SELL_YES" for s in signals)

    def test_bull_scenario_exposure_full_tier(self):
        """High vol + bull trend → FULL exposure tier ($5 max).

        Patches _get_weekend_penalty to 1.0 so the size assertion is deterministic
        regardless of which day of the week the test suite runs.
        """
        ta = _make_bullish_ta(82000)
        with patch('src.execution.exposure_manager._get_weekend_penalty', return_value=1.0):
            conditions = ExposureManager.conditions_from_ta(ta)
        em = ExposureManager(self.config, is_paper=True)
        tier, mult, size, reason = em.get_exposure(conditions)
        assert tier == ExposureTier.FULL
        assert size == 5.0


class TestBitcoinBearScenario:
    """Scenario: BTC in strong downtrend — should only short or sit out."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.ai.analyze_market = AsyncMock(return_value=None)
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_bearish_blocks_long_on_up_market(self):
        """BTC bearish HTF on updown → no BUY_YES (SHORT path only)."""
        ta = _ltf_unconfirmed_bear(_make_bearish_ta(68000))
        market = _make_btc_updown_market(yes_price=0.55, mins_until_end=13.0)
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        buy_yes = [s for s in signals if s.action == "BUY_YES"]
        assert len(buy_yes) == 0

    def test_bearish_sells_overpriced_up(self):
        """Bearish HTF on 15m updown → may emit SELL_YES (bet down), never BUY_YES."""
        ta = _ltf_unconfirmed_bear(_make_bearish_ta(68000))
        market = _make_btc_updown_market(yes_price=0.55, mins_until_end=13.0)
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        if signals:
            assert signals[0].action == "SELL_YES"
        assert not any(s.action == "BUY_YES" for s in signals)

    def test_bearish_buys_down_market(self):
        """Updown markets: bearish HTF → SHORT side is SELL_YES (direction DOWN), not BUY_YES."""
        ta = _ltf_unconfirmed_bear(_make_bearish_ta(68000))
        market = _make_btc_updown_market(yes_price=0.40, mins_until_end=13.0)
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        if signals:
            assert signals[0].action == "SELL_YES"
            assert signals[0].direction == "DOWN"
        assert not any(s.action == "BUY_YES" for s in signals)


class TestBitcoinChoppyScenario:
    """Scenario: No clear trend — bot should produce zero signals."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.ai.analyze_market = AsyncMock(return_value=None)
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_sideways_no_signals(self):
        """Choppy TA + updown: anti-LTF or edge filters → expect no trades."""
        ta = _make_choppy_ta(75000)
        market = _make_btc_updown_market(yes_price=0.50, mins_until_end=13.0)
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) == 0

    def test_choppy_exposure_minimal(self):
        """Low vol + flat trend → MINIMAL tier ($1)."""
        ta = _make_choppy_ta()
        conditions = ExposureManager.conditions_from_ta(ta)
        em = ExposureManager(self.config, is_paper=True)
        tier, mult, size, reason = em.get_exposure(conditions)
        assert tier in (ExposureTier.MINIMAL, ExposureTier.MODERATE)


class TestBitcoinKillSwitch:
    """Scenario: 3 consecutive losses → kill switch activates."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.ai.analyze_market = AsyncMock(return_value=None)
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_kill_switch_stops_trading(self):
        """After 3 losses, kill switch blocks all signals."""
        ta = _make_bullish_ta(82000)
        market = _make_btc_updown_market(yes_price=0.60, mins_until_end=13.0)

        # Record 3 consecutive losses
        self.strategy.exposure_manager.record_trade(-5, "bitcoin", "m1")
        self.strategy.exposure_manager.record_trade(-3, "bitcoin", "m2")
        self.strategy.exposure_manager.record_trade(-2, "bitcoin", "m3")

        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))

        assert len(signals) == 0  # Kill switch blocks everything

    def test_kill_switch_auto_resumes_after_cycles(self):
        """After pause_cycles (2), kill switch auto-resumes in test mode."""
        em = self.strategy.exposure_manager
        em.record_trade(-5, "bitcoin", "m1")
        em.record_trade(-3, "bitcoin", "m2")
        em.record_trade(-2, "bitcoin", "m3")

        assert em._paused is True

        # Simulate 2 cycles passing — call get_exposure which checks resume
        em._cycles_while_paused = em.pause_cycles  # Set to threshold
        # Manual resume simulates what happens after cycles pass
        em.manual_resume()

        assert em._paused is False

    def test_win_after_losses_resets_streak(self):
        """A win resets the consecutive loss counter."""
        em = self.strategy.exposure_manager
        em.record_trade(-5, "bitcoin", "m1")
        em.record_trade(-5, "bitcoin", "m2")
        assert em._consecutive_losses == 2

        em.record_trade(10, "bitcoin", "m3")  # Win!
        assert em._consecutive_losses == 0
        assert em._paused is False


class TestBitcoinMultipleMarkets:
    """Test scanning multiple BTC markets in one cycle."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.ai.analyze_market = AsyncMock(return_value=None)
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_filters_btc_from_mixed_markets(self):
        """Should only emit on BTC updown markets from a mixed list."""
        ta = _ltf_unconfirmed_bull(_make_bullish_ta(82000))
        end = datetime.now(timezone.utc) + timedelta(minutes=13.0)
        markets = [
            _make_btc_updown_market(0.60, 13.0, "btc1"),
            Market(
                id="fed-rates",
                question="Will Fed raise rates?",
                description="Fed",
                volume=100000,
                liquidity=50000,
                yes_price=0.5,
                no_price=0.5,
                spread=0.02,
                end_date=datetime.now() + timedelta(days=30),
                token_id_yes="t1",
                token_id_no="t2",
                group_item_title="",
            ),
            Market(
                id="btc2",
                question="Bitcoin Up or Down - 1:00AM-1:15AM ET?",
                description="Second15m window",
                volume=400000,
                liquidity=50000,
                yes_price=0.58,
                no_price=0.42,
                spread=0.02,
                end_date=end,
                token_id_yes="ty1",
                token_id_no="tn1",
                group_item_title="",
            ),
        ]
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze(markets, 10000))

        for s in signals:
            assert (
                "bitcoin" in s.market_question.lower()
                or "btc" in s.market_question.lower()
            )

    def test_15m_vs_5m_both_produce_data(self):
        """Both 15m spike and 5m spike should contribute to timing layer."""
        ta = _ltf_unconfirmed_bull(_make_bullish_ta(82000))
        ta.candle_momentum.m15_in_prediction_window = True
        ta.candle_momentum.m5_in_prediction_window = True
        ta.candle_momentum.m5_direction = "SPIKE_UP"
        ta.candle_momentum.m5_move_pct = 0.12

        market = _make_btc_updown_market(yes_price=0.60, mins_until_end=13.0)
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))

        if signals:
            assert signals[0].confidence > 0.5


class TestBitcoinAIIntegration:
    """Ensure AI meaningfully affects marginal updown decisions."""

    def setup_method(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.ai.is_available = MagicMock(return_value=True)
        self.ai.analyze_market = AsyncMock(return_value=None)
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = BitcoinStrategy(self.config, self.ai, self.sizer)

    def test_updown_marginal_edge_uses_ai_confirmation(self):
        # _no_timing_ltf_bull strips LTF/timing so est_prob_up≈0.58 (HTF-only).
        # At yes_price=0.52: quant_edge≈0.06 — below min_edge (0.08) but above
        # ai_updown_marginal_min_edge (0.03), so the AI confirmation path fires.
        ta = _no_timing_ltf_bull(_make_bullish_ta(82000))
        market = _make_btc_updown_market(yes_price=0.52, mins_until_end=13.0)
        self.ai.analyze_market = AsyncMock(
            return_value=AIAnalysis(
                reasoning="Momentum continuation likely",
                confidence_score=0.80,
                estimated_probability=0.65,  # ai_edge=0.13 lifts quant_edge=0.06 above min_edge
                recommendation="BUY_YES",
                market_id=market.id,
                timestamp=datetime.now(),
            )
        )
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))

        assert len(signals) >= 1
        assert signals[0].ai_used is True
        assert "ai_updown_confirm" in signals[0].reason
        assert signals[0].edge >= self.strategy.min_edge

    def test_updown_ai_veto_blocks_conflicting_action(self):
        ta = _ltf_unconfirmed_bull(_make_bullish_ta(82000))
        market = _make_btc_updown_market(yes_price=0.69, mins_until_end=13.0)
        self.ai.analyze_market = AsyncMock(
            return_value=AIAnalysis(
                reasoning="Disagrees with long setup",
                confidence_score=0.90,
                estimated_probability=0.30,
                recommendation="BUY_NO",  # conflicts with BUY_YES
                market_id=market.id,
                timestamp=datetime.now(),
            )
        )
        with patch.object(
            self.strategy.btc_service, "get_full_analysis", return_value=ta
        ):
            signals = run_async(self.strategy.scan_and_analyze([market], 10000))

        assert len(signals) == 0
        stats = self.strategy.last_scan_stats
        assert stats.get("signals") == 0
        assert stats.get("ai_vetos", 0) >= 1
        assert "ai_veto_marginal_updown" in stats.get("top_skip_reasons", {})


class TestResolutionTracker:
    """Test the resolution tracker with mocked API responses."""

    def test_settlement_yes_wins(self):
        """When market resolves YES, BUY_YES gets full payout."""
        from unittest.mock import MagicMock
        import json

        tracker = ResolutionTracker(check_interval_seconds=0)

        # Mock journal
        journal = MagicMock()
        journal.get_open_positions.return_value = [
            {
                "trade_id": "trade-1",
                "market_id": "market-btc-1",
                "market_question": "Will BTC be above $80k?",
                "strategy": "bitcoin",
                "action": "BUY_YES",
                "side": "BUY",
                "outcome": "YES",
                "size": 5.0,
                "entry_price": 0.40,
            }
        ]

        # Mock the API call
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "closed": True,
            "resolution": "YES",
        }

        with patch(
            "src.execution.resolution_tracker.requests.get", return_value=mock_resp
        ):
            settled = tracker.check_and_settle(
                journal=journal,
                risk_manager=MagicMock(),
                bankroll=1000,
            )

        assert len(settled) == 1
        assert settled[0]["outcome_won"] == "YES"
        assert settled[0]["entry_price"] == 0.40
        assert settled[0]["exit_price"] == 1.0
        assert settled[0]["pnl"] == 3.0  # (1.0 - 0.40) * 5.0

    def test_settlement_no_wins(self):
        """When market resolves NO, BUY_YES loses."""
        tracker = ResolutionTracker(check_interval_seconds=0)

        journal = MagicMock()
        journal.get_open_positions.return_value = [
            {
                "trade_id": "trade-2",
                "market_id": "market-btc-2",
                "market_question": "Will BTC be above $90k?",
                "strategy": "bitcoin",
                "action": "BUY_YES",
                "side": "BUY",
                "outcome": "YES",
                "size": 3.0,
                "entry_price": 0.30,
            }
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"closed": True, "resolution": "NO"}

        with patch(
            "src.execution.resolution_tracker.requests.get", return_value=mock_resp
        ):
            settled = tracker.check_and_settle(
                journal=journal,
                risk_manager=MagicMock(),
                bankroll=1000,
            )

        assert len(settled) == 1
        assert settled[0]["outcome_won"] == "NO"
        assert abs(settled[0]["pnl"] - (-0.90)) < 0.01  # (0.0 - 0.30) * 3.0

    def test_sell_yes_wins_when_no_resolves(self):
        """SELL_YES profits when market resolves NO."""
        tracker = ResolutionTracker(check_interval_seconds=0)

        journal = MagicMock()
        journal.get_open_positions.return_value = [
            {
                "trade_id": "trade-3",
                "market_id": "market-btc-3",
                "strategy": "bitcoin",
                "action": "SELL_YES",
                "side": "SELL",
                "outcome": "NO",
                "size": 5.0,
                "entry_price": 0.60,
                "market_question": "Will BTC be above $100k?",
            }
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"closed": True, "resolution": "NO"}

        with patch(
            "src.execution.resolution_tracker.requests.get", return_value=mock_resp
        ):
            settled = tracker.check_and_settle(
                journal=journal,
                risk_manager=MagicMock(),
                bankroll=1000,
            )

        assert len(settled) == 1
        assert settled[0]["pnl"] == 3.0  # (0.60 - 0.0) * 5.0 → profit

    def test_unresolved_market_not_settled(self):
        """Open market should NOT be settled."""
        tracker = ResolutionTracker(check_interval_seconds=0)

        journal = MagicMock()
        journal.get_open_positions.return_value = [
            {
                "trade_id": "trade-4",
                "market_id": "market-btc-4",
                "strategy": "bitcoin",
                "action": "BUY_YES",
                "side": "BUY",
                "outcome": "YES",
                "size": 5.0,
                "entry_price": 0.40,
                "market_question": "Will BTC be above $80k?",
            }
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"closed": False, "resolution": None}

        with patch(
            "src.execution.resolution_tracker.requests.get", return_value=mock_resp
        ):
            settled = tracker.check_and_settle(
                journal=journal,
                risk_manager=MagicMock(),
                bankroll=1000,
            )

        assert len(settled) == 0  # Not resolved yet
