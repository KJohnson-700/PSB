"""Tests for session directory resolution (dashboard vs bot resume alignment)."""

import json
from pathlib import Path
import src.execution.trade_journal as trade_journal_module

import pytest

from src.execution.trade_journal import TradeJournal, is_phantom_exit_row


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


def test_summary_file_updates_immediately_on_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path)
    journal = TradeJournal(session_id="20260525_entry_summary", resume_latest=False)

    journal.log_entry(
        trade_id="t-open",
        market_id="btc-updown",
        market_question="Bitcoin Up or Down",
        strategy="bitcoin",
        action="BUY_YES",
        side="BUY",
        outcome="YES",
        size=10.0,
        entry_price=0.5,
        bankroll=500.0,
    )

    summary = json.loads((journal.session_dir / "summary.json").read_text())
    assert summary["total_entries"] == 1
    assert summary["open_positions"] == 1
    assert summary["total_cost"] == 5.0


def test_last_bankroll_ignores_annotation_zero_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path)
    journal = TradeJournal(session_id="20260607_bankroll_annotations", resume_latest=False)

    journal.log_entry(
        trade_id="t-open",
        market_id="btc-updown",
        market_question="Bitcoin Up or Down",
        strategy="bitcoin",
        action="BUY_YES",
        side="BUY",
        outcome="YES",
        size=10.0,
        entry_price=0.5,
        bankroll=504.25,
    )
    journal.append_annotation(
        trade_id="__scan_diagnostics__::1",
        text="diagnostic row",
        strategy="scan_diagnostics",
    )

    assert journal.last_bankroll_from_entries_log() == 504.25


def test_bnb_and_doge_updown_entries_are_written_to_fill_log(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path / "journals")
    fill_log = tmp_path / "updown_fills.jsonl"
    monkeypatch.setattr(trade_journal_module, "ENTRY_PRICE_LOG", fill_log)
    journal = TradeJournal(session_id="20260531_fill_log", resume_latest=False)

    journal.log_entry(
        trade_id="bnb-no",
        market_id="bnb-updown-1",
        market_question="BNB Up or Down",
        strategy="bnb_macro",
        action="BUY_NO",
        side="BUY",
        outcome="NO",
        size=10.0,
        entry_price=0.43,
        bankroll=500.0,
    )
    journal.log_entry(
        trade_id="doge-yes",
        market_id="doge-updown-1",
        market_question="Dogecoin Up or Down",
        strategy="doge_macro",
        action="BUY_YES",
        side="BUY",
        outcome="YES",
        size=10.0,
        entry_price=0.54,
        bankroll=500.0,
    )

    rows = [json.loads(line) for line in fill_log.read_text().splitlines()]
    assert [row["strategy"] for row in rows] == ["bnb_macro", "doge_macro"]
    assert rows[0]["yes_price"] == pytest.approx(0.57)
    assert rows[1]["yes_price"] == 0.54


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


def test_session_fill_count_matches_journal_not_phantom_filtered_subtotal(
    tmp_path: Path, monkeypatch,
) -> None:
    """get_summary totals must mirror open + closed_trades counts.

    _build_closed_stats drops some rows from WR/PnL (phantom ep+exit≈1); that filter was
    written for YES-quote exits but applies to every close. NO-leg closures can satisfy
    ep+exit≈1 without being phantom; fills were still wrongly excluded from session counters.
    """
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path)
    journal = TradeJournal(session_id="20260510_fillcount", resume_latest=False)
    journal.log_entry(
        trade_id="t-no-quote",
        market_id="m1",
        market_question="Ether up or Down",
        strategy="eth_macro",
        action="BUY_NO",
        side="BUY",
        outcome="NO",
        size=10.0,
        entry_price=0.35,
        bankroll=1000.0,
    )
    # Would be blocked only for YES leg in log_exit; NO leg slips through — still a real exit.
    journal.log_exit(
        trade_id="t-no-quote",
        exit_price=0.65,
        bankroll=1003.0,
        reason="test_resolve",
    )
    summary = journal.get_summary()
    assert summary["total_entries"] == 1
    assert summary["total_exits"] == 1
    assert summary["open_positions"] == 0


def test_buy_no_complementary_exit_price_is_not_phantom() -> None:
    row = {
        "event": "EXIT",
        "action": "BUY_NO",
        "side": "BUY",
        "outcome": "NO",
        "entry_price": 0.395,
        "current_price": 0.6,
        "pnl": 5.1899,
        "extra": {"entry_leg": "NO"},
    }
    assert is_phantom_exit_row(row) is False


