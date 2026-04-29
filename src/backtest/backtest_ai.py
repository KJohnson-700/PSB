"""
Deterministic AI proxy for backtesting.
Avoids real LLM calls; uses rule-based estimates (mean reversion, fade logic).

DIFFERENCES FROM LIVE AIAgent:
- Live AIAgent calls real LLMs (OpenAI, Anthropic, Gemini, Groq, MiniMax) with
  market context, news, and technical indicators. It returns variable confidence
  scores and nuanced probability estimates.
- BacktestAIAgent uses fixed rules:
  * Fade zone [consensus_threshold_lower, consensus_threshold_upper]:
    YES consensus -> estimated_prob = 1.0 - yes_price (fade the crowd)
    NO consensus  -> estimated_prob = yes_price + (1-yes_price)*0.5
  * Arbitrage zone (yes_price > 0.60 or < 0.40):
    Mean reversion toward 0.5: estimated_prob = 0.5 + (0.5 - yes_price) * 0.3
  * Fixed confidence: 0.72 for all signals
  * Returns None for prices in [0.40, 0.60] (no edge)

This proxy tests the RULE-BASED entry/exit logic only, not AI judgment.
To test AI quality, run live paper trading and compare against backtest results.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.analysis.ai_agent import AIAnalysis


class BacktestAIAgent:
    """
    Rule-based AI proxy for backtesting.
    - Fade: assumes extreme consensus tends to mean-revert (AI prob = 1 - consensus)
    - Arbitrage: assumes mean reversion toward 0.5
    """

    def __init__(self, config: dict):
        self.config = config
        self.proxy_disclaimer = (
            "BACKTEST PROXY: deterministic rule-based AI; metrics do not validate live LLM behavior."
        )

    def is_available(self) -> bool:
        return True

    async def analyze_market(
        self,
        market_question: str,
        market_description: str,
        current_yes_price: float,
        market_id: str,
        end_date: Optional[datetime] = None,
        news_context: str = "",
    ) -> Optional[AIAnalysis]:
        """
        Return deterministic analysis for backtest.
        Fade logic: when yes_price > 0.95, assume true prob = 1 - yes_price (fade)
        Arbitrage: when |yes_price - 0.5| > 0.15, assume mean reversion
        """
        # Fade zone: consensus in [0.80, 0.95] — aligned with FadeStrategy thresholds
        fade_cfg = self.config.get("strategies", {}).get("fade", {})
        fade_lower = fade_cfg.get("consensus_threshold_lower", 0.80)
        fade_upper = fade_cfg.get("consensus_threshold_upper", 0.95)

        if fade_lower <= current_yes_price <= fade_upper:
            # YES side has consensus — fade it: true prob is much lower
            estimated_prob = 1.0 - current_yes_price
            recommendation = "BUY_NO"
        elif fade_lower <= (1.0 - current_yes_price) <= fade_upper:
            # NO side has consensus — fade it: YES is undervalued
            estimated_prob = current_yes_price + (1.0 - current_yes_price) * 0.5
            recommendation = "BUY_YES"
        # Arbitrage zone: moderate mispricing (mean-reversion)
        elif current_yes_price > 0.60:
            estimated_prob = 0.5 + (0.5 - current_yes_price) * 0.3
            recommendation = "BUY_NO"
        elif current_yes_price < 0.40:
            estimated_prob = 0.5 + (0.5 - current_yes_price) * 0.3
            recommendation = "BUY_YES"
        else:
            return None

        estimated_prob = max(0.01, min(0.99, estimated_prob))
        confidence = 0.72

        return AIAnalysis(
            reasoning="[BACKTEST PROXY] Rule-based mean reversion / fade proxy",
            confidence_score=confidence,
            estimated_probability=estimated_prob,
            recommendation=recommendation,
            market_id=market_id,
            timestamp=datetime.now(),
        )
