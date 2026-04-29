from datetime import datetime, timedelta

import pytest

from src.execution.clob_client import CLOBClient, Position, RiskManager


@pytest.mark.asyncio
async def test_can_sell_token_fails_closed_when_live_client_missing():
    client = CLOBClient({"trading": {"dry_run": False}, "polymarket": {}})
    client.client = None

    assert await client.can_sell_token("token", "market") is False


def _risk_manager() -> RiskManager:
    return RiskManager(
        {
            "term_risk": {
                "min_edge": {"SHORT_TERM": 0.01},
                "caps": {"SHORT_TERM": 0.15},
                "sizing": {"SHORT_TERM": 0.15},
            },
            "risk": {"max_trades_per_day": 50},
        }
    )


def test_crypto_buy_position_counts_full_usd_cost():
    rm = _risk_manager()
    rm.active_positions["buy"] = Position(
        position_id="buy",
        market_id="m1",
        market_question="Bitcoin Up or Down",
        outcome="YES",
        size=10.0,
        entry_price=0.5,
        current_price=0.5,
        pnl=0.0,
        opened_at=datetime.now(),
        end_date=datetime.now() + timedelta(minutes=10),
        strategy="bitcoin",
    )

    can_trade, size, reason = rm.evaluate_entry(
        end_date=datetime.now() + timedelta(minutes=10),
        current_edge=0.1,
        bankroll=100.0,
        strategy="bitcoin",
    )

    assert can_trade is True
    assert size == 5.0
    assert reason == "OK"


def test_crypto_sell_yes_position_counts_share_cost():
    rm = _risk_manager()
    rm.active_positions["sell"] = Position(
        position_id="sell",
        market_id="m1",
        market_question="Bitcoin Up or Down",
        outcome="NO",
        size=20.0,
        entry_price=0.5,
        current_price=0.5,
        pnl=0.0,
        opened_at=datetime.now(),
        end_date=datetime.now() + timedelta(minutes=10),
        strategy="bitcoin",
    )

    can_trade, size, reason = rm.evaluate_entry(
        end_date=datetime.now() + timedelta(minutes=10),
        current_edge=0.1,
        bankroll=100.0,
        strategy="bitcoin",
    )

    assert can_trade is True
    assert size == 5.0
    assert reason == "OK"
