"""Regression checks for the alt/BTC decoupling invariant."""
from __future__ import annotations

import pytest

from src.strategies.bnb_macro import BNBMacroStrategy
from src.strategies.doge_macro import DOGEMacroStrategy
from src.strategies.eth_macro import ETHMacroStrategy
from src.strategies.hype_macro import HYPEMacroStrategy
from src.strategies.sol_macro import SolMacroStrategy
from src.strategies.xrp_macro import XRPMacroStrategy


@pytest.mark.parametrize(
    "strategy_cls",
    [
        SolMacroStrategy,
        ETHMacroStrategy,
        HYPEMacroStrategy,
        XRPMacroStrategy,
        DOGEMacroStrategy,
        BNBMacroStrategy,
    ],
)
def test_alt_macro_btc_trade_inputs_are_disabled(strategy_cls: type) -> None:
    strategy = strategy_cls.__new__(strategy_cls)

    assert strategy._btc_trade_inputs_enabled() is False


@pytest.mark.parametrize(
    "strategy_cls",
    [
        SolMacroStrategy,
        ETHMacroStrategy,
        HYPEMacroStrategy,
        XRPMacroStrategy,
        DOGEMacroStrategy,
        BNBMacroStrategy,
    ],
)
def test_alt_macro_ai_gate_windows_are_15m_1h_only(strategy_cls: type) -> None:
    strategy = strategy_cls.__new__(strategy_cls)

    assert strategy._DECISION_GATE_WINDOWS == frozenset({"15m", "1h"})
    assert "5m" not in strategy._DECISION_GATE_WINDOWS
