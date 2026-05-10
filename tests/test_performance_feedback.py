"""Tests for drift-driven runtime performance feedback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.execution.live_testing import DriftReport
from src.execution.performance_feedback import (
    get_drift_kelly_mult,
    get_drift_min_edge_mult,
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
