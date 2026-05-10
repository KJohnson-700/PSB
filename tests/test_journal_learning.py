"""Tests for journal-driven learning aggregation and proposals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis import journal_learning as jl


def test_phantom_exit_filtered() -> None:
    row = {
        "event": "EXIT",
        "pnl": -300,
        "entry_price": 0.5,
        "current_price": 0.48,
        "strategy": "bitcoin",
        "size": 10,
    }
    assert jl._phantom_exit(row) is True
    ok = {
        "event": "EXIT",
        "pnl": 1.5,
        "entry_price": 0.5,
        "current_price": 0.65,
        "strategy": "bitcoin",
        "size": 10,
        "edge": 0.08,
        "extra": {"rsi": 65, "window_size": "15m", "entry_edge": 0.08},
    }
    assert jl._phantom_exit(ok) is False


def test_aggregate_segments_and_edge_gap() -> None:
    rows = []
    for i in range(20):
        win = i % 3 != 0
        rows.append(
            {
                "event": "EXIT",
                "strategy": "bitcoin",
                "pnl": 2.0 if win else -1.5,
                "entry_price": 0.5,
                "size": 10.0,
                "edge": 0.12,
                "extra": {"rsi": 62 if i >= 10 else 45, "window_size": "15m", "entry_edge": 0.12},
            }
        )
    bys = jl.aggregate_by_strategy(rows)
    b = bys["bitcoin"]
    assert b.trades == 20
    assert "rsi_ge_60" in b.segments
    assert b.segments["rsi_ge_60"].trades == 10


def test_iter_exit_events_from_file(tmp_path: Path) -> None:
    p = tmp_path / "entries.jsonl"
    rows = [
        {"event": "ENTRY", "trade_id": "a"},
        {
            "event": "EXIT",
            "trade_id": "a",
            "strategy": "sol_macro",
            "pnl": 1.0,
            "entry_price": 0.4,
            "current_price": 0.5,
            "size": 10,
        },
    ]
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    got = list(jl.iter_exit_events_from_file(p, session_id="sess1"))
    assert len(got) == 1
    assert got[0]["_session_id"] == "sess1"


def test_trade_ai_used_detection() -> None:
    assert jl.trade_ai_used({"extra": {"ai_used": True}}) is True
    assert jl.trade_ai_used({"extra": {"ai_used": "true"}}) is True
    assert jl.trade_ai_used({"extra": {"ai_used": False}}) is False


def test_ai_path_proposal_with_mature_sample() -> None:
    cfg = {
        "strategies": {"bitcoin": {"min_edge": 0.10, "ai_confidence_threshold": 0.60}}
    }
    lc = {
        "ai_proposal_min_trades": 75,
        "ai_proposal_win_rate_below": 0.63,
        "ai_proposal_require_negative_pnl": True,
    }
    rows = []
    wins = int(75 * 0.55)
    for i in range(75):
        win = i < wins
        rows.append(
            {
                "event": "EXIT",
                "strategy": "bitcoin",
                "pnl": 1.5 if win else -2.0,
                "entry_price": 0.5,
                "size": 10,
                "edge": 0.08,
                "extra": {"ai_used": True, "window_size": "15m", "rsi": 50},
            }
        )
    bys = jl.aggregate_by_strategy(rows)
    props = jl.propose_param_updates(
        cfg, bys, [], learning_cfg=lc, min_trades=15, min_segment_trades=15
    )
    ai_props = [p for p in props if p["param"] == "ai_confidence_threshold"]
    assert ai_props, "expected AI cohort proposal"
    assert ai_props[0]["proposed"] > ai_props[0]["current"]


def test_proposals_rsi_bracket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jl, "_REPO_ROOT", tmp_path)
    cfg = {
        "strategies": {
            "bitcoin": {
                "min_edge": 0.10,
                "neutral_rsi_extra_min_edge": 0.02,
            }
        }
    }
    rows = []
    for _ in range(20):
        rows.append(
            {
                "event": "EXIT",
                "strategy": "bitcoin",
                "pnl": -2.0,
                "entry_price": 0.5,
                "size": 10,
                "edge": 0.1,
                "extra": {"rsi": 65, "window_size": "15m", "entry_edge": 0.1},
            }
        )
    for _ in range(20):
        rows.append(
            {
                "event": "EXIT",
                "strategy": "bitcoin",
                "pnl": 2.0,
                "entry_price": 0.5,
                "size": 10,
                "edge": 0.1,
                "extra": {"rsi": 50, "window_size": "15m", "entry_edge": 0.1},
            }
        )
    bys = jl.aggregate_by_strategy(rows)
    props = jl.propose_param_updates(cfg, bys, [], min_trades=15, min_segment_trades=15)
    rsi_props = [p for p in props if p["param"] == "neutral_rsi_extra_min_edge"]
    assert rsi_props
    assert rsi_props[0]["requires_human_review"] is True


def test_run_learning_cycle_writes_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jl, "_REPO_ROOT", tmp_path)
    sess = tmp_path / "data" / "paper_trades" / "testsess"
    sess.mkdir(parents=True)
    row = {
        "event": "EXIT",
        "strategy": "bitcoin",
        "pnl": 1.0,
        "entry_price": 0.5,
        "current_price": 0.55,
        "size": 10,
        "edge": 0.08,
        "extra": {"rsi": 50, "window_size": "5m"},
    }
    with open(sess / "entries.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")

    # Only scan our tmp journal tree — patch JOURNAL_DIR used in iter
    import src.execution.trade_journal as tj_mod

    monkeypatch.setattr(tj_mod, "JOURNAL_DIR", tmp_path / "data" / "paper_trades")

    cfg = {
        "learning_loop": {
            "enabled": True,
            "persist_artifacts": True,
            "write_vault_log": True,
            "proposal_dir": str(tmp_path / "out"),
        },
        "strategies": {"bitcoin": {"min_edge": 0.1}},
    }
    out = jl.run_learning_cycle(cfg, write_files=True, vault_path=tmp_path / "vault.md")
    assert out["exit_events_used"] >= 1
    assert "proposal_json_path" in out
    assert Path(out["proposal_json_path"]).exists()
    assert (tmp_path / "vault.md").exists()
