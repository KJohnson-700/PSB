from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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
                "by_tf": {
                    "5m": {
                        "min_edge": 0.09,
                        "ai_override_min_edge": 0.12,
                    }
                },
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


def test_eth_15m_follow_threshold_can_relax_short_lane_only():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["eth_follow_15m_min_adj"] = 0.04
    cfg["strategies"]["eth_macro"]["eth_follow_15m_min_adj_short"] = 0.03
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strat._eth_follow_15m_required_adj("LONG") == 0.04
    assert strat._eth_follow_15m_required_adj("SHORT") == 0.03


def test_eth_direction_guard_blocks_5m_buy_no_when_btc_1h_bull_and_no_too_cheap():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    decision = strat._resolve_eth_direction(
        market_allowed_side="SHORT",
        side_source="eth_5m_native",
        raw_est_prob=0.42,
        momentum_bias="BEARISH",
    )

    reason = strat._eth_direction_guard_reason(
        window_size="5m",
        decision=decision,
        yes_price=0.72,
        btc_htf_bias="BEARISH",
        btc_1h_regime="BULL",
        alt_h1_trend="BEARISH",
        rsi_14=42.0,
    )

    assert decision.action == "BUY_NO"
    assert reason == "eth_5m_bull_regime_expensive_short"


def test_eth_direction_guard_blocks_15m_overbought_long_when_btc_bearish_and_alt_neutral():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    decision = strat._resolve_eth_direction(
        market_allowed_side="LONG",
        side_source="eth_15m_native",
        raw_est_prob=0.59,
        momentum_bias="BULLISH",
    )

    reason = strat._eth_direction_guard_reason(
        window_size="15m",
        decision=decision,
        yes_price=0.48,
        btc_htf_bias="BEARISH",
        btc_1h_regime="BULL",
        alt_h1_trend="NEUTRAL",
        rsi_14=71.0,
    )

    assert decision.action == "BUY_YES"
    assert reason == "eth_15m_overbought_long_vs_btc"


def test_eth_liquidity_floor_is_lane_aware_by_window_and_side():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["min_liquidity_15m_buy_no"] = 2200
    cfg["strategies"]["eth_macro"]["min_liquidity_1h"] = 3500
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())

    assert strat._resolve_min_liquidity_floor(window_size="15m", action="BUY_NO") == 2200
    assert strat._resolve_min_liquidity_floor(window_size="1h", action="BUY_YES") == 3500


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


def test_eth_resolver_picks_long_on_bullish_rally_under_bull_macro():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["buy_no_ltf_override_enabled"] = True
    cfg["strategies"]["eth_macro"]["buy_yes_ltf_override_enabled"] = True
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=62.0,
            macd_15m=MACDResult(histogram=0.06, histogram_rising=True),
            macd_5m=MACDResult(histogram=0.04, histogram_rising=True),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=0.05),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )

    side, source, _ = strat._resolve_allowed_side_with_ltf_overrides(ta, "BULLISH")

    assert side == "LONG"
    assert source == "bullish_rally_default"


def test_eth_resolver_bull_default_long_fires_even_without_rally_confirmation():
    """Additive-only: BULL default LONG cannot be blocked by weak LTF."""
    cfg = _config()
    cfg["strategies"]["eth_macro"]["buy_no_ltf_override_enabled"] = True
    cfg["strategies"]["eth_macro"]["buy_yes_ltf_override_enabled"] = True
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            rsi_14=50.0,
            macd_15m=MACDResult(histogram=0.01, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.01, histogram_rising=False),
        ),
        correlation=BTCSOLCorrelation(btc_move_5m_pct=0.0),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )

    side, source, _ = strat._resolve_allowed_side_with_ltf_overrides(ta, "BULLISH")

    assert side == "LONG"
    assert source == "bullish_rally_default"


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


