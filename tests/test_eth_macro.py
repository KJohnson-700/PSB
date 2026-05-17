from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.analysis.btc_price_service import CandleMomentum, MACDResult, TechnicalAnalysis
from src.analysis.sol_btc_service import (
    BTCSOLCorrelation,
    MultiTimeframeTrend,
    SOLAnalysis,
    SOLTechnicalAnalysis,
)
from src.market.scanner import Market
from src.strategies.eth_macro import ETHMacroStrategy
from tests.async_helpers import run_async


def _config():
    return {
        "strategies": {
            "sol_macro": {"enabled": False},
            "eth_macro": {
                "enabled": True,
                "min_edge": 0.09,
                "min_edge_5m": 0.09,
                "entry_price_min": 0.46,
                "entry_price_max": 0.54,
                "btc_follow_1h_hist_min": 8.0,
                "btc_follow_1h_allow_rising_recovery": False,
                "btc_follow_1h_allow_floor_without_rising": False,
                "btc_follow_15m_hist_min": 0.03,
                "btc_follow_5m_requires_impulse": True,
                "eth_follow_5m_min_adj": 0.04,
                "eth_follow_15m_hist_min": 0.03,
                "eth_follow_15m_min_adj": 0.05,
                "rsi_sell_block_below": 40.0,
                "ai_hold_veto_ttl_sec": 111,
                "min_edge_5m_ai_override": 0.12,
            }
        },
        "trading": {"dry_run": True},
    }


def test_eth_btc_follow_1h_gate_requires_real_continuation():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    btc_ta = TechnicalAnalysis(
        current_price=90000.0,
        macd_1h=MACDResult(histogram=9.0, histogram_rising=True, crossover="NONE"),
    )
    assert strat._btc_follow_1h_ok(btc_ta, "LONG") is True
    btc_ta.macd_1h = MACDResult(histogram=3.0, histogram_rising=False, crossover="NONE")
    assert strat._btc_follow_1h_ok(btc_ta, "LONG") is False


def test_eth_btc_follow_5m_impulse_scores_only_real_btc_impulse():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    mom = CandleMomentum(m5_direction="DRIFT_UP", m5_move_pct=0.04, m5_in_prediction_window=False)
    score, _ = strat._btc_follow_5m_impulse_score(mom, "LONG")
    assert score == 0.04
    mom = CandleMomentum(m5_direction="NONE", m5_move_pct=0.0, m5_in_prediction_window=False)
    score, _ = strat._btc_follow_5m_impulse_score(mom, "LONG")
    assert score == 0.0


def test_eth_1h_follow_score_prefers_real_hourly_alignment():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    score, reasons = strat._eth_1h_follow_score(
        MACDResult(histogram=0.08, histogram_rising=True, crossover="NONE"),
        "LONG",
    )
    assert score > 0
    assert "ETH1h green+rising" in reasons

    score_against, reasons_against = strat._eth_1h_follow_score(
        MACDResult(histogram=-0.03, histogram_rising=False, crossover="NONE"),
        "LONG",
    )
    assert score_against < 0
    assert "ETH1h against" in reasons_against


def test_eth_rsi_soft_penalty_buy_no_when_oversold_not_hard_block():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    hard, delta = strat._resolve_rsi_gate("BUY_NO", 35.0)
    assert hard is False
    assert delta > 0
    hard2, delta2 = strat._resolve_rsi_gate("BUY_NO", 45.0)
    assert hard2 is False
    assert delta2 == 0.0


def test_eth_buy_no_ltf_override_uses_eth_strategy_config():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["buy_no_ltf_override_enabled"] = True
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=38.0,
            macd_15m=MACDResult(histogram=-0.05, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.02, histogram_rising=False),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=-0.01),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )

    allowed, reason = strat._buy_no_ltf_override(ta)

    assert allowed is True
    assert "bearish_ltf_override" in reason


