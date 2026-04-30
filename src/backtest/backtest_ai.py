"""
Deterministic AI proxy for backtesting.

This proxy is intentionally generic. It avoids real LLM calls and applies a
small mean-reversion nudge around 0.50 so backtests can exercise AI-gated code
paths without depending on removed fade/arbitrage strategy config.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.analysis.ai_agent import AIAnalysis


class BacktestAIAgent:
    """
    Rule-based AI proxy for backtesting.
    - Small mean-reversion bias away from extreme YES prices
    - No dependency on legacy fade / arbitrage config
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
        news_context: str = "",
        strategy_hint: str = "",
        end_date: Optional[datetime] = None,
        **kwargs,
    ) -> Optional[AIAnalysis]:
        """
        Return deterministic analysis for backtest.
        This is a lightweight proxy only. It does not attempt to emulate live AI.
        """
        _ = (market_question, market_description, market_id, news_context, strategy_hint, end_date, kwargs)
        proxy_cfg = self.config.get("backtest", {}).get("ai_proxy", {})
        center_band = float(proxy_cfg.get("center_band", 0.04))
        reversion_strength = float(proxy_cfg.get("reversion_strength", 0.35))

        if current_yes_price >= 0.5 + center_band:
            estimated_prob = 0.5 + (0.5 - current_yes_price) * reversion_strength
            recommendation = "BUY_NO"
        elif current_yes_price <= 0.5 - center_band:
            estimated_prob = 0.5 + (0.5 - current_yes_price) * reversion_strength
            recommendation = "BUY_YES"
        else:
            return None

        estimated_prob = max(0.01, min(0.99, estimated_prob))
        confidence = 0.72

        return AIAnalysis(
            reasoning="[BACKTEST PROXY] Rule-based mean reversion proxy",
            confidence_score=confidence,
            estimated_probability=estimated_prob,
            recommendation=recommendation,
            market_id=market_id,
            timestamp=datetime.now(),
        )
