"""TradeJournal.append_annotation: append-only, doesn't disturb open positions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.execution.trade_journal import TradeJournal


@pytest.fixture
def journal(tmp_path: Path, monkeypatch) -> TradeJournal:
    import src.execution.trade_journal as tj
    monkeypatch.setattr(tj, "JOURNAL_DIR", tmp_path)
    j = TradeJournal(session_id="annotation_test", resume_latest=False)
    return j


def test_append_annotation_creates_jsonl_line(journal: TradeJournal) -> None:
    journal.append_annotation(
        trade_id="tid_1",
        text="Entered on bull breakout; invalidate below $80k.",
        strategy="bitcoin",
        market_id="mkt_1",
        market_question="Will BTC exceed $85k by Friday?",
    )
    entries = Path(journal._entries_file)
    assert entries.exists()
    with open(entries, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["event"] == "ANNOTATION"
    assert rec["trade_id"] == "tid_1"
    assert rec["strategy"] == "bitcoin"
    assert rec["extra"]["text"].startswith("Entered")


def test_append_annotation_does_not_open_position(journal: TradeJournal) -> None:
    before = dict(journal.open_positions)
    journal.append_annotation(trade_id="tid_2", text="hello")
    assert journal.open_positions == before


def test_append_annotation_with_extra_merges_into_payload(journal: TradeJournal) -> None:
    journal.append_annotation(
        trade_id="tid_3",
        text="thesis ok",
        extra={"source": "post_trade_annotation", "ai_confidence": 0.7},
    )
    with open(journal._entries_file, encoding="utf-8") as f:
        rec = json.loads([l for l in f if l.strip()][-1])
    assert rec["extra"]["source"] == "post_trade_annotation"
    assert rec["extra"]["ai_confidence"] == 0.7
    assert rec["extra"]["text"] == "thesis ok"


def test_append_annotation_skipped_when_text_empty(journal: TradeJournal) -> None:
    def _line_count() -> int:
        if not Path(journal._entries_file).exists():
            return 0
        with open(journal._entries_file, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    before = _line_count()
    journal.append_annotation(trade_id="tid_4", text="")
    assert _line_count() == before
