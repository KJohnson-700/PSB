"""
Strategy unit tests — verify signal logic produces correct outputs for known inputs.
No random data. Each test represents a real market scenario.
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from src.market.scanner import Market
from src.backtest.backtest_ai import BacktestAIAgent
from src.analysis.math_utils import PositionSizer
from src.strategies.fade import FadeStrategy
from src.strategies.arbitrage import ArbitrageStrategy
from src.strategies.neh import NothingEverHappensStrategy

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
    }


_SENTINEL = object()

def _make_market(yes_price, end_date=_SENTINEL, liquidity=0, market_id="test-market"):
    if end_date is _SENTINEL:
        end_date = datetime.now() + timedelta(days=90)
    return Market(
        id=market_id,
        question="Will X happen?",
        description="Test market",
        volume=100000,
        liquidity=liquidity,
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        spread=0.02,
        end_date=end_date,
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        group_item_title="",
    )


# ─── FADE STRATEGY ────────────────────────────────────────────────

class TestFadeStrategy:
    def setup_method(self):
        self.config = _make_config()
        self.ai = BacktestAIAgent(self.config)
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = FadeStrategy(self.config, self.ai, self.sizer)

    def test_fade_triggers_on_yes_consensus(self):
        """When YES is 0.82, fade should sell YES. no_price=0.18 passes entry filter [0.15, 0.45]."""
        market = _make_market(yes_price=0.82)
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) >= 1
        sig = signals[0]
        assert sig.action == "SELL_YES"
        assert sig.size > 0
        assert sig.implied_probability_gap > 0.10

    def test_fade_triggers_on_no_consensus(self):
        """When YES is 0.10 (NO at 0.90), fade should buy YES."""
        market = _make_market(yes_price=0.10)
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        # NO consensus at 0.90 is in [0.80, 0.95], action should be BUY_YES
        # But entry_price for BUY_YES = yes_price = 0.10 < entry_price_min (0.15)
        # So this should be FILTERED OUT
        assert len(signals) == 0

    def test_fade_rejects_mid_range(self):
        """No signal when price is in the 0.40-0.60 zone — no consensus."""
        market = _make_market(yes_price=0.50)
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) == 0

    def test_fade_rejects_lottery_zone(self):
        """Entry price 0.05 (buying NO when YES=0.95) is below 0.15 min — reject."""
        market = _make_market(yes_price=0.95)
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        # YES consensus at 0.95. Action would be SELL_YES.
        # Entry price for SELL_YES = no_price = 0.05 < 0.15 -> filtered
        assert len(signals) == 0

    def test_fade_respects_entry_price_range(self):
        """YES at 0.82 -> SELL_YES. Entry price = NO = 0.18, which is in [0.15, 0.45]."""
        market = _make_market(yes_price=0.82)
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) >= 1
        sig = signals[0]
        assert sig.action == "SELL_YES"

    def test_fade_disabled_returns_nothing(self):
        """When fade is disabled, no signals should be generated."""
        self.config["strategies"]["fade"]["enabled"] = False
        strategy = FadeStrategy(self.config, self.ai, self.sizer)
        market = _make_market(yes_price=0.90)
        signals = run_async(strategy.scan_and_analyze([market], 10000))
        assert len(signals) == 0

    def test_fade_use_ai_false_skips_analyze_market_in_ambiguous_band(self):
        """Ambiguous 0.80–0.90: use_ai=false must not call analyze_market (synthetic path only)."""
        self.config["strategies"]["fade"]["use_ai"] = False
        strategy = FadeStrategy(self.config, self.ai, self.sizer)
        market = _make_market(yes_price=0.82)
        mock_analyze = AsyncMock(side_effect=AssertionError("analyze_market should not be called"))
        with patch.object(self.ai, "analyze_market", mock_analyze):
            run_async(strategy.scan_and_analyze([market], 10000))
        mock_analyze.assert_not_called()


# ─── ARBITRAGE STRATEGY ──────────────────────────────────────────

class TestArbitrageStrategy:
    def setup_method(self):
        self.config = _make_config()
        self.ai = BacktestAIAgent(self.config)
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = ArbitrageStrategy(self.config, self.ai, self.sizer)

    def test_arb_triggers_on_underpriced_yes(self):
        """YES at 0.30 — AI thinks mean reversion to ~0.56, edge > 0.10."""
        market = _make_market(yes_price=0.30)
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) >= 1
        sig = signals[0]
        assert sig.action == "BUY_YES"
        assert sig.edge > 0.10

    def test_arb_rejects_fair_price(self):
        """No signal when price is near fair value (0.50)."""
        market = _make_market(yes_price=0.50)
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) == 0

    def test_arb_rejects_outside_entry_range(self):
        """YES at 0.15 — below entry_price_min (0.20), should reject."""
        market = _make_market(yes_price=0.15)
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) == 0

    def test_arb_rejects_high_price(self):
        """YES at 0.70 — NO price = 0.30. AI says BUY_NO but NO at 0.30 is in range.
        Check whether arb generates BUY_NO with sufficient edge."""
        market = _make_market(yes_price=0.70)
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        # AI at yes=0.70: estimated_prob = 0.5 + (0.5-0.7)*0.3 = 0.44, rec=BUY_NO
        # no_edge = (1 - 0.44) - 0.30 = 0.26, effective = 0.26 - 0.02 - 0.03 = 0.21 > 0.10
        # no_price = 0.30, in [0.20, 0.40] -> should trigger
        assert len(signals) >= 1
        assert signals[0].action == "BUY_NO"

    def test_arb_reset_processed_allows_rescan(self):
        """After reset_processed, same market can be analyzed again."""
        market = _make_market(yes_price=0.30)
        run_async(self.strategy.scan_and_analyze([market], 10000))
        # Second scan should return nothing (market already processed)
        signals2 = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals2) == 0
        # After reset, should work again
        self.strategy.reset_processed()
        signals3 = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals3) >= 1


# ─── NEH STRATEGY ────────────────────────────────────────────────

class TestNEHStrategy:
    def setup_method(self):
        self.config = _make_config()
        self.ai = BacktestAIAgent(self.config)
        self.sizer = PositionSizer(kelly_fraction=0.25, max_position_pct=0.05)
        self.strategy = NothingEverHappensStrategy(self.config, self.ai, self.sizer)

    def test_neh_triggers_on_low_yes_long_term(self):
        """YES at 0.08, resolves in 60 days — classic NEH opportunity."""
        market = _make_market(yes_price=0.08, end_date=datetime.now() + timedelta(days=60))
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) >= 1
        sig = signals[0]
        assert sig.action == "SELL_YES"
        assert sig.price == 0.08

    def test_neh_rejects_high_yes_price(self):
        """YES at 0.30 — too expensive for NEH (max_yes_price=0.15)."""
        market = _make_market(yes_price=0.30, end_date=datetime.now() + timedelta(days=60))
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) == 0

    def test_neh_rejects_short_term(self):
        """YES at 0.08, resolves in 10 days — too short for NEH (min 30 days)."""
        market = _make_market(yes_price=0.08, end_date=datetime.now() + timedelta(days=10))
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) == 0

    def test_neh_rejects_no_end_date(self):
        """Market with no end_date should be skipped."""
        market = _make_market(yes_price=0.08, end_date=None)
        signals = run_async(self.strategy.scan_and_analyze([market], 10000))
        assert len(signals) == 0

    def test_neh_confidence_scales_with_time(self):
        """Longer-dated markets should have higher confidence."""
        market_60d = _make_market(yes_price=0.08, end_date=datetime.now() + timedelta(days=60), market_id="m60")
        market_180d = _make_market(yes_price=0.08, end_date=datetime.now() + timedelta(days=180), market_id="m180")
        sig_60 = run_async(self.strategy.scan_and_analyze([market_60d], 10000))
        self.strategy = NothingEverHappensStrategy(self.config, self.ai, self.sizer)
        sig_180 = run_async(self.strategy.scan_and_analyze([market_180d], 10000))
        assert len(sig_60) >= 1 and len(sig_180) >= 1
        assert sig_180[0].confidence > sig_60[0].confidence


# ─── BACKTEST AI AGENT ────────────────────────────────────────────

class TestBacktestAI:
    def setup_method(self):
        self.config = _make_config()
        self.ai = BacktestAIAgent(self.config)

    def test_ai_fade_yes_consensus(self):
        """YES at 0.90 -> AI should say true prob is much lower, recommend BUY_NO."""
        result = run_async(self.ai.analyze_market("Q", "", 0.90, "m1"))
        assert result is not None
        assert result.recommendation == "BUY_NO"
        assert result.estimated_probability < 0.50

    def test_ai_fade_no_consensus(self):
        """YES at 0.10 (NO at 0.90) -> AI should recommend BUY_YES."""
        result = run_async(self.ai.analyze_market("Q", "", 0.10, "m2"))
        assert result is not None
        assert result.recommendation == "BUY_YES"
        assert result.estimated_probability > 0.10

    def test_ai_arb_underpriced_yes(self):
        """YES at 0.30 -> AI estimates higher, recommends BUY_YES."""
        result = run_async(self.ai.analyze_market("Q", "", 0.30, "m3"))
        assert result is not None
        assert result.recommendation == "BUY_YES"
        assert result.estimated_probability > 0.30

    def test_ai_no_signal_at_fair(self):
        """YES at 0.50 -> no signal (fair price, no edge)."""
        result = run_async(self.ai.analyze_market("Q", "", 0.50, "m4"))
        assert result is None

    def test_ai_no_signal_in_dead_zone(self):
        """YES at 0.45 -> between arb threshold (0.40) and fade (0.80), no signal."""
        result = run_async(self.ai.analyze_market("Q", "", 0.45, "m5"))
        assert result is None


# ─── CONSENSUS vs CRYPTO UPDOWN ───────────────────────────────────

class TestConsensusSkipsCryptoUpdown:
    """Consensus alerts must not fire on BTC/SOL/ETH short up-down windows (wrong label + wrong product)."""

    def test_is_crypto_updown_market_detects_btc_sol_eth(self):
        from src.market.scanner import is_crypto_updown_market

        end = datetime.now() + timedelta(hours=10)
        for q in (
            "Bitcoin Up or Down - March 9, 2:15AM-2:30AM ET",
            "Solana Up or Down - March 9, 2:15AM-2:20AM ET",
            "Ethereum Up or Down - March 9, 2:15AM-2:30AM ET",
            "Hyperliquid Up or Down - March 9, 2:15AM-2:20AM ET",
        ):
            m = Market(
                id="x",
                question=q,
                description="",
                volume=1,
                liquidity=50000,
                yes_price=0.90,
                no_price=0.10,
                spread=0.02,
                end_date=end,
                token_id_yes="y",
                token_id_no="n",
                group_item_title="",
            )
            assert is_crypto_updown_market(m) is True
        generic = _make_market(0.90, end_date=end)
        assert is_crypto_updown_market(generic) is False

    def test_consensus_scan_ignores_btc_updown_even_at_high_yes(self):
        from src.strategies.consensus import ConsensusStrategy

        cfg = {
            "strategies": {
                "consensus": {
                    "enabled": True,
                    "threshold": 0.85,
                    "min_opposite_liquidity": 1,
                    "expiration_window_hours": 48,
                }
            }
        }
        strat = ConsensusStrategy(cfg)
        end = datetime.now() + timedelta(hours=10)
        m = Market(
            id="btc-updown-15m",
            question="Bitcoin Up or Down - March 9, 2:15AM-2:30AM ET",
            description="",
            volume=100000,
            liquidity=50000,
            yes_price=0.92,
            no_price=0.08,
            spread=0.02,
            end_date=end,
            token_id_yes="y",
            token_id_no="n",
            group_item_title="",
        )
        assert strat.scan_for_consensus([m]) == []

    def test_consensus_scan_still_emits_for_generic_market(self):
        from src.strategies.consensus import ConsensusStrategy

        cfg = {
            "strategies": {
                "consensus": {
                    "enabled": True,
                    "threshold": 0.85,
                    "min_opposite_liquidity": 1,
                    "expiration_window_hours": 48,
                }
            }
        }
        strat = ConsensusStrategy(cfg)
        end = datetime.now() + timedelta(hours=10)
        m = _make_market(0.92, end_date=end, liquidity=50000, market_id="gen-1")
        m = Market(
            id=m.id,
            question="Will candidate X win the primary?",
            description=m.description,
            volume=m.volume,
            liquidity=m.liquidity,
            yes_price=m.yes_price,
            no_price=m.no_price,
            spread=m.spread,
            end_date=m.end_date,
            token_id_yes=m.token_id_yes,
            token_id_no=m.token_id_no,
            group_item_title=m.group_item_title,
        )
        alerts = strat.scan_for_consensus([m])
        assert len(alerts) == 1
        assert alerts[0].market_id == "gen-1"
