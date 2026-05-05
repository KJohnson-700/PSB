"""Typed schemas for optional multi-stage AI artifacts."""

from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class PortfolioRating(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    CONFIDENT_BULLISH = "CONFIDENT_BULLISH"
    CONFIDENT_BEARISH = "CONFIDENT_BEARISH"


class TraderAction(str, Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    HOLD = "HOLD"
    SKIP = "SKIP"


class AIRecommendation(str, Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    HOLD = "HOLD"


class ResearchPlan(BaseModel):
    market: str = Field(..., min_length=1)
    recommendation: PortfolioRating
    rationale: str = Field(..., min_length=1)
    strategic_actions: list[str] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TraderProposal(BaseModel):
    action: TraderAction
    reasoning: str
    entry_price: float = Field(..., ge=0.0, le=1.0)
    stop_loss: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    position_sizing: float = Field(..., ge=0.0, le=1.0)
    max_loss_acceptable: Optional[float] = Field(default=None, ge=0.0)


class PortfolioDecision(BaseModel):
    rating: PortfolioRating
    action: TraderAction
    executive_summary: str
    investment_horizon: str
    position_size: float = Field(..., ge=0.0)
    entry_price: float = Field(..., ge=0.0, le=1.0)
    stop_loss: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MarginalAIAnalysis(BaseModel):
    """Schema mirror for the existing AIAgent output contract."""

    reasoning: str = Field(..., min_length=1)
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    estimated_probability: float = Field(..., ge=0.0, le=1.0)
    recommendation: AIRecommendation


def map_recommendation_to_portfolio_rating(
    recommendation: AIRecommendation,
    confidence: float,
) -> PortfolioRating:
    """Map BUY_YES/BUY_NO/HOLD + confidence into rating buckets."""
    conf = max(0.0, min(1.0, float(confidence)))
    if recommendation == AIRecommendation.HOLD:
        return PortfolioRating.NEUTRAL
    if recommendation == AIRecommendation.BUY_YES:
        return (
            PortfolioRating.CONFIDENT_BULLISH
            if conf >= 0.75
            else PortfolioRating.BULLISH
        )
    return (
        PortfolioRating.CONFIDENT_BEARISH
        if conf >= 0.75
        else PortfolioRating.BEARISH
    )

