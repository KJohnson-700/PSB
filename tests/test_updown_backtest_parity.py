import pandas as pd
import pytest

from src.backtest.updown_engine import UpdownBacktestEngine


def _config() -> dict:
    return {
        "trading": {
            "default_position_size": 10.0,
            "max_position_size": 15.0,
        },
        "exposure": {
            "min_trade_usd": 25.0,
            "full_size": 15.0,
        },
        "strategies": {
            "bitcoin": {"kelly_fraction": 0.15, "entry_price_min_updown": 0.46, "entry_price_max_updown": 0.54},
            "sol_macro": {"kelly_fraction": 0.15, "entry_price_min": 0.46, "entry_price_max": 0.54},
        },
        "backtest": {},
    }


def test_updown_fill_uses_additive_slippage_floor():
    engine = UpdownBacktestEngine(config=_config(), initial_bankroll=500.0)
    engine.slippage_bps = 100

    fill, slip = engine._simulate_fill(0.10, "BUY")

    assert fill == pytest.approx(0.105)
    assert slip == pytest.approx(0.005)


def test_updown_flat_candle_is_unsettled_not_no_win():
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=15, freq="1min")
    df = pd.DataFrame(
        {
            "open_time": ts,
            "open": [100.0] * len(ts),
            "close": [100.0] * len(ts),
        }
    )

    yes_won, open_price, close_price = UpdownBacktestEngine._settle(
        df,
        pd.Timestamp("2026-01-01T00:00:00Z"),
        pd.Timestamp("2026-01-01T00:15:00Z"),
    )

    assert yes_won is None
    assert open_price == close_price == 100.0


def test_updown_sizing_approximates_live_full_tier_floor_and_cap():
    engine = UpdownBacktestEngine(config=_config(), initial_bankroll=500.0)
    engine.kelly_fraction = 0.15

    assert engine._size_position(bankroll=500.0, edge=0.10) == 15.0
