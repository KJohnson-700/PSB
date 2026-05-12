"""Tests for the append-only AI decision log."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.analysis import ai_call_log


def _common_kwargs(**overrides):
    base = dict(
        market_question="BTC price > $80,000 by 7:05 PM ET",
        market_id="ABC123",
        strategy_hint="bitcoin",
        quant_action="BUY_YES",
        quant_edge=0.123,
        quant_confidence=0.715,
        quant_threshold=0.08,
        approved=True,
        ai_action="BUY_YES",
        ai_confidence=0.72,
        ai_estimated_probability=0.61,
        ai_edge=0.11,
        ai_reason="direct_ai_approved",
        ai_source="direct",
    )
    base.update(overrides)
    return base


def test_context_hash_stable_across_calls():
    a = ai_call_log.context_hash(
        market_question="BTC up?",
        market_id="X",
        strategy_hint="bitcoin",
        quant_action="BUY_YES",
        quant_edge=0.10,
        quant_confidence=0.65,
    )
    b = ai_call_log.context_hash(
        market_question="BTC up?",
        market_id="X",
        strategy_hint="bitcoin",
        quant_action="BUY_YES",
        quant_edge=0.10,
        quant_confidence=0.65,
    )
    assert a == b


def test_context_hash_rounds_float_noise():
    """Hash treats edge=0.10000001 and 0.10000002 as the same context (4dp)."""
    a = ai_call_log.context_hash(
        market_question="q", market_id="X", strategy_hint="bitcoin",
        quant_action="BUY_YES", quant_edge=0.10000001, quant_confidence=0.65,
    )
    b = ai_call_log.context_hash(
        market_question="q", market_id="X", strategy_hint="bitcoin",
        quant_action="BUY_YES", quant_edge=0.10000002, quant_confidence=0.65,
    )
    assert a == b


def test_context_hash_differs_on_action():
    a = ai_call_log.context_hash(
        market_question="q", market_id="X", strategy_hint="bitcoin",
        quant_action="BUY_YES", quant_edge=0.10, quant_confidence=0.65,
    )
    b = ai_call_log.context_hash(
        market_question="q", market_id="X", strategy_hint="bitcoin",
        quant_action="BUY_NO", quant_edge=0.10, quant_confidence=0.65,
    )
    assert a != b


def test_append_record_writes_jsonl(tmp_path):
    ai_call_log.append_record(
        log_dir=tmp_path,
        now=datetime(2026, 5, 12, 4, 30, tzinfo=timezone.utc),
        **_common_kwargs(),
    )
    expected_file = tmp_path / "2026-05-12.jsonl"
    assert expected_file.exists()
    lines = expected_file.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["market_id"] == "ABC123"
    assert rec["strategy_hint"] == "bitcoin"
    assert rec["quant_action"] == "BUY_YES"
    assert rec["approved"] is True
    assert rec["ai_action"] == "BUY_YES"
    assert "context_hash" in rec and len(rec["context_hash"]) == 64
    assert rec["ts"].startswith("2026-05-12")


def test_append_record_appends_not_truncates(tmp_path):
    now = datetime(2026, 5, 12, 4, 30, tzinfo=timezone.utc)
    ai_call_log.append_record(log_dir=tmp_path, now=now, **_common_kwargs(market_id="M1"))
    ai_call_log.append_record(log_dir=tmp_path, now=now, **_common_kwargs(market_id="M2"))
    lines = (tmp_path / "2026-05-12.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    ids = [json.loads(l)["market_id"] for l in lines]
    assert ids == ["M1", "M2"]


def test_append_record_handles_rejection(tmp_path):
    ai_call_log.append_record(
        log_dir=tmp_path,
        now=datetime(2026, 5, 12, 4, 30, tzinfo=timezone.utc),
        **_common_kwargs(
            approved=False,
            ai_action="HOLD",
            ai_edge=None,
            ai_reason="direct_ai_hold",
        ),
    )
    rec = json.loads((tmp_path / "2026-05-12.jsonl").read_text().strip())
    assert rec["approved"] is False
    assert rec["ai_action"] == "HOLD"
    assert rec["ai_edge"] is None
    assert rec["ai_reason"] == "direct_ai_hold"


def test_append_record_swallows_disk_errors(monkeypatch, tmp_path):
    """A disk write error must never raise — that would block a trade."""
    def boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "mkdir", boom)
    # Should not raise
    ai_call_log.append_record(log_dir=tmp_path / "fail", **_common_kwargs())
