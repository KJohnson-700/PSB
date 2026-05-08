"""Dashboard /api/journal/ai-summary surfaces narrator ANNOTATION events
written by the startup-narrators path."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.dashboard.server import (
    _format_narrator_block,
    _read_narrator_annotations,
)


def _write_entries(tmp_path: Path, records: list) -> Path:
    f = tmp_path / "entries.jsonl"
    with open(f, "w", encoding="utf-8") as out:
        for r in records:
            out.write(json.dumps(r) + "\n")
    return f


def _journal_with_entries(entries_file: Path) -> MagicMock:
    j = MagicMock()
    j._entries_file = entries_file
    j.session_dir = entries_file.parent
    return j


def test_read_narrator_annotations_filters_by_source(tmp_path: Path) -> None:
    entries = _write_entries(
        tmp_path,
        [
            {"event": "ENTRY", "extra": {}},
            {
                "event": "ANNOTATION",
                "timestamp": "2026-05-08T12:00:00Z",
                "extra": {
                    "source": "session_summary",
                    "narrator": "skip_exit_reasons",
                    "previous_session": "test_2026_05_07",
                    "text": "rsi_extreme_block dominated 38% of skips.",
                },
            },
            {
                "event": "ANNOTATION",
                "extra": {
                    "source": "post_trade_annotation",
                    "text": "Other annotation type — should be filtered out",
                },
            },
        ],
    )
    j = _journal_with_entries(entries)
    out = _read_narrator_annotations(j)
    assert len(out) == 1
    assert out[0]["narrator"] == "skip_exit_reasons"
    assert "dominated" in out[0]["text"]


def test_read_narrator_annotations_returns_empty_when_no_file(tmp_path: Path) -> None:
    j = MagicMock()
    j._entries_file = tmp_path / "missing.jsonl"
    assert _read_narrator_annotations(j) == []


def test_format_narrator_block_renders_pretty_titles() -> None:
    anns = [
        {
            "narrator": "underperformance",
            "text": "BTC 5m regressed.",
            "previous_session": "x",
            "timestamp": "t",
        },
        {
            "narrator": "calibration_drift",
            "text": "AI is over-confident in mid bucket.",
            "previous_session": "x",
            "timestamp": "t",
        },
    ]
    out = _format_narrator_block(anns)
    assert "AI Narrators" in out
    assert "### Underperformance" in out
    assert "BTC 5m regressed." in out
    assert "### Calibration drift" in out
    assert "over-confident" in out


def test_format_narrator_block_empty_input_returns_empty() -> None:
    assert _format_narrator_block([]) == ""
