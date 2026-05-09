"""Regression: AI JSON parsing (MiniMax and other provider quirks)."""

import pytest

from datetime import datetime

from src.analysis.ai_agent import AIAgent, AIAnalysis, AIResponseValidationError
from tests.async_helpers import run_async


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


def test_parse_salvages_missing_comma_between_fields() -> None:
    raw = (
        '{"reasoning":"BTC is strong and breadth remains supportive." '
        '"confidence_score":"medium-high",'
        '"estimated_probability":0.61,'
        '"recommendation":"BUY_YES"}'
    )
    a = _agent()._parse_response(raw, "m2b", anchor_yes_price=0.5)
    assert a.recommendation == "BUY_YES"
    assert a.reasoning.startswith("BTC is strong")
    assert a.confidence_score == 0.72
    assert a.estimated_probability == 0.61


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


def test_research_narrative_config_helpers() -> None:
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "provider_chain": [],
                "research_narrative": {
                    "enabled": True,
                    "max_calls_per_scan": 3,
                    "min_confidence_to_log": 0.62,
                },
            }
        }
    )
    assert ag.research_narrative_enabled() is True
    assert ag.research_narrative_max_calls_per_scan() == 3
    assert abs(ag.research_narrative_min_confidence() - 0.62) < 1e-9


def test_analyze_research_plan_uses_research_prompt_and_parser() -> None:
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "provider_chain": [
                    {
                        "name": "fake_provider",
                        "type": "fake",
                        "model": "fake-model",
                        "api_key_secret": "FAKE_KEY",
                    }
                ],
                "research_narrative": {
                    "enabled": True,
                    "log_jsonl": False,
                },
            }
        }
    )
    ag.set_api_keys({"FAKE_KEY": "token"})

    async def _fake_provider(
        prompt: str,
        market_id: str,
        model: str,
        api_key: str,
        provider_config: dict,
        anchor_yes_price: float = 0.0,
        response_parser=None,
        system_prompt=None,
    ):
        assert "RESEARCH MANAGER REQUEST" in prompt
        assert system_prompt == ag.RESEARCH_SYSTEM_PROMPT
        assert response_parser is not None
        payload = (
            '{"market":"Will ETH close above 3k?","recommendation":"BULLISH",'
            '"rationale":"Context is supportive.","strategic_actions":["Wait for confirm"],'
            '"confidence":0.79}'
        )
        return response_parser(payload, market_id, anchor_yes_price)

    setattr(ag, "_analyze_with_fake", _fake_provider)
    plan = run_async(
        ag.analyze_research_plan(
            market_question="Will ETH close above 3k?",
            market_description="Context block",
            current_yes_price=0.52,
            market_id="m-research",
            strategy_hint="eth_macro",
            quant_action="BUY_YES",
            quant_edge=0.07,
            quant_threshold=0.09,
        )
    )
    assert plan is not None
    assert plan.market.startswith("Will ETH")
    assert plan.recommendation.value in {"BULLISH", "CONFIDENT_BULLISH"}
    assert len(plan.strategic_actions) >= 1


def test_research_provider_chain_selects_named_provider_and_model_override() -> None:
    ag = AIAgent(
        {
            "ai": {
                "provider_chain": [
                    {"name": "minimax", "type": "minimax", "model": "base-a"},
                    {"name": "openrouter_free", "type": "openai", "model": "base-b"},
                ],
                "research_narrative": {
                    "enabled": True,
                    "provider_name": "openrouter_free",
                    "model_override": "research-model-x",
                },
            }
        }
    )
    chain = ag._research_provider_chain()
    assert len(chain) == 1
    assert chain[0]["name"] == "openrouter_free"
    assert chain[0]["model"] == "research-model-x"


def test_shadow_pipeline_config_helpers() -> None:
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "provider_chain": [],
                "shadow_pipeline": {
                    "enabled": True,
                    "max_calls_per_scan": 2,
                    "min_confidence_to_log": 0.55,
                },
            }
        }
    )
    assert ag.shadow_pipeline_enabled() is True
    assert ag.shadow_pipeline_max_calls_per_scan() == 2
    assert abs(ag.shadow_pipeline_min_confidence() - 0.55) < 1e-9


def test_parse_trader_and_portfolio_shadow_json() -> None:
    ag = _agent()
    tr = ag._parse_trader_response(
        '{"action":"BUY_YES","reasoning":"ok","entry_price":0.52,"stop_loss":null,'
        '"position_sizing":0.1,"max_loss_acceptable":null}',
        "m1",
    )
    assert tr.action.value == "BUY_YES"
    assert tr.position_sizing == 0.1
    pd = ag._parse_portfolio_shadow_response(
        '{"rating":"BULLISH","action":"BUY_YES","executive_summary":"go",'
        '"investment_horizon":"15m","position_size":0.2,"entry_price":0.5,'
        '"stop_loss":null,"metadata":{}}',
        "m1",
    )
    assert pd.rating.value == "BULLISH"
    assert pd.position_size == 0.2


def test_shadow_marginal_mismatch() -> None:
    ag = _agent()
    from src.ai.schema import TraderAction

    assert ag._shadow_marginal_mismatch("BUY_YES", TraderAction.BUY_NO) is True
    assert ag._shadow_marginal_mismatch("BUY_YES", TraderAction.BUY_YES) is False


