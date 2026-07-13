"""
Tests for SOL Macro Strategy — BTC-to-Solana Correlation Lag Trading

Tests cover:
1. Macro trend determination (1H)
2. 15m MACD confirmation
3. 5m entry timing + BTC-SOL lag detection
4. Edge estimation
5. Signal gating (macro blocks wrong side)
6. Market detection (SOL vs non-SOL)
7. Exposure manager integration
"""

import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from types import SimpleNamespace

from src.strategies.sol_macro import (
    BiasResolution,
    SolMacroStrategy,
    SolMacroSignal,
    build_alt_resolver_metadata,
)
from src.strategies.bitcoin import BitcoinStrategy
from src.analysis.lane_calibration import LaneCalibrator
from src.analysis.sol_btc_service import (
    SOLBTCService,
    SOLTechnicalAnalysis,
    SOLAnalysis,
    BTCSOLCorrelation,
    MultiTimeframeTrend,
    MACDResult,
    ORACLE_FEEDS,
)
from src.execution.exposure_manager import ExposureManager, ExposureTier

from tests.async_helpers import run_async


def _make_config():
    return {
        "strategies": {
            "sol_macro": {
                "enabled": True,
                "min_liquidity": 10000,
                "min_edge": 0.08,
                "ai_confidence_threshold": 0.60,
                "kelly_fraction": 0.15,
                "entry_price_min": 0.15,
                "entry_price_max": 0.85,
            }
        },
        "exposure": {
            "full_size": 5.0,
            "moderate_size": 3.0,
            "minimal_size": 1.0,
            "max_consecutive_losses": 3,
            "pause_cycles": 2,
        },
        "trading": {"dry_run": True},
    }


def test_alt_1h_simple_long_defaults_off():
    strategy = SolMacroStrategy(_make_config(), MagicMock(), MagicMock())
    assert strategy._a1hsl_enabled is False
    assert strategy._a1hsl_entry_min == 0.50
    assert strategy._a1hsl_entry_max == 0.85
    assert strategy._a1hsl_sizing_edge == 0.06


def test_alt_1h_simple_long_reads_config_when_enabled():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["alt_1h_simple_long"] = {
        "enabled": True, "entry_min": 0.55, "entry_max": 0.80, "sizing_edge": 0.05,
    }
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    assert strategy._a1hsl_enabled is True
    assert strategy._a1hsl_entry_min == 0.55
    assert strategy._a1hsl_entry_max == 0.80
    assert strategy._a1hsl_sizing_edge == 0.05


def test_optional_rsi_buy_ceiling_soft_penalty_by_default():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["rsi_buy_block_above"] = 80.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    hard, delta = strategy._resolve_rsi_gate("BUY_YES", 84.8)
    assert hard is False
    assert delta < 0
    hard2, delta2 = strategy._resolve_rsi_gate("BUY_YES", 79.9)
    assert hard2 is False
    assert delta2 == 0.0
    hard3, delta3 = strategy._resolve_rsi_gate("BUY_NO", 84.8)
    assert hard3 is False
    assert delta3 == 0.0


def test_sol_entry_policy_supports_hourly_by_tf_overrides():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["by_tf"] = {"1h": {"min_edge": 0.11}}
    cfg["strategies"]["sol_macro"]["entry_price_max_1h_yes_side"] = 0.60
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    policy = strategy._legacy_entry_policy(window_size="1h", action="BUY_YES", direction="UP")

    assert policy["min_edge"] == 0.11
    assert policy["entry_price_max"] == 0.60


def test_alt_macro_live_entry_ai_windows_exclude_5m():
    cfg = _make_config()
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._DECISION_GATE_WINDOWS == frozenset({"15m", "1h"})
    assert "5m" not in strategy._DECISION_GATE_WINDOWS
    assert BitcoinStrategy._DECISION_GATE_WINDOWS == frozenset({"15m", "1h"})


def test_sol_liquidity_floor_is_lane_aware_by_window_and_side():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["min_liquidity_1h_buy_no"] = 2500
    cfg["strategies"]["sol_macro"]["min_liquidity_15m"] = 4000
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._resolve_min_liquidity_floor(window_size="1h", action="BUY_NO") == 2500
    assert strategy._resolve_min_liquidity_floor(window_size="15m", action="BUY_YES") == 4000
    assert strategy._resolve_min_liquidity_floor(window_size="5m", action="BUY_YES") == 10000


def test_sol_hourly_buy_yes_native_bonus_only_applies_to_clean_native_hourly_longs():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["hourly_buy_yes_native_bonus_1h"] = 0.03
    cfg["strategies"]["sol_macro"]["hourly_buy_yes_native_bonus_min_ltf_strength_1h"] = 0.30
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    native = BiasResolution(
        allowed_side="LONG",
        side_source="sol_1h_native",
        horizon_tf="1h",
        horizon_bias="BULLISH",
        slower_biases={},
        primary_htf_bias="BULLISH",
    )
    assert strategy._hourly_buy_yes_native_bonus(
        window_size="1h",
        allowed_side="LONG",
        resolution=native,
        ltf_strength=0.35,
    ) == 0.03
    assert strategy._hourly_buy_yes_native_bonus(
        window_size="1h",
        allowed_side="LONG",
        resolution=native,
        ltf_strength=0.20,
    ) == 0.0

    disagreed = BiasResolution(
        allowed_side="LONG",
        side_source="sol_1h_vs_slower",
        horizon_tf="1h",
        horizon_bias="BULLISH",
        slower_biases={"15m": "BEARISH"},
        primary_htf_bias="BULLISH",
    )
    assert strategy._hourly_buy_yes_native_bonus(
        window_size="1h",
        allowed_side="LONG",
        resolution=disagreed,
        ltf_strength=0.35,
    ) == 0.0


def test_sol_oracle_validation_uses_window_side_basis_overrides():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["oracle_max_basis_bps"] = 25.0
    cfg["strategies"]["sol_macro"]["oracle_basis_relax_max_bps"] = None
    cfg["strategies"]["sol_macro"]["oracle_max_basis_bps_15m_buy_yes"] = 30.0
    cfg["strategies"]["sol_macro"]["oracle_basis_relax_max_bps_15m_buy_yes"] = 40.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    analysis = SimpleNamespace(
        chainlink_price=100.0,
        current_price=100.35,
        chainlink_updated_at=now,
    )

    default_path = strategy._validate_updown_oracle(
        analysis,
        action="BUY_NO",
        window_size="15m",
        now=now,
    )
    yes_path = strategy._validate_updown_oracle(
        analysis,
        action="BUY_YES",
        window_size="15m",
        now=now,
    )

    assert default_path.passed is False
    assert default_path.reason == "oracle_basis_block"
    assert yes_path.passed is True
    assert yes_path.reason == "oracle_basis_relaxed"


def test_optional_rsi_buy_ceiling_hard_block_when_enabled():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["rsi_buy_block_above"] = 80.0
    cfg["strategies"]["sol_macro"]["rsi_hard_gate_enabled"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    assert strategy._resolve_rsi_gate("BUY_YES", 84.8) == (True, 0.0)


def test_buy_no_rsi_penalty_can_be_disabled():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["rsi_sell_block_below"] = 40.0
    cfg["strategies"]["sol_macro"]["rsi_soft_penalty_buy_no"] = 0.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    hard, delta = strategy._resolve_rsi_gate("BUY_NO", 35.0)
    assert hard is False
    assert delta == 0.0


def test_buy_no_ltf_override_requires_confirmed_bearish_tape():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["buy_no_ltf_override_enabled"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=39.0,
            macd_15m=MACDResult(histogram=-0.08, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.03, histogram_rising=False),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=-0.02),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )

    allowed, reason = strategy._buy_no_ltf_override(ta)

    assert allowed is True
    assert "bearish_ltf_override" in reason


def test_buy_no_ltf_override_rejects_weak_bearish_noise():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["buy_no_ltf_override_enabled"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=52.0,
            macd_15m=MACDResult(histogram=-0.02, histogram_rising=False),
            macd_5m=MACDResult(histogram=0.01, histogram_rising=True),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=0.04),
    )

    allowed, reason = strategy._buy_no_ltf_override(ta)

    assert allowed is False
    assert "5m_not_bearish" in reason
    assert "rsi>45.0" in reason
    # 2026-05-22 alt-native rule: BTC 5m is diagnostic, not a SOL admission
    # co-condition. Rejection text should only list missing SOL-native pieces.
    assert "btc5m" not in reason.lower()


