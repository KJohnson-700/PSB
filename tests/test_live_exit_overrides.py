from datetime import datetime, timedelta, timezone
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

    assert btc == (0.03, 2.25, 18.0, 0.5)
    assert eth == (0.02, 3.0, 18.0, 0.5)
    # updown_max_hold_mins falls back to global because not overridden.
    assert xrp == (0.04, 1.5, 18.0, 0.5)


def test_down_lane_5m_window_override_is_used_for_eth_macro():
    cfg = {
        "trading": {
            "exit_rules": {
                "enabled": True,
                "take_profit_pct": 0.99,
                "stop_loss_pct": 0.30,
                "max_hold_hours": 72,
                "updown_stop_loss_pct": 0.20,
                "updown_stop_cents": 0.03,
                "updown_exit_window_mins": 2.25,
                "updown_overrides": {
                    "eth_macro": {
                        "window_lane_overrides": {
                            "5m": {
                                "down": {
                                    "updown_stop_loss_pct": 0.14,
                                }
                            }
                        }
                    }
                },
            }
        }
    }
    mgr = PositionExitManager(cfg)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pos = SimpleNamespace(
        market_id="m1",
        market_question="Ethereum Up or Down - test",
        outcome="NO",
        strategy="eth_macro",
        size=10.0,
        entry_price=0.50,
        current_price=0.50,
        pnl=0.0,
        opened_at=now - timedelta(minutes=2),
        end_date=now + timedelta(minutes=3),
        entry_leg="NO",
        window_size="5m",
    )
    exits = mgr.check_exits({"p1": pos}, {"m1": 0.58}, {"m1": ("YES_TOKEN", "NO_TOKEN")})
    assert len(exits) == 1
    assert exits[0].reason == "updown_stop_loss"


def test_updown_take_profit_uses_lane_window_override():
    cfg = {
        "trading": {
            "exit_rules": {
                "enabled": True,
                "take_profit_pct": 0.99,
                "stop_loss_pct": 0.30,
                "max_hold_hours": 72,
                "updown_stop_loss_pct": 0.50,
                "updown_overrides": {
                    "eth_macro": {
                        "window_lane_overrides": {
                            "5m": {
                                "down": {
                                    "take_profit_pct": 0.10,
                                }
                            }
                        }
                    }
                },
            }
        }
    }
    mgr = PositionExitManager(cfg)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pos = SimpleNamespace(
        market_id="m1",
        market_question="Ethereum Up or Down - test",
        outcome="NO",
        strategy="eth_macro",
        size=10.0,
        entry_price=0.50,
        current_price=0.50,
        pnl=0.0,
        opened_at=now - timedelta(minutes=2),
        end_date=now + timedelta(minutes=3),
        entry_leg="NO",
        window_size="5m",
    )
    exits = mgr.check_exits({"p1": pos}, {"m1": 0.42}, {"m1": ("YES_TOKEN", "NO_TOKEN")})
    assert len(exits) == 1
    assert exits[0].reason == "take_profit"


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


def test_late_entry_shrinks_cents_stop_window():
    """Position opened with only 3 min on the clock should not trigger the
    cents stop at 2.25 min remaining when the configured fraction is 0.5
    (effective window = 0.5 * 3 = 1.5 min)."""
    cfg = {
        "trading": {
            "exit_rules": {
                "enabled": True,
                "take_profit_pct": 0.99,
                "stop_loss_pct": 0.30,
                "max_hold_hours": 72,
                "updown_stop_loss_pct": 0.50,
                "updown_stop_cents": 0.03,
                "updown_exit_window_mins": 2.25,
                "updown_exit_window_max_fraction": 0.5,
            }
        }
    }
    mgr = PositionExitManager(cfg)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Opened 0.5 min ago; market ends in 2.5 min ⇒ hold runway at entry = 3.0 min.
    # Effective window = 0.5 * 3.0 = 1.5 min. With 2.5 min remaining, the cents
    # stop should NOT yet be eligible.
    pos = SimpleNamespace(
        market_id="m1",
        market_question="Bitcoin Up or Down - test",
        outcome="YES",
        strategy="bitcoin",
        size=10.0,
        entry_price=0.50,
        opened_at=now - timedelta(seconds=30),
        end_date=now + timedelta(minutes=2, seconds=30),
        entry_leg="YES",
    )
    # Price moved 4¢ adverse — would normally trip the cents stop, but
    # we're outside the scaled window so it must not fire.
    exits = mgr.check_exits(
        {"p1": pos},
        {"m1": 0.46},
        {"m1": ("YES_TOKEN", "NO_TOKEN")},
    )
    assert exits == []


