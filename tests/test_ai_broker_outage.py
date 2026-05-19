"""Outage / degraded-mode tests for AIDecisionBroker."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.analysis.ai_decision_broker import (
    AIDecisionBroker,
    PendingDecision,
    STATE_FAILED,
    STATE_PENDING,
)
from tests.async_helpers import run_async


def _make_pending():
    return PendingDecision(
        key=("bitcoin", "m1", "lane", "BUY_YES"),
        state=STATE_PENDING,
        created_at=0.0,
        cycle_enqueued=0,
        yes_price_at_enqueue=0.42,
        edge_sign=1,
        action="BUY_YES",
        market_question="Q",
        market_description="D",
        current_yes_price=0.42,
        edge=0.05,
        confidence=0.5,
        estimated_prob=0.55,
        raw_est_prob=0.55,
        quant_threshold=0.04,
        require_shadow_portfolio=False,
    )


def test_ai_unavailable_marks_failed_without_calling_agent():
    async def scenario():
        agent = MagicMock()
        agent.is_available = MagicMock(return_value=False)
        agent.evaluate_trade_decision = AsyncMock(return_value=None)
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            broker.enqueue(_make_pending())
            for _ in range(50):
                await asyncio.sleep(0.02)
                entry = list(broker._decisions.values())[0]
                if entry.state == STATE_FAILED:
                    break
            entry = list(broker._decisions.values())[0]
            assert entry.state == STATE_FAILED
            assert entry.error == "ai_unavailable"
            # Provider was never asked because circuit was open.
            agent.evaluate_trade_decision.assert_not_called()
        finally:
            await broker.stop()

    run_async(scenario())


def test_provider_exception_marks_failed_and_worker_continues():
    async def scenario():
        agent = MagicMock()
        agent.is_available = MagicMock(return_value=True)

        call_count = {"n": 0}

        async def flaky(**_):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise asyncio.TimeoutError("provider stuck")
            d = MagicMock(approved=True, action="BUY_YES", reason="ok", confidence=0.7)
            return d

        agent.evaluate_trade_decision = AsyncMock(side_effect=flaky)
        broker = AIDecisionBroker(ai_agent=agent, log_jsonl=False)
        await broker.start()
        try:
            pd1 = _make_pending()
            pd2 = PendingDecision(**{**pd1.__dict__, "key": ("bitcoin", "m2", "lane", "BUY_YES")})
            broker.enqueue(pd1)
            broker.enqueue(pd2)
            for _ in range(100):
                await asyncio.sleep(0.02)
                s1 = broker._decisions.get(pd1.key)
                s2 = broker._decisions.get(pd2.key)
                if s1 and s1.state == STATE_FAILED and s2 and s2.state == "RESOLVED":
                    break
            assert broker._decisions[pd1.key].state == STATE_FAILED
            assert broker._decisions[pd2.key].state == "RESOLVED"
        finally:
            await broker.stop()

    run_async(scenario())