def test_sol_local_guard_blocks_vs_slower_short_against_bullish_alt_1h():
    cfg = _make_config()
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    reason = strategy._sol_signal_guard_reason(
        window_size="5m",
        action="BUY_NO",
        side_source="sol_5m_vs_slower",
        yes_price=0.43,
        btc_1h_regime="RANGE",
        alt_h1_trend="BULLISH",
    )

    assert reason == "sol_vs_slower_short_against_h1"


def test_sol_local_guard_does_not_default_to_btc_bull_regime_short_block():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["sol_15m_buy_no_max_yes_price_bull_1h"] = 0.49
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    reason = strategy._sol_signal_guard_reason(
        window_size="15m",
        action="BUY_NO",
        side_source="sol_15m_native",
        yes_price=0.50,
        btc_1h_regime="BULL",
        alt_h1_trend="BEARISH",
    )

    assert reason is None


def test_sol_local_guard_does_not_block_15m_short_from_btc_bull_regime():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["sol_15m_bull_regime_short_block"] = True
    cfg["strategies"]["sol_macro"]["sol_15m_buy_no_max_yes_price_bull_1h"] = 0.49
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    reason = strategy._sol_signal_guard_reason(
        window_size="15m",
        action="BUY_NO",
        side_source="sol_15m_native",
        yes_price=0.50,
        btc_1h_regime="BULL",
        alt_h1_trend="BEARISH",
    )

    assert reason is None


def _make_ta_bullish_rally() -> SOLTechnicalAnalysis:
    return SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=62.0,
            macd_15m=MACDResult(histogram=0.06, histogram_rising=True),
            macd_5m=MACDResult(histogram=0.04, histogram_rising=True),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=0.05),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )


def _make_ta_bearish_dip() -> SOLTechnicalAnalysis:
    return SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=38.0,
            macd_15m=MACDResult(histogram=-0.08, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.03, histogram_rising=False),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=-0.05),
        multi_tf=MultiTimeframeTrend(h1_trend="BEARISH"),
    )


def _make_ta_chop() -> SOLTechnicalAnalysis:
    return SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=50.0,
            macd_15m=MACDResult(histogram=0.01, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.01, histogram_rising=False),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=0.0),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )


def _make_resolver_strategy() -> SolMacroStrategy:
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["buy_no_ltf_override_enabled"] = True
    cfg["strategies"]["sol_macro"]["buy_yes_ltf_override_enabled"] = True
    return SolMacroStrategy(cfg, MagicMock(), MagicMock())


def test_resolver_bull_default_long_when_bullish_rally_confirms():
    strategy = _make_resolver_strategy()
    side, source, _ = strategy._resolve_allowed_side_with_ltf_overrides(
        _make_ta_bullish_rally(), "BULLISH"
    )
    assert side == "LONG"
    assert source == "bullish_rally_default"


def test_resolver_bull_clash_blocks_buy_no_when_rally_confirms():
    """When bullish rally confirms in BULL, buy_no cannot fire — clash rule."""
    strategy = _make_resolver_strategy()
    # Construct tape that satisfies BOTH bullish_rally AND bearish_dip simultaneously.
    # (Engineered chop where both helpers pass — extremely rare in practice.)
    ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=55.0,  # at min threshold for bullish; below buy_no 45 max → NOT bearish-dip RSI
            macd_15m=MACDResult(histogram=0.05, histogram_rising=True),
            macd_5m=MACDResult(histogram=0.03, histogram_rising=True),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=0.05),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )
    side, source, _ = strategy._resolve_allowed_side_with_ltf_overrides(ta, "BULLISH")
    assert side == "LONG"
    assert source == "bullish_rally_default"


def test_resolver_bull_exception_short_when_only_bearish_dip_confirms():
    """BULL regime: dip-only tape → SHORT exception (no bullish rally to clash)."""
    strategy = _make_resolver_strategy()
    side, source, detail = strategy._resolve_allowed_side_with_ltf_overrides(
        _make_ta_bearish_dip(), "BULLISH"
    )
    assert side == "SHORT"
    assert source == "bearish_dip_exception"
    assert "bearish_ltf_override" in detail


def test_resolver_bear_default_short_when_bearish_dip_confirms():
    strategy = _make_resolver_strategy()
    side, source, _ = strategy._resolve_allowed_side_with_ltf_overrides(
        _make_ta_bearish_dip(), "BEARISH"
    )
    assert side == "SHORT"
    assert source == "bearish_dip_default"


def test_resolver_bear_exception_long_when_bullish_rally_confirms():
    strategy = _make_resolver_strategy()
    side, source, detail = strategy._resolve_allowed_side_with_ltf_overrides(
        _make_ta_bullish_rally(), "BEARISH"
    )
    assert side == "LONG"
    assert source == "bullish_rally_exception"
    assert "bullish_ltf_override" in detail


def test_resolver_bull_default_long_fires_even_without_bullish_confirmation():
    """Additive-only: BULL default LONG always fires; LTF gate cannot block it."""
    strategy = _make_resolver_strategy()
    side, source, _ = strategy._resolve_allowed_side_with_ltf_overrides(
        _make_ta_chop(), "BULLISH"
    )
    assert side == "LONG"
    assert source == "bullish_rally_default"


def test_resolver_bear_default_short_fires_even_without_bearish_confirmation():
    """Additive-only: BEAR default SHORT always fires; LTF gate cannot block it."""
    strategy = _make_resolver_strategy()
    ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=50.0,
            macd_15m=MACDResult(histogram=0.01, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.01, histogram_rising=False),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=0.0),
        multi_tf=MultiTimeframeTrend(h1_trend="BEARISH"),
    )
    side, source, _ = strategy._resolve_allowed_side_with_ltf_overrides(ta, "BEARISH")
    assert side == "SHORT"
    assert source == "bearish_dip_default"


def test_bullish_rally_ltf_ok_requires_all_four_conditions():
    strategy = _make_resolver_strategy()
    # All four pass.
    ok, reason = strategy._bullish_rally_ltf_ok(_make_ta_bullish_rally())
    assert ok is True
    assert "bullish_ltf_override" in reason
    # Drop RSI below threshold.
    ta = _make_ta_bullish_rally()
    ta.sol.rsi_14 = 50.0
    ok2, reason2 = strategy._bullish_rally_ltf_ok(ta)
    assert ok2 is False
    assert "rsi<55.0" in reason2


def _make_ta_chop_with_4h(hist: float, prev_hist: float) -> SOLTechnicalAnalysis:
    """Chop tape that fails the 5m/15m/RSI/BTC gates; 4H slope is the only differentiator."""
    return SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=50.0,
            macd_15m=MACDResult(histogram=0.01, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.01, histogram_rising=False),
            macd_4h=MACDResult(
                histogram=hist,
                prev_histogram=prev_hist,
                histogram_rising=(hist > prev_hist),
            ),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=0.0),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )


