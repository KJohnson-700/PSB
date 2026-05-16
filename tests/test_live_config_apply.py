"""Tests for live dashboard config updates on a running bot."""

import asyncio
from unittest.mock import MagicMock

from src.main import PolyBot


class _FakeAIAgent:
    def __init__(self):
        self.refreshed_with = None

    def refresh_from_config(self, cfg):
        self.refreshed_with = cfg


class _FakeExposureManager:
    def __init__(self):
        self.reloaded_with = None
        self._on_pause_ai_callback = None

    def reload_from_config(self, cfg):
        self.reloaded_with = cfg


class _FakeScanner:
    def __init__(self):
        self.reloaded_with = None

    def reload_from_config(self, cfg):
        self.reloaded_with = cfg


class _FakeNotifier:
    def __init__(self):
        self.reloaded_with = None

    def reload_from_config(self, cfg):
        self.reloaded_with = cfg


def test_apply_config_updates_refreshes_live_runtime_objects():
    bot = PolyBot.__new__(PolyBot)
    bot.config = {
        "ai": {"enabled": True},
        "trading": {
            "kelly_fraction": 0.25,
            "max_exposure_per_trade": 0.05,
            "default_position_size": 10,
            "max_position_size": 15,
        },
        "strategies": {
            "bitcoin": {"enabled": True, "kelly_fraction": 0.15},
            "sol_macro": {"enabled": True, "kelly_fraction": 0.15},
            "eth_macro": {"enabled": False, "kelly_fraction": 0.12},
            "hype_macro": {"enabled": False, "kelly_fraction": 0.08},
            "xrp_macro": {"enabled": False, "kelly_fraction": 0.10},
            "weather": {"enabled": False, "kelly_fraction": 0.25},
        },
        "exposure": {"loss_kill_switch_enabled": True},
        "polymarket": {"min_liquidity": 10000, "scanner_sync_timeout_sec": 90},
    }
    bot.ai_agent = _FakeAIAgent()
    bot.notifier = _FakeNotifier()
    bot.market_scanner = _FakeScanner()
    bot.btc_exposure_manager = _FakeExposureManager()
    bot.sol_exposure_manager = _FakeExposureManager()
    bot.eth_exposure_manager = _FakeExposureManager()
    bot.hype_exposure_manager = _FakeExposureManager()
    bot.xrp_exposure_manager = _FakeExposureManager()
    _weather_em = _FakeExposureManager()
    bot.weather_exposure_manager = _weather_em
    bot.event_exposure_manager = _weather_em
    bot._dead_zone_skip_callback = lambda **kwargs: None
    bot.kelly_sizer = None
    bot.lane_calibrator = object()

    bot.apply_config_updates(
        {
            "ai": {"enabled": False},
            "trading": {
                "kelly_fraction": 0.5,
                "default_position_size": 7,
                "max_position_size": 22,
            },
            "strategies": {
                "bitcoin": {"enabled": False, "kelly_fraction": 0.20},
                "eth_macro": {"enabled": True},
                "weather": {"enabled": True},
            },
            "exposure": {"loss_kill_switch_enabled": False},
        }
    )

    assert bot.market_scanner.reloaded_with is bot.config
    assert bot.ai_agent.refreshed_with == {"enabled": False}
    assert bot.position_sizer.kelly_fraction == 0.5
    assert bot.position_sizer.min_position == 7
    assert bot.position_sizer.max_position == 22
    assert bot.kelly_sizer.get_asset_config("bitcoin").base_kelly_fraction == 0.20
    assert bot.bitcoin_strategy.enabled is False
    assert bot.eth_macro_strategy.enabled is True
    assert bot.weather_strategy.enabled is True
    assert callable(bot.bitcoin_strategy.dead_zone_skip_callback)
    assert bot.btc_exposure_manager.reloaded_with == {"loss_kill_switch_enabled": False}
    assert bot.event_exposure_manager.reloaded_with == {"loss_kill_switch_enabled": False}
    assert bot.notifier.reloaded_with is bot.config
    assert bot.bitcoin_strategy.lane_calibrator is bot.lane_calibrator
    assert bot.sol_macro_strategy.lane_calibrator is bot.lane_calibrator
    assert bot.eth_macro_strategy.lane_calibrator is bot.lane_calibrator
    assert bot.hype_macro_strategy.lane_calibrator is bot.lane_calibrator
    assert bot.xrp_macro_strategy.lane_calibrator is bot.lane_calibrator


def test_apply_realized_pnl_to_bankroll_floors_at_zero():
    bot = PolyBot.__new__(PolyBot)
    bot.bankroll = 3.5
    bot.risk_manager = MagicMock()

    updated = bot._apply_realized_pnl_to_bankroll(-10.0)

    assert updated == 0.0
    assert bot.bankroll == 0.0
    bot.risk_manager.update_pnl.assert_called_once_with(-10.0)
    assert bot.risk_manager.bankroll == 0.0


def test_run_resolution_check_records_kelly_outcome_for_settled_trade():
    bot = PolyBot.__new__(PolyBot)
    bot.bankroll = 100.0
    bot.risk_manager = MagicMock()
    bot.journal = MagicMock()
    bot.ctf_redeemer = None
    bot._apply_realized_pnl_to_bankroll = MagicMock(return_value=104.0)
    bot.kelly_sizer = MagicMock()

    exposure_manager = MagicMock()
    bot._get_exposure_manager_for = MagicMock(return_value=exposure_manager)

    settled = [
        {
            "strategy": "bitcoin",
            "market_id": "m1",
            "market_question": "Bitcoin Up or Down - April 21, 1:30AM-1:35AM ET",
            "pnl": 4.0,
        }
    ]
    bot.resolution_tracker = MagicMock()
    bot.resolution_tracker.check_and_settle = MagicMock(return_value=settled)
    bot.resolution_tracker.check_price_updates = MagicMock(return_value=0)

    asyncio.run(bot._run_resolution_check())

    exposure_manager.record_trade.assert_called_once_with(
        pnl=4.0, strategy="bitcoin", market_id="m1"
    )
    bot.kelly_sizer.record_outcome.assert_called_once_with("bitcoin", True, "5m")