def test_eth_oracle_basis_block_is_logged_to_rejected_candidates():
    cfg = _config()
    cfg["strategies"]["eth_macro"].update(
        {
            "dead_zone_enabled": False,
            "use_ai": False,
            "use_ai_updown": False,
            "min_liquidity": 1,
            "min_edge": 0.03,
            "entry_window_auto_align": False,
            "oracle_max_basis_bps": 10.0,
        }
    )
    ai = MagicMock()
    ai.research_narrative_enabled.return_value = False
    ai.research_narrative_max_calls_per_scan.return_value = 0
    ai.research_narrative_min_confidence.return_value = 1.0
    kelly = MagicMock()
    kelly.size_from_edge.return_value = 10.0
    strat = ETHMacroStrategy(cfg, ai, MagicMock(), kelly_sizer=kelly)
    strat._get_btc_htf_bias = MagicMock(return_value="BEARISH")

    eth_ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=3507.0,
            chainlink_price=3500.0,
            chainlink_updated_at=datetime.now(timezone.utc) - timedelta(seconds=30),
            oracle_basis_bps=20.0,
            rsi_14=38.0,
            macd_15m=MACDResult(histogram=-0.05, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.02, histogram_rising=False),
        ),
        correlation=BTCSOLCorrelation(
            btc_price=100000.0,
            btc_move_5m_pct=-0.10,
            btc_move_15m_pct=-0.20,
            correlation_1h=0.8,
            sol_trend="BEARISH",
        ),
        multi_tf=MultiTimeframeTrend(h1_trend="BEARISH"),
    )
    btc_ta = TechnicalAnalysis(
        current_price=100000.0,
        macd_1h=MACDResult(histogram=-20.0, histogram_rising=False),
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
        id="eth15_oracle",
        question="Ethereum Up or Down - May 13, 9:00AM-9:15AM ET",
        description="ETH 15m oracle block test market",
        volume=1000.0,
        liquidity=1000.0,
        yes_price=0.50,
        no_price=0.50,
        spread=0.02,
        end_date=datetime.now(timezone.utc) + timedelta(minutes=14),
        token_id_yes="yes",
        token_id_no="no",
        group_item_title="Ethereum Up or Down",
        slug="eth-updown-15m-1770000001",
    )

    with patch("src.strategies.eth_macro.log_rejected_candidate") as mock_log:
        signals = run_async(strat.scan_and_analyze([market], bankroll=10000.0))

    assert signals == []
    assert mock_log.call_count == 1
    assert mock_log.call_args.kwargs["reason"] == "oracle_basis_block"
    assert mock_log.call_args.kwargs["policy_version"] == "oracle_basis_block_v1"
    assert any(
        p.get("probe") == "oracle_basis_abs_bps"
        for p in mock_log.call_args.kwargs["probe_variants"]
    )


def test_eth_scan_uses_relaxed_oracle_basis_policy():
    cfg = _config()
    cfg["strategies"]["eth_macro"].update(
        {
            "dead_zone_enabled": False,
            "use_ai": False,
            "use_ai_updown": False,
            "min_liquidity": 1,
            "min_edge": 0.03,
            "entry_window_auto_align": False,
            "oracle_max_basis_bps": 10.0,
            "oracle_basis_relax_max_bps": 12.0,
        }
    )
    ai = MagicMock()
    ai.research_narrative_enabled.return_value = False
    ai.research_narrative_max_calls_per_scan.return_value = 0
    ai.research_narrative_min_confidence.return_value = 1.0
    kelly = MagicMock()
    kelly.size_from_edge.return_value = 10.0
    strat = ETHMacroStrategy(cfg, ai, MagicMock(), kelly_sizer=kelly)
    strat._get_btc_htf_bias = MagicMock(return_value="BEARISH")

    eth_ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=3503.85,
            chainlink_price=3500.0,
            chainlink_updated_at=datetime.now(timezone.utc) - timedelta(seconds=30),
            oracle_basis_bps=11.0,
            rsi_14=38.0,
            macd_15m=MACDResult(histogram=-0.05, histogram_rising=False),
            macd_5m=MACDResult(histogram=-0.02, histogram_rising=False),
        ),
        correlation=BTCSOLCorrelation(
            btc_price=100000.0,
            btc_move_5m_pct=-0.10,
            btc_move_15m_pct=-0.20,
            correlation_1h=0.8,
            sol_trend="BEARISH",
        ),
        multi_tf=MultiTimeframeTrend(h1_trend="BEARISH"),
    )
    btc_ta = TechnicalAnalysis(
        current_price=100000.0,
        macd_1h=MACDResult(histogram=-20.0, histogram_rising=False),
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
        id="eth15_oracle_relaxed",
        question="Ethereum Up or Down - May 13, 9:00AM-9:15AM ET",
        description="ETH 15m relaxed oracle test market",
        volume=1000.0,
        liquidity=1000.0,
        yes_price=0.50,
        no_price=0.50,
        spread=0.02,
        end_date=datetime.now(timezone.utc) + timedelta(minutes=14),
        token_id_yes="yes",
        token_id_no="no",
        group_item_title="Ethereum Up or Down",
        slug="eth-updown-15m-1770000002",
    )

    with patch("src.strategies.eth_macro.log_rejected_candidate") as mock_log:
        signals = run_async(strat.scan_and_analyze([market], bankroll=10000.0))

    assert len(signals) == 1
    assert signals[0].action == "BUY_NO"
    mock_log.assert_not_called()