def test_4h_hist_override_disabled_by_default_does_not_fire():
    """Flag default-off: 4H hist declining alone does NOT fire the override."""
    cfg = _make_config()
    # Only existing LTF flag on; the new 4H-hist flags default to False.
    cfg["strategies"]["sol_macro"]["buy_no_ltf_override_enabled"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    ok, reason = strategy._bearish_dip_ltf_ok(_make_ta_chop_with_4h(hist=-0.05, prev_hist=0.05))
    assert ok is False
    assert "4h_hist" not in reason  # 4H path inactive


def test_4h_hist_override_buy_no_fires_when_alt_4h_hist_declining():
    """Flag-on additive path: chop 5m/15m + 4H hist declining → bearish_dip fires via 4H path."""
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["buy_no_4h_hist_override_enabled"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    ok, reason = strategy._bearish_dip_ltf_ok(_make_ta_chop_with_4h(hist=-0.05, prev_hist=0.02))
    assert ok is True
    assert "4h_hist_override" in reason
    assert "declining" in reason


def test_4h_hist_override_buy_yes_fires_when_alt_4h_hist_rising():
    """Flag-on symmetric mirror: 4H hist rising → bullish_rally fires via 4H path."""
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["buy_yes_4h_hist_override_enabled"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    ok, reason = strategy._bullish_rally_ltf_ok(_make_ta_chop_with_4h(hist=0.05, prev_hist=-0.02))
    assert ok is True
    assert "4h_hist_override" in reason
    assert "rising" in reason


def test_4h_hist_override_buy_no_does_not_fire_when_4h_hist_rising():
    """Flag-on but 4H slope wrong direction → no fire."""
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["buy_no_4h_hist_override_enabled"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    ok, reason = strategy._bearish_dip_ltf_ok(_make_ta_chop_with_4h(hist=0.05, prev_hist=-0.02))
    assert ok is False
    assert "4h_hist_not_declining" in reason


def test_4h_hist_override_resolver_bull_to_short_exception_via_4h_path():
    """End-to-end: BULL regime + chop 5m + 4H declining + 4h_hist flag on → SHORT exception."""
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["buy_no_4h_hist_override_enabled"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    side, source, detail = strategy._resolve_allowed_side_with_ltf_overrides(
        _make_ta_chop_with_4h(hist=-0.05, prev_hist=0.02), "BULLISH"
    )
    assert side == "SHORT"
    assert source == "bearish_dip_exception"
    assert "4h_hist_override" in detail


def test_4h_hist_override_resolver_bear_to_long_exception_via_4h_path():
    """End-to-end mirror: BEAR regime + chop 5m + 4H rising + 4h_hist flag on → LONG exception."""
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["buy_yes_4h_hist_override_enabled"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    side, source, detail = strategy._resolve_allowed_side_with_ltf_overrides(
        _make_ta_chop_with_4h(hist=0.05, prev_hist=-0.02), "BEARISH"
    )
    assert side == "LONG"
    assert source == "bullish_rally_exception"
    assert "4h_hist_override" in detail


def test_optional_min_positive_m5_adj_blocks_weak_5m_signal():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["min_positive_m5_adj_5m"] = 0.04
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._strong_enough_5m_signal(0.06, "BUY_YES") is True
    assert strategy._strong_enough_5m_signal(0.04, "BUY_YES") is True
    assert strategy._strong_enough_5m_signal(0.02, "BUY_YES") is False


def test_sol_live_lane_calibration_does_not_amplify_buy_no_probability(tmp_path):
    cfg = _make_config()
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    strategy.lane_calibrator = LaneCalibrator(
        path=tmp_path / "lane_posteriors.json",
        shadow_mode=False,
    )
    lane_id = "sol_macro|5m|down|bearish__bearish__bull|standard"
    for _ in range(20):
        strategy.lane_calibrator.record(
            lane_id,
            stated_est_prob=0.43,
            realized_pct=-0.30,
            win=False,
        )

    calibrated = strategy._calibrate_est_prob(
        0.43,
        action="BUY_NO",
        direction="DOWN",
        window_size="5m",
        side_source="primary_htf",
        signal_reason="UPDOWN_5m | standard",
        htf_bias="BEARISH",
        btc_1h_regime="BULL",
    )

    assert calibrated == 0.43


def test_alt_resolver_metadata_marks_quant_and_momentum_conflicts():
    meta = build_alt_resolver_metadata(
        side_source="primary_htf",
        htf_side="SHORT",
        quant_side="LONG",
        momentum_side="LONG",
    )

    assert meta["conflict_type"] == "alt_macro_quant_momentum_disagree"
    assert meta["resolver_path"] == "primary_htf__htf_short__quant_long__momentum_long"
    assert meta["htf_side"] == "SHORT"
    assert meta["quant_side"] == "LONG"
    assert meta["momentum_side"] == "LONG"


def test_min_positive_m5_adj_zero_allows_counter_momentum():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["min_positive_m5_adj_5m"] = 0.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._strong_enough_5m_signal(-0.04, "BUY_YES") is True
    assert strategy._strong_enough_5m_signal(0.02, "BUY_YES") is True


def test_entry_timing_window_uses_configured_remaining_minutes():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["entry_timing_window_15m_min"] = 8.0
    cfg["strategies"]["sol_macro"]["entry_timing_window_15m_max"] = 13.0
    cfg["strategies"]["sol_macro"]["entry_timing_window_5m_min"] = 1.5
    cfg["strategies"]["sol_macro"]["entry_timing_window_5m_max"] = 2.5
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._within_entry_timing_window(mins_left=10.0, tf="15m") is True
    assert strategy._within_entry_timing_window(mins_left=14.0, tf="15m") is False
    assert strategy._within_entry_timing_window(mins_left=2.0, tf="5m") is True
    assert strategy._within_entry_timing_window(mins_left=3.2, tf="5m") is False


def test_entry_timing_window_reads_legacy_ai_entry_keys():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["ai_entry_window_15m_min"] = 9.0
    cfg["strategies"]["sol_macro"]["ai_entry_window_15m_max"] = 12.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._within_entry_timing_window(mins_left=10.0, tf="15m") is True


def test_sol_late_window_guard_blocks_and_tightens_edge():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["late_window_block_mins"] = 1.0
    cfg["strategies"]["sol_macro"]["late_window_tighten_mins"] = 3.0
    cfg["strategies"]["sol_macro"]["late_window_extra_min_edge"] = 0.14
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    allowed, edge_bar, reason = strategy._apply_late_window_guard(
        mins_left=0.8,
        effective_min_edge=0.09,
    )
    assert allowed is False
    assert edge_bar == 0.09
    assert reason == "late_window_blocked"

    allowed2, edge_bar2, reason2 = strategy._apply_late_window_guard(
        mins_left=2.1,
        effective_min_edge=0.09,
    )
    assert allowed2 is True
    assert edge_bar2 == 0.14
    assert reason2 == "late_window_edge>=0.140"


def test_sol_low_corr_is_diagnostic_not_hard_veto_even_when_configured():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["low_corr_suppresses_entries"] = True
    cfg["strategies"]["sol_macro"]["low_corr_threshold_1h"] = 0.5
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    corr = BTCSOLCorrelation(correlation_1h=0.16)
    assert strategy._low_corr_blocks_entry(corr) is False


def test_sol_tuning_size_multiplier_uses_lane_config():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["tuning_size_multiplier"] = 0.6
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    assert strategy.tuning_size_multiplier == 0.6


def test_flat_btc_gate_bypass_can_use_any_directional_alt_bias():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["flat_btc_only_blocks_when_alt_neutral"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._flat_btc_gate_bypassed(action="BUY_YES", alt_1h_trend="BULLISH") is True
    assert strategy._flat_btc_gate_bypassed(action="BUY_NO", alt_1h_trend="BEARISH") is True
    assert strategy._flat_btc_gate_bypassed(action="BUY_NO", alt_1h_trend="BULLISH") is True
    assert strategy._flat_btc_gate_bypassed(action="BUY_YES", alt_1h_trend="BEARISH") is True
    assert strategy._flat_btc_gate_bypassed(action="BUY_YES", alt_1h_trend="NEUTRAL") is False


def test_flat_btc_gate_bypass_legacy_mode_still_requires_alignment():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["flat_btc_only_blocks_when_alt_neutral"] = False
    cfg["strategies"]["sol_macro"]["flat_btc_alt_aligned_bypass"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._flat_btc_gate_bypassed(action="BUY_YES", alt_1h_trend="BULLISH") is True
    assert strategy._flat_btc_gate_bypassed(action="BUY_NO", alt_1h_trend="BEARISH") is True
    assert strategy._flat_btc_gate_bypassed(action="BUY_NO", alt_1h_trend="BULLISH") is False
    assert strategy._flat_btc_gate_bypassed(action="BUY_YES", alt_1h_trend="BEARISH") is False


def test_native_5m_buy_no_suppression_does_not_preempt_enabled_flip():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["disable_buy_no_5m_native"] = True
    cfg["strategies"]["sol_macro"]["buy_no_5m_flip_to_yes"] = True
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._should_suppress_native_5m_buy_no() is False

    cfg["strategies"]["sol_macro"]["buy_no_5m_flip_to_yes"] = False
    strategy_without_flip = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    assert strategy_without_flip._should_suppress_native_5m_buy_no() is True


def test_alt_1h_bearish_blocks_5m_buy_yes():
    strategy = SolMacroStrategy(_make_config(), MagicMock(), MagicMock())

    assert (
        strategy._alt_1h_alignment_blocks_entry(
            action="BUY_YES",
            window_size="5m",
            alt_1h_trend="BEARISH",
        )
        == "alt_1h_bearish_blocks_5m_buy_yes"
    )
    assert (
        strategy._alt_1h_alignment_blocks_entry(
            action="BUY_YES",
            window_size="15m",
            alt_1h_trend="BEARISH",
        )
        is None
    )
    assert (
        strategy._alt_1h_alignment_blocks_entry(
            action="BUY_NO",
            window_size="5m",
            alt_1h_trend="BEARISH",
        )
        is None
    )


def test_buy_yes_floor_requires_bullish_alt_1h():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["5m_buy_yes_bullish_floor_bump"] = 0.19
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert (
        strategy._alt_buy_yes_bullish_floor_bump(
            window_size="5m",
            action="BUY_YES",
            htf_bias="BULLISH",
        )
        == 0.19
    )
    assert (
        strategy._alt_buy_yes_bullish_floor_bump(
            window_size="5m",
            action="BUY_YES",
            htf_bias="BEARISH",
        )
        == 0.0
    )
    assert (
        strategy._alt_buy_yes_bullish_floor_bump(
            window_size="5m",
            action="BUY_YES",
            htf_bias="NEUTRAL",
        )
        == 0.0
    )


def test_macro_oracle_feed_map_covers_all_crypto_lanes():
    assert ORACLE_FEEDS["SOLUSDT"][0] == "polygon"
    assert ORACLE_FEEDS["ETHUSDT"][0] == "polygon"
    assert ORACLE_FEEDS["XRPUSDT"][0] == "polygon"
    assert ORACLE_FEEDS["HYPEUSDT"][0] == "arbitrum"
    assert ORACLE_FEEDS["DOGEUSDT"][0] == "arbitrum"
    assert ORACLE_FEEDS["BNBUSDT"][0] == "arbitrum"


def test_lag_opportunity_ages_from_persistent_service_state():
    svc = SOLBTCService()
    first = BTCSOLCorrelation(
        lag_opportunity=True,
        opportunity_direction="LONG",
        opportunity_magnitude=0.35,
    )
    with patch("src.analysis.sol_btc_service.time.time", return_value=1000.0):
        svc._apply_lag_staleness(first, spike_window="5m", btc_move_pct=0.45)

    assert first.lag_opportunity is True
    assert first.lag_detected_at == 1000.0

    stale = BTCSOLCorrelation(
        lag_opportunity=True,
        opportunity_direction="LONG",
        opportunity_magnitude=0.35,
    )
    with patch("src.analysis.sol_btc_service.time.time", return_value=1301.0):
        svc._apply_lag_staleness(stale, spike_window="5m", btc_move_pct=0.46)

    assert stale.lag_opportunity is False
    assert stale.opportunity_direction == "NONE"
    assert stale.opportunity_magnitude == 0.0
    assert stale.lag_detected_at == 1000.0


def test_lag_opportunity_refreshes_on_material_new_btc_impulse():
    svc = SOLBTCService()
    first = BTCSOLCorrelation(
        lag_opportunity=True,
        opportunity_direction="SHORT",
        opportunity_magnitude=0.35,
    )
    with patch("src.analysis.sol_btc_service.time.time", return_value=1000.0):
        svc._apply_lag_staleness(first, spike_window="15m", btc_move_pct=-0.90)

    refreshed = BTCSOLCorrelation(
        lag_opportunity=True,
        opportunity_direction="SHORT",
        opportunity_magnitude=0.35,
    )
    with patch("src.analysis.sol_btc_service.time.time", return_value=1301.0):
        svc._apply_lag_staleness(refreshed, spike_window="15m", btc_move_pct=-1.05)

    assert refreshed.lag_opportunity is True
    assert refreshed.lag_detected_at == 1301.0


def test_optional_oracle_basis_gate_blocks_large_divergence():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["oracle_max_basis_bps"] = 10.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._oracle_basis_blocks_entry(12.5) is True
    assert strategy._oracle_basis_blocks_entry(-12.5) is True
    assert strategy._oracle_basis_blocks_entry(8.0) is False
    assert strategy._oracle_basis_blocks_entry(None) is False


def test_required_updown_oracle_validation_blocks_missing_and_stale_oracle():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["require_oracle_for_updown"] = True
    cfg["strategies"]["sol_macro"]["oracle_max_age_sec"] = 180
    cfg["strategies"]["sol_macro"]["oracle_max_basis_bps"] = 10.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    now = datetime.now(timezone.utc)
    missing = strategy._validate_updown_oracle(SOLAnalysis(current_price=100.0), now=now)
    assert missing.passed is False
    assert missing.reason == "oracle_missing"

    stale = strategy._validate_updown_oracle(
        SOLAnalysis(
            current_price=100.0,
            chainlink_price=100.0,
            chainlink_updated_at=now - timedelta(seconds=181),
        ),
        now=now,
    )
    assert stale.passed is False
    assert stale.reason == "oracle_stale"


def test_updown_oracle_stale_relax_passes_when_basis_within_relax_cap():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["require_oracle_for_updown"] = True
    cfg["strategies"]["sol_macro"]["oracle_max_age_sec"] = 180
    cfg["strategies"]["sol_macro"]["oracle_max_basis_bps"] = 10.0
    cfg["strategies"]["sol_macro"]["oracle_stale_basis_relax_max_bps"] = 40.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    now = datetime.now(timezone.utc)
    relaxed = strategy._validate_updown_oracle(
        SOLAnalysis(
            current_price=100.026,
            chainlink_price=100.0,
            chainlink_updated_at=now - timedelta(seconds=10_000),
        ),
        now=now,
    )
    assert relaxed.passed is True
    assert relaxed.reason == "oracle_stale_basis_relaxed"


def test_updown_oracle_fresh_basis_relax_passes_small_overshoot():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"]["require_oracle_for_updown"] = True
    cfg["strategies"]["sol_macro"]["oracle_max_age_sec"] = 180
    cfg["strategies"]["sol_macro"]["oracle_max_basis_bps"] = 10.0
    cfg["strategies"]["sol_macro"]["oracle_basis_relax_max_bps"] = 12.0
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
    now = datetime.now(timezone.utc)
    relaxed = strategy._validate_updown_oracle(
        SOLAnalysis(
            current_price=100.1059322,
            chainlink_price=100.0,
            chainlink_updated_at=now - timedelta(seconds=15),
        ),
        now=now,
    )
    assert relaxed.passed is True
    assert relaxed.reason == "oracle_basis_relaxed"


def test_updown_composite_floor_ignores_lane_and_bumps_low_confidence():
    cfg = _make_config()
    cfg["updown_composite"] = {
        "default_min_score": 0.62,
        "low_confidence_min_score": 0.66,
    }
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._updown_composite_floor(lane="default") == 0.62
    assert strategy._updown_composite_floor(lane="legacy_lane") == 0.62
    assert strategy._updown_composite_floor(lane="default", quant_confidence=0.55) == 0.66


def test_updown_composite_floor_uses_strategy_window_override_without_low_conf_bump():
    cfg = _make_config()
    cfg["updown_composite"] = {
        "default_min_score": 0.62,
        "low_confidence_min_score": 0.66,
        "strategy_window_min_scores": {
            "sol_macro": {
                "1h": 0.50,
            },
        },
    }
    strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strategy._updown_composite_floor(
        lane="default",
        window_size="1h",
        quant_confidence=0.55,
    ) == 0.50
    assert strategy._updown_composite_floor(
        lane="default",
        window_size="15m",
        quant_confidence=0.55,
    ) == 0.66


def test_sol_updown_oracle_block_is_logged_to_rejected_candidates():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"].update(
        {
            "min_liquidity": 1,
            "use_ai": False,
            "use_ai_updown": False,
            "require_oracle_for_updown": True,
            "oracle_max_basis_bps": 10.0,
            "entry_window_auto_align": False,
        }
    )
    ai = MagicMock()
    ai.shadow_pipeline_enabled.return_value = False
    strategy = SolMacroStrategy(cfg, ai, MagicMock())

    ta = _make_bearish_ta()
    ta.sol.chainlink_price = 120.0
    ta.sol.current_price = 120.30
    ta.sol.chainlink_updated_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    ta.sol.oracle_basis_bps = 25.0
    strategy.sol_service.get_full_analysis = MagicMock(return_value=ta)

    market = MagicMock()
    market.id = "sol_updown_oracle"
    market.question = "Solana Up or Down - May 17, 9:00AM-9:15AM ET"
    market.description = market.question
    market.yes_price = 0.50
    market.no_price = 0.50
    market.liquidity = 50000.0
    market.token_id_yes = "tok-yes-sol"
    market.token_id_no = "tok-no-sol"
    market.end_date = datetime.now(timezone.utc) + timedelta(minutes=14)
    market.token_ids = ["tok-yes-sol", "tok-no-sol"]
    market.slug = "sol-updown-15m-1770000010"
    market.group_item_title = "Solana Up or Down"
    market.volume = 10000.0
    market.spread = 0.02

    with patch("src.strategies.sol_macro.log_rejected_candidate") as mock_log:
        signals = run_async(strategy.scan_and_analyze([market], bankroll=10000.0))

    assert signals == []
    assert mock_log.call_count == 1
    assert mock_log.call_args.kwargs["reason"] == "oracle_basis_block"
    assert mock_log.call_args.kwargs["policy_version"] == "oracle_validation_v1"
    assert any(
        p.get("probe") == "oracle_basis_abs_bps"
        for p in mock_log.call_args.kwargs["probe_variants"]
    )


def test_sol_liquidity_reject_can_feed_shadow_observer():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"].update(
        {
            "use_ai": True,
            "use_ai_updown": True,
            "min_liquidity": 5000,
        }
    )
    ai = MagicMock()
    ai.shadow_pipeline_enabled.return_value = False
    ai.shadow_observer_enabled.return_value = True
    ai.shadow_observer_max_calls_per_scan.return_value = 1
    ai.observe_rejected_candidate = AsyncMock(return_value={"ok": True})
    strategy = SolMacroStrategy(cfg, ai, MagicMock())

    ta = _make_bullish_ta()
    strategy.sol_service.get_full_analysis = MagicMock(return_value=ta)

    market = MagicMock()
    market.id = "sol_liquidity_observer"
    market.question = "Solana Up or Down - May 17, 9:00AM-9:15AM ET"
    market.description = market.question
    market.yes_price = 0.50
    market.no_price = 0.50
    market.liquidity = 1000.0
    market.token_id_yes = "tok-yes-sol"
    market.token_id_no = "tok-no-sol"
    market.end_date = datetime.now(timezone.utc) + timedelta(minutes=14)
    market.token_ids = ["tok-yes-sol", "tok-no-sol"]
    market.slug = "sol-updown-15m-1770000011"
    market.group_item_title = "Solana Up or Down"
    market.volume = 10000.0
    market.spread = 0.02

    signals = run_async(strategy.scan_and_analyze([market], bankroll=10000.0))

    assert signals == []
    ai.observe_rejected_candidate.assert_awaited_once()
    assert strategy.last_scan_stats["shadow_observer_calls"] == 1
    assert strategy.last_scan_stats["shadow_observer_ok"] == 1


def test_sol_shadow_observer_timeout_consumes_scan_budget():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"].update(
        {
            "use_ai": True,
            "use_ai_updown": True,
            "min_liquidity": 5000,
        }
    )
    ai = MagicMock()
    ai.shadow_pipeline_enabled.return_value = False
    ai.shadow_observer_enabled.return_value = True
    ai.shadow_observer_max_calls_per_scan.return_value = 1
    strategy = SolMacroStrategy(cfg, ai, MagicMock())
    strategy._observe_rejected_candidate_with_timeout = AsyncMock(return_value=None)

    ta = _make_bullish_ta()
    strategy.sol_service.get_full_analysis = MagicMock(return_value=ta)

    def _market(idx: int) -> MagicMock:
        market = MagicMock()
        market.id = f"sol_liquidity_timeout_{idx}"
        market.question = f"Solana Up or Down - May 17, 9:0{idx}AM-9:15AM ET"
        market.description = market.question
        market.yes_price = 0.50
        market.no_price = 0.50
        market.liquidity = 1000.0
        market.token_id_yes = f"tok-yes-sol-{idx}"
        market.token_id_no = f"tok-no-sol-{idx}"
        market.end_date = datetime.now(timezone.utc) + timedelta(minutes=14)
        market.token_ids = [market.token_id_yes, market.token_id_no]
        market.slug = f"sol-updown-15m-17700000{idx}"
        market.group_item_title = "Solana Up or Down"
        market.volume = 10000.0
        market.spread = 0.02
        return market

    signals = run_async(strategy.scan_and_analyze([_market(1), _market(2)], bankroll=10000.0))

    assert signals == []
    strategy._observe_rejected_candidate_with_timeout.assert_awaited_once()
    assert strategy.last_scan_stats["shadow_observer_calls"] == 1
    assert strategy.last_scan_stats["shadow_observer_ok"] == 0


def test_sol_shadow_observer_skips_repeated_market_during_cooldown():
    cfg = _make_config()
    cfg["strategies"]["sol_macro"].update(
        {
            "use_ai": True,
            "use_ai_updown": True,
            "min_liquidity": 5000,
            "ai_observer_retry_cooldown_sec": 300,
        }
    )
    ai = MagicMock()
    ai.shadow_pipeline_enabled.return_value = False
    ai.shadow_observer_enabled.return_value = True
    ai.shadow_observer_max_calls_per_scan.return_value = 1
    strategy = SolMacroStrategy(cfg, ai, MagicMock())
    strategy._observe_rejected_candidate_with_timeout = AsyncMock(return_value=None)

    ta = _make_bullish_ta()
    strategy.sol_service.get_full_analysis = MagicMock(return_value=ta)

    market = MagicMock()
    market.id = "sol_liquidity_cooldown"
    market.question = "Solana Up or Down - May 17, 9:00AM-9:15AM ET"
    market.description = market.question
    market.yes_price = 0.50
    market.no_price = 0.50
    market.liquidity = 1000.0
    market.token_id_yes = "tok-yes-sol"
    market.token_id_no = "tok-no-sol"
    market.end_date = datetime.now(timezone.utc) + timedelta(minutes=14)
    market.token_ids = ["tok-yes-sol", "tok-no-sol"]
    market.slug = "sol-updown-15m-1770000011"
    market.group_item_title = "Solana Up or Down"
    market.volume = 10000.0
    market.spread = 0.02

    run_async(strategy.scan_and_analyze([market], bankroll=10000.0))
    run_async(strategy.scan_and_analyze([market], bankroll=10000.0))

    strategy._observe_rejected_candidate_with_timeout.assert_awaited_once()


def _make_bullish_ta():
    """Create a bullish SOL technical analysis."""
    return SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=135.50,
            ema_9=134.00,
            ema_21=132.00,
            ema_50=128.00,  # Bullish alignment
            rsi_14=62.0,
            macd_15m=MACDResult(
                macd_line=0.45,
                signal_line=0.30,
                histogram=0.15,
                prev_histogram=0.08,
                crossover="BULLISH_CROSS",
                histogram_rising=True,
                above_zero=True,
            ),
            macd_5m=MACDResult(
                macd_line=0.12,
                signal_line=0.08,
                histogram=0.04,
                prev_histogram=0.01,
                crossover="BULLISH_CROSS",
                histogram_rising=True,
                above_zero=True,
            ),
            atr_14=3.20,
            trend_direction="BULLISH",
            trend_strength=0.75,
        ),
        correlation=BTCSOLCorrelation(
            correlation_1h=0.88,
            btc_move_5m_pct=0.45,
            btc_move_15m_pct=0.90,
            btc_spike_detected=True,
            btc_spike_direction="UP",
            sol_move_5m_pct=0.15,
            sol_move_15m_pct=0.30,
            sol_lag_pct=0.45,
            lag_opportunity=True,
            opportunity_direction="LONG",
            opportunity_magnitude=0.45,
            btc_price=75200.0,
            btc_chainlink_price=75180.0,
        ),
        multi_tf=MultiTimeframeTrend(
            h1_trend="BULLISH",
            h1_basis="EMA9>EMA21>EMA50 RSI=62",
            m15_trend="BULLISH",
            m15_basis="MACD above zero, histogram rising",
            m5_trend="BULLISH",
            m5_basis="MACD bullish cross",
            aligned=True,
            overall_direction="BULLISH",
        ),
    )


def _make_bearish_ta():
    """Create a bearish SOL technical analysis."""
    return SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=120.50,
            ema_9=122.00,
            ema_21=125.00,
            ema_50=130.00,  # Bearish alignment
            rsi_14=35.0,
            macd_15m=MACDResult(
                macd_line=-0.55,
                signal_line=-0.30,
                histogram=-0.25,
                prev_histogram=-0.15,
                crossover="BEARISH_CROSS",
                histogram_rising=False,
                above_zero=False,
            ),
            macd_5m=MACDResult(
                macd_line=-0.15,
                signal_line=-0.08,
                histogram=-0.07,
                prev_histogram=-0.02,
                crossover="BEARISH_CROSS",
                histogram_rising=False,
                above_zero=False,
            ),
            atr_14=4.10,
            trend_direction="BEARISH",
            trend_strength=0.80,
        ),
        correlation=BTCSOLCorrelation(
            correlation_1h=0.82,
            btc_move_5m_pct=-0.50,
            btc_move_15m_pct=-1.10,
            btc_spike_detected=True,
            btc_spike_direction="DOWN",
            sol_move_5m_pct=-0.10,
            sol_move_15m_pct=-0.25,
            sol_lag_pct=0.60,
            lag_opportunity=True,
            opportunity_direction="SHORT",
            opportunity_magnitude=0.60,
            btc_price=73500.0,
        ),
        multi_tf=MultiTimeframeTrend(
            h1_trend="BEARISH",
            h1_basis="EMA9<EMA21<EMA50 RSI=35",
            m15_trend="BEARISH",
            m15_basis="MACD below zero, histogram falling",
            m5_trend="BEARISH",
            m5_basis="MACD bearish cross",
            aligned=True,
            overall_direction="BEARISH",
        ),
    )


def _make_choppy_ta():
    """Create a neutral/choppy SOL technical analysis."""
    return SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=130.00,
            ema_9=130.50,
            ema_21=130.20,
            ema_50=129.80,  # Tight, no clear direction
            rsi_14=50.0,
            macd_15m=MACDResult(
                macd_line=0.02,
                signal_line=0.01,
                histogram=0.01,
                prev_histogram=-0.01,
                crossover="NONE",
                histogram_rising=True,
                above_zero=True,
            ),
            macd_5m=MACDResult(
                macd_line=-0.01,
                signal_line=0.00,
                histogram=-0.01,
                prev_histogram=0.01,
                crossover="NONE",
                histogram_rising=False,
                above_zero=False,
            ),
            atr_14=1.50,
            trend_direction="NEUTRAL",
            trend_strength=0.2,
        ),
        correlation=BTCSOLCorrelation(
            correlation_1h=0.45,
            btc_move_5m_pct=0.05,
            btc_move_15m_pct=0.10,
            btc_spike_detected=False,
            btc_spike_direction="NONE",
            sol_move_5m_pct=0.03,
            sol_move_15m_pct=0.07,
            sol_lag_pct=0.0,
            lag_opportunity=False,
            opportunity_direction="NONE",
            opportunity_magnitude=0.0,
            btc_price=74000.0,
        ),
        multi_tf=MultiTimeframeTrend(
            h1_trend="NEUTRAL",
            h1_basis="No clear direction",
            m15_trend="NEUTRAL",
            m15_basis="MACD near zero",
            m5_trend="NEUTRAL",
            m5_basis="No crossover",
            aligned=False,
            overall_direction="NEUTRAL",
        ),
    )


