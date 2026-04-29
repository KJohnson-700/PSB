"""Parallel AI ensemble for borderline weather markets."""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.analysis.weather_ensemble_models import (
    WeatherAgentResult,
    WeatherEnsembleDecision,
)

if TYPE_CHECKING:
    from src.analysis.ai_agent import AIAgent


class WeatherEnsembleRunner:
    """Run a 3-role AI ensemble and blend the result toward market on disagreement."""

    _ROLE_WEIGHTS = {
        "forecaster": 0.40,
        "bull": 0.30,
        "bear": 0.30,
    }

    def __init__(self, ai_agent: Optional[AIAgent], config: Dict[str, Any]):
        self.ai_agent = ai_agent
        weather_cfg = ((config.get("strategies") or {}).get("weather") or {})
        self.timeout = float(weather_cfg.get("weather_ai_timeout_sec", 25))
        self.cache_ttl = float(weather_cfg.get("weather_ai_cache_ttl_sec", 600))
        self.disagreement_threshold = float(
            weather_cfg.get("ensemble_disagreement_threshold", 0.12)
        )
        self.min_confidence = float(weather_cfg.get("ensemble_min_confidence", 0.55))
        self._cache: Dict[str, tuple] = {}

    async def run(
        self,
        *,
        market_id: str,
        question: str,
        description: str,
        subtype: str,
        city_key: str,
        icao: str,
        horizon_days: int,
        horizon_hours: Optional[float],
        forecast_prob: float,
        raw_forecast_prob: float,
        market_price: float,
        calibration_bias: float,
        calibration_count: int,
        forecast_temp_f: Optional[float] = None,
        metar_obs_f: Optional[float] = None,
        ensemble_std_f: Optional[float] = None,
    ) -> Optional[WeatherEnsembleDecision]:
        """Return an AI probability estimate for a borderline weather market."""
        if not self.ai_agent or not self.ai_agent.is_available():
            return None

        cached = self._get_cached(market_id, market_price)
        if cached is not None:
            return cached

        started = time.time()
        tasks = [
            asyncio.create_task(
                self._call_role(
                role=role,
                question=question,
                description=description,
                subtype=subtype,
                city_key=city_key,
                icao=icao,
                horizon_days=horizon_days,
                horizon_hours=horizon_hours,
                forecast_prob=forecast_prob,
                raw_forecast_prob=raw_forecast_prob,
                market_price=market_price,
                calibration_bias=calibration_bias,
                calibration_count=calibration_count,
                forecast_temp_f=forecast_temp_f,
                metar_obs_f=metar_obs_f,
                ensemble_std_f=ensemble_std_f,
                market_id=market_id,
            )
            )
            for role in self._ROLE_WEIGHTS
        ]

        done, pending = await asyncio.wait(tasks, timeout=self.timeout)
        for task in pending:
            task.cancel()

        results: List[WeatherAgentResult] = []
        for task in done:
            try:
                result = task.result()
            except Exception:
                result = None
            if result is not None:
                results.append(result)

        if not results:
            return None

        elapsed = time.time() - started
        decision = self._aggregate(results, market_price, elapsed)
        self._set_cache(market_id, market_price, decision)
        return decision

    def _cache_key(self, market_id: str) -> str:
        return f"weather_ensemble:{market_id}"

    def _get_cached(
        self, market_id: str, market_price: float
    ) -> Optional[WeatherEnsembleDecision]:
        key = self._cache_key(market_id)
        cached = self._cache.get(key)
        if not cached:
            return None
        ts, cached_price, decision = cached
        if time.time() - ts > self.cache_ttl:
            self._cache.pop(key, None)
            return None
        if abs(float(cached_price) - float(market_price)) > 0.05:
            self._cache.pop(key, None)
            return None
        return decision

    def _set_cache(
        self, market_id: str, market_price: float, decision: WeatherEnsembleDecision
    ) -> None:
        self._cache[self._cache_key(market_id)] = (time.time(), market_price, decision)

    async def _call_role(
        self,
        *,
        role: str,
        question: str,
        description: str,
        subtype: str,
        city_key: str,
        icao: str,
        horizon_days: int,
        horizon_hours: Optional[float],
        forecast_prob: float,
        raw_forecast_prob: float,
        market_price: float,
        calibration_bias: float,
        calibration_count: int,
        forecast_temp_f: Optional[float],
        metar_obs_f: Optional[float],
        ensemble_std_f: Optional[float],
        market_id: str,
    ) -> Optional[WeatherAgentResult]:
        if not self.ai_agent:
            return None

        role_prompt = self._build_role_prompt(
            role=role,
            question=question,
            description=description,
            subtype=subtype,
            city_key=city_key,
            icao=icao,
            horizon_days=horizon_days,
            horizon_hours=horizon_hours,
            forecast_prob=forecast_prob,
            raw_forecast_prob=raw_forecast_prob,
            market_price=market_price,
            calibration_bias=calibration_bias,
            calibration_count=calibration_count,
            forecast_temp_f=forecast_temp_f,
            metar_obs_f=metar_obs_f,
            ensemble_std_f=ensemble_std_f,
        )
        analysis = await self.ai_agent.analyze_market(
            market_question=question,
            market_description=role_prompt,
            current_yes_price=market_price,
            market_id=market_id,
            strategy_hint=f"weather:{role}",
        )
        if analysis is None:
            return None
        return WeatherAgentResult(
            role=role,
            reasoning=analysis.reasoning,
            confidence_score=float(analysis.confidence_score),
            estimated_probability=float(analysis.estimated_probability),
            recommendation=str(analysis.recommendation),
        )

    def _aggregate(
        self,
        results: List[WeatherAgentResult],
        market_price: float,
        elapsed_seconds: float,
    ) -> WeatherEnsembleDecision:
        active_weights = {
            result.role: self._ROLE_WEIGHTS.get(result.role, 0.0) for result in results
        }
        weight_sum = sum(active_weights.values()) or 1.0
        normalized_weights = {
            role: weight / weight_sum for role, weight in active_weights.items()
        }

        weighted_prob = sum(
            normalized_weights.get(result.role, 0.0) * result.estimated_probability
            for result in results
        )
        probs = [result.estimated_probability for result in results]
        disagreement_std = math.sqrt(
            sum((prob - weighted_prob) ** 2 for prob in probs) / len(probs)
        )

        disagreement_penalty = max(0.0, (disagreement_std - 0.08) / 0.10)
        confidence = max(0.1, 1.0 - disagreement_penalty)
        final_prob = (
            weighted_prob * confidence + market_price * (1.0 - confidence)
        )
        final_prob = min(0.99, max(0.01, final_prob))

        if disagreement_std > self.disagreement_threshold or confidence < self.min_confidence:
            recommendation = "HOLD"
        elif final_prob > market_price + 0.02:
            recommendation = "BUY_YES"
        elif final_prob < market_price - 0.02:
            recommendation = "BUY_NO"
        else:
            recommendation = "HOLD"

        reasoning = "; ".join(
            result.reasoning.strip()
            for result in results[:2]
            if result.reasoning.strip()
        )
        if not reasoning:
            reasoning = "Weather ensemble updated the marginal probability estimate."

        return WeatherEnsembleDecision(
            reasoning=reasoning,
            confidence_score=confidence,
            estimated_probability=final_prob,
            recommendation=recommendation,
            agent_results={result.role: result for result in results},
            weighted_probability=weighted_prob,
            disagreement_std=disagreement_std,
            agent_count=len(results),
            elapsed_seconds=elapsed_seconds,
        )

    @staticmethod
    def _build_role_prompt(
        *,
        role: str,
        question: str,
        description: str,
        subtype: str,
        city_key: str,
        icao: str,
        horizon_days: int,
        horizon_hours: Optional[float],
        forecast_prob: float,
        raw_forecast_prob: float,
        market_price: float,
        calibration_bias: float,
        calibration_count: int,
        forecast_temp_f: Optional[float],
        metar_obs_f: Optional[float],
        ensemble_std_f: Optional[float],
    ) -> str:
        horizon_hours_text = (
            f"{horizon_hours:.1f}" if horizon_hours is not None else "unknown"
        )
        shared = (
            f"{description}\n\n"
            f"Weather market: {question}\n"
            f"Subtype: {subtype}\n"
            f"Location: {city_key} ({icao})\n"
            f"Horizon: T+{horizon_days} ({horizon_hours_text} hours)\n"
            f"Market YES price: {market_price:.3f}\n"
            f"Calibrated quant YES probability: {forecast_prob:.3f}\n"
            f"Raw quant YES probability: {raw_forecast_prob:.3f}\n"
            f"Calibration bias: {calibration_bias:+.3f} ({calibration_count} samples)\n"
            f"Forecast max temp: "
            f"{f'{forecast_temp_f:.1f}F' if forecast_temp_f is not None else 'n/a'}\n"
            f"Current METAR temp: "
            f"{f'{metar_obs_f:.1f}F' if metar_obs_f is not None else 'n/a'}\n"
            f"Forecast uncertainty std: "
            f"{f'{ensemble_std_f:.1f}F' if ensemble_std_f is not None else 'n/a'}\n"
        )
        if role == "forecaster":
            return (
                shared
                + "\nRole: Forecaster.\n"
                + "Estimate the true YES probability from the weather evidence. "
                + "Weight the numerical forecast most heavily, then adjust for "
                + "uncertainty, station observations, and horizon."
            )
        if role == "bull":
            return (
                shared
                + "\nRole: Bull researcher.\n"
                + "Argue why YES may be more likely than the market price implies. "
                + "Focus on specific supportive weather factors and how strongly they matter."
            )
        return (
            shared
            + "\nRole: Bear researcher.\n"
            + "Argue why NO may be more likely than the market price implies. "
            + "Focus on forecast uncertainty, reversion risk, and reasons the YES side may be overstated."
        )
