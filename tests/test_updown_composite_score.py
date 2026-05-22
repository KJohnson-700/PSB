from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.updown_composite_score import (
    CompositeScore,
    validate_oracle_reference,
    score_updown_candidate,
)


def test_high_quality_setup_passes_default_floor() -> None:
    oracle = validate_oracle_reference(
        oracle_price=100.0,
        exchange_spot=100.05,
        oracle_updated_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
    )

    score = score_updown_candidate(
        edge=0.11,
        min_edge=0.09,
        quant_confidence=0.72,
        micro_momentum=0.80,
        timeframe_alignment=0.85,
        oracle=oracle,
        minutes_to_resolution=8.0,
        yes_price=0.51,
        floor=0.62,
    )

    assert isinstance(score, CompositeScore)
    assert score.passed is True
    assert score.score >= 0.62
    assert score.components["oracle_integrity"] == 1.0


def test_low_momentum_fails_even_with_valid_oracle() -> None:
    oracle = validate_oracle_reference(
        oracle_price=100.0,
        exchange_spot=100.02,
        oracle_updated_at=datetime.now(timezone.utc) - timedelta(seconds=20),
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
    )

    score = score_updown_candidate(
        edge=0.07,
        min_edge=0.09,
        quant_confidence=0.52,
        micro_momentum=0.0,
        timeframe_alignment=0.20,
        oracle=oracle,
        minutes_to_resolution=14.0,
        yes_price=0.56,
        floor=0.62,
    )

    assert score.passed is False
    assert score.reason == "composite_score_below_floor"
    assert score.components["micro_momentum"] == 0.0


def test_missing_stale_and_bad_basis_oracle_fail_validation() -> None:
    missing = validate_oracle_reference(
        oracle_price=None,
        exchange_spot=100.0,
        oracle_updated_at=None,
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
    )
    assert missing.passed is False
    assert missing.reason == "oracle_missing"

    stale = validate_oracle_reference(
        oracle_price=100.0,
        exchange_spot=100.0,
        oracle_updated_at=datetime.now(timezone.utc) - timedelta(seconds=181),
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
    )
    assert stale.passed is False
    assert stale.reason == "oracle_stale"

    bad_basis = validate_oracle_reference(
        oracle_price=100.0,
        exchange_spot=100.20,
        oracle_updated_at=datetime.now(timezone.utc),
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
    )
    assert bad_basis.passed is False
    assert bad_basis.reason == "oracle_basis_block"
    assert bad_basis.basis_bps == pytest.approx(20.0)

    fresh_relaxed = validate_oracle_reference(
        oracle_price=100.0,
        exchange_spot=100.1059322,
        oracle_updated_at=datetime.now(timezone.utc),
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
        basis_relax_max_bps=12.0,
    )
    assert fresh_relaxed.passed is True
    assert fresh_relaxed.reason == "oracle_basis_relaxed"

    relaxed = validate_oracle_reference(
        oracle_price=100.0,
        exchange_spot=100.02,
        oracle_updated_at=datetime.now(timezone.utc) - timedelta(seconds=5000),
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
        stale_basis_relax_max_bps=35.0,
    )
    assert relaxed.passed is True
    assert relaxed.reason == "oracle_stale_basis_relaxed"

    exchange_only = validate_oracle_reference(
        oracle_price=None,
        exchange_spot=100.0,
        oracle_updated_at=None,
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
        allow_exchange_when_oracle_missing=True,
    )
    assert exchange_only.passed is True
    assert exchange_only.reason == "oracle_exchange_only_missing_chainlink"

    stale_wide_basis = validate_oracle_reference(
        oracle_price=100.0,
        exchange_spot=100.50,
        oracle_updated_at=datetime.now(timezone.utc) - timedelta(seconds=5000),
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
        stale_basis_relax_max_bps=35.0,
    )
    assert stale_wide_basis.passed is False
    assert stale_wide_basis.reason == "oracle_stale"


def test_lane_floors_rank_btc_neutral_and_stricter_floors():
    oracle = validate_oracle_reference(
        oracle_price=100.0,
        exchange_spot=100.01,
        oracle_updated_at=datetime.now(timezone.utc),
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
    )
    kwargs = dict(
        edge=0.067,
        min_edge=0.09,
        quant_confidence=0.58,
        micro_momentum=0.55,
        timeframe_alignment=0.55,
        oracle=oracle,
        minutes_to_resolution=8.0,
        yes_price=0.54,
    )

    default = score_updown_candidate(**kwargs, floor=0.62)
    btc_neutral = score_updown_candidate(**kwargs, floor=0.68)
    strict_floor = score_updown_candidate(**kwargs, floor=0.70)

    assert default.passed is True
    assert btc_neutral.floor > default.floor
    assert strict_floor.floor > btc_neutral.floor
    assert strict_floor.passed is False


def test_btc_regime_action_gate_blocks_weak_chase_entries() -> None:
    oracle = validate_oracle_reference(
        oracle_price=100.0,
        exchange_spot=100.01,
        oracle_updated_at=datetime.now(timezone.utc),
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
    )

    score = score_updown_candidate(
        edge=0.075,
        min_edge=0.09,
        quant_confidence=0.52,
        micro_momentum=0.45,
        timeframe_alignment=0.45,
        oracle=oracle,
        minutes_to_resolution=8.0,
        yes_price=0.54,
        floor=0.50,
        action="BUY_YES",
        btc_1h_regime="BULL",
        regime_action_gate_enabled=True,
        regime_action_min_convergence=0.55,
    )

    assert score.passed is False
    assert score.reason == "btc_regime_action_block"
    assert score.components["btc_1h_regime_alignment"] == 0.25


def test_btc_regime_action_gate_allows_strong_convergence_chase_entries() -> None:
    oracle = validate_oracle_reference(
        oracle_price=100.0,
        exchange_spot=100.01,
        oracle_updated_at=datetime.now(timezone.utc),
        max_age_sec=180,
        max_basis_bps=10.0,
        require_oracle=True,
    )

    score = score_updown_candidate(
        edge=0.13,
        min_edge=0.09,
        quant_confidence=0.80,
        micro_momentum=0.90,
        timeframe_alignment=0.90,
        oracle=oracle,
        minutes_to_resolution=8.0,
        yes_price=0.50,
        floor=0.62,
        action="BUY_NO",
        btc_1h_regime="BEAR",
        regime_action_gate_enabled=True,
        regime_action_min_convergence=0.55,
    )

    assert score.passed is True
    assert score.reason == "composite_ok"
    assert score.convergence_score >= 0.55
