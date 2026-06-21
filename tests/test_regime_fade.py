"""Tests for the PER-LANE band-targeted regime fade filter."""
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


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _row(*, lane, side, est_prob, win, ts):
    return {
        "ts": ts.isoformat(), "closed_at": ts.isoformat(),
        "strategy": lane, "side": side, "calibrated_est_prob": est_prob, "win": win,
    }


def _cfg(**over):
    base = {
        "enabled": True, "band_low": 0.45, "band_high": 0.65, "window_trades": 60,
        "min_band_samples": 8, "fade_below_wr": 0.48, "recover_above_wr": 0.53,
        "max_trade_age_hours": 48, "cache_ttl_sec": 0,
    }
    base.update(over)
    return {"regime_fade": base}


def test_predicted_p_win():
    assert predicted_p_win("BUY_YES", 0.6) == pytest.approx(0.6)
    assert predicted_p_win("BUY_NO", 0.6) == pytest.approx(0.4)


def test_in_band_protects_high_and_low():
    cfg = RegimeFadeConfig.from_dict({"band_low": 0.45, "band_high": 0.65})
    assert cfg.in_band(0.45) and cfg.in_band(0.60)
    assert not cfg.in_band(0.65) and not cfg.in_band(0.40)


def test_disabled(tmp_path):
    _write(tmp_path / "t.jsonl", [])
    st = evaluate({"regime_fade": {"enabled": False}}, lane="hype_macro",
                  trades_path=tmp_path / "t.jsonl", status_path=tmp_path / "s.json", force=True)
    assert st.active is False and st.reason == "disabled"


def test_per_lane_independent_states(tmp_path):
    """The crux: a bleeding lane fades; a healthy lane does not — same file."""
    now = datetime.now(timezone.utc)
    rows = []
    # hype: 12 in-band, 4 win -> 33% -> ACTIVE
    for i in range(12):
        rows.append(_row(lane="hype_macro", side="BUY_YES", est_prob=0.6,
                         win=(i < 4), ts=now - timedelta(minutes=200 + i)))
    # btc: 12 in-band, 8 win -> 67% -> healthy, NOT active
    for i in range(12):
        rows.append(_row(lane="bitcoin", side="BUY_YES", est_prob=0.6,
                         win=(i < 8), ts=now - timedelta(minutes=100 + i)))
    p = tmp_path / "t.jsonl"
    _write(p, rows)
    cfg = _cfg()
    hype = evaluate(cfg, lane="hype_macro", trades_path=p, status_path=tmp_path / "s.json", now=now, force=True)
    btc = evaluate(cfg, lane="bitcoin", trades_path=p, status_path=tmp_path / "s.json", now=now)
    assert hype.active is True and hype.rolling_wr == pytest.approx(4 / 12)
    assert btc.active is False and btc.rolling_wr == pytest.approx(8 / 12)
    # suppression is per-lane
    assert should_suppress(hype, 0.55, cfg)[0] is True
    assert should_suppress(btc, 0.55, cfg)[0] is False


def test_thin_lane_stays_inactive(tmp_path):
    now = datetime.now(timezone.utc)
    # doge: only 5 in-band losers -> below min_band_samples -> inactive
    rows = [_row(lane="doge_macro", side="BUY_YES", est_prob=0.6, win=False,
                 ts=now - timedelta(minutes=i)) for i in range(5)]
    p = tmp_path / "t.jsonl"
    _write(p, rows)
    st = evaluate(_cfg(), lane="doge_macro", trades_path=p, status_path=tmp_path / "s.json", now=now, force=True)
    assert st.active is False
    assert "insufficient_band_samples" in st.reason


def test_07_winners_protected_and_excluded_from_band(tmp_path):
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(12):  # in-band losers
        rows.append(_row(lane="hype_macro", side="BUY_YES", est_prob=0.6,
                         win=(i < 4), ts=now - timedelta(minutes=100 + i)))
    for i in range(10):  # 0.7 winners (out of band)
        rows.append(_row(lane="hype_macro", side="BUY_YES", est_prob=0.7,
                         win=True, ts=now - timedelta(minutes=10 + i)))
    p = tmp_path / "t.jsonl"
    _write(p, rows)
    st = evaluate(_cfg(), lane="hype_macro", trades_path=p, status_path=tmp_path / "s.json", now=now, force=True)
    assert st.n_band == 12 and st.rolling_wr == pytest.approx(4 / 12) and st.active is True
    assert should_suppress(st, 0.70, _cfg())[0] is False   # winner protected
    assert should_suppress(st, 0.60, _cfg())[0] is True


