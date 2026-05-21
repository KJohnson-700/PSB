import json
from pathlib import Path

from scripts.probability_diagnostics import build_report, load_resolved_trades, write_artifacts


def test_build_report_includes_brier_baselines_and_time_aware_folds(tmp_path: Path) -> None:
    root = tmp_path / "paper_trades"
    session = root / "test_session"
    session.mkdir(parents=True)
    entries = session / "entries.jsonl"
    rows = [
        {
            "timestamp": "2026-05-20T00:00:00+00:00",
            "event": "EXIT",
            "trade_id": "t1",
            "strategy": "eth_macro",
            "action": "BUY_YES",
            "entry_price": 0.46,
            "reason": "RESOLVED:YES (real)",
            "extra": {"window_size": "5m", "est_prob": 0.62, "yes_price": 0.46, "outcome_won": "YES"},
        },
        {
            "timestamp": "2026-05-20T00:10:00+00:00",
            "event": "EXIT",
            "trade_id": "t2",
            "strategy": "eth_macro",
            "action": "BUY_NO",
            "entry_price": 0.54,
            "reason": "RESOLVED:NO (real)",
            "extra": {"window_size": "5m", "raw_est_prob": 0.40, "yes_price": 0.54, "outcome_won": "NO"},
        },
        {
            "timestamp": "2026-05-20T00:20:00+00:00",
            "event": "EXIT",
            "trade_id": "t3",
            "strategy": "eth_macro",
            "action": "BUY_YES",
            "entry_price": 0.51,
            "reason": "take_profit",
            "extra": {"window_size": "5m", "est_prob": 0.58, "yes_price": 0.51},
        },
        {
            "timestamp": "2026-05-20T00:30:00+00:00",
            "event": "EXIT",
            "trade_id": "t4",
            "strategy": "bitcoin",
            "action": "BUY_NO",
            "entry_price": 0.57,
            "reason": "RESOLVED:NO (real)",
            "extra": {"window_size": "15m", "est_prob": 0.43, "yes_price": 0.57, "outcome_won": "NO"},
        },
    ]
    entries.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    report = build_report(root=root, n_splits=3, purge_minutes=5)

    assert report["eligible_resolved_trades"] == 3
    assert report["all_exits_after_cutoff"] == 4
    assert report["overall"]["brier_model"] > 0
    assert report["overall"]["brier_market"] > 0
    assert report["overall"]["brier_table"] > 0
    assert {row["lane"] for row in report["lanes"]} == {
        "bitcoin|15m|DOWN",
        "eth_macro|5m|DOWN",
        "eth_macro|5m|UP",
    }
    assert report["time_aware_folds"]


def test_write_artifacts_creates_report_and_svg(tmp_path: Path) -> None:
    root = tmp_path / "paper_trades"
    session = root / "test_session"
    session.mkdir(parents=True)
    entries = session / "entries.jsonl"
    rows = [
        {
            "timestamp": "2026-05-20T00:00:00+00:00",
            "event": "EXIT",
            "trade_id": "t1",
            "strategy": "eth_macro",
            "action": "BUY_YES",
            "entry_price": 0.46,
            "reason": "RESOLVED:YES (real)",
            "extra": {"window_size": "5m", "est_prob": 0.62, "yes_price": 0.46, "outcome_won": "YES"},
        },
        {
            "timestamp": "2026-05-20T00:10:00+00:00",
            "event": "EXIT",
            "trade_id": "t2",
            "strategy": "eth_macro",
            "action": "BUY_NO",
            "entry_price": 0.54,
            "reason": "RESOLVED:NO (real)",
            "extra": {"window_size": "5m", "est_prob": 0.40, "yes_price": 0.54, "outcome_won": "NO"},
        },
    ]
    entries.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    report = build_report(root=root, n_splits=2, purge_minutes=5)
    trades, _ = load_resolved_trades(root=root)
    out_dir = tmp_path / "out"
    write_artifacts(report, trades, out_dir=out_dir, bucket_width=0.05)

    assert (out_dir / "report.json").is_file()
    assert (out_dir / "report.md").is_file()
    assert (out_dir / "reliability_pooled.svg").is_file()