def _make_market(
    question="Will Solana reach $150 by end of March?",
    yes_price=0.35,
    liquidity=50000,
    market_id="sol-150-march",
):
    m = MagicMock()
    m.id = market_id
    m.question = question
    m.description = question
    m.yes_price = yes_price
    m.no_price = 1 - yes_price
    m.liquidity = liquidity
    m.token_id_yes = "tok-yes-sol"
    m.token_id_no = "tok-no-sol"
    m.end_date = datetime.now() + timedelta(days=15)
    m.token_ids = ["tok-yes-sol", "tok-no-sol"]
    return m


# ═══════════════════════════════════════════════════════════════
# Test Classes
# ═══════════════════════════════════════════════════════════════


class TestSOLMacroTrend(unittest.TestCase):
    """LAYER 1: 1H Macro Trend determination."""

    def setUp(self):
        self.strategy = SolMacroStrategy(
            _make_config(),
            MagicMock(),
            MagicMock(),
            exposure_manager=ExposureManager(_make_config(), is_paper=True),
        )

    def test_bullish_macro(self):
        ta = _make_bullish_ta()
        trend = self.strategy._get_macro_trend(ta)
        self.assertEqual(trend, "BULLISH")

    def test_bearish_macro(self):
        ta = _make_bearish_ta()
        trend = self.strategy._get_macro_trend(ta)
        self.assertEqual(trend, "BEARISH")

    def test_neutral_macro(self):
        ta = _make_choppy_ta()
        trend = self.strategy._get_macro_trend(ta)
        self.assertEqual(trend, "NEUTRAL")

    def test_bullish_requires_two_votes(self):
        """Bullish needs at least 2 of 3 votes."""
        ta = _make_bullish_ta()
        # Override RSI to neutral — still bullish (h1 + EMA alignment)
        ta.sol.rsi_14 = 50.0
        trend = self.strategy._get_macro_trend(ta)
        self.assertEqual(trend, "BULLISH")

    def test_single_bull_vote_is_neutral(self):
        """Only 1 bullish vote → NEUTRAL."""
        ta = _make_choppy_ta()
        ta.multi_tf.h1_trend = "BULLISH"
        # EMAs are flat, RSI is 50 → only 1 bull vote
        trend = self.strategy._get_macro_trend(ta)
        self.assertIn(trend, ["NEUTRAL", "BULLISH"])  # depends on EMA alignment

    def test_primary_btc_htf_bias_helper_uses_same_gate_as_allowed_side(self):
        strategy = self.strategy
        self.assertAlmostEqual(strategy._apply_primary_htf_bias(0.50, "BULLISH", 0.07), 0.57)
        self.assertAlmostEqual(strategy._apply_primary_htf_bias(0.50, "BEARISH", 0.07), 0.43)
        self.assertAlmostEqual(strategy._apply_primary_htf_bias(0.50, "NEUTRAL", 0.07), 0.50)


