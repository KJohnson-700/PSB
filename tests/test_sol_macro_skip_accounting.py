"""Regression checks for SOL-family market-loop skip diagnostics."""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SOL_MACRO = REPO / "src" / "strategies" / "sol_macro.py"


def test_updown_market_loop_early_continues_are_counted() -> None:
    source = SOL_MACRO.read_text(encoding="utf-8")
    required_skip_reasons = (
        "liquidity",
        "missing_end_date",
        "lane_entry_window",
        "btc_min_move_dollars",
        "price_too_far_from_even",
        "histogram_1h_blocks_long_5m",
        "histogram_1h_blocks_short_5m",
        "histogram_1h_blocks_long_15m",
        "histogram_1h_blocks_short_15m",
        "low_corr_suppressed",
        "lane_min_edge",
        "lane_price_band",
        "edge_above_cap",
        "lane_size_too_small",
    )
    for reason in required_skip_reasons:
        assert f'_bump_skip("{reason}")' in source


def test_updown_entry_band_uses_explicit_min_and_max() -> None:
    source = SOL_MACRO.read_text(encoding="utf-8")
    assert "_yp_low = lane_policy.entry_price_min" in source
    assert "_yp_high = lane_policy.entry_price_max" in source
    assert "_yp_low  = 1.0 - self.entry_price_max" not in source


def test_buy_no_is_not_hard_suppressed_by_bullish_alt_1h() -> None:
    source = SOL_MACRO.read_text(encoding="utf-8")
    assert '_bump_skip("sell_yes_suppressed_bullish_1h")' not in source
    assert "buy_no_against_alt_1h_bullish" in source


def test_buy_yes_is_still_hard_suppressed_against_bearish_alt_1h() -> None:
    source = SOL_MACRO.read_text(encoding="utf-8")
    assert '_bump_skip("buy_yes_suppressed_bearish_1h")' in source
