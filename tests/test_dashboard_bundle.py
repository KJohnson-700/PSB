"""Guard dashboard index.html + HTTP shell so pre-restart checks include the UI bundle."""
from __future__ import annotations

import re
import subprocess
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
    assert 'id="positions-master"' in body and 'id="ops-digest-ticker"' in body and 'id="positions-orderbook-wrap"' in body and 'id="ops-metric-deck-scroll"' in body
    assert 'id="live-shutdown-btn"' not in body
    assert "function shutdownLiveBot" in body
    assert "/api/live/shutdown" in body
    # Backtest UI elements (bt-hud, backtest-output-tail) removed 2026-05-24 with the broken backtester.
    # Ghost Lab tab replaced it.
    assert 'id="view-ghosts"' in body
    assert 'id="gl-clock"' in body
    assert 'id="action-perf-lanes"' in body
    assert "BTC Signals" not in body.split('id="strategy-boxes"')[1].split('id="strategy-table"')[0]

    h = c.get("/health")
    assert h.status_code == 200
    data = h.json()
    assert data.get("status") == "ok"
    assert data.get("dashboard_ui_rev"), "bump dashboard_ui_rev in server.py when shipping HTML/JS"

    snippet = c.get("/api/dashboard/health-snippet")
    assert snippet.status_code == 200
    assert "text/html" in (snippet.headers.get("content-type") or "")
    assert data.get("dashboard_ui_rev") in snippet.text

    gl = c.get("/api/ghosts/lab?since=2026-05-01")
    assert gl.status_code == 200
    assert "no-store" in (gl.headers.get("cache-control") or "").lower()

    gr = c.get("/api/ghosts/regime-breakdown?since=2026-05-01")
    assert gr.status_code == 200
    assert "no-store" in (gr.headers.get("cache-control") or "").lower()
    assert "rows" in gr.json()

    gd = c.get("/api/ghosts/decision-digest?since=2026-05-01")
    assert gd.status_code == 200
    assert "no-store" in (gd.headers.get("cache-control") or "").lower()
    gd_payload = gd.json()
    assert "deadzone_theory" in gd_payload
    assert "ghost_gate" in gd_payload
    assert "calibration" in gd_payload


