"""Tests for live dashboard config updates on a running bot."""

import asyncio
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.clob_client import OrderStatus, Position
from src.execution.live_testing import ExitDecision
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
            "doge_macro": {"enabled": False, "kelly_fraction": 0.10},
            "bnb_macro": {"enabled": False, "kelly_fraction": 0.10},
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
    bot.doge_exposure_manager = _FakeExposureManager()
    bot.bnb_exposure_manager = _FakeExposureManager()
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
    assert bot.btc_exposure_manager.reloaded_with == {"loss_kill_switch_enabled": False}
    assert bot.notifier.reloaded_with is bot.config
    assert bot.bitcoin_strategy.lane_calibrator is bot.lane_calibrator
    assert bot.sol_macro_strategy.lane_calibrator is bot.lane_calibrator
    assert bot.eth_macro_strategy.lane_calibrator is bot.lane_calibrator
    assert bot.hype_macro_strategy.lane_calibrator is bot.lane_calibrator
    assert bot.xrp_macro_strategy.lane_calibrator is bot.lane_calibrator
    assert bot.doge_macro_strategy.lane_calibrator is bot.lane_calibrator
    assert bot.bnb_macro_strategy.lane_calibrator is bot.lane_calibrator


def test_apply_config_updates_does_not_mutate_exposure_env_override(monkeypatch):
    bot = PolyBot.__new__(PolyBot)
    bot.config = {
        "ai": {"enabled": True},
        "trading": {"dry_run": True},
        "strategies": {
            "bitcoin": {"enabled": True},
            "sol_macro": {"enabled": True},
            "eth_macro": {"enabled": True},
            "hype_macro": {"enabled": True},
            "xrp_macro": {"enabled": True},
            "doge_macro": {"enabled": True},
            "bnb_macro": {"enabled": True},
        },
        "exposure": {"loss_kill_switch_enabled": True},
        "polymarket": {},
    }
    bot.ai_agent = _FakeAIAgent()
    bot.notifier = _FakeNotifier()
    bot.market_scanner = _FakeScanner()
    bot.btc_exposure_manager = _FakeExposureManager()
    bot.sol_exposure_manager = _FakeExposureManager()
    bot.eth_exposure_manager = _FakeExposureManager()
    bot.hype_exposure_manager = _FakeExposureManager()
    bot.xrp_exposure_manager = _FakeExposureManager()
    bot.doge_exposure_manager = _FakeExposureManager()
    bot.bnb_exposure_manager = _FakeExposureManager()
    bot.kelly_sizer = None
    bot.lane_calibrator = object()
    monkeypatch.setenv("EXPOSURE_LOSS_KILL_SWITCH_ENABLED", "true")

    bot.apply_config_updates({"exposure": {"loss_kill_switch_enabled": False}})

    assert os.environ["EXPOSURE_LOSS_KILL_SWITCH_ENABLED"] == "true"


def test_lane_calibration_mode_follows_dry_run_toggle():
    bot = PolyBot.__new__(PolyBot)
    bot.config = {
        "trading": {"dry_run": True},
        "lane_calibration": {
            "enabled": True,
            "shadow_mode": True,
            "paper_shadow_mode": True,
            "live_shadow_mode": False,
        },
    }

    assert bot._lane_calibration_shadow_mode() is True

    bot.config["trading"]["dry_run"] = False
    assert bot._lane_calibration_shadow_mode() is False


def test_apply_config_updates_recomputes_lane_calibration_mode_when_trading_mode_changes():
    class _FakeCalibrator:
        def __init__(self):
            self.shadow_mode = True

    bot = PolyBot.__new__(PolyBot)
    bot.config = {
        "trading": {"dry_run": True},
        "lane_calibration": {
            "enabled": True,
            "shadow_mode": True,
            "paper_shadow_mode": True,
            "live_shadow_mode": False,
        },
        "ai": {"enabled": True},
        "strategies": {
            "bitcoin": {"enabled": True},
            "sol_macro": {"enabled": True},
            "eth_macro": {"enabled": True},
            "hype_macro": {"enabled": True},
            "xrp_macro": {"enabled": True},
            "doge_macro": {"enabled": True},
            "bnb_macro": {"enabled": True},
        },
        "exposure": {},
        "polymarket": {},
    }
    bot.ai_agent = _FakeAIAgent()
    bot.notifier = _FakeNotifier()
    bot.market_scanner = _FakeScanner()
    bot.btc_exposure_manager = _FakeExposureManager()
    bot.sol_exposure_manager = _FakeExposureManager()
    bot.eth_exposure_manager = _FakeExposureManager()
    bot.hype_exposure_manager = _FakeExposureManager()
    bot.xrp_exposure_manager = _FakeExposureManager()
    bot.doge_exposure_manager = _FakeExposureManager()
    bot.bnb_exposure_manager = _FakeExposureManager()
    bot.kelly_sizer = None
    bot.lane_calibrator = _FakeCalibrator()

    bot.apply_config_updates({"trading": {"dry_run": False}})

    assert bot.lane_calibrator.shadow_mode is False


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
        pnl=4.0, strategy="bitcoin", market_id="m1", window_size="5m"
    )
    bot.kelly_sizer.record_outcome.assert_called_once_with("bitcoin", True, "5m")


