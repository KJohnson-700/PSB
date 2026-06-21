"""Tests for the regime fade filter (src/analysis/regime_fade.py)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.analysis import regime_fade
from src.analysis.regime_fade import (
    RegimeFadeConfig,
    RegimeFadeState,
    evaluate,
    predicted_p_win,
    should_suppress,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    regime_fade.reset_cache()
    yield
    regime_fade.reset_cache()


def _write_trades(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _row(*, side, est_prob, win, ts):
    return {
        "ts": ts.isoformat(),
        "closed_at": ts.isoformat(),
        "side": side,
        "calibrated_est_prob": est_prob,
        "win": win,
    }


def _cfg(**over):
    base = {
        "enabled": True,
        "high_conf_threshold": 0.60,
        "window_trades": 25,
        "min_high_conf_samples": 8,
        "fade_below_wr": 0.45,
        "recover_above_wr": 0.50,
        "max_trade_age_hours": 48,
        "cache_ttl_sec": 0,  # disable TTL so each evaluate recomputes in tests
    }
    base.update(over)
    return {"regime_fade": base}


# --- predicted_p_win -------------------------------------------------------

def test_predicted_p_win_long_vs_short():
    assert predicted_p_win("BUY_YES", 0.7) == pytest.approx(0.7)
    assert predicted_p_win("BUY_NO", 0.7) == pytest.approx(0.3)
    assert predicted_p_win("SHORT", 0.8) == pytest.approx(0.2)
    assert predicted_p_win("LONG", 0.8) == pytest.approx(0.8)
    assert predicted_p_win("BUY_YES", None) is None


# --- config parsing --------------------------------------------------------

def test_config_defaults_and_action_clamp():
    cfg = RegimeFadeConfig.from_dict({"action": "nonsense"})
    assert cfg.action == "sit_out"
    assert cfg.enabled is True
    cfg2 = RegimeFadeConfig.from_dict({"action": "raise_bar"})
    assert cfg2.action == "raise_bar"


def test_disabled_returns_inactive(tmp_path):
    p = tmp_path / "trades.jsonl"
    _write_trades(p, [])
    state = evaluate({"regime_fade": {"enabled": False}}, trades_path=p,
                     status_path=tmp_path / "s.json", force=True)
    assert state.active is False
    assert state.reason == "disabled"


# --- core fade activation --------------------------------------------------

def test_fade_activates_when_high_conf_wr_collapses(tmp_path):
    now = datetime.now(timezone.utc)
    rows = []
    # 10 high-confidence (pred p_win 0.7) BUY_YES trades, only 3 win -> WR 0.30
    for i in range(10):
        rows.append(_row(side="BUY_YES", est_prob=0.7, win=(i < 3),
                         ts=now - timedelta(minutes=10 * (10 - i))))
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    assert state.active is True
    assert state.n_high_conf == 10
    assert state.rolling_wr == pytest.approx(0.30)


def test_no_fade_when_high_conf_wr_healthy(tmp_path):
    now = datetime.now(timezone.utc)
    rows = [_row(side="BUY_YES", est_prob=0.7, win=(i < 7),
                 ts=now - timedelta(minutes=10 * (10 - i))) for i in range(10)]
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    assert state.active is False
    assert state.rolling_wr == pytest.approx(0.70)


def test_insufficient_high_conf_samples_stays_inactive(tmp_path):
    now = datetime.now(timezone.utc)
    # Only 3 high-conf trades (< min_high_conf_samples=8) all losses.
    rows = [_row(side="BUY_YES", est_prob=0.7, win=False,
                 ts=now - timedelta(minutes=5 * i)) for i in range(3)]
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    assert state.active is False
    assert "insufficient_high_conf_samples" in state.reason


def test_low_conf_trades_do_not_count_toward_high_conf_wr(tmp_path):
    now = datetime.now(timezone.utc)
    # 8 low-conf losers (p_win 0.5) + 8 high-conf winners (p_win 0.7).
    rows = []
    for i in range(8):
        rows.append(_row(side="BUY_YES", est_prob=0.5, win=False,
                         ts=now - timedelta(minutes=100 + i)))
    for i in range(8):
        rows.append(_row(side="BUY_YES", est_prob=0.7, win=True,
                         ts=now - timedelta(minutes=10 + i)))
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    # high-conf WR = 8/8 = 1.0, low-conf losers excluded -> not active.
    assert state.n_high_conf == 8
    assert state.rolling_wr == pytest.approx(1.0)
    assert state.active is False


def test_buy_no_predicted_p_win_counts_as_high_conf(tmp_path):
    now = datetime.now(timezone.utc)
    # BUY_NO with est_prob 0.3 -> predicted p_win 0.7 (high conf). 9 of them, 2 win.
    rows = [_row(side="BUY_NO", est_prob=0.3, win=(i < 2),
                 ts=now - timedelta(minutes=5 * (9 - i))) for i in range(9)]
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    assert state.n_high_conf == 9
    assert state.rolling_wr == pytest.approx(2 / 9)
    assert state.active is True


def test_stale_trades_excluded_by_age(tmp_path):
    now = datetime.now(timezone.utc)
    # All losers but older than max_trade_age_hours -> excluded -> inactive.
    rows = [_row(side="BUY_YES", est_prob=0.7, win=False,
                 ts=now - timedelta(hours=100 + i)) for i in range(10)]
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(max_trade_age_hours=48), trades_path=p,
                     status_path=tmp_path / "s.json", now=now, force=True)
    assert state.active is False
    assert state.n_window == 0


def test_shadow_mode_rows_excluded(tmp_path):
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(10):
        r = _row(side="BUY_YES", est_prob=0.7, win=False,
                 ts=now - timedelta(minutes=i))
        r["shadow_mode"] = True
        rows.append(r)
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    assert state.active is False
    assert state.n_window == 0


# --- suppression decision --------------------------------------------------

def test_should_suppress_only_high_conf_when_active():
    state = RegimeFadeState(active=True, rolling_wr=0.3, n_high_conf=10,
                            high_conf_threshold=0.6, action="sit_out")
    # high-conf candidate -> suppress
    sup, reason = should_suppress(state, 0.7, _cfg())
    assert sup is True
    assert "regime_fade_high_conf_chop" in reason
    # low-conf candidate -> allowed even in fade
    sup2, _ = should_suppress(state, 0.55, _cfg())
    assert sup2 is False


def test_should_suppress_inactive_allows_everything():
    state = RegimeFadeState(active=False)
    sup, _ = should_suppress(state, 0.95, _cfg())
    assert sup is False


def test_raise_bar_action_blocks_low_edge_allows_high_edge():
    state = RegimeFadeState(active=True, rolling_wr=0.3, n_high_conf=10,
                            high_conf_threshold=0.6, action="raise_bar")
    cfg = _cfg(action="raise_bar", raise_bar_min_edge_bonus=0.05)
    # high-conf, edge below bonus -> blocked
    sup, _ = should_suppress(state, 0.7, cfg, edge=0.02)
    assert sup is True
    # high-conf, edge clears bonus -> allowed
    sup2, _ = should_suppress(state, 0.7, cfg, edge=0.10)
    assert sup2 is False


# --- hysteresis ------------------------------------------------------------

def test_hysteresis_holds_fade_between_thresholds(tmp_path):
    now = datetime.now(timezone.utc)
    p = tmp_path / "trades.jsonl"
    status = tmp_path / "s.json"
    cfg = _cfg(fade_below_wr=0.45, recover_above_wr=0.55)

    # Phase 1: WR 0.3 -> activate.
    rows = [_row(side="BUY_YES", est_prob=0.7, win=(i < 3),
                 ts=now - timedelta(minutes=10 * (10 - i))) for i in range(10)]
    _write_trades(p, rows)
    s1 = evaluate(cfg, trades_path=p, status_path=status, now=now, force=True)
    assert s1.active is True

    # Phase 2: WR 0.5 — above fade_below(0.45) but below recover(0.55).
    # Because we were active, hysteresis keeps it active.
    rows2 = [_row(side="BUY_YES", est_prob=0.7, win=(i < 5),
                  ts=now - timedelta(minutes=10 * (10 - i))) for i in range(10)]
    _write_trades(p, rows2)
    s2 = evaluate(cfg, trades_path=p, status_path=status, now=now, force=True)
    assert s2.rolling_wr == pytest.approx(0.5)
    assert s2.active is True  # held by hysteresis

    # Phase 3: WR 0.6 -> recovers above recover_above_wr -> inactive.
    rows3 = [_row(side="BUY_YES", est_prob=0.7, win=(i < 6),
                  ts=now - timedelta(minutes=10 * (10 - i))) for i in range(10)]
    _write_trades(p, rows3)
    s3 = evaluate(cfg, trades_path=p, status_path=status, now=now, force=True)
    assert s3.rolling_wr == pytest.approx(0.6)
    assert s3.active is False


def test_status_file_written(tmp_path):
    now = datetime.now(timezone.utc)
    p = tmp_path / "trades.jsonl"
    status = tmp_path / "runtime" / "regime_fade_state.json"
    rows = [_row(side="BUY_YES", est_prob=0.7, win=(i < 3),
                 ts=now - timedelta(minutes=i)) for i in range(10)]
    _write_trades(p, rows)
    evaluate(_cfg(), trades_path=p, status_path=status, now=now, force=True)
    assert status.exists()
    data = json.loads(status.read_text())
    assert data["active"] is True
    assert data["n_high_conf"] == 10
