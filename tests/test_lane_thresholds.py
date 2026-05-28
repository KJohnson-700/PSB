"""Tests for live + ghost merge in lane_thresholds.compute_lane_thresholds."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis.lane_thresholds import (
    aggregate_ghost_buckets,
    aggregate_live_buckets,
    compute_lane_thresholds,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _live_row(lane_id: str, win: bool) -> dict:
    return {"lane_id": lane_id, "win": win}


def _ghost_row(strategy: str, window: str, direction: str, family: str, bias: str, win: bool) -> dict:
    # live_lane_id is the easy path — translator returns it directly
    return {
        "strategy": strategy,
        "window": window,
        "live_lane_id": f"{strategy}|{window}|{direction}|{bias}|{family}",
        "win": win,
    }


def test_aggregate_live_buckets_keys_and_counts(tmp_path: Path) -> None:
    p = tmp_path / "trades.jsonl"
    _write_jsonl(
        p,
        [
            _live_row("bitcoin|5m|down|bearish|htf_bearish_side_short", True),
            _live_row("bitcoin|5m|down|bearish|htf_bearish_side_short", False),
            _live_row("bitcoin|5m|down|bearish|htf_bearish_side_short", False),
            _live_row("bitcoin|15m|up|bullish|standard", True),
        ],
    )
    buckets = aggregate_live_buckets(p)
    assert set(buckets.keys()) == {
        "bitcoin|5m|down|bearish|htf_bearish_side_short",
        "bitcoin|15m|up|bullish|standard",
    }
    short = buckets["bitcoin|5m|down|bearish|htf_bearish_side_short"]
    assert short.n == 3 and short.wins == 1


def test_aggregate_live_buckets_skips_invalid_rows(tmp_path: Path) -> None:
    p = tmp_path / "trades.jsonl"
    _write_jsonl(
        p,
        [
            _live_row("bitcoin|5m|down|bearish|standard", True),
            # missing win
            {"lane_id": "bitcoin|5m|down|bearish|standard"},
            # win not bool
            {"lane_id": "bitcoin|5m|down|bearish|standard", "win": "yes"},
            # malformed lane_id (only 3 parts)
            {"lane_id": "bitcoin|5m|down", "win": True},
            # empty lane_id
            {"lane_id": "", "win": True},
        ],
    )
    buckets = aggregate_live_buckets(p)
    assert list(buckets.keys()) == ["bitcoin|5m|down|bearish|standard"]
    assert buckets["bitcoin|5m|down|bearish|standard"].n == 1


def test_compute_uses_live_only_when_mature(tmp_path: Path) -> None:
    """When live_n >= live_mature_n, ghost is dropped from the decision."""
    settled = tmp_path / "ghost.jsonl"
    trades = tmp_path / "trades.jsonl"
    lane = "bitcoin|5m|down|bearish|htf_bearish_side_short"

    # Ghost: 100 records, 70 wins → ghost_wr 0.70 (would defeat veto if mixed)
    _write_jsonl(
        settled,
        [_ghost_row("bitcoin", "5m", "down", "htf_bearish_side_short", "bearish", i < 70) for i in range(100)],
    )
    # Live: 100 records, 30 wins → live_wr 0.30 (mature: n >= 50)
    _write_jsonl(trades, [_live_row(lane, i < 30) for i in range(100)])

    payload = compute_lane_thresholds(
        settled_path=settled,
        trades_path=trades,
        min_bucket_n=50,
        wr_veto_threshold=0.40,
        live_mature_n=50,
    )
    info = payload["thresholds"][lane]
    # Decision uses live only
    assert info["decision_source"] == "live"
    assert info["n"] == 100
    assert info["wr"] == 0.30
    # Ghost stats are still recorded for visibility
    assert info["ghost_n"] == 100
    assert info["ghost_wr"] == 0.70
    assert info["live_n"] == 100
    assert info["live_wr"] == 0.30
    # 0.30 < 0.40 → veto fires
    assert info["veto_recommended"] is True


def test_compute_merges_when_live_immature(tmp_path: Path) -> None:
    """When live_n < live_mature_n, ghost+live merge equal-weight."""
    settled = tmp_path / "ghost.jsonl"
    trades = tmp_path / "trades.jsonl"
    lane = "bitcoin|5m|down|bearish|standard"

    # Ghost: 100/70 = 0.70, Live: 10/0 = 0.0 (live too small to decide alone)
    _write_jsonl(
        settled,
        [_ghost_row("bitcoin", "5m", "down", "standard", "bearish", i < 70) for i in range(100)],
    )
    _write_jsonl(trades, [_live_row(lane, False) for _ in range(10)])

    payload = compute_lane_thresholds(
        settled_path=settled,
        trades_path=trades,
        min_bucket_n=50,
        wr_veto_threshold=0.40,
        live_mature_n=50,
    )
    info = payload["thresholds"][lane]
    assert info["decision_source"] == "combined"
    assert info["n"] == 110  # 100 + 10
    # 70 / 110 = 0.636 → no veto
    assert info["wr"] == pytest.approx(0.6364, abs=0.001)
    assert info["veto_recommended"] is False


def test_compute_vetoes_when_live_only_loses(tmp_path: Path) -> None:
    """Lane with NO ghost data but losing live record gets vetoed."""
    settled = tmp_path / "ghost.jsonl"
    trades = tmp_path / "trades.jsonl"
    lane = "bitcoin|5m|down|bearish|drift"
    _write_jsonl(settled, [])
    # 100 live trades, 30% WR
    _write_jsonl(trades, [_live_row(lane, i < 30) for i in range(100)])

    payload = compute_lane_thresholds(
        settled_path=settled,
        trades_path=trades,
        min_bucket_n=50,
        wr_veto_threshold=0.40,
        live_mature_n=50,
    )
    info = payload["thresholds"][lane]
    assert info["decision_source"] == "live"
    assert info["ghost_n"] == 0
    assert info["live_n"] == 100
    assert info["wr"] == 0.30
    assert info["veto_recommended"] is True
    # ghost_wr should be absent because ghost_n == 0
    assert "ghost_wr" not in info


def test_compute_respects_min_bucket_n_on_combined(tmp_path: Path) -> None:
    """Lane with ghost_n=20 + live_n=20 < min_bucket_n=50 is dropped."""
    settled = tmp_path / "ghost.jsonl"
    trades = tmp_path / "trades.jsonl"
    lane = "bitcoin|5m|down|bearish|standard"
    _write_jsonl(
        settled,
        [_ghost_row("bitcoin", "5m", "down", "standard", "bearish", False) for _ in range(20)],
    )
    _write_jsonl(trades, [_live_row(lane, False) for _ in range(20)])

    payload = compute_lane_thresholds(
        settled_path=settled,
        trades_path=trades,
        min_bucket_n=50,
        wr_veto_threshold=0.40,
        live_mature_n=50,
    )
    assert lane not in payload["thresholds"]


def test_compute_ghost_only_lane_still_works(tmp_path: Path) -> None:
    """Backwards-compat: lane with only ghost data still produces output."""
    settled = tmp_path / "ghost.jsonl"
    trades = tmp_path / "trades.jsonl"
    _write_jsonl(
        settled,
        [_ghost_row("bitcoin", "5m", "down", "standard", "bearish", i < 60) for i in range(100)],
    )
    _write_jsonl(trades, [])

    payload = compute_lane_thresholds(
        settled_path=settled,
        trades_path=trades,
        min_bucket_n=50,
        wr_veto_threshold=0.40,
        live_mature_n=50,
    )
    lane = "bitcoin|5m|down|bearish|standard"
    info = payload["thresholds"][lane]
    assert info["decision_source"] == "combined"
    assert info["live_n"] == 0
    assert info["ghost_n"] == 100
    assert info["wr"] == 0.60
    assert info["veto_recommended"] is False
    assert "live_wr" not in info
