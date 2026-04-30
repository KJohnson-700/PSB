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
        "outside_entry_window",
        "btc_min_move_dollars",
        "price_too_far_from_even",
        "histogram_1h_blocks_long_5m",
        "histogram_1h_blocks_short_5m",
        "histogram_1h_blocks_long_15m",
        "histogram_1h_blocks_short_15m",
        "low_corr_suppressed",
        "edge_below_min",
        "entry_price_band_updown",
        "edge_above_cap",
        "size_too_small",
    )
    for reason in required_skip_reasons:
        assert f'_bump_skip("{reason}")' in source


def test_updown_entry_band_uses_explicit_min_and_max() -> None:
    source = SOL_MACRO.read_text(encoding="utf-8")
    assert "_yp_low = self.entry_price_min" in source
    assert "_yp_high = self.entry_price_max" in source
    assert "_yp_low  = 1.0 - self.entry_price_max" not in source
