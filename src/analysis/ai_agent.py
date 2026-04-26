"""
AIAgent for Market Analysis

This module defines the AIAgent, which is responsible for analyzing market
conditions using a chain of Large Language Models (LLMs).

Architecture Overview:
- Provider Chain: The agent uses a `provider_chain` defined in `config/settings.yaml`.
  It attempts to get an analysis from each provider in order until one succeeds.
- API Key Management: API keys are loaded in `main.py` and passed to the agent.
  The agent selects the appropriate key based on the `api_key_secret` in the config.
- Dynamic Dispatch: The `type` field in the config (e.g., "google") is used to
  dynamically call the corresponding analysis method (e.g., `_analyze_with_google`).

For detailed instructions on how to add or configure AI providers, please see:
/docs/AI_PROVIDER_INTEGRATION.md
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Any

import anthropic
import openai
try:
    from openai import RateLimitError as OpenAIRateLimitError
except Exception:
    class OpenAIRateLimitError(Exception):
        """Fallback when SDK does not expose RateLimitError."""
        pass

from src.analysis.usage_tracker import UsageTracker, usage_tracker as global_usage_tracker

logger = logging.getLogger(__name__)


@dataclass
class AIAnalysis:
    """Result of AI analysis on a market"""
    reasoning: str
    confidence_score: float  # 0.0 - 1.0
    estimated_probability: float  # 0.0 - 1.0
    recommendation: str  # "BUY_YES", "BUY_NO", "HOLD"
    market_id: str
    timestamp: datetime
    
    @property
    def edge(self) -> float:
        """Calculate edge vs market price"""
        return 0.0  # Will be calculated with market price


class AIAgent:
    """AI-powered decision engine for market analysis"""
    
    SYSTEM_PROMPT = """You are a probabilistic risk engine for prediction markets.
Your job is to analyze events and estimate the true probability of outcomes.

Instructions:
1. Ignore hype, social media sentiment, and personal biases
2. Focus on factual likelihood based on evidence
3. Consider: historical data, expert opinions, market_mechanics, timeline
4. Be conservative - prediction markets often overestimate certain outcomes.

Output ONLY valid JSON in this exact format:
{
    "reasoning": "brief explanation of your analysis (50-100 words)",
    "confidence_score": 0.0-1.0,
    "estimated_probability": 0.0-1.0,
    "recommendation": "BUY_YES" or "BUY_NO" or "HOLD"
}

