"""Dashboard action_breakdown endpoint — BUY_YES vs BUY_NO session stats."""

from __future__ import annotations

import pytest


def _stub_journal(closed):
    class _J:
        def __init__(self):
            self.session_id = "test-session"
            self.session_dir = None

        def get_closed_trades(self):
            return list(closed)

    return _J()


def test_action_breakdown_counts_and_slipping(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from src.dashboard import server as dashboard_server

    closed = [
        {
            "strategy": "bitcoin",
            "action": "BUY_YES",
            "pnl": 2.0,
            "closed_at": "2026-05-15T10:00:00Z",
        },
        {
            "strategy": "bitcoin",
            "action": "BUY_YES",
            "pnl": 1.5,
            "closed_at": "2026-05-15T10:15:00Z",
        },
        {
            "strategy": "bitcoin",
            "action": "BUY_YES",
            "pnl": -3.0,
            "closed_at": "2026-05-15T10:30:00Z",
        },
        {
            "strategy": "bitcoin",
            "action": "BUY_NO",
            "pnl": 4.0,
            "closed_at": "2026-05-15T11:00:00Z",
        },
        {
            "strategy": "bitcoin",
            "action": "BUY_NO",
            "pnl": 3.0,
            "closed_at": "2026-05-15T11:15:00Z",
        },
        {
            "strategy": "bitcoin",
            "action": "BUY_NO",
            "pnl": 2.5,
            "closed_at": "2026-05-15T11:30:00Z",
        },
        {
            "strategy": "bitcoin",
            "action": "BUY_NO",
            "pnl": -1.0,
            "closed_at": "2026-05-15T11:45:00Z",
        },
    ]
    dashboard_server._action_breakdown_cache.clear()
    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: _stub_journal(closed))

    client = TestClient(dashboard_server.app)
    r = client.get("/api/journal/action_breakdown")
    assert r.status_code == 200, r.text
    data = r.json()

    yes = data["actions"]["BUY_YES"]
    no = data["actions"]["BUY_NO"]
    assert yes["wins"] == 2
    assert yes["losses"] == 1
    assert yes["trades"] == 3
    assert yes["net_pnl"] == pytest.approx(0.5, abs=1e-6)
    assert no["wins"] == 3
    assert no["losses"] == 1
    assert no["trades"] == 4
    assert data["total_closed"] == 7
    assert data["slipping"] is not None
    assert data["slipping"]["action"] == "BUY_YES"
    assert data["slipping"]["reason"] == "lower_net_pnl"
    btc = data["by_strategy"]["bitcoin"]
    assert btc["actions"]["BUY_YES"]["trades"] == 3
    assert btc["actions"]["BUY_NO"]["trades"] == 4
    assert btc["slipping"]["action"] == "BUY_YES"
    assert data["by_strategy"]["sol_macro"]["total_trades"] == 0


def test_action_breakdown_flat_in_denominator(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from src.dashboard import server as dashboard_server

    closed = [
        {"action": "BUY_YES", "pnl": 1.0},
        {"action": "BUY_YES", "pnl": 0.0},
        {"action": "BUY_NO", "pnl": -2.0},
        {"action": "BUY_NO", "pnl": -1.0},
        {"action": "BUY_NO", "pnl": -0.5},
    ]
    dashboard_server._action_breakdown_cache.clear()
    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: _stub_journal(closed))

    client = TestClient(dashboard_server.app)
    data = client.get("/api/journal/action_breakdown").json()
    yes = data["actions"]["BUY_YES"]
    assert yes["flat"] == 1
    assert yes["trades"] == 2
    assert yes["win_rate"] == 0.5
    assert data["slipping"] is None


def test_action_breakdown_empty_journal(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from src.dashboard import server as dashboard_server

    dashboard_server._action_breakdown_cache.clear()
    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: None)
    client = TestClient(dashboard_server.app)
    data = client.get("/api/journal/action_breakdown").json()
    assert data["total_closed"] == 0
    assert data["slipping"] is None
