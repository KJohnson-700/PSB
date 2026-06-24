"""Tests for the regime layer: runtime gate (fail-safe + precedence) and the
map builder (enable rule, size, hysteresis). Pure-stdlib for the runtime; the
builder test uses an in-memory duckdb fixture when duckdb is importable."""

import json
import os
import time

import pytest

from src.analysis import lane_regime_runtime as rt


def _has_duckdb():
    try:
        import duckdb  # noqa
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _write(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh)


def _fresh_map(lanes, ttl=600):
    now = time.time()
    return {
        "version": 1,
        "expires_at_epoch": now + ttl,
        "lanes": lanes,
    }


def _point_runtime_at(tmp_path, map_obj=None, ovr_obj=None):
    """Repoint the module's cached loaders at tmp files and clear caches."""
    mpath = str(tmp_path / "map.json")
    opath = str(tmp_path / "ovr.json")
    if map_obj is not None:
        _write(mpath, map_obj)
    if ovr_obj is not None:
        _write(opath, ovr_obj)
    rt._map_cache = rt._JsonFileCache(mpath)
    rt._ovr_cache = rt._JsonFileCache(opath)
    return mpath, opath


# --------------------------------------------------------------------------- #
# runtime: fail-safe
# --------------------------------------------------------------------------- #
def test_missing_map_is_neutral(tmp_path):
    _point_runtime_at(tmp_path)  # no files written
    d = rt.evaluate_lane("doge_macro", "1h", "BUY_NO", "BEARISH")
    assert d.is_neutral and not d.overrides_yaml_disable and not d.force_off
    assert d.source in ("stale_fallback", "neutral")


def test_stale_map_falls_back_to_yaml(tmp_path):
    stale = {"version": 1, "expires_at_epoch": time.time() - 1, "lanes": {
        "doge_macro|1h|BUY_NO|BEARISH": {"enabled": True, "size_scalar": 0.5}}}
    _point_runtime_at(tmp_path, map_obj=stale)
    d = rt.evaluate_lane("doge_macro", "1h", "BUY_NO", "BEARISH")
    assert not d.overrides_yaml_disable
    assert d.stale and d.source == "stale_fallback"


def test_corrupt_map_is_neutral(tmp_path):
    mpath = str(tmp_path / "map.json")
    with open(mpath, "w") as fh:
        fh.write("{not valid json")
    rt._map_cache = rt._JsonFileCache(mpath)
    rt._ovr_cache = rt._JsonFileCache(str(tmp_path / "ovr.json"))
    d = rt.evaluate_lane("doge_macro", "1h", "BUY_NO", "BEARISH")
    assert not d.overrides_yaml_disable and not d.force_off


# --------------------------------------------------------------------------- #
# runtime: enable
# --------------------------------------------------------------------------- #
def test_fresh_map_enables_disabled_lane(tmp_path):
    mp = _fresh_map({"doge_macro|1h|BUY_NO|BEARISH":
                     {"enabled": True, "size_scalar": 0.5, "sample_n": 3164,
                      "reason": "BEARISH median +0.05"}})
    _point_runtime_at(tmp_path, map_obj=mp)
    d = rt.evaluate_lane("doge_macro", "1h", "BUY_NO", "BEARISH")
    assert d.overrides_yaml_disable and d.enabled
    assert d.size_scalar == 0.5 and d.sample_n == 3164
    assert d.source == "map_enable"


def test_wrong_regime_not_enabled(tmp_path):
    # lane enabled only for BEARISH; live bias BULLISH -> no override
    mp = _fresh_map({"doge_macro|1h|BUY_NO|BEARISH":
                     {"enabled": True, "size_scalar": 0.5}})
    _point_runtime_at(tmp_path, map_obj=mp)
    d = rt.evaluate_lane("doge_macro", "1h", "BUY_NO", "BULLISH")
    assert not d.overrides_yaml_disable


def test_bias_normalization(tmp_path):
    mp = _fresh_map({"sol_macro|15m|BUY_NO|BEARISH":
                     {"enabled": True, "size_scalar": 0.25}})
    _point_runtime_at(tmp_path, map_obj=mp)
    for raw in ("bear", "BEAR", "Bearish", "BEARISH"):
        d = rt.evaluate_lane("sol_macro", "15m", "BUY_NO", raw)
        assert d.overrides_yaml_disable, raw


# --------------------------------------------------------------------------- #
# runtime: overrides + precedence
# --------------------------------------------------------------------------- #
def test_force_off_wins_over_map_enable(tmp_path):
    mp = _fresh_map({"bnb_macro|1h|BUY_NO|BEARISH":
                     {"enabled": True, "size_scalar": 0.5}})
    ovr = {"overrides": {"bnb_macro|1h|BUY_NO":
                         {"mode": "force_off", "reason": "operator pause"}}}
    _point_runtime_at(tmp_path, map_obj=mp, ovr_obj=ovr)
    d = rt.evaluate_lane("bnb_macro", "1h", "BUY_NO", "BEARISH")
    assert d.force_off and not d.overrides_yaml_disable
    assert d.size_scalar == 0.0


