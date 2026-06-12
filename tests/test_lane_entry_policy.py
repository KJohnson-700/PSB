from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml

from src.analysis.lane_entry_policy import resolve_entry_policy_side, resolve_lane_entry_policy
from src.analysis.math_utils import PositionSizer
from src.strategies.strategy_config import resolve_tf_config_value
from src.strategies.bitcoin import BitcoinStrategy
from src.strategies.eth_macro import ETHMacroStrategy
from src.strategies.hype_macro import HYPEMacroStrategy
from src.strategies.sol_macro import SolMacroStrategy
from src.strategies.xrp_macro import XRPMacroStrategy


def _base_cfg() -> dict:
    return {
        "entry_policy": {"defaults": {"enabled": True, "size_multiplier": 1.0}},
        "strategies": {
            "bitcoin": {"enabled": True, "min_edge": 0.10},
            "sol_macro": {"enabled": True, "min_edge": 0.09},
            "eth_macro": {"enabled": True, "min_edge": 0.09},
            "hype_macro": {"enabled": True, "min_edge": 0.09},
            "xrp_macro": {"enabled": True, "min_edge": 0.09},
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


def test_tf_config_resolver_scopes_by_timeframe_without_legacy_tf_keys():
    cfg = {
        "min_edge": 0.09,
        "defaults": {"min_edge": 0.10, "entry_window_min": 2.0},
        "by_tf": {
            "15m": {"min_edge": 0.11},
            "1h": {"entry_window_min": 4.0},
        },
    }

    assert resolve_tf_config_value(cfg, tf="5m", key="min_edge") == 0.10
    assert resolve_tf_config_value(cfg, tf="15m", key="min_edge") == 0.11
    assert resolve_tf_config_value(cfg, tf="1h", key="entry_window_min") == 4.0
    assert resolve_tf_config_value(cfg, tf="5m", key="entry_window_min") == 2.0


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


def test_bitcoin_legacy_policy_uses_by_tf_without_cross_timeframe_leakage():
    cfg = _base_cfg()
    cfg["strategies"]["bitcoin"].update(
        {
            "min_edge": 0.09,
            "entry_window_auto_align": False,
            "defaults": {"min_edge": 0.10},
            "by_tf": {
                "5m": {"min_edge": 0.12, "entry_window_min": 0.25, "entry_window_max": 4.25},
                "15m": {"min_edge": 0.095, "entry_window_min": 2.0, "entry_window_max": 19.0},
            },
        }
    )
    strat = BitcoinStrategy(cfg, MagicMock(), PositionSizer())

    policy_5m = strat._legacy_entry_policy(window_size="5m", action="BUY_YES")
    policy_15m = strat._legacy_entry_policy(window_size="15m", action="BUY_YES")

    assert policy_5m["min_edge"] == 0.12
    assert policy_5m["entry_window_min"] == 0.25
    assert policy_15m["min_edge"] == 0.095
    assert policy_15m["entry_window_min"] == 2.0


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


def test_sol_legacy_policy_uses_by_tf_thresholds_and_windows():
    cfg = _base_cfg()
    cfg["strategies"]["sol_macro"].update(
        {
            "min_edge": 0.09,
            "hard_min_edge": 0.07,
            "entry_window_auto_align": False,
            "by_tf": {
                "5m": {"min_edge": 0.105, "entry_window_min": 0.0, "entry_window_max": 3.5},
                "15m": {"min_edge_buy_no": 0.115, "entry_window_min": 1.0, "entry_window_max": 32.0},
            },
        }
    )
    strat = SolMacroStrategy(cfg, MagicMock(), PositionSizer())

    policy_5m = strat._legacy_entry_policy(window_size="5m", action="BUY_YES", direction="UP")
    policy_15m_down = strat._legacy_entry_policy(
        window_size="15m",
        action="BUY_NO",
        direction="DOWN",
    )

    assert policy_5m["min_edge"] == 0.105
    assert policy_5m["entry_window_max"] == 3.5
    assert policy_15m_down["min_edge"] == 0.115
    assert policy_15m_down["entry_window_max"] == 32.0


def test_bnb_15m_buy_yes_reopen_keeps_buy_no_disabled_in_canonical_policy():
    cfg_path = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    bnb_15m = cfg["strategies"]["bnb_macro"]["by_tf"]["15m"]

    up_policy = resolve_lane_entry_policy(
        strategy_name="bnb_macro",
        window_size="15m",
        side="up",
        full_config=cfg,
        legacy_policy={"min_edge": 0.0},
    )
    down_policy = resolve_lane_entry_policy(
        strategy_name="bnb_macro",
        window_size="15m",
        side="down",
        full_config=cfg,
        legacy_policy={"min_edge": 0.0},
    )

    assert bnb_15m["min_edge"] == 0.09
    assert bnb_15m["min_edge_buy_no"] == 0.50
    assert up_policy.min_edge == 0.09
    assert up_policy.size_multiplier == 0.3
    assert down_policy.min_edge == 0.50