def test_eth_scan_buy_no_ltf_override_uses_eth_ta_without_name_error():
    cfg = _config()
    cfg["strategies"]["eth_macro"].update(
        {
            "buy_no_ltf_override_enabled": True,
            "dead_zone_enabled": False,
            "use_ai": False,
            "use_ai_updown": False,
            "min_liquidity": 1,
            "min_edge": 0.03,
            "entry_window_auto_align": False,
        }
    )
    ai = MagicMock()
    ai.research_narrative_enabled.return_value = False
    ai.research_narrative_max_calls_per_scan.return_value = 0
    ai.research_narrative_min_confidence.return_value = 1.0
    kelly = MagicMock()
    kelly.size_from_edge.return_value = 10.0
    strat = ETHMacroStrategy(cfg, ai, MagicMock(), kelly_sizer=kelly)
    strat._get_btc_htf_bias = MagicMock(return_value="BULLISH")

    eth_ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=3500.0,
            rsi_14=38.0,
            macd_15m=MACDResult(histogram=-0.05, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.02, histogram_rising=False),
        ),
        correlation=BTCSOLCorrelation(
            btc_price=100000.0,
            btc_move_5m_pct=-0.10,
            btc_move_15m_pct=-0.20,
            correlation_1h=0.8,
            sol_trend="BULLISH",
        ),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )
    btc_ta = TechnicalAnalysis(
        current_price=100000.0,
        macd_1h=MACDResult(histogram=20.0, histogram_rising=True),
        macd_15m=MACDResult(histogram=-0.05, histogram_rising=False),
        candle_momentum=CandleMomentum(
            m15_direction="DRIFT_DOWN",
            m5_direction="DRIFT_DOWN",
            m5_move_pct=-0.1,
        ),
    )
    strat.sol_service.get_full_analysis = MagicMock(return_value=eth_ta)
    strat.btc_service.get_full_analysis = MagicMock(return_value=btc_ta)

    market = Market(
        id="eth15",
        question="Ethereum Up or Down - May 13, 9:00AM-9:15AM ET",
        description="ETH 15m up/down test market",
        volume=1000.0,
        liquidity=1000.0,
        yes_price=0.50,
        no_price=0.50,
        spread=0.02,
        end_date=datetime.now(timezone.utc) + timedelta(minutes=14),
        token_id_yes="yes",
        token_id_no="no",
        group_item_title="Ethereum Up or Down",
        slug="eth-updown-15m-1770000000",
    )

    signals = run_async(strat.scan_and_analyze([market], bankroll=10000.0))

    assert signals
    assert signals[0].action == "BUY_NO"
    assert signals[0].price == 0.50


def test_eth_scan_eth_only_when_btc_full_analysis_unavailable():
    """BTC price service get_full_analysis may fail; ETH leg must still run."""
    cfg = _config()
    cfg["strategies"]["eth_macro"].update(
        {
            "buy_no_ltf_override_enabled": True,
            "dead_zone_enabled": False,
            "use_ai": False,
            "use_ai_updown": False,
            "min_liquidity": 1,
            "min_edge": 0.03,
            "entry_window_auto_align": False,
            "btc_follow_1h_required": False,
            "neutral_macro_require_spike_or_lag": False,
        }
    )
    ai = MagicMock()
    ai.research_narrative_enabled.return_value = False
    ai.research_narrative_max_calls_per_scan.return_value = 0
    ai.research_narrative_min_confidence.return_value = 1.0
    kelly = MagicMock()
    kelly.size_from_edge.return_value = 10.0
    strat = ETHMacroStrategy(cfg, ai, MagicMock(), kelly_sizer=kelly)

    eth_ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=3500.0,
            rsi_14=38.0,
            macd_15m=MACDResult(histogram=-0.05, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.02, histogram_rising=False),
        ),
        correlation=BTCSOLCorrelation(
            btc_price=100000.0,
            btc_move_5m_pct=-0.10,
            btc_move_15m_pct=-0.20,
            correlation_1h=0.8,
            sol_trend="BULLISH",
        ),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )
    strat.sol_service.get_full_analysis = MagicMock(return_value=eth_ta)
    strat.btc_service.get_full_analysis = MagicMock(return_value=None)

    market = Market(
        id="eth15b",
        question="Ethereum Up or Down - May 13, 9:00AM-9:15AM ET",
        description="ETH 15m up/down test market",
        volume=1000.0,
        liquidity=1000.0,
        yes_price=0.50,
        no_price=0.50,
        spread=0.02,
        end_date=datetime.now(timezone.utc) + timedelta(minutes=14),
        token_id_yes="yes",
        token_id_no="no",
        group_item_title="Ethereum Up or Down",
        slug="eth-updown-15m-1770000000",
    )

    signals = run_async(strat.scan_and_analyze([market], bankroll=10000.0))

    assert strat.last_scan_stats.get("abort_reason") != "analysis_unavailable"
    assert signals
    assert signals[0].action == "BUY_NO"


