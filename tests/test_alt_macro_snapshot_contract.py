"""Per-asset snapshot contract tests for alt macro strategies."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.analysis.sol_btc_service import BTCSOLCorrelation, MACDResult, SOLAnalysis
from src.strategies.bnb_macro import BNBMacroStrategy
from src.strategies.doge_macro import DOGEMacroStrategy
from src.strategies.eth_macro import ETHMacroStrategy
from src.strategies.hype_macro import HYPEMacroStrategy
from src.strategies.sol_macro import SolMacroStrategy
from src.strategies.xrp_macro import XRPMacroStrategy


def _config(strategy_name: str) -> dict:
    return {
        "strategies": {
            strategy_name: {
                "enabled": True,
                "min_liquidity": 1000,
                "min_edge": 0.08,
                "use_ai": False,
            },
        },
        "trading": {"dry_run": True},
        "hyperliquid": {},
    }


def _strategy(cls, strategy_name: str):
    return cls(_config(strategy_name), MagicMock(), MagicMock())


def _alt_analysis() -> SOLAnalysis:
    return SOLAnalysis(
        ema_9=103.0,
        ema_21=101.0,
        ema_50=99.0,
        rsi_14=61.25,
        macd_1h=MACDResult(
            histogram=0.14,
            histogram_rising=True,
            crossover="BULLISH_CROSS",
            above_zero=True,
        ),
        macd_30m=MACDResult(
            histogram=0.09,
            histogram_rising=True,
            crossover="NONE",
            above_zero=True,
        ),
        macd_15m=MACDResult(
            histogram=-0.04,
            histogram_rising=False,
            crossover="BEARISH_CROSS",
            above_zero=False,
        ),
        macd_5m=MACDResult(
            histogram=0.03,
            histogram_rising=True,
            crossover="NONE",
            above_zero=True,
        ),
    )


@pytest.mark.parametrize(
    ("cls", "strategy_name", "asset_code"),
    [
        (SolMacroStrategy, "sol_macro", "sol"),
        (ETHMacroStrategy, "eth_macro", "eth"),
        (XRPMacroStrategy, "xrp_macro", "xrp"),
        (HYPEMacroStrategy, "hype_macro", "hype"),
        (BNBMacroStrategy, "bnb_macro", "bnb"),
        (DOGEMacroStrategy, "doge_macro", "doge"),
    ],
)
def test_alt_macro_strategy_builds_full_fsm_snapshot_per_asset(
    cls,
    strategy_name: str,
    asset_code: str,
):
    strategy = _strategy(cls, strategy_name)
    corr = BTCSOLCorrelation(btc_move_5m_pct=0.12345, btc_move_15m_pct=-0.23456)

    snapshot = strategy._build_alt_indicator_snapshot(
        _alt_analysis(),
        correlation=corr,
        composite_score=0.81234,
        convergence_score=0.45678,
        entry_volatility=0.01234567,
    )

    assert strategy._signal_strategy_name == strategy_name
    assert strategy._alt_asset_code() == asset_code
    assert snapshot["composite_score"] == 0.8123
    assert snapshot["convergence_score"] == 0.4568
    assert snapshot["entry_volatility"] == 0.012346
    assert snapshot["alt_30m_histogram"] == 0.09
    assert snapshot["alt_15m_crossover"] == "BEARISH_CROSS"
    assert snapshot["alt_5m_above_zero"] is True
    assert snapshot["alt_ema_9"] == 103.0
    assert snapshot["alt_ema_21"] == 101.0
    assert snapshot["alt_ema_50"] == 99.0
    assert snapshot["alt_rsi_14"] == 61.25
    assert snapshot["btc_move_5m_pct"] == 0.1235
    assert snapshot["btc_move_15m_pct"] == -0.2346


def test_alt_snapshot_falls_back_to_15m_when_30m_missing():
    strategy = _strategy(SolMacroStrategy, "sol_macro")
    alt = _alt_analysis()
    delattr(alt, "macd_30m")

    snapshot = strategy._build_alt_indicator_snapshot(alt)

    assert snapshot["alt_30m_histogram"] == snapshot["alt_15m_histogram"]
    assert snapshot["alt_30m_crossover"] == snapshot["alt_15m_crossover"]
