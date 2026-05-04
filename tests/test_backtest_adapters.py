"""Nautilus / PolyBot adapter — signal action → order side (all crypto + weather paths)."""

import pytest
from nautilus_trader.model.enums import OrderSide

from backtesting.adapters import signal_action_to_order_side


@pytest.mark.parametrize(
    "action,expected",
    [
        ("BUY_YES", OrderSide.BUY),
        ("BUY_NO", OrderSide.BUY),
        ("SELL_YES", OrderSide.SELL),
        ("HOLD", None),
        ("", None),
    ],
)
def test_signal_action_to_order_side_maps_buy_sell_for_backtest(action, expected):
    assert signal_action_to_order_side(action) is expected
