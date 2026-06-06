"""Tests for DOGEMacroStrategy hourly BUY_YES behavior."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml

from src.analysis.math_utils import PositionSizer
from src.strategies.doge_macro import DOGEMacroStrategy
from src.strategies.sol_macro import BiasResolution


def _pos_sizer() -> PositionSizer:
    return PositionSizer(
        kelly_fraction=0.25,
        max_position_pct=0.05,
        min_position=5,
        max_position=50,
    )


def _cfg() -> dict:
    return {
        "trading": {
            "default_position_size": 5,
            "max_position_size": 50,
            "kelly_fraction": 0.25,
            "max_exposure_per_trade": 0.05,
        },
        "strategies": {
            "doge_macro": {
                "enabled": True,
                "min_liquidity": 5000,
                "min_edge": 0.08,
                "use_ai": False,
            }
        },
    }


def test_doge_macro_hourly_buy_yes_native_bonus_is_off_by_default() -> None:
    st = DOGEMacroStrategy(_cfg(), MagicMock(), _pos_sizer())
    native = BiasResolution(
        allowed_side="LONG",
        side_source="doge_1h_native",
        horizon_tf="1h",
        horizon_bias="BULLISH",
        slower_biases={},
        primary_htf_bias="BULLISH",
    )

    assert st._hourly_buy_yes_native_bonus(
        window_size="1h",
        allowed_side="LONG",
        resolution=native,
        ltf_strength=0.35,
    ) == 0.0


def test_doge_session_trial_keeps_buy_yes_and_reopens_buy_no_in_settings() -> None:
    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    doge = settings["strategies"]["doge_macro"]

    assert doge["disable_buy_yes_updown"] is False
    assert doge["disable_buy_no_5m_native"] is False
