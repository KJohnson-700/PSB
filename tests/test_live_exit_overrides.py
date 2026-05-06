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