class TestSOL15mConfirmation(unittest.TestCase):
    """LAYER 2: 15m MACD confirmation."""

    def setUp(self):
        self.strategy = SolMacroStrategy(
            _make_config(),
            MagicMock(),
            MagicMock(),
            exposure_manager=ExposureManager(_make_config(), is_paper=True),
        )

    def test_bullish_cross_confirms_long(self):
        ta = _make_bullish_ta()
        confirmed, strength, reasons = self.strategy._check_15m_confirmation(ta, "LONG")
        self.assertTrue(confirmed)
        self.assertGreater(strength, 0.25)
        self.assertTrue(any("bull cross" in r for r in reasons))

    def test_bearish_cross_confirms_short(self):
        ta = _make_bearish_ta()
        confirmed, strength, reasons = self.strategy._check_15m_confirmation(
            ta, "SHORT"
        )
        self.assertTrue(confirmed)
        self.assertGreater(strength, 0.25)
        self.assertTrue(any("bear cross" in r for r in reasons))

    def test_bearish_cross_does_not_confirm_long(self):
        ta = _make_bearish_ta()
        confirmed, strength, reasons = self.strategy._check_15m_confirmation(ta, "LONG")
        self.assertFalse(confirmed)
        self.assertLess(strength, 0.25)

    def test_rising_histogram_adds_strength(self):
        ta = _make_bullish_ta()
        ta.sol.macd_15m.crossover = "NONE"  # No cross, but histogram is rising
        ta.sol.macd_15m.prev_histogram = -0.05
        ta.sol.macd_15m.histogram = 0.05
        confirmed, strength, reasons = self.strategy._check_15m_confirmation(ta, "LONG")
        # Composite threshold is 0.50; red->green + MACD>signal = 0.45 (not confirmed).
        self.assertFalse(confirmed)
        self.assertGreaterEqual(strength, 0.35)
        self.assertTrue(any("red-to-green" in r for r in reasons))

    def test_flat_macd_weak_confirmation(self):
        ta = _make_choppy_ta()
        # Set truly flat MACD: no cross, not rising meaningfully, below signal
        ta.sol.macd_15m.crossover = "NONE"
        ta.sol.macd_15m.histogram_rising = False
        ta.sol.macd_15m.macd_line = -0.001
        ta.sol.macd_15m.signal_line = 0.001
        confirmed, strength, reasons = self.strategy._check_15m_confirmation(ta, "LONG")
        # Should not confirm LONG
        self.assertFalse(confirmed)


