"""Tests for XRPMacroStrategy — verifies it inherits correctly from SolMacroStrategy."""
from __future__ import annotations
from unittest.mock import MagicMock

from src.strategies.xrp_macro import XRPMacroStrategy
from src.strategies.sol_macro import SolMacroStrategy
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
