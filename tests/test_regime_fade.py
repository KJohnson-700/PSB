"""Tests for the band-targeted regime fade filter (src/analysis/regime_fade.py)."""
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
        "band_low": 0.45,
        "band_high": 0.65,
        "window_trades": 40,
        "min_band_samples": 10,
        "fade_below_wr": 0.48,
        "recover_above_wr": 0.53,
        "max_trade_age_hours": 48,
        "cache_ttl_sec": 0,
    }
    base.update(over)
    return {"regime_fade": base}


# --- predicted_p_win -------------------------------------------------------

def test_predicted_p_win_long_vs_short():
    assert predicted_p_win("BUY_YES", 0.6) == pytest.approx(0.6)
    assert predicted_p_win("BUY_NO", 0.6) == pytest.approx(0.4)
    assert predicted_p_win("SHORT", 0.45) == pytest.approx(0.55)
    assert predicted_p_win("BUY_YES", None) is None


def test_in_band():
    cfg = RegimeFadeConfig.from_dict({"band_low": 0.45, "band_high": 0.65})
    assert cfg.in_band(0.45) is True
    assert cfg.in_band(0.60) is True
    assert cfg.in_band(0.64999) is True
    assert cfg.in_band(0.65) is False   # 0.7 winner protected
    assert cfg.in_band(0.40) is False   # low-conf left alone
    assert cfg.in_band(None) is False


def test_disabled_returns_inactive(tmp_path):
    p = tmp_path / "trades.jsonl"
    _write_trades(p, [])
    state = evaluate({"regime_fade": {"enabled": False}}, trades_path=p,
                     status_path=tmp_path / "s.json", force=True)
    assert state.active is False
    assert state.reason == "disabled"


# --- band activation -------------------------------------------------------

def test_fade_activates_when_band_wr_collapses(tmp_path):
    now = datetime.now(timezone.utc)
    # 12 in-band (p_win 0.6) BUY_YES, only 4 win -> band WR 0.33 < 0.48
    rows = [_row(side="BUY_YES", est_prob=0.6, win=(i < 4),
                 ts=now - timedelta(minutes=i)) for i in range(12)]
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    assert state.active is True
    assert state.n_band == 12
    assert state.rolling_wr == pytest.approx(4 / 12)


def test_no_fade_when_band_healthy(tmp_path):
    now = datetime.now(timezone.utc)
    rows = [_row(side="BUY_YES", est_prob=0.6, win=(i < 7),
                 ts=now - timedelta(minutes=i)) for i in range(12)]
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    assert state.active is False  # WR 0.58 >= fade_below 0.48


def test_07_winners_do_not_dilute_band_and_are_protected(tmp_path):
    now = datetime.now(timezone.utc)
    rows = []
    # 12 in-band losers (p_win 0.6, 33% WR) ...
    for i in range(12):
        rows.append(_row(side="BUY_YES", est_prob=0.6, win=(i < 4),
                         ts=now - timedelta(minutes=100 + i)))
    # ... plus 10 genuine 0.7 winners (p_win 0.7, out of band) all winning.
    for i in range(10):
        rows.append(_row(side="BUY_YES", est_prob=0.7, win=True,
                         ts=now - timedelta(minutes=10 + i)))
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    # band WR must reflect ONLY the in-band trades (0.6), not the 0.7 winners.
    assert state.n_band == 12
    assert state.rolling_wr == pytest.approx(4 / 12)
    assert state.active is True
    # and a 0.7 candidate must NOT be suppressed
    sup, _ = should_suppress(state, 0.70, _cfg())
    assert sup is False
    # an in-band 0.6 candidate IS suppressed
    sup2, reason = should_suppress(state, 0.60, _cfg())
    assert sup2 is True
    assert "regime_fade_band_chop" in reason


def test_insufficient_band_samples_stays_inactive(tmp_path):
    now = datetime.now(timezone.utc)
    rows = [_row(side="BUY_YES", est_prob=0.6, win=False,
                 ts=now - timedelta(minutes=i)) for i in range(5)]
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    assert state.active is False
    assert "insufficient_band_samples" in state.reason


def test_low_conf_left_alone(tmp_path):
    now = datetime.now(timezone.utc)
    # below band (p_win 0.40), even if losing, must not activate the band filter
    rows = [_row(side="BUY_YES", est_prob=0.40, win=False,
                 ts=now - timedelta(minutes=i)) for i in range(15)]
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    assert state.active is False
    assert state.n_band == 0


def test_buy_no_maps_into_band(tmp_path):
    now = datetime.now(timezone.utc)
    # BUY_NO est_prob 0.40 -> p_win 0.60 (in band). 12 of them, 4 win.
    rows = [_row(side="BUY_NO", est_prob=0.40, win=(i < 4),
                 ts=now - timedelta(minutes=i)) for i in range(12)]
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(), trades_path=p, status_path=tmp_path / "s.json",
                     now=now, force=True)
    assert state.n_band == 12
    assert state.active is True


