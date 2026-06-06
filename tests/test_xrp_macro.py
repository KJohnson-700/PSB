"""Tests for XRPMacroStrategy — verifies it inherits correctly from SolMacroStrategy."""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from src.strategies.xrp_macro import XRPMacroStrategy
from src.strategies.sol_macro import BiasResolution, SolMacroStrategy
from src.analysis.math_utils import PositionSizer


def _pos_sizer() -> PositionSizer:
    return PositionSizer(
        kelly_fraction=0.25,
        max_position_pct=0.05,
        min_position=5,
        max_position=50,
    )


def _cfg(enabled: bool = False) -> dict:
    return {
        "trading": {
            "default_position_size": 5,
            "max_position_size": 50,
            "kelly_fraction": 0.25,
            "max_exposure_per_trade": 0.05,
        },
        "strategies": {
            "xrp_macro": {
                "enabled": enabled,
                "min_liquidity": 5000,
                "min_edge": 0.08,
                "use_ai": False,
            }
        },
    }


def _mk_strategy(enabled: bool = False) -> XRPMacroStrategy:
    return XRPMacroStrategy(_cfg(enabled=enabled), MagicMock(), _pos_sizer())


def test_xrp_macro_is_subclass_of_sol_macro():
    assert issubclass(XRPMacroStrategy, SolMacroStrategy)


def test_xrp_macro_instantiates_with_correct_config():
    st = _mk_strategy()
    assert st._signal_strategy_name == "xrp_macro"
    assert st.min_edge == 0.08
    assert st.min_liquidity == 5000


def test_xrp_macro_disabled_by_default():
    st = _mk_strategy(enabled=False)
    assert not st.enabled


def test_xrp_session_trial_disables_weak_5m_native_lanes_in_settings():
    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    xrp = settings["strategies"]["xrp_macro"]

    assert xrp["disable_buy_no_5m_native"] is True
    assert xrp["disable_buy_yes_5m_native_when_alt_1h_neutral"] is False


def test_xrp_macro_detects_xrp_market():
    st = _mk_strategy()

    class _M:
        question = "XRP Up or Down 2:15AM–2:30AM ET"
        description = ""

    assert st._is_solana_market(_M())


def test_xrp_macro_rejects_non_xrp_market():
    st = _mk_strategy()

    class _M:
        question = "Bitcoin Up or Down 2:15AM–2:30AM ET"
        description = ""

    assert not st._is_solana_market(_M())


def test_xrp_macro_hourly_buy_yes_native_bonus_is_opted_in():
    cfg = _cfg(enabled=True)
    cfg["strategies"]["xrp_macro"]["hourly_buy_yes_native_bonus_1h"] = 0.03
    cfg["strategies"]["xrp_macro"]["hourly_buy_yes_native_bonus_min_ltf_strength_1h"] = 0.30
    st = XRPMacroStrategy(cfg, MagicMock(), _pos_sizer())

    native = BiasResolution(
        allowed_side="LONG",
        side_source="xrp_1h_native",
        horizon_tf="1h",
        horizon_bias="BULLISH",
        slower_biases={},
        primary_htf_bias="BULLISH",
    )

    assert st._hourly_buy_yes_native_bonus(
        window_size="1h",
        allowed_side="LONG",
        resolution=native,
        ltf_strength=0.35,
    ) == 0.03
