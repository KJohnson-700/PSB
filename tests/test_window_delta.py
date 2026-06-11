"""Unit tests for the model-independent window-delta probability signal."""

import types

import pytest

from src.analysis.window_delta import (
    delta_confirms_side,
    evaluate_window_delta,
    window_delta_pct,
    window_delta_prob,
)


# --- window_delta_pct -------------------------------------------------------


def test_delta_pct_basic():
    assert window_delta_pct(101.0, 100.0) == pytest.approx(1.0)
    assert window_delta_pct(99.0, 100.0) == pytest.approx(-1.0)
    assert window_delta_pct(100.0, 100.0) == pytest.approx(0.0)


def test_delta_pct_bad_open_is_zero():
    # Missing/garbage open reads as "no information", not a crash.
    assert window_delta_pct(100.0, 0.0) == 0.0
    assert window_delta_pct(100.0, -5.0) == 0.0


# --- window_delta_prob: core behaviour --------------------------------------


def test_zero_delta_full_window_is_half():
    # No move, whole window left => maximally uncertain.
    p = window_delta_prob(move_pct=0.0, mins_left=15.0, window_minutes=15.0, atr_pct=0.3)
    assert p == pytest.approx(0.5, abs=1e-9)


def test_monotonic_in_move_pct():
    f = lambda m: window_delta_prob(m, mins_left=7.0, window_minutes=15.0, atr_pct=0.3)
    seq = [f(m) for m in (-0.5, -0.1, 0.0, 0.1, 0.5)]
    assert seq == sorted(seq)
    assert seq[0] < 0.5 < seq[-1]


def test_approaches_step_as_time_runs_out():
    # Same positive delta: near resolution it should approach certainty (ceil),
    # with a full window left it should be much closer to 0.5.
    near = window_delta_prob(0.2, mins_left=0.05, window_minutes=15.0, atr_pct=0.3)
    far = window_delta_prob(0.2, mins_left=15.0, window_minutes=15.0, atr_pct=0.3)
    assert near >= 0.95 - 1e-9  # hits the ceiling
    assert far < near
    assert far > 0.5  # still a positive lean

    # Symmetric on the downside.
    near_dn = window_delta_prob(-0.2, mins_left=0.05, window_minutes=15.0, atr_pct=0.3)
    assert near_dn <= 0.05 + 1e-9


def test_output_clamped():
    hi = window_delta_prob(10.0, mins_left=0.01, window_minutes=5.0, atr_pct=0.2)
    lo = window_delta_prob(-10.0, mins_left=0.01, window_minutes=5.0, atr_pct=0.2)
    assert hi <= 0.95
    assert lo >= 0.05


# --- window_delta_prob: fallback path ---------------------------------------


def test_atr_missing_fallback_is_monotonic_and_sane():
    # No atr_pct => time-insensitive logistic, still monotonic & centred at 0.5.
    assert window_delta_prob(0.0, 7.0, 15.0, None) == pytest.approx(0.5)
    up = window_delta_prob(0.3, 7.0, 15.0, None)
    dn = window_delta_prob(-0.3, 7.0, 15.0, None)
    assert dn < 0.5 < up
    # Fallback also triggers on non-positive atr / window.
    assert window_delta_prob(0.0, 7.0, 15.0, 0.0) == pytest.approx(0.5)
    assert window_delta_prob(0.0, 7.0, 0.0, 0.3) == pytest.approx(0.5)


# --- delta_confirms_side ----------------------------------------------------


def test_confirms_side_directions():
    # Long agrees with an up-lean; short agrees with a down-lean.
    assert delta_confirms_side(0.7, "BUY_YES") is True
    assert delta_confirms_side(0.3, "BUY_YES") is False
    assert delta_confirms_side(0.3, "BUY_NO") is True
    assert delta_confirms_side(0.7, "BUY_NO") is False


def test_confirms_side_margin():
    # A 0.05 margin makes a barely-positive lean insufficient to confirm a long.
    assert delta_confirms_side(0.52, "BUY_YES", margin=0.05) is False
    assert delta_confirms_side(0.56, "BUY_YES", margin=0.05) is True


def test_confirms_side_unknown_action_passes():
    # The gate only adjudicates the two directional sides.
    assert delta_confirms_side(0.99, "HOLD") is True
    assert delta_confirms_side(0.01, "SOMETHING") is True


# --- evaluate_window_delta (TA-agnostic getattr helper) ---------------------


def _ta(**kw):
    base = dict(current_price=100.0, atr_14=0.3, window_open_5m=99.5,
               window_open_15m=99.0, window_open_1h=98.0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_evaluate_reads_correct_window():
    # 15m window: open 99.0, current 100.0 => +1.01% move, up-lean prob.
    move, prob = evaluate_window_delta(_ta(), "15m", mins_left=7.0)
    assert move == pytest.approx((100.0 - 99.0) / 99.0 * 100.0)
    assert prob > 0.5


def test_evaluate_fails_open_when_unavailable():
    # Missing window open => None (caller must fail open, never block).
    assert evaluate_window_delta(_ta(window_open_15m=0.0), "15m", 7.0) is None
    # Unknown tf => None.
    assert evaluate_window_delta(_ta(), "4h", 7.0) is None
    # Zero current price => None.
    assert evaluate_window_delta(_ta(current_price=0.0), "5m", 7.0) is None


def test_evaluate_atr_missing_still_returns():
    # No ATR => fallback logistic path, still a usable lean.
    move, prob = evaluate_window_delta(_ta(atr_14=0.0), "5m", mins_left=2.0)
    assert move > 0  # current 100 > open 99.5
    assert prob > 0.5
