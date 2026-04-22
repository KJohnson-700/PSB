"""Shared LLM context lines for crypto strategies (BTC / SOL / ETH)."""
from __future__ import annotations

from src.market.scanner import Market


def format_market_metadata(market: Market) -> str:
    """Compact, model-friendly market facts for AI prompts."""
    parts = [f"id={market.id}"]
    title = getattr(market, "group_item_title", "") or ""
    if title.strip():
        parts.append(f"group={title.strip()}")
    hrs = market.hours_to_expiration
    if hrs is not None:
        parts.append(f"hours_to_resolution={hrs:.2f}")
    parts.append(f"liquidity_usd={market.liquidity:,.0f}")
    parts.append(f"spread={market.spread:.4f}")
    parts.append(f"yes={market.yes_price:.3f} no={market.no_price:.3f}")
    return " | ".join(parts)


def ai_recommendation_supports_action(recommendation: str, action: str) -> bool:
    """True when the model's recommendation matches the planned CLOB action."""
    rec = (recommendation or "").strip().upper()
    if action == "BUY_YES":
        return rec == "BUY_YES"
    if action == "SELL_YES":
        return rec == "BUY_NO"
    return False