def test_buy_no_maps_into_band(tmp_path):
    now = datetime.now(timezone.utc)
    rows = [_row(lane="bnb_macro", side="BUY_NO", est_prob=0.40, win=(i < 4),
                 ts=now - timedelta(minutes=i)) for i in range(12)]
    p = tmp_path / "t.jsonl"
    _write(p, rows)
    st = evaluate(_cfg(), lane="bnb_macro", trades_path=p, status_path=tmp_path / "s.json", now=now, force=True)
    assert st.n_band == 12 and st.active is True


def test_unknown_lane_inactive(tmp_path):
    now = datetime.now(timezone.utc)
    _write(tmp_path / "t.jsonl", [_row(lane="hype_macro", side="BUY_YES", est_prob=0.6,
                                       win=False, ts=now - timedelta(minutes=i)) for i in range(12)])
    st = evaluate(_cfg(), lane="xrp_macro", trades_path=tmp_path / "t.jsonl",
                  status_path=tmp_path / "s.json", now=now, force=True)
    assert st.active is False and st.reason == "no_lane_data"


def test_hysteresis_per_lane(tmp_path):
    now = datetime.now(timezone.utc)
    p = tmp_path / "t.jsonl"
    status = tmp_path / "s.json"
    cfg = _cfg(fade_below_wr=0.48, recover_above_wr=0.55)
    _write(p, [_row(lane="hype_macro", side="BUY_YES", est_prob=0.6, win=(i < 4),
                    ts=now - timedelta(minutes=i)) for i in range(12)])
    assert evaluate(cfg, lane="hype_macro", trades_path=p, status_path=status, now=now, force=True).active is True
    # WR 0.5: above fade, below recover -> held active
    _write(p, [_row(lane="hype_macro", side="BUY_YES", est_prob=0.6, win=(i < 6),
                    ts=now - timedelta(minutes=i)) for i in range(12)])
    s2 = evaluate(cfg, lane="hype_macro", trades_path=p, status_path=status, now=now, force=True)
    assert s2.rolling_wr == pytest.approx(0.5) and s2.active is True
    # WR 0.58: recovered -> inactive
    _write(p, [_row(lane="hype_macro", side="BUY_YES", est_prob=0.6, win=(i < 7),
                    ts=now - timedelta(minutes=i)) for i in range(12)])
    assert evaluate(cfg, lane="hype_macro", trades_path=p, status_path=status, now=now, force=True).active is False


def test_status_file_per_lane(tmp_path):
    now = datetime.now(timezone.utc)
    p = tmp_path / "t.jsonl"
    status = tmp_path / "runtime" / "regime_fade_state.json"
    _write(p, [_row(lane="hype_macro", side="BUY_YES", est_prob=0.6, win=(i < 4),
                    ts=now - timedelta(minutes=i)) for i in range(12)])
    evaluate(_cfg(), lane="hype_macro", trades_path=p, status_path=status, now=now, force=True)
    data = json.loads(status.read_text())
    assert "hype_macro" in data["per_lane"]
    assert data["per_lane"]["hype_macro"]["active"] is True
    assert "hype_macro" in data["active_lanes"]


def test_cache_invalidates_on_config_change(tmp_path):
    now = datetime.now(timezone.utc)
    p = tmp_path / "t.jsonl"
    status = tmp_path / "s.json"
    _write(p, [_row(lane="hype_macro", side="BUY_YES", est_prob=0.6, win=(i < 4),
                    ts=now - timedelta(minutes=i)) for i in range(12)])
    s1 = evaluate(_cfg(cache_ttl_sec=999), lane="hype_macro", trades_path=p, status_path=status, now=now)
    assert s1.active is True
    # narrow band so 0.6 is out of band -> recompute -> inactive (no stale reuse)
    s2 = evaluate(_cfg(cache_ttl_sec=999, band_high=0.55), lane="hype_macro",
                  trades_path=p, status_path=status, now=now)
    assert s2.active is False and s2.n_band == 0