def test_stale_and_shadow_rows_excluded(tmp_path):
    now = datetime.now(timezone.utc)
    rows = [_row(side="BUY_YES", est_prob=0.6, win=False,
                 ts=now - timedelta(hours=100 + i)) for i in range(12)]
    for i in range(12):
        r = _row(side="BUY_YES", est_prob=0.6, win=False, ts=now - timedelta(minutes=i))
        r["shadow_mode"] = True
        rows.append(r)
    p = tmp_path / "trades.jsonl"
    _write_trades(p, rows)
    state = evaluate(_cfg(max_trade_age_hours=48), trades_path=p,
                     status_path=tmp_path / "s.json", now=now, force=True)
    assert state.active is False
    assert state.n_window == 0


# --- suppression decision --------------------------------------------------

def test_should_suppress_only_in_band_when_active():
    state = RegimeFadeState(active=True, rolling_wr=0.33, n_band=12,
                            band_low=0.45, band_high=0.65, action="sit_out")
    assert should_suppress(state, 0.55, _cfg())[0] is True   # in band
    assert should_suppress(state, 0.40, _cfg())[0] is False  # below band
    assert should_suppress(state, 0.70, _cfg())[0] is False  # above band (winner)


def test_should_suppress_inactive_allows_everything():
    state = RegimeFadeState(active=False)
    assert should_suppress(state, 0.55, _cfg())[0] is False


def test_raise_bar_blocks_low_edge_allows_high():
    state = RegimeFadeState(active=True, rolling_wr=0.33, n_band=12,
                            band_low=0.45, band_high=0.65, action="raise_bar")
    cfg = _cfg(action="raise_bar", raise_bar_min_edge_bonus=0.08)
    assert should_suppress(state, 0.55, cfg, edge=0.02)[0] is True
    assert should_suppress(state, 0.55, cfg, edge=0.12)[0] is False


# --- hysteresis ------------------------------------------------------------

def test_hysteresis_holds_between_thresholds(tmp_path):
    now = datetime.now(timezone.utc)
    p = tmp_path / "trades.jsonl"
    status = tmp_path / "s.json"
    cfg = _cfg(fade_below_wr=0.48, recover_above_wr=0.55)

    # activate: band WR 0.33
    _write_trades(p, [_row(side="BUY_YES", est_prob=0.6, win=(i < 4),
                           ts=now - timedelta(minutes=i)) for i in range(12)])
    assert evaluate(cfg, trades_path=p, status_path=status, now=now, force=True).active is True

    # WR 0.50: above fade(0.48), below recover(0.55) -> held active by hysteresis
    _write_trades(p, [_row(side="BUY_YES", est_prob=0.6, win=(i < 6),
                           ts=now - timedelta(minutes=i)) for i in range(12)])
    s2 = evaluate(cfg, trades_path=p, status_path=status, now=now, force=True)
    assert s2.rolling_wr == pytest.approx(0.5)
    assert s2.active is True

    # WR 0.58: recovers above 0.55 -> inactive
    _write_trades(p, [_row(side="BUY_YES", est_prob=0.6, win=(i < 7),
                           ts=now - timedelta(minutes=i)) for i in range(12)])
    s3 = evaluate(cfg, trades_path=p, status_path=status, now=now, force=True)
    assert s3.active is False


def test_cache_invalidates_on_config_change(tmp_path):
    # Same file + within TTL, but a config change must NOT reuse stale state.
    now = datetime.now(timezone.utc)
    p = tmp_path / "trades.jsonl"
    status = tmp_path / "s.json"
    _write_trades(p, [_row(side="BUY_YES", est_prob=0.6, win=(i < 4),
                           ts=now - timedelta(minutes=i)) for i in range(12)])
    cfg_on = _cfg(cache_ttl_sec=999)            # band active (WR 0.33)
    s1 = evaluate(cfg_on, trades_path=p, status_path=status, now=now)
    assert s1.active is True
    # Widen the band floor so 0.6 is now ABOVE band_high -> no in-band samples ->
    # must recompute to inactive, not return the cached active state.
    cfg_narrow = _cfg(cache_ttl_sec=999, band_high=0.55)
    s2 = evaluate(cfg_narrow, trades_path=p, status_path=status, now=now)
    assert s2.active is False
    assert s2.n_band == 0


def test_status_file_written(tmp_path):
    now = datetime.now(timezone.utc)
    p = tmp_path / "trades.jsonl"
    status = tmp_path / "runtime" / "regime_fade_state.json"
    _write_trades(p, [_row(side="BUY_YES", est_prob=0.6, win=(i < 4),
                           ts=now - timedelta(minutes=i)) for i in range(12)])
    evaluate(_cfg(), trades_path=p, status_path=status, now=now, force=True)
    assert status.exists()
    data = json.loads(status.read_text())
    assert data["active"] is True
    assert data["n_band"] == 12