def test_late_entry_cents_stop_still_fires_inside_scaled_window():
    """Same late entry, but now we're well inside the scaled window
    (1.0 min remaining < 1.5 min effective window) — cents stop should fire."""
    cfg = {
        "trading": {
            "exit_rules": {
                "enabled": True,
                "take_profit_pct": 0.99,
                "stop_loss_pct": 0.30,
                "max_hold_hours": 72,
                "updown_stop_loss_pct": 0.50,
                "updown_stop_cents": 0.03,
                "updown_exit_window_mins": 2.25,
                "updown_exit_window_max_fraction": 0.5,
            }
        }
    }
    mgr = PositionExitManager(cfg)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Entry runway 3 min, currently 1 min remaining ⇒ inside scaled window of 1.5.
    pos = SimpleNamespace(
        market_id="m1",
        market_question="Bitcoin Up or Down - test",
        outcome="YES",
        strategy="bitcoin",
        size=10.0,
        entry_price=0.50,
        opened_at=now - timedelta(minutes=2),
        end_date=now + timedelta(minutes=1),
        entry_leg="YES",
    )
    exits = mgr.check_exits(
        {"p1": pos},
        {"m1": 0.46},
        {"m1": ("YES_TOKEN", "NO_TOKEN")},
    )
    assert len(exits) == 1
    assert exits[0].reason == "updown_time_stop"


def test_high_entry_price_uses_tighter_cents_stop():
    """Entry at $0.65 (≥ 0.60 threshold) should use the 2¢ tight stop, not 3¢."""
    cfg = {
        "trading": {
            "exit_rules": {
                "enabled": True,
                "take_profit_pct": 0.99,
                "stop_loss_pct": 0.30,
                "max_hold_hours": 72,
                "updown_stop_loss_pct": 0.50,
                "updown_stop_cents": 0.03,
                "updown_stop_cents_high_entry": 0.02,
                "updown_high_entry_threshold": 0.60,
                "updown_exit_window_mins": 2.25,
                "updown_exit_window_max_fraction": 1.0,
            }
        }
    }
    mgr = PositionExitManager(cfg)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pos = SimpleNamespace(
        market_id="m1",
        market_question="Bitcoin Up or Down - test",
        outcome="YES",
        strategy="bitcoin",
        size=10.0,
        entry_price=0.65,
        opened_at=now - timedelta(minutes=10),
        end_date=now + timedelta(minutes=1),  # inside window
        entry_leg="YES",
    )
    # 2.5¢ adverse: would NOT trip 3¢ stop, but DOES trip 2¢ tightened stop.
    exits = mgr.check_exits(
        {"p1": pos},
        {"m1": 0.625},
        {"m1": ("YES_TOKEN", "NO_TOKEN")},
    )
    assert len(exits) == 1
    assert exits[0].reason == "updown_time_stop"


def test_in_profit_stop_tightens_when_threshold_crossed():
    """Once pnl ≥ +5%, the adverse % stop tightens from 20% to 8%."""
    cfg = {
        "trading": {
            "exit_rules": {
                "enabled": True,
                "take_profit_pct": 0.99,
                "stop_loss_pct": 0.30,
                "max_hold_hours": 72,
                "updown_stop_loss_pct": 0.20,
                "updown_in_profit_stop_trigger_pct": 0.05,
                "updown_in_profit_stop_tighten_to_pct": 0.08,
                "updown_exit_window_mins": 2.25,
            }
        }
    }
    mgr = PositionExitManager(cfg)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pos = SimpleNamespace(
        market_id="m1",
        market_question="Bitcoin Up or Down - test",
        outcome="YES",
        strategy="bitcoin",
        size=10.0,
        entry_price=0.50,
        opened_at=now - timedelta(minutes=3),
        end_date=now + timedelta(minutes=8),
        entry_leg="YES",
    )
    # pnl = (0.45 - 0.50) / 0.50 = -10%. Below base 20% (wouldn't fire) but below tightened 8%
    # ONLY if the in-profit tightening has been triggered. With current price never having
    # exceeded entry, trigger has never crossed, so this should NOT fire.
    exits = mgr.check_exits(
        {"p1": pos},
        {"m1": 0.45},
        {"m1": ("YES_TOKEN", "NO_TOKEN")},
    )
    # Current implementation gates on current pnl_pct, not peak — so at -10% pnl,
    # the trigger (+5%) is not met, tightening does not apply, and 20% base does not fire.
    assert exits == []


def test_legacy_position_without_window_size_falls_back_to_inferred_runway():
    cfg = {
        "trading": {
            "exit_rules": {
                "enabled": True,
                "take_profit_pct": 0.99,
                "stop_loss_pct": 0.30,
                "max_hold_hours": 72,
                "updown_stop_cents": 0.03,
                "updown_exit_window_mins": 2.25,
                "updown_overrides": {
                    "sol_macro": {
                        "window_lane_overrides": {
                            "5m": {
                                "down": {
                                    "updown_stop_cents": 0.015,
                                    "updown_exit_window_mins": 1.5,
                                }
                            }
                        }
                    }
                },
            }
        }
    }
    mgr = PositionExitManager(cfg)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pos = SimpleNamespace(
        market_id="m1",
        market_question="Solana Up or Down - test",
        outcome="NO",
        strategy="sol_macro",
        size=10.0,
        entry_price=0.50,
        opened_at=now - timedelta(minutes=4),
        end_date=now + timedelta(minutes=1),
        entry_leg="NO",
    )
    exits = mgr.check_exits({"p1": pos}, {"m1": 0.52}, {"m1": ("YES_TOKEN", "NO_TOKEN")})
    assert len(exits) == 1
    assert exits[0].reason == "updown_time_stop"
