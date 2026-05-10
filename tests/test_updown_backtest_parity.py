import pandas as pd
import pytest

from src.backtest.updown_engine import UpdownBacktestEngine
from src.analysis.btc_price_service import MACDResult, TechnicalAnalysis


def _config() -> dict:
    return {
        "trading": {
            "default_position_size": 10.0,
            "max_position_size": 15.0,
            "exit_rules": {
                "take_profit_pct": 0.15,
                "updown_stop_loss_pct": 0.20,
                "updown_stop_cents": 0.03,
                "updown_exit_window_mins": 2.25,
                "updown_overrides": {
                    "bitcoin": {
                        "updown_stop_cents": 0.03,
                        "updown_exit_window_mins": 2.25,
                    }
                },
            },
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


def test_updown_yes_price_proxy_moves_with_underlying_direction():
    engine = UpdownBacktestEngine(config=_config(), initial_bankroll=500.0)

    bullish = engine._proxy_yes_price_from_underlying(100.0, 100.25, window_minutes=15)
    bearish = engine._proxy_yes_price_from_underlying(100.0, 99.75, window_minutes=15)

    assert bullish > 0.50
    assert bearish < 0.50


def test_updown_live_exit_proxy_can_time_stop_near_expiry():
    cfg = _config()
    cfg["trading"]["exit_rules"]["updown_stop_loss_pct"] = 0.80
    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=5, freq="1min")
    df = pd.DataFrame(
        {
            "open_time": ts,
            "open": [100.0, 100.0, 100.0, 100.0, 100.0],
            "close": [100.0, 99.98, 99.95, 99.9, 99.85],
        }
    )

    pnl, outcome, exit_price, _, _, exit_reason = engine._settle_updown_with_live_exit_proxy(
        df_1m=df,
        window_open=ts[0],
        window_close=ts[0] + pd.Timedelta(minutes=5),
        action="BUY_YES",
        entry_price=0.48,
        size=50.0,
        asset_open=100.0,
        fill_price=0.48,
        symbol="BTC",
        window_minutes=5,
    )

    assert exit_reason == "updown_time_stop"
    assert outcome == "LOSS"
    assert exit_price < 0.48
    assert pnl < 0


def test_updown_live_exit_proxy_can_stop_loss_before_expiry_window():
    engine = UpdownBacktestEngine(config=_config(), initial_bankroll=500.0)
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=5, freq="1min")
    df = pd.DataFrame(
        {
            "open_time": ts,
            "open": [100.0] * len(ts),
            "close": [100.0, 99.90, 99.85, 99.80, 99.75],
        }
    )

    pnl, outcome, exit_price, _, _, exit_reason = engine._settle_updown_with_live_exit_proxy(
        df_1m=df,
        window_open=ts[0],
        window_close=ts[0] + pd.Timedelta(minutes=15),
        action="BUY_YES",
        entry_price=0.50,
        size=50.0,
        asset_open=100.0,
        fill_price=0.50,
        symbol="BTC",
        window_minutes=15,
    )

    assert exit_reason == "updown_stop_loss"
    assert outcome == "LOSS"
    assert exit_price < 0.50
    assert pnl < 0


def test_updown_live_exit_proxy_can_take_profit_before_settlement():
    engine = UpdownBacktestEngine(config=_config(), initial_bankroll=500.0)
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=5, freq="1min")
    df = pd.DataFrame(
        {
            "open_time": ts,
            "open": [100.0] * len(ts),
            "close": [100.0, 100.10, 100.15, 100.20, 100.25],
        }
    )

    pnl, outcome, exit_price, _, _, exit_reason = engine._settle_updown_with_live_exit_proxy(
        df_1m=df,
        window_open=ts[0],
        window_close=ts[0] + pd.Timedelta(minutes=15),
        action="BUY_YES",
        entry_price=0.50,
        size=50.0,
        asset_open=100.0,
        fill_price=0.50,
        symbol="BTC",
        window_minutes=15,
    )

    assert exit_reason == "take_profit"
    assert outcome == "WIN"
    assert exit_price > 0.50
    assert pnl > 0


def test_updown_live_exit_proxy_falls_back_to_settlement_without_time_stop():
    cfg = _config()
    cfg["trading"]["exit_rules"]["take_profit_pct"] = 0.90
    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=5, freq="1min")
    df = pd.DataFrame(
        {
            "open_time": ts,
            "open": [100.0, 100.0, 100.0, 100.0, 100.0],
            "close": [100.0, 100.02, 100.01, 100.03, 100.04],
        }
    )

    pnl, outcome, exit_price, _, _, exit_reason = engine._settle_updown_with_live_exit_proxy(
        df_1m=df,
        window_open=ts[0],
        window_close=ts[0] + pd.Timedelta(minutes=5),
        action="BUY_YES",
        entry_price=0.48,
        size=50.0,
        asset_open=100.0,
        fill_price=0.48,
        symbol="BTC",
        window_minutes=5,
    )

    assert exit_reason == "settlement_yes"
    assert outcome == "WIN"
    assert exit_price == 1.0
    assert pnl > 0


def test_eth_backtest_respects_btc_follow_1h_required_flag():
    cfg = _config()
    cfg["strategies"]["eth_macro"] = {"btc_follow_1h_required": False}

    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)

    assert engine.eth_btc_follow_1h_required is False


def test_btc_15m_edge_uses_1h_recovery_and_timing_bonus():
    engine = UpdownBacktestEngine(config=_config(), initial_bankroll=500.0)
    ta = TechnicalAnalysis(
        current_price=101.0,
        rsi_14=50.0,
        macd_4h=MACDResult(
            histogram=10.0,
            prev_histogram=12.0,
            histogram_rising=False,
            above_zero=True,
        ),
        macd_1h=MACDResult(
            histogram=2.0,
            prev_histogram=1.0,
            histogram_rising=True,
        ),
        macd_15m=MACDResult(
            macd_line=0.2,
            signal_line=0.1,
            histogram=0.03,
            prev_histogram=0.01,
            histogram_rising=True,
            above_zero=True,
        ),
    )
    ta.trend_sabre.trend = 1
    ta.trend_sabre.ma_value = 100.0
    ta.candle_momentum.m15_direction = "DRIFT_UP"
    ta.candle_momentum.m5_direction = "DRIFT_UP"
    ta.candle_momentum.m15_in_prediction_window = True

    edge, confidence = engine._edge_15m(ta, "LONG", ltf_strength=0.35, htf_bias="BULLISH")

    assert edge > 0.0
    assert confidence > 0.57
