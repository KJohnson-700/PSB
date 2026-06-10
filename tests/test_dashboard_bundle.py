"""Guard dashboard index.html + HTTP shell so pre-restart checks include the UI bundle."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "src" / "dashboard" / "index.html"


def _fetchall_promise_all_block(html: str) -> tuple[list[str], int]:
    """Parse fetchAll()'s main Promise.all; tolerate let + split try/catch.

    The invariant that keeps the in-browser destructuring aligned is
    ``#bindings == #Promise.all array elements`` — NOT ``#fetch calls``. An element
    can legitimately be a bare ``Promise.resolve(null)`` placeholder (a view-gated
    or intentionally-disabled poll, e.g. /api/scans/latest), so count top-level
    array elements (commas at paren/brace/bracket depth 0), not fetch() calls.
    """
    m = re.search(
        r"(?:const )?\[([^\]]+)\]\s*=\s*await Promise\.all\(\[([\s\S]*?)\]\);\s*\n\s*\} catch",
        html,
    )
    assert m, "fetchAll() Promise.all block not found (expected `] = await Promise.all([` then `} catch`)"
    names = [x.strip() for x in m.group(1).split(",")]
    block = m.group(2)
    segments, depth, cur = [], 0, []
    for ch in block:
        if ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            segments.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    segments.append("".join(cur))
    elements = sum(1 for s in segments if s.strip())  # ignores trailing comma
    return names, elements


def test_dashboard_index_fetchall_bind_count_matches_fetch_calls():
    html = INDEX.read_text(encoding="utf-8")
    names, elements = _fetchall_promise_all_block(html)
    assert len(names) == elements, (
        f"fetchAll destructuring has {len(names)} vars but Promise.all has {elements} "
        f"array elements — missing/extra binding breaks the whole dashboard in-browser."
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
    assert "/api/ghosts/morning-summary" in body
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
    gl_payload = gl.json()
    assert "current_regime" in gl_payload
    assert "combined_regime" in gl_payload["current_regime"]

    gr = c.get("/api/ghosts/regime-breakdown?since=2026-05-01")
    assert gr.status_code == 200
    assert "no-store" in (gr.headers.get("cache-control") or "").lower()
    assert "rows" in gr.json()

    gd = c.get("/api/ghosts/decision-digest?since=2026-05-01")
    assert gd.status_code == 200
    assert "no-store" in (gd.headers.get("cache-control") or "").lower()
    gd_payload = gd.json()
    assert "ghost_gate" in gd_payload
    assert "calibration" in gd_payload

    gm = c.get("/api/ghosts/morning-summary?since=2099-01-01T00:00:00&until=2099-01-01T12:00:00")
    assert gm.status_code == 200
    assert "no-store" in (gm.headers.get("cache-control") or "").lower()
    gm_payload = gm.json()
    assert gm_payload["hermes_crons_needed"] is False
    assert "standouts" in gm_payload
    assert "settings_adjustments" in gm_payload
    assert "lane_calibrations" in gm_payload
    assert "data_loops" in gm_payload
    assert "learning_loop" in gm_payload
    assert "priority_actions" in gm_payload
    assert "source_files" in gm_payload
    assert gm_payload["learning_loop"]["auto_apply"] is False
    assert gm_payload["learning_loop"]["next_step"] in {"review_priority_actions", "collect_more_overnight_samples"}
    assert any(row["loop"] == "settings candidates -> live config change" for row in gm_payload["data_loops"])


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


def test_ghost_lab_uses_persistent_live_lane_from_settled_context(tmp_path, monkeypatch):
    from src.dashboard import server as dashboard_server

    cal_dir = tmp_path / "calibration"
    cal_dir.mkdir(parents=True)
    row = {
        "ts": "2026-05-26T05:00:00+00:00",
        "lane_id": "sol_macro|5m|down|bearish|rejected",
        "strategy": "sol_macro",
        "window": "5m",
        "side": "SHORT",
        "action": "BUY_NO",
        "reason": "lane_min_edge",
        "win": True,
        "context": {
            "calibration_lane_id": "sol_macro|5m|down|bearish__bearish__bull|standard"
        },
    }
    (cal_dir / "rejected_candidates_settled.jsonl").write_text(
        json.dumps(row, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(dashboard_server, "DATA_ROOT", tmp_path)

    events = dashboard_server._gl_load_ghosts(
        dashboard_server._gl_parse_ts("2026-05-25T00:00:00")
    )

    assert events[0]["lane_id"] == "sol_macro|5m|down|bearish__bearish__bull|standard"
    assert events[0]["ghost_lane_id"] == "sol_macro|5m|down|bearish|rejected"


def test_ghost_lab_timestamp_parser_normalizes_to_utc():
    from src.dashboard import server as dashboard_server

    ts = dashboard_server._gl_parse_ts("2026-06-04T19:00:00-07:00")

    assert ts is not None
    assert ts.tzinfo is None
    assert ts.isoformat() == "2026-06-05T02:00:00"


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
    assert "loss_kill_switch_enabled" in html
    assert "LOSS KILL ON" in html or "LOSS KILL OFF" in html
    assert 'id="loss-kill-toggle-btn" disabled>Loss Kill: Loading</button>' in html
    # Dead-zone toggle button + badge removed 2026-06-10 (deadzone feature purged).
    assert "toggleDeadZones()" not in html
    assert "DEAD ZONES ON" not in html and "DEAD ZONES OFF" not in html
    assert "lossBtn.textContent = lossKillSwitchEnabled ? 'Loss Kill: On' : 'Loss Kill: Off';" in html
    assert "deadBtn.textContent" not in html
    assert "kill-trigger" in html
    assert "live-off" in html


def test_action_breakdown_backend_includes_doge_and_bnb():
    server = (REPO / "src" / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert '"doge_macro"' in server
    assert '"bnb_macro"' in server
    assert "_ACTION_BREAKDOWN_STRATEGIES = _DASHBOARD_STRATEGY_NAMES" in server
    assert "_DASHBOARD_STRATEGY_NAMES = ACTIVE_STRATEGY_NAMES" in server


def test_live_performance_backend_uses_live_strategy_set():
    server = (REPO / "src" / "dashboard" / "server.py").read_text(encoding="utf-8")
    start = server.find("async def get_live_performance(")
    assert start != -1, "live performance handler missing"
    end = server.find("# Build closed-trade list for equity curve", start)
    assert end != -1, "live performance strategy filter block missing"
    block = server[start:end]
    assert "_DASHBOARD_STRATEGY_NAMES" in block
    assert "active_perf_strategies" not in block


def test_heavy_dashboard_endpoints_use_cached_journal_summary():
    server = (REPO / "src" / "dashboard" / "server.py").read_text(encoding="utf-8")
    for name in ("get_status", "get_live_performance", "get_strategy_metrics", "get_journal_summary"):
        start = server.find(f"async def {name}(")
        assert start != -1, f"{name} missing"
        next_route = server.find("\n@app.", start + 1)
        block = server[start: next_route if next_route != -1 else len(server)]
        assert "_get_cached_journal_summary()" in block


def test_manual_journal_refresh_clears_summary_cache():
    server = (REPO / "src" / "dashboard" / "server.py").read_text(encoding="utf-8")
    start = server.find("async def invalidate_journal_cache(")
    assert start != -1, "journal cache invalidation route missing"
    next_route = server.find("\n@app.", start + 1)
    block = server[start: next_route if next_route != -1 else len(server)]
    assert '_journal_summary_cache.update({"ts": 0.0, "summary": None})' in block


@pytest.mark.asyncio
async def test_live_performance_matches_journal_summary_pnl(monkeypatch):
    from src.dashboard import server as dashboard_server

    dashboard_server._journal_summary_cache.update({"ts": 0.0, "summary": None})
    monkeypatch.setattr(
        dashboard_server,
        "_get_journal_summary",
        lambda: {
            "total_exits": 3,
            "wins": 0,
            "losses": 3,
            "win_rate": 0.0,
            "realized_pnl": -4.25,
            "unrealized_pnl": 1.5,
            "total_pnl": -2.75,
            "strategy_stats": {
                "bitcoin": {"trades": 3, "wins": 0, "pnl": -4.25, "win_rate": 0.0},
                "unknown_legacy": {"trades": 99, "wins": 99, "pnl": 999, "win_rate": 1.0},
            },
        },
    )
    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: None)
    monkeypatch.setattr(dashboard_server, "_kelly_state_payload", lambda: {})

    payload = await dashboard_server.get_live_performance()

    assert payload["total_trades"] == 3
    assert payload["win_rate"] == 0.0
    assert payload["realized_pnl"] == -4.25
    assert payload["unrealized_pnl"] == 1.5
    assert payload["total_pnl"] == -2.75
    assert "bitcoin" in payload["by_strategy"]
    assert "unknown_legacy" not in payload["by_strategy"]


def test_live_performance_frontend_renders_zero_winrate_for_closed_trades():
    html = INDEX.read_text(encoding="utf-8")
    assert "lpWin.textContent = totalTrades ? (winRate * 100).toFixed(0) + '%' : '-';" in html
    assert "const winRate = _winRateFraction(d.win_rate || 0);" in html


def test_command_center_sse_positions_do_not_override_empty_status_list():
    html = INDEX.read_text(encoding="utf-8")
    assert "const cachedPositions = Array.isArray(lastStatusData && lastStatusData.positions)" in html
    assert "positions: cachedPositions || undefined" in html
    assert "openCount = lastStatusData.positions.length;" in html


def test_exit_timing_hud_distinguishes_empty_window_from_no_data():
    html = INDEX.read_text(encoding="utf-8")
    assert "No closed trades in the visible BTC chart window" in html
    assert "No closed trade points yet" in html


@pytest.mark.asyncio
async def test_strategy_metrics_counts_disk_open_positions(monkeypatch):
    from src.dashboard import server as dashboard_server

    dashboard_server._journal_summary_cache.update({"ts": 0.0, "summary": None})
    monkeypatch.setattr(dashboard_server, "bot_instance", None)
    monkeypatch.setattr(
        dashboard_server,
        "_get_journal_summary",
        lambda: {
            "strategy_stats": {
                "doge_macro": {
                    "trades": 2,
                    "pnl": 1.25,
                    "win_rate": 0.5,
                    "wins": 1,
                    "avg_pnl": 0.625,
                }
            }
        },
    )
    monkeypatch.setattr(
        dashboard_server,
        "_load_disk_positions_for_status",
        lambda: [
            {"position_id": "p1", "strategy": "doge_macro"},
            {"position_id": "p2", "strategy": "bnb_macro"},
        ],
    )

    payload = await dashboard_server.get_strategy_metrics()

    assert payload["doge_macro"]["trades"] == 2
    assert payload["doge_macro"]["open_positions"] == 1
    assert payload["bnb_macro"]["open_positions"] == 1
    assert payload["bitcoin"]["open_positions"] == 0


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
    assert 'id="gl-digest-tbody"' in html
    assert 'id="gl-morning-summary"' in html
    assert 'id="gl-morning-loops"' in html
    assert 'id="gl-morning-next-edits"' in html
    assert "function loadGhostLab" in html
    assert "function loadGhostMorningSummary" in html
    assert "/api/ghosts/lab" in html
    assert "/api/ghosts/regime-breakdown" in html
    assert "/api/ghosts/decision-digest" in html
    assert "/api/ghosts/morning-summary" in html
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


def test_config_post_accepts_hold_winners_to_resolution_with_auth(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "trading:\n  dry_run: true\n  exit_rules:\n    updown_hold_winners_to_resolution: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboard_server, "CONFIG_PATH", config_path)
    monkeypatch.setattr(dashboard_server, "DASHBOARD_API_KEY", "test-key")

    r = TestClient(dashboard_server.app).post(
        "/api/config",
        headers={"X-API-Key": "test-key"},
        json={"trading": {"exit_rules": {"updown_hold_winners_to_resolution": True}}},
    )

    assert r.status_code == 200
    assert "updown_hold_winners_to_resolution: true" in config_path.read_text(
        encoding="utf-8"
    )


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


def test_performance_table_labels_session_metrics_not_backtest():
    html = INDEX.read_text(encoding="utf-8")
    table = html.split('id="strategy-table"', 1)[1].split("</table>", 1)[0]

    assert "Session Trades" in table
    assert "Session PnL" in table
    assert "<th>Open</th>" in table
    assert "Backtest Trades" not in table
    assert "Backtest PnL" not in table
    assert "${m.open_positions||0}" in html


def test_dashboard_copy_does_not_instruct_operator_to_restart_bot():
    html = INDEX.read_text(encoding="utf-8")

    assert "Restart the bot so /api/journal/action_breakdown is available" not in html
    assert "Old code — restart bot to activate" not in html
    assert "/api/journal/action_breakdown did not respond" in html
    assert "No new-code marker in recent logs" in html


def test_journal_archive_fetches_summary_and_trades_through_session_api():
    html = INDEX.read_text(encoding="utf-8")

    assert "needJournal ? _journalApiPath('/api/journal/summary') : '/api/journal/summary'" in html
    assert "needJournal ? fetchT(_journalApiPath('/api/journal/trades'))" in html


def test_fetchall_only_loads_action_breakdown_on_performance_tab():
    html = INDEX.read_text(encoding="utf-8")

    assert "needPerformance ? fetchT('/api/journal/action_breakdown') : Promise.resolve(null)" in html


def test_fetchall_skips_retired_scan_endpoint():
    html = INDEX.read_text(encoding="utf-8")
    fetchall_start = html.find("async function fetchAll()")
    assert fetchall_start != -1, "fetchAll missing"
    fetchall_end = html.find("async function", fetchall_start + 1)
    block = html[fetchall_start: fetchall_end if fetchall_end != -1 else len(html)]

    assert "fetchT('/api/scans/latest')" not in block


def test_fetchall_gates_alt_analysis_by_strategy_state():
    html = INDEX.read_text(encoding="utf-8")

    assert "function _liveAssetPollEnabled(strategyKey)" in html
    for strategy, endpoint in {
        "sol_macro": "/api/sol/analysis",
        "eth_macro": "/api/eth/analysis",
        "hype_macro": "/api/hype/analysis",
        "xrp_macro": "/api/xrp/analysis",
        "doge_macro": "/api/doge/analysis",
        "bnb_macro": "/api/bnb/analysis",
    }.items():
        assert f"needLive && _liveAssetPollEnabled('{strategy}') ? fetchT('{endpoint}', 14000)" in html


def test_ghost_lab_initial_payload_is_bounded():
    html = INDEX.read_text(encoding="utf-8")

    assert "limit=5000" in html
    assert "limit=20000" not in html


def test_updown_breakdown_can_read_archive_session(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src.dashboard import server as dashboard_server

    class FakeJournal:
        def get_closed_trades(self):
            return [
                {
                    "strategy": "sol_macro",
                    "pnl": 4.25,
                    "market_question": "Solana Up or Down - 15m",
                    "closed_at": "2026-06-05T01:00:00",
                }
            ]

    monkeypatch.setattr(dashboard_server, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(dashboard_server, "_get_journal", lambda: (_ for _ in ()).throw(AssertionError("used live journal")))
    monkeypatch.setattr(dashboard_server, "_journal_for_query", lambda session_id: FakeJournal())

    r = TestClient(dashboard_server.app).get("/api/journal/updown_breakdown?session_id=archive_session")

    assert r.status_code == 200
    payload = r.json()
    assert payload["old_code"]["SOL_updown_15m"]["trades"] == 1
    assert payload["old_code"]["SOL_updown_15m"]["pnl"] == 4.25
