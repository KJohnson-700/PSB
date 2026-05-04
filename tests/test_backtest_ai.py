"""Backtest AI proxy recommendations (BUY_YES / BUY_NO) used by quant backtests."""

import pytest

from src.backtest.backtest_ai import BacktestAIAgent


@pytest.fixture
def proxy_agent():
    return BacktestAIAgent(
        {
            "backtest": {
                "ai_proxy": {"center_band": 0.04, "reversion_strength": 0.35},
            }
        }
    )


@pytest.mark.asyncio
async def test_backtest_ai_high_yes_recommends_buy_no(proxy_agent):
    analysis = await proxy_agent.analyze_market(
        "Will it rain?", "desc", 0.56, "m1"
    )
    assert analysis is not None
    assert analysis.recommendation == "BUY_NO"


@pytest.mark.asyncio
async def test_backtest_ai_low_yes_recommends_buy_yes(proxy_agent):
    analysis = await proxy_agent.analyze_market(
        "Will it rain?", "desc", 0.44, "m1"
    )
    assert analysis is not None
    assert analysis.recommendation == "BUY_YES"


@pytest.mark.asyncio
async def test_backtest_ai_neutral_band_returns_none(proxy_agent):
    analysis = await proxy_agent.analyze_market(
        "Will it rain?", "desc", 0.50, "m1"
    )
    assert analysis is None
