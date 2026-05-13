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
    fetches = len(re.findall(r"\b(?:fetch|fetchT)\(", block))
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


def test_command_center_includes_ai_pipeline_digest_stub():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="ops-ai-pipeline"' in html
    assert "function updateCommandCenterDigests" in html


def test_command_center_decision_gates_do_not_trap_scroll():
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r"\.ops-mini-body\s*\{([^}]+)\}", html)
    assert m, "ops mini body style missing"
    body_rule = m.group(1)
    assert "overflow-y:auto" not in body_rule.replace(" ", "")
    assert "max-height" not in body_rule
    assert "overflow:visible" in body_rule.replace(" ", "")
    assert "@media (max-height:760px)" in html


def test_command_center_decision_gate_chips_are_atomic():
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r"function formatDecisionGateDigest\(dg\) \{([\s\S]*?)\n\}", html)
    assert m, "decision gate formatter missing"
    formatter = m.group(1)
    assert "oracle ${oracleLanes.slice" not in formatter
    assert "oracleLanes.slice(0, 4).join" not in formatter
    assert "floor default" in formatter
    assert "BTC 15m floor" in formatter
    assert "for (const lane of oracleLanes.slice(0, 4))" in formatter
    assert "overflow-wrap:anywhere" in html


def test_crypto_live_panels_wrap_and_slow_ticker_helpers_exist():
    html = INDEX.read_text(encoding="utf-8")
    server = (REPO / "src" / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert (
        ".crypto-grid .crypto-hero-six .hero-item .hero-val,.crypto-grid .crypto-hero-seven .hero-item .hero-val{"
        in html
    )
    assert "@keyframes dashTickerSlow" in html
    assert "function dashRefreshCryptoLiveTickers()" in html
    assert "function dashApplySlowTickerIfNeeded(el)" in html
    assert "dashRefreshCryptoLiveTickers();" in html
    assert ".crypto-grid.two,.crypto-grid.three{grid-template-columns:1fr}" in html
    assert "dashboard_ui_rev" in server and '"dashboard_ui_rev":' in server


def test_command_center_trades_card_uses_daily_trades_not_session_fills():
    html = INDEX.read_text(encoding="utf-8")
    assert "Trades today (UTC)" in html
    assert "const dailyTrades = Number(p.daily_trades || 0);" in html
    assert "if (tradesEl) tradesEl.textContent = dailyTrades;" in html
    assert "daily_trades: raw.trades_today" in html
    assert "Session fills" in html
    assert "Paper Trade Journal" in html


def test_dashboard_sse_uses_risk_manager_daily_fields():
    server = (REPO / "src" / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert 'int(getattr(rm, "daily_trades", 0) or 0)' in server
    assert 'round(float(getattr(rm, "daily_pnl", 0) or 0), 2)' in server
    assert '"trades_today": daily_trades_n' in server
    assert '"ai_pipeline": ai_pipeline_payload' in server


def test_ai_summary_text_extractor_handles_provider_shapes():
    from src.dashboard.server import _extract_ai_summary_text

    assert _extract_ai_summary_text({"content": [{"type": "text", "text": " good "}]}) == "good"
    assert _extract_ai_summary_text({"content": [{"type": "text", "content": " nested "}]}) == "nested"
    assert _extract_ai_summary_text({"choices": [{"message": {"content": " choice "}}]}) == "choice"
    assert _extract_ai_summary_text({"content": [{"type": "tool_use", "name": "noop"}]}) == ""


def test_ai_summary_text_extractor_hides_thinking_blocks():
    from src.dashboard.server import _extract_ai_summary_text

    assert (
        _extract_ai_summary_text(
            {
                "content": [
                    {"type": "thinking", "text": "internal chain of thought"},
                    {"type": "text", "text": "Operator-safe summary."},
                ]
            }
        )
        == "Operator-safe summary."
    )
    assert _extract_ai_summary_text("<think>private reasoning</think>\nFinal summary.") == "Final summary."


def test_dashboard_contains_operator_toggle_buttons():
    html = INDEX.read_text(encoding="utf-8")
    assert "toggleWeather72hCap()" in html
    assert "toggleDeadZones()" in html
    assert "resolution_window_enabled" in html
    assert "Weather 72h cap:" in html
    assert "Dead zones:" in html


def test_startup_auto_backtests_skip_duplicate_session_spec(monkeypatch):
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
            "risk_manager": object(),
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


def test_dashboard_status_handles_bootstrap_shim(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    shim = type(
        "Shim",
        (),
        {
            "config": {
                "trading": {"dry_run": True},
                "strategies": {"bitcoin": {"enabled": True}},
            }
        },
    )()
    monkeypatch.setattr(dashboard_server, "bot_instance", shim)

    r = TestClient(dashboard_server.app).get("/api/status")
    assert r.status_code == 200
    assert r.json()["running"] is False


def test_resolve_bankroll_snapshot_preserves_real_zero(tmp_path):
    from src.dashboard.server import _resolve_bankroll_snapshot

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    payload = _resolve_bankroll_snapshot(
        0.0,
        session_dir,
        summary_total_pnl=-500.0,
        summary_has_session=True,
        initial_bankroll=500.0,
    )
    assert payload["bankroll"] == 0.0
    assert payload["source"] == "journal"


def test_resolve_bankroll_snapshot_reports_unavailable(tmp_path):
    from src.dashboard.server import _resolve_bankroll_snapshot

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    payload = _resolve_bankroll_snapshot(
        None,
        session_dir,
        summary_total_pnl=0.0,
        summary_has_session=False,
        initial_bankroll=500.0,
    )
    assert payload["bankroll"] is None
    assert payload["source"] == "unavailable"
    assert "Could not resolve bankroll" in payload["warning"]


def test_config_post_fails_closed_without_dashboard_key(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    config_path = tmp_path / "settings.yaml"
    config_path.write_text("trading:\n  dry_run: true\n", encoding="utf-8")
    monkeypatch.setattr(dashboard_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(dashboard_server, "DASHBOARD_API_KEY", "")

    r = TestClient(dashboard_server.app).post(
        "/api/config",
        json={"trading": {"dry_run": False}},
    )
    assert r.status_code == 503
    assert "DASHBOARD_API_KEY required" in r.json()["detail"]


def test_config_post_rejects_unsafe_values_with_auth(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    config_path = tmp_path / "settings.yaml"
    config_path.write_text("strategies:\n  bitcoin:\n    kelly_fraction: 0.1\n", encoding="utf-8")
    monkeypatch.setattr(dashboard_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(dashboard_server, "DASHBOARD_API_KEY", "test-key")

    r = TestClient(dashboard_server.app).post(
        "/api/config",
        headers={"X-API-Key": "test-key"},
        json={"strategies": {"bitcoin": {"kelly_fraction": -0.5}}},
    )
    assert r.status_code == 422
    assert "kelly_fraction" in r.text


def test_config_post_accepts_updown_stop_loss_pct_with_auth(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "trading:\n  dry_run: true\n  exit_rules:\n    updown_stop_loss_pct: 0.2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(dashboard_server, "DASHBOARD_API_KEY", "test-key")

    r = TestClient(dashboard_server.app).post(
        "/api/config",
        headers={"X-API-Key": "test-key"},
        json={"trading": {"exit_rules": {"updown_stop_loss_pct": 0.18}}},
    )
    assert r.status_code == 200
    assert "updown_stop_loss_pct: 0.18" in config_path.read_text(encoding="utf-8")


def test_exit_reason_summary_groups_current_journal(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    class FakeJournal:
        session_id = "test_session"
        session_dir = None

        def get_closed_trades(self):
            return [
                {"strategy": "bitcoin", "exit_reason": "take_profit", "pnl": 1.5},
                {"strategy": "bitcoin", "exit_reason": "updown_stop_loss", "pnl": -2.0},
                {"strategy": "eth_macro", "exit_reason": "updown_stop_loss", "pnl": -1.0},
                {"strategy": "eth_macro", "exit_reason": "", "pnl": 5.0},
            ]

    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: FakeJournal())
    dashboard_server._exit_reason_summary_cache.clear()

    r = TestClient(dashboard_server.app).get("/api/journal/exit-reason-summary")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert data["total_by_reason"]["updown_stop_loss"] == 2
    assert data["by_strategy"]["bitcoin"]["take_profit"] == 1
    assert data["win_loss_by_reason"]["updown_stop_loss"] == {
        "wins": 0,
        "losses": 2,
        "pnl": -3.0,
    }
