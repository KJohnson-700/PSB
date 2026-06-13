"""Order-book fill simulation (src/execution/fill_sim.py)."""

from __future__ import annotations

import pytest

from src.execution.fill_sim import simulate_book_fill


def test_sell_fills_at_top_bid_when_depth_sufficient():
    # 10 units, best bid 0.42 has 50 -> all fill at 0.42.
    px, filled = simulate_book_fill("SELL", 10.0, [(0.42, 50.0), (0.40, 50.0)])
    assert filled == 10.0
    assert px == pytest.approx(0.42)


def test_sell_walks_down_the_ladder_vwap():
    # 30 units across 0.42(x10), 0.40(x10), 0.38(x10) -> VWAP 0.40.
    px, filled = simulate_book_fill(
        "SELL", 30.0, [(0.42, 10.0), (0.40, 10.0), (0.38, 10.0)]
    )
    assert filled == 30.0
    assert px == pytest.approx((0.42 + 0.40 + 0.38) / 3)


def test_buy_walks_up_the_asks():
    px, filled = simulate_book_fill("BUY", 20.0, [(0.55, 10.0), (0.57, 10.0)])
    assert filled == 20.0
    assert px == pytest.approx(0.56)


def test_partial_when_ladder_exhausted():
    px, filled = simulate_book_fill("SELL", 100.0, [(0.42, 10.0), (0.40, 10.0)])
    assert filled == 20.0  # only 20 available
    assert px == pytest.approx(0.41)


def test_pad_remainder_fills_full_size_at_worst_level():
    # 100 wanted, 20 available across 0.42/0.40; remainder 80 padded at 0.40.
    px, filled = simulate_book_fill(
        "SELL", 100.0, [(0.42, 10.0), (0.40, 10.0)], pad_remainder_at_worst=True
    )
    assert filled == 100.0
    # (10*0.42 + 10*0.40 + 80*0.40) / 100
    assert px == pytest.approx((10 * 0.42 + 90 * 0.40) / 100)


def test_passive_limit_only_fills_acceptable_levels():
    # Passive SELL limit at 0.41: the 0.40 level is below the limit -> not taken.
    px, filled = simulate_book_fill(
        "SELL", 30.0, [(0.42, 10.0), (0.40, 10.0)], marketable=False, limit_price=0.41
    )
    assert filled == 10.0
    assert px == pytest.approx(0.42)


def test_nothing_fills_returns_limit_or_best():
    px, filled = simulate_book_fill(
        "SELL", 10.0, [(0.30, 10.0)], marketable=False, limit_price=0.41
    )
    assert filled == 0.0
    assert px == pytest.approx(0.41)  # fallback to the limit when nothing acceptable


def test_ignores_nonpositive_levels():
    px, filled = simulate_book_fill("SELL", 5.0, [(0.0, 10.0), (0.42, 10.0), (0.40, -3.0)])
    assert filled == 5.0
    assert px == pytest.approx(0.42)
