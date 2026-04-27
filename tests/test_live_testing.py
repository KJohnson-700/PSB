from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from src.execution.live_testing import PositionExitManager


def test_sell_yes_take_profit_buys_back_yes_token():
    mgr = PositionExitManager(
        {
            "trading": {
                "exit_rules": {
                    "enabled": True,
                    "take_profit_pct": 0.15,
                    "stop_loss_pct": 0.30,
                }
            }
        }
    )
    pos = SimpleNamespace(
        market_id="m1",
        market_question="Ethereum Up or Down - test",
        outcome="NO",
        strategy="eth_macro",
        size=10.0,
        entry_price=0.50,
        opened_at=datetime.now() - timedelta(minutes=3),
    )

    exits = mgr.check_exits(
        {"p1": pos},
        {"m1": 0.40},
        {"m1": ("YES_TOKEN", "NO_TOKEN")},
    )

    assert len(exits) == 1
    decision = exits[0]
    assert decision.action == "BUY"
    assert decision.token_id == "YES_TOKEN"
    assert decision.exit_price == 0.40
    assert decision.unrealized_pnl == 1.0
