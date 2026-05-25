from __future__ import annotations

from unittest.mock import MagicMock

from src.analysis.lane_entry_policy import resolve_entry_policy_side, resolve_lane_entry_policy
from src.analysis.math_utils import PositionSizer
from src.strategies.bitcoin import BitcoinStrategy
from src.strategies.eth_macro import ETHMacroStrategy
from src.strategies.hype_macro import HYPEMacroStrategy
from src.strategies.sol_macro import SolMacroStrategy
from src.strategies.xrp_macro import XRPMacroStrategy


def _base_cfg() -> dict:
    return {
        "entry_policy": {"defaults": {"enabled": True, "size_multiplier": 1.0}},
        "strategies": {
            "bitcoin": {"enabled": True, "min_edge": 0.10, "min_edge_5m": 0.08},
            "sol_macro": {"enabled": True, "min_edge": 0.09, "min_edge_5m": 0.085},
            "eth_macro": {"enabled": True, "min_edge": 0.09, "min_edge_5m": 0.10},
            "hype_macro": {"enabled": True, "min_edge": 0.09, "min_edge_5m": 0.07},
            "xrp_macro": {"enabled": True, "min_edge": 0.09, "min_edge_5m": 0.085},
        },
        "hyperliquid": {},
    }


def test_resolve_entry_policy_side_maps_trade_thesis():
    assert resolve_entry_policy_side(direction="UP", action="BUY_YES") == "up"
    assert resolve_entry_policy_side(direction="UP", action="BUY_NO") == "up"
    assert resolve_entry_policy_side(direction="DOWN", action="BUY_YES") == "down"
    assert resolve_entry_policy_side(direction="DOWN", action="BUY_NO") == "down"


def test_entry_policy_precedence_window_side_beats_strategy_and_global():
    cfg = _base_cfg()
    cfg["strategies"]["bitcoin"]["entry_policy"] = {
        "defaults": {
            "min_edge": 0.10,
            "entry_price_min": 0.45,
            "entry_price_max": 0.55,
            "entry_window_min": 2.0,
            "entry_window_max": 10.0,
        },
        "window_side_overrides": {
            "5m": {
                "down": {
                    "min_edge": 0.12,
                    "entry_window_min": 0.5,
                    "entry_window_max": 5.0,
                    "size_multiplier": 0.7,
                }
            }
        },
    }
    policy = resolve_lane_entry_policy(
        strategy_name="bitcoin",
        window_size="5m",
        side="down",
        full_config=cfg,
        legacy_policy={"min_edge": 0.09, "entry_window_min": 1.0, "entry_window_max": 6.0},
    )
    assert policy.min_edge == 0.12
    assert policy.entry_window_min == 0.5
    assert policy.entry_window_max == 5.0
    assert policy.size_multiplier == 0.7


def test_entry_policy_uses_legacy_fallback_when_new_keys_absent():
    policy = resolve_lane_entry_policy(
        strategy_name="bitcoin",
        window_size="15m",
        side="up",
        full_config=_base_cfg(),
        legacy_policy={
            "min_edge": 0.10,
            "entry_price_min": 0.45,
            "entry_price_max": 0.55,
            "entry_window_min": 2.0,
            "entry_window_max": 19.0,
        },
    )
    assert policy.min_edge == 0.10
    assert policy.entry_price_min == 0.45
    assert policy.entry_window_max == 19.0


def test_bitcoin_resolves_lane_specific_policy():
    cfg = _base_cfg()
    cfg["strategies"]["bitcoin"].update(
        {
            "entry_price_min_updown": 0.45,
            "entry_price_max_updown": 0.55,
            "entry_window_5m_min": 0.5,
            "entry_window_5m_max": 5.5,
            "min_edge_buy_no": 0.09,
            "entry_policy": {
                "window_side_overrides": {
                    "5m": {"down": {"enabled": False}, "up": {"min_edge": 0.08}}
                }
            },
        }
    )
    strat = BitcoinStrategy(cfg, MagicMock(), PositionSizer())
    side, policy = strat._resolve_lane_entry_policy(window_size="5m", action="BUY_NO", direction="DOWN")
    assert side == "down"
    assert policy.enabled is False
    side2, policy2 = strat._resolve_lane_entry_policy(window_size="5m", action="BUY_YES", direction="UP")
    assert side2 == "up"
    assert policy2.min_edge == 0.08


def test_sol_style_strategies_resolve_window_side_specific_policy():
    cfg = _base_cfg()
    cfg["strategies"]["sol_macro"].update(
        {
            "entry_price_min": 0.42,
            "entry_price_max": 0.58,
            "entry_window_15m_min": 1.0,
            "entry_window_15m_max": 28.0,
            "entry_policy": {
                "window_side_overrides": {
                    "15m": {
                        "down": {"min_edge": 0.11, "size_multiplier": 0.8},
                        "up": {"min_edge": 0.09},
                    }
                }
            },
        }
    )
    for cls, key in (
        (SolMacroStrategy, "sol_macro"),
        (ETHMacroStrategy, "eth_macro"),
        (HYPEMacroStrategy, "hype_macro"),
        (XRPMacroStrategy, "xrp_macro"),
    ):
        local_cfg = _base_cfg()
        local_cfg["strategies"][key].update(cfg["strategies"]["sol_macro"])
        strat = cls(local_cfg, MagicMock(), PositionSizer())
        side, policy = strat._resolve_lane_entry_policy(window_size="15m", action="BUY_NO", direction="DOWN")
        assert side == "down"
        assert policy.min_edge == 0.11
        assert policy.size_multiplier == 0.8
