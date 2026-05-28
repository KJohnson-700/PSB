"""
Execution-driver tests: PolyBot._execute_*_signal_impl paths that place orders.

These catch Python scoping bugs (e.g. UnboundLocalError on `side`) and ordering bugs
(`can_sell_token` vs `side` assignment) without booting MarketScanner / WebSocket / full bot.

See vault note: Hermes `projects/psb/notes/2026-04-22-psb-execution-driver-tests.md`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.main import PolyBot
from src.strategies.bitcoin import BitcoinSignal
from src.strategies.sol_macro import SolMacroSignal


def _bare_polybot() -> PolyBot:
    """PolyBot instance without running __init__ (no scanner/ws)."""
    return object.__new__(PolyBot)


def _base_config() -> dict:
    return {
        "trading": {
            "dry_run": True,
            "default_position_size": 10,
            "max_position_size": 15,
        },
        "ai": {
            "enabled": True,
            "live_inferencing": True,
            "decision_layer": {"enabled": False},
        },
    }


def _attach_mocks(bot: PolyBot) -> None:
    bot.config = _base_config()
    bot.bankroll = 500.0
    bot.risk_manager = MagicMock()
    bot.risk_manager.can_trade = MagicMock(return_value=(True, "OK"))
    bot.risk_manager.evaluate_entry = MagicMock(return_value=(True, 15.0, "ok"))
    bot.risk_manager.add_position = MagicMock()
    bot.risk_manager.active_positions = {}
    bot.journal = MagicMock()
    bot.journal.get_open_positions = MagicMock(return_value=[])
    bot.journal.get_closed_trades = MagicMock(return_value=[])
    bot.journal.get_all_entries = MagicMock(return_value=[])
    bot.notifier = MagicMock()
    bot.notifier.notify_trade = AsyncMock()
    bot.clob_client = MagicMock()
    bot.clob_client.can_sell_token = AsyncMock(return_value=True)
    order = MagicMock()
    order.order_id = "ord_exec_driver_test"
    bot.clob_client.place_order = AsyncMock(return_value=order)
    bot.lane_manager = MagicMock()
    bot.lane_manager.can_execute = MagicMock(
        side_effect=lambda lane_id, *, dry_run: (True, "", "active", lane_id)
    )
    bot._session_traded_market_ids = set()


def _sol_like_signal(*, action: str, strategy_name: str = "hype_macro") -> SolMacroSignal:
    direction = "DOWN" if action == "BUY_NO" else "UP"
    return SolMacroSignal(
        market_id="m_exec_drv_1",
        market_question="Hyperliquid Up or Down — test",
        action=action,
        price=0.5,
        size=10.0,
        confidence=0.6,
        edge=0.1,
        token_id_yes="0x" + "a" * 64,
        token_id_no="0x" + "b" * 64,
        end_date=datetime.now(timezone.utc) + timedelta(hours=1),
        direction=direction,
        strategy_name=strategy_name,
        reason="execution driver test",
        est_prob=0.42,
        raw_est_prob=0.47,
        htf_bias="BEARISH" if action == "BUY_NO" else "BULLISH",
        primary_htf_bias="BEARISH" if action == "BUY_NO" else "BULLISH",
        alt_htf_bias="BULLISH",
        btc_htf_bias="BULLISH",
        side_source="primary_htf",
        conflict_type="alt_macro_quant_momentum_disagree",
        resolver_path="primary_htf__htf_short__quant_long__momentum_long",
        htf_side="SHORT",
        quant_side="LONG",
        momentum_side="LONG",
        btc_1h_regime="BEAR",
        window_size="15m",
        entry_policy={"side": "up", "window_size": "15m", "min_edge": 0.1},
    )


def _bitcoin_signal(*, action: str = "BUY_YES") -> BitcoinSignal:
    direction = "DOWN" if action == "BUY_NO" else "UP"
    return BitcoinSignal(
        market_id="m_btc_1",
        market_question="Bitcoin Up or Down — test",
        action=action,
        price=0.5,
        size=10.0,
        confidence=0.6,
        edge=0.1,
        token_id_yes="0x" + "c" * 64,
        token_id_no="0x" + "d" * 64,
        end_date=datetime.now(timezone.utc) + timedelta(hours=1),
        direction=direction,
        htf_bias="BULLISH",
        window_size="15m",
        est_prob=0.42,
        raw_est_prob=0.47,
        side_source="btc_quant_disagree_flip",
        conflict_type="long_to_short_quant_disagree",
        resolver_path="htf_bullish__side_long__quant_short",
        htf_side="LONG",
        quant_side="SHORT",
        momentum_side="SHORT",
        entry_policy={"side": "up", "window_size": "15m", "min_edge": 0.1},
    )


def _xrp_signal(*, action: str = "BUY_YES") -> SolMacroSignal:
    return SolMacroSignal(
        market_id="m_xrp_1",
        market_question="XRP Up or Down — test",
        action=action,
        price=0.4,
        size=10.0,
        confidence=0.6,
        edge=0.08,
        token_id_yes="0x" + "e" * 64,
        token_id_no="0x" + "f" * 64,
        end_date=datetime.now(timezone.utc) + timedelta(hours=1),
        direction="UP",
        strategy_name="xrp_macro",
        reason="xrp macro test",
    )


def _assert_buy_no_execution(bot: PolyBot, *, token_id_no: str, strategy: str) -> None:
    order_kwargs = bot.clob_client.place_order.call_args.kwargs
    assert order_kwargs["side"] == "BUY"
    assert order_kwargs["token_id"] == token_id_no
    assert order_kwargs.get("order_outcome") == "NO"

    position = bot.risk_manager.add_position.call_args.args[0]
    assert position.outcome == "NO"
    assert position.entry_leg == "NO"
    assert position.strategy == strategy

    journal_kwargs = bot.journal.log_entry.call_args.kwargs
    assert journal_kwargs["strategy"] == strategy
    assert journal_kwargs["action"] == "BUY_NO"
    assert journal_kwargs["side"] == "BUY"
    assert journal_kwargs["outcome"] == "NO"
    assert journal_kwargs["entry_leg"] == "NO"


@pytest.mark.asyncio
async def test_execute_sol_macro_impl_hype_macro_buy_yes_no_unbound_side():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _sol_like_signal(action="BUY_YES", strategy_name="hype_macro")
    await bot._execute_sol_macro_signal_impl(sig)
    bot.clob_client.place_order.assert_called_once()
    kwargs = bot.clob_client.place_order.call_args.kwargs
    assert kwargs["side"] == "BUY"
    bot.journal.log_entry.assert_called_once()


@pytest.mark.asyncio
async def test_execute_sol_macro_impl_sell_yes():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _sol_like_signal(action="SELL_YES", strategy_name="sol_macro")
    await bot._execute_sol_macro_signal_impl(sig)
    assert bot.clob_client.place_order.call_args.kwargs["side"] == "SELL"


@pytest.mark.asyncio
async def test_execute_sol_macro_impl_unknown_action_returns_without_place():
    bot = _bare_polybot()
    _attach_mocks(bot)
    raw = _sol_like_signal(action="BUY_YES", strategy_name="sol_macro")
    sig = raw.model_copy(update={"action": "INVALID_ACTION"})
    await bot._execute_sol_macro_signal_impl(sig)
    bot.clob_client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_sol_macro_impl_buy_no():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _sol_like_signal(action="BUY_NO", strategy_name="sol_macro")
    await bot._execute_sol_macro_signal_impl(sig)
    bot.clob_client.place_order.assert_called_once()
    _assert_buy_no_execution(bot, token_id_no=sig.token_id_no, strategy="sol_macro")


@pytest.mark.asyncio
async def test_execute_sol_macro_blocks_reentry_after_same_market_closed():
    bot = _bare_polybot()
    _attach_mocks(bot)
    bot.journal.get_closed_trades.return_value = [
        {"market_id": "m_exec_drv_1", "trade_id": "old_trade", "strategy": "hype_macro"}
    ]
    bot._session_traded_market_ids = bot._load_session_traded_market_ids()
    sig = _sol_like_signal(action="BUY_NO", strategy_name="hype_macro")

    await bot._execute_sol_macro_signal_impl(sig)

    bot.clob_client.place_order.assert_not_called()
    bot.risk_manager.add_position.assert_not_called()
    bot.journal.log_entry.assert_not_called()
    bot.journal.log_skip.assert_called_once()
    assert bot.journal.log_skip.call_args.args[3] == "duplicate_session_market"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_name", ["sol_macro", "eth_macro", "hype_macro", "xrp_macro", "bnb_macro"])
async def test_sol_style_strategies_execute_buy_no_as_no_leg(strategy_name: str):
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _sol_like_signal(action="BUY_NO", strategy_name=strategy_name)
    await bot._execute_sol_macro_signal_impl(sig)
    bot.clob_client.place_order.assert_called_once()
    _assert_buy_no_execution(bot, token_id_no=sig.token_id_no, strategy=strategy_name)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_name", ["sol_macro", "eth_macro", "hype_macro", "xrp_macro", "bnb_macro"])
async def test_sol_style_strategies_execute_buy_yes_as_yes_leg(strategy_name: str):
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _sol_like_signal(action="BUY_YES", strategy_name=strategy_name)
    await bot._execute_sol_macro_signal_impl(sig)
    bot.clob_client.place_order.assert_called_once()
    kwargs = bot.clob_client.place_order.call_args.kwargs
    assert kwargs["side"] == "BUY"
    assert kwargs["token_id"] == sig.token_id_yes
    assert kwargs.get("order_outcome") == "YES"
    journal_kwargs = bot.journal.log_entry.call_args.kwargs
    assert journal_kwargs["strategy"] == strategy_name
    assert journal_kwargs["action"] == "BUY_YES"
    assert journal_kwargs["outcome"] == "YES"


@pytest.mark.asyncio
async def test_buy_no_annotation_receives_yes_mid_not_no_entry_price():
    bot = _bare_polybot()
    _attach_mocks(bot)
    bot._annotate_entry_async = AsyncMock()
    sig = _sol_like_signal(action="BUY_NO", strategy_name="sol_macro").model_copy(
        update={"price": 0.37}
    )

    await bot._execute_sol_macro_signal_impl(sig)

    annotation_kwargs = bot._annotate_entry_async.call_args.kwargs
    assert annotation_kwargs["action"] == "BUY_NO"
    assert annotation_kwargs["yes_price"] == pytest.approx(0.63)


@pytest.mark.asyncio
async def test_strategy_execution_logs_entry_policy_metadata_for_macro_signals():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _sol_like_signal(action="BUY_NO", strategy_name="sol_macro")

    await bot._execute_sol_macro_signal_impl(sig)

    journal_kwargs = bot.journal.log_entry.call_args.kwargs
    assert journal_kwargs["extra"]["entry_policy"] == sig.entry_policy


@pytest.mark.asyncio
async def test_sol_style_execution_lane_id_uses_distinct_primary_alt_btc_biases():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _sol_like_signal(action="BUY_NO", strategy_name="xrp_macro")

    await bot._execute_sol_macro_signal_impl(sig)

    lane_id = bot.lane_manager.can_execute.call_args.args[0]
    assert lane_id == "xrp_macro|15m|down|bearish__bullish__bear|standard"
    journal_extra = bot.journal.log_entry.call_args.kwargs["extra"]
    assert journal_extra["lane_id"] == lane_id
    assert journal_extra["primary_htf_bias"] == "BEARISH"
    assert journal_extra["alt_htf_bias"] == "BULLISH"
    assert journal_extra["side_source"] == "primary_htf"
    assert journal_extra["conflict_type"] == "alt_macro_quant_momentum_disagree"
    assert journal_extra["resolver_path"] == "primary_htf__htf_short__quant_long__momentum_long"
    assert journal_extra["htf_side"] == "SHORT"
    assert journal_extra["quant_side"] == "LONG"
    assert journal_extra["momentum_side"] == "LONG"
    position = bot.risk_manager.add_position.call_args.args[0]
    assert position.entry_signal["lane_id"] == lane_id
    assert position.entry_signal["resolver_path"] == "primary_htf__htf_short__quant_long__momentum_long"


@pytest.mark.asyncio
async def test_execute_bitcoin_impl_sets_side_before_order():
    bot = _bare_polybot()
    _attach_mocks(bot)
    await bot._execute_bitcoin_signal_impl(_bitcoin_signal(action="BUY_YES"))
    kwargs = bot.clob_client.place_order.call_args.kwargs
    assert kwargs["side"] == "BUY"
    assert kwargs.get("order_outcome") == "YES"


@pytest.mark.asyncio
async def test_execute_bitcoin_impl_buy_no_order_outcome():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _bitcoin_signal(action="BUY_NO")
    await bot._execute_bitcoin_signal_impl(sig)
    _assert_buy_no_execution(bot, token_id_no=sig.token_id_no, strategy="bitcoin")
    extra = bot.journal.log_entry.call_args.kwargs["extra"]
    assert extra["raw_est_prob"] == pytest.approx(0.47)
    assert extra["est_prob"] == pytest.approx(0.42)
    assert extra["conflict_type"] == "long_to_short_quant_disagree"
    assert extra["resolver_path"] == "htf_bullish__side_long__quant_short"
    assert extra["htf_side"] == "LONG"
    assert extra["quant_side"] == "SHORT"
    assert extra["momentum_side"] == "SHORT"
    position = bot.risk_manager.add_position.call_args.args[0]
    assert position.entry_signal["conflict_type"] == "long_to_short_quant_disagree"
    assert (
        position.entry_signal["lane_id"]
        == "bitcoin|15m|down|bullish|htf_bullish_side_long_quant_short"
    )
    assert extra["ai_enabled_at_entry"] is False
    assert extra["ai_decision_gate_enabled_at_entry"] is False
    assert extra["ai_analytics_enabled_at_entry"] is True
    assert extra["ai_live_inferencing_at_entry"] is True
    assert extra["ai_consulted"] is False
    assert extra["ai_verdict"] == "not_consulted"
    assert extra["ai_influenced_decision"] is False


@pytest.mark.asyncio
async def test_ai_attribution_marks_gate_influence_only_when_decision_gate_enabled():
    bot = _bare_polybot()
    _attach_mocks(bot)
    bot.config["ai"]["decision_layer"]["enabled"] = True
    sig = _bitcoin_signal(action="BUY_YES").model_copy(
        update={"ai_used": True, "reason": "ai_decision=direct"}
    )

    await bot._execute_bitcoin_signal_impl(sig)

    extra = bot.journal.log_entry.call_args.kwargs["extra"]
    assert extra["ai_enabled_at_entry"] is True
    assert extra["ai_decision_gate_enabled_at_entry"] is True
    assert extra["ai_consulted"] is True
    assert extra["ai_verdict"] == "approved"
    assert extra["ai_influenced_decision"] is True


@pytest.mark.asyncio
async def test_execute_bitcoin_blocks_second_entry_same_market_in_process():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _bitcoin_signal(action="BUY_YES")

    await bot._execute_bitcoin_signal_impl(sig)
    await bot._execute_bitcoin_signal_impl(sig)

    bot.clob_client.place_order.assert_called_once()
    assert bot.journal.log_skip.call_args.args[3] == "duplicate_session_market"


@pytest.mark.asyncio
async def test_execute_sol_macro_impl_preserves_raw_and_calibrated_probabilities():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _sol_like_signal(action="BUY_NO", strategy_name="sol_macro")
    await bot._execute_sol_macro_signal_impl(sig)
    extra = bot.journal.log_entry.call_args.kwargs["extra"]
    assert extra["raw_est_prob"] == pytest.approx(0.47)
    assert extra["est_prob"] == pytest.approx(0.42)
    assert extra["ai_enabled_at_entry"] is False
    assert extra["ai_consulted"] is False
    assert extra["ai_influenced_decision"] is False


@pytest.mark.asyncio
async def test_execute_xrp_macro_impl_buy_yes():
    bot = _bare_polybot()
    _attach_mocks(bot)
    await bot._execute_sol_macro_signal_impl(_xrp_signal(action="BUY_YES"))
    bot.clob_client.place_order.assert_called_once()
    assert bot.clob_client.place_order.call_args.kwargs["side"] == "BUY"


@pytest.mark.asyncio
async def test_execute_xrp_macro_impl_buy_no():
    bot = _bare_polybot()
    _attach_mocks(bot)
    sig = _xrp_signal(action="BUY_NO")
    await bot._execute_sol_macro_signal_impl(sig)
    bot.clob_client.place_order.assert_called_once()
    _assert_buy_no_execution(bot, token_id_no=sig.token_id_no, strategy="xrp_macro")