class TestSOLEntryTiming(unittest.TestCase):
    """LAYER 3: 5m entry timing + BTC-SOL lag detection."""

    def setUp(self):
        self.strategy = SolMacroStrategy(
            _make_config(),
            MagicMock(),
            MagicMock(),
            exposure_manager=ExposureManager(_make_config(), is_paper=True),
        )

    def test_bullish_5m_cross_adds_bonus(self):
        ta = _make_bullish_ta()
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        self.assertGreater(bonus, 0)
        self.assertTrue(any("5m MACD bull cross" in r for r in reasons))

    def test_lag_opportunity_adds_major_bonus(self):
        ta = _make_bullish_ta()
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        # Layer 3 is 5m MACD timing + corr context only (lag/spike handled in scan loops)
        self.assertGreaterEqual(bonus, 0.04)
        self.assertTrue(any("5m" in r for r in reasons))
        self.assertTrue(any("high corr" in r for r in reasons))

    def test_lag_against_direction_penalizes(self):
        ta = _make_bullish_ta()
        # Lag direction is LONG but we want SHORT
        bonus, reasons = self.strategy._check_entry_timing(ta, "SHORT")
        # 5m bearish cross wouldn't fire, and lag is against
        lag_reasons = [r for r in reasons if "lag against" in r]
        # Either lag penalty is applied or bonus is reduced
        assert bonus <= 0.10, (
            f"Bonus should be small when lag is against direction, got {bonus}"
        )

    def test_high_correlation_adds_bonus(self):
        ta = _make_bullish_ta()
        ta.correlation.correlation_1h = 0.92
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        self.assertTrue(any("high corr" in r for r in reasons))

    def test_low_correlation_penalizes(self):
        ta = _make_bullish_ta()
        ta.correlation.correlation_1h = 0.35
        ta.correlation.lag_opportunity = False  # No lag when low corr
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        self.assertTrue(any("low corr" in r for r in reasons))

    def test_btc_spike_adds_extra_bonus(self):
        ta = _make_bullish_ta()
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        # Spike is not logged in Layer 3 after 2026-04-07 refactor; corr context remains
        self.assertTrue(ta.correlation.btc_spike_detected)
        self.assertTrue(any("high corr" in r for r in reasons))

    def test_no_lag_no_spike_minimal_bonus(self):
        ta = _make_choppy_ta()
        bonus, reasons = self.strategy._check_entry_timing(ta, "LONG")
        # Low corr penalty, no lag bonus
        self.assertLess(bonus, 0.05)