def test_run_shadow_pipeline_three_stages_fake() -> None:
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "provider_chain": [
                    {
                        "name": "fake_provider",
                        "type": "fake",
                        "model": "fake-model",
                        "api_key_secret": "FAKE_KEY",
                    }
                ],
                "shadow_pipeline": {"enabled": True, "log_jsonl": False},
            }
        }
    )
    ag.set_api_keys({"FAKE_KEY": "token"})

    async def _fake_provider(
        prompt: str,
        market_id: str,
        model: str,
        api_key: str,
        provider_config: dict,
        anchor_yes_price: float = 0.0,
        response_parser=None,
        system_prompt=None,
    ):
        assert response_parser is not None
        if system_prompt == ag.RESEARCH_SYSTEM_PROMPT:
            payload = (
                '{"market":"Q","recommendation":"BULLISH","rationale":"r",'
                '"strategic_actions":["a"],"confidence":0.7}'
            )
        elif system_prompt == ag.TRADER_SHADOW_SYSTEM_PROMPT:
            payload = (
                '{"action":"BUY_YES","reasoning":"t","entry_price":0.5,'
                '"stop_loss":null,"position_sizing":0.1,"max_loss_acceptable":null}'
            )
        elif system_prompt == ag.PORTFOLIO_SHADOW_SYSTEM_PROMPT:
            payload = (
                '{"rating":"BULLISH","action":"BUY_NO","executive_summary":"e",'
                '"investment_horizon":"15m","position_size":0.1,"entry_price":0.5,'
                '"stop_loss":null,"metadata":{}}'
            )
        else:
            raise AssertionError("unexpected system prompt")
        return response_parser(payload, market_id, anchor_yes_price)

    setattr(ag, "_analyze_with_fake", _fake_provider)
    out = run_async(
        ag.run_shadow_pipeline(
            market_question="Q",
            market_description="D",
            current_yes_price=0.48,
            market_id="m-shadow",
            strategy_hint="eth_macro",
            marginal_recommendation="BUY_YES",
            quant_action="BUY_YES",
            quant_edge=0.06,
        )
    )
    assert out is not None
    assert out.get("ok") is True
    assert out.get("marginal_mismatch") is True
    assert out.get("portfolio_action") == "BUY_NO"


def test_ai_agent_accepts_full_root_config_or_ai_section_only() -> None:
    ai_block = {"enabled": True, "provider_chain": [{"name": "p", "type": "openai"}]}
    from_full = AIAgent({"trading": {"dry_run": True}, "ai": ai_block})
    assert from_full.provider_chain == ai_block["provider_chain"]
    from_section = AIAgent(
        {"enabled": True, "provider_chain": [{"name": "q", "type": "groq"}]}
    )
    assert from_section.provider_chain[0]["name"] == "q"

    from_full.refresh_from_config({"trading": {}, "ai": ai_block})
    assert from_full.provider_chain == ai_block["provider_chain"]
    from_full.refresh_from_config({"enabled": False, "provider_chain": []})
    assert from_full.provider_chain == []


def test_decision_layer_approves_matching_ai_action() -> None:
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "live_inferencing": True,
                "provider_chain": [{"name": "fake", "type": "fake"}],
                "decision_layer": {"enabled": True, "min_confidence": 0.60},
            }
        }
    )

    async def fake_analyze_market(**kwargs):
        return AIAnalysis(
            reasoning="ok",
            confidence_score=0.72,
            estimated_probability=0.64,
            recommendation="BUY_YES",
            market_id=kwargs["market_id"],
            timestamp=datetime.utcnow(),
        )

    ag.analyze_market = fake_analyze_market  # type: ignore[method-assign]
    decision = run_async(
        ag.evaluate_trade_decision(
            market_question="BTC up?",
            market_description="context",
            current_yes_price=0.52,
            market_id="m-decision",
            strategy_hint="bitcoin",
            quant_action="BUY_YES",
            quant_edge=0.10,
            quant_confidence=0.54,
            quant_threshold=0.12,
        )
    )
    assert decision.approved is True
    assert decision.action == "BUY_YES"
    assert decision.edge == pytest.approx(0.12)


def test_decision_layer_rejects_low_confidence_ai_action() -> None:
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "live_inferencing": True,
                "provider_chain": [{"name": "fake", "type": "fake"}],
                "decision_layer": {"enabled": True, "min_confidence": 0.60},
            }
        }
    )

    async def fake_analyze_market(**kwargs):
        return AIAnalysis(
            reasoning="unclear",
            confidence_score=0.51,
            estimated_probability=0.64,
            recommendation="BUY_YES",
            market_id=kwargs["market_id"],
            timestamp=datetime.utcnow(),
        )

    ag.analyze_market = fake_analyze_market  # type: ignore[method-assign]
    decision = run_async(
        ag.evaluate_trade_decision(
            market_question="BTC up?",
            market_description="context",
            current_yes_price=0.52,
            market_id="m-low-conf",
            strategy_hint="bitcoin",
            quant_action="BUY_YES",
            quant_edge=0.10,
            quant_confidence=0.54,
            quant_threshold=0.12,
        )
    )
    assert decision.approved is False
    assert decision.reason == "direct_ai_low_confidence"