def test_eth_lane_entry_window_is_logged_to_rejected_candidates():
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
        id="eth15_window",
        question="Ethereum Up or Down - May 13, 9:00AM-9:15AM ET",
        description="ETH 15m lane window test market",
        volume=1000.0,
        liquidity=1000.0,
        yes_price=0.50,
        no_price=0.50,
        spread=0.02,
        end_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        token_id_yes="yes",
        token_id_no="no",
        group_item_title="Ethereum Up or Down",
        slug="eth-updown-15m-1770000002",
    )

    with patch("src.strategies.eth_macro.log_rejected_candidate") as mock_log:
        signals = run_async(strat.scan_and_analyze([market], bankroll=10000.0))

    assert signals == []
    assert mock_log.call_count == 1
    assert mock_log.call_args.kwargs["reason"] == "lane_entry_window"
    assert mock_log.call_args.kwargs["policy_version"] == "lane_entry_window_v1"
    assert any(
        p.get("probe") == "entry_window_mins_left"
        for p in mock_log.call_args.kwargs["probe_variants"]
    )


def test_eth_liquidity_reject_can_feed_shadow_observer():
    cfg = _config()
    cfg["strategies"]["eth_macro"].update(
        {
            "dead_zone_enabled": False,
            "use_ai": True,
            "use_ai_updown": True,
            "min_liquidity": 5000,
        }
    )
    ai = MagicMock()
    ai.research_narrative_enabled.return_value = False
    ai.research_narrative_max_calls_per_scan.return_value = 0
    ai.research_narrative_min_confidence.return_value = 1.0
    ai.shadow_observer_enabled.return_value = True
    ai.shadow_observer_max_calls_per_scan.return_value = 1
    ai.observe_rejected_candidate = AsyncMock(return_value={"ok": True})
    strat = ETHMacroStrategy(cfg, ai, MagicMock(), kelly_sizer=MagicMock())
    strat._get_btc_htf_bias = MagicMock(return_value="BULLISH")

    eth_ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=3500.0,
            rsi_14=55.0,
            macd_15m=MACDResult(histogram=0.05, histogram_rising=True),
            macd_5m=MACDResult(histogram=0.02, histogram_rising=True),
        ),
        correlation=BTCSOLCorrelation(
            btc_price=100000.0,
            btc_move_5m_pct=0.10,
            btc_move_15m_pct=0.20,
            correlation_1h=0.8,
            sol_trend="BULLISH",
        ),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )
    btc_ta = TechnicalAnalysis(
        current_price=100000.0,
        macd_1h=MACDResult(histogram=20.0, histogram_rising=True),
        macd_15m=MACDResult(histogram=0.05, histogram_rising=True),
        candle_momentum=CandleMomentum(
            m15_direction="DRIFT_UP",
            m5_direction="DRIFT_UP",
            m5_move_pct=0.1,
        ),
    )
    strat.sol_service.get_full_analysis = MagicMock(return_value=eth_ta)
    strat.btc_service.get_full_analysis = MagicMock(return_value=btc_ta)

    market = Market(
        id="eth15_liquidity_observer",
        question="Ethereum Up or Down - May 13, 9:00AM-9:15AM ET",
        description="ETH 15m liquidity observer test",
        volume=1000.0,
        liquidity=1000.0,
        yes_price=0.50,
        no_price=0.50,
        spread=0.02,
        end_date=datetime.now(timezone.utc) + timedelta(minutes=14),
        token_id_yes="yes",
        token_id_no="no",
        group_item_title="Ethereum Up or Down",
        slug="eth-updown-15m-1770000003",
    )

    signals = run_async(strat.scan_and_analyze([market], bankroll=10000.0))

    assert signals == []
    ai.observe_rejected_candidate.assert_awaited_once()
    assert strat.last_scan_stats["shadow_observer_calls"] == 1
    assert strat.last_scan_stats["shadow_observer_ok"] == 1


