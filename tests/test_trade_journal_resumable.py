"""Tests for session directory resolution (dashboard vs bot resume alignment)."""

from pathlib import Path
import src.execution.trade_journal as trade_journal_module

from src.execution.trade_journal import TradeJournal


def test_newest_resumable_session_dir_skips_empty_stubs(tmp_path: Path) -> None:
    old = tmp_path / "20260101_100000"
    new_empty = tmp_path / "20260102_200000"
    old.mkdir()
    new_empty.mkdir()
    (old / "entries.jsonl").write_text(
        '{"timestamp":"2026-01-01T10:00:00+00:00","event":"ENTRY","trade_id":"t1"}\n',
        encoding="utf-8",
    )

    got = TradeJournal.newest_resumable_session_dir(tmp_path)
    assert got is not None
    assert got.name == "20260101_100000"


def test_list_sessions_skips_empty_summary_only_dirs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path)
    empty = tmp_path / "20260102_200000"
    full = tmp_path / "20260101_100000"
    empty.mkdir()
    full.mkdir()
    (empty / "summary.json").write_text("{}", encoding="utf-8")
    (full / "entries.jsonl").write_text(
        '{"timestamp":"2026-01-01T10:00:00+00:00","event":"ENTRY","trade_id":"t1"}\n',
        encoding="utf-8",
    )
    sessions = TradeJournal.list_sessions()
    assert [s["session_id"] for s in sessions] == ["20260101_100000"]


def test_weather_subtype_summary_tracks_open_and_closed_stats(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path)
    journal = TradeJournal(session_id="20260427_210000", resume_latest=False)
    journal.log_entry(
        trade_id="t-temp",
        market_id="wx-temp",
        market_question="Highest temperature in Hong Kong on May 5, 2026?",
        strategy="weather",
        action="BUY_YES",
        side="BUY",
        outcome="YES",
        size=10.0,
        entry_price=0.4,
        bankroll=1000.0,
        extra={"weather_subtype": "temp"},
    )
    journal.log_entry(
        trade_id="t-precip",
        market_id="wx-precip",
        market_question="Will Hong Kong have 100-110mm of precipitation in May 2026?",
        strategy="weather",
        action="BUY_NO",
        side="BUY",
        outcome="NO",
        size=12.0,
        entry_price=0.5,
        bankroll=1000.0,
        extra={"weather_subtype": "precip"},
    )
    journal.log_exit("t-temp", exit_price=0.7, bankroll=1003.0, reason="test")
    summary = journal.get_summary()
    assert summary["weather_subtype_stats"]["temp"]["trades"] == 1
    assert summary["weather_subtype_stats"]["temp"]["wins"] == 1
    assert summary["weather_subtype_stats"]["precip"]["trades"] == 0
    assert summary["weather_open_stats"]["temp"]["open"] == 0
    assert summary["weather_open_stats"]["precip"]["open"] == 1


def test_dead_zone_skip_records_and_resolves(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path)
    journal = TradeJournal(session_id="20260428_220000", resume_latest=False)
    journal.log_dead_zone_skip(
        market_id="btc-updown-1",
        market_question="Bitcoin Up or Down - Apr 28, 10:00AM-10:15AM ET",
        strategy="bitcoin",
        action="BUY_YES",
        hour_utc=18,
        blocked_hours=[18, 22],
        bankroll=1000.0,
        edge=0.12,
        extra={"confidence": 0.66, "window_size": "15m"},
    )
    journal.resolve_dead_zone_skips(
        {
            "btc-updown-1": {
                "resolved": True,
                "outcome_won": "YES",
                "resolved_at": "2026-04-28T18:15:00+00:00",
            }
        }
    )
    entries = journal.get_all_entries(limit=10)
    events = [entry["event"] for entry in entries]
    assert "DEAD_ZONE_SKIP" in events
    assert "DEAD_ZONE_SKIP_RESOLVED" in events
    resolved = next(entry for entry in entries if entry["event"] == "DEAD_ZONE_SKIP_RESOLVED")
    assert resolved["outcome"] == "YES"
    assert resolved["extra"]["hypothetical_result"] == "WIN"
    assert resolved["extra"]["hour_utc"] == 18
