"""Tests for session directory resolution (dashboard vs bot resume alignment)."""

from pathlib import Path

from src.execution.trade_journal import TradeJournal


def test_newest_resumable_session_dir_skips_empty_stubs(tmp_path: Path) -> None:
    old = tmp_path / "20260101_100000"
    new_empty = tmp_path / "20260102_200000"
    old.mkdir()
    new_empty.mkdir()
    (old / "summary.json").write_text("{}", encoding="utf-8")

    got = TradeJournal.newest_resumable_session_dir(tmp_path)
    assert got is not None
    assert got.name == "20260101_100000"
