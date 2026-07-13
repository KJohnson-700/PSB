"""Unit tests for shared crypto up/down exit parsing and adverse-stop helpers."""

from pathlib import Path

import pytest
import yaml

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
    assert symbol_to_strategy_name("doge") == "doge_macro"
    assert symbol_to_strategy_name("bnb") == "bnb_macro"


def test_crypto_updown_strategy_set_includes_all_enabled_macro_assets():
    from src.execution.updown_exit_shared import CRYPTO_UPDOWN_STRATEGIES

    assert {
        "bitcoin",
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "doge_macro",
        "bnb_macro",
    }.issubset(CRYPTO_UPDOWN_STRATEGIES)


def test_parse_hold_winners_to_resolution_policy():
    g = parse_updown_exit_globals({"updown_hold_winners_to_resolution": True})

    assert g.updown_hold_winners_to_resolution is True


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


def test_positive_trailing_floor_locks_gains():
    # Below arm: trailing inactive, base stop in force.
    assert (
        effective_updown_stop_loss_pct(
            0.17, 0.05, peak_pnl_pct=0.05,
            in_profit_trigger_pct=0.0, tighten_to_pct=0.0,
            trail_arm_pct=0.10, trail_gap_pct=0.15,
        )
        == 0.17
    )
    # Peaked +30%, gap 15% -> floor at +15%, returned as negative magnitude so
    # caller's `pnl <= -mag` fires at +0.15 (banks gains, not just caps loss).
    assert (
        effective_updown_stop_loss_pct(
            0.17, 0.20, peak_pnl_pct=0.30,
            in_profit_trigger_pct=0.0, tighten_to_pct=0.0,
            trail_arm_pct=0.10, trail_gap_pct=0.15,
        )
        == pytest.approx(-0.15)
    )
    # Trailing floor is only ever MORE protective than the from-entry stop:
    # early in the run (peak just above arm) it tightens toward entry, never
    # wider than the base stop.
    assert (
        effective_updown_stop_loss_pct(
            0.17, 0.08, peak_pnl_pct=0.10,
            in_profit_trigger_pct=0.0, tighten_to_pct=0.0,
            trail_arm_pct=0.10, trail_gap_pct=0.15,
        )
        == pytest.approx(0.05)
    )


def test_trailing_floor_defaults_off_preserve_legacy():
    # No trail args supplied -> identical to legacy behavior.
    assert (
        effective_updown_stop_loss_pct(
            0.17, 0.40, peak_pnl_pct=0.40,
            in_profit_trigger_pct=0.0, tighten_to_pct=0.0,
        )
        == 0.17
    )


def test_zero_green_stop_knobs_preserve_base_stop_floor():
    assert (
        effective_updown_stop_loss_pct(
            0.30,
            -0.50,
            peak_pnl_pct=0.25,
            in_profit_trigger_pct=0.0,
            tighten_to_pct=0.0,
            trail_arm_pct=0.0,
            trail_gap_pct=0.0,
        )
        == pytest.approx(0.30)
    )


def test_settings_keep_global_green_stop_protection_enabled():
    cfg_path = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    exit_rules = cfg["trading"]["exit_rules"]

    assert exit_rules["updown_in_profit_stop_trigger_pct"] == pytest.approx(0.05)
    assert exit_rules["updown_in_profit_stop_tighten_to_pct"] == pytest.approx(0.07)
    assert exit_rules["updown_trail_arm_pct"] == pytest.approx(0.06)
    assert exit_rules["updown_trail_gap_pct"] == pytest.approx(0.05)


def test_settings_exempt_btc_1h_up_from_green_stop_banking():
    cfg_path = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    exit_rules = cfg["trading"]["exit_rules"]
    resolved = resolve_updown_exit_params_for_position(
        parse_updown_exit_globals(exit_rules),
        strategy_name="bitcoin",
        window_size="1h",
        entry_leg="YES",
        outcome="YES",
    )

    assert resolved.updown_hold_winners_to_resolution is True
    assert resolved.updown_in_profit_stop_trigger_pct == pytest.approx(0.0)
    assert resolved.updown_in_profit_stop_tighten_to_pct == pytest.approx(0.0)
    assert resolved.updown_trail_arm_pct == pytest.approx(0.0)
    assert resolved.updown_trail_gap_pct == pytest.approx(0.0)


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


