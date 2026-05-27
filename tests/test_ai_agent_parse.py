"""Regression: AI JSON parsing (MiniMax and other provider quirks)."""

import pytest

from datetime import datetime
import json
from types import MethodType, SimpleNamespace

import src.analysis.ai_agent as ai_agent_mod
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


def test_parse_minimax_probability_fallback_infers_missing_probability() -> None:
    raw = (
        '{"reasoning":"Momentum still favors YES.","confidence_score":"medium-high",'
        '"recommendation":"BUY_YES"}'
    )
    a = _agent()._parse_response_allow_probability_inference(raw, "m3b", 0.52)
    assert a.recommendation == "BUY_YES"
    assert a.confidence_score == 0.72
    assert a.estimated_probability > 0.52
    assert a.estimated_probability <= 0.95


def test_parse_minimax_probability_fallback_recovers_unusable_probability() -> None:
    raw = (
        '{"reasoning":"Momentum still favors YES.","confidence_score":0.8,'
        '"estimated_probability":"likely around sixty percent",'
        '"recommendation":"BUY_YES"}'
    )
    a = _agent()._parse_response_allow_probability_inference(raw, "m3c", 0.50)
    assert a.recommendation == "BUY_YES"
    assert a.estimated_probability > 0.50


def test_coerce_confidence_phrases() -> None:
    ag = _agent()
    assert ag._coerce_confidence_score("medium-high") == 0.72
    assert ag._coerce_confidence_score("high") == 0.82
    assert ag._coerce_confidence_score(0.65) == 0.65


def test_extract_openai_message_text_checks_kimi_alternate_fields() -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="",
                    reasoning_content=(
                        '{"reasoning":"compact","confidence_score":0.7,'
                        '"estimated_probability":0.58,"recommendation":"BUY_YES"}'
                    ),
                )
            )
        ]
    )

    assert AIAgent._extract_openai_message_text(response).startswith('{"reasoning"')


def test_kimi_empty_content_cools_down_without_second_attempt(monkeypatch) -> None:
    calls = {"n": 0}

    class FakeCompletions:
        async def create(self, **kwargs):
            calls["n"] += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=""))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=0),
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        def close(self):
            return None

    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "provider_chain": [],
                "temperature": 0.1,
                "max_tokens": 800,
            }
        }
    )

    async def fake_token(provider_config, *, force_refresh=False):
        return "token"

    monkeypatch.setattr(ai_agent_mod, "openai", SimpleNamespace(AsyncOpenAI=FakeClient))
    monkeypatch.setattr(ag, "_kimi_code_access_token", fake_token)

    with pytest.raises(AIResponseValidationError, match="empty Kimi response"):
        run_async(
            ag._analyze_with_kimi_coding(
                "prompt",
                "m-kimi-empty",
                "kimi-for-coding",
                "oauth",
                {
                    "name": "kimi_coding",
                    "type": "kimi_coding",
                    "json_mode": True,
                    "cooldown_on_parse_error_seconds": 600,
                    "retry_without_json_mode_on_400": False,
                },
                0.52,
            )
        )

    assert calls["n"] == 1
    assert ag._on_cooldown("kimi_coding:kimi-for-coding") is True


def test_kimi_cooldown_raises_explicit_reason() -> None:
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "provider_chain": [],
                "temperature": 0.1,
                "max_tokens": 800,
            }
        }
    )
    ag._set_cooldown("kimi_coding:kimi-for-coding", 600)

    with pytest.raises(RuntimeError, match="kimi_coding_cooldown:kimi-for-coding"):
        run_async(
            ag._analyze_with_kimi_coding(
                "prompt",
                "m-kimi-cooldown",
                "kimi-for-coding",
                "oauth",
                {
                    "name": "kimi_coding",
                    "type": "kimi_coding",
                    "model": "kimi-for-coding",
                },
                0.52,
            )
        )