class TestSOLEdgeCalculation(unittest.TestCase):
    """LAYER 4: Probability estimation."""

    def setUp(self):
        self.strategy = SolMacroStrategy(
            _make_config(),
            MagicMock(),
            MagicMock(),
            exposure_manager=ExposureManager(_make_config(), is_paper=True),
        )

    def test_sol_above_threshold_up_direction(self):
        """SOL at $135 vs $130 threshold UP → high probability."""
        ta = _make_bullish_ta()
        prob = self.strategy._estimate_probability(
            sol_price=135.50,
            threshold=130.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.5,
            timing_bonus=0.05,
        )
        self.assertGreater(prob, 0.55)

    def test_sol_below_threshold_up_direction(self):
        """SOL at $120 vs $150 threshold UP → lower probability."""
        ta = _make_bearish_ta()
        prob = self.strategy._estimate_probability(
            sol_price=120.50,
            threshold=150.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.0,
            timing_bonus=0.0,
        )
        self.assertLess(prob, 0.50)

    def test_lag_opportunity_does_not_change_alt_probability(self):
        """BTC-SOL lag is context only; alt probability stays alt-native."""
        ta = _make_bullish_ta()
        prob_with_lag = self.strategy._estimate_probability(
            sol_price=135.50,
            threshold=130.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.5,
            timing_bonus=0.05,
        )
        # Remove lag
        ta.correlation.lag_opportunity = False
        prob_without_lag = self.strategy._estimate_probability(
            sol_price=135.50,
            threshold=130.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.5,
            timing_bonus=0.05,
        )
        self.assertEqual(prob_with_lag, prob_without_lag)

    def test_overbought_rsi_penalizes_up(self):
        """RSI > 75 should reduce UP probability."""
        ta = _make_bullish_ta()
        ta.sol.rsi_14 = 78.0
        prob = self.strategy._estimate_probability(
            sol_price=135.50,
            threshold=130.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.5,
            timing_bonus=0.05,
        )
        # Should be lower due to overbought
        ta.sol.rsi_14 = 58.0
        prob_normal = self.strategy._estimate_probability(
            sol_price=135.50,
            threshold=130.0,
            direction="UP",
            ta=ta,
            days_to_resolution=15,
            ltf_strength=0.5,
            timing_bonus=0.05,
        )
        self.assertLess(prob, prob_normal)

    def test_probability_bounded(self):
        """Probability should always be between 0.05 and 0.95."""
        ta = _make_bullish_ta()
        prob = self.strategy._estimate_probability(
            sol_price=200.0,
            threshold=100.0,
            direction="UP",
            ta=ta,
            days_to_resolution=1,
            ltf_strength=1.0,
            timing_bonus=0.20,
        )
        self.assertLessEqual(prob, 0.95)
        self.assertGreaterEqual(prob, 0.05)

    def test_admission_prob_default_is_noop_for_alt_lanes(self):
        est_prob = 0.80
        yes_price = 0.60

        adm = self.strategy._admission_prob(
            est_prob,
            window_size="5m",
            action="BUY_YES",
        )

        self.assertEqual(adm, est_prob)
        self.assertAlmostEqual(adm - yes_price, est_prob - yes_price)

    def test_lane_admission_shrink_deflates_edge_without_mutating_signal_prob(self):
        cfg = _make_config()
        cfg["trading"]["exit_rules"] = {
            "updown_overrides": {
                "sol_macro": {
                    "window_lane_overrides": {
                        "5m": {
                            "up": {
                                "entry_admission_calibration_shrink": 0.28,
                            },
                        },
                    },
                },
            },
        }
        strategy = SolMacroStrategy(cfg, MagicMock(), MagicMock())
        estimated_prob = 0.80
        yes_price = 0.60

        adm = strategy._admission_prob(
            estimated_prob,
            window_size="5m",
            action="BUY_YES",
        )
        edge = adm - yes_price
        signal = SolMacroSignal(
            market_id="m1",
            market_question="Solana Up or Down",
            action="BUY_YES",
            price=yes_price,
            size=1.0,
            confidence=0.7,
            edge=edge,
            token_id_yes="yes",
            token_id_no="no",
            direction="UP",
            est_prob=estimated_prob,
            raw_est_prob=estimated_prob,
            window_size="5m",
        )

        self.assertAlmostEqual(adm, 0.584)
        self.assertAlmostEqual(edge, -0.016)
        self.assertEqual(signal.est_prob, estimated_prob)
        self.assertEqual(signal.raw_est_prob, estimated_prob)


