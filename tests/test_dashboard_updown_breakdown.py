"""Verify the updown win-rate card includes flat trades in the denominator.

Prior bug: `if pnl > 0.01: wins += 1; elif pnl < -0.01: losses += 1` silently
dropped break-even trades (|pnl| <= 0.01) from BOTH numerator and denominator.
After the fix, flat trades land in their own bucket and `trades = wins + losses + flat`.
"""

from __future__ import annotations

import pytest


def _stub_journal(closed):
    """Minimal journal stub matching the attributes the endpoint touches."""

    class _J:
        def get_closed_trades(self):
            return list(closed)

    return _J()


def _bucket_for(data: dict, key: str) -> dict | None:
    """Pull the named bucket from whichever code-era the endpoint placed it in."""
    return (data.get("new_code") or {}).get(key) or (data.get("old_code") or {}).get(key)


def test_flat_trades_count_in_denominator(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    closed = [
        # Two clear wins, one clear loss, one flat (break-even) — all BTC 15m updown.
        {
            "strategy": "bitcoin",
            "market_id": "m_w1",
            "market_question": "Bitcoin Up or Down - 5:00PM-5:15PM ET",
            "pnl": 3.0,
            "closed_at": "2026-05-15T22:00:00Z",
        },
        {
            "strategy": "bitcoin",
            "market_id": "m_w2",
            "market_question": "Bitcoin Up or Down - 5:15PM-5:30PM ET",
            "pnl": 1.5,
            "closed_at": "2026-05-15T22:15:00Z",
        },
        {
            "strategy": "bitcoin",
            "market_id": "m_l1",
            "market_question": "Bitcoin Up or Down - 5:30PM-5:45PM ET",
            "pnl": -2.0,
            "closed_at": "2026-05-15T22:30:00Z",
        },
        {
            "strategy": "bitcoin",
            "market_id": "m_f1",
            "market_question": "Bitcoin Up or Down - 5:45PM-6:00PM ET",
            "pnl": 0.0,  # break-even
            "closed_at": "2026-05-15T22:45:00Z",
        },
    ]

    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: _stub_journal(closed))

    client = TestClient(dashboard_server.app)
    r = client.get("/api/journal/updown_breakdown")
    assert r.status_code == 200, r.text
    data = r.json()

    bucket = _bucket_for(data, "BTC_updown_15m")
    assert bucket is not None, (
        f"BTC_updown_15m missing; old={list((data.get('old_code') or {}).keys())} "
        f"new={list((data.get('new_code') or {}).keys())}"
    )

    # Wins + losses + flat must equal total trades (denominator no longer drops flat).
    assert bucket["wins"] == 2
    assert bucket["losses"] == 1
    assert bucket["flat"] == 1
    assert bucket["trades"] == 4
    # Win-rate is honest: 2/4 = 0.5, not 2/3 ≈ 0.667 (the buggy denominator).
    assert bucket["win_rate"] == 0.5
    # PnL still aggregates over all four.
    assert bucket["pnl"] == pytest.approx(2.5, abs=1e-6)


def test_no_flat_field_when_zero_flat(monkeypatch):
    """When there are no flat trades, the field is still present (just 0)."""
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    closed = [
        {
            "strategy": "bitcoin",
            "market_id": "m1",
            "market_question": "Bitcoin Up or Down - 1:00PM-1:15PM ET",
            "pnl": 1.0,
            "closed_at": "2026-05-15T18:00:00Z",
        },
    ]

    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: _stub_journal(closed))
    client = TestClient(dashboard_server.app)
    r = client.get("/api/journal/updown_breakdown")
    assert r.status_code == 200
    bucket = _bucket_for(r.json(), "BTC_updown_15m")
    assert bucket is not None
    assert bucket["flat"] == 0
    assert bucket["trades"] == 1
    assert bucket["win_rate"] == 1.0