def test_direct_decision_scope_uses_only_configured_provider() -> None:
    calls = []
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "live_inferencing": True,
                "provider_chain": [
                    {"name": "kimi_coding", "type": "fake_kimi", "model": "kimi", "local": True},
                    {"name": "minimax", "type": "fake_minimax", "model": "mini", "local": True},
                ],
                "direct_decision_provider_names": ["kimi_coding"],
                "direct_decision_fallback_to_chain": False,
                "min_call_gap": 0,
            }
        }
    )

    async def fake_kimi(self, prompt, market_id, model, api_key, provider_config, anchor_yes_price):
        calls.append(provider_config["name"])
        return AIAnalysis(
            reasoning="kimi decision",
            confidence_score=0.7,
            estimated_probability=0.62,
            recommendation="BUY_YES",
            market_id=market_id,
            timestamp=datetime.utcnow(),
        )

    async def fake_minimax(self, prompt, market_id, model, api_key, provider_config, anchor_yes_price):
        calls.append(provider_config["name"])
        return AIAnalysis(
            reasoning="minimax fallback",
            confidence_score=0.7,
            estimated_probability=0.38,
            recommendation="BUY_NO",
            market_id=market_id,
            timestamp=datetime.utcnow(),
        )

    ag._analyze_with_fake_kimi = MethodType(fake_kimi, ag)  # type: ignore[attr-defined]
    ag._analyze_with_fake_minimax = MethodType(fake_minimax, ag)  # type: ignore[attr-defined]

    out = run_async(
        ag.analyze_market(
            market_question="BTC up?",
            market_description="context",
            current_yes_price=0.52,
            market_id="m-direct-scope",
            strategy_hint="bitcoin",
            quant_action="BUY_YES",
            provider_scope="decision",
        )
    )

    assert out is not None
    assert out.recommendation == "BUY_YES"
    assert calls == ["kimi_coding"]


def test_analysis_scope_can_still_use_minimax_provider() -> None:
    calls = []
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "live_inferencing": True,
                "provider_chain": [
                    {"name": "kimi_coding", "type": "fake_kimi", "model": "kimi", "local": True},
                    {"name": "minimax", "type": "fake_minimax", "model": "mini", "local": True},
                ],
                "direct_decision_provider_names": ["kimi_coding"],
                "min_call_gap": 0,
            }
        }
    )

    async def fake_kimi(self, prompt, market_id, model, api_key, provider_config, anchor_yes_price):
        calls.append(provider_config["name"])
        raise RuntimeError("kimi unavailable")

    async def fake_minimax(self, prompt, market_id, model, api_key, provider_config, anchor_yes_price):
        calls.append(provider_config["name"])
        return AIAnalysis(
            reasoning="analysis fallback",
            confidence_score=0.65,
            estimated_probability=0.48,
            recommendation="BUY_NO",
            market_id=market_id,
            timestamp=datetime.utcnow(),
        )

    ag._analyze_with_fake_kimi = MethodType(fake_kimi, ag)  # type: ignore[attr-defined]
    ag._analyze_with_fake_minimax = MethodType(fake_minimax, ag)  # type: ignore[attr-defined]

    out = run_async(
        ag.analyze_market(
            market_question="diagnostic",
            market_description="context",
            current_yes_price=0.52,
            market_id="m-analysis-scope",
            strategy_hint="narrator",
            provider_scope="analysis",
        )
    )

    assert out is not None
    assert out.recommendation == "BUY_NO"
    assert calls == ["kimi_coding", "minimax"]


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


def test_cache_key_includes_lane_id() -> None:
    ag = _agent()
    k1 = ag._cache_key("m1", "bitcoin", "bitcoin|15m|down|bearish|ai_assisted")
    k2 = ag._cache_key("m1", "bitcoin", "bitcoin|15m|up|bullish|ai_assisted")
    assert k1 != k2


