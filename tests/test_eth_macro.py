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


def test_eth_sell_yes_blocks_when_rsi_is_oversold():
    strat = ETHMacroStrategy(_config(), MagicMock(), MagicMock())
    assert strat._rsi_blocks_entry("SELL_YES", 35.0) is True
    assert strat._rsi_blocks_entry("SELL_YES", 45.0) is False


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
