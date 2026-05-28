from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.analysis import ghost_calibration as gc
from src.analysis import lane_thresholds as lt


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_settle_rejected_candidates_appends_only_newly_resolved(tmp_path, monkeypatch):
    rejected = tmp_path / "rejected.jsonl"
    settled = tmp_path / "settled.jsonl"
    regime = tmp_path / "market_regime.jsonl"
    now = datetime(2026, 5, 16, 15, 0, tzinfo=timezone.utc)
    _write_jsonl(
        regime,
        [
            {
                "ts": "2026-05-16T14:00:20+00:00",
                "price_regime": "flat",
                "polymarket_regime": "deadzone",
                "combined_regime": "deadzone_confirmed",
            }
        ],
    )
    _write_jsonl(
        rejected,
        [
            {
                "ts": "2026-05-16T14:00:00+00:00",
                "market_id": "m1",
                "reason": "hist_gate_5m_long_reject",
                "action": "BUY_YES",
                "yes_price": 0.40,
                "btc_1h_regime": "BEAR",
                "convergence_score": 0.42,
                "market_end_ts": (now - timedelta(minutes=10)).isoformat(),
            },
            {
                "ts": "2026-05-16T14:10:00+00:00",
                "market_id": "m2",
                "reason": "hist_gate_5m_short_reject",
                "action": "BUY_NO",
                "no_price": 0.45,
                "market_end_ts": (now - timedelta(seconds=30)).isoformat(),
            },
        ],
    )

    monkeypatch.setattr(
        gc,
        "fetch_resolution",
        lambda market_id, cache, timeout=10.0: "YES" if market_id == "m1" else None,
    )

    summary = gc.settle_rejected_candidates(
        input_path=rejected,
        output_path=settled,
        regime_path=regime,
        now=now,
    )

    assert summary["newly_settled"] == 1
    assert summary["regime_matched"] == 1
    assert summary["written"] == 1
    assert summary["too_recent"] == 1

    rows = list(gc._iter_jsonl(settled))
    assert len(rows) == 1
    assert rows[0]["market_id"] == "m1"
    assert rows[0]["win"] is True
    assert rows[0]["realized_pct"] == 1.5
    assert rows[0]["btc_1h_regime"] == "BEAR"
    assert rows[0]["convergence_score"] == 0.42
    assert rows[0]["price_regime"] == "flat"
    assert rows[0]["polymarket_regime"] == "deadzone"
    assert rows[0]["combined_regime"] == "deadzone_confirmed"
    assert rows[0]["regime_source"] == "market_regime"


def test_backfill_settled_regimes_labels_existing_rows(tmp_path):
    settled = tmp_path / "settled.jsonl"
    regime = tmp_path / "market_regime.jsonl"
    rejected = tmp_path / "rejected.jsonl"
    rejected_rows = [
        {
            "ts": "2026-05-16T14:00:00+00:00",
            "market_id": "",
            "reason": "r1",
            "action": "BUY_YES",
            "btc_1h_regime": "BEAR",
            "convergence_score": 0.33,
        },
        {
            "ts": "2026-05-16T16:00:00+00:00",
            "market_id": "",
            "reason": "r2",
            "action": "BUY_NO",
            "btc_1h_regime": "BULL",
            "convergence_score": 0.81,
        },
    ]
    _write_jsonl(
        settled,
        [
            {
                "ghost_id": gc.ghost_id(rejected_rows[0]),
                "ts": "2026-05-16T14:00:00+00:00",
                "reason": "r1",
                "action": "BUY_YES",
                "regime_source": "unmatched",
            },
            {
                "ghost_id": gc.ghost_id(rejected_rows[1]),
                "ts": "2026-05-16T16:00:00+00:00",
                "reason": "r2",
                "action": "BUY_NO",
            },
        ],
    )
    _write_jsonl(
        regime,
        [
            {
                "ts": "2026-05-16T14:05:00+00:00",
                "price_regime": "hot",
                "polymarket_regime": "signal",
                "combined_regime": "active",
            }
        ],
    )
    _write_jsonl(
        rejected,
        rejected_rows,
    )

    summary = gc.backfill_settled_regimes(
        input_path=settled,
        regime_path=regime,
        rejected_path=rejected,
        max_age_sec=600,
    )

    rows = list(gc._iter_jsonl(settled))
    assert summary["rows"] == 2
    assert summary["matched"] == 1
    assert summary["unmatched"] == 1
    assert summary["rejected_metadata_copied"] == 2
    assert rows[0]["combined_regime"] == "active"
    assert rows[0]["btc_1h_regime"] == "BEAR"
    assert rows[0]["convergence_score"] == 0.33
    assert rows[0]["regime_source"] == "market_regime"
    assert rows[1]["btc_1h_regime"] == "BULL"
    assert rows[1]["regime_source"] == "unmatched"


def test_build_ghost_calibration_status_summarizes_logs(tmp_path):
    rejected = tmp_path / "rejected.jsonl"
    settled = tmp_path / "settled.jsonl"
    _write_jsonl(
        rejected,
        [
            {"ts": "1", "market_id": "a", "reason": "r1", "action": "BUY_YES"},
            {"ts": "2", "market_id": "b", "reason": "r1", "action": "BUY_YES"},
            {"ts": "3", "market_id": "c", "reason": "r2", "action": "BUY_NO"},
        ],
    )
    _write_jsonl(
        settled,
        [
            {
                "ghost_id": "g1",
                "reason": "r1",
                "action": "BUY_YES",
                "win": True,
                "settled_at": "2026-05-16T15:00:00+00:00",
            },
            {
                "ghost_id": "g2",
                "reason": "r1",
                "action": "BUY_YES",
                "win": False,
                "settled_at": "2026-05-16T15:05:00+00:00",
            },
        ],
    )

    status = gc.build_ghost_calibration_status(
        rejected_path=rejected,
        settled_path=settled,
    )

    assert status["total_rejected"] == 3
    assert status["total_settled"] == 2
    assert status["unresolved"] == 1
    assert status["wins"] == 1
    assert status["losses"] == 1
    assert status["settled_win_rate"] == 0.5
    assert status["last_settled_at"] == "2026-05-16T15:05:00+00:00"
    assert status["top_reason_action"]["r1|BUY_YES"]["n"] == 2


def test_ghost_lane_reconstruction_preserves_uniform_family_without_live_lane_id():
    rec = {
        "lane_id": "eth_macro|15m|up|bullish|rejected",
        "strategy": "eth_macro",
        "window": "15m",
        "action": "BUY_YES",
        "reason": "hist_gate_15m_long_reject",
        "side_source": "eth_15m_native",
        "primary_htf_bias": "BULLISH",
        "alt_htf_bias": "BULLISH",
        "btc_1h_regime": "BEAR",
    }

    expected = "eth_macro|15m|up|bullish__bullish__bear|eth_15m_native"
    assert gc._ghost_to_live_lane_keys(rec) == [expected]
    assert lt._ghost_to_live_lane_id(rec) == expected


def test_ghost_lane_reconstruction_uses_recorded_lane_family_when_present():
    rec = {
        "lane_id": "doge_macro|5m|down|bearish|rejected",
        "strategy": "doge_macro",
        "window": "5m",
        "action": "BUY_NO",
        "reason": "entry_window_block",
        "lane_family": "bearish_dip_default",
        "primary_htf_bias": "BEARISH",
        "btc_1h_regime": "BULL",
    }

    expected = "doge_macro|5m|down|bearish__bearish__bull|bearish_dip_default"
    assert gc._ghost_to_live_lane_keys(rec) == [expected]
    assert lt._ghost_to_live_lane_id(rec) == expected