def test_force_on_without_map(tmp_path):
    ovr = {"overrides": {"hype_macro|1h|BUY_NO":
                         {"mode": "force_on", "size_scalar": 0.3}}}
    _point_runtime_at(tmp_path, ovr_obj=ovr)  # no map
    d = rt.evaluate_lane("hype_macro", "1h", "BUY_NO", "BEARISH")
    assert d.overrides_yaml_disable and d.size_scalar == 0.3
    assert d.source == "override_force_on"


def test_cap_size_caps_map_size(tmp_path):
    mp = _fresh_map({"sol_macro|1h|BUY_NO|BEARISH":
                     {"enabled": True, "size_scalar": 0.5}})
    ovr = {"overrides": {"sol_macro|1h|BUY_NO":
                         {"mode": "cap_size", "size_scalar": 0.1}}}
    _point_runtime_at(tmp_path, map_obj=mp, ovr_obj=ovr)
    d = rt.evaluate_lane("sol_macro", "1h", "BUY_NO", "BEARISH")
    assert d.overrides_yaml_disable and d.size_scalar == 0.1


def test_expired_override_ignored(tmp_path):
    mp = _fresh_map({})
    ovr = {"overrides": {"bnb_macro|1h|BUY_NO":
                         {"mode": "force_off", "expires_at_epoch": time.time() - 1}}}
    _point_runtime_at(tmp_path, map_obj=mp, ovr_obj=ovr)
    d = rt.evaluate_lane("bnb_macro", "1h", "BUY_NO", "BEARISH")
    assert not d.force_off


def test_size_scalar_clamped(tmp_path):
    mp = _fresh_map({"sol_macro|5m|BUY_NO|BEARISH":
                     {"enabled": True, "size_scalar": 9.9}})  # absurd
    _point_runtime_at(tmp_path, map_obj=mp)
    d = rt.evaluate_lane("sol_macro", "5m", "BUY_NO", "BEARISH")
    assert d.size_scalar == 1.0  # clamped to <= 1.0


# --------------------------------------------------------------------------- #
# builder: pure helpers + hysteresis
# --------------------------------------------------------------------------- #
def test_builder_qualifies_and_size():
    from src.analysis import lane_regime_map as lm
    cfg = dict(lm.DEFAULTS)
    strong = dict(n=3000, wr=0.62, ev=0.12, median=1.0)
    mid = dict(n=3000, wr=0.57, ev=0.07, median=1.0)
    thin = dict(n=10, wr=0.9, ev=0.5, median=1.0)
    breakeven = dict(n=3000, wr=0.55, ev=0.01, median=0.0)  # +EV but below enable_ev
    broken = dict(n=3000, wr=0.40, ev=0.03, median=-1.0)    # WR below sanity floor
    # high EV but low confidence (small n, WR Wilson-LB < floor) => sizes down, still enabled
    low_conf = dict(n=160, wr=0.55, ev=0.15, median=1.0)
    assert lm._qualifies(strong, cfg) and lm._size_for(strong, cfg) == cfg["size_strong"]
    assert lm._qualifies(mid, cfg) and lm._size_for(mid, cfg) == cfg["size_mid"]
    assert not lm._qualifies(thin, cfg)        # too few samples
    assert not lm._qualifies(breakeven, cfg)   # ev below enable threshold
    assert not lm._qualifies(broken, cfg)      # WR below sanity floor
    assert lm._qualifies(low_conf, cfg)
    assert lm._size_for(low_conf, cfg) == cfg["size_marginal"]  # Wilson-LB sizes it down
    assert lm._wilson_lower_bound(60, 100) < 0.60   # LB below point estimate


@pytest.mark.skipif(not _has_duckdb(), reason="duckdb not installed")
def test_builder_hysteresis_end_to_end(tmp_path):
    import duckdb
    from src.analysis import lane_regime_map as lm

    db = str(tmp_path / "ghost.duckdb")
    con = duckdb.connect(db)
    con.execute('CREATE TABLE ghost_settled("window" VARCHAR, strategy VARCHAR, '
                'side VARCHAR, realized_pct DOUBLE, win BOOLEAN, htf_bias VARCHAR, '
                'settled_at TIMESTAMP)')
    # a clearly +EV bearish doge 1h short lane, large sample
    rows = []
    for i in range(400):
        rp = 0.2 if i % 3 else -0.1   # median > 0, wr ~0.66
        rows.append(("1h", "doge_macro", "SHORT", rp, i % 3 != 0, "BEARISH",
                     "2026-06-24 00:00:00"))
    con.executemany("INSERT INTO ghost_settled VALUES (?,?,?,?,?,?,?)", rows)
    con.close()

    out_p = str(tmp_path / "map.json")
    st_p = str(tmp_path / "state.json")
    cfg = dict(enter_builds=3, exit_builds=2, min_sample=100)
    key = "doge_macro|1h|BUY_NO|BEARISH"

    # 1st & 2nd builds: qualifying but not yet entered (hysteresis)
    m1 = lm.build_map(db, out_p, st_p, cfg)
    assert key not in m1["lanes"]
    m2 = lm.build_map(db, out_p, st_p, cfg)
    assert key not in m2["lanes"]
    # 3rd build: enters
    m3 = lm.build_map(db, out_p, st_p, cfg)
    assert key in m3["lanes"] and m3["lanes"][key]["enabled"]
    assert m3["expires_at_epoch"] > time.time()
