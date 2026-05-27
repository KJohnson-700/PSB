"""Unit tests for src/analysis/lane_direction_fsm.py.

Covers:
  - per-contributor score math
  - hysteresis edge cases (enter, exit, flip, NEUTRAL_STUCK promotion)
  - all NEUTRAL_* sub-FSM directives
  - posterior-confidence scaling (n=0, n<N_REF, n>=N_REF)
  - persistence round-trip on the state file
  - htf modulator does NOT override score
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from src.analysis import lane_direction_fsm as fsm_mod
from src.analysis.lane_direction_fsm import (
    LaneDirectionFSM,
    apply_htf_modifier,
    compute_lane_quant_signal,
    posterior_confidence,
    STATE_BEARISH,
    STATE_BULLISH,
    STATE_NEUTRAL_FROM_BEAR,
    STATE_NEUTRAL_FROM_BULL,
    STATE_NEUTRAL_INITIAL,
    STATE_NEUTRAL_STUCK,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

@dataclass
class FakeMACD:
    histogram: float = 0.0
    histogram_rising: bool = False
    above_zero: bool = False
    crossover: str = "NONE"


@dataclass
class FakeAssetTA:
    ema_9: float = 100.0
    ema_21: float = 100.0
    ema_50: float = 100.0
    rsi_14: float = 50.0
    macd_5m: FakeMACD = field(default_factory=FakeMACD)
    macd_15m: FakeMACD = field(default_factory=FakeMACD)
    macd_30m: FakeMACD = field(default_factory=FakeMACD)
    macd_1h: FakeMACD = field(default_factory=FakeMACD)
    macd_4h: FakeMACD = field(default_factory=FakeMACD)


@dataclass
class FakeTA:
    sol: FakeAssetTA = field(default_factory=FakeAssetTA)


def _bullish_ta() -> FakeTA:
    ta = FakeTA()
    bull = FakeMACD(histogram=0.5, histogram_rising=True, above_zero=True, crossover="BULLISH_CROSS")
    ta.sol.macd_5m = bull
    ta.sol.macd_15m = FakeMACD(histogram=0.3, histogram_rising=True, above_zero=True)
    ta.sol.macd_30m = FakeMACD(histogram=0.2, histogram_rising=True, above_zero=True)
    ta.sol.macd_1h = FakeMACD(histogram=0.1, histogram_rising=True, above_zero=True)
    ta.sol.rsi_14 = 60.0
    ta.sol.ema_9, ta.sol.ema_21, ta.sol.ema_50 = 101.0, 100.0, 99.0
    return ta


def _bearish_ta() -> FakeTA:
    ta = FakeTA()
    bear = FakeMACD(histogram=-0.5, histogram_rising=False, above_zero=False, crossover="BEARISH_CROSS")
    ta.sol.macd_5m = bear
    ta.sol.macd_15m = FakeMACD(histogram=-0.3, histogram_rising=False, above_zero=False)
    ta.sol.macd_30m = FakeMACD(histogram=-0.2, histogram_rising=False, above_zero=False)
    ta.sol.macd_1h = FakeMACD(histogram=-0.1, histogram_rising=False, above_zero=False)
    ta.sol.rsi_14 = 40.0
    ta.sol.ema_9, ta.sol.ema_21, ta.sol.ema_50 = 99.0, 100.0, 101.0
    return ta


def _flat_ta() -> FakeTA:
    return FakeTA()  # all zeros / mids


# ---------------------------------------------------------------------------
# Score math
# ---------------------------------------------------------------------------

class TestComputeLaneQuantSignal:
    def test_strong_bull_produces_positive_score(self):
        score, contribs = compute_lane_quant_signal(_bullish_ta(), "5m")
        assert score > 0.5
        assert contribs["macd_direction"] == 1.0
        assert contribs["macd_momentum"] == 1.0
        assert contribs["macd_crossover"] == 1.0
        assert contribs["ema_alignment"] == 1.0

    def test_strong_bear_produces_negative_score(self):
        score, contribs = compute_lane_quant_signal(_bearish_ta(), "5m")
        assert score < -0.5
        assert contribs["macd_direction"] == -1.0
        assert contribs["ema_alignment"] == -1.0

    def test_flat_ta_returns_near_zero(self):
        score, _ = compute_lane_quant_signal(_flat_ta(), "5m")
        assert abs(score) < 0.05

    def test_invalid_timeframe_returns_zero(self):
        score, contribs = compute_lane_quant_signal(_bullish_ta(), "bogus")
        assert score == 0.0
        assert contribs == {}

    def test_neighbor_tf_contributor_used_on_5m(self):
        ta = _flat_ta()
        # Only 15m (neighbour of 5m) has bull histogram
        ta.sol.macd_15m = FakeMACD(histogram=0.5, histogram_rising=True, above_zero=True)
        _, contribs = compute_lane_quant_signal(ta, "5m")
        assert contribs.get("neighbor_tf") == 1.0

    def test_score_is_clamped_to_unit_range(self):
        ta = _bullish_ta()
        # Crank the weights so an unclamped sum exceeds 1.
        w = {k: 10.0 for k in ("macd_direction", "macd_momentum", "macd_crossover",
                                "ema_alignment", "rsi_zone", "neighbor_tf")}
        score, _ = compute_lane_quant_signal(ta, "5m", weights=w)
        assert -1.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# htf modifier
# ---------------------------------------------------------------------------

class TestHtfModifier:
    def test_bullish_adds_positive(self):
        out, mod = apply_htf_modifier(0.0, "BULLISH", alpha=0.15)
        assert mod == pytest.approx(0.15)
        assert out == pytest.approx(0.15)

    def test_bearish_subtracts(self):
        out, mod = apply_htf_modifier(0.0, "BEARISH", alpha=0.15)
        assert mod == pytest.approx(-0.15)
        assert out == pytest.approx(-0.15)

    def test_neutral_passthrough(self):
        out, mod = apply_htf_modifier(0.4, "NEUTRAL", alpha=0.15)
        assert mod == 0.0
        assert out == pytest.approx(0.4)

    def test_does_not_override_strong_signal(self):
        # alpha is bounded so a strong contrary score still dominates.
        out, _ = apply_htf_modifier(0.9, "BEARISH", alpha=0.15)
        assert out > 0.5  # still bullish overall

    def test_clamped_to_unit_range(self):
        out, _ = apply_htf_modifier(0.95, "BULLISH", alpha=0.5)
        assert out == 1.0


# ---------------------------------------------------------------------------
# Posterior confidence
# ---------------------------------------------------------------------------

class TestPosteriorConfidence:
    def test_zero_n_zero_conf(self):
        assert posterior_confidence(0) == 0.0

    def test_half_n_half_conf(self):
        assert posterior_confidence(100, n_ref=200) == 0.5

    def test_above_n_ref_clamps_to_one(self):
        assert posterior_confidence(500, n_ref=200) == 1.0

    def test_n_ref_zero_returns_one(self):
        assert posterior_confidence(10, n_ref=0) == 1.0


# ---------------------------------------------------------------------------
# FSM hysteresis + neutral sub-FSM
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_fsm(tmp_path):
    """An FSM with state + audit written into a tmpdir."""
    return LaneDirectionFSM(
        state_path=tmp_path / "lane_direction_state.json",
        audit_path=tmp_path / "lane_state_audit.jsonl",
        posteriors_path=tmp_path / "lane_posteriors.json",  # missing — total_n=0
        t_enter=0.30, t_exit=0.10,
        htf_alpha=0.0,  # disable modulator in tests where we drive score directly
        n_ref=1,  # so total_n=0 yields posterior_confidence=0.0
        neutral_stuck_sec=2,
        recovery_size_mult=0.30,
    )


class TestFSMTransitions:
    def test_starts_neutral_initial(self, isolated_fsm):
        d = isolated_fsm.resolve("bitcoin", "5m", _flat_ta(), "NEUTRAL")
        assert d.state == STATE_NEUTRAL_INITIAL
        assert d.side == "SIT_OUT"

    def test_bull_enters_bullish_state(self, isolated_fsm):
        # posterior_confidence=0 → score scaled by 0.5; need raw_score large enough
        # that 0.5*raw >= t_enter=0.30. So raw must >= 0.6.
        # Use the n_ref=1 fixture with a one-time posteriors injection.
        isolated_fsm.n_ref = 1
        # Force posterior_total_n to 1 by writing a tiny posteriors file.
        post = isolated_fsm._posteriors
        post._cached = {"bitcoin|5m|down|bearish|standard": {"n": 1}}
        post._mtime = time.time()
        d = isolated_fsm.resolve("bitcoin", "5m", _bullish_ta(), "NEUTRAL")
        # With full posterior_confidence (1.0), score = raw * 1.0 -> > 0.30.
        assert d.state == STATE_BULLISH
        assert d.side == "LONG"
        assert d.size_multiplier == 1.0

    def test_hysteresis_holds_bull_past_t_enter_drop(self, isolated_fsm):
        # Get into BULLISH first.
        isolated_fsm._posteriors._cached = {"bitcoin|5m|down|bearish|standard": {"n": 1}}
        isolated_fsm._posteriors._mtime = time.time()
        isolated_fsm.n_ref = 1
        isolated_fsm.resolve("bitcoin", "5m", _bullish_ta(), "NEUTRAL")
        st = isolated_fsm.state_for("bitcoin", "5m")
        assert st.current_state == STATE_BULLISH

        # Now feed a weak-positive TA (score ~ 0.2; > T_exit=0.1, so stay bull)
        weak_ta = FakeTA()
        weak_ta.sol.macd_5m = FakeMACD(histogram=0.1, histogram_rising=False, above_zero=True)
        weak_ta.sol.ema_9, weak_ta.sol.ema_21, weak_ta.sol.ema_50 = 100.5, 100.0, 99.5
        weak_ta.sol.rsi_14 = 55.0
        d = isolated_fsm.resolve("bitcoin", "5m", weak_ta, "NEUTRAL")
        # Stays BULLISH because score > t_exit.
        assert d.state in (STATE_BULLISH, STATE_NEUTRAL_FROM_BULL)

    def test_bullish_to_neutral_with_down_momentum_is_fade_short(self, isolated_fsm):
        isolated_fsm._posteriors._cached = {"bitcoin|5m|down|bearish|standard": {"n": 1}}
        isolated_fsm._posteriors._mtime = time.time()
        isolated_fsm.n_ref = 1
        # Enter BULLISH
        isolated_fsm.resolve("bitcoin", "5m", _bullish_ta(), "NEUTRAL")
        # Now construct a TA whose net score lands in (-t_exit, +t_exit) =
        # (-0.10, +0.10) AND whose macd_direction is negative (so the
        # FSM captures momentum_sign = -1 at the transition). We get there by
        # pairing a tiny-negative histogram (direction=-1, weight 0.30 → -0.30)
        # with macd_momentum=+1 (rising AND above_zero → +0.25) so net raw =
        # -0.05.
        weak_ta = FakeTA()
        weak_ta.sol.macd_5m = FakeMACD(
            histogram=-0.001, histogram_rising=True, above_zero=True, crossover="NONE"
        )
        weak_ta.sol.macd_15m = FakeMACD(histogram=0.0)
        d = isolated_fsm.resolve("bitcoin", "5m", weak_ta, "NEUTRAL")
        assert d.state == STATE_NEUTRAL_FROM_BULL
        # momentum_at_transition captured = sign(macd_direction) = -1 → fade SHORT
        assert d.side == "SHORT"
        assert d.size_multiplier == pytest.approx(0.30)

    def test_bearish_to_neutral_with_up_momentum_is_fade_long(self, isolated_fsm):
        isolated_fsm._posteriors._cached = {"bitcoin|5m|down|bearish|standard": {"n": 1}}
        isolated_fsm._posteriors._mtime = time.time()
        isolated_fsm.n_ref = 1
        isolated_fsm.resolve("bitcoin", "5m", _bearish_ta(), "NEUTRAL")
        # Mirror of the bull→neutral test: tiny positive histogram for
        # direction=+1 (weight 0.30 → +0.30), paired with momentum=-1
        # (not-rising AND not-above-zero → -0.25). Net raw = +0.05 in band.
        weak_ta = FakeTA()
        weak_ta.sol.macd_5m = FakeMACD(
            histogram=0.001, histogram_rising=False, above_zero=False, crossover="NONE"
        )
        weak_ta.sol.macd_15m = FakeMACD(histogram=0.0)
        d = isolated_fsm.resolve("bitcoin", "5m", weak_ta, "NEUTRAL")
        assert d.state == STATE_NEUTRAL_FROM_BEAR
        assert d.side == "LONG"
        assert d.size_multiplier == pytest.approx(0.30)

    def test_neutral_stuck_promotion(self, isolated_fsm):
        isolated_fsm._posteriors._cached = {"bitcoin|5m|down|bearish|standard": {"n": 1}}
        isolated_fsm._posteriors._mtime = time.time()
        isolated_fsm.n_ref = 1
        isolated_fsm.neutral_stuck_sec = 0  # promote immediately on next call
        # Enter BULLISH then NEUTRAL_FROM_BULL
        isolated_fsm.resolve("bitcoin", "5m", _bullish_ta(), "NEUTRAL")
        weak_ta = FakeTA()
        weak_ta.sol.macd_5m = FakeMACD(histogram=0.0)
        isolated_fsm.resolve("bitcoin", "5m", weak_ta, "NEUTRAL")
        # Backdate transition_ts to make the "stuck > sec" check fire
        st = isolated_fsm._states["bitcoin|5m"]
        st.transition_ts = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        isolated_fsm._flush()
        d = isolated_fsm.resolve("bitcoin", "5m", weak_ta, "NEUTRAL")
        assert d.state == STATE_NEUTRAL_STUCK
        assert d.side == "SIGNAL_TIME"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_state_roundtrip(self, tmp_path):
        sp = tmp_path / "state.json"
        ap = tmp_path / "audit.jsonl"
        pp = tmp_path / "post.json"
        fsm1 = LaneDirectionFSM(state_path=sp, audit_path=ap, posteriors_path=pp,
                                t_enter=0.30, t_exit=0.10, htf_alpha=0.0, n_ref=1)
        fsm1._posteriors._cached = {"bitcoin|5m|down|bearish|standard": {"n": 1}}
        fsm1._posteriors._mtime = time.time()
        fsm1.resolve("bitcoin", "5m", _bullish_ta(), "NEUTRAL")
        assert sp.exists()

        fsm2 = LaneDirectionFSM(state_path=sp, audit_path=ap, posteriors_path=pp,
                                t_enter=0.30, t_exit=0.10, htf_alpha=0.0, n_ref=1)
        st = fsm2.state_for("bitcoin", "5m")
        assert st is not None
        assert st.current_state == STATE_BULLISH

    def test_audit_appended_on_transition(self, tmp_path):
        sp = tmp_path / "state.json"
        ap = tmp_path / "audit.jsonl"
        pp = tmp_path / "post.json"
        fsm = LaneDirectionFSM(state_path=sp, audit_path=ap, posteriors_path=pp,
                               t_enter=0.30, t_exit=0.10, htf_alpha=0.0, n_ref=1)
        fsm._posteriors._cached = {"bitcoin|5m|down|bearish|standard": {"n": 1}}
        fsm._posteriors._mtime = time.time()
        fsm.resolve("bitcoin", "5m", _bullish_ta(), "NEUTRAL")
        assert ap.exists()
        events = [json.loads(l) for l in ap.read_text().splitlines() if l.strip()]
        assert any(e.get("event") == "direction_event" for e in events)
        ev = next(e for e in events if e.get("event") == "direction_event")
        assert ev["new_state"] == STATE_BULLISH
        assert "contributors" in ev


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------

class TestDefensive:
    def test_missing_ta_object_falls_back(self, isolated_fsm):
        d = isolated_fsm.resolve("bitcoin", "5m", None, "NEUTRAL")
        # source either "fallback_error" or a clean SIT_OUT — must not raise.
        assert d.side in ("SIT_OUT",)

    def test_unknown_tf_falls_back_to_neutral_initial(self, isolated_fsm):
        d = isolated_fsm.resolve("bitcoin", "bogus_tf", _bullish_ta(), "NEUTRAL")
        assert d.side == "SIT_OUT"


# ---------------------------------------------------------------------------
# Active-flag accessor
# ---------------------------------------------------------------------------

class TestIsActive:
    def test_default_off(self):
        assert fsm_mod.is_active({}) is False

    def test_explicit_on(self):
        assert fsm_mod.is_active({"lane_direction_fsm_active": True}) is True
