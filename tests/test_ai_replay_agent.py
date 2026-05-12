"""Tests for the deterministic AI replay agent."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.analysis import ai_call_log
from src.analysis.ai_replay_agent import AIReplayAgent


def _seed_log(tmp_path: Path, records: list[dict]) -> None:
    """Write several records via the live append_record API."""
    now = datetime(2026, 5, 12, 4, 30, tzinfo=timezone.utc)
    for rec in records:
        ai_call_log.append_record(log_dir=tmp_path, now=now, **rec)


def _base(**overrides) -> dict:
    base = dict(
        market_question="BTC Up or Down - 7:35PM-7:40PM ET",
        market_id="M1",
        strategy_hint="bitcoin",
        quant_action="BUY_YES",
        quant_edge=0.115,
        quant_confidence=0.69,
        quant_threshold=0.08,
        approved=True,
        ai_action="BUY_YES",
        ai_confidence=0.74,
        ai_estimated_probability=0.62,
        ai_edge=0.11,
        ai_reason="direct_ai_approved",
        ai_source="direct",
    )
    base.update(overrides)
    return base


def test_load_indexes_all_records(tmp_path):
    _seed_log(tmp_path, [_base(market_id="A"), _base(market_id="B"), _base(market_id="C")])
    agent = AIReplayAgent(tmp_path).load()
    assert agent.records_loaded == 3


def test_load_skips_corrupt_lines(tmp_path):
    _seed_log(tmp_path, [_base(market_id="A")])
    (tmp_path / "2026-05-12.jsonl").open("a").write("not json\n")
    agent = AIReplayAgent(tmp_path).load()
    assert agent.records_loaded == 1


def test_lookup_hit_by_hash(tmp_path):
    _seed_log(tmp_path, [_base(market_id="M1")])
    agent = AIReplayAgent(tmp_path).load()
    rec = agent.lookup(
        market_question="BTC Up or Down - 7:35PM-7:40PM ET",
        market_id="M1",
        strategy_hint="bitcoin",
        quant_action="BUY_YES",
        quant_edge=0.115,
        quant_confidence=0.69,
    )
    assert rec is not None
    assert rec.ai_action == "BUY_YES"
    assert agent.stats.hits_by_hash == 1


def test_lookup_falls_back_on_edge_drift(tmp_path):
    """Edge=0.115 recorded; replay asks edge=0.130 — exact-hash misses but
    fallback by (market_id, strategy_hint, action) hits."""
    _seed_log(tmp_path, [_base(market_id="M1", quant_edge=0.115, quant_confidence=0.69)])
    agent = AIReplayAgent(tmp_path).load()
    rec = agent.lookup(
        market_question="BTC Up or Down - 7:35PM-7:40PM ET",
        market_id="M1",
        strategy_hint="bitcoin",
        quant_action="BUY_YES",
        quant_edge=0.130,
        quant_confidence=0.71,
    )
    assert rec is not None
    assert agent.stats.hits_by_fallback == 1
    assert agent.stats.hits_by_hash == 0


def test_lookup_miss_when_market_unknown(tmp_path):
    _seed_log(tmp_path, [_base(market_id="M1")])
    agent = AIReplayAgent(tmp_path).load()
    rec = agent.lookup(
        market_question="?",
        market_id="UNKNOWN",
        strategy_hint="bitcoin",
        quant_action="BUY_YES",
        quant_edge=0.10,
        quant_confidence=0.65,
    )
    assert rec is None
    assert agent.stats.misses == 1


def test_evaluate_returns_replayed_decision(tmp_path):
    _seed_log(tmp_path, [_base(market_id="M1", approved=True, ai_action="BUY_YES",
                                ai_confidence=0.74, ai_edge=0.11)])
    agent = AIReplayAgent(tmp_path).load()
    decision = asyncio.run(agent.evaluate_trade_decision(
        market_question="BTC Up or Down - 7:35PM-7:40PM ET",
        market_description="",
        current_yes_price=0.475,
        market_id="M1",
        strategy_hint="bitcoin",
        quant_action="BUY_YES",
        quant_edge=0.115,
        quant_confidence=0.69,
        quant_threshold=0.08,
    ))
    assert decision.approved is True
    assert decision.action == "BUY_YES"
    assert decision.confidence == 0.74
    assert decision.edge == 0.11
    assert decision.reason.startswith("replay:")
    assert decision.source.startswith("replay:")


def test_evaluate_replay_miss_returns_skip(tmp_path):
    """Backtest can distinguish replay miss from a recorded SKIP via source."""
    agent = AIReplayAgent(tmp_path).load()
    decision = asyncio.run(agent.evaluate_trade_decision(
        market_question="?",
        market_description="",
        current_yes_price=0.5,
        market_id="UNKNOWN",
        strategy_hint="bitcoin",
        quant_action="BUY_YES",
        quant_edge=0.10,
        quant_confidence=0.65,
        quant_threshold=0.08,
    ))
    assert decision.approved is False
    assert decision.action == "SKIP"
    assert decision.reason == "replay_miss"
    assert decision.source == "replay_miss"


def test_evaluate_replays_rejection(tmp_path):
    _seed_log(tmp_path, [_base(
        market_id="M_HOLD",
        approved=False,
        ai_action="HOLD",
        ai_confidence=0.55,
        ai_edge=None,
        ai_reason="direct_ai_hold",
    )])
    agent = AIReplayAgent(tmp_path).load()
    decision = asyncio.run(agent.evaluate_trade_decision(
        market_question="BTC Up or Down - 7:35PM-7:40PM ET",
        market_description="",
        current_yes_price=0.5,
        market_id="M_HOLD",
        strategy_hint="bitcoin",
        quant_action="BUY_YES",
        quant_edge=0.115,
        quant_confidence=0.69,
        quant_threshold=0.08,
    ))
    assert decision.approved is False
    assert decision.action == "HOLD"
    assert decision.reason == "replay:direct_ai_hold"
    assert decision.edge is None


def test_load_specific_days_only(tmp_path):
    """`load(days=...)` reads only the specified date files."""
    ai_call_log.append_record(log_dir=tmp_path,
                              now=datetime(2026, 5, 10, 12, tzinfo=timezone.utc),
                              **_base(market_id="OLD"))
    ai_call_log.append_record(log_dir=tmp_path,
                              now=datetime(2026, 5, 12, 12, tzinfo=timezone.utc),
                              **_base(market_id="NEW"))
    agent = AIReplayAgent(tmp_path).load(days=["2026-05-12"])
    assert agent.records_loaded == 1
    rec = agent.lookup(
        market_question="BTC Up or Down - 7:35PM-7:40PM ET",
        market_id="NEW", strategy_hint="bitcoin", quant_action="BUY_YES",
        quant_edge=0.115, quant_confidence=0.69,
    )
    assert rec is not None