def test_effective_stop_loss_applies_range_regime_multiplier():
    assert (
        effective_updown_stop_loss_pct(
            0.20,
            0.00,
            in_profit_trigger_pct=0.05,
            tighten_to_pct=0.08,
            dynamic_stop_enabled=True,
            btc_1h_regime="RANGE",
            entry_volatility=0.01,
            convergence_score=0.65,
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
        == pytest.approx(0.20 * 1.05)
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


def _rce_globals(**rce):
    base = {
        "updown_stop_loss_pct": 0.20,
        "take_profit_pct": 0.15,
        # A lane statically set to tight (hold off) so we can prove the conditioner
        # FLIPS it to hold when trend-side, and leaves it tight otherwise.
        "updown_hold_winners_to_resolution": False,
        "updown_trail_arm_pct": 0.0,
        "updown_trail_gap_pct": 0.0,
        "regime_conditioned_exits": {"enabled": True, **rce},
    }
    return parse_updown_exit_globals(base)


def _resolve(g, *, lane_side, regime):
    # lane_side: "up" -> LONG (YES/YES); "down" -> SHORT (NO/NO)
    leg, out = ("YES", "YES") if lane_side == "up" else ("NO", "NO")
    return resolve_updown_exit_params_for_position(
        g, strategy_name="sol_macro", window_size="5m",
        entry_leg=leg, outcome=out, btc_1h_regime=regime,
    )


def test_regime_conditioned_exits_default_off_is_byte_identical():
    # No regime block at all -> conditioner must never touch hold/trail.
    g = parse_updown_exit_globals(
        {"updown_hold_winners_to_resolution": True,
         "updown_trail_arm_pct": 0.1, "updown_trail_gap_pct": 0.15}
    )
    p = resolve_updown_exit_params_for_position(
        g, strategy_name="sol_macro", window_size="5m",
        entry_leg="YES", outcome="YES", btc_1h_regime="BEAR",  # counter-trend
    )
    # Static config wins because the feature is off.
    assert p.updown_hold_winners_to_resolution is True
    assert p.updown_trail_arm_pct == pytest.approx(0.1)


def test_regime_conditioned_long_in_bull_holds_and_trails():
    g = _rce_globals(trend_side_trail_arm_pct=0.10, trend_side_trail_gap_pct=0.15)
    p = _resolve(g, lane_side="up", regime="BULL")  # GREEN/LONG
    assert p.updown_hold_winners_to_resolution is True
    assert p.updown_trail_arm_pct == pytest.approx(0.10)
    assert p.updown_trail_gap_pct == pytest.approx(0.15)


def test_regime_conditioned_short_in_bear_holds_and_trails():
    g = _rce_globals()
    p = _resolve(g, lane_side="down", regime="BEAR")  # RED/SHORT
    assert p.updown_hold_winners_to_resolution is True
    assert p.updown_trail_arm_pct == pytest.approx(0.10)


@pytest.mark.parametrize(
    "lane_side,regime",
    [
        ("up", "BEAR"),    # RED/LONG  — counter-trend
        ("up", "RANGE"),   # FLAT/LONG — chop
        ("down", "BULL"),  # GREEN/SHORT — counter-trend
        ("down", "RANGE"), # FLAT/SHORT — chop
        ("up", None),      # unknown regime -> off-trend
        ("up", ""),        # empty regime -> off-trend
    ],
)
def test_regime_conditioned_off_trend_forces_tight(lane_side, regime):
    g = _rce_globals()
    p = _resolve(g, lane_side=lane_side, regime=regime)
    assert p.updown_hold_winners_to_resolution is False
    assert p.updown_trail_arm_pct == pytest.approx(0.0)
    assert p.updown_trail_gap_pct == pytest.approx(0.0)


def test_regime_conditioned_off_trend_can_be_left_alone():
    # off_trend_force_tight=False -> conditioner only adds hold on trend-side,
    # never strips a statically-configured hold off-trend.
    g = parse_updown_exit_globals(
        {"updown_hold_winners_to_resolution": True,
         "updown_trail_arm_pct": 0.1, "updown_trail_gap_pct": 0.15,
         "regime_conditioned_exits": {"enabled": True, "off_trend_force_tight": False}}
    )
    p = resolve_updown_exit_params_for_position(
        g, strategy_name="sol_macro", window_size="5m",
        entry_leg="NO", outcome="NO", btc_1h_regime="BULL",  # counter-trend short
    )
    assert p.updown_hold_winners_to_resolution is True  # untouched
    assert p.updown_trail_arm_pct == pytest.approx(0.1)


def test_regime_conditioned_exclude_lane_keeps_take_profit():
    # Excluded strategy must keep its static (tight/take-profit) config even in a
    # trend-side regime that would otherwise force hold+trail.
    base = {
        "updown_hold_winners_to_resolution": False,
        "updown_trail_arm_pct": 0.0,
        "updown_trail_gap_pct": 0.0,
        "regime_conditioned_exits": {"enabled": True, "exclude_lanes": ["bitcoin"]},
    }
    g = parse_updown_exit_globals(base)
    # bitcoin LONG in BULL would be trend-side, but it's excluded -> stays tight.
    p = resolve_updown_exit_params_for_position(
        g, strategy_name="bitcoin", window_size="1h",
        entry_leg="YES", outcome="YES", btc_1h_regime="BULL",
    )
    assert p.updown_hold_winners_to_resolution is False
    assert p.updown_trail_arm_pct == 0.0
    # A non-excluded strategy still gets the trend-side hold.
    p2 = resolve_updown_exit_params_for_position(
        g, strategy_name="sol_macro", window_size="1h",
        entry_leg="YES", outcome="YES", btc_1h_regime="BULL",
    )
    assert p2.updown_hold_winners_to_resolution is True


def test_regime_conditioned_exclude_supports_strategy_window():
    g = parse_updown_exit_globals({
        "regime_conditioned_exits": {"enabled": True, "exclude_lanes": ["bitcoin|1h"]},
    })
    # 1h excluded -> tight; 15m NOT excluded -> trend-side hold still applies.
    p1h = resolve_updown_exit_params_for_position(
        g, strategy_name="bitcoin", window_size="1h",
        entry_leg="YES", outcome="YES", btc_1h_regime="BULL",
    )
    p15 = resolve_updown_exit_params_for_position(
        g, strategy_name="bitcoin", window_size="15m",
        entry_leg="YES", outcome="YES", btc_1h_regime="BULL",
    )
    assert p1h.updown_hold_winners_to_resolution is False
    assert p15.updown_hold_winners_to_resolution is True


def test_infer_window_size_from_runway_for_legacy_positions():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    assert infer_updown_window_size("", opened_at=now, end_date=now + timedelta(minutes=5)) == "5m"
    assert infer_updown_window_size("", opened_at=now, end_date=now + timedelta(minutes=15)) == "15m"
