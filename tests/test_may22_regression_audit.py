import json
from pathlib import Path

from scripts.may22_regression_audit import (
    build_report,
    select_gold_sessions,
    select_recent_sessions,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _entry(trade_id: str, strategy: str, size: float, action: str = "BUY_NO") -> dict:
    return {
        "event": "ENTRY",
        "trade_id": trade_id,
        "strategy": strategy,
        "action": action,
        "size": size,
        "entry_price": 0.54,
        "extra": {
            "lane_id": "bearish_dip_default",
            "lane_family": "bearish_dip",
            "window_size": "15m",
            "entry_policy": {"name": "lane", "size_multiplier": 0.5},
        },
    }


def _exit(trade_id: str, strategy: str, pnl: float, reason: str, action: str = "BUY_NO") -> dict:
    return {
        "event": "EXIT",
        "trade_id": trade_id,
        "strategy": strategy,
        "action": action,
        "size": 999.0,
        "entry_price": 0.54,
        "current_price": 0.72,
        "pnl": pnl,
        "reason": reason,
        "extra": {"lane_side": "NO"},
    }


def test_build_report_compares_trade_economics(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper_trades"
    _write_jsonl(
        paper_root / "base" / "entries.jsonl",
        [
            _entry("b1", "bitcoin", 20.0),
            _exit("b1", "bitcoin", 10.0, "take_profit"),
            _entry("b2", "bitcoin", 10.0),
            _exit("b2", "bitcoin", -4.0, "updown_stop_loss"),
        ],
    )
    _write_jsonl(
        paper_root / "now" / "entries.jsonl",
        [
            _entry("n1", "bitcoin", 8.0),
            _exit("n1", "bitcoin", 5.0, "take_profit"),
            _entry("n2", "bitcoin", 8.0),
            _exit("n2", "bitcoin", -5.0, "updown_stop_loss"),
        ],
    )

    report = build_report(paper_root, ["base"], ["now"])
    strategy = {
        row["group"]: row for row in report["comparisons"]["strategy"]
    }["bitcoin"]

    assert report["baseline_closed_trades"] == 2
    assert report["current_closed_trades"] == 2
    assert strategy["baseline_avg_win"] == 10.0
    assert strategy["baseline_avg_loss"] == -4.0
    assert strategy["baseline_win_loss_ratio"] == 2.5
    assert strategy["current_win_loss_ratio"] == 1.0
    assert strategy["delta_ratio_pct"] == -0.6
    assert strategy["baseline_avg_size"] == 15.0
    assert strategy["current_avg_size"] == 8.0
    assert round(strategy["delta_size_pct"], 6) == -0.466667


def test_build_report_keeps_direction_mix(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper_trades"
    _write_jsonl(
        paper_root / "base" / "entries.jsonl",
        [
            _entry("b1", "xrp_macro", 12.0, "BUY_NO"),
            _exit("b1", "xrp_macro", 3.0, "take_profit", "BUY_NO"),
            _entry("b2", "xrp_macro", 12.0, "BUY_YES"),
            _exit("b2", "xrp_macro", -2.0, "updown_stop_loss", "BUY_YES"),
        ],
    )
    _write_jsonl(
        paper_root / "now" / "entries.jsonl",
        [
            _entry("n1", "xrp_macro", 12.0, "BUY_NO"),
            _exit("n1", "xrp_macro", -1.0, "updown_stop_loss", "BUY_NO"),
        ],
    )

    report = build_report(paper_root, ["base"], ["now"])
    action_rows = {
        row["group"]: row for row in report["comparisons"]["strategy_action"]
    }

    assert action_rows["xrp_macro::BUY_NO"]["baseline_trades"] == 1
    assert action_rows["xrp_macro::BUY_NO"]["current_trades"] == 1
    assert action_rows["xrp_macro::BUY_YES"]["baseline_trades"] == 1
    assert action_rows["xrp_macro::BUY_YES"]["current_trades"] == 0


def test_select_gold_sessions_is_deterministic_by_pnl(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper_trades"
    for session_id, pnl in [
        ("test_20260522_010000", 5.0),
        ("test_20260522_020000", 12.0),
        ("test_20260522_030000", 9.0),
        ("test_20260521_010000", 100.0),
    ]:
        _write_jsonl(
            paper_root / session_id / "entries.jsonl",
            [
                _entry(f"{session_id}-1", "bitcoin", 10.0),
                _exit(f"{session_id}-1", "bitcoin", pnl, "take_profit"),
            ],
        )

    selected, rule, summaries = select_gold_sessions(
        paper_root,
        baseline="5/22",
        top_n=2,
        min_trades=1,
    )

    assert selected == ["test_20260522_020000", "test_20260522_030000"]
    assert "top 2 sessions by realized PnL on 5/22" in rule
    assert [row.session_id for row in summaries] == [
        "test_20260522_020000",
        "test_20260522_030000",
        "test_20260522_010000",
    ]


def test_select_recent_sessions_excludes_baseline_and_returns_selected_only(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper_trades"
    for session_id in [
        "test_20260522_020000",
        "test_20260524_010000",
        "test_20260525_010000",
        "test_20260526_010000",
    ]:
        _write_jsonl(
            paper_root / session_id / "entries.jsonl",
            [
                _entry(f"{session_id}-1", "bitcoin", 10.0),
                _exit(f"{session_id}-1", "bitcoin", 1.0, "take_profit"),
            ],
        )

    selected, _, summaries = select_recent_sessions(
        paper_root,
        top_n=2,
        min_trades=1,
        exclude_sessions=["test_20260522_020000"],
    )

    assert selected == ["test_20260526_010000", "test_20260525_010000"]
    assert [row.session_id for row in summaries] == selected


def test_build_report_hypothesis_ledger_separates_dimensions(tmp_path: Path) -> None:
    paper_root = tmp_path / "paper_trades"
    baseline_rows = []
    current_rows = []
    for i in range(15):
        baseline_rows.append(_entry(f"b{i}", "bitcoin", 20.0))
        baseline_rows.append(
            _exit(
                f"b{i}",
                "bitcoin",
                10.0 if i % 2 == 0 else -4.0,
                "take_profit" if i % 2 == 0 else "updown_stop_loss",
            )
        )
        current_rows.append(_entry(f"n{i}", "bitcoin", 12.0))
        current_rows.append(
            _exit(
                f"n{i}",
                "bitcoin",
                5.0 if i % 5 == 0 else -5.0,
                "take_profit" if i % 5 == 0 else "updown_stop_loss",
            )
        )
    _write_jsonl(
        paper_root / "base" / "entries.jsonl",
        baseline_rows,
    )
    _write_jsonl(
        paper_root / "now" / "entries.jsonl",
        current_rows,
    )

    report = build_report(paper_root, ["base"], ["now"])
    ledger = {row["strategy"]: row for row in report["hypothesis_ledger"]}

    assert ledger["bitcoin"]["classification"] == "sizing+exit"
    assert ledger["bitcoin"]["standing"] == "partly_explained_by_known_sizing_revert"
