"""Tests for structured AI schemas and markdown rendering."""

from src.ai.render import render_marginal_analysis, render_research_plan
from src.ai.schema import MarginalAIAnalysis, PortfolioRating, ResearchPlan


def test_render_research_plan_non_empty() -> None:
    plan = ResearchPlan(
        market="Will ETH close above 3k?",
        recommendation=PortfolioRating.BULLISH,
        rationale="Momentum and context suggest upside continuation.",
        strategic_actions=["Watch spread and only enter if edge remains positive."],
        confidence=0.71,
        metadata={"source": "unit-test"},
    )
    markdown = render_research_plan(plan)
    assert markdown.strip()
    assert "## Research Plan" in markdown
    assert "BULLISH" in markdown


def test_render_marginal_analysis_non_empty() -> None:
    analysis = MarginalAIAnalysis(
        reasoning="Short-window momentum aligns with HTF trend.",
        confidence_score=0.68,
        estimated_probability=0.59,
        recommendation="BUY_YES",
    )
    markdown = render_marginal_analysis(
        analysis,
        market_id="m-123",
        strategy_hint="eth_macro",
    )
    assert markdown.strip()
    assert "## Marginal Analysis" in markdown
    assert "BUY_YES" in markdown

