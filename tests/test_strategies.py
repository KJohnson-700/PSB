"""
Strategy unit tests — verify signal logic produces correct outputs for known inputs.
No random data. Each test represents a real market scenario.
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from src.analysis.kelly_sizer import KellySizer
from src.market.scanner import Market
from src.backtest.backtest_ai import BacktestAIAgent
from src.analysis.math_utils import PositionSizer
from src.strategies.weather import WeatherStrategy
from src.main import _merge_weather_market_sources, _weather_general_scan_enabled

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
        "strategies": {},
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


# ─── BACKTEST AI AGENT ────────────────────────────────────────────

class TestBacktestAI:
    def setup_method(self):
        self.config = _make_config()
        self.ai = BacktestAIAgent(self.config)

    def test_ai_yes_consensus(self):
        """YES at 0.90 -> AI should say true prob is much lower, recommend BUY_NO."""
        result = run_async(self.ai.analyze_market("Q", "", 0.90, "m1"))
        assert result is not None
        assert result.recommendation == "BUY_NO"
        assert result.estimated_probability < 0.50

    def test_ai_no_consensus(self):
        """YES at 0.10 (NO at 0.90) -> AI should recommend BUY_YES."""
        result = run_async(self.ai.analyze_market("Q", "", 0.10, "m2"))
        assert result is not None
        assert result.recommendation == "BUY_YES"
        assert result.estimated_probability > 0.10

    def test_ai_underpriced_yes(self):
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
        """YES at 0.45 -> between thresholds, no signal."""
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


class TestWeatherStrategy:
    def test_merge_weather_market_sources_prefers_dedicated_and_dedupes(self):
        dedicated = [
            _make_market(0.20, market_id="wx-1"),
            _make_market(0.25, market_id="wx-2"),
        ]
        fallback = [
            _make_market(0.30, market_id="wx-2"),
            _make_market(0.35, market_id="wx-3"),
        ]
        merged = _merge_weather_market_sources(dedicated, fallback)
        assert [m.id for m in merged] == ["wx-1", "wx-2", "wx-3"]

    def test_weather_general_market_scan_is_opt_in(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {"enabled": True}
        assert _weather_general_scan_enabled(cfg) is False
        cfg["strategies"]["weather"]["scan_from_general_markets"] = True
        assert _weather_general_scan_enabled(cfg) is True

    def test_weather_strategy_ignores_crypto_updown_markets(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.50,
            "kelly_fraction": 0.25,
            "metar_enabled": False,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        end = datetime.now() + timedelta(hours=12)
        market = Market(
            id="sol-updown-15m",
            question="Solana Up or Down - March 9, 2:15AM-2:30AM ET",
            description="Weather-like words should not matter here.",
            volume=100000,
            liquidity=50000,
            yes_price=0.20,
            no_price=0.80,
            spread=0.02,
            end_date=end,
            token_id_yes="y",
            token_id_no="n",
            group_item_title="",
            slug="sol-updown-15m-123456",
        )
        with patch.object(
            strat,
            "_fetch_precip_forecast",
            side_effect=AssertionError("crypto updown market should be skipped before forecast fetch"),
        ):
            signals = run_async(strat.scan_and_analyze([market], bankroll=500.0))
        assert signals == []
        assert strat._scan_stats["total_markets_seen"] == 1
        assert strat._scan_stats["weather_keyword_matches"] == 0

    def test_kelly_sizer_honors_weather_strategy_config(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "kelly_fraction": 0.18,
        }
        sizer = KellySizer(cfg)
        assert sizer.get_kelly_fraction("weather", streak_multiplier=1.0) == 0.18

    def test_weather_strategy_resolves_city_from_slug_and_group_title(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.95,
            "kelly_fraction": 0.25,
            "metar_enabled": False,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        loc = strat._parse_market_location(
            "Highest temperature in Manila on Apr 29, 2026?",
            "highest-temperature-in-manila-on-apr-29-2026 manila",
        )
        assert loc is not None
        coords, icao = loc
        assert icao == "RPLL"
        assert coords[0] == pytest.approx(14.5086)

    @pytest.mark.parametrize(
        ("question", "description", "expected_icao"),
        [
            (
                "Will NYC have less than 2 inches of precipitation in April?",
                "will-nyc-have-less-than-2-inches-of-precipitation-in-april",
                "KLGA",
            ),
            (
                "Will Boston Logan have less than 2 inches of precipitation in April?",
                "will-boston-logan-have-less-than-2-inches-of-precipitation-in-april",
                "KBOS",
            ),
            (
                "Highest temperature in Chicago, IL on Apr 29, 2026?",
                "highest-temperature-in-chicago-il-on-apr-29-2026 ord",
                "KORD",
            ),
            (
                "Highest temperature in Paris on Apr 29, 2026?",
                "highest-temperature-in-paris-on-apr-29-2026 lfpb",
                "LFPB",
            ),
        ],
    )
    def test_weather_strategy_matches_live_city_alias_variants(
        self,
        question,
        description,
        expected_icao,
    ):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.95,
            "kelly_fraction": 0.25,
            "metar_enabled": False,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        loc = strat._parse_market_location(question, description)
        assert loc is not None
        _, icao = loc
        assert icao == expected_icao

    def test_weather_strategy_uses_le_bourget_for_paris(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.95,
            "kelly_fraction": 0.25,
            "metar_enabled": False,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        loc = strat._parse_market_location(
            "Highest temperature in Paris on Apr 29, 2026?",
            "highest-temperature-in-paris-on-apr-29-2026",
        )
        assert loc is not None
        coords, icao = loc
        assert icao == "LFPB"
        assert coords[0] == pytest.approx(49.0097)
        assert coords[1] == pytest.approx(2.5479)

    def test_weather_fee_buffer_scales_with_contract_price(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {"enabled": True, "ev_fee_buffer_pct": 0.02}
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        assert strat._fee_buffer(0.05) == pytest.approx(0.001)
        assert strat._fee_buffer(0.70) == pytest.approx(0.014)

    def test_weather_min_ev_is_horizon_adaptive(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "min_ev": 0.05,
            "min_ev_mult_t1": 1.2,
            "min_ev_mult_t2": 1.0,
            "min_ev_mult_t3": 0.8,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        assert strat._required_min_ev(1) == pytest.approx(0.06)
        assert strat._required_min_ev(2) == pytest.approx(0.05)
        assert strat._required_min_ev(3) == pytest.approx(0.04)

    def test_weather_metar_threshold_scales_by_horizon(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "metar_mismatch_threshold_c": 3.0,
            "metar_mismatch_threshold_c_per_day": 1.0,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        assert strat._metar_threshold_f(1) == pytest.approx(5.4)
        assert strat._metar_threshold_f(3) == pytest.approx(9.0)

    def test_weather_binary_kelly_uses_price_specific_payout(self):
        sizer = PositionSizer(
            kelly_fraction=0.25,
            max_position_pct=0.50,
            min_position=0.0,
            max_position=500.0,
        )
        low_price_size = sizer.calculate_binary_kelly_bet(
            bankroll=500.0,
            win_probability=0.20,
            contract_price=0.10,
            kelly_fraction=0.25,
        )
        high_price_size = sizer.calculate_binary_kelly_bet(
            bankroll=500.0,
            win_probability=0.80,
            contract_price=0.70,
            kelly_fraction=0.25,
        )
        assert low_price_size != high_price_size
        assert low_price_size == pytest.approx(13.89, abs=0.01)
        assert high_price_size == pytest.approx(41.67, abs=0.01)

    def test_weather_strategy_prices_cheap_precip_signal_with_binary_kelly(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.95,
            "kelly_fraction": 0.25,
            "metar_enabled": False,
            "ev_fee_buffer_pct": 0.02,
            "min_ev_mult_t1": 1.2,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        market = Market(
            id="wx-cheap-precip",
            question="Will NYC have more than 0.5 inches of precipitation tomorrow?",
            description="Weather precipitation market",
            volume=100000,
            liquidity=50000,
            yes_price=0.10,
            no_price=0.90,
            spread=0.02,
            end_date=datetime.now() + timedelta(hours=12),
            token_id_yes="y",
            token_id_no="n",
            group_item_title="Weather",
            slug="will-nyc-have-more-than-0-5-inches-of-precipitation-tomorrow",
        )
        with patch.object(strat, "_fetch_precip_forecast", return_value=0.30):
            signals = run_async(strat.scan_and_analyze([market], bankroll=500.0))
        assert len(signals) == 1
        signal = signals[0]
        assert signal.action == "BUY_YES"
        assert signal.price == pytest.approx(0.10)
        assert signal.size == pytest.approx(25.0)
        assert signal.horizon_days == 1

    def test_weather_strategy_skips_ai_for_clear_quant_edge(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "borderline_high": 0.10,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.95,
            "kelly_fraction": 0.25,
            "metar_enabled": False,
            "use_weather_ai": True,
        }
        fake_ai = type("FakeAI", (), {"is_available": lambda self: True})()
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg), fake_ai)
        strat.weather_ensemble.run = AsyncMock(return_value=None)
        market = Market(
            id="wx-clear-edge",
            question="Will NYC have more than 0.5 inches of precipitation tomorrow?",
            description="Weather precipitation market",
            volume=100000,
            liquidity=50000,
            yes_price=0.10,
            no_price=0.90,
            spread=0.02,
            end_date=datetime.now() + timedelta(hours=12),
            token_id_yes="y",
            token_id_no="n",
            group_item_title="Weather",
            slug="will-nyc-have-more-than-0-5-inches-of-precipitation-tomorrow",
        )
        with patch.object(strat, "_fetch_precip_forecast", return_value=0.40):
            signals = run_async(strat.scan_and_analyze([market], bankroll=500.0))
        assert len(signals) == 1
        strat.weather_ensemble.run.assert_not_called()

    def test_weather_strategy_borderline_ai_recomputes_probability(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "borderline_high": 0.10,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.95,
            "kelly_fraction": 0.25,
            "metar_enabled": False,
            "use_weather_ai": True,
        }
        fake_ai = type("FakeAI", (), {"is_available": lambda self: True})()
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg), fake_ai)
        strat.weather_ensemble.run = AsyncMock(
            return_value=type(
                "Decision",
                (),
                {
                    "recommendation": "BUY_NO",
                    "estimated_probability": 0.18,
                    "to_signal_payload": lambda self: {
                        "recommendation": "BUY_NO",
                        "estimated_probability": 0.18,
                    },
                },
            )()
        )
        market = Market(
            id="wx-borderline-ai",
            question="Will NYC have more than 0.5 inches of precipitation tomorrow?",
            description="Weather precipitation market",
            volume=100000,
            liquidity=50000,
            yes_price=0.40,
            no_price=0.60,
            spread=0.02,
            end_date=datetime.now() + timedelta(hours=12),
            token_id_yes="y",
            token_id_no="n",
            group_item_title="Weather",
            slug="will-nyc-have-more-than-0-5-inches-of-precipitation-tomorrow",
        )
        with patch.object(strat, "_fetch_precip_forecast", return_value=0.58):
            signals = run_async(strat.scan_and_analyze([market], bankroll=500.0))
        assert len(signals) == 1
        signal = signals[0]
        assert signal.action == "BUY_NO"
        assert signal.forecast_prob == pytest.approx(0.18)
        assert signal.price == pytest.approx(0.60)
        assert signal.ai_ensemble == {
            "recommendation": "BUY_NO",
            "estimated_probability": 0.18,
        }
        strat.weather_ensemble.run.assert_awaited_once()

    def test_weather_strategy_prefers_temp_subtype_when_keywords_overlap(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.95,
            "kelly_fraction": 0.25,
            "metar_enabled": False,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        market = Market(
            id="wx-temp-overlap",
            question="Will the highest temperature in Hong Kong on May 5, 2026 be above 38 after rain?",
            description="Weather temperature market with overlapping rain wording.",
            volume=100000,
            liquidity=50000,
            yes_price=0.40,
            no_price=0.60,
            spread=0.02,
            end_date=datetime.now() + timedelta(hours=12),
            token_id_yes="y",
            token_id_no="n",
            group_item_title="Weather",
            slug="highest-temperature-in-hong-kong-on-may-5-2026-38c",
        )
        with patch.object(strat, "_fetch_temp_forecast", return_value=0.70), patch.object(
            strat,
            "_fetch_precip_forecast",
            side_effect=AssertionError("temp market should not route through precip forecast"),
        ):
            signals = run_async(strat.scan_and_analyze([market], bankroll=500.0))
        assert len(signals) == 1
        assert signals[0].subtype == "temp"
        assert signals[0].raw_forecast_prob == pytest.approx(0.70)

    def test_weather_strategy_precip_signal_is_tagged_precip(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.95,
            "kelly_fraction": 0.25,
            "metar_enabled": False,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        market = Market(
            id="wx-precip",
            question="Will Hong Kong have between 100-110mm of precipitation in May 2026?",
            description="Monthly precipitation market",
            volume=100000,
            liquidity=50000,
            yes_price=0.30,
            no_price=0.70,
            spread=0.02,
            end_date=datetime.now() + timedelta(hours=12),
            token_id_yes="y",
            token_id_no="n",
            group_item_title="Weather",
            slug="will-hong-kong-have-between-100-110mm-of-precipitation-in-may-2026",
        )
        with patch.object(strat, "_fetch_precip_forecast", return_value=0.60):
            signals = run_async(strat.scan_and_analyze([market], bankroll=500.0))
        assert len(signals) == 1
        assert signals[0].subtype == "precip"

    def test_weather_strategy_applies_city_horizon_calibration_bias(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.95,
            "kelly_fraction": 0.25,
            "metar_enabled": False,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        strat.calibration_store.apply_correction = lambda raw, city, horizon: (0.50, 0.20, 30)
        market = Market(
            id="wx-cal",
            question="Will the highest temperature in Hong Kong on May 5, 2026 be above 38?",
            description="Weather temperature market",
            volume=100000,
            liquidity=50000,
            yes_price=0.30,
            no_price=0.70,
            spread=0.02,
            end_date=datetime.now() + timedelta(hours=12),
            token_id_yes="y",
            token_id_no="n",
            group_item_title="Weather",
            slug="highest-temperature-in-hong-kong-on-may-5-2026-38c",
        )
        with patch.object(strat, "_fetch_temp_forecast", return_value={"prob": 0.70, "forecast_temp_f": 85.0}):
            signals = run_async(strat.scan_and_analyze([market], bankroll=500.0))
        assert len(signals) == 1
        assert signals[0].forecast_prob == pytest.approx(0.50)
        assert signals[0].raw_forecast_prob == pytest.approx(0.70)
        assert signals[0].calibration_bias == pytest.approx(0.20)
        assert signals[0].calibration_count == 30
        assert signals[0].city == "hong-kong"
        assert signals[0].horizon_days == 1

    def test_weather_strategy_skips_large_metar_mismatch(self):
        cfg = _make_config()
        cfg["strategies"]["weather"] = {
            "enabled": True,
            "gap_min": 0.15,
            "min_ev": 0.05,
            "min_volume": 0,
            "min_liquidity": 0,
            "min_hours_to_resolution": 0,
            "max_hours_to_resolution": 9999,
            "max_yes_price": 0.95,
            "kelly_fraction": 0.25,
            "metar_enabled": True,
            "metar_mismatch_threshold_c": 3.0,
        }
        strat = WeatherStrategy(cfg, PositionSizer(cfg), KellySizer(cfg))
        market = Market(
            id="wx-metar",
            question="Will the highest temperature in Hong Kong on May 5, 2026 be above 38?",
            description="Weather temperature market",
            volume=100000,
            liquidity=50000,
            yes_price=0.30,
            no_price=0.70,
            spread=0.02,
            end_date=datetime.now() + timedelta(hours=12),
            token_id_yes="y",
            token_id_no="n",
            group_item_title="Weather",
            slug="highest-temperature-in-hong-kong-on-may-5-2026-38c",
        )
        with patch.object(strat, "_fetch_temp_forecast", return_value={"prob": 0.70, "forecast_temp_f": 85.0}), patch.object(
            strat, "_fetch_metar", return_value={"temp": 20.0}
        ):
            signals = run_async(strat.scan_and_analyze([market], bankroll=500.0))
        assert signals == []
        assert strat._scan_stats["skipped_metar_mismatch"] == 1
