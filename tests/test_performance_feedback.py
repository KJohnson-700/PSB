"""Tests for drift-driven runtime performance feedback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.execution.live_testing import DriftReport
from src.execution.performance_feedback import (
    get_loosen_min_edge_mult,
    get_drift_kelly_mult,
    get_drift_min_edge_mult,
    public_feedback_status,
    refresh_performance_feedback,
)


def test_get_drift_min_edge_mult_disabled():
    cfg = {
        "performance_feedback": {"enabled": False},
        "_runtime_feedback": {
            "enabled": True,
            "by_strategy": {"bitcoin": {"min_edge_mult": 1.12}},
        },
    }
    assert get_drift_min_edge_mult("bitcoin", cfg) == 1.0


def test_get_drift_min_edge_mult_from_runtime():
    cfg = {
        "performance_feedback": {"enabled": True},
        "_runtime_feedback": {
            "enabled": True,
            "by_strategy": {"bitcoin": {"min_edge_mult": 1.12}},
        },
    }
    assert get_drift_min_edge_mult("bitcoin", cfg) == pytest.approx(1.12)
    assert get_drift_min_edge_mult("sol_macro", cfg) == 1.0


def test_get_drift_kelly_mult_when_diverging():
    cfg = {
        "performance_feedback": {"enabled": True},
        "_runtime_feedback": {
            "enabled": True,
            "by_strategy": {"eth_macro": {"kelly_mult": 0.88}},
        },
    }
    assert get_drift_kelly_mult("eth_macro", cfg) == pytest.approx(0.88)


def test_get_loosen_min_edge_mult_from_runtime():
    cfg = {
        "performance_feedback": {"enabled": True},
        "_runtime_feedback": {
            "enabled": True,
            "by_lane": {
                "bitcoin|1h|down|bearish": {"min_edge_mult": 0.72},
            },
        },
    }
    assert (
        get_loosen_min_edge_mult(
            "bitcoin",
            cfg,
            window="1h",
            side="down",
            regime="BEARISH",
        )
        == pytest.approx(0.72)
    )
    assert (
        get_loosen_min_edge_mult(
            "bitcoin",
            cfg,
            window="15m",
            side="up",
            regime="BULLISH",
        )
        == pytest.approx(1.0)
    )


def test_get_loosen_min_edge_mult_supports_new_alt_assets():
    cfg = {
        "performance_feedback": {"enabled": True},
        "_runtime_feedback": {
            "enabled": True,
            "by_lane": {
                "doge_macro|5m|up|bullish": {"min_edge_mult": 0.83},
                "bnb_macro|15m|down|bearish": {"min_edge_mult": 0.79},
            },
        },
    }
    assert (
        get_loosen_min_edge_mult(
            "doge_macro",
            cfg,
            window="5m",
            side="up",
            regime="BULLISH",
        )
        == pytest.approx(0.83)
    )
    assert (
        get_loosen_min_edge_mult(
            "bnb_macro",
            cfg,
            window="15m",
            side="down",
            regime="BEARISH",
        )
        == pytest.approx(0.79)
    )


@patch("src.execution.performance_feedback.PerformanceTracker")
def test_refresh_clamps_min_edge_mult(mock_pt):
    mock_inst = MagicMock()
    mock_pt.return_value = mock_inst
    mock_inst.check_drift.return_value = [
        DriftReport(
            strategy="bitcoin",
            is_diverging=True,
            verdict="DIVERGING: test",
            live_sample_size=20,
        )
    ]
    cfg = {
        "performance_feedback": {
            "enabled": True,
            "min_live_sample": 15,
            "diverge_min_edge_mult": 2.0,
            "min_min_edge_mult": 1.0,
            "max_min_edge_mult": 1.15,
            "kelly_mult_when_diverging": 0.9,
            "kelly_mult_min": 0.5,
            "kelly_mult_max": 1.0,
        }
    }
    with patch(
        "src.execution.performance_feedback.load_backtest_expectations",
        return_value={"bitcoin": {"win_rate": 0.5, "avg_edge": 0.1, "trades_per_day": 1}},
    ):
        refresh_performance_feedback(cfg)
    row = cfg["_runtime_feedback"]["by_strategy"]["bitcoin"]
    assert row["min_edge_mult"] == pytest.approx(1.15)
    assert row["kelly_mult"] == pytest.approx(0.9)


@patch("src.execution.performance_feedback.PerformanceTracker")
def test_refresh_empty_expectations(mock_pt):
    cfg = {"performance_feedback": {"enabled": True}}
    with patch(
        "src.execution.performance_feedback.load_backtest_expectations",
        return_value={},
    ):
        refresh_performance_feedback(cfg)
    mock_pt.return_value.check_drift.assert_not_called()
    assert cfg["_runtime_feedback"].get("expectations_empty") is True


@patch("src.execution.performance_feedback.PerformanceTracker")
def test_refresh_adds_overtight_lane_feedback(mock_pt, tmp_path):
    settled_path = tmp_path / "rejected_candidates_settled.jsonl"
    settled_path.write_text(
        "\n".join(
            [
                (
                    '{"lane_id":"bitcoin|1h|down|bearish|rejected","strategy":"bitcoin",'
                    '"window":"1h","reason":"lane_min_edge","win":true,"realized_pct":0.7,'
                    '"context":{"edge":0.01,"effective_min_edge":0.10}}'
                ),
                (
                    '{"lane_id":"bitcoin|1h|down|bearish|rejected","strategy":"bitcoin",'
                    '"window":"1h","reason":"lane_min_edge","win":true,"realized_pct":0.5,'
                    '"context":{"edge":0.02,"effective_min_edge":0.10}}'
                ),
                (
                    '{"lane_id":"bitcoin|1h|down|bearish|rejected","strategy":"bitcoin",'
                    '"window":"1h","reason":"lane_min_edge","win":true,"realized_pct":0.3,'
                    '"context":{"edge":0.03,"effective_min_edge":0.10}}'
                ),
                (
                    '{"lane_id":"bitcoin|1h|down|bearish|rejected","strategy":"bitcoin",'
                    '"window":"1h","reason":"lane_min_edge","win":true,"realized_pct":0.1,'
                    '"context":{"edge":0.04,"effective_min_edge":0.10}}'
                ),
                (
                    '{"lane_id":"bitcoin|1h|down|bearish|rejected","strategy":"bitcoin",'
                    '"window":"1h","reason":"lane_min_edge","win":false,"realized_pct":-1.0,'
                    '"context":{"edge":0.08,"effective_min_edge":0.10}}'
                ) + "\n"
            ]
        ),
        encoding="utf-8",
    )
    cfg = {
        "performance_feedback": {
            "enabled": True,
            "overtight_min_lane_sample": 5,
            "overtight_min_pass_sample": 4,
            "overtight_ghost_wr_threshold": 0.55,
            "overtight_max_relax_delta": 0.08,
            "overtight_min_edge_mult_floor": 0.70,
        }
    }
    with patch(
        "src.execution.performance_feedback.load_backtest_expectations",
        return_value={},
    ):
        refresh_performance_feedback(cfg, settled_path=settled_path)
    mock_pt.return_value.check_drift.assert_not_called()
    lane_row = cfg["_runtime_feedback"]["by_lane"]["bitcoin|1h|down|bearish"]
    assert lane_row["ghost_win_rate"] == pytest.approx(0.8)
    assert lane_row["recommended_relax_delta"] == pytest.approx(0.08)
    assert lane_row["min_edge_mult"] == pytest.approx(0.70)
    assert cfg["_runtime_feedback"]["overtight_count"] == 1


def test_public_feedback_status_exposes_preview_when_disabled(tmp_path):
    settled_path = tmp_path / "rejected_candidates_settled.jsonl"
    settled_path.write_text(
        "\n".join(
            [
                (
                    '{"lane_id":"bitcoin|1h|down|bearish|rejected","strategy":"bitcoin",'
                    '"window":"1h","reason":"lane_min_edge","win":true,"realized_pct":0.7,'
                    '"context":{"edge":0.08,"effective_min_edge":0.10}}'
                ),
                (
                    '{"lane_id":"bitcoin|1h|down|bearish|rejected","strategy":"bitcoin",'
                    '"window":"1h","reason":"lane_min_edge","win":true,"realized_pct":0.5,'
                    '"context":{"edge":0.09,"effective_min_edge":0.10}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = {
        "performance_feedback": {
            "enabled": False,
            "overtight_min_lane_sample": 2,
            "overtight_min_pass_sample": 2,
            "overtight_ghost_wr_threshold": 0.5,
        }
    }
    with patch(
        "src.execution.performance_feedback.DEFAULT_SETTLED_LOG",
        settled_path,
    ):
        status = public_feedback_status(cfg)
    assert status["feature_enabled"] is False
    assert status["overtight_preview"]["count"] == 1
