"""Guard dashboard index.html + HTTP shell so pre-restart checks include the UI bundle."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "src" / "dashboard" / "index.html"


def _fetchall_promise_all_block(html: str) -> tuple[list[str], int]:
    """Parse fetchAll()'s main Promise.all — 18-way poll; tolerate let + split try/catch."""
    m = re.search(
        r"(?:const )?\[([^\]]+)\]\s*=\s*await Promise\.all\(\[([\s\S]*?)\]\);\s*\n\s*\} catch",
        html,
    )
    assert m, "fetchAll() Promise.all block not found (expected `] = await Promise.all([` then `} catch`)"
    names = [x.strip() for x in m.group(1).split(",")]
    block = m.group(2)
    fetches = len(re.findall(r"\bfetch\(", block))
    return names, fetches


def test_dashboard_index_fetchall_bind_count_matches_fetch_calls():
    html = INDEX.read_text(encoding="utf-8")
    names, fetches = _fetchall_promise_all_block(html)
    assert len(names) == fetches, (
        f"fetchAll destructuring has {len(names)} vars but Promise.all has {fetches} "
        f"fetch() calls — missing/extra binding breaks the whole dashboard in-browser."
    )


def test_dashboard_index_serves_and_health_has_ui_rev():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from src.dashboard.server import app

    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in (r.headers.get("content-type") or "")
    body = r.text
    assert "fetchAll" in body and "Command Center" in body

    h = c.get("/health")
    assert h.status_code == 200
    data = h.json()
    assert data.get("status") == "ok"
    assert data.get("dashboard_ui_rev"), "bump dashboard_ui_rev in server.py when shipping HTML/JS"

    snippet = c.get("/api/dashboard/health-snippet")
    assert snippet.status_code == 200
    assert "text/html" in (snippet.headers.get("content-type") or "")
    assert data.get("dashboard_ui_rev") in snippet.text


def test_command_center_trades_card_uses_daily_trades_not_session_fills():
    html = INDEX.read_text(encoding="utf-8")
    assert "Trades today (UTC)" in html
    assert "const dailyTrades = Number(p.daily_trades || 0);" in html
    assert "if (tradesEl) tradesEl.textContent = dailyTrades;" in html
    assert "fills this session" in html


def test_dashboard_contains_operator_toggle_buttons():
    html = INDEX.read_text(encoding="utf-8")
    assert "toggleWeather72hCap()" in html
    assert "toggleDeadZones()" in html
    assert "resolution_window_enabled" in html
    assert "Weather 72h cap:" in html
    assert "Dead zones:" in html


def test_startup_auto_backtests_skip_duplicate_session_spec(monkeypatch):
    pytest.importorskip("uvicorn")
    from src.dashboard import server as dashboard_server

    fake_bot = type(
        "Bot",
        (),
        {
            "config": {
                "trading": {"dry_run": True},
                "dashboard": {
                    "auto_sol5_backtest_on_startup": True,
                    "auto_weather_backtest_on_startup": False,
                },
            },
            "journal": type("Journal", (), {"session_id": "test_session"})(),
        },
    )()
    monkeypatch.setattr(dashboard_server, "bot_instance", fake_bot)
    dashboard_server._auto_startup_backtests_started.clear()

    started = []

    def _fake_start(cmd_args, summary):
        started.append(summary)
        return {"status": "started", "job_id": f"job{len(started)}", "pid": 100 + len(started), "summary": summary}

    monkeypatch.setattr(dashboard_server, "_start_backtest_job", _fake_start)

    first = dashboard_server._maybe_start_auto_backtests("startup")
    second = dashboard_server._maybe_start_auto_backtests("startup")

    assert len(first) == 1
    assert first[0]["status"] == "started"
    assert len(second) == 1
    assert second[0]["status"] == "skipped"
    assert second[0]["reason"] == "startup_dedupe"
    assert started == ["SOL 5m crypto [auto-on-startup:test_session]"]