def test_dashboard_inline_scripts_parse_cleanly():
    html = INDEX.read_text(encoding="utf-8")
    scripts = re.findall(r"<script[^>]*>([\s\S]*?)</script>", html)
    assert scripts, "expected inline dashboard scripts"

    for idx, script in enumerate(scripts):
        result = subprocess.run(
            ["node", "--check"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"dashboard inline script {idx} has a JS syntax error:\n"
            f"{result.stderr or result.stdout}"
        )


def test_api_orderbook_returns_503_without_bot():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from src.dashboard.server import app

    c = TestClient(app)
    r = c.get("/api/orderbook", params={"token_id": "12345678901234567890"})
    assert r.status_code == 503


def test_live_shutdown_without_process_handle_does_not_change_pause_memory(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from src.dashboard import server as dashboard_server

    monkeypatch.setattr(dashboard_server, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(dashboard_server, "DASHBOARD_API_KEY", "test-key")
    monkeypatch.setattr(dashboard_server, "_bot_process", None)
    monkeypatch.setattr(dashboard_server, "bot_instance", None)

    r = TestClient(dashboard_server.app).post(
        "/api/live/shutdown",
        headers={"X-API-Key": "test-key"},
    )

    assert r.status_code == 200
    assert r.json()["status"] == "no_running_bot_handle"
    assert r.json()["kill_switch_active"] is False
    assert not (tmp_path / "KILL_SWITCH").exists()


def test_live_pause_resume_memory_is_persisted_by_kill_switch(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from src.dashboard import server as dashboard_server

    monkeypatch.setattr(dashboard_server, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(dashboard_server, "DASHBOARD_API_KEY", "test-key")
    monkeypatch.setattr(dashboard_server, "_bot_process", None)

    client = TestClient(dashboard_server.app)
    pause = client.post("/api/live/stop", headers={"X-API-Key": "test-key"})
    assert pause.status_code == 200
    assert pause.json()["kill_switch_active"] is True
    assert (tmp_path / "KILL_SWITCH").exists()

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["kill_switch_active"] is True

    resume = client.post("/api/live/resume", headers={"X-API-Key": "test-key"})
    assert resume.status_code == 200
    assert resume.json()["kill_switch_active"] is False
    assert not (tmp_path / "KILL_SWITCH").exists()


def test_command_center_includes_ai_pipeline_digest_stub():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="ops-ai-pipeline"' in html
    assert "function updateCommandCenterDigests" in html


def test_command_center_trading_control_uses_pause_language():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="live-stop-btn" onclick="stopLiveBot()" disabled>Pause Trading</button>' in html
    assert "stopBtn.textContent = halted ? 'Unpause Trading' : 'Pause Trading';" in html
    assert "btn.textContent === 'Unpause Trading'" in html
    assert "Stop Trading" not in html
    assert "Resume Trading" not in html


def test_exposure_tile_labels_consecutive_losses_explicitly():
    html = INDEX.read_text(encoding="utf-8")
    assert "Consec Losses" in html
    assert "live streak" in html


def test_btc_chart_uses_css_variable_overlay_for_trade_markers():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="btc-trade-overlay"' in html
    assert "#btc-trade-overlay{position:absolute;inset:0;" in html
    assert 'class="bbl-layer"' in html
    assert "function stratChipHex(stratKey)" in html
    assert "function stratChipHexForJournal(strategy)" in html
    assert "stratChipHexForJournal(point.strategy)" in html
    assert "dot.style.setProperty('--bbl-fill', markerStyle.fill);" in html
    assert "dot.style.setProperty('--bbl-border', markerStyle.border);" in html
    assert "ring.className = 'bbl-ring';" in html
    assert "tip.className = 'bbl-tip';" in html
    assert "function _drawBTCTradeOverlay()" in html
    assert "_btcChart.timeScale().subscribeVisibleLogicalRangeChange(() => { _queueBTCTradeOverlayDraw(); });" in html
    assert "_setBTCTradeOverlayData(overlayPoints, _candles || []);" in html
    assert "_btcCandleSeries.setMarkers((sabreMarkers || []).slice(-80));" in html


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
    assert "toggleLossKillSwitch()" in html
    assert "toggleDeadZones()" in html
    assert "loss_kill_switch_enabled" in html
    assert "LOSS KILL ON" in html or "LOSS KILL OFF" in html
    assert "DEAD ZONES ON" in html or "DEAD ZONES OFF" in html
    assert 'id="loss-kill-toggle-btn" disabled>Loss Kill: Loading</button>' in html
    assert 'id="dead-zone-toggle-btn" disabled>Dead Zones: Loading</button>' in html
    assert "lossBtn.textContent = lossKillSwitchEnabled ? 'Loss Kill: On' : 'Loss Kill: Off';" in html
    assert "deadBtn.textContent = deadZoneEnabled ? 'Dead Zones: On' : 'Dead Zones: Off';" in html
    assert "kill-trigger" in html
    assert "live-off" in html


def test_action_breakdown_backend_includes_doge_and_bnb():
    server = (REPO / "src" / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert '"doge_macro"' in server
    assert '"bnb_macro"' in server
    assert "_ACTION_BREAKDOWN_STRATEGIES = _DASHBOARD_STRATEGY_NAMES" in server
    assert "_DASHBOARD_STRATEGY_NAMES = ACTIVE_STRATEGY_NAMES + (\"weather\",)" in server


def test_reason_buckets_backend_includes_doge_and_bnb():
    server = (REPO / "src" / "dashboard" / "server.py").read_text(encoding="utf-8")
    start = server.find("async def get_strategy_reason_buckets(")
    assert start != -1, "reason buckets handler missing"
    end = server.find("# 1) Recent ENTRY reasons from journal", start)
    assert end != -1, "reason buckets payload seed missing"
    block = server[start:end]
    assert "strategy: _empty_reason_bucket() for strategy in _DASHBOARD_STRATEGY_NAMES" in block
    assert "_empty_reason_bucket()" in server


def test_kelly_backend_includes_doge_and_bnb():
    server = (REPO / "src" / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert "_KELLY_STRATEGY_KEYS = ACTIVE_STRATEGY_NAMES" in server
    assert '"doge_macro"' in server
    assert '"bnb_macro"' in server


def test_command_center_pnl_chart_uses_bottom_anchor_until_negative():
    html = INDEX.read_text(encoding="utf-8")
    assert "function _commandCenterPnlDomain(values, baseline, minimumVisualRange = 25)" in html
    assert "const crossedNegative = minSeen < 0;" in html
    assert "min: baseline," in html
    assert "max: Math.max(...values, baseline + minimumVisualRange)," in html
    assert "const downRoom = Math.max(Math.abs(minSeen) * 1.2, minimumVisualRange);" in html
    assert "const upRoom = Math.max(Math.abs(maxSeen) * 1.2, minimumVisualRange);" in html
    assert "const domain = _commandCenterPnlDomain(values, baseline, 25);" in html
    assert "const floor = Math.max(observedMag * 1.2, 25);" not in html


def test_kelly_sizer_defaults_include_doge_and_bnb():
    from src.analysis.kelly_sizer import KellySizer

    ks = KellySizer({"strategies": {}})
    assert ks.get_asset_config("doge_macro") is not None
    assert ks.get_asset_config("bnb_macro") is not None
    assert "doge_macro" in ks.get_all_window_stats()
    assert "bnb_macro" in ks.get_all_window_stats()


def test_kelly_payload_pads_missing_live_strategies(monkeypatch):
    from src.dashboard import server as dashboard_server

    class LegacyKellySizer:
        def get_current_streak(self, strategy):
            return 0

        def get_kelly_fraction(self, strategy):
            return 0.15

        def get_all_window_stats(self):
            return {
                "bitcoin": {
                    "5m": {"streak": 1, "wins": 1, "losses": 0, "wr": 100.0, "trades": 1},
                    "15m": {"streak": 0, "wins": 0, "losses": 0, "wr": 0.0, "trades": 0},
                    "30m": {"streak": 0, "wins": 0, "losses": 0, "wr": 0.0, "trades": 0},
                    "1h": {"streak": 0, "wins": 0, "losses": 0, "wr": 0.0, "trades": 0},
                }
            }

    fake_bot = type("Bot", (), {"kelly_sizer": LegacyKellySizer()})()
    monkeypatch.setattr(dashboard_server, "bot_instance", fake_bot)

    payload = dashboard_server._kelly_state_payload()
    assert "doge_macro" in payload
    assert "bnb_macro" in payload
    assert "doge_macro" in payload["_window_stats"]
    assert "bnb_macro" in payload["_window_stats"]
    assert payload["_window_stats"]["doge_macro"]["1h"]["trades"] == 0
    assert payload["_window_stats"]["bnb_macro"]["15m"]["wr"] == 0.0


# Backtest-tab tests removed 2026-05-24 with the broken backtester
# (test_dashboard_crypto_backtest_select_includes_all_bundle,
#  test_backtest_tab_renders_output_tail_and_poll_updates_it,
#  test_startup_auto_backtests_skip_duplicate_session_spec,
#  test_backtest_start_rejects_deprecated_30m_window,
#  test_live_backtest_scope_includes_1h_not_30m,
#  test_backtest_start_all_bundle_accepts_1h_window,
#  test_backtest_start_all_bundle_invokes_bundle_script).
# The replacement Ghost Lab tab is exercised in the index/health asserts above.


def test_ghost_lab_tab_renders():
    """Ghost Lab nav button + view container + key panel widgets are in the HTML."""
    html = INDEX.read_text(encoding="utf-8")
    assert 'data-view="ghosts"' in html
    assert 'id="view-ghosts"' in html
    assert 'id="gl-clock"' in html and 'id="gl-heatmap"' in html
    assert 'id="gl-replay"' in html and 'id="gl-lane-table"' in html
    assert 'id="gl-deadzone-tbody"' in html
    assert "function loadGhostLab" in html
    assert "/api/ghosts/lab" in html
    assert "/api/ghosts/regime-breakdown" in html
    assert "/api/ghosts/decision-digest" in html
    assert "high ghost WR" in html
    assert "LOOSEN" not in html


def test_ghost_lab_regime_breakdown_uses_embedded_ghost_regime_fields():
    from src.dashboard.server import _gl_regime_breakdown

    report = _gl_regime_breakdown(
        [
            {
                "source": "ghost",
                "lane_id": "bitcoin|15m|up|neutral|lane_min_edge",
                "combined_regime": "deadzone_confirmed",
                "regime_source": "market_regime",
                "win": True,
            },
            {
                "source": "ghost",
                "lane_id": "bitcoin|15m|up|neutral|lane_min_edge",
                "combined_regime": "deadzone_confirmed",
                "regime_source": "market_regime",
                "win": False,
            },
            {
                "source": "live",
                "lane_id": "bitcoin|15m|up|neutral|lane_min_edge",
                "combined_regime": "deadzone_confirmed",
                "win": True,
            },
        ]
    )

    assert report["rows"][0]["gate"] == "lane_min_edge"
    assert report["rows"][0]["regime"] == "deadzone_confirmed"
    assert report["rows"][0]["n"] == 2
    assert report["metadata"]["source"] == "rejected_candidates_settled.jsonl.embedded_regime_fields"


def test_ghost_lab_deadzone_theory_compares_skip_and_live_buckets():
    from src.dashboard.server import _gl_deadzone_theory

    report = _gl_deadzone_theory(
        [
            {
                "source": "deadzone_skip",
                "hour_utc": 7,
                "lane_id": "bitcoin|15m|up|neutral|standard",
                "combined_regime": "deadzone_confirmed",
                "win": True,
            }
            for _ in range(5)
        ]
        + [
            {
                "source": "live",
                "hour_utc": 7,
                "lane_id": "bitcoin|15m|up|neutral|standard",
                "combined_regime": "deadzone_confirmed",
                "win": False,
            }
            for _ in range(5)
        ]
    )

    row = report["hours"][0]
    assert row["hour_utc"] == 7
    assert row["combined_regime"] == "deadzone_confirmed"
    assert row["deadzone"]["n"] == 5
    assert row["live"]["n"] == 5
    assert row["verdict"] == "block_hurting"


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
    data = r.json()
    assert data["running"] is False
    assert isinstance(data.get("ts"), int) and data["ts"] > 1700000000


def test_dashboard_status_exposes_latest_loss_kill_trigger(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    class _Mgr:
        lane_name = "SOL"
        loss_kill_switch_enabled = True

        def get_status(self):
            return {
                "paused": True,
                "pause_reason": "3 consecutive losses",
                "last_loss_kill_trigger": {
                    "lane": "SOL",
                    "window_size": "15m",
                    "reason": "3 consecutive losses",
                    "timestamp": "2026-05-21T12:00:00",
                },
            }

    bot = type(
        "Bot",
        (),
        {
            "running": True,
            "config": {"trading": {"dry_run": True}, "exposure": {"loss_kill_switch_enabled": True}},
            "journal": type("Journal", (), {"get_summary": staticmethod(lambda: {})})(),
            "bankroll": 500.0,
            "risk_manager": type(
                "Risk",
                (),
                {
                    "active_positions": {},
                    "can_trade": staticmethod(lambda: (True, "")),
                    "get_portfolio_summary": staticmethod(lambda _bankroll: None),
                },
            )(),
            "ai_agent": type("AI", (), {"api_keys": {}})(),
            "btc_exposure_manager": _Mgr(),
            "sol_exposure_manager": _Mgr(),
            "eth_exposure_manager": None,
            "hype_exposure_manager": None,
            "xrp_exposure_manager": None,
            "doge_exposure_manager": None,
            "bnb_exposure_manager": None,
            "weather_exposure_manager": None,
            "event_exposure_manager": None,
        },
    )()
    monkeypatch.setattr(dashboard_server, "bot_instance", bot)

    r = TestClient(dashboard_server.app).get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert data["loss_pause_active"] is True
    assert data["loss_pause_latest_trigger"]["lane"] == "SOL"
    assert data["loss_pause_latest_trigger"]["window_size"] == "15m"


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


def test_config_get_overlays_effective_runtime_loss_kill_switch(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "exposure:\n  loss_kill_switch_enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(
        dashboard_server,
        "bot_instance",
        SimpleNamespace(
            config={"exposure": {"loss_kill_switch_enabled": False}},
            risk_manager=object(),
            journal=object(),
            btc_exposure_manager=SimpleNamespace(loss_kill_switch_enabled=True),
        ),
    )

    r = TestClient(dashboard_server.app).get("/api/config")
    assert r.status_code == 200
    payload = r.json()
    assert payload["exposure"]["loss_kill_switch_enabled"] is True
    assert payload["exposure"]["_runtime_source"] == "bot"


def test_config_post_preserves_nested_window_lane_overrides(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "trading:\n  exit_rules:\n    updown_overrides:\n      eth_macro: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(dashboard_server, "DASHBOARD_API_KEY", "test-key")

    r = TestClient(dashboard_server.app).post(
        "/api/config",
        headers={"X-API-Key": "test-key"},
        json={
            "trading": {
                "exit_rules": {
                    "updown_overrides": {
                        "eth_macro": {
                            "window_lane_overrides": {
                                "5m": {
                                    "down": {
                                        "updown_stop_loss_pct": 0.14,
                                    }
                                },
                                "1h": {
                                    "up": {
                                        "updown_max_hold_mins": 12,
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
    )
    assert r.status_code == 200
    text = config_path.read_text(encoding="utf-8")
    assert "window_lane_overrides" in text
    assert "updown_stop_loss_pct: 0.14" in text
    assert "updown_max_hold_mins: 12" in text


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


def test_lane_health_endpoint_groups_closed_trades(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    class FakeJournal:
        session_id = "test_session"
        session_dir = None

        def get_closed_trades(self):
            return [
                {
                    "strategy": "bitcoin",
                    "pnl": 2.0,
                    "edge": 0.1,
                    "confidence": 0.6,
                    "size": 10.0,
                    "exit_reason": "take_profit",
                    "entry_signal": {
                        "lane_id": "bitcoin|5m|up|bullish|drift",
                        "lane_side": "up",
                        "lane_window": "5m",
                        "lane_regime": "bullish",
                        "entry_family": "drift",
                        "promotion_state": "live",
                    },
                },
                {
                    "strategy": "bitcoin",
                    "pnl": -1.0,
                    "edge": 0.08,
                    "confidence": 0.55,
                    "size": 10.0,
                    "exit_reason": "updown_stop_loss",
                    "entry_signal": {
                        "lane_id": "bitcoin|5m|up|bullish|drift",
                        "lane_side": "up",
                        "lane_window": "5m",
                        "lane_regime": "bullish",
                        "entry_family": "drift",
                        "promotion_state": "live",
                    },
                },
            ]

    monkeypatch.setattr(dashboard_server, "LANE_CANDIDATE_STATUS_PATH", tmp_path / "lane_candidate_status_health.json")
    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: FakeJournal())
    r = TestClient(dashboard_server.app).get("/api/journal/lane-health")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    lane = data["lanes"][0]
    assert lane["lane_id"] == "bitcoin|5m|up|bullish|drift"
    assert lane["trades"] == 2
    assert lane["top_exit_reason"] == "take_profit"
    assert lane["recommended_state"] in {"paper", "live", "paused"}
    assert "auto_pause_candidate" in lane
    assert "auto_pause_first_seen_at" in lane


def test_lane_states_endpoint_returns_configured_and_observed(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    class FakeJournal:
        session_id = "test_session"
        session_dir = None

        def get_closed_trades(self):
            base_trade = {
                "strategy": "bitcoin",
                "pnl": -1.0,
                "edge": 0.08,
                "confidence": 0.55,
                "size": 10.0,
                "exit_reason": "updown_stop_loss",
                "entry_signal": {
                    "lane_id": "bitcoin|5m|down|bearish|drift",
                    "lane_side": "down",
                    "lane_window": "5m",
                    "lane_regime": "bearish",
                    "entry_family": "drift",
                    "promotion_state": "live",
                },
            }
            return [dict(base_trade) for _ in range(9)]

        def get_all_entries(self, limit=5000):
            return [
                {
                    "event": "ENTRY",
                    "strategy": "bitcoin",
                    "extra": {
                        "lane_id": "bitcoin|5m|down|bearish|drift",
                        "lane_side": "down",
                        "lane_window": "5m",
                    },
                }
            ]

    monkeypatch.setattr(dashboard_server, "LANE_CANDIDATE_STATUS_PATH", tmp_path / "lane_candidate_status_states.json")
    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: FakeJournal())
    monkeypatch.setattr(
        dashboard_server,
        "_load_yaml_config",
        lambda: {
            "trading": {"dry_run": True},
            "lane_management": {
                "enabled": True,
                "default_state": "paper",
                "states": {
                    "bitcoin|5m|down": "live",
                    "eth_macro|15m|up": "live",
                },
            },
        },
    )
    monkeypatch.setattr(dashboard_server, "bot_instance", None)
    r = TestClient(dashboard_server.app).get("/api/journal/lane-states")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True
    lane_ids = [row["lane_id"] for row in data["lanes"]]
    assert "bitcoin|5m|down|bearish|drift" in lane_ids
    assert "eth_macro|15m|up" in lane_ids
    observed = next(row for row in data["lanes"] if row["lane_id"] == "bitcoin|5m|down|bearish|drift")
    assert "recommended_state" in observed
    assert "state_meta" in observed
    assert "auto_pause_candidate" in observed
    assert observed["auto_pause_first_seen_at"]
    assert observed["auto_pause_status"] in {"watch", "ready"}


def test_lane_state_update_endpoint_persists_rule(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    config_path = tmp_path / "settings.yaml"
    audit_path = tmp_path / "lane_state_audit.jsonl"
    config_path.write_text(
        "trading:\n  dry_run: true\nlane_management:\n  enabled: false\n  default_state: paper\n  states: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(dashboard_server, "LANE_STATE_AUDIT_LOG", audit_path)
    monkeypatch.setattr(dashboard_server, "DASHBOARD_API_KEY", "test-key")
    monkeypatch.setattr(dashboard_server, "bot_instance", None)

    r = TestClient(dashboard_server.app).post(
        "/api/lane-state",
        headers={"X-API-Key": "test-key"},
        json={"lane_id": "bitcoin|5m|down", "state": "paused", "source": "dashboard_recommendation", "note": "apply rec"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "saved"
    text = config_path.read_text(encoding="utf-8")
    assert "bitcoin|5m|down: paused" in text
    assert "state_meta:" in text
    assert "updated_via: dashboard_recommendation" in text
    assert data["state_meta"]["review_note"] == "apply rec"
    audit_text = audit_path.read_text(encoding="utf-8")
    assert '"lane_id":"bitcoin|5m|down"' in audit_text
    assert '"source":"dashboard_recommendation"' in audit_text


def test_lane_state_update_endpoint_default_removes_rule(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "trading:\n  dry_run: true\nlane_management:\n  enabled: true\n  default_state: paper\n  states:\n    bitcoin|5m|down: paused\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(dashboard_server, "DASHBOARD_API_KEY", "test-key")
    monkeypatch.setattr(dashboard_server, "bot_instance", None)

    r = TestClient(dashboard_server.app).post(
        "/api/lane-state",
        headers={"X-API-Key": "test-key"},
        json={"lane_id": "bitcoin|5m|down", "state": "default"},
    )
    assert r.status_code == 200
    text = config_path.read_text(encoding="utf-8")
    assert "bitcoin|5m|down: paused" not in text


def test_lane_state_history_endpoint_returns_recent_rows(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    audit_path = tmp_path / "lane_state_audit.jsonl"
    audit_path.write_text(
        '{"timestamp":"2026-05-14T10:00:00Z","lane_id":"bitcoin|5m|down","requested_state":"paused","effective_state":"paused","previous_state":"paper","source":"dashboard_manual","note":"manual pause"}\n'
        '{"timestamp":"2026-05-14T11:00:00Z","lane_id":"eth_macro|15m|up","requested_state":"live","effective_state":"live","previous_state":"paper","source":"dashboard_recommendation","note":"apply rec"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "LANE_STATE_AUDIT_LOG", audit_path)
    r = TestClient(dashboard_server.app).get("/api/lane-state-history?limit=5")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert data["items"][0]["lane_id"] == "eth_macro|15m|up"
    assert data["items"][1]["lane_id"] == "bitcoin|5m|down"


def test_lane_health_ready_live_warning_appends_audit_row(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    class FakeJournal:
        session_id = "test_session"
        session_dir = None

        def get_closed_trades(self):
            base_trade = {
                "strategy": "bitcoin",
                "pnl": -1.0,
                "edge": 0.08,
                "confidence": 0.55,
                "size": 10.0,
                "exit_reason": "updown_stop_loss",
                "entry_signal": {
                    "lane_id": "bitcoin|5m|down|bearish|drift",
                    "lane_side": "down",
                    "lane_window": "5m",
                    "lane_regime": "bearish",
                    "entry_family": "drift",
                    "promotion_state": "live",
                },
            }
            return [dict(base_trade) for _ in range(11)]

    candidate_path = tmp_path / "lane_candidate_status.json"
    audit_path = tmp_path / "lane_state_audit.jsonl"
    monkeypatch.setattr(dashboard_server, "LANE_CANDIDATE_STATUS_PATH", candidate_path)
    monkeypatch.setattr(dashboard_server, "LANE_STATE_AUDIT_LOG", audit_path)
    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: FakeJournal())
    monkeypatch.setattr(
        dashboard_server,
        "_load_yaml_config",
        lambda: {
            "lane_management": {
                "enabled": True,
                "default_state": "paper",
                "states": {"bitcoin|5m|down": "live"},
            }
        },
    )
    monkeypatch.setattr(dashboard_server, "bot_instance", None)
    r = TestClient(dashboard_server.app).get("/api/journal/lane-health")
    assert r.status_code == 200
    audit_text = audit_path.read_text(encoding="utf-8")
    assert '"event_type":"ready_live_warning"' in audit_text
    assert '"source":"auto_pause_ready_live"' in audit_text


def test_dashboard_contains_lane_state_controls():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="lane-health-card"' in html
    assert 'id="lane-history-body"' in html
    assert 'id="lane-filter-ready"' in html
    assert "function setLaneState(laneId, state)" in html
    assert "function setLaneHealthFilter(mode)" in html
    assert "function applyLaneRecommendation(laneId, state)" in html
    assert "function updateLaneHistory(history)" in html
    assert "/api/lane-state" in html
    assert "/api/lane-state-history" in html
    assert "Ready only" in html
    assert "AUTO-PAUSE WATCH" in html
    assert "candidate just detected" in html
    assert "Recommend" in html
    assert "Reviewed" in html
