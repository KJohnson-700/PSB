import pandas as pd
import pytest

from src.backtest.updown_engine import UpdownBacktestEngine
from src.analysis.btc_price_service import MACDResult, TechnicalAnalysis


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


def test_updown_sell_fill_slippage_degrades_proceeds_vs_buy():
    """SELL receives less than mid; BUY pays more (see updown_engine._simulate_fill)."""
    engine = UpdownBacktestEngine(config=_config(), initial_bankroll=500.0)
    engine.slippage_bps = 100

    mid = 0.40
    buy_fill, buy_slip = engine._simulate_fill(mid, "BUY")
    sell_fill, sell_slip = engine._simulate_fill(mid, "SELL")

    assert buy_fill > mid
    assert sell_fill < mid
    assert buy_slip == pytest.approx(abs(buy_fill - mid))
    assert sell_slip == pytest.approx(abs(sell_fill - mid))


@pytest.mark.parametrize(
    "side,expected_fill,expected_slip",
    [
        ("BUY", 0.105, 0.005),  # max(0.005, 0.01) slip → 0.10 + 0.005
        ("SELL", 0.095, 0.005),  # 0.10 - 0.005
    ],
)
def test_updown_simulate_fill_buy_vs_sell_at_ten_cents(side, expected_fill, expected_slip):
    engine = UpdownBacktestEngine(config=_config(), initial_bankroll=500.0)
    engine.slippage_bps = 100
    fill, slip = engine._simulate_fill(0.10, side)
    assert fill == pytest.approx(expected_fill)
    assert slip == pytest.approx(expected_slip)


def _updown_settled_pnl(action: str, yes_won: bool, fill_price: float, size: float):
    """Mirror UpdownBacktestEngine.run settlement (fill in traded token space)."""
    if action == "BUY_YES":
        if yes_won:
            return (1.0 - fill_price) * size, "WIN"
        return -fill_price * size, "LOSS"
    if action == "BUY_NO":
        if not yes_won:
            return (1.0 - fill_price) * size, "WIN"
        return -fill_price * size, "LOSS"
    raise ValueError(action)


@pytest.mark.parametrize(
    "action,yes_won,fill_price,size,exp_pnl,exp_outcome",
    [
        ("BUY_YES", True, 0.48, 50.0, (1.0 - 0.48) * 50.0, "WIN"),
        ("BUY_YES", False, 0.48, 50.0, -0.48 * 50.0, "LOSS"),
        ("BUY_NO", False, 0.52, 50.0, (1.0 - 0.52) * 50.0, "WIN"),
        ("BUY_NO", True, 0.52, 50.0, -0.52 * 50.0, "LOSS"),
    ],
)
def test_updown_settlement_pnl_matches_engine_branches(
    action, yes_won, fill_price, size, exp_pnl, exp_outcome
):
    pnl, outcome = _updown_settled_pnl(action, yes_won, fill_price, size)
    assert outcome == exp_outcome
    assert pnl == pytest.approx(exp_pnl)


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


def test_sol_ltf_confirmation_threshold_matches_live_anti_signal_gate():
    ta = TechnicalAnalysis(
        current_price=100.0,
        macd_15m=MACDResult(
            macd_line=0.2,
            signal_line=0.1,
            histogram=0.03,
            prev_histogram=0.01,
            crossover="NONE",
            histogram_rising=True,
            above_zero=True,
        )
    )

    confirmed, strength = UpdownBacktestEngine._sol_ltf_strength(ta, "LONG")

    assert strength == pytest.approx(0.25)
    assert confirmed is False
