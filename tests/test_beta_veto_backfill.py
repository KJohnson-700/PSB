from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.analysis.beta_veto_backfill import build_beta_veto_backfill


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_backfill_reconstructs_matching_rows_from_trade_history(tmp_path: Path) -> None:
    trades = tmp_path / "trades.jsonl"
    rejected = tmp_path / "rejected.jsonl"
    settled = tmp_path / "settled.jsonl"

    lane = "eth_macro|5m|down|bearish__bearish__bull|standard"
    _write_jsonl(
        trades,
        [
            {
                "closed_at": "2026-05-27T10:00:00+00:00",
                "lane_id": lane,
                "win": False,
            },
            {
                "closed_at": "2026-05-27T10:05:00+00:00",
                "lane_id": lane,
                "win": False,
            },
            {
                "closed_at": "2026-05-27T10:10:00+00:00",
                "lane_id": lane,
                "win": False,
            },
        ]
        + [
            {
                "closed_at": f"2026-05-27T10:{11 + idx:02d}:00+00:00",
                "lane_id": lane,
                "win": False,
            }
            for idx in range(27)
        ],
    )
    target_reject = {
        "ts": "2026-05-27T10:39:00+00:00",
        "strategy": "eth_macro",
        "window": "5m",
        "action": "BUY_NO",
        "reason": "lane_min_edge",
        "market_id": "m1",
        "market_question": "ETH Up or Down",
        "live_lane_id": lane,
    }
    ghost_id = hashlib.sha1(
        f"{target_reject['ts']}|{target_reject['market_id']}|{target_reject['reason']}".encode("utf-8")
    ).hexdigest()[:16]
    _write_jsonl(
        rejected,
        [
            target_reject,
            {
                "ts": "2026-05-27T10:02:00+00:00",
                "strategy": "eth_macro",
                "window": "5m",
                "action": "BUY_NO",
                "reason": "lane_min_edge",
                "market_id": "m2",
                "market_question": "ETH Up or Down 2",
                "live_lane_id": lane,
            },
        ],
    )
    _write_jsonl(
        settled,
        [
            {
                "ghost_id": ghost_id,
                "outcome": "NO",
                "win": True,
                "realized_pct": 1.0,
                "settled_at": "2026-05-27T11:00:00+00:00",
            }
        ],
    )

    rows, summary = build_beta_veto_backfill(
        trades_path=trades,
        rejected_path=rejected,
        settled_path=settled,
        beta_veto_max_mean=0.42,
        beta_veto_min_n=30,
    )

    assert len(rows) == 1
    assert rows[0]["live_lane_id"] == lane
    assert rows[0]["would_beta_veto"] is True
    assert rows[0]["posterior_before_reject"]["n"] == 30
    assert rows[0]["posterior_before_reject"]["beta_mean"] < 0.42
    assert summary["counts"]["rows_matching_beta_veto"] == 1
    assert summary["counts"]["settled_rows_matching_beta_veto"] == 1
    assert summary["counts"]["settled_wins"] == 1
