"""Unit tests for shared crypto up/down exit parsing and adverse-stop helpers."""

import pytest

from src.execution.updown_exit_shared import (
    adverse_for_updown_cents_time_stop,
    cents_stop_for_entry_price,
    effective_updown_stop_loss_pct,
    parse_updown_exit_globals,
    resolve_updown_exit_params,
    scaled_exit_window_mins,
    symbol_to_strategy_name,
)


def test_symbol_to_strategy_name_maps_hype():
    assert symbol_to_strategy_name("hype") == "hype_macro"


def test_parse_includes_exit_window_max_fraction_in_overrides():
    g = parse_updown_exit_globals(
        {
            "updown_exit_window_mins": 2.25,
            "updown_exit_window_max_fraction": 0.4,
            "updown_overrides": {
                "bitcoin": {"updown_exit_window_max_fraction": 0.9},
            },
        }
    )
    btc = resolve_updown_exit_params(g, "bitcoin")
    assert btc[3] == pytest.approx(0.9)
    sol = resolve_updown_exit_params(g, "sol_macro")
    assert sol[3] == pytest.approx(0.4)


def test_scaled_exit_window_caps_late_entry():
    assert scaled_exit_window_mins(2.25, 0.5, 3.0) == pytest.approx(1.5)
    assert scaled_exit_window_mins(2.25, 1.0, 3.0) == pytest.approx(2.25)


def test_cents_stop_tightens_for_high_entry():
    assert cents_stop_for_entry_price(0.03, 0.65, high_threshold=0.60, high_stop_cents=0.02) == 0.02
    assert cents_stop_for_entry_price(0.03, 0.50, high_threshold=0.60, high_stop_cents=0.02) == 0.03


def test_effective_stop_loss_tightens_in_profit():
    assert (
        effective_updown_stop_loss_pct(
            0.20, 0.04, in_profit_trigger_pct=0.05, tighten_to_pct=0.08
        )
        == 0.20
    )
    assert (
        effective_updown_stop_loss_pct(
            0.20, 0.06, in_profit_trigger_pct=0.05, tighten_to_pct=0.08
        )
        == 0.08
    )


def test_adverse_time_stop_long_yes_and_no():
    assert adverse_for_updown_cents_time_stop(
        entry_leg="YES",
        outcome="YES",
        current_yes=0.46,
        current_no=0.54,
        entry_price=0.50,
        up_stop_cents=0.03,
    )
    assert not adverse_for_updown_cents_time_stop(
        entry_leg="YES",
        outcome="YES",
        current_yes=0.48,
        current_no=0.52,
        entry_price=0.50,
        up_stop_cents=0.03,
    )
    assert adverse_for_updown_cents_time_stop(
        entry_leg="NO",
        outcome="YES",
        current_yes=0.60,
        current_no=0.38,
        entry_price=0.42,
        up_stop_cents=0.03,
    )
    assert adverse_for_updown_cents_time_stop(
        entry_leg="YES",
        outcome="NO",
        current_yes=0.55,
        current_no=0.45,
        entry_price=0.50,
        up_stop_cents=0.03,
    )
