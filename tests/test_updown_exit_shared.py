"""Unit tests for shared crypto up/down exit parsing and adverse-stop helpers."""

import pytest

from src.execution.updown_exit_shared import (
    adverse_for_updown_cents_time_stop,
    cents_stop_for_entry_price,
    effective_updown_stop_loss_pct,
    infer_updown_window_size,
    parse_updown_exit_globals,
    resolve_updown_exit_params_for_position,
    resolve_updown_lane,
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


def test_parse_includes_dynamic_stop_policy():
    g = parse_updown_exit_globals(
        {
            "dynamic_stop_enabled": True,
            "dynamic_stop_bull_mult": 0.95,
            "dynamic_stop_range_mult": 1.05,
            "dynamic_stop_bear_mult": 1.15,
            "dynamic_stop_high_vol_mult": 1.15,
            "dynamic_stop_volatility_threshold": 0.02,
            "dynamic_stop_low_convergence_mult": 1.10,
            "dynamic_stop_high_convergence_mult": 0.95,
            "dynamic_stop_low_convergence_threshold": 0.55,
            "dynamic_stop_high_convergence_threshold": 0.75,
        }
    )

    assert g.dynamic_stop_enabled is True
    assert g.dynamic_stop_bear_mult == pytest.approx(1.15)
    assert g.dynamic_stop_high_vol_mult == pytest.approx(1.15)
    assert g.dynamic_stop_low_convergence_threshold == pytest.approx(0.55)


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


def test_effective_stop_loss_applies_dynamic_regime_volatility_and_convergence():
    assert (
        effective_updown_stop_loss_pct(
            0.20,
            0.00,
            in_profit_trigger_pct=0.05,
            tighten_to_pct=0.08,
            dynamic_stop_enabled=True,
            btc_1h_regime="BEAR",
            entry_volatility=0.03,
            convergence_score=0.40,
            dynamic_stop_bull_mult=0.95,
            dynamic_stop_range_mult=1.05,
            dynamic_stop_bear_mult=1.15,
            dynamic_stop_high_vol_mult=1.15,
            dynamic_stop_volatility_threshold=0.02,
            dynamic_stop_low_convergence_mult=1.10,
            dynamic_stop_high_convergence_mult=0.95,
            dynamic_stop_low_convergence_threshold=0.55,
            dynamic_stop_high_convergence_threshold=0.75,
        )
        == pytest.approx(0.20 * 1.15 * 1.15 * 1.10)
    )


def test_effective_stop_loss_tightens_for_bull_high_convergence():
    assert (
        effective_updown_stop_loss_pct(
            0.20,
            0.00,
            in_profit_trigger_pct=0.05,
            tighten_to_pct=0.08,
            dynamic_stop_enabled=True,
            btc_1h_regime="BULL",
            entry_volatility=0.01,
            convergence_score=0.80,
            dynamic_stop_bull_mult=0.95,
            dynamic_stop_range_mult=1.05,
            dynamic_stop_bear_mult=1.15,
            dynamic_stop_high_vol_mult=1.15,
            dynamic_stop_volatility_threshold=0.02,
            dynamic_stop_low_convergence_mult=1.10,
            dynamic_stop_high_convergence_mult=0.95,
            dynamic_stop_low_convergence_threshold=0.55,
            dynamic_stop_high_convergence_threshold=0.75,
        )
        == pytest.approx(0.20 * 0.95 * 0.95)
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


def test_resolve_updown_lane_maps_buy_no_and_legacy_short_yes_to_down():
    assert resolve_updown_lane(entry_leg="NO", outcome="NO") == "down"
    assert resolve_updown_lane(entry_leg="YES", outcome="NO") == "down"
    assert resolve_updown_lane(entry_leg="YES", outcome="YES") == "up"


def test_window_lane_override_precedence_beats_strategy_lane_and_global_lane():
    g = parse_updown_exit_globals(
        {
            "updown_stop_loss_pct": 0.20,
            "take_profit_pct": 0.15,
            "updown_lane_overrides": {
                "down": {"updown_stop_loss_pct": 0.19, "take_profit_pct": 0.18},
            },
            "updown_overrides": {
                "eth_macro": {
                    "updown_stop_loss_pct": 0.18,
                    "take_profit_pct": 0.20,
                    "lane_overrides": {
                        "down": {"updown_stop_loss_pct": 0.17, "take_profit_pct": 0.22},
                    },
                    "window_lane_overrides": {
                        "5m": {
                            "down": {"updown_stop_loss_pct": 0.14, "take_profit_pct": 0.25},
                        }
                    },
                }
            },
        }
    )
    params = resolve_updown_exit_params_for_position(
        g,
        strategy_name="eth_macro",
        window_size="5m",
        entry_leg="NO",
        outcome="NO",
    )
    assert params.updown_stop_loss_pct == pytest.approx(0.14)
    assert params.take_profit_pct == pytest.approx(0.25)


def test_strategy_level_override_used_when_no_lane_specific_override():
    g = parse_updown_exit_globals(
        {
            "updown_stop_cents": 0.03,
            "updown_overrides": {
                "eth_macro": {"updown_stop_cents": 0.02},
            },
        }
    )
    params = resolve_updown_exit_params_for_position(
        g,
        strategy_name="eth_macro",
        window_size="15m",
        entry_leg="YES",
        outcome="YES",
    )
    assert params.updown_stop_cents == pytest.approx(0.02)


def test_infer_window_size_from_runway_for_legacy_positions():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    assert infer_updown_window_size("", opened_at=now, end_date=now + timedelta(minutes=5)) == "5m"
    assert infer_updown_window_size("", opened_at=now, end_date=now + timedelta(minutes=15)) == "15m"