def test_lane_feedback_context_includes_prompt_version_and_bucket_stats(tmp_path) -> None:
    trades = tmp_path / "trades.jsonl"
    trades.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "strategy": "bitcoin",
                        "window": "15m",
                        "side": "BUY_NO",
                        "lane_id": "bitcoin|15m|down|bearish|ai_assisted",
                        "stated_est_prob": 0.40,
                        "win": False,
                    }
                ),
                json.dumps(
                    {
                        "strategy": "bitcoin",
                        "window": "15m",
                        "side": "BUY_NO",
                        "lane_id": "bitcoin|15m|down|bearish|ai_assisted",
                        "stated_est_prob": 0.35,
                        "win": True,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    posteriors = tmp_path / "lane_posteriors.json"
    posteriors.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lanes": {
                    "bitcoin|15m|down|bearish|ai_assisted": {
                        "n": 12,
                        "alpha_ewma": 0.85,
                        "beta_a": 6.0,
                        "beta_b": 8.0,
                        "last_updated": "2026-05-17T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    rejected = tmp_path / "rejected_candidates_settled.jsonl"
    rejected.write_text(
        json.dumps(
            {
                "strategy": "bitcoin",
                "window": "15m",
                "action": "BUY_NO",
                "win": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "provider_chain": [],
                "prompt_version": "lane-feedback-test",
                "lane_feedback_min_samples": 1,
                "trades_log_path": str(trades),
                "lane_posteriors_path": str(posteriors),
                "rejected_settled_path": str(rejected),
            }
        }
    )

    text = ag._build_lane_feedback_context(
        strategy_hint="bitcoin",
        lane_id="bitcoin|15m|down|bearish|ai_assisted",
        quant_action="BUY_NO",
    )

    assert "lane-feedback-test" in text
    assert "bitcoin 15m BUY_NO" in text
    assert "too optimistic historically" in text
    assert "Rejected sibling candidates" in text


def test_lane_feedback_bundle_exposes_source_metadata(tmp_path) -> None:
    trades = tmp_path / "trades.jsonl"
    trades.write_text(
        json.dumps(
            {
                "strategy": "bitcoin",
                "window": "15m",
                "side": "BUY_NO",
                "lane_id": "bitcoin|15m|down|bearish|ai_assisted",
                "stated_est_prob": 0.40,
                "win": True,
                "entry_price_bucket": "0.49_0.51",
                "regime_tag_bucket": "bearish",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    posteriors = tmp_path / "lane_posteriors.json"
    posteriors.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lanes": {
                    "bitcoin|15m|down|bearish|ai_assisted": {
                        "n": 2,
                        "alpha_ewma": 0.9,
                        "beta_a": 3.0,
                        "beta_b": 3.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "provider_chain": [],
                "lane_feedback_min_samples": 1,
                "trades_log_path": str(trades),
                "lane_posteriors_path": str(posteriors),
            }
        }
    )
    bundle = ag._build_lane_feedback_bundle(
        strategy_hint="bitcoin",
        lane_id="bitcoin|15m|down|bearish|ai_assisted",
        quant_action="BUY_NO",
        calibration_bucket_tags={"edge_bucket": "0.05_0.08"},
    )
    assert "exact_lane_posterior" in bundle["feedback_sources_used"]
    assert "lane_family_trades" in bundle["feedback_sources_used"]
    assert bundle["sample_count_used"]["exact_lane_posterior"] == 2
    assert bundle["bucket_tags"]["edge_bucket"] == "0.05_0.08"


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


def test_parse_portfolio_shadow_accepts_summary_aliases() -> None:
    ag = _agent()
    pd = ag._parse_portfolio_shadow_response(
        '{"rating":"BULLISH","action":"BUY_YES","summary":"go",'
        '"investment_horizon":"15m","position_size":0.2,"entry_price":0.5,'
        '"stop_loss":null,"metadata":{}}',
        "m1",
    )
    assert pd.executive_summary == "go"


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


def test_run_shadow_pipeline_logs_prompt_policy_and_feature_hash(
    tmp_path, monkeypatch
) -> None:
    from src.analysis import ai_agent as ai_agent_mod

    shadow_log = tmp_path / "shadow_pipeline.jsonl"
    monkeypatch.setattr(ai_agent_mod, "SHADOW_PIPELINE_LOG_FILE", shadow_log)

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
                "prompt_version": "prompt-v-test",
                "policy_version": "policy-v-test",
                "shadow_pipeline": {"enabled": True, "log_jsonl": True},
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
                '{"rating":"BULLISH","action":"BUY_YES","executive_summary":"e",'
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
            market_id="m-shadow-log",
            strategy_hint="eth_macro",
            lane_id="eth_macro|15m|down|bearish|marginal",
            marginal_recommendation="BUY_YES",
            quant_action="BUY_YES",
            quant_edge=0.06,
            quant_threshold=0.09,
        )
    )

    assert out is not None
    assert out["prompt_version"] == "prompt-v-test"
    assert out["policy_version"] == "policy-v-test"
    assert out["feature_hash"]

    rows = shadow_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    payload = json.loads(rows[0])
    assert payload["prompt_version"] == "prompt-v-test"
    assert payload["policy_version"] == "policy-v-test"
    assert payload["feature_hash"] == out["feature_hash"]


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
            quant_edge=0.14,
            quant_confidence=0.54,
            quant_threshold=0.12,
        )
    )
    assert decision.approved is False
    assert decision.reason == "direct_ai_low_confidence"


