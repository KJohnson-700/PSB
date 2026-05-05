"""Markdown render helpers for structured AI artifacts."""

from __future__ import annotations

import json
from typing import Dict, Optional, Union

from src.ai.schema import (
    MarginalAIAnalysis,
    PortfolioDecision,
    ResearchPlan,
    TraderProposal,
)


def _pct(value: float) -> str:
    return f"{float(value) * 100:.1f}%"


def _json_block(data: Dict) -> str:
    if not data:
        return "_none_"
    return f"```json\n{json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True)}\n```"


def render_research_plan(plan: ResearchPlan) -> str:
    actions = plan.strategic_actions or []
    action_lines = "\n".join(f"- {item}" for item in actions) if actions else "- _none_"
    return (
        "## Research Plan\n"
        f"- Market: `{plan.market}`\n"
        f"- Recommendation: `{plan.recommendation.value}`\n"
        f"- Confidence: `{_pct(plan.confidence)}`\n\n"
        "### Rationale\n"
        f"{plan.rationale.strip()}\n\n"
        "### Strategic Actions\n"
        f"{action_lines}\n\n"
        "### Metadata\n"
        f"{_json_block(plan.metadata)}\n"
    )


def render_trader_proposal(proposal: TraderProposal) -> str:
    stop = "n/a" if proposal.stop_loss is None else f"{proposal.stop_loss:.3f}"
    max_loss = (
        "n/a"
        if proposal.max_loss_acceptable is None
        else f"{proposal.max_loss_acceptable:.4f}"
    )
    return (
        "## Trader Proposal\n"
        f"- Action: `{proposal.action.value}`\n"
        f"- Entry Price: `{proposal.entry_price:.3f}`\n"
        f"- Stop Loss: `{stop}`\n"
        f"- Position Sizing (Kelly frac): `{proposal.position_sizing:.4f}`\n"
        f"- Max Loss Acceptable: `{max_loss}`\n\n"
        "### Reasoning\n"
        f"{proposal.reasoning.strip()}\n"
    )


def render_portfolio_decision(decision: PortfolioDecision) -> str:
    stop = "n/a" if decision.stop_loss is None else f"{decision.stop_loss:.3f}"
    return (
        "## Portfolio Decision\n"
        f"- Rating: `{decision.rating.value}`\n"
        f"- Action: `{decision.action.value}`\n"
        f"- Horizon: `{decision.investment_horizon}`\n"
        f"- Position Size: `{decision.position_size:.4f}`\n"
        f"- Entry Price: `{decision.entry_price:.3f}`\n"
        f"- Stop Loss: `{stop}`\n\n"
        "### Executive Summary\n"
        f"{decision.executive_summary.strip()}\n\n"
        "### Metadata\n"
        f"{_json_block(decision.metadata)}\n"
    )


def render_marginal_analysis(
    analysis: Union[MarginalAIAnalysis, object],
    *,
    market_id: Optional[str] = None,
    strategy_hint: str = "",
) -> str:
    """
    Render the existing marginal AI output to markdown.

    Accepts either `MarginalAIAnalysis` or a dataclass-like object with matching attributes.
    """
    if isinstance(analysis, MarginalAIAnalysis):
        model = analysis
    else:
        model = MarginalAIAnalysis(
            reasoning=str(getattr(analysis, "reasoning", "") or ""),
            confidence_score=float(getattr(analysis, "confidence_score", 0.0) or 0.0),
            estimated_probability=float(
                getattr(analysis, "estimated_probability", 0.0) or 0.0
            ),
            recommendation=str(getattr(analysis, "recommendation", "HOLD") or "HOLD"),
        )

    return (
        "## Marginal Analysis\n"
        f"- Strategy: `{strategy_hint or 'n/a'}`\n"
        f"- Market ID: `{market_id or 'n/a'}`\n"
        f"- Recommendation: `{model.recommendation.value}`\n"
        f"- Confidence: `{_pct(model.confidence_score)}`\n"
        f"- Estimated P(YES): `{_pct(model.estimated_probability)}`\n\n"
        "### Reasoning\n"
        f"{model.reasoning.strip()}\n"
    )

