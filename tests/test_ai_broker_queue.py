"""Queue cap and overflow tests for AIDecisionBroker."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.analysis.ai_decision_broker import (
    AIDecisionBroker,
    PendingDecision,
    STATE_PENDING,
    STATE_INFLIGHT,
)


def _make_pending(market_id):
    return PendingDecision(
        key=("bitcoin", market_id, "lane", "BUY_YES"),
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


def test_enqueue_below_cap():
    broker = AIDecisionBroker(ai_agent=MagicMock(), max_pending_decisions=5, log_jsonl=False)
    for i in range(3):
        assert broker.enqueue(_make_pending(f"m{i}")) is True
    assert len(broker._decisions) == 3


def test_overflow_evicts_oldest_pending():
    broker = AIDecisionBroker(ai_agent=MagicMock(), max_pending_decisions=3, log_jsonl=False)
    for i in range(3):
        broker.enqueue(_make_pending(f"m{i}"))
    # 4th enqueue evicts m0.
    broker.enqueue(_make_pending("m_new"))
    assert ("bitcoin", "m0", "lane", "BUY_YES") not in broker._decisions
    assert ("bitcoin", "m_new", "lane", "BUY_YES") in broker._decisions
    assert broker._counters["rejected_overflow"] == 1


def test_overflow_never_evicts_inflight():
    broker = AIDecisionBroker(ai_agent=MagicMock(), max_pending_decisions=2, log_jsonl=False)
    broker.enqueue(_make_pending("m0"))
    # Manually mark m0 IN_FLIGHT (would normally be done by worker).
    broker._decisions[("bitcoin", "m0", "lane", "BUY_YES")].state = STATE_INFLIGHT
    broker.enqueue(_make_pending("m1"))
    broker.enqueue(_make_pending("m2"))  # over cap; should evict m1, not m0
    assert ("bitcoin", "m0", "lane", "BUY_YES") in broker._decisions
    assert ("bitcoin", "m1", "lane", "BUY_YES") not in broker._decisions
    assert ("bitcoin", "m2", "lane", "BUY_YES") in broker._decisions


def test_duplicate_enqueue_skipped():
    broker = AIDecisionBroker(ai_agent=MagicMock(), max_pending_decisions=10, log_jsonl=False)
    broker.enqueue(_make_pending("m0"))
    # Same key → no-op.
    assert broker.enqueue(_make_pending("m0")) is False
    assert broker._counters["duplicate_enqueue_skipped"] == 1
    assert broker._counters["enqueued"] == 1


def test_overflow_when_all_inflight_accepts_new():
    """If everything is IN_FLIGHT and nothing can be evicted, accept the new
    entry rather than silently drop it (worker will drain quickly)."""
    broker = AIDecisionBroker(ai_agent=MagicMock(), max_pending_decisions=2, log_jsonl=False)
    broker.enqueue(_make_pending("m0"))
    broker.enqueue(_make_pending("m1"))
    for k in list(broker._decisions.keys()):
        broker._decisions[k].state = STATE_INFLIGHT
    # No PENDING to evict; new entry still added.
    assert broker.enqueue(_make_pending("m2")) is True
    assert ("bitcoin", "m2", "lane", "BUY_YES") in broker._decisions
