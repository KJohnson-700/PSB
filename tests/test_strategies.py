"""
Strategy unit tests — verify signal logic produces correct outputs for known inputs.
No random data. Each test represents a real market scenario.
"""
import inspect
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from src.analysis.ai_agent import AIAgent
from src.analysis.kelly_sizer import KellySizer
from src.analysis.null_ai_agent import NullAIAgent
from src.market.scanner import Market
from src.analysis.math_utils import PositionSizer

from tests.async_helpers import run_async


def _make_config():
    return {
        "polymarket": {"min_liquidity": 10000, "max_spread": 0.05},
        "trading": {
            "kelly_fraction": 0.25,
            "max_exposure_per_trade": 0.05,
            "default_position_size": 50,
            "max_position_size": 200,
        },
        "strategies": {},
    }


_SENTINEL = object()

def _make_market(yes_price, end_date=_SENTINEL, liquidity=0, market_id="test-market"):
    if end_date is _SENTINEL:
        end_date = datetime.now() + timedelta(days=90)
    return Market(
        id=market_id,
        question="Will X happen?",
        description="Test market",
        volume=100000,
        liquidity=liquidity,
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        spread=0.02,
        end_date=end_date,
        token_id_yes="tok_yes",
        token_id_no="tok_no",
        group_item_title="",
    )


# BACKTEST AI AGENT tests removed with src/backtest/* deletion (2026-05-24).
# Per CLAUDE.md, backtest engines are known-broken; validation goes through
# the ghost log (rejected_candidates_settled.jsonl) instead. The live AIAgent
# is covered by its own tests in tests/test_ai_agent.py.


class TestNullAIAgent:
    def test_null_ai_matches_live_call_shape_and_returns_none(self):
        ai = NullAIAgent()
        assert ai.is_available() is False
        assert ai.research_narrative_enabled() is False
        assert ai.shadow_pipeline_enabled() is False
        result = run_async(
            ai.analyze_market(
                "Q",
                "desc",
                0.51,
                "m7",
                news_context="news",
                strategy_hint="sol_macro",
            )
        )
        assert result is None


# ─── CONSENSUS vs CRYPTO UPDOWN ───────────────────────────────────

class TestConsensusSkipsCryptoUpdown:
    """Consensus alerts must not fire on BTC/SOL/ETH short up-down windows (wrong label + wrong product)."""

    def test_is_crypto_updown_market_detects_btc_sol_eth(self):
        from src.market.scanner import is_crypto_updown_market

        end = datetime.now() + timedelta(hours=10)
        for q in (
            "Bitcoin Up or Down - March 9, 2:15AM-2:30AM ET",
            "Solana Up or Down - March 9, 2:15AM-2:20AM ET",
            "Ethereum Up or Down - March 9, 2:15AM-2:30AM ET",
            "Hyperliquid Up or Down - March 9, 2:15AM-2:20AM ET",
        ):
            m = Market(
                id="x",
                question=q,
                description="",
                volume=1,
                liquidity=50000,
                yes_price=0.90,
                no_price=0.10,
                spread=0.02,
                end_date=end,
                token_id_yes="y",
                token_id_no="n",
                group_item_title="",
            )
            assert is_crypto_updown_market(m) is True
        generic = _make_market(0.90, end_date=end)
        assert is_crypto_updown_market(generic) is False

    def test_consensus_scan_ignores_btc_updown_even_at_high_yes(self):
        from src.strategies.consensus import ConsensusStrategy

        cfg = {
            "strategies": {
                "consensus": {
                    "enabled": True,
                    "threshold": 0.85,
                    "min_opposite_liquidity": 1,
                    "expiration_window_hours": 48,
                }
            }
        }
        strat = ConsensusStrategy(cfg)
        end = datetime.now() + timedelta(hours=10)
        m = Market(
            id="btc-updown-15m",
            question="Bitcoin Up or Down - March 9, 2:15AM-2:30AM ET",
            description="",
            volume=100000,
            liquidity=50000,
            yes_price=0.92,
            no_price=0.08,
            spread=0.02,
            end_date=end,
            token_id_yes="y",
            token_id_no="n",
            group_item_title="",
        )
        assert strat.scan_for_consensus([m]) == []

    def test_consensus_scan_still_emits_for_generic_market(self):
        from src.strategies.consensus import ConsensusStrategy

        cfg = {
            "strategies": {
                "consensus": {
                    "enabled": True,
                    "threshold": 0.85,
                    "min_opposite_liquidity": 1,
                    "expiration_window_hours": 48,
                }
            }
        }
        strat = ConsensusStrategy(cfg)
        end = datetime.now() + timedelta(hours=10)
        m = _make_market(0.92, end_date=end, liquidity=50000, market_id="gen-1")
        m = Market(
            id=m.id,
            question="Will candidate X win the primary?",
            description=m.description,
            volume=m.volume,
            liquidity=m.liquidity,
            yes_price=m.yes_price,
            no_price=m.no_price,
            spread=m.spread,
            end_date=m.end_date,
            token_id_yes=m.token_id_yes,
            token_id_no=m.token_id_no,
            group_item_title=m.group_item_title,
        )
        alerts = strat.scan_for_consensus([m])
        assert len(alerts) == 1
        assert alerts[0].market_id == "gen-1"
