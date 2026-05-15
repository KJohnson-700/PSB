"""Phase 0 calibration log tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.analysis.calibration_log import (
    append_calibration_record,
    build_record_from_closed_trade,
)


def _sample_closed(**over):
    closed = {
        "trade_id": "dry_t1",
        "market_id": "m1",
        "market_question": "ETH Up or Down ...",
        "strategy": "eth_macro",
        "action": "BUY_YES",
        "side": "BUY",
        "outcome": "YES",
        "size": 21.978,
        "entry_price": 0.455,
        "current_price": 0.62,
        "pnl": 3.6266,
        "edge": 0.125,
        "confidence": 0.58,
        "entry_reason": "...",
        "opened_at": "2026-05-14T21:46:13.971+00:00",
        "entry_leg": "YES",
        "window_size": "5m",
        "closed_at": "2026-05-14T21:48:00.0+00:00",
        "exit_price": 0.62,
        "exit_reason": "take_profit",
        "entry_signal": {
            "lane_id": "eth_macro|5m|down|bearish__bearish__bull|standard",
            "est_prob": 0.58,
            "raw_est_prob": 0.58,
            "window_size": "5m",
        },
    }
    closed.update(over)
    return closed


def test_build_record_extracts_canonical_schema():
    closed = _sample_closed()
    rec = build_record_from_closed_trade(closed, session_id="test_S")

    assert rec["session_id"] == "test_S"
    assert rec["trade_id"] == "dry_t1"
    assert rec["lane_id"] == "eth_macro|5m|down|bearish__bearish__bull|standard"
    assert rec["strategy"] == "eth_macro"
    assert rec["window"] == "5m"
    assert rec["side"] == "BUY_YES"
    assert rec["entry_price"] == 0.455
    assert rec["exit_price"] == 0.62
    assert rec["size"] == 21.978
    # notional = size * entry_price; realized_pct = pnl / notional
    assert abs(rec["notional"] - 21.978 * 0.455) < 1e-6
    assert abs(rec["realized_pct"] - (3.6266 / (21.978 * 0.455))) < 1e-6
    assert rec["win"] is True
    assert rec["stated_edge"] == 0.125
    assert rec["stated_est_prob"] == 0.58
    assert rec["calibrated_est_prob"] == 0.58  # Phase 0 identity
    assert rec["alpha_used"] == 1.0
    assert rec["exit_reason"] == "take_profit"
    assert rec["schema_version"] == 1


def test_build_record_handles_loss_and_buy_no():
    closed = _sample_closed(
        action="BUY_NO",
        entry_leg="NO",
        entry_price=0.495,
        exit_price=0.30,
        pnl=-4.291,
        edge=0.10,
        entry_signal={
            "lane_id": "xrp_macro|5m|down|bullish|standard",
            "est_prob": 0.40,
        },
    )
    rec = build_record_from_closed_trade(closed, session_id="s")
    assert rec["side"] == "BUY_NO"
    assert rec["win"] is False
    assert rec["realized_pct"] < 0
    assert rec["stated_est_prob"] == 0.40


def test_build_record_falls_back_when_lane_id_missing():
    closed = _sample_closed(entry_signal={"est_prob": 0.55, "window_size": "5m"})
    rec = build_record_from_closed_trade(closed, session_id="s")
    # Fallback should still produce a stable lane_id with the coarse triple.
    assert rec["lane_id"].startswith("eth_macro|5m|up|")
    assert rec["lane_id"].endswith("|fallback")


def test_build_record_missing_est_prob_yields_none():
    closed = _sample_closed(entry_signal={})
    rec = build_record_from_closed_trade(closed, session_id="s")
    assert rec["stated_est_prob"] is None
    assert rec["calibrated_est_prob"] is None
    assert rec["alpha_used"] == 1.0


def test_build_record_zero_size_does_not_div_by_zero():
    closed = _sample_closed(size=0.0, pnl=0.0)
    rec = build_record_from_closed_trade(closed, session_id="s")
    assert rec["notional"] == 0.0
    assert rec["realized_pct"] == 0.0


def test_append_writes_one_jsonl_line(tmp_path: Path):
    log = tmp_path / "trades.jsonl"
    rec = build_record_from_closed_trade(_sample_closed(), session_id="s1")
    assert append_calibration_record(rec, log_path=log) is True
    contents = log.read_text().strip().splitlines()
    assert len(contents) == 1
    parsed = json.loads(contents[0])
    assert parsed["trade_id"] == "dry_t1"
    assert parsed["lane_id"] == "eth_macro|5m|down|bearish__bearish__bull|standard"


def test_append_is_idempotent_append(tmp_path: Path):
    log = tmp_path / "trades.jsonl"
    rec = build_record_from_closed_trade(_sample_closed(), session_id="s1")
    append_calibration_record(rec, log_path=log)
    append_calibration_record(rec, log_path=log)
    append_calibration_record(rec, log_path=log)
    assert len(log.read_text().strip().splitlines()) == 3


def test_append_creates_parent_directory(tmp_path: Path):
    log = tmp_path / "nested" / "deep" / "trades.jsonl"
    rec = build_record_from_closed_trade(_sample_closed(), session_id="s1")
    assert append_calibration_record(rec, log_path=log) is True
    assert log.exists()


def test_append_never_raises_on_serialize_failure(tmp_path: Path):
    log = tmp_path / "trades.jsonl"
    # Non-JSON-serializable value in the dict should NOT raise into caller.
    bad = {"ts": datetime.now(timezone.utc), "blob": object()}
    assert append_calibration_record(bad, log_path=log) is False
