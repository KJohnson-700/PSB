from __future__ import annotations

from typing import Optional

from src.analysis.ai_agent import AIAnalysis


class NullAIAgent:
    """Explicit no-op AI shim for strategies that should never call live or proxy AI."""

    def is_available(self) -> bool:
        return False

    def research_narrative_enabled(self) -> bool:
        return False

    def research_narrative_max_calls_per_scan(self) -> int:
        return 0

    def research_narrative_min_confidence(self) -> float:
        return 1.0

    def shadow_pipeline_enabled(self) -> bool:
        return False

    def shadow_pipeline_max_calls_per_scan(self) -> int:
        return 0

    def shadow_pipeline_min_confidence(self) -> float:
        return 1.0

    async def analyze_market(
        self,
        market_question: str,
        market_description: str,
        current_yes_price: float,
        market_id: str,
        news_context: str = "",
        strategy_hint: str = "",
        **kwargs,
    ) -> Optional[AIAnalysis]:
        _ = (
            market_question,
            market_description,
            current_yes_price,
            market_id,
            news_context,
            strategy_hint,
            kwargs,
        )
        return None