def test_yes_complementary_exit_price_is_phantom() -> None:
    row = {
        "event": "EXIT",
        "action": "BUY_YES",
        "side": "BUY",
        "outcome": "YES",
        "entry_price": 0.395,
        "current_price": 0.6,
        "pnl": 5.1899,
        "extra": {"entry_leg": "YES"},
    }
    assert is_phantom_exit_row(row) is True


def test_buy_no_skip_event_is_persisted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path)
    journal = TradeJournal(session_id="20260505_120000", resume_latest=False)
    journal.log_buy_no_skip(
        market_id="sol-updown-1",
        market_question="Solana Up or Down - May 5, 5:00AM-5:05AM ET",
        strategy="sol_macro",
        bankroll=500.0,
        skip_reason="edge_below_min",
        window_size="5m",
        yes_price=0.53,
        edge=0.07,
        effective_min_edge=0.09,
        rsi=28.4,
        htf_bias="NEUTRAL",
        signal_reason="BTC_HTF=NEUTRAL | ALT_HTF=BEARISH | side=SHORT",
        alt_1h_trend="BULLISH",
    )
    entries = journal.get_all_entries(limit=10)
    event = next(entry for entry in entries if entry["event"] == "BUY_NO_SKIP")
    assert event["action"] == "BUY_NO"
    assert event["reason"] == "edge_below_min"
    assert event["extra"]["effective_min_edge"] == 0.09
    assert event["extra"]["alt_1h_trend"] == "BULLISH"


def test_lane_metadata_persists_on_entry_and_exit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path)
    journal = TradeJournal(session_id="20260514_lane_meta", resume_latest=False)
    journal.log_entry(
        trade_id="lane-1",
        market_id="btc-updown-1",
        market_question="Bitcoin Up or Down - test",
        strategy="bitcoin",
        action="BUY_NO",
        side="BUY",
        outcome="NO",
        size=10.0,
        entry_price=0.49,
        bankroll=1000.0,
        extra={
            "window_size": "5m",
            "lane_id": "bitcoin|5m|down|bearish|drift",
            "lane_side": "down",
            "lane_window": "5m",
            "lane_regime": "bearish",
            "entry_family": "drift",
            "promotion_state": "paper",
        },
    )
    journal.log_exit("lane-1", exit_price=0.55, bankroll=1001.0, reason="test_exit")
    entries = journal.get_all_entries(limit=10)
    entry = next(item for item in entries if item["event"] == "ENTRY")
    exit_row = next(item for item in entries if item["event"] == "EXIT")
    assert entry["extra"]["lane_id"] == "bitcoin|5m|down|bearish|drift"
    assert exit_row["extra"]["lane_id"] == "bitcoin|5m|down|bearish|drift"
    assert exit_row["extra"]["promotion_state"] == "paper"


def test_skip_extra_persists_lane_metadata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path)
    journal = TradeJournal(session_id="20260514_skip_lane_meta", resume_latest=False)
    journal.log_skip(
        "btc-updown-2",
        "Bitcoin Up or Down - skip test",
        "bitcoin",
        "lane_paused",
        1000.0,
        extra={
            "lane_id": "bitcoin|5m|down|bearish|drift",
            "promotion_state": "paused",
            "skip_reason": "lane_paused",
        },
    )
    entries = journal.get_all_entries(limit=10)
    skip = next(item for item in entries if item["event"] == "SKIP")
    assert skip["extra"]["lane_id"] == "bitcoin|5m|down|bearish|drift"
    assert skip["extra"]["promotion_state"] == "paused"


def test_open_position_window_size_persists_and_resumes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", tmp_path)
    session_id = "20260514_000001"
    journal = TradeJournal(session_id=session_id, resume_latest=False)
    journal.log_entry(
        trade_id="t1",
        market_id="eth-updown-1",
        market_question="Ethereum Up or Down - test",
        strategy="eth_macro",
        action="BUY_NO",
        side="BUY",
        outcome="NO",
        size=10.0,
        entry_price=0.51,
        bankroll=1000.0,
        extra={"window_size": "5m"},
    )
    assert journal.get_open_positions()[0]["window_size"] == "5m"

    resumed = TradeJournal(session_id=session_id, resume_latest=True)
    assert resumed.get_open_positions()[0]["window_size"] == "5m"
