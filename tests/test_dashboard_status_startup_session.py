from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_status_prefers_new_empty_startup_session_over_old_pnl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    import src.execution.trade_journal as trade_journal_module
    from src.dashboard import server as dashboard_server

    journal_root = tmp_path / "paper_trades"
    old = journal_root / "test_20260607_191739"
    new_empty = journal_root / "test_20260607_204931"
    old.mkdir(parents=True)
    new_empty.mkdir(parents=True)
    (old / "entries.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-06-08T03:18:31Z",
                "event": "EXIT",
                "trade_id": "t1",
                "strategy": "bitcoin",
                "opened_at": "2026-06-08T03:12:31+00:00",
                "closed_at": "2026-06-08T03:18:31+00:00",
                "entry_price": 0.46,
                "current_price": 0.33,
                "bankroll": 504.25,
                "pnl": 4.26,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (old / "summary.json").write_text(
        json.dumps(
            {
                "session_id": old.name,
                "total_entries": 5,
                "total_exits": 5,
                "open_positions": 0,
                "total_cost": 0,
                "realized_pnl": 4.26,
                "total_pnl": 4.26,
            }
        ),
        encoding="utf-8",
    )

    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "trading:\n  dry_run: true\nbacktest:\n  initial_bankroll: 500\n",
        encoding="utf-8",
    )

    dashboard_server._journal_summary_cache.clear()
    dashboard_server._journal_cache.clear()
    dashboard_server.bot_instance = None
    monkeypatch.setattr(trade_journal_module, "JOURNAL_DIR", journal_root)
    monkeypatch.setattr(dashboard_server, "CONFIG_PATH", config_path)

    client = TestClient(dashboard_server.app)
    data = client.get("/api/status").json()

    assert data["session_id"] == new_empty.name
    assert data["bankroll"] == 500
    assert data["total_pnl"] == 0
    assert data["session"]["total_pnl"] == 0

    selected_empty = dashboard_server._empty_startup_session_dir_for_summary(
        {"session_id": new_empty.name, "summary_source": "empty_startup_session"}
    )
    assert selected_empty == new_empty

    trade_points = client.get("/api/journal/trade-points?limit=500").json()
    assert trade_points["session_id"] == new_empty.name
    assert trade_points["points"] == []

    equity_history = client.get("/api/session/equity_history?limit=600").json()
    assert equity_history["session_id"] == new_empty.name
    assert equity_history["points"] == []
