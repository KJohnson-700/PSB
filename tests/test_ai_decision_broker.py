"""State machine tests for AIDecisionBroker."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analysis.ai_decision_broker import (
    AIDecisionBroker,
    PendingDecision,
    STATE_CONSUMED,
    STATE_FAILED,
    STATE_INFLIGHT,
    STATE_PENDING,
    STATE_RESOLVED,
)
from tests.async_helpers import run_async


def _make_pending(
    *,
    strategy="bitcoin",
    market_id="m1",
    lane_id="btc_lane",
    action="BUY_YES",
    yes_price=0.42,
    edge=0.05,
):
    return PendingDecision(
        key=(strategy, market_id, lane_id, action),
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


def _make_decision(approved=True, action="BUY_YES", reason="ok"):
    d = MagicMock()
    d.approved = approved
    d.action = action
    d.confidence = 0.7
    d.reason = reason
    return d


def _make_agent(return_value=None, *, available=True, side_effect=None):
    agent = MagicMock()
    agent.is_available = MagicMock(return_value=available)
    if side_effect is not None:
        agent.evaluate_trade_decision = AsyncMock(side_effect=side_effect)
    else:
        agent.evaluate_trade_decision = AsyncMock(return_value=return_value)
    return agent


def test_enqueue_and_resolve_happy_path():
    async def scenario():
        agent = _make_agent(return_value=_make_decision(approved=True))
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd = _make_pending()
            assert broker.enqueue(pd) is True
            # Worker drains.
            for _ in range(50):
                await asyncio.sleep(0.02)
                if broker._decisions[pd.key].state == STATE_RESOLVED:
                    break
            assert broker._decisions[pd.key].state == STATE_RESOLVED
            decision = broker.get_resolved(
                pd.key,
                current_yes_price=pd.yes_price_at_enqueue,
                current_action=pd.action,
                current_edge=pd.edge,
            )
            assert decision is not None
            assert decision.approved is True
            # Consumed → removed from store.
            assert pd.key not in broker._decisions
        finally:
            await broker.stop()

    run_async(scenario())


def test_enqueue_idempotent_while_pending_or_inflight():
    async def scenario():
        # Slow agent so we can observe IN_FLIGHT.
        async def slow_decision(**_):
            await asyncio.sleep(0.5)
            return _make_decision()

        agent = _make_agent(side_effect=slow_decision)
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd = _make_pending()
            assert broker.enqueue(pd) is True
            # Second enqueue while PENDING/IN_FLIGHT must skip.
            pd2 = _make_pending()
            assert broker.enqueue(pd2) is False
            assert broker._counters["duplicate_enqueue_skipped"] == 1
        finally:
            await broker.stop()

    run_async(scenario())


def test_failed_state_when_agent_returns_none():
    async def scenario():
        agent = _make_agent(return_value=None)
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd = _make_pending()
            broker.enqueue(pd)
            for _ in range(50):
                await asyncio.sleep(0.02)
                if broker._decisions.get(pd.key, pd).state == STATE_FAILED:
                    break
            assert broker._decisions[pd.key].state == STATE_FAILED
            # get_resolved on FAILED returns None and removes the entry.
            result = broker.get_resolved(
                pd.key,
                current_yes_price=pd.yes_price_at_enqueue,
                current_action=pd.action,
                current_edge=pd.edge,
            )
            assert result is None
            assert pd.key not in broker._decisions
        finally:
            await broker.stop()

    run_async(scenario())


def test_exception_in_agent_marks_failed():
    async def scenario():
        agent = _make_agent(side_effect=RuntimeError("boom"))
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd = _make_pending()
            broker.enqueue(pd)
            for _ in range(50):
                await asyncio.sleep(0.02)
                if broker._decisions.get(pd.key, pd).state == STATE_FAILED:
                    break
            entry = broker._decisions[pd.key]
            assert entry.state == STATE_FAILED
            assert entry.error and "RuntimeError" in entry.error
        finally:
            await broker.stop()

    run_async(scenario())


def test_inflight_state_is_observable():
    async def scenario():
        gate = asyncio.Event()

        async def held(**_):
            await gate.wait()
            return _make_decision()

        agent = _make_agent(side_effect=held)
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd = _make_pending()
            broker.enqueue(pd)
            for _ in range(50):
                await asyncio.sleep(0.02)
                if broker._decisions[pd.key].state == STATE_INFLIGHT:
                    break
            assert broker._decisions[pd.key].state == STATE_INFLIGHT
            gate.set()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if broker._decisions[pd.key].state == STATE_RESOLVED:
                    break
            assert broker._decisions[pd.key].state == STATE_RESOLVED
        finally:
            await broker.stop()

    run_async(scenario())


def test_worker_supervisor_recovers_from_one_bad_payload():
    async def scenario():
        # Two enqueues — first throws, second succeeds.
        results = iter([RuntimeError("first"), _make_decision()])

        async def maybe_raise(**_):
            v = next(results)
            if isinstance(v, Exception):
                raise v
            return v

        agent = _make_agent(side_effect=maybe_raise)
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd1 = _make_pending(market_id="m1")
            pd2 = _make_pending(market_id="m2")
            broker.enqueue(pd1)
            broker.enqueue(pd2)
            for _ in range(100):
                await asyncio.sleep(0.02)
                s1 = broker._decisions.get(pd1.key, pd1).state
                s2 = broker._decisions.get(pd2.key, pd2).state
                if s1 in (STATE_FAILED,) and s2 == STATE_RESOLVED:
                    break
            assert broker._decisions[pd1.key].state == STATE_FAILED
            assert broker._decisions[pd2.key].state == STATE_RESOLVED
        finally:
            await broker.stop()

    run_async(scenario())


def test_stats_shape():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    s = broker.stats()
    for k in ("pending", "inflight", "consumed", "expired", "failed",
              "rejected_overflow", "oldest_age_sec", "worker_alive"):
        assert k in s


def test_reset_clears_state():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    pd = _make_pending()
    broker._decisions[pd.key] = pd
    broker._queue.append(pd.key)
    broker.reset()
    assert broker._decisions == {}
    assert len(broker._queue) == 0
