"""Integration tests modeling the two-cycle strategy → broker → strategy flow.

These do not exercise a full strategy; they verify the contract a strategy
relies on: enqueue → worker resolves → next-cycle lookup succeeds OR is
invalidated for the correct reason.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.analysis.ai_decision_broker import (
    AIDecisionBroker,
    PendingDecision,
    STATE_PENDING,
    STATE_RESOLVED,
)
from tests.async_helpers import run_async


def _make_pending(yes_price=0.42, edge=0.05, action="BUY_YES", market_id="m1"):
    return PendingDecision(
        key=("bitcoin", market_id, "lane", action),
        state=STATE_PENDING,
        created_at=0.0,
        cycle_enqueued=0,
        yes_price_at_enqueue=yes_price,
        edge_sign=1 if edge >= 0 else -1,
        action=action,
        market_question="Q",
        market_description="D",
        current_yes_price=yes_price,
        edge=edge,
        confidence=0.5,
        estimated_prob=0.55,
        raw_est_prob=0.55,
        quant_threshold=0.04,
        require_shadow_portfolio=False,
    )


async def _drive_to_resolved(broker, pd, timeout=2.0):
    elapsed = 0.0
    step = 0.02
    while elapsed < timeout:
        await asyncio.sleep(step)
        entry = broker._decisions.get(pd.key)
        if entry and entry.state == STATE_RESOLVED:
            return entry
        elapsed += step
    raise AssertionError(f"never reached RESOLVED, last state={entry.state if entry else 'missing'}")


def test_two_cycle_happy_path():
    """Cycle 1: enqueue + bail. Cycle 2: read resolved → signal-ready."""
    async def scenario():
        decision = MagicMock(approved=True, action="BUY_YES", reason="ok", confidence=0.7)
        agent = MagicMock()
        agent.is_available = MagicMock(return_value=True)
        agent.evaluate_trade_decision = AsyncMock(return_value=decision)
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd = _make_pending()
            # Cycle 1: miss, enqueue.
            assert broker.get_resolved(
                pd.key, current_yes_price=pd.yes_price_at_enqueue,
                current_action=pd.action, current_edge=pd.edge,
            ) is None
            broker.enqueue(pd)
            # Worker resolves between cycles.
            await _drive_to_resolved(broker, pd)
            # Cycle 2: hit, consume.
            resolved = broker.get_resolved(
                pd.key, current_yes_price=pd.yes_price_at_enqueue,
                current_action=pd.action, current_edge=pd.edge,
            )
            assert resolved is decision
            assert pd.key not in broker._decisions
        finally:
            await broker.stop()

    run_async(scenario())


def test_two_cycle_price_drift_invalidates_between_cycles():
    """Cycle 1 enqueues at price 0.42. Cycle 2 reads at 0.55 → invalidated."""
    async def scenario():
        decision = MagicMock(approved=True, action="BUY_YES", reason="ok", confidence=0.7)
        agent = MagicMock()
        agent.is_available = MagicMock(return_value=True)
        agent.evaluate_trade_decision = AsyncMock(return_value=decision)
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd = _make_pending(yes_price=0.42)
            broker.enqueue(pd)
            await _drive_to_resolved(broker, pd)
            # Price moved 0.13.
            resolved = broker.get_resolved(
                pd.key, current_yes_price=0.55,
                current_action=pd.action, current_edge=pd.edge,
            )
            assert resolved is None
            assert pd.key not in broker._decisions
            assert broker._counters["expired"] == 1
        finally:
            await broker.stop()

    run_async(scenario())


def test_two_cycle_position_held_invalidates():
    async def scenario():
        decision = MagicMock(approved=True, action="BUY_YES", reason="ok", confidence=0.7)
        agent = MagicMock()
        agent.is_available = MagicMock(return_value=True)
        agent.evaluate_trade_decision = AsyncMock(return_value=decision)
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd = _make_pending(market_id="market_X")
            broker.enqueue(pd)
            await _drive_to_resolved(broker, pd)
            # Between cycles, position opened on market_X.
            resolved = broker.get_resolved(
                pd.key, current_yes_price=pd.yes_price_at_enqueue,
                current_action=pd.action, current_edge=pd.edge,
                open_position_ids={"market_X"},
            )
            assert resolved is None
            assert pd.key not in broker._decisions
        finally:
            await broker.stop()

    run_async(scenario())


def test_two_cycle_action_flip_invalidates():
    async def scenario():
        decision = MagicMock(approved=True, action="BUY_YES", reason="ok", confidence=0.7)
        agent = MagicMock()
        agent.is_available = MagicMock(return_value=True)
        agent.evaluate_trade_decision = AsyncMock(return_value=decision)
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd = _make_pending(action="BUY_YES")
            broker.enqueue(pd)
            await _drive_to_resolved(broker, pd)
            # Strategy now wants BUY_NO — different key entirely, so this is a
            # miss (resolved is None), not an "invalidation". Confirm.
            new_key = ("bitcoin", pd.market_id, pd.lane_id, "BUY_NO")
            resolved = broker.get_resolved(
                new_key, current_yes_price=pd.yes_price_at_enqueue,
                current_action="BUY_NO", current_edge=pd.edge,
            )
            assert resolved is None
            # The old BUY_YES decision is still in the broker, available for a
            # BUY_YES caller (but no such caller exists this cycle).
            assert pd.key in broker._decisions
        finally:
            await broker.stop()

    run_async(scenario())


def test_two_cycle_failed_decision_falls_through():
    async def scenario():
        agent = MagicMock()
        agent.is_available = MagicMock(return_value=True)
        agent.evaluate_trade_decision = AsyncMock(return_value=None)  # provider failed
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd = _make_pending()
            broker.enqueue(pd)
            for _ in range(50):
                await asyncio.sleep(0.02)
                if broker._decisions.get(pd.key) and broker._decisions[pd.key].state == "FAILED":
                    break
            resolved = broker.get_resolved(
                pd.key, current_yes_price=pd.yes_price_at_enqueue,
                current_action=pd.action, current_edge=pd.edge,
            )
            assert resolved is None
            # Strategy can now re-enqueue.
            assert broker.enqueue(_make_pending()) is True
        finally:
            await broker.stop()

    run_async(scenario())
