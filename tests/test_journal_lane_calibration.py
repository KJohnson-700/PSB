import json
from pathlib import Path

from scripts.journal_lane_calibration import build_report


def test_build_report_groups_by_strategy_window_action(tmp_path: Path) -> None:
    entries = tmp_path / "entries.jsonl"
    rows = [
        {
            "event": "ENTRY",
            "trade_id": "t1",
            "strategy": "xrp_macro",
            "action": "BUY_NO",
            "size": 10.0,
            "entry_price": 0.54,
            "edge": 0.09,
            "confidence": 0.55,
            "extra": {
                "window_size": "5m",
                "minutes_to_market_end": 3,
                "lane_id": "xrp_macro|5m|down|bearish|standard",
                "lane_side": "down",
                "lane_regime": "bearish",
                "entry_family": "standard",
            },
        },
        {"event": "EXIT", "trade_id": "t1", "strategy": "xrp_macro", "pnl": 1.5, "reason": "take_profit"},
        {
            "event": "ENTRY",
            "trade_id": "t2",
            "strategy": "xrp_macro",
            "action": "BUY_YES",
            "size": 10.0,
            "entry_price": 0.46,
            "edge": 0.10,
            "confidence": 0.56,
            "extra": {
                "window_size": "15m",
                "lane_id": "xrp_macro|15m|up|bullish|standard",
                "lane_side": "up",
                "lane_regime": "bullish",
                "entry_family": "standard",
            },
        },
        {"event": "EXIT", "trade_id": "t2", "strategy": "xrp_macro", "pnl": -1.0, "reason": "updown_stop_loss"},
    ]
    entries.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    report = build_report(entries)

    assert report["closed_trades"] == 2
    assert report["lanes"]["xrp_macro|5m|down|bearish|standard"]["pnl"] == 1.5
    assert report["lanes"]["xrp_macro|5m|down|bearish|standard"]["win_rate"] == 1.0
    assert report["lanes"]["xrp_macro|15m|up|bullish|standard"]["exit_reasons"] == {"updown_stop_loss": 1}
    assert report["lanes"]["xrp_macro|5m|down|bearish|standard"]["avg_realized_return_on_notional"] == 0.15
