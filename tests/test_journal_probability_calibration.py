import json
from pathlib import Path

from scripts.journal_probability_calibration import build_report


def test_build_report_groups_by_bucket_strategy_and_side_source(tmp_path: Path) -> None:
    entries = tmp_path / "entries.jsonl"
    rows = [
        {
            "event": "ENTRY",
            "trade_id": "t1",
            "strategy": "eth_macro",
            "action": "BUY_NO",
            "reason": "entry one",
            "extra": {
                "window_size": "5m",
                "est_prob": 0.38,
                "raw_est_prob": 0.38,
                "side_source": "alt_1h_legacy_btc_mode",
            },
        },
        {
            "event": "EXIT",
            "trade_id": "t1",
            "strategy": "eth_macro",
            "reason": "RESOLVED:NO (real)",
            "extra": {"outcome_won": "NO"},
        },
        {
            "event": "ENTRY",
            "trade_id": "t2",
            "strategy": "eth_macro",
            "action": "BUY_YES",
            "reason": "entry two",
            "extra": {
                "window_size": "15m",
                "est_prob": 0.62,
                "raw_est_prob": 0.62,
                "side_source": "eth_1h_primary",
            },
        },
        {
            "event": "EXIT",
            "trade_id": "t2",
            "strategy": "eth_macro",
            "reason": "RESOLVED:YES (real)",
            "extra": {"outcome_won": "YES"},
        },
        {
            "event": "ENTRY",
            "trade_id": "t3",
            "strategy": "eth_macro",
            "action": "BUY_NO",
            "reason": "entry three",
            "extra": {
                "window_size": "5m",
                "est_prob": 0.41,
                "raw_est_prob": 0.41,
                "side_source": "alt_1h_legacy_btc_mode",
            },
        },
        {
            "event": "EXIT",
            "trade_id": "t3",
            "strategy": "eth_macro",
            "reason": "updown_stop_loss",
            "extra": {},
        },
    ]
    entries.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    report = build_report(entries, bucket_width=0.1)

    assert report["eligible_trades"] == 2
    assert report["overall"]["avg_est_prob"] == 0.5
    assert report["overall"]["actual_yes_rate"] == 0.5
    assert report["buckets"]["0.30-0.40"]["trades"] == 1
    assert report["buckets"]["0.60-0.70"]["trades"] == 1
    assert report["by_side_source"]["alt_1h_legacy_btc_mode"]["actual_yes_rate"] == 0.0
    assert report["by_strategy_side_source"]["eth_macro|eth_1h_primary"]["actual_yes_rate"] == 1.0
