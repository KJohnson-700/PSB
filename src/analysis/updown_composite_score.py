"""Deterministic pre-AI quality gates for short-window up/down candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional


@dataclass(frozen=True)
class OracleValidation:
    passed: bool
    reason: str
    oracle_price: Optional[float]
    exchange_spot: Optional[float]
    basis_bps: Optional[float]
    freshness_sec: Optional[float]


@dataclass(frozen=True)
class CompositeScore:
    score: float
    passed: bool
    floor: float
    components: Dict[str, float]
    reason: str


WEIGHTS: Dict[str, float] = {
    "edge_quality": 0.20,
    "quant_confidence": 0.15,
    "micro_momentum": 0.20,
    "timeframe_alignment": 0.15,
    "oracle_integrity": 0.15,
    "entry_timing": 0.10,
    "market_price_quality": 0.05,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _freshness_seconds(updated_at: datetime, now: Optional[datetime]) -> float:
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return max(0.0, (ref - updated_at).total_seconds())


def validate_oracle_reference(
    *,
    oracle_price: Optional[float],
    exchange_spot: Optional[float],
    oracle_updated_at: Optional[datetime],
    max_age_sec: float,
    max_basis_bps: float,
    require_oracle: bool,
    now: Optional[datetime] = None,
    allow_exchange_when_oracle_missing: bool = False,
    stale_basis_relax_max_bps: Optional[float] = None,
) -> OracleValidation:
    """Validate oracle freshness and basis against the exchange spot feed.

    ``allow_exchange_when_oracle_missing``: when Chainlink fields are absent but
    ``require_oracle`` is True, still admit up/down if exchange spot exists (basis
    integrity unknown — use only when ops accepts exchange-only resolution risk).

    ``stale_basis_relax_max_bps``: when the feed on-chain ``updatedAt`` is older than
    ``max_age_sec`` but spot vs oracle still agrees within this many bps, pass anyway
    (slow-updating feeds vs tight freshness caps — common on some alt feeds).
    """
    def _spot_positive(sp: Optional[float]) -> Optional[float]:
        if sp is None:
            return None
        try:
            v = float(sp)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    spot_ok = _spot_positive(exchange_spot)

    if oracle_price is None and oracle_updated_at is None:
        if require_oracle and allow_exchange_when_oracle_missing and spot_ok is not None:
            return OracleValidation(
                passed=True,
                reason="oracle_exchange_only_missing_chainlink",
                oracle_price=None,
                exchange_spot=spot_ok,
                basis_bps=None,
                freshness_sec=None,
            )
        return OracleValidation(
            passed=not require_oracle,
            reason="oracle_missing" if require_oracle else "oracle_optional_missing",
            oracle_price=None,
            exchange_spot=exchange_spot,
            basis_bps=None,
            freshness_sec=None,
        )

    if oracle_price is None or oracle_updated_at is None or exchange_spot is None:
        return OracleValidation(
            passed=not require_oracle,
            reason="oracle_missing" if require_oracle else "oracle_optional_missing",
            oracle_price=oracle_price,
            exchange_spot=exchange_spot,
            basis_bps=None,
            freshness_sec=None,
        )

    oracle_f = float(oracle_price)
    spot_f = float(exchange_spot)
    if oracle_f <= 0 or spot_f <= 0:
        return OracleValidation(
            passed=not require_oracle,
            reason="oracle_missing" if require_oracle else "oracle_optional_invalid",
            oracle_price=oracle_price,
            exchange_spot=exchange_spot,
            basis_bps=None,
            freshness_sec=None,
        )

    freshness = _freshness_seconds(oracle_updated_at, now)
    basis = ((spot_f - oracle_f) / oracle_f) * 10000.0
    if freshness > float(max_age_sec):
        relax_cap = stale_basis_relax_max_bps
        if relax_cap is not None and abs(basis) <= float(relax_cap):
            return OracleValidation(
                passed=True,
                reason="oracle_stale_basis_relaxed",
                oracle_price=oracle_f,
                exchange_spot=spot_f,
                basis_bps=basis,
                freshness_sec=freshness,
            )
        return OracleValidation(
            passed=False,
            reason="oracle_stale",
            oracle_price=oracle_f,
            exchange_spot=spot_f,
            basis_bps=basis,
            freshness_sec=freshness,
        )
    if abs(basis) > float(max_basis_bps):
        return OracleValidation(
            passed=False,
            reason="oracle_basis_block",
            oracle_price=oracle_f,
            exchange_spot=spot_f,
            basis_bps=basis,
            freshness_sec=freshness,
        )
    return OracleValidation(
        passed=True,
        reason="oracle_ok",
        oracle_price=oracle_f,
        exchange_spot=spot_f,
        basis_bps=basis,
        freshness_sec=freshness,
    )


def score_updown_candidate(
    *,
    edge: float,
    min_edge: float,
    quant_confidence: float,
    micro_momentum: float,
    timeframe_alignment: float,
    oracle: OracleValidation,
    minutes_to_resolution: float,
    yes_price: float,
    floor: float,
) -> CompositeScore:
    """Return an auditable 0-1 candidate quality score before AI/sizing."""
    min_edge_f = max(0.0001, float(min_edge))
    edge_quality = _clamp(float(edge) / min_edge_f)
    confidence_quality = _clamp((float(quant_confidence) - 0.45) / 0.40)
    oracle_integrity = 1.0 if oracle.passed else 0.0
    # Prefer entries with some candle formed but not in the final scramble.
    mins = float(minutes_to_resolution)
    if mins <= 0:
        timing = 0.0
    elif mins < 1.0:
        timing = 0.25
    elif mins <= 14.5:
        timing = 1.0
    elif mins <= 16.0:
        timing = 0.70
    else:
        timing = 0.35
    # Centered books are usually cleaner for short-window up/down entries.
    market_price_quality = _clamp(1.0 - (abs(float(yes_price) - 0.50) / 0.12))

    components = {
        "edge_quality": edge_quality,
        "quant_confidence": confidence_quality,
        "micro_momentum": _clamp(micro_momentum),
        "timeframe_alignment": _clamp(timeframe_alignment),
        "oracle_integrity": oracle_integrity,
        "entry_timing": timing,
        "market_price_quality": market_price_quality,
    }
    score = sum(components[name] * weight for name, weight in WEIGHTS.items())
    score = _clamp(score)
    floor_f = _clamp(floor)
    return CompositeScore(
        score=score,
        passed=score >= floor_f,
        floor=floor_f,
        components=components,
        reason="composite_ok" if score >= floor_f else "composite_score_below_floor",
    )
