import importlib.util
import json
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parent.parent
    path = root / "scripts" / "compare_paper_to_backtest.py"
    spec = importlib.util.spec_from_file_location("compare_paper_to_backtest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_load_report_prefers_test_section_for_split_mode(tmp_path):
    mod = _load_module()
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "split_mode": True,
                "strategy_base": "eth_macro",
                "window_minutes": 15,
                "test": {"windows_entered": 7, "win_rate": 0.6},
            }
        ),
        encoding="utf-8",
    )

    payload = mod._load_report(report)

    assert payload["windows_entered"] == 7
    assert payload["_split_mode"] is True


def test_infer_strategy_preserves_macro_suffix():
    mod = _load_module()

    assert mod._infer_strategy({"strategy_base": "eth_macro"}, None) == "eth_macro"
    assert mod._infer_strategy({"strategy": "sol_macro"}, None) == "sol_macro"


def test_summarize_journal_filters_strategy_and_window():
    mod = _load_module()
    rows = [
        {
            "event": "EXIT",
            "strategy": "eth_macro",
            "action": "BUY_YES",
            "entry_price": 0.5,
            "pnl": 3.0,
            "reason": "take_profit",
            "extra": {"window_size": "15m", "entry_edge": 0.11},
        },
        {
            "event": "EXIT",
            "strategy": "eth_macro",
            "action": "BUY_NO",
            "entry_price": 0.4,
            "pnl": -2.0,
            "reason": "updown_stop_loss",
            "extra": {"window_size": "5m", "entry_edge": 0.09},
        },
    ]

    summary = mod._summarize_journal(rows, "eth_macro", "15m")

    assert summary["trade_count"] == 1
    assert summary["wins"] == 1
    assert summary["actions"] == {"BUY_YES": 1}


def test_summarize_report_uses_entry_price_bands_not_fill_price():
    mod = _load_module()

    summary = mod._summarize_report(
        {
            "windows_entered": 1,
            "wins": 1,
            "losses": 0,
            "net_pnl": 1.0,
            "win_rate": 1.0,
            "expectancy": 1.0,
            "avg_edge": 0.1,
            "trades": [
                {
                    "action": "BUY_YES",
                    "entry_price": 0.50,
                    "fill_price": 0.56,
                    "pnl": 1.0,
                    "exit_reason": "take_profit",
                }
            ],
        }
    )

    assert summary["entry_price_bands"] == {"0.46-0.54": 1}
