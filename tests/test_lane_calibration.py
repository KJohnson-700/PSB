"""Phase 6 LaneCalibrator tests — math, shadow mode, persistence, recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis.lane_calibration import (
    ALPHA_CLAMP_HI,
    ALPHA_CLAMP_LO,
    PRIOR_A,
    PRIOR_B,
    SCHEMA_VERSION,
    SHRINK_N,
    LaneCalibrator,
)


@pytest.fixture
def tmp_log(tmp_path: Path) -> Path:
    return tmp_path / "lane_posteriors.json"


# ─────────────────────────────────────────────────────────────────────────── math


def test_first_record_initialises_alpha_to_observation(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    # stated_prob = 0.58, realized = +0.20 → a_obs = 0.20 / 0.08 = 2.5 (clamped 5.0)
    snap = cal.record("lane_A", stated_est_prob=0.58, realized_pct=0.20, win=True)
    assert snap["n"] == 1
    # With n=1 below SHRINK_N, shrunk alpha blends 0.1*a + 0.9*1 = 0.25 + 0.9 = 1.15
    raw = cal.raw_alpha("lane_A")
    assert raw is not None
    assert raw == pytest.approx(2.5, abs=1e-6)
    # Shrunk alpha at n=1 → w=0.1, blend = 0.1*2.5 + 0.9*1.0 = 1.15 (within clamp)
    assert cal.alpha("lane_A") == pytest.approx(1.15, abs=1e-6)


def test_alpha_clamps_at_high_bound(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    # Feed extreme positive observations enough times so alpha_ewma saturates.
    for _ in range(40):
        cal.record("L", stated_est_prob=0.58, realized_pct=0.60, win=True)
    raw = cal.raw_alpha("L")
    assert raw is not None
    # Raw EWMA may exceed clamp; shrunk α is clamped.
    assert cal.alpha("L") == pytest.approx(ALPHA_CLAMP_HI, abs=1e-6)


def test_alpha_clamps_at_low_bound(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    # Sign-flipped lane: stated edge up (p>0.5) but realized always negative.
    for _ in range(40):
        cal.record("L", stated_est_prob=0.62, realized_pct=-0.30, win=False)
    assert cal.alpha("L") == pytest.approx(ALPHA_CLAMP_LO, abs=1e-6)


def test_shrinkage_blends_to_identity_below_n_10(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    for _ in range(5):
        cal.record("L", stated_est_prob=0.60, realized_pct=0.30, win=True)
    raw = cal.raw_alpha("L")
    # n=5, w=0.5 → shrunk = 0.5 * raw + 0.5 * 1.0
    expected = 0.5 * raw + 0.5 * 1.0
    expected = max(ALPHA_CLAMP_LO, min(ALPHA_CLAMP_HI, expected))
    assert cal.alpha("L") == pytest.approx(expected, abs=1e-6)


def test_n_at_or_above_threshold_uses_raw_ewma_clamped(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    for _ in range(SHRINK_N):
        cal.record("L", stated_est_prob=0.60, realized_pct=0.20, win=True)
    raw = cal.raw_alpha("L")
    expected = max(ALPHA_CLAMP_LO, min(ALPHA_CLAMP_HI, raw))
    assert cal.alpha("L") == pytest.approx(expected, abs=1e-6)


def test_near_half_stated_prob_skips_alpha_but_still_updates_beta(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    # dev = 0.5005 - 0.5 = 0.0005 < DEV_FLOOR=0.005 → α skipped
    snap = cal.record("L", stated_est_prob=0.5005, realized_pct=0.10, win=True)
    assert snap["n"] == 1
    assert cal.raw_alpha("L") == 1.0  # initialised to identity, never updated
    # Beta still moved on the win.
    assert snap["beta_a"] == PRIOR_A + 1
    assert snap["beta_b"] == PRIOR_B


def test_beta_posterior_tracks_wins_and_losses(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    cal.record("L", 0.6, 0.2, win=True)
    cal.record("L", 0.6, 0.1, win=True)
    cal.record("L", 0.6, -0.3, win=False)
    post = cal.posterior("L")
    assert post["beta_a"] == PRIOR_A + 2
    assert post["beta_b"] == PRIOR_B + 1


def test_alpha_for_unknown_lane_is_identity(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    assert cal.alpha("does_not_exist") == 1.0
    assert cal.raw_alpha("does_not_exist") is None
    p = cal.posterior("does_not_exist")
    assert p["n"] == 0


# ───────────────────────────────────────────────────────────────────── shadow mode


def test_shadow_mode_calibrate_returns_raw(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    # Even with strong posterior, shadow mode does not warp probability.
    for _ in range(20):
        cal.record("L", stated_est_prob=0.60, realized_pct=0.40, win=True)
    assert cal.calibrate("L", 0.75) == 0.75
    assert cal.calibrate("L", 0.20) == 0.20


def test_live_mode_warps_by_alpha(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=False)
    # Build a lane with α > 1 (model under-predicts).
    for _ in range(SHRINK_N + 5):
        cal.record("L", stated_est_prob=0.60, realized_pct=0.30, win=True)
    a = cal.alpha("L")
    assert a > 1.0
    # p_cal = 0.5 + a * (0.65 - 0.5)
    p_raw = 0.65
    assert cal.calibrate("L", p_raw) == pytest.approx(0.5 + a * (p_raw - 0.5), abs=1e-6)


def test_live_mode_clamps_calibrated_into_unit_interval(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=False)
    # Force α near upper clamp.
    for _ in range(SHRINK_N + 20):
        cal.record("L", stated_est_prob=0.55, realized_pct=0.40, win=True)
    # Even extreme raw prob shouldn't escape [0.01, 0.99].
    assert 0.01 <= cal.calibrate("L", 0.99) <= 0.99
    assert 0.01 <= cal.calibrate("L", 0.01) <= 0.99


# ─────────────────────────────────────────────────────────────────── persistence


def test_record_persists_to_disk(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    cal.record("L", 0.6, 0.2, win=True)
    assert tmp_log.exists()
    blob = json.loads(tmp_log.read_text(encoding="utf-8"))
    assert blob["schema_version"] == SCHEMA_VERSION
    assert "L" in blob["lanes"]
    assert blob["lanes"]["L"]["n"] == 1


def test_round_trip_through_new_instance(tmp_log: Path):
    cal_a = LaneCalibrator(path=tmp_log, shadow_mode=True)
    for _ in range(3):
        cal_a.record("L", 0.6, 0.25, win=True)
    cal_b = LaneCalibrator(path=tmp_log, shadow_mode=True)
    assert cal_b.posterior("L")["n"] == 3
    assert cal_b.raw_alpha("L") == pytest.approx(cal_a.raw_alpha("L"), abs=1e-6)


def test_missing_file_starts_empty(tmp_path: Path):
    cal = LaneCalibrator(path=tmp_path / "absent.json", shadow_mode=True)
    assert cal.alpha("anything") == 1.0


def test_corrupt_json_is_archived_and_resets(tmp_log: Path):
    tmp_log.write_text("this is not json", encoding="utf-8")
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    assert cal.alpha("anything") == 1.0
    # Corrupt file was moved aside, not silently lost.
    archived = list(tmp_log.parent.glob("lane_posteriors.json.corrupt.*"))
    assert len(archived) == 1


def test_schema_version_mismatch_archives(tmp_log: Path):
    tmp_log.write_text(
        json.dumps({"schema_version": 999, "lanes": {"L": {"n": 5}}}),
        encoding="utf-8",
    )
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    assert cal.alpha("L") == 1.0  # treated as empty after archive
    archived = list(tmp_log.parent.glob("lane_posteriors.json.corrupt.*"))
    assert len(archived) == 1


def test_empty_lane_id_returns_identity_snapshot(tmp_log: Path):
    cal = LaneCalibrator(path=tmp_log, shadow_mode=True)
    snap = cal.record("", 0.6, 0.2, win=True)
    assert snap["n"] == 0  # nothing was actually written


# ───────────────────────────────────────────────────────────── integration smoke


def test_main_record_path_smoke():
    """Sanity check the integration shape used by main._handle_exit_decision."""
    from src.analysis.calibration_log import build_record_from_closed_trade

    closed = {
        "trade_id": "t1",
        "strategy": "eth_macro",
        "action": "BUY_NO",
        "side": "BUY",
        "outcome": "NO",
        "size": 20.0,
        "entry_price": 0.50,
        "exit_price": 0.62,
        "pnl": 2.4,
        "edge": 0.10,
        "entry_leg": "NO",
        "window_size": "5m",
        "opened_at": "2026-05-14T20:00:00Z",
        "closed_at": "2026-05-14T20:02:00Z",
        "exit_reason": "take_profit",
        "entry_signal": {
            "lane_id": "eth_macro|5m|down|bearish|standard",
            "est_prob": 0.58,
        },
    }
    rec = build_record_from_closed_trade(closed, session_id="sess1")
    assert rec["stated_est_prob"] == 0.58
    assert rec["win"] is True
    # The Phase 6 hook in main.py would now call:
    #   cal.record(rec['lane_id'], rec['stated_est_prob'], rec['realized_pct'], rec['win'])
    # which exercises the same call path tested above with synthetic data.