Never output anything else except the JSON."""
    
    def __init__(self, config: Dict[str, Any], usage_tracker: UsageTracker = global_usage_tracker):
        self.config = config.get('ai', {})
        self.provider_chain = self.config.get('provider_chain', [])
        self.temperature = self.config.get('temperature', 0.1)
        self.max_tokens = self.config.get('max_tokens', 800)  # Was 1500 — JSON responses don't need that much
        self.timeout = self.config.get('timeout', 30)
        self.api_keys = {}
        self.usage_tracker = usage_tracker
        self.consensus_enabled = self.config.get('consensus_enabled', False)
        self.consensus_min_agree = self.config.get('consensus_min_agree', 2)

        # ── Token conservation: cache + rate limiting ──
        # Cache AI results per market_id for cache_ttl seconds to avoid
        # re-analyzing the same market every cycle.
        # 3-tuple: (timestamp, AIAnalysis, cached_yes_price)
        # Price-based invalidation: if yes_price moves >0.05 the cached result is stale.
        self._cache: Dict[str, tuple] = {}  # market_id -> (timestamp, AIAnalysis, price)
        self._cache_ttl = self.config.get('cache_ttl', 600)  # 10 min default
        self._last_call_time = 0.0
        self._min_call_gap = self.config.get('min_call_gap', 1.5)  # seconds between API calls
        self._rate_lock: Optional[asyncio.Lock] = None  # Created lazily inside event loop

        # Free-tier / multi-model rotation (OpenRouter, etc.)
        self._free_tier_daily_budget = self.config.get("free_tier_daily_request_budget") or 0
        self._provider_daily_counts: Dict[str, int] = {}
        self._quota_warned: Dict[str, set] = {}
        self._model_cooldown_until: Dict[str, float] = {}
        self.live_inferencing = self.config.get("live_inferencing", True)

    def refresh_from_config(self, ai_cfg: Dict[str, Any]) -> None:
        """
        Re-read AI settings after live config merge (dashboard save).
        Keeps self.config as the same dict reference when ai_cfg is config['ai'].
        """
        self.config = ai_cfg
        self.provider_chain = self.config.get("provider_chain", [])
        self.temperature = self.config.get("temperature", 0.1)
        self.max_tokens = self.config.get("max_tokens", 800)
        self.timeout = self.config.get("timeout", 30)
        self.consensus_enabled = self.config.get("consensus_enabled", False)
        self.consensus_min_agree = self.config.get("consensus_min_agree", 2)
        self._cache_ttl = self.config.get("cache_ttl", 600)
        self._min_call_gap = self.config.get("min_call_gap", 1.5)
        self._free_tier_daily_budget = self.config.get("free_tier_daily_request_budget") or 0
        self.live_inferencing = self.config.get("live_inferencing", True)

    def is_available(self) -> bool:
        """
        Return True when AI is both enabled in config AND has at least one
        provider configured.  All strategies call this before any AI call so
        that a single toggle (ai.enabled: false) or an empty provider_chain
        is enough to run the whole bot in pure-quant mode.
        """
        if not self.config.get("enabled", True):
            return False
        return bool(self.provider_chain)

    def set_api_keys(self, api_keys: Dict[str, str]):
        """Set all available API keys from a dictionary."""
        self.api_keys = api_keys

    def _normalize_recommendation(self, rec: str) -> str:
        """Map recommendation to BUY_YES | BUY_NO | HOLD."""
        if not rec:
            return "HOLD"
        rec = str(rec).strip().upper()
        if "BUY_YES" in rec or "YES" == rec:
            return "BUY_YES"
        if "BUY_NO" in rec or "NO" == rec or "SELL_YES" in rec:
            return "BUY_NO"
        return "HOLD"

    def _cache_key(self, market_id: str, strategy_hint: str = "") -> str:
        """Cache key includes strategy so the same market gets fresh analysis per strategy."""
        return f"{market_id}:{strategy_hint}" if strategy_hint else market_id

    def _get_cached(self, market_id: str, current_price: float = 0.0, strategy_hint: str = "") -> Optional[AIAnalysis]:
        """Return cached analysis if still fresh and price hasn't moved significantly.

        Invalidates cache if yes_price moved >0.05 (5 cents) since caching —
        a significant price move means market sentiment shifted and the old
        analysis is no longer valid.
        """
        key = self._cache_key(market_id, strategy_hint)
        if key in self._cache:
            cached_time, cached_result, cached_price = self._cache[key]
            age = time.time() - cached_time
            if age < self._cache_ttl:
                # Price-based invalidation: skip if price moved materially
                if current_price > 0 and cached_price > 0 and abs(current_price - cached_price) > 0.05:
                    logger.debug(
                        f"AI cache INVALIDATED {market_id}: "
                        f"price {cached_price:.3f}->{current_price:.3f} (delta>{0.05})"
                    )
                    del self._cache[key]
                    return None
                logger.debug(
                    f"AI cache HIT {market_id} (age={age:.0f}s, "
                    f"price={current_price:.3f}, ttl={self._cache_ttl}s)"
                )
                return cached_result
            else:
                del self._cache[key]
        return None

    def _set_cache(self, market_id: str, result: AIAnalysis, price: float = 0.0, strategy_hint: str = ""):
        """Store analysis result in cache with the yes_price at time of caching."""
        key = self._cache_key(market_id, strategy_hint)
        self._cache[key] = (time.time(), result, price)
        logger.debug(f"AI CACHE SET: {market_id} price={price:.3f} (size={len(self._cache)}, ttl={self._cache_ttl}s)")
        # Prune old entries if cache gets large
        if len(self._cache) > 200:
            cutoff = time.time() - self._cache_ttl
            self._cache = {k: v for k, v in self._cache.items() if v[0] > cutoff}

    async def _rate_limit(self):
        """Enforce minimum gap between API calls to avoid rate limits.

        Uses an asyncio.Lock created lazily on first call (must be inside an
        event loop).  The lock serialises concurrent coroutines so that two
        simultaneous callers can't both read _last_call_time as "elapsed ≥ gap"
        and both fire requests at the same instant.
        """
        if self._rate_lock is None:
            self._rate_lock = asyncio.Lock()
        async with self._rate_lock:
            now = time.time()
            elapsed = now - self._last_call_time
            if elapsed < self._min_call_gap:
                wait = self._min_call_gap - elapsed
                await asyncio.sleep(wait)
            self._last_call_time = time.time()

    @staticmethod
    def _models_for_provider(provider_config: Dict[str, Any], fallback_model: str) -> List[str]:
        raw = provider_config.get("models")
        if isinstance(raw, list) and raw:
            return [str(m) for m in raw]
        if provider_config.get("model"):
            return [str(provider_config["model"])]
        return [fallback_model] if fallback_model else []

    @staticmethod
    def _response_headers(response: Any) -> Optional[Dict[str, str]]:
        raw = getattr(response, "response", None) or getattr(response, "_response", None)
        if raw is None:
            return None
        h = getattr(raw, "headers", None)
        if h is None:
            return None
        try:
            return {str(k): str(v) for k, v in dict(h).items()}
        except Exception:
            return None

    def _daily_usage_key(self, provider_name: str) -> str:
        return f"{provider_name}:{date.today().isoformat()}"

    def _bump_local_quota_and_warn(self, provider_name: str) -> None:
        if not self._free_tier_daily_budget or self._free_tier_daily_budget <= 0:
            return
        k = self._daily_usage_key(provider_name)
        n = self._provider_daily_counts.get(k, 0) + 1
        self._provider_daily_counts[k] = n
        lim = self._free_tier_daily_budget
        frac = n / float(lim)
        wk = self._quota_warned.setdefault(k, set())
        if frac >= 0.5 and 50 not in wk:
            wk.add(50)
            logger.warning(
                f"AI free-tier budget ~50% used for '{provider_name}' "
                f"({n}/{lim} requests today, local counter)."
            )
        if frac >= 0.75 and 75 not in wk:
            wk.add(75)
            logger.warning(
                f"AI free-tier budget ~75% used for '{provider_name}' "
                f"({n}/{lim} requests today, local counter)."
            )

    def _maybe_warn_header_quota(self, provider_name: str, headers: Optional[Dict[str, str]]) -> None:
        if not headers:
            return
        low = {str(k).lower(): v for k, v in headers.items()}
        rem = low.get("x-ratelimit-remaining")
        lim = low.get("x-ratelimit-limit")
        if rem is None or lim is None:
            return
        try:
            r, l = int(float(rem)), int(float(lim))
        except (TypeError, ValueError):
            return
        if l <= 0:
            return
        frac_used = 1.0 - (r / float(l))
        k = f"{provider_name}:hdr:{date.today().isoformat()}"
        wk = self._quota_warned.setdefault(k, set())
        if frac_used >= 0.5 and 50 not in wk:
            wk.add(50)
            logger.warning(
                f"AI rate-limit ~50% consumed for '{provider_name}' "
                f"(remaining={r}/{l} per response headers)."
            )
        if frac_used >= 0.75 and 75 not in wk:
            wk.add(75)
            logger.warning(
                f"AI rate-limit ~75% consumed for '{provider_name}' "
                f"(remaining={r}/{l} per response headers)."
            )

    def _cooldown_key(self, provider_name: str, model: str) -> str:
        return f"{provider_name}:{model}"

    def _on_cooldown(self, key: str) -> bool:
        return time.time() < self._model_cooldown_until.get(key, 0.0)

    def _set_cooldown(self, key: str, seconds: float) -> None:
        self._model_cooldown_until[key] = time.time() + max(1.0, seconds)

    async def analyze_market(
        self,
        market_question: str,
        market_description: str,
        current_yes_price: float,
        market_id: str,
        news_context: str = "",
        strategy_hint: str = "",
    ) -> Optional[AIAnalysis]:
        """
        Analyze a market with caching and rate limiting.
        Returns cached result if available (within cache_ttl).
        Otherwise calls providers with rate limiting between calls.

        strategy_hint: optional short tag (e.g. "bitcoin", "sol_lag") so the same
        market_id gets independent cache entries when analyzed by different strategies.
        """
        # Live quota saver — backtests do not use AIAgent (see BacktestAIAgent).
        if not self.config.get("live_inferencing", True):
            logger.debug(
                "AI live_inferencing disabled — skipping provider call for %s",
                market_id,
            )
            return None

        # Check cache first — avoid burning tokens on repeat analysis
        cached = self._get_cached(market_id, current_yes_price, strategy_hint)
        if cached is not None:
            return cached

        user_prompt = self._build_prompt(
            market_question=market_question,
            market_description=market_description,
            current_yes_price=current_yes_price,
            news_context=news_context
        )

        if self.consensus_enabled:
            result = await self._analyze_consensus(user_prompt, market_id)
            if result:
                self._set_cache(market_id, result, current_yes_price, strategy_hint)
            return result

        # Skip entirely if no providers configured
        if not self.provider_chain:
            logger.debug(f"No AI providers configured — skipping market {market_id}.")
            return None

        # Rate limit before making the API call
        await self._rate_limit()

        for provider_config in self.provider_chain:
            provider_name = provider_config.get("name")
            provider_type = provider_config.get("type")
            model = provider_config.get("model")
            api_key_name = provider_config.get("api_key_secret")
            api_key = self.api_keys.get(api_key_name)

            if not api_key:
                if provider_config.get("local", False):
                    api_key = "local"
                else:
                    logger.warning(f"API key '{api_key_name}' not found for provider '{provider_name}'. Skipping.")
                    continue

            logger.info(f"Attempting analysis with provider: {provider_name} (Model: {model})")

            try:
                analysis_function = getattr(self, f"_analyze_with_{provider_type}", None)
                if not analysis_function:
                    logger.error(f"Unknown provider type '{provider_type}' in provider chain. Skipping.")
                    continue

                analysis_result = await analysis_function(
                    user_prompt, market_id, model, api_key, provider_config
                )

                if analysis_result:
                    logger.info(
                        f"AI analysis OK: {provider_name} -> {market_id} "
                        f"rec={analysis_result.recommendation} conf={analysis_result.confidence_score:.2f}"
                    )
                    self._set_cache(market_id, analysis_result, current_yes_price, strategy_hint)
                    return analysis_result

            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Provider '{provider_name}' failed: {e}. Falling back to next provider.")
                continue

        if self.provider_chain:
            logger.error(f"All AI providers failed for market {market_id}. No analysis could be performed.")
        else:
            logger.debug(f"No AI providers configured — skipping AI analysis for market {market_id}.")
        return None

    async def _analyze_consensus(self, user_prompt: str, market_id: str) -> Optional[AIAnalysis]:
        """Call all providers with keys in parallel; return only if >= consensus_min_agree agree on same action."""
        providers_with_keys = []
        for pc in self.provider_chain:
            api_key = self.api_keys.get(pc.get("api_key_secret"))
            if api_key and getattr(self, f"_analyze_with_{pc.get('type')}", None):
                providers_with_keys.append(pc)

        if len(providers_with_keys) < self.consensus_min_agree:
            logger.warning(
                "Consensus enabled but only %d provider(s) have keys (need %d). Falling back to chain.",
                len(providers_with_keys), self.consensus_min_agree
            )
            return await self._analyze_chain_fallback(user_prompt, market_id)

        async def call_one(pc: Dict) -> Optional[AIAnalysis]:
            try:
                fn = getattr(self, f"_analyze_with_{pc['type']}", None)
                if not fn:
                    return None
                model_candidates = self._models_for_provider(pc, str(pc.get("model", "")))
                model_for_call = model_candidates[0] if model_candidates else ""
                return await asyncio.wait_for(
                    fn(
                        user_prompt,
                        market_id,
                        model_for_call,
                        self.api_keys[pc["api_key_secret"]],
                        pc,
                    ),
                    timeout=self.timeout,
                )
            except Exception:
                return None

        results = await asyncio.gather(*[call_one(pc) for pc in providers_with_keys])
        analyses = [a for a in results if a is not None]
        if not analyses:
            logger.warning("Consensus: all providers failed.")
            return None

        rec_counts: Dict[str, List[AIAnalysis]] = {}
        for a in analyses:
            rec = self._normalize_recommendation(a.recommendation)
            rec_counts.setdefault(rec, []).append(a)

        for rec in ("BUY_YES", "BUY_NO"):
            agree = rec_counts.get(rec, [])
            if len(agree) >= self.consensus_min_agree:
                probs = [x.estimated_probability for x in agree]
                confs = [x.confidence_score for x in agree]
                reasoning = " | ".join(x.reasoning[:80] for x in agree[:2])
                return AIAnalysis(
                    reasoning=f"[Consensus {len(agree)}/{len(analyses)}] {reasoning}",
                    confidence_score=min(confs),
                    estimated_probability=float(sum(probs) / len(probs)),
                    recommendation=rec,
                    market_id=market_id,
                    timestamp=datetime.now(),
                )
        logger.info("Consensus: no recommendation reached min agreement (min=%d).", self.consensus_min_agree)
        return None

    async def _analyze_chain_fallback(self, user_prompt: str, market_id: str) -> Optional[AIAnalysis]:
        """Original chain fallback (first successful provider)."""
        for provider_config in self.provider_chain:
            api_key = self.api_keys.get(provider_config.get("api_key_secret"))
            if not api_key:
                continue
            provider_type = provider_config.get("type")
            model = provider_config.get("model")
            fn = getattr(self, f"_analyze_with_{provider_type}", None)
            if not fn:
                continue
            try:
                result = await fn(user_prompt, market_id, model, api_key, provider_config)
                if result:
                    return result
            except Exception:
                continue
        return None
    
    def _build_prompt(
        self,
        market_question: str,
        market_description: str,
        current_yes_price: float,
        news_context: str
    ) -> str:
        """Build the user prompt for the AI"""
        
        prompt = f"""Analyze this prediction market:

