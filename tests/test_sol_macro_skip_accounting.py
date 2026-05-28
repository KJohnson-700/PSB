"""Regression checks for SOL-family market-loop skip diagnostics."""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SOL_MACRO = REPO / "src" / "strategies" / "sol_macro.py"
ETH_MACRO = REPO / "src" / "strategies" / "eth_macro.py"


def test_updown_market_loop_early_continues_are_counted() -> None:
    """Verify the sol_macro market loop still accounts for each early-continue path.

    Note: list pruned 2026-05-28 — `lane_entry_window`, `btc_min_move_dollars`,
    `lane_min_edge`, `lane_price_band`, `lane_size_too_small` were removed by the
    BTC-decouple refactor and the horizon-coherent bias rewrite. The remaining
    reasons are the ones still wired in sol_macro.py and that the live skip
    accounting depends on.
    """
    source = SOL_MACRO.read_text(encoding="utf-8")
    required_skip_reasons = (
        "liquidity",
        "missing_end_date",
        "price_too_far_from_even",
        "low_corr_suppressed",
        "neutral_bias",
    )
    for reason in required_skip_reasons:
        assert f'_bump_skip("{reason}")' in source, f"skip reason {reason!r} not wired in sol_macro.py"


def test_updown_entry_band_uses_explicit_min_and_max() -> None:
    source = SOL_MACRO.read_text(encoding="utf-8")
    assert "_yp_low = lane_policy.entry_price_min" in source
    assert "_yp_high = lane_policy.entry_price_max" in source
    assert "_yp_low  = 1.0 - self.entry_price_max" not in source


def test_buy_no_is_not_hard_suppressed_by_bullish_alt_1h() -> None:
    source = SOL_MACRO.read_text(encoding="utf-8")
    assert '_bump_skip("sell_yes_suppressed_bullish_1h")' not in source
    assert "buy_no_against_alt_1h_bullish" in source


def test_alt_1h_alignment_is_diagnostic_only_in_sol_macro() -> None:
    source = SOL_MACRO.read_text(encoding="utf-8")
    assert '_bump_skip("buy_yes_suppressed_bearish_1h")' not in source
    assert '_bump_skip("histogram_1h_blocks_long_5m")' not in source
    assert '_bump_skip("histogram_1h_blocks_short_5m")' not in source
    assert '_bump_skip("histogram_1h_blocks_long_15m")' not in source
    assert '_bump_skip("histogram_1h_blocks_short_15m")' not in source
    assert "buy_yes_against_alt_1h_bearish" in source


def test_alt_1h_alignment_is_diagnostic_only_in_eth_macro() -> None:
    source = ETH_MACRO.read_text(encoding="utf-8")
    assert '_bump_skip("eth_1h_bearish")' not in source
    assert "buy_yes_against_alt_1h_bearish" in source
