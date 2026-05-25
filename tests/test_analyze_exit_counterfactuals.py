import json
from pathlib import Path

import pytest

from scripts.analyze_exit_counterfactuals import analyze_trade, build_report


def _row(**kwargs):
    return kwargs


def test_analyze_trade_reconstructs_mixed_journal_rows_buy_yes() -> None:
    rows = [
        _row(
            timestamp="2026-05-24T13:00:00+00:00",
            event="ENTRY",
            trade_id="t1",
            market_id="m1",
            market_question="Bitcoin Up or Down - May 24, 9:00AM-9:05AM ET",
            strategy="bitcoin",
            action="BUY_YES",
            side="BUY",
            outcome="YES",
            size=10.0,
            entry_price=0.50,
            current_price=0.50,
            extra={
                "window_size": "5m",
                "lane_id": "bitcoin|5m|up|bullish|standard",
                "entry_leg": "YES",
            },
        ),
        _row(
            timestamp="2026-05-24T13:01:00+00:00",
            event="PRICE_UPDATE",
            trade_id="t1",
            current_price=0.60,
            entry_price=0.50,
        ),
        _row(
            timestamp="2026-05-24T13:02:00+00:00",
            event="EXIT",
            trade_id="t1",
            market_question="Bitcoin Up or Down - May 24, 9:00AM-9:05AM ET",
            strategy="bitcoin",
            action="BUY_YES",
            side="BUY",
            outcome="YES",
            size=10.0,
            entry_price=0.50,
            current_price=0.58,
            pnl=0.8,
            reason="take_profit",
        ),
        _row(
            timestamp="2026-05-24T13:03:00+00:00",
            event="PRICE_UPDATE",
            trade_id="t1",
            current_price=0.80,
            entry_price=0.50,
        ),
    ]

    trade = analyze_trade(rows, tp_pct=0.20, sl_pct=0.20)

    assert trade is not None
    assert trade.actual_pnl == pytest.approx(0.8)
    assert trade.hold_pnl == pytest.approx(3.0)
    assert trade.mfe == pytest.approx(3.0)
    assert trade.mae == pytest.approx(0.0)
    assert trade.profit_capture_ratio == pytest.approx(0.2667)
    assert trade.winner_exit_class == "premature_take_profit"
    assert trade.triple_barrier_label == "profit_barrier"


def test_analyze_trade_uses_traded_token_coordinates_for_buy_no() -> None:
    rows = [
        _row(
            timestamp="2026-05-24T13:00:00+00:00",
            event="ENTRY",
            trade_id="t2",
            market_id="m2",
            market_question="XRP Up or Down - May 24, 9:00AM-9:05AM ET",
            strategy="xrp_macro",
            action="BUY_NO",
            side="BUY",
            outcome="NO",
            size=20.0,
            entry_price=0.40,
            current_price=0.40,
            extra={
                "window_size": "5m",
                "lane_id": "xrp_macro|5m|down|bearish|standard",
                "entry_leg": "NO",
            },
        ),
        _row(
            timestamp="2026-05-24T13:01:00+00:00",
            event="PRICE_UPDATE",
            trade_id="t2",
            current_price=0.30,
            entry_price=0.40,
        ),
        _row(
            timestamp="2026-05-24T13:02:00+00:00",
            event="EXIT",
            trade_id="t2",
            market_question="XRP Up or Down - May 24, 9:00AM-9:05AM ET",
            strategy="xrp_macro",
            action="BUY_NO",
            side="BUY",
            outcome="NO",
            size=20.0,
            entry_price=0.40,
            current_price=0.50,
            pnl=2.0,
            reason="take_profit",
        ),
        _row(
            timestamp="2026-05-24T13:04:00+00:00",
            event="PRICE_UPDATE",
            trade_id="t2",
            current_price=0.70,
            entry_price=0.40,
        ),
    ]

    trade = analyze_trade(rows, tp_pct=0.25, sl_pct=0.20)

    assert trade is not None
    assert trade.actual_pnl == pytest.approx(2.0)
    assert trade.hold_pnl == pytest.approx(6.0)
    assert trade.mfe == pytest.approx(6.0)
    assert trade.mae == pytest.approx(2.0)
    assert trade.profit_capture_ratio == pytest.approx(0.3333)
    assert trade.winner_exit_class == "premature_take_profit"
    assert trade.triple_barrier_label == "stop_barrier"


def test_build_report_groups_lanes_and_summarizes_classes(tmp_path: Path) -> None:
    entries = tmp_path / "entries.jsonl"
    rows = [
        {
            "timestamp": "2026-05-24T13:00:00+00:00",
            "event": "ENTRY",
            "trade_id": "t1",
            "market_id": "m1",
            "market_question": "Bitcoin Up or Down - May 24, 9:00AM-9:05AM ET",
            "strategy": "bitcoin",
            "action": "BUY_YES",
            "side": "BUY",
            "outcome": "YES",
            "size": 10.0,
            "entry_price": 0.50,
            "current_price": 0.50,
            "extra": {
                "window_size": "5m",
                "lane_id": "bitcoin|5m|up|bullish|standard",
                "entry_leg": "YES",
            },
        },
        {
            "timestamp": "2026-05-24T13:01:00+00:00",
            "event": "EXIT",
            "trade_id": "t1",
            "market_question": "Bitcoin Up or Down - May 24, 9:00AM-9:05AM ET",
            "strategy": "bitcoin",
            "action": "BUY_YES",
            "side": "BUY",
            "outcome": "YES",
            "size": 10.0,
            "entry_price": 0.50,
            "current_price": 0.60,
            "pnl": 1.0,
            "reason": "take_profit",
        },
        {
            "timestamp": "2026-05-24T13:02:00+00:00",
            "event": "PRICE_UPDATE",
            "trade_id": "t1",
            "current_price": 0.80,
            "entry_price": 0.50,
        },
    ]
    entries.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    report = build_report(entries, tp_pct=0.20, sl_pct=0.20)

    assert report["eligible_trades"] == 1
    lane = report["lanes"]["bitcoin|5m|up|bullish|standard"]
    assert lane["actual_pnl"] == pytest.approx(1.0)
    assert lane["hold_pnl"] == pytest.approx(3.0)
    assert lane["regret"] == pytest.approx(2.0)
    assert lane["classes"] == {"premature_take_profit": 1}
