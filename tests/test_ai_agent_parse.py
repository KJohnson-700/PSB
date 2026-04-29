"""Regression: AI JSON parsing (MiniMax and other provider quirks)."""

import pytest

from src.analysis.ai_agent import AIAgent, AIResponseValidationError


def _agent() -> AIAgent:
    return AIAgent({"ai": {"enabled": True, "provider_chain": []}})


def test_parse_minimax_style_json() -> None:
    raw = (
        '{"recommendation": "YES", "confidence_score": "medium-high", '
        '"reasoning": "BTC correlation suggests upside.", '
        '"estimated_probability": 0.55}'
    )
    a = _agent()._parse_response(raw, "m1", anchor_yes_price=0.52)
    assert a.recommendation == "BUY_YES"
    assert a.reasoning.startswith("BTC")
    assert 0.0 < a.confidence_score < 1.0
    assert abs(a.estimated_probability - 0.55) < 1e-6


def test_parse_strict_json_unchanged() -> None:
    raw = (
        '{"reasoning":"x","confidence_score":0.7,"estimated_probability":0.58,'
        '"recommendation":"BUY_YES"}'
    )
    a = _agent()._parse_response(raw, "m2", anchor_yes_price=0.5)
    assert a.estimated_probability == 0.58
    assert a.confidence_score == 0.7


def test_parse_missing_estimated_probability_raises() -> None:
    raw = (
        '{"reasoning":"x","confidence_score":0.7,"recommendation":"BUY_YES"}'
    )
    with pytest.raises(AIResponseValidationError, match="estimated_probability"):
        _agent()._parse_response(raw, "m3", 0.5)


def test_coerce_confidence_phrases() -> None:
    ag = _agent()
    assert ag._coerce_confidence_score("medium-high") == 0.72
    assert ag._coerce_confidence_score("high") == 0.82
    assert ag._coerce_confidence_score(0.65) == 0.65


def test_short_window_cache_ttl_overrides_legacy_default() -> None:
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "provider_chain": [],
                "cache_ttl": 600,
                "cache_ttl_15m": 180,
                "cache_ttl_5m": 60,
            }
        }
    )

    assert ag._cache_ttl_for_market("Bitcoin Up or Down 15m", "bitcoin") == 180
    assert ag._cache_ttl_for_market("Ethereum Up or Down 5m", "eth_macro") == 60
    assert ag._cache_ttl_for_market("Will BTC hit $120k?", "bitcoin") == 600
