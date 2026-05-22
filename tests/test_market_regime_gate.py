from __future__ import annotations

from src.main import market_regime_gate_decision


def test_market_regime_gate_blocks_low_convergence_deadzone() -> None:
    allowed, reason, extra = market_regime_gate_decision(
        gate_config={"enabled": True, "deadzone_min_convergence": 0.55},
        latest_regime={
            "ts": "2026-05-21T12:00:00+00:00",
            "price_regime": "flat",
            "polymarket_regime": "deadzone",
            "combined_regime": "deadzone_confirmed",
        },
        convergence_score=0.42,
    )

    assert allowed is False
    assert reason == "market_deadzone_low_convergence"
    assert extra["combined_regime"] == "deadzone_confirmed"


def test_market_regime_gate_allows_active_or_strong_convergence() -> None:
    active_allowed, active_reason, _ = market_regime_gate_decision(
        gate_config={"enabled": True, "deadzone_min_convergence": 0.55},
        latest_regime={"combined_regime": "active", "polymarket_regime": "signal"},
        convergence_score=0.2,
    )
    deadzone_allowed, deadzone_reason, _ = market_regime_gate_decision(
        gate_config={"enabled": True, "deadzone_min_convergence": 0.55},
        latest_regime={"combined_regime": "deadzone_confirmed", "polymarket_regime": "deadzone"},
        convergence_score=0.72,
    )

    assert active_allowed is True
    assert active_reason == "not_deadzone"
    assert deadzone_allowed is True
    assert deadzone_reason == "deadzone_convergence_ok"
