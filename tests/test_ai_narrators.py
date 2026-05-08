"""Unit tests for AI narrators — verify gating, joining, conflict detection."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analysis.ai_narrators import (
    _bucket_calibration,
    _detect_conflicts,
    _index_closed_trades_by_market,
    _join_shadow_with_outcomes,
    detect_calibration_drift,
    explain_strategy_conflict,
    summarize_skip_exit_reasons,
    summarize_underperformance,
)


def _stub_agent(reasoning: str = "narrative output") -> Any:
    agent = MagicMock()
    agent.is_available = MagicMock(return_value=True)
    fake_resp = MagicMock()
    fake_resp.reasoning = reasoning
    fake_resp.confidence_score = 0.7
    agent.analyze_market = AsyncMock(return_value=fake_resp)
    return agent


def _disabled_agent() -> Any:
    agent = MagicMock()
    agent.is_available = MagicMock(return_value=False)
    agent.analyze_market = AsyncMock(return_value=None)
    return agent


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if (
        hasattr(asyncio, "get_event_loop") and not asyncio.get_event_loop().is_closed()
    ) else asyncio.run(coro)


# ── underperformance ──────────────────────────────────────────────────────


def test_underperformance_returns_empty_on_empty_report() -> None:
    result = asyncio.run(summarize_underperformance({}, _stub_agent()))
    assert result == ""


def test_underperformance_returns_empty_when_agent_unavailable() -> None:
    result = asyncio.run(
        summarize_underperformance({"some": "data"}, _disabled_agent())
    )
    assert result == ""


def test_underperformance_returns_reasoning_when_available() -> None:
    agent = _stub_agent("BTC 5m regressed; consider raising RSI floor.")
    result = asyncio.run(
        summarize_underperformance({"strategy": "btc", "metric": -0.1}, agent)
    )
    assert "regressed" in result.lower()
    agent.analyze_market.assert_awaited_once()


# ── skip/exit summarizer ──────────────────────────────────────────────────


def test_skip_exit_summarizer_skips_when_below_threshold() -> None:
    out = asyncio.run(
        summarize_skip_exit_reasons({}, {}, _stub_agent(), total_skips_threshold=1)
    )
    assert out == ""


def test_skip_exit_summarizer_calls_ai_when_data_present() -> None:
    agent = _stub_agent("rsi_extreme_block dominates")
    out = asyncio.run(
        summarize_skip_exit_reasons(
            {"rsi_extreme_block": 30, "edge_below_min": 5},
            {"updown_time_stop": 10},
            agent,
        )
    )
    assert "dominates" in out
    agent.analyze_market.assert_awaited_once()


# ── calibration drift ─────────────────────────────────────────────────────


def test_index_closed_trades_aggregates_pnl_and_wins_losses() -> None:
    trades = [
        {"market_id": "m1", "pnl": 5.0},
        {"market_id": "m1", "pnl": -2.0},
        {"market_id": "m2", "pnl": 0.0},
    ]
    idx = _index_closed_trades_by_market(trades)
    assert idx["m1"]["wins"] == 1 and idx["m1"]["losses"] == 1
    assert abs(idx["m1"]["pnl"] - 3.0) < 1e-9
    assert idx["m2"]["wins"] == 0 and idx["m2"]["losses"] == 0


def test_join_shadow_with_outcomes_filters_unmatched_market_ids() -> None:
    shadow = [
        {"market_id": "m1", "confidence_score": 0.8},
        {"market_id": "m999", "confidence_score": 0.5},
    ]
    closed_idx = {"m1": {"wins": 1, "losses": 0, "pnl": 1.0}}
    joined = _join_shadow_with_outcomes(shadow, closed_idx)
    assert len(joined) == 1
    assert joined[0]["market_id"] == "m1"


def test_bucket_calibration_groups_by_confidence_band() -> None:
    joined = [
        {"ai_confidence": 0.9, "outcome": {"wins": 3, "losses": 1, "pnl": 5.0}},
        {"ai_confidence": 0.5, "outcome": {"wins": 1, "losses": 1, "pnl": 0.0}},
        {"ai_confidence": 0.2, "outcome": {"wins": 0, "losses": 2, "pnl": -3.0}},
    ]
    buckets = _bucket_calibration(joined)
    assert buckets["high (0.7–1.0)"]["n_trades"] == 4
    assert buckets["mid (0.4–0.7)"]["n_trades"] == 2
    assert buckets["low (0.0–0.4)"]["n_trades"] == 2


def test_calibration_drift_skips_when_too_few_paired_records() -> None:
    out = asyncio.run(
        detect_calibration_drift(
            shadow_records=[{"market_id": "m1", "confidence_score": 0.5}],
            closed_trades=[{"market_id": "m1", "pnl": 1.0}],
            ai_agent=_stub_agent(),
            min_paired_records=5,
        )
    )
    assert out == ""


def test_calibration_drift_calls_ai_when_enough_paired_records() -> None:
    shadow = [{"market_id": f"m{i}", "confidence_score": 0.7} for i in range(6)]
    closed = [{"market_id": f"m{i}", "pnl": 1.0 if i % 2 == 0 else -1.0} for i in range(6)]
    agent = _stub_agent("AI is over-confident in mid bucket.")
    out = asyncio.run(detect_calibration_drift(shadow, closed, agent, min_paired_records=5))
    assert "over-confident" in out
    agent.analyze_market.assert_awaited_once()


# ── strategy conflict ─────────────────────────────────────────────────────


def test_detect_conflicts_finds_bullish_vs_bearish_pair() -> None:
    summaries = {
        "bitcoin": {"htf_bias": "BULL"},
        "eth_macro": {"htf_bias": "BEAR"},
        "sol_macro": {"htf_bias": "BULL"},
    }
    conflicts = _detect_conflicts(summaries)
    assert any("bitcoin" in c and "eth_macro" in c for c in conflicts)


def test_detect_conflicts_returns_empty_when_all_agree() -> None:
    summaries = {"bitcoin": {"htf_bias": "BULL"}, "eth_macro": {"htf_bias": "BULL"}}
    assert _detect_conflicts(summaries) == []


def test_explain_strategy_conflict_skips_when_no_conflicts() -> None:
    out = asyncio.run(
        explain_strategy_conflict(
            {"bitcoin": {"htf_bias": "BULL"}, "eth": {"htf_bias": "BULL"}},
            _stub_agent(),
        )
    )
    assert out == ""


def test_explain_strategy_conflict_calls_ai_when_conflicts_found() -> None:
    agent = _stub_agent("BTC bullish disagrees with ETH bearish; trust longer-timeframe.")
    out = asyncio.run(
        explain_strategy_conflict(
            {"bitcoin": {"htf_bias": "BULL"}, "eth_macro": {"htf_bias": "BEAR"}},
            agent,
        )
    )
    assert "disagrees" in out
    agent.analyze_market.assert_awaited_once()
