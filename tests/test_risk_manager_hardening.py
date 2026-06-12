from datetime import datetime, timedelta

import pytest

from src.execution.clob_client import CLOBClient, Position, RiskManager


def test_clob_cash_balance_normalizes_micro_usdc():
    assert CLOBClient._extract_cash_balance({"balance": "504250000"}) == 504.25


def test_clob_cash_balance_accepts_decimal_usdc():
    assert CLOBClient._extract_cash_balance({"balance": "12.34"}) == 12.34


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


def test_crypto_buy_position_counts_share_cost():
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
    assert size == 10.0
    assert reason == "OK"


def test_crypto_buy_no_position_counts_usd_notional():
    rm = _risk_manager()
    rm.active_positions["long_no"] = Position(
        position_id="long_no",
        market_id="m1",
        market_question="Bitcoin Up or Down",
        outcome="NO",
        size=10.0,
        entry_price=0.4,
        current_price=0.4,
        pnl=0.0,
        opened_at=datetime.now(),
        end_date=datetime.now() + timedelta(minutes=10),
        strategy="bitcoin",
        entry_leg="NO",
    )

    can_trade, size, reason = rm.evaluate_entry(
        end_date=datetime.now() + timedelta(minutes=10),
        current_edge=0.1,
        bankroll=100.0,
        strategy="bitcoin",
    )

    assert can_trade is True
    assert size == 11.0
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


def test_bnb_counts_against_short_term_budget():
    rm = _risk_manager()
    rm.active_positions["bnb"] = Position(
        position_id="bnb",
        market_id="m1",
        market_question="BNB Up or Down",
        outcome="YES",
        size=20.0,
        entry_price=0.5,
        current_price=0.5,
        pnl=0.0,
        opened_at=datetime.now(),
        end_date=datetime.now() + timedelta(minutes=10),
        strategy="bnb_macro",
    )

    can_trade, size, reason = rm.evaluate_entry(
        end_date=datetime.now() + timedelta(minutes=10),
        current_edge=0.1,
        bankroll=100.0,
        strategy="doge_macro",
    )

    assert can_trade is True
    assert size == 5.0
    assert reason == "OK"


def test_all_active_positions_share_global_slot_limit():
    rm = RiskManager(
        {
            "trading": {"dry_run": True},
            "risk": {"max_concurrent_positions": 1, "max_trades_per_day": 50},
        }
    )
    rm.active_positions["btc"] = Position(
        position_id="btc",
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

    can_trade, reason = rm.can_trade(strategy="doge_macro")

    assert can_trade is False
    assert reason == "Max concurrent positions reached"


def test_strategy_concurrent_position_limit_is_enforced_before_order():
    rm = RiskManager(
        {
            "trading": {"dry_run": True},
            "risk": {"max_concurrent_positions": 50, "max_trades_per_day": 50},
            "strategies": {"bnb_macro": {"max_concurrent_positions": 2}},
        }
    )
    for idx in range(2):
        rm.active_positions[f"bnb_{idx}"] = Position(
            position_id=f"bnb_{idx}",
            market_id=f"m{idx}",
            market_question="BNB Up or Down",
            outcome="YES",
            size=30.0,
            entry_price=0.5,
            current_price=0.5,
            pnl=0.0,
            opened_at=datetime.now(),
            end_date=datetime.now() + timedelta(minutes=15),
            strategy="bnb_macro",
        )

    can_trade, reason = rm.can_trade(strategy="bnb_macro")

    assert can_trade is False
    assert reason == "Max concurrent positions reached for bnb_macro"


def test_paper_trading_uses_paper_daily_trade_limit():
    rm = RiskManager(
        {
            "trading": {"dry_run": True},
            "risk": {"max_trades_per_day": 500, "paper_max_trades_per_day": 2000},
        }
    )
    rm.daily_trades = 500

    can_trade, reason = rm.can_trade()

    assert rm.effective_max_trades_per_day() == 2000
    assert can_trade is True
    assert reason == "OK"


def test_live_trading_uses_live_daily_trade_limit():
    rm = RiskManager(
        {
            "trading": {"dry_run": False},
            "risk": {"max_trades_per_day": 500, "paper_max_trades_per_day": 2000},
        }
    )
    rm.daily_trades = 500

    can_trade, reason = rm.can_trade()

    assert rm.effective_max_trades_per_day() == 500
    assert can_trade is False
    assert reason == "Daily trade limit reached"
