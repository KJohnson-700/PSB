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
