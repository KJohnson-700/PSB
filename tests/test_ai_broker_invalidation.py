"""Invalidation rule tests for AIDecisionBroker.get_resolved."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

from src.analysis.ai_decision_broker import (
    AIDecisionBroker,
    PendingDecision,
    STATE_PENDING,
    STATE_RESOLVED,
    STATE_FAILED,
)


def _resolved_pd(broker, *, yes_price=0.42, edge=0.05, action="BUY_YES",
                 strategy="bitcoin", market_id="m1", lane_id="lane",
                 age_sec=0.0, approved=True):
    key = (strategy, market_id, lane_id, action)
    decision = MagicMock(approved=approved, action=action, reason="ok", confidence=0.7)
    pd = PendingDecision(
        key=key,
        state=STATE_RESOLVED,
        created_at=time.time() - age_sec,
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
        ai_decision=decision,
    )
    broker._decisions[key] = pd
    return key, pd


def test_not_present_returns_none():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    out = broker.get_resolved(
        ("strat", "nope", "lane", "BUY_YES"),
        current_yes_price=0.5, current_action="BUY_YES", current_edge=0.1,
    )
    assert out is None


def test_failed_state_returns_none_and_removes():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    key, pd = _resolved_pd(broker)
    pd.state = STATE_FAILED
    pd.ai_decision = None
    out = broker.get_resolved(
        key, current_yes_price=pd.yes_price_at_enqueue,
        current_action=pd.action, current_edge=pd.edge,
    )
    assert out is None
    assert key not in broker._decisions


def test_pending_returns_none_without_removing():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    key, pd = _resolved_pd(broker)
    pd.state = STATE_PENDING
    out = broker.get_resolved(
        key, current_yes_price=pd.yes_price_at_enqueue,
        current_action=pd.action, current_edge=pd.edge,
    )
    assert out is None
    assert key in broker._decisions  # still pending; do not remove


def test_age_expiry():
    broker = AIDecisionBroker(ai_agent=MagicMock(), max_decision_age_sec=10.0, log_jsonl=False)
    key, pd = _resolved_pd(broker, age_sec=20.0)
    out = broker.get_resolved(
        key, current_yes_price=pd.yes_price_at_enqueue,
        current_action=pd.action, current_edge=pd.edge,
    )
    assert out is None
    assert key not in broker._decisions
    assert broker._counters["expired"] == 1


def test_price_drift_invalidates():
    broker = AIDecisionBroker(ai_agent=MagicMock(), price_drift_threshold=0.03, log_jsonl=False)
    key, pd = _resolved_pd(broker, yes_price=0.42)
    out = broker.get_resolved(
        key, current_yes_price=0.50,  # moved 0.08 > 0.03
        current_action=pd.action, current_edge=pd.edge,
    )
    assert out is None
    assert key not in broker._decisions


def test_price_drift_within_threshold_is_ok():
    broker = AIDecisionBroker(ai_agent=MagicMock(), price_drift_threshold=0.03, log_jsonl=False)
    key, pd = _resolved_pd(broker, yes_price=0.42)
    out = broker.get_resolved(
        key, current_yes_price=0.44,  # +0.02 < 0.03
        current_action=pd.action, current_edge=pd.edge,
    )
    assert out is not None
    assert out.approved is True


def test_action_flip_invalidates():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    key, pd = _resolved_pd(broker, action="BUY_YES")
    out = broker.get_resolved(
        key, current_yes_price=pd.yes_price_at_enqueue,
        current_action="BUY_NO", current_edge=pd.edge,
    )
    assert out is None


def test_edge_sign_flip_invalidates():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    key, pd = _resolved_pd(broker, edge=0.05)
    out = broker.get_resolved(
        key, current_yes_price=pd.yes_price_at_enqueue,
        current_action=pd.action, current_edge=-0.01,
    )
    assert out is None


def test_position_held_invalidates():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    key, pd = _resolved_pd(broker, market_id="held_market")
    out = broker.get_resolved(
        key, current_yes_price=pd.yes_price_at_enqueue,
        current_action=pd.action, current_edge=pd.edge,
        open_position_ids={"held_market"},
    )
    assert out is None


def test_market_closed_invalidates():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    key, pd = _resolved_pd(broker)
    out = broker.get_resolved(
        key, current_yes_price=pd.yes_price_at_enqueue,
        current_action=pd.action, current_edge=pd.edge,
        market_closed=True,
    )
    assert out is None


def test_rule_precedence_failed_before_age():
    """A FAILED state must short-circuit before age check."""
    broker = AIDecisionBroker(ai_agent=MagicMock(), max_decision_age_sec=10.0, log_jsonl=False)
    key, pd = _resolved_pd(broker, age_sec=20.0)
    pd.state = STATE_FAILED
    pd.ai_decision = None
    out = broker.get_resolved(
        key, current_yes_price=pd.yes_price_at_enqueue,
        current_action=pd.action, current_edge=pd.edge,
    )
    assert out is None
    # Counter not incremented as 'expired' — failed path is separate.
    assert broker._counters["expired"] == 0


def test_rule_precedence_age_before_price_drift():
    broker = AIDecisionBroker(
        ai_agent=MagicMock(), max_decision_age_sec=10.0,
        price_drift_threshold=0.03, log_jsonl=False,
    )
    key, pd = _resolved_pd(broker, yes_price=0.42, age_sec=20.0)
    # Both age and price drift would expire; age fires first.
    out = broker.get_resolved(
        key, current_yes_price=0.99, current_action=pd.action, current_edge=pd.edge,
    )
    assert out is None
    assert broker._counters["expired"] == 1


def test_sweep_expired_drops_stale_resolved():
    broker = AIDecisionBroker(ai_agent=MagicMock(), max_decision_age_sec=10.0, log_jsonl=False)
    key, pd = _resolved_pd(broker, age_sec=20.0)
    broker.sweep_expired()
    assert key not in broker._decisions


def test_sweep_expired_marks_position_held_pending():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    key, pd = _resolved_pd(broker, market_id="held")
    pd.state = STATE_PENDING
    broker.sweep_expired(open_position_ids={"held"})
    assert key not in broker._decisions


def test_consumed_counter_increments():
    broker = AIDecisionBroker(ai_agent=MagicMock(), log_jsonl=False)
    key, pd = _resolved_pd(broker)
    broker.get_resolved(
        key, current_yes_price=pd.yes_price_at_enqueue,
        current_action=pd.action, current_edge=pd.edge,
    )
    assert broker._counters["consumed"] == 1
