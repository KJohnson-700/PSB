"""Pre-entry veto + shadow-pipeline config helpers on AIAgent."""

from src.analysis.ai_agent import AIAgent


def _agent(ai_cfg: dict) -> AIAgent:
    return AIAgent({"ai": ai_cfg})


def test_preentry_veto_disabled_by_default() -> None:
    a = _agent({"enabled": True, "provider_chain": []})
    assert a.preentry_veto_active(0.0) is False
    assert a.preentry_veto_active(0.99) is False


def test_preentry_veto_fires_below_threshold() -> None:
    a = _agent({
        "enabled": True,
        "provider_chain": [],
        "preentry_veto": {"enabled": True, "min_confidence": 0.25},
    })
    assert a.preentry_veto_active(0.10) is True
    assert a.preentry_veto_active(0.24) is True


def test_preentry_veto_passes_at_or_above_threshold() -> None:
    a = _agent({
        "enabled": True,
        "provider_chain": [],
        "preentry_veto": {"enabled": True, "min_confidence": 0.25},
    })
    assert a.preentry_veto_active(0.25) is False
    assert a.preentry_veto_active(0.80) is False


def test_preentry_veto_handles_bad_threshold() -> None:
    a = _agent({
        "enabled": True,
        "provider_chain": [],
        "preentry_veto": {"enabled": True, "min_confidence": "garbage"},
    })
    assert a.preentry_veto_active(0.10) is False


def test_preentry_veto_disabled_flag_overrides_threshold() -> None:
    a = _agent({
        "enabled": True,
        "provider_chain": [],
        "preentry_veto": {"enabled": False, "min_confidence": 0.99},
    })
    assert a.preentry_veto_active(0.0) is False


def test_shadow_pipeline_helpers_round_trip() -> None:
    a = _agent({
        "enabled": True,
        "provider_chain": [],
        "shadow_pipeline": {
            "enabled": True,
            "max_calls_per_scan": 3,
            "min_confidence_to_log": 0.6,
        },
    })
    assert a.shadow_pipeline_enabled() is True
    assert a.shadow_pipeline_max_calls_per_scan() == 3
    assert a.shadow_pipeline_min_confidence() == 0.6


def test_shadow_observer_helpers_round_trip() -> None:
    a = _agent({
        "enabled": True,
        "provider_chain": [],
        "shadow_observer": {
            "enabled": True,
            "max_calls_per_scan": 2,
            "reasons": ["liquidity", "lane_entry_window"],
        },
    })
    assert a.shadow_observer_enabled() is True
    assert a.shadow_observer_max_calls_per_scan() == 2
    assert a.shadow_observer_reasons() == {"liquidity", "lane_entry_window"}


def test_refresh_from_config_picks_up_veto() -> None:
    a = _agent({"enabled": True, "provider_chain": []})
    assert a.preentry_veto_active(0.1) is False
    a.refresh_from_config({
        "enabled": True,
        "provider_chain": [],
        "preentry_veto": {"enabled": True, "min_confidence": 0.30},
    })
    assert a.preentry_veto_active(0.1) is True
    assert a.preentry_veto_active(0.40) is False