def test_decision_layer_enforced_lane_config_helpers() -> None:
    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "provider_chain": [],
                "decision_layer": {
                    "enabled": True,
                    "hard_skip_if_unavailable_on_enforced": True,
                    "enforced_lanes": {
                        "bitcoin": ["neutral_15m", "marginal"],
                        "hype_macro": ["marginal"],
                    },
                    "shadow_required_lanes": {
                        "bitcoin": ["marginal"],
                        "hype_macro": ["marginal"],
                    },
                },
            }
        }
    )

    assert ag.decision_layer_lane_enforced("bitcoin", "neutral_15m") is True
    assert ag.decision_layer_lane_enforced("bitcoin", "5m") is False
    assert ag.decision_layer_lane_requires_shadow("bitcoin", "marginal") is True
    assert ag.decision_layer_lane_requires_shadow("bitcoin", "neutral_15m") is False
    assert ag.decision_layer_lane_enforced("hype_macro", "marginal") is True
    assert ag.decision_layer_hard_skip_unavailable("hype_macro", "marginal") is True
    assert ag.decision_layer_lane_requires_shadow("hype_macro", "marginal") is True
    assert ag.decision_layer_lane_enforced("hype_macro", "default") is False


def test_decision_layer_rejects_hold_and_action_mismatch() -> None:
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

    async def fake_hold(**kwargs):
        return AIAnalysis(
            reasoning="wait",
            confidence_score=0.90,
            estimated_probability=0.70,
            recommendation="HOLD",
            market_id=kwargs["market_id"],
            timestamp=datetime.utcnow(),
        )

    ag.analyze_market = fake_hold  # type: ignore[method-assign]
    hold = run_async(
        ag.evaluate_trade_decision(
            market_question="BTC up?",
            market_description="context",
            current_yes_price=0.52,
            market_id="m-hold",
            strategy_hint="bitcoin",
            quant_action="BUY_YES",
            quant_edge=0.14,
            quant_confidence=0.70,
            quant_threshold=0.12,
        )
    )
    assert hold.approved is False
    assert hold.reason == "direct_ai_hold"

    async def fake_mismatch(**kwargs):
        return AIAnalysis(
            reasoning="downside",
            confidence_score=0.90,
            estimated_probability=0.30,
            recommendation="BUY_NO",
            market_id=kwargs["market_id"],
            timestamp=datetime.utcnow(),
        )

    ag.analyze_market = fake_mismatch  # type: ignore[method-assign]
    mismatch = run_async(
        ag.evaluate_trade_decision(
            market_question="BTC up?",
            market_description="context",
            current_yes_price=0.52,
            market_id="m-mismatch",
            strategy_hint="bitcoin",
            quant_action="BUY_YES",
            quant_edge=0.10,
            quant_confidence=0.70,
            quant_threshold=0.12,
        )
    )
    assert mismatch.approved is False
    assert mismatch.reason == "direct_ai_action_mismatch"