def test_eth_shadow_observer_timeout_consumes_scan_budget():
    cfg = _config()
    cfg["strategies"]["eth_macro"].update(
        {
            "dead_zone_enabled": False,
            "use_ai": True,
            "use_ai_updown": True,
            "min_liquidity": 5000,
        }
    )
    ai = MagicMock()
    ai.shadow_pipeline_enabled.return_value = False
    ai.research_narrative_enabled.return_value = False
    ai.research_narrative_max_calls_per_scan.return_value = 0
    ai.research_narrative_min_confidence.return_value = 1.0
    ai.shadow_observer_enabled.return_value = True
    ai.shadow_observer_max_calls_per_scan.return_value = 1
    strat = ETHMacroStrategy(cfg, ai, MagicMock(), kelly_sizer=MagicMock())
    strat._observe_rejected_candidate_with_timeout = AsyncMock(return_value=None)
    strat._get_btc_htf_bias = MagicMock(return_value="BULLISH")

    eth_ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=3500.0,
            rsi_14=55.0,
            macd_15m=MACDResult(histogram=0.05, histogram_rising=True),
            macd_5m=MACDResult(histogram=0.02, histogram_rising=True),
        ),
        correlation=BTCSOLCorrelation(
            btc_price=100000.0,
            btc_move_5m_pct=0.10,
            btc_move_15m_pct=0.20,
            correlation_1h=0.8,
            sol_trend="BULLISH",
        ),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )
    btc_ta = TechnicalAnalysis(
        current_price=100000.0,
        macd_1h=MACDResult(histogram=20.0, histogram_rising=True),
        macd_15m=MACDResult(histogram=0.05, histogram_rising=True),
        candle_momentum=CandleMomentum(
            m15_direction="DRIFT_UP",
            m5_direction="DRIFT_UP",
            m5_move_pct=0.1,
        ),
    )
    strat.sol_service.get_full_analysis = MagicMock(return_value=eth_ta)
    strat.btc_service.get_full_analysis = MagicMock(return_value=btc_ta)

    def _market(idx: int) -> Market:
        return Market(
            id=f"eth15_liquidity_timeout_{idx}",
            question=f"Ethereum Up or Down - May 13, 9:0{idx}AM-9:15AM ET",
            description="ETH 15m liquidity observer timeout test",
            volume=1000.0,
            liquidity=1000.0,
            yes_price=0.50,
            no_price=0.50,
            spread=0.02,
            end_date=datetime.now(timezone.utc) + timedelta(minutes=14),
            token_id_yes=f"yes-{idx}",
            token_id_no=f"no-{idx}",
            group_item_title="Ethereum Up or Down",
            slug=f"eth-updown-15m-17700000{idx}",
        )

    signals = run_async(strat.scan_and_analyze([_market(1), _market(2)], bankroll=10000.0))

    assert signals == []
    strat._observe_rejected_candidate_with_timeout.assert_awaited_once()
    assert strat.last_scan_stats["shadow_observer_calls"] == 1
    assert strat.last_scan_stats["shadow_observer_ok"] == 0


def test_eth_shadow_observer_skips_repeated_market_during_cooldown():
    cfg = _config()
    cfg["strategies"]["eth_macro"].update(
        {
            "dead_zone_enabled": False,
            "use_ai": True,
            "use_ai_updown": True,
            "min_liquidity": 5000,
            "ai_observer_retry_cooldown_sec": 300,
        }
    )
    ai = MagicMock()
    ai.shadow_pipeline_enabled.return_value = False
    ai.research_narrative_enabled.return_value = False
    ai.research_narrative_max_calls_per_scan.return_value = 0
    ai.research_narrative_min_confidence.return_value = 1.0
    ai.shadow_observer_enabled.return_value = True
    ai.shadow_observer_max_calls_per_scan.return_value = 1
    strat = ETHMacroStrategy(cfg, ai, MagicMock(), kelly_sizer=MagicMock())
    strat._observe_rejected_candidate_with_timeout = AsyncMock(return_value=None)
    strat._get_btc_htf_bias = MagicMock(return_value="BULLISH")

    eth_ta = SOLTechnicalAnalysis(
        sol=SOLAnalysis(
            current_price=3500.0,
            rsi_14=55.0,
            macd_15m=MACDResult(histogram=0.05, histogram_rising=True),
            macd_5m=MACDResult(histogram=0.02, histogram_rising=True),
        ),
        correlation=BTCSOLCorrelation(
            btc_price=100000.0,
            btc_move_5m_pct=0.10,
            btc_move_15m_pct=0.20,
            correlation_1h=0.8,
            sol_trend="BULLISH",
        ),
        multi_tf=MultiTimeframeTrend(h1_trend="BULLISH"),
    )
    btc_ta = TechnicalAnalysis(
        current_price=100000.0,
        macd_1h=MACDResult(histogram=20.0, histogram_rising=True),
        macd_15m=MACDResult(histogram=0.05, histogram_rising=True),
        candle_momentum=CandleMomentum(
            m15_direction="DRIFT_UP",
            m5_direction="DRIFT_UP",
            m5_move_pct=0.1,
        ),
    )
    strat.sol_service.get_full_analysis = MagicMock(return_value=eth_ta)
    strat.btc_service.get_full_analysis = MagicMock(return_value=btc_ta)

    market = Market(
        id="eth15_liquidity_cooldown",
        question="Ethereum Up or Down - May 13, 9:00AM-9:15AM ET",
        description="ETH 15m liquidity observer cooldown test",
        volume=1000.0,
        liquidity=1000.0,
        yes_price=0.50,
        no_price=0.50,
        spread=0.02,
        end_date=datetime.now(timezone.utc) + timedelta(minutes=14),
        token_id_yes="yes",
        token_id_no="no",
        group_item_title="Ethereum Up or Down",
        slug="eth-updown-15m-1770000003",
    )

    run_async(strat.scan_and_analyze([market], bankroll=10000.0))
    run_async(strat.scan_and_analyze([market], bankroll=10000.0))

    strat._observe_rejected_candidate_with_timeout.assert_awaited_once()


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
