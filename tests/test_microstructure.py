"""Unit tests for order-book / trade-flow microstructure features."""

import pytest

from src.market.microstructure import ob_imbalance, trade_flow_ratio


# --- ob_imbalance -----------------------------------------------------------

def _book(bid_levels, ask_levels):
    return (
        [{"price": p, "size": s} for p, s in bid_levels],
        [{"price": p, "size": s} for p, s in ask_levels],
    )


def test_ob_balanced_is_half():
    bids, asks = _book([(0.50, 100)], [(0.50, 100)])
    assert ob_imbalance(bids, asks) == pytest.approx(0.5)


def test_ob_bid_heavy_gt_half():
    bids, asks = _book([(0.50, 1000)], [(0.50, 100)])
    assert ob_imbalance(bids, asks) > 0.5


def test_ob_ask_heavy_lt_half():
    bids, asks = _book([(0.50, 100)], [(0.50, 1000)])
    assert ob_imbalance(bids, asks) < 0.5


def test_ob_robust_to_ordering():
    # bids given low->high, asks given high->low; must still pick best levels
    bids, asks = _book([(0.40, 10), (0.49, 10)], [(0.60, 10), (0.51, 10)])
    v = ob_imbalance(bids, asks, levels=1)
    # best bid 0.49*10=4.9 vs best ask 0.51*10=5.1 -> slightly < 0.5
    assert v == pytest.approx(4.9 / (4.9 + 5.1))


def test_ob_empty_side_is_none():
    assert ob_imbalance([], [{"price": 0.5, "size": 10}]) is None
    assert ob_imbalance([{"price": 0.5, "size": 10}], []) is None


def test_ob_bad_input_is_none():
    assert ob_imbalance(None, None) is None
    assert ob_imbalance("x", "y") is None
    assert ob_imbalance([{"price": "junk", "size": "junk"}], [{"price": 0.5, "size": 1}]) is None


# --- trade_flow_ratio -------------------------------------------------------

def test_flow_all_bullish():
    trades = [
        {"side": "BUY", "size": 10, "outcome": "Up"},
        {"side": "SELL", "size": 5, "outcome": "Down"},  # also bullish
    ]
    assert trade_flow_ratio(trades) == pytest.approx(1.0)


def test_flow_all_bearish():
    trades = [
        {"side": "SELL", "size": 10, "outcome": "Up"},
        {"side": "BUY", "size": 5, "outcome": "Down"},
    ]
    assert trade_flow_ratio(trades) == pytest.approx(-1.0)


def test_flow_net_zero():
    trades = [
        {"side": "BUY", "size": 10, "outcome": "Up"},
        {"side": "SELL", "size": 10, "outcome": "Up"},
    ]
    assert trade_flow_ratio(trades) == pytest.approx(0.0)


def test_flow_empty_or_bad_is_none():
    assert trade_flow_ratio([]) is None
    assert trade_flow_ratio(None) is None
    assert trade_flow_ratio([{"side": "BUY", "size": 0, "outcome": "Up"}]) is None
    assert trade_flow_ratio([{"junk": 1}]) is None