def test_decision_layer_abstains_on_marginal_hold_and_low_confidence() -> None:
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

    async def fake_hold(**kwargs):
        return AIAnalysis(
            reasoning="wait",
            confidence_score=0.90,
            estimated_probability=0.70,
            recommendation="HOLD",
            market_id=kwargs["market_id"],
            timestamp=datetime.utcnow(),
        )

    ag.analyze_market = fake_hold  # type: ignore[method-assign]
    hold = run_async(
        ag.evaluate_trade_decision(
            market_question="BTC up?",
            market_description="context",
            current_yes_price=0.52,
            market_id="m-hold-marginal",
            strategy_hint="bitcoin",
            quant_action="BUY_YES",
            quant_edge=0.10,
            quant_confidence=0.70,
            quant_threshold=0.12,
        )
    )
    assert hold.approved is True
    assert hold.action == "BUY_YES"
    assert hold.reason == "direct_ai_hold_abstain_marginal"

    async def fake_low_conf(**kwargs):
        return AIAnalysis(
            reasoning="unclear",
            confidence_score=0.51,
            estimated_probability=0.64,
            recommendation="BUY_YES",
            market_id=kwargs["market_id"],
            timestamp=datetime.utcnow(),
        )

    ag.analyze_market = fake_low_conf  # type: ignore[method-assign]
    low_conf = run_async(
        ag.evaluate_trade_decision(
            market_question="BTC up?",
            market_description="context",
            current_yes_price=0.52,
            market_id="m-low-conf-marginal",
            strategy_hint="bitcoin",
            quant_action="BUY_YES",
            quant_edge=0.10,
            quant_confidence=0.54,
            quant_threshold=0.12,
        )
    )
    assert low_conf.approved is True
    assert low_conf.action == "BUY_YES"
    assert low_conf.reason == "direct_ai_low_confidence_abstain_marginal"


def test_decision_layer_rejects_ai_unavailable_and_shadow_mismatch_when_required() -> None:
    unavailable = AIAgent(
        {
            "ai": {
                "enabled": True,
                "live_inferencing": False,
                "provider_chain": [{"name": "fake", "type": "fake"}],
                "decision_layer": {"enabled": True, "min_confidence": 0.60},
            }
        }
    )
    decision = run_async(
        unavailable.evaluate_trade_decision(
            market_question="BTC up?",
            market_description="context",
            current_yes_price=0.52,
            market_id="m-unavailable",
            strategy_hint="bitcoin",
            quant_action="BUY_YES",
            quant_edge=0.10,
            quant_confidence=0.70,
            quant_threshold=0.12,
        )
    )
    assert decision.approved is False
    assert decision.reason == "ai_unavailable"

    ag = AIAgent(
        {
            "ai": {
                "enabled": True,
                "live_inferencing": True,
                "provider_chain": [{"name": "fake", "type": "fake"}],
                "decision_layer": {"enabled": True, "min_confidence": 0.60},
                "shadow_pipeline": {"enabled": True, "log_jsonl": False},
            }
        }
    )

    async def fake_analyze_market(**kwargs):
        return AIAnalysis(
            reasoning="ok",
            confidence_score=0.82,
            estimated_probability=0.64,
            recommendation="BUY_YES",
            market_id=kwargs["market_id"],
            timestamp=datetime.utcnow(),
        )

    async def fake_shadow(**kwargs):
        return {"ok": True, "portfolio_action": "BUY_NO", "shadow_confidence": 0.90}

    ag.analyze_market = fake_analyze_market  # type: ignore[method-assign]
    ag.run_shadow_pipeline = fake_shadow  # type: ignore[method-assign]
    shadow = run_async(
        ag.evaluate_trade_decision(
            market_question="BTC up?",
            market_description="context",
            current_yes_price=0.52,
            market_id="m-shadow-required",
            strategy_hint="bitcoin",
            quant_action="BUY_YES",
            quant_edge=0.10,
            quant_confidence=0.70,
            quant_threshold=0.12,
            require_shadow_portfolio=True,
        )
    )
    assert shadow.approved is False
    assert shadow.reason == "shadow_portfolio_action_mismatch"
