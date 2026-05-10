from datetime import datetime, timedelta
from types import SimpleNamespace

from src.execution.live_testing import PositionExitManager


def test_updown_override_resolution_for_eth_and_xrp_macro():
    cfg = {
        "trading": {
            "exit_rules": {
                "enabled": True,
                "take_profit_pct": 0.15,
                "stop_loss_pct": 0.30,
                "max_hold_hours": 72,
                "updown_stop_cents": 0.03,
                "updown_exit_window_mins": 2.25,
                "updown_max_hold_mins": 18.0,
                "updown_overrides": {
                    "eth_macro": {
                        "updown_stop_cents": 0.02,
                        "updown_exit_window_mins": 3.0,
                    },
                    "xrp_macro": {
                        "updown_stop_cents": 0.04,
                        "updown_exit_window_mins": 1.5,
                    }
                },
            }
        }
    }
    mgr = PositionExitManager(cfg)

    btc = mgr._resolve_updown_exit_params("bitcoin")
    eth = mgr._resolve_updown_exit_params("eth_macro")
    xrp = mgr._resolve_updown_exit_params("xrp_macro")

    assert btc == (0.03, 2.25, 18.0)
    assert eth == (0.02, 3.0, 18.0)
    # updown_max_hold_mins falls back to global because not overridden.
    assert xrp == (0.04, 1.5, 18.0)


def test_updown_percentage_stop_loss_fires_before_expiry_window():
    cfg = {
        "trading": {
            "exit_rules": {
                "enabled": True,
                "take_profit_pct": 0.15,
                "stop_loss_pct": 0.30,
                "max_hold_hours": 72,
                "updown_stop_loss_pct": 0.20,
                "updown_exit_window_mins": 2.25,
            }
        }
    }
    mgr = PositionExitManager(cfg)
    pos = SimpleNamespace(
        market_id="m1",
        market_question="Bitcoin Up or Down - test",
        outcome="YES",
        strategy="bitcoin",
        size=10.0,
        entry_price=0.50,
        opened_at=datetime.now() - timedelta(minutes=3),
        end_date=datetime.now() + timedelta(minutes=8),
        entry_leg="YES",
    )

    exits = mgr.check_exits(
        {"p1": pos},
        {"m1": 0.39},
        {"m1": ("YES_TOKEN", "NO_TOKEN")},
    )

    assert len(exits) == 1
    decision = exits[0]
    assert decision.reason == "updown_stop_loss"
    assert decision.exit_price == 0.39
    assert decision.unrealized_pnl == -1.1