def test_eth_rsi_hard_gate_when_enabled():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["rsi_hard_gate_enabled"] = True
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    hard, delta = strat._resolve_rsi_gate("BUY_NO", 35.0)
    assert hard is True
    assert delta == 0.0


def test_eth_buy_no_rsi_penalty_can_be_disabled():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["rsi_soft_penalty_buy_no"] = 0.0
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    hard, delta = strat._resolve_rsi_gate("BUY_NO", 35.0)
    assert hard is False
    assert delta == 0.0


def test_eth_macro_leg_blocks_short_when_leg_is_positive():
    cfg = _config()
    cfg["strategies"]["eth_macro"].update(
        {
            "block_counter_macro_leg_updown": True,
            "updown_macro_leg_max_for_short": 0.0,
        }
    )
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())

    blocked, reason, threshold = strat._macro_leg_blocks_updown_side("SHORT", 0.12)

    assert blocked is True
    assert reason == "macro_leg_blocks_short"
    assert threshold == 0.0


def test_eth_macro_leg_allows_short_when_leg_is_negative():
    cfg = _config()
    cfg["strategies"]["eth_macro"].update(
        {
            "block_counter_macro_leg_updown": True,
            "updown_macro_leg_max_for_short": 0.0,
        }
    )
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())

    blocked, reason, _ = strat._macro_leg_blocks_updown_side("SHORT", -0.12)

    assert blocked is False
    assert reason == ""


def test_eth_macro_leg_still_blocks_long_when_leg_below_floor():
    cfg = _config()
    cfg["strategies"]["eth_macro"].update(
        {
            "block_counter_macro_leg_updown": True,
            "updown_macro_leg_min_for_long": 0.0,
        }
    )
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())

    blocked, reason, threshold = strat._macro_leg_blocks_updown_side("LONG", -0.12)

    assert blocked is True
    assert reason == "macro_leg_blocks_long"
    assert threshold == 0.0


def test_eth_uses_its_own_ai_hold_config():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    assert strat.ai_hold_veto_ttl_sec == 111
    assert strat.min_edge_5m_ai_override == 0.12


def test_eth_can_disable_5m_1h_impulse_bypass():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["btc_follow_5m_allow_1h_impulse_bypass"] = False
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    assert strat.btc_follow_5m_allow_1h_impulse_bypass is False


def test_eth_late_window_guard_blocks_and_tightens_edge():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["late_window_block_mins"] = 1.0
    cfg["strategies"]["eth_macro"]["late_window_tighten_mins"] = 3.0
    cfg["strategies"]["eth_macro"]["late_window_extra_min_edge"] = 0.14
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())

    allowed, edge_bar, reason = strat._apply_late_window_guard(
        mins_left=0.9,
        effective_min_edge=0.09,
    )
    assert allowed is False
    assert edge_bar == 0.09
    assert reason == "late_window_blocked"

    allowed2, edge_bar2, reason2 = strat._apply_late_window_guard(
        mins_left=2.4,
        effective_min_edge=0.09,
    )
    assert allowed2 is True
    assert edge_bar2 == 0.14
    assert reason2 == "late_window_edge>=0.140"


