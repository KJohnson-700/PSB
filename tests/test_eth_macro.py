from unittest.mock import MagicMock

from src.analysis.btc_price_service import CandleMomentum, MACDResult, TechnicalAnalysis
from src.strategies.eth_macro import ETHMacroStrategy


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


def test_eth_rsi_soft_penalty_buy_no_when_oversold_not_hard_block():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    hard, delta = strat._resolve_rsi_gate("BUY_NO", 35.0)
    assert hard is False
    assert delta > 0
    hard2, delta2 = strat._resolve_rsi_gate("BUY_NO", 45.0)
    assert hard2 is False
    assert delta2 == 0.0


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


def test_eth_uses_its_own_ai_hold_config():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    assert strat.ai_hold_veto_ttl_sec == 111
    assert strat.min_edge_5m_ai_override == 0.12


def test_eth_oracle_basis_gate_uses_eth_config():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["oracle_max_basis_bps"] = 10.0
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    assert strat._oracle_basis_blocks_entry(15.0) is True
    assert strat._oracle_basis_blocks_entry(5.0) is False


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


def test_eth_side_resolution_hybrid_strong_short_override():
    cfg = _config()
    cfg["strategies"]["eth_macro"]["direction_source"] = "hybrid"
    strat = ETHMacroStrategy(cfg, MagicMock(), MagicMock())
    side, source = strat._resolve_market_side(
        base_side="LONG",
        btc_htf_bias="BEARISH",
        market_yes_price=0.42,
    )
    assert side == "SHORT"
    assert source == "hybrid_strong_short"


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