MARKET QUESTION: {market_question}

MARKET DESCRIPTION: {market_description}

CURRENT MARKET PRICE: YES = ${current_yes_price:.2f} (implies {current_yes_price*100:.1f}% probability)

"""
        
        if news_context:
            prompt += f"RECENT NEWS CONTEXT:\n{news_context}\n"
        
        prompt += """
Based on your analysis, estimate the TRUE probability and recommend a trade.
Remember: The market price may be wrong due to sentiment, bias, or incomplete information."""
        
        return prompt
    
    async def _analyze_with_openai(
        self,
        prompt: str,
        market_id: str,
        model: str,
        api_key: str,
        provider_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[AIAnalysis]:
        """OpenAI-compatible API (OpenAI, OpenRouter, and other base_url hosts)."""
        pc = provider_config or {}
        provider_label = pc.get("name", "openai")
        base_url = pc.get("base_url")
        json_mode = pc.get("json_mode", True)
        cooldown_429 = float(pc.get("cooldown_on_429_seconds", 90.0))

        extra_headers: Dict[str, str] = {}
        if pc.get("http_referer"):
            extra_headers["HTTP-Referer"] = str(pc["http_referer"])
        if pc.get("x_title"):
            extra_headers["X-Title"] = str(pc["x_title"])
        dh = pc.get("default_headers")
        if isinstance(dh, dict):
            extra_headers.update({str(k): str(v) for k, v in dh.items()})

        models = self._models_for_provider(pc, model)
        if not models:
            logger.warning("OpenAI-compatible provider has no model configured — skipping.")
            return None

        client_kw: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kw["base_url"] = base_url
        if extra_headers:
            client_kw["default_headers"] = extra_headers

        client = openai.AsyncOpenAI(**client_kw)

        last_err: Optional[BaseException] = None
        for m in models:
            ck = self._cooldown_key(provider_label, m)
            if self._on_cooldown(ck):
                logger.debug(f"OpenAI-compat: model {m} on cooldown — trying next.")
                continue
            # Local counter tracks request attempts; header-based warnings remain source-of-truth when present.
            self._bump_local_quota_and_warn(provider_label)
            start_time = time.time()
            try:
                kwargs: Dict[str, Any] = dict(
                    model=m,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}

                response = await asyncio.wait_for(
                    client.chat.completions.create(**kwargs),
                    timeout=self.timeout,
                )

                content = response.choices[0].message.content
                usage = response.usage
                latency = time.time() - start_time
                hdrs = self._response_headers(response)

                if usage:
                    self.usage_tracker.record_usage(
                        provider="openrouter" if base_url and "openrouter" in str(base_url).lower() else "openai",
                        model=m,
                        input_tokens=usage.prompt_tokens,
                        output_tokens=usage.completion_tokens,
                        latency=latency,
                    )

                self._maybe_warn_header_quota(provider_label, hdrs)

                parsed = self._parse_response(content or "", market_id)
                if parsed is None:
                    logger.warning(f"OpenAI-compat: invalid JSON from model {m} — trying next.")
                    last_err = ValueError("parse_failed")
                    continue

                return parsed

            except asyncio.TimeoutError:
                last_err = asyncio.TimeoutError()
                logger.warning(f"OpenAI-compat: timeout model={m} — trying next.")
                continue
            except OpenAIRateLimitError as e:
                last_err = e
                self._set_cooldown(ck, cooldown_429)
                logger.warning(f"OpenAI-compat: rate limit model={m} — cooldown {cooldown_429:.0f}s, trying next.")
                continue
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if "429" in msg or "rate" in msg:
                    self._set_cooldown(ck, cooldown_429)
                logger.debug(f"OpenAI-compat error model={m}: {e} — trying next.")
                continue

        if last_err:
            raise last_err
        return None

    async def _analyze_with_google(
        self,
        prompt: str,
        market_id: str,
        model: str,
        api_key: str,
        provider_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[AIAnalysis]:
        """Analyzes market data using Google Gemini API (GOOGLE_API_KEY from AI Studio)."""
        start_time = time.time()
        try:
            from google import genai

            if not api_key:
                raise ValueError("GOOGLE_API_KEY required for Gemini API. Get one at aistudio.google.com/app/apikey")

            client = genai.Client(api_key=api_key)
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=self.SYSTEM_PROMPT,
                        temperature=self.temperature,
                        max_output_tokens=self.max_tokens,
                        response_mime_type="application/json",
                    ),
                ),
                timeout=self.timeout
            )

            content = response.text if hasattr(response, "text") else str(response)
            latency = time.time() - start_time

            input_tokens = getattr(getattr(response, "usage_metadata", None), "prompt_token_count", 0) or 0
            output_tokens = getattr(getattr(response, "usage_metadata", None), "candidates_token_count", 0) or 0

            self.usage_tracker.record_usage(
                provider='google',
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency=latency
            )

            logger.debug(f"Received analysis from Gemini: {content[:100]}...")
            return self._parse_response(content, market_id)

        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"Gemini API error details: {e}")
            raise e

    async def _analyze_with_groq(
        self,
        prompt: str,
        market_id: str,
        model: str,
        api_key: str,
        provider_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[AIAnalysis]:
        """Analyzes using Groq API (free tier, no credit card). Get key at console.groq.com"""
        start_time = time.time()
        try:
            from groq import AsyncGroq

            if not api_key:
                raise ValueError("GROQ_API_KEY required. Get free key at console.groq.com/keys")

            client = AsyncGroq(api_key=api_key)
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                ),
                timeout=self.timeout
            )

            content = response.choices[0].message.content
            if not content:
                raise ValueError("Empty response from Groq")
            latency = time.time() - start_time

            usage = getattr(response, "usage", None)
            input_tokens = usage.prompt_tokens if usage else 0
            output_tokens = usage.completion_tokens if usage else 0

            self.usage_tracker.record_usage(
                provider='groq',
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency=latency
            )

            logger.debug(f"Received analysis from Groq: {content[:100]}...")
            return self._parse_response(content, market_id)

        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"Groq API error details: {e}")
            raise e

    async def _analyze_with_anthropic(
        self,
        prompt: str,
        market_id: str,
        model: str,
        api_key: str,
        provider_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[AIAnalysis]:
        """Analyze using Anthropic API"""
        start_time = time.time()
        try:
            client = anthropic.AsyncAnthropic(api_key=api_key)

            response = await asyncio.wait_for(
                client.messages.create(
                    model=model,
                    system=self.SYSTEM_PROMPT,
                    messages=[
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                ),
                timeout=self.timeout
            )

            content = self._extract_text_from_content(response.content)
            usage = response.usage
            latency = time.time() - start_time

            if usage:
                self.usage_tracker.record_usage(
                    provider='anthropic',
                    model=model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    latency=latency
                )

            return self._parse_response(content, market_id)
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"Anthropic API error details: {e}")
            raise e

    async def _analyze_with_minimax(
        self,
        prompt: str,
        market_id: str,
        model: str,
        api_key: str,
        provider_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[AIAnalysis]:
        """Analyzes market data using the MiniMax Anthropic-compatible API (Coding Plan).

        Retries once on 529 OverloadedError (transient server overload) before raising,
        so a momentary spike doesn't cascade to broken fallback providers.
        """
        max_attempts = 2
        for attempt in range(max_attempts):
            start_time = time.time()
            try:
                # MiniMax Coding Plan uses Anthropic-compatible endpoint with sk-cp- keys.
                client = anthropic.AsyncAnthropic(
                    api_key=api_key,
                    base_url="https://api.minimax.io/anthropic"
                )

                response = await asyncio.wait_for(
                    client.messages.create(
                        model=model,
                        max_tokens=self.max_tokens,
                        system=self.SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=self.temperature,
                    ),
                    timeout=self.timeout
                )

                content = self._extract_text_from_content(response.content)
                usage = response.usage
                latency = time.time() - start_time

                # Record usage, attributing cost to minimax
                if usage:
                    self.usage_tracker.record_usage(
                        provider='minimax',
                        model=model,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        latency=latency
                    )

                logger.debug(f"Received analysis from Minimax: {content}")
                return self._parse_response(content, market_id)

            except asyncio.TimeoutError:
                logger.warning(f"Minimax API timed out after {self.timeout}s for market {market_id}")
                raise
            except Exception as e:
                err_msg = str(e)
                status_code = getattr(e, "status_code", None)
                is_overloaded = (
                    status_code == 529
                    or "529" in err_msg
                    or "overloaded" in err_msg.lower()
                )
                if is_overloaded and attempt < max_attempts - 1:
                    logger.warning(
                        f"MiniMax overloaded (529) — retrying in 5s for market {market_id} "
                        f"(attempt {attempt + 1}/{max_attempts})"
                    )
                    await asyncio.sleep(5)
                    continue
                logger.warning(f"Minimax API error: {type(e).__name__}: {e}")
                raise
    
    def _extract_text_from_content(self, content_blocks) -> str:
        """Extract text from Anthropic-style content blocks (handles ThinkingBlock + TextBlock)."""
        for block in content_blocks:
            # Skip ThinkingBlocks (MiniMax M2.5 returns these)
            block_type = getattr(block, "type", "")
            if block_type == "thinking":
                continue
            if hasattr(block, "text") and block.text:
                return block.text
        # Fallback: return any block that has text
        for block in content_blocks:
            if hasattr(block, "text") and block.text:
                return block.text
        return ""

    def _parse_response(self, content: str, market_id: str) -> Optional[AIAnalysis]:
        """Parse AI response into AIAnalysis object"""
        try:
            # Strip markdown code blocks if present
            cleaned = content.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                # Remove first and last lines (```json and ```)
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines).strip()
            # Extract JSON from mixed text (find first { to last })
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1:
                cleaned = cleaned[start:end + 1]
            data = json.loads(cleaned)
            
            return AIAnalysis(
                reasoning=data.get('reasoning', ''),
                confidence_score=float(data.get('confidence_score', 0.0)),
                estimated_probability=float(data.get('estimated_probability', 0.0)),
                recommendation=data.get('recommendation', 'HOLD'),
                market_id=market_id,
                timestamp=datetime.now()
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response: {e}")
            logger.debug(f"Raw response: {content}")
            return None
        except Exception as e:
            logger.error(f"Error parsing AI response: {e}")
            return None
    
    async def batch_analyze(
        self,
        markets: List[Dict[str, Any]],
        news_contexts: Dict[str, str] = None,
    ) -> List[AIAnalysis]:
        """Analyze multiple markets in parallel"""
        tasks = []
        
        for market in markets:
            news = ""
            if news_contexts and market['id'] in news_contexts:
                news = news_contexts[market['id']]
            
            task = self.analyze_market(
                market_question=market['question'],
                market_description=market.get('description', ''),
                current_yes_price=market['yes_price'],
                market_id=market['id'],
                news_context=news
            )
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        analyses = []
        for result in results:
            if isinstance(result, AIAnalysis):
                analyses.append(result)
            elif isinstance(result, Exception):
                logger.error(f"Analysis failed: {result}")
        
        return analyses
    
    def calculate_edge(self, analysis: AIAnalysis, market_price: float) -> float:
        """Calculate the edge between AI estimate and market price"""
        return analysis.estimated_probability - market_price
    
    def should_auto_trade(
        self,
        analysis: AIAnalysis,
        market_price: float,
        min_edge: float,
        ai_confidence_threshold: float
    ) -> bool:
        """Determine if a trade should be auto-executed"""
        # Check AI confidence
        if analysis.confidence_score < ai_confidence_threshold:
            return False
        
        # Calculate edge
        edge = self.calculate_edge(analysis, market_price)
        
        # Check if edge is sufficient
        if abs(edge) < min_edge:
            return False
        
        return True