def test_resolution_price_updates_do_not_run_exit_checks():
    # Regression (2026-06-14): the resolution/price-update stage must NOT call
    # _run_exit_checks. That serializes on _exit_lock and is already driven by the 60s
    # scan cycle + the 10s fast-exit loop; a third caller here blocked the cycle on the
    # lock while the fast-exit loop held it mid-exit, hanging the whole trading loop
    # right after "Updated prices…". Marks update; the fast-exit loop handles exits.
    bot = PolyBot.__new__(PolyBot)
    bot.bankroll = 100.0
    pos = SimpleNamespace(
        market_id="m1",
        token_id_yes="yes-token",
        token_id_no="no-token",
    )
    bot.risk_manager = SimpleNamespace(active_positions={"p1": pos})
    bot.journal = MagicMock()
    bot.journal.take_snapshot = MagicMock()
    bot.ctf_redeemer = None
    bot.resolution_tracker = MagicMock()
    bot.resolution_tracker.check_and_settle = MagicMock(return_value=[])
    bot.resolution_tracker.check_price_updates = MagicMock(return_value=(1, {"m1": 0.72}))
    bot._run_exit_checks = AsyncMock(return_value=1)

    asyncio.run(bot._run_resolution_check("[test]"))

    bot.resolution_tracker.check_price_updates.assert_called_once_with(
        bot.journal, bot.bankroll, True
    )
    bot._run_exit_checks.assert_not_awaited()


def _exit_test_bot(status: OrderStatus) -> PolyBot:
    bot = PolyBot.__new__(PolyBot)
    bot.config = {"trading": {"dry_run": False}}
    bot.bankroll = 100.0
    bot._execution_lock = asyncio.Lock()
    pos = Position(
        position_id="p1",
        market_id="m1",
        market_question="Bitcoin Up or Down - test",
        outcome="YES",
        size=10.0,
        entry_price=0.50,
        current_price=0.50,
        pnl=0.0,
        opened_at=datetime.now(),
        end_date=datetime.now() + timedelta(minutes=5),
        strategy="bitcoin",
        window_size="5m",
    )
    bot.risk_manager = SimpleNamespace(active_positions={"p1": pos})
    bot.clob_client = SimpleNamespace(
        place_order=AsyncMock(return_value=SimpleNamespace(order_id="oid1")),
        get_order_status=AsyncMock(return_value=status),
    )
    bot.journal = MagicMock()
    bot.circuit_breakers = MagicMock()
    bot.circuit_breakers.action_from_position.return_value = "BUY_YES"
    bot.kelly_sizer = MagicMock()
    bot.notifier = SimpleNamespace(notify_exit=AsyncMock(return_value=True))
    bot.exit_excursion = MagicMock()
    bot._get_exposure_manager_for = MagicMock(return_value=None)
    bot._apply_realized_pnl_to_bankroll = MagicMock(return_value=101.0)
    bot._log_closed_trade_for_calibration = MagicMock()
    return bot


def _exit_decision() -> ExitDecision:
    return ExitDecision(
        position_id="p1",
        market_id="m1",
        action="SELL",
        token_id="yes-token",
        size=10.0,
        current_price=0.60,
        exit_price=0.60,
        reason="take_profit",
        unrealized_pnl=1.0,
        hours_held=0.1,
    )


@pytest.mark.asyncio
async def test_live_exit_pending_order_keeps_position_open():
    bot = _exit_test_bot(OrderStatus.PENDING)

    await bot._handle_exit_decision(_exit_decision())

    assert "p1" in bot.risk_manager.active_positions
    assert getattr(bot.risk_manager.active_positions["p1"], "pending_exit_order_id") == "oid1"
    bot.journal.log_exit.assert_not_called()
    bot._apply_realized_pnl_to_bankroll.assert_not_called()


@pytest.mark.asyncio
async def test_live_exit_filled_order_closes_position_and_logs():
    bot = _exit_test_bot(OrderStatus.FILLED)

    await bot._handle_exit_decision(_exit_decision())

    assert "p1" not in bot.risk_manager.active_positions
    bot.journal.log_exit.assert_called_once()
    bot._apply_realized_pnl_to_bankroll.assert_called_once_with(1.0)
