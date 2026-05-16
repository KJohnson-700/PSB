from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.analysis import ghost_calibration as gc


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_settle_rejected_candidates_appends_only_newly_resolved(tmp_path, monkeypatch):
    rejected = tmp_path / "rejected.jsonl"
    settled = tmp_path / "settled.jsonl"
    now = datetime(2026, 5, 16, 15, 0, tzinfo=timezone.utc)
    _write_jsonl(
        rejected,
        [
            {
                "ts": "2026-05-16T14:00:00+00:00",
                "market_id": "m1",
                "reason": "hist_gate_5m_long_reject",
                "action": "BUY_YES",
                "yes_price": 0.40,
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
        now=now,
    )

    assert summary["newly_settled"] == 1
    assert summary["written"] == 1
    assert summary["too_recent"] == 1

    rows = list(gc._iter_jsonl(settled))
    assert len(rows) == 1
    assert rows[0]["market_id"] == "m1"
    assert rows[0]["win"] is True
    assert rows[0]["realized_pct"] == 1.5


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