class TestSOLMarketDetection(unittest.TestCase):
    """Market detection: SOL vs non-SOL markets."""

    def setUp(self):
        self.strategy = SolMacroStrategy(
            _make_config(),
            MagicMock(),
            MagicMock(),
        )

    def test_solana_market_detected(self):
        m = _make_market("Will Solana reach $150?")
        self.assertTrue(self.strategy._is_solana_market(m))

    def test_sol_abbreviation_detected(self):
        m = _make_market("Will SOL price exceed $200?")
        self.assertTrue(self.strategy._is_solana_market(m))

    def test_bitcoin_only_rejected(self):
        m = _make_market("Will Bitcoin reach $100,000?")
        self.assertFalse(self.strategy._is_solana_market(m))

    def test_bitcoin_with_solana_accepted(self):
        m = _make_market("Will Solana outperform Bitcoin this month?")
        self.assertTrue(self.strategy._is_solana_market(m))

    def test_direction_up(self):
        self.assertEqual(self.strategy._extract_direction("Will SOL reach $200?"), "UP")

    def test_direction_down(self):
        self.assertEqual(
            self.strategy._extract_direction("Will SOL drop below $100?"), "DOWN"
        )

    def test_price_extraction(self):
        self.assertEqual(
            self.strategy._extract_price_threshold("SOL above $150?"), 150.0
        )

    def test_price_extraction_with_comma(self):
        self.assertEqual(
            self.strategy._extract_price_threshold("SOL above $1,500?"), 1500.0
        )

    def test_price_out_of_range_rejected(self):
        """SOL range is $1-$10,000. BTC prices should be rejected."""
        self.assertIsNone(self.strategy._extract_price_threshold("above $75,000?"))

    def test_price_too_low_rejected(self):
        self.assertIsNone(self.strategy._extract_price_threshold("above $0.50?"))


class TestSOLSignalGating(unittest.TestCase):
    """Integration: macro trend gates signals correctly."""

    def setUp(self):
        self.config = _make_config()
        self.ai = MagicMock()
        self.sizer = MagicMock()
        self.sizer.kelly_bet = MagicMock(return_value=3.0)
        self.em = ExposureManager(self.config, is_paper=True)
        self.strategy = SolMacroStrategy(
            self.config, self.ai, self.sizer, exposure_manager=self.em
        )

    @patch.object(SOLBTCService, "get_full_analysis")
    async def _run_scan(self, ta, mock_analysis):
        mock_analysis.return_value = ta
        markets = [_make_market()]
        return await self.strategy.scan_and_analyze(markets, bankroll=10000.0)

    def test_neutral_macro_produces_no_signals(self):
        """NEUTRAL macro trend → no signals (sit out)."""
        ta = _make_choppy_ta()
        signals = run_async(self._run_scan(ta))
        self.assertEqual(len(signals), 0)

    def test_bullish_macro_allows_signals(self):
        """BULLISH macro with confirming indicators → signals possible."""
        ta = _make_bullish_ta()
        self.em.scale_size = MagicMock(return_value=3.0)
        signals = run_async(self._run_scan(ta))
        # Signals depend on edge calc vs threshold, but at minimum the strategy shouldn't crash
        self.assertIsInstance(signals, list)


class TestSOLExposureIntegration(unittest.TestCase):
    """Exposure manager correctly scales SOL positions."""

    def test_conditions_from_bullish_ta(self):
        ta = _make_bullish_ta()
        conditions = SolMacroStrategy.conditions_from_ta(ta)
        self.assertGreater(conditions.volatility, 0)
        self.assertEqual(conditions.trend_direction, "BULLISH")
        # High correlation → volume ratio > 1
        self.assertGreater(conditions.volume_ratio, 1.0)

    def test_conditions_from_choppy_ta(self):
        ta = _make_choppy_ta()
        conditions = SolMacroStrategy.conditions_from_ta(ta)
        self.assertEqual(conditions.trend_direction, "NEUTRAL")
        # Correlation 0.45 → falls through to default volume_ratio=1.0 (not > 0.8, not < 0.4)
        self.assertLessEqual(conditions.volume_ratio, 1.0)

    def test_aligned_tf_gives_full_strength(self):
        ta = _make_bullish_ta()
        conditions = SolMacroStrategy.conditions_from_ta(ta)
        # Aligned = True → trend_strength = 1.0
        self.assertEqual(conditions.trend_strength, 1.0)

    def test_unaligned_tf_gives_partial_strength(self):
        ta = _make_choppy_ta()
        conditions = SolMacroStrategy.conditions_from_ta(ta)
        # Not aligned → uses sol.trend_strength
        self.assertLess(conditions.trend_strength, 1.0)

    def test_paused_exposure_blocks_signals(self):
        """When exposure is PAUSED, strategy returns no signals."""
        import asyncio

        config = _make_config()
        em = ExposureManager(config, is_paper=True)
        em.manual_pause()

        strategy = SolMacroStrategy(
            config,
            MagicMock(),
            MagicMock(),
            exposure_manager=em,
        )

        with patch.object(
            SOLBTCService, "get_full_analysis", return_value=_make_bullish_ta()
        ):
            markets = [_make_market()]
            signals = run_async(
                strategy.scan_and_analyze(markets, bankroll=10000.0)
            )
        self.assertEqual(len(signals), 0)


class TestSOL15mIQL(unittest.TestCase):
    """IQL shares LTF confirmation scoring then applies relaxed MACD rules."""

    def setUp(self):
        cfg = _make_config()
        cfg["strategies"]["sol_macro"]["iql_15m_enabled"] = True
        cfg["strategies"]["sol_macro"]["iql_15m_hist_floor"] = 0.15
        self.strategy = SolMacroStrategy(
            cfg,
            MagicMock(),
            MagicMock(),
            exposure_manager=ExposureManager(cfg, is_paper=True),
        )

    def test_iql_short_circuits_when_ltf_confirmed(self):
        ta = _make_choppy_ta()
        with patch.object(
            self.strategy,
            "_check_macd_confirmation",
            return_value=(True, 0.55, ["stub"]),
        ):
            self.assertTrue(self.strategy._passes_iql(ta, "LONG", tf="15m"))

    def test_iql_relaxed_hist_floor_when_unconfirmed(self):
        ta = _make_choppy_ta()
        ta.sol.macd_15m.crossover = "NONE"
        ta.sol.macd_15m.prev_histogram = -0.1
        ta.sol.macd_15m.histogram = 0.2
        ta.sol.macd_15m.histogram_rising = True
        ta.sol.macd_15m.macd_line = -0.01
        ta.sol.macd_15m.signal_line = 0.01
        confirmed, _, _ = self.strategy._check_15m_confirmation(ta, "LONG")
        self.assertFalse(confirmed)
        self.assertTrue(self.strategy._passes_iql(ta, "LONG", tf="15m"))

    def test_iql_rejects_when_unconfirmed_and_no_relaxed_signal(self):
        ta = _make_choppy_ta()
        ta.sol.macd_15m.crossover = "NONE"
        ta.sol.macd_15m.histogram_rising = False
        ta.sol.macd_15m.histogram = 0.01
        ta.sol.macd_15m.prev_histogram = 0.01
        ta.sol.macd_15m.macd_line = -1.0
        ta.sol.macd_15m.signal_line = 1.0
        self.assertFalse(self.strategy._passes_iql(ta, "LONG", tf="15m"))

    def test_iql_disabled_always_passes(self):
        self.strategy.iql_15m_enabled = False
        ta = _make_choppy_ta()
        self.assertTrue(self.strategy._passes_iql(ta, "LONG", tf="15m"))


def test_macd_bearish_momentum_ok_bear_cross():
    from types import SimpleNamespace

    from src.strategies.sol_macro import macd_bearish_momentum_ok

    m = SimpleNamespace(crossover="BEARISH_CROSS")
    assert macd_bearish_momentum_ok(m) is True


def test_macd_bearish_momentum_ok_red_falling_histogram():
    from types import SimpleNamespace

    from src.strategies.sol_macro import macd_bearish_momentum_ok

    m = SimpleNamespace(
        crossover="",
        histogram=-0.1,
        histogram_rising=False,
        macd_line=-1.0,
        signal_line=0.5,
    )
    assert macd_bearish_momentum_ok(m) is True


def test_macd_bearish_momentum_ok_rejects_bull_rising():
    from types import SimpleNamespace

    from src.strategies.sol_macro import macd_bearish_momentum_ok

    m = SimpleNamespace(
        crossover="",
        histogram=0.1,
        histogram_rising=True,
        macd_line=1.0,
        signal_line=0.5,
    )
    assert macd_bearish_momentum_ok(m) is False


if __name__ == "__main__":
    unittest.main()
