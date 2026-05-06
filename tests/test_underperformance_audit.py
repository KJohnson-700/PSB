import importlib.util
import json
from pathlib import Path

from src.analysis.underperformance_audit import (
    BuyNoSkipEvent,
    ClosedTrade,
    build_underperformance_report,
)


def _load_script_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "diagnose_underperformance.py"
    spec = importlib.util.spec_from_file_location("diagnose_underperformance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_underperformance_report_flags_exit_and_suppression():
    baseline_rows = [
        ClosedTrade("base", "1", "bitcoin", "BUY_NO", 3.0, "take_profit", 0.11, 0.64, "15m", "BULLISH"),
        ClosedTrade("base", "2", "bitcoin", "BUY_NO", 2.0, "take_profit", 0.10, 0.65, "15m", "BULLISH"),
        ClosedTrade("base", "3", "bitcoin", "BUY_YES", -1.0, "updown_time_stop", 0.10, 0.48, "15m", "BULLISH"),
    ]
    recent_rows = [
        ClosedTrade("recent", "4", "bitcoin", "BUY_YES", -4.0, "updown_time_stop", 0.10, 0.47, "15m", "BULLISH"),
        ClosedTrade("recent", "5", "bitcoin", "BUY_YES", -3.0, "updown_time_stop", 0.09, 0.48, "15m", "BULLISH"),
        ClosedTrade("recent", "6", "bitcoin", "BUY_YES", 1.0, "take_profit", 0.09, 0.46, "15m", "BULLISH"),
    ]
    skip_rows = [
        BuyNoSkipEvent("recent", "bitcoin", "counter_trend_filter", 0.12, 0.08, 0.62, 42.0, "BULLISH", ""),
    ]
    backtests = {
        "bitcoin": {
            "file": "backtest_crypto_BTC_15m_test.json",
            "net_pnl": 20.0,
            "win_rate": 0.58,
            "trades_count": 40,
            "is_reliable_control": True,
            "by_action": {
                "BUY_NO": {"net_pnl": 25.0, "win_rate": 0.60},
                "BUY_YES": {"net_pnl": -5.0, "win_rate": 0.40},
            },
        }
    }

    report = build_underperformance_report(
        baseline_rows=baseline_rows,
        recent_rows=recent_rows,
        skip_rows=skip_rows,
        backtests=backtests,
        baseline_sessions=["base"],
        recent_sessions=["recent"],
    )

    btc = report["per_strategy"]["bitcoin"]
    causes = [item["cause"] for item in btc["ranked_hypotheses"]]
    assert causes[:2] == ["exit_path_damage", "signal_suppression"]
    assert btc["buy_no_skip_events"]["count"] == 1
    assert report["overall"]["recent_buy_yes_time_stop_loss_share_of_negative_pnl"] == 1.0


def test_diagnose_script_writes_outputs(tmp_path, monkeypatch):
    repo_root = tmp_path
    paper_root = repo_root / "data" / "paper_trades"
    reports_dir = repo_root / "data" / "backtest" / "reports"
    out_dir = repo_root / "docs" / "session_reports"
    session_dir = paper_root / "test_20260504_034719"
    recent_dir = paper_root / "test_20260504_150648"
    session_dir.mkdir(parents=True)
    recent_dir.mkdir(parents=True)
    reports_dir.mkdir(parents=True)

    baseline_rows = [
        {
            "timestamp": "2026-05-04T00:00:00+00:00",
            "event": "ENTRY",
            "trade_id": "t1",
            "strategy": "bitcoin",
            "action": "BUY_NO",
            "entry_price": 0.35,
            "current_price": 0.35,
            "pnl": 0,
            "reason": "",
            "extra": {"edge": 0.11, "yes_price": 0.65, "window_size": "15m", "htf_bias": "BULLISH"},
        },
        {
            "timestamp": "2026-05-04T00:15:00+00:00",
            "event": "EXIT",
            "trade_id": "t1",
            "strategy": "bitcoin",
            "action": "BUY_NO",
            "entry_price": 0.35,
            "current_price": 0.50,
            "pnl": 2.5,
            "reason": "take_profit",
            "extra": {"exit_reason": "take_profit"},
        },
    ]
    recent_rows = [
        {
            "timestamp": "2026-05-05T00:00:00+00:00",
            "event": "ENTRY",
            "trade_id": "t2",
            "strategy": "bitcoin",
            "action": "BUY_YES",
            "entry_price": 0.47,
            "current_price": 0.47,
            "pnl": 0,
            "reason": "",
            "extra": {"edge": 0.09, "yes_price": 0.47, "window_size": "15m", "htf_bias": "BULLISH"},
        },
        {
            "timestamp": "2026-05-05T00:01:00+00:00",
            "event": "EXIT",
            "trade_id": "t2",
            "strategy": "bitcoin",
            "action": "BUY_YES",
            "entry_price": 0.47,
            "current_price": 0.40,
            "pnl": -3.0,
            "reason": "updown_time_stop",
            "extra": {"exit_reason": "updown_time_stop"},
        },
    ]
    (session_dir / "entries.jsonl").write_text(
        "\n".join(json.dumps(row) for row in baseline_rows) + "\n",
        encoding="utf-8",
    )
    (recent_dir / "entries.jsonl").write_text(
        "\n".join(json.dumps(row) for row in recent_rows) + "\n",
        encoding="utf-8",
    )
    report = {
        "net_pnl": 10.0,
        "win_rate": 0.55,
        "trades_count": 10,
        "trades": [
            {"action": "BUY_NO", "edge": 0.11, "pnl": 12.0},
            {"action": "BUY_YES", "edge": 0.11, "pnl": -2.0},
        ],
    }
    (reports_dir / "backtest_crypto_BTC_15m_20260505_test.json").write_text(
        json.dumps(report),
        encoding="utf-8",
    )

    module = _load_script_module()
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(
        "sys.argv",
        [
            "diagnose_underperformance.py",
            "--paper-root",
            str(paper_root),
            "--reports-dir",
            str(reports_dir),
            "--out-dir",
            str(out_dir),
            "--label",
            "diagnosis_test",
            "--recent-sessions",
            "test_20260504_150648",
        ],
    )
    module.main()

    md_path = out_dir / "diagnosis_test.md"
    json_path = out_dir / "diagnosis_test.json"
    assert md_path.exists()
    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["meta"]["recent_sessions"] == ["test_20260504_150648"]
    assert payload["per_strategy"]["bitcoin"]["ranked_hypotheses"][0]["cause"] == "exit_path_damage"