def test_eth_tuning_size_multiplier_uses_lane_config():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["tuning_size_multiplier"] = 0.6
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    assert strat.tuning_size_multiplier == 0.6


def test_eth_oracle_basis_gate_uses_eth_config():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["oracle_max_basis_bps"] = 10.0
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    assert strat._oracle_basis_blocks_entry(15.0) is True
    assert strat._oracle_basis_blocks_entry(5.0) is False


def test_eth_required_updown_oracle_validation_uses_eth_config():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["require_oracle_for_updown"] = True
    cfg["strategies"]["eth_macro"]["oracle_max_age_sec"] = 180
    cfg["strategies"]["eth_macro"]["oracle_max_basis_bps"] = 10.0
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())

    now = datetime.now(timezone.utc)
    result = strat._validate_updown_oracle(
        SOLAnalysis(
            current_price=100.2,
            chainlink_price=100.0,
            chainlink_updated_at=now - timedelta(seconds=30),
        ),
        now=now,
    )

    assert result.passed is False
    assert result.reason == "oracle_basis_block"


def test_eth_15m_follow_score_rejects_weak_above_signal_state():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    macd = MACDResult(
        macd_line=0.05,
        signal_line=0.04,
        histogram=0.01,
        histogram_rising=False,
        crossover="NONE",
    )
    score, reasons = strat._eth_15m_follow_score(macd, "LONG")
    assert score == 0.0
    assert reasons == []


def test_eth_15m_follow_score_accepts_strong_in_direction_histogram():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    macd = MACDResult(
        macd_line=0.08,
        signal_line=0.04,
        histogram=0.04,
        histogram_rising=True,
        crossover="NONE",
    )
    score, reasons = strat._eth_15m_follow_score(macd, "LONG")
    assert score == 0.05
    assert "ETH15m green+rising>0.03" in reasons


def test_eth_btc_follow_15m_requires_macd_and_candle_agreement():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    btc_ta = TechnicalAnalysis(
        current_price=90000.0,
        candle_momentum=CandleMomentum(m15_direction="DRIFT_UP"),
        macd_15m=MACDResult(histogram=0.01, histogram_rising=True, crossover="NONE"),
    )
    assert strat._btc_follow_15m_impulse_ok(btc_ta, "LONG") is False
    btc_ta.macd_15m = MACDResult(histogram=0.04, histogram_rising=True, crossover="NONE")
    assert strat._btc_follow_15m_impulse_ok(btc_ta, "LONG") is True


def test_eth_side_resolution_hybrid_keeps_alt_side_over_btc_proxy():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["direction_source"] = "hybrid"
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    side, source = strat._resolve_market_side(
        base_side="LONG",
        btc_htf_bias="BEARISH",
        market_yes_price=0.42,
    )
    assert side == "LONG"
    assert source == "hybrid_alt_first"


def test_eth_side_resolution_signal_first_toggle():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["direction_source"] = "signal_first"
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    # signal_first lets the market 15m signal set side, but only when BTC HTF
    # doesn't actively disagree (BULLISH HTF blocks SHORT, BEARISH HTF blocks LONG).
    side, source = strat._resolve_market_side(
        base_side="LONG",
        btc_htf_bias="NEUTRAL",
        market_yes_price=0.43,
    )
    assert side == "SHORT"
    assert source == "signal_first_short"

    # When BTC HTF disagrees with the market signal, fall back to base_side.
    side, source = strat._resolve_market_side(
        base_side="LONG",
        btc_htf_bias="BULLISH",
        market_yes_price=0.43,
    )
    assert side == "LONG"
    assert source == "signal_first_fallback"
