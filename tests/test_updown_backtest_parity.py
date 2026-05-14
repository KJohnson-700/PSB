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


def _ohlcv_fixture() -> dict:
    ts_1m = pd.date_range("2026-01-01T00:00:00Z", periods=60, freq="1min")
    ts_5m = pd.date_range("2026-01-01T00:00:00Z", periods=12, freq="5min")
    ts_15m = pd.date_range("2026-01-01T00:00:00Z", periods=8, freq="15min")
    ts_1h = pd.date_range("2025-12-31T20:00:00Z", periods=8, freq="1h")
    return {
        "1m": pd.DataFrame({"open_time": ts_1m, "open": 100.0, "close": 100.1}),
        "5m": pd.DataFrame({"open_time": ts_5m, "open": 100.0, "close": 100.1}),
        "15m": pd.DataFrame({"open_time": ts_15m, "open": 100.0, "close": 100.1}),
        "1h": pd.DataFrame({"open_time": ts_1h, "open": 100.0, "close": 100.1}),
    }


def _with_recent_1m_move(data: dict, start_close: float, end_close: float) -> dict:
    out = {k: v.copy() for k, v in data.items()}
    n = len(out["1m"])
    closes = pd.Series(
        [start_close + (end_close - start_close) * i / max(n - 1, 1) for i in range(n)],
        dtype="float64",
    )
    out["1m"]["open"] = closes.shift(1).fillna(start_close)
    out["1m"]["close"] = closes
    return out


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


def test_updown_live_exit_proxy_uses_down_lane_window_override_for_buy_no():
    cfg = _config()
    cfg["trading"]["exit_rules"]["updown_stop_loss_pct"] = 0.30
    cfg["trading"]["exit_rules"]["updown_overrides"] = {
        "eth_macro": {
            "window_lane_overrides": {
                "5m": {
                    "down": {
                        "updown_stop_loss_pct": 0.14,
                    }
                }
            }
        }
    }
    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=5, freq="1min")
    df = pd.DataFrame(
        {
            "open_time": ts,
            "open": [100.0] * len(ts),
            "close": [100.0, 100.05, 100.10, 100.12, 100.15],
        }
    )

    pnl, outcome, exit_price, _, _, exit_reason = engine._settle_updown_with_live_exit_proxy(
        df_1m=df,
        window_open=ts[0],
        window_close=ts[0] + pd.Timedelta(minutes=5),
        action="BUY_NO",
        entry_price=0.50,
        size=50.0,
        asset_open=100.0,
        fill_price=0.50,
        symbol="ETH",
        window_minutes=5,
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


def test_updown_live_exit_proxy_prefers_polymarket_yes_marks_over_proxy():
    engine = UpdownBacktestEngine(config=_config(), initial_bankroll=500.0)
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=5, freq="1min")
    df = pd.DataFrame(
        {
            "open_time": ts,
            "open": [100.0] * len(ts),
            "close": [100.0] * len(ts),
        }
    )
    pm_yes = pd.Series(
        [0.50, 0.80, 0.80, 0.80, 0.80],
        index=ts,
        dtype="float64",
    )

    pnl_pm, outcome_pm, exit_price_pm, _, _, exit_reason_pm = (
        engine._settle_updown_with_live_exit_proxy(
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
            pm_yes=pm_yes,
        )
    )
    pnl_proxy, outcome_proxy, exit_price_proxy, _, _, exit_reason_proxy = (
        engine._settle_updown_with_live_exit_proxy(
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
            pm_yes=None,
        )
    )

    assert exit_reason_pm == "take_profit"
    assert outcome_pm == "WIN"
    assert exit_price_pm > 0.50
    assert pnl_pm > 0

    assert exit_reason_proxy == "unsettled"
    assert outcome_proxy == ""
    assert exit_price_proxy == 0.0
    assert pnl_proxy == 0.0


def test_updown_live_exit_proxy_uses_asof_marks_for_missing_minutes():
    cfg = _config()
    cfg["trading"]["exit_rules"]["take_profit_pct"] = 0.90
    cfg["trading"]["exit_rules"]["updown_stop_loss_pct"] = 0.80
    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    ts = pd.date_range("2026-01-01T00:00:00Z", periods=5, freq="1min")
    df = pd.DataFrame(
        {
            "open_time": ts,
            "open": [100.0] * len(ts),
            "close": [100.0] * len(ts),
        }
    )
    pm_yes_gap = pd.Series(
        [0.50, 0.44],
        index=[ts[0], ts[3]],
        dtype="float64",
    )

    pnl, outcome, exit_price, _, _, exit_reason = engine._settle_updown_with_live_exit_proxy(
        df_1m=df,
        window_open=ts[0],
        window_close=ts[0] + pd.Timedelta(minutes=5),
        action="BUY_YES",
        entry_price=0.50,
        size=50.0,
        asset_open=100.0,
        fill_price=0.50,
        symbol="BTC",
        window_minutes=5,
        pm_yes=pm_yes_gap,
    )

    assert exit_reason == "updown_time_stop"
    assert outcome == "LOSS"
    assert exit_price < 0.50
    assert pnl < 0


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
    cfg["strategies"]["eth_macro"] = {
        "btc_follow_1h_required": False,
        "entry_price_min": 0.46,
        "entry_price_max": 0.54,
        "entry_timing_window_15m_min": 2.0,
        "entry_timing_window_15m_max": 15.0,
    }

    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)

    assert engine.eth_btc_follow_1h_required is False

    ta = TechnicalAnalysis(current_price=100.0)
    engine._build_ta = lambda *args, **kwargs: ta
    engine._alt_1h_trend_from_df = lambda *args, **kwargs: "BULLISH"
    engine._sol_ltf_strength = lambda *args, **kwargs: (False, 0.0)
    engine._edge_15m_eth_follow = lambda *args, **kwargs: (0.10, 0.6)
    engine._sample_entry_price = lambda: 0.50
    engine._settle_updown_with_live_exit_proxy = lambda **kwargs: (
        5.0,
        "WIN",
        1.0,
        100.0,
        100.1,
        "settlement_yes",
    )

    result = engine.run(
        data=_ohlcv_fixture(),
        start_date="2026-01-01",
        end_date="2026-01-01",
        window_minutes=15,
        symbol="ETH",
        btc_data=None,
    )

    assert result.windows_entered > 0
    assert result.skip_counts.get("btc_data_unavailable", 0) == 0


def test_sol_backtest_uses_btc_spike_fallback_when_alt_htf_is_neutral():
    cfg = _config()
    cfg["strategies"]["sol_macro"].update(
        {
            "entry_timing_window_15m_min": 2.0,
            "entry_timing_window_15m_max": 15.0,
            "neutral_macro_require_spike_or_lag": False,
        }
    )

    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    engine._build_ta = lambda *args, **kwargs: TechnicalAnalysis(current_price=100.0)
    engine._get_sol_htf_bias = lambda *args, **kwargs: "NEUTRAL"
    engine._alt_1h_trend_from_df = lambda *args, **kwargs: "NEUTRAL"
    engine._sol_ltf_strength = lambda *args, **kwargs: (False, 0.0)
    engine._edge_15m_sol = lambda *args, **kwargs: (0.10, 0.6)
    engine._sample_entry_price = lambda: 0.50
    engine._settle_updown_with_live_exit_proxy = lambda **kwargs: (
        5.0,
        "WIN",
        1.0,
        100.0,
        100.1,
        "settlement_yes",
    )

    result = engine.run(
        data=_ohlcv_fixture(),
        btc_data=_with_recent_1m_move(_ohlcv_fixture(), 100.0, 101.2),
        start_date="2026-01-01",
        end_date="2026-01-01",
        window_minutes=15,
        symbol="SOL",
    )

    assert result.windows_entered > 0
    assert result.skip_counts.get("htf_neutral", 0) == 0


def test_sol_backtest_buy_no_override_can_flip_bullish_macro_short():
    cfg = _config()
    cfg["strategies"]["sol_macro"].update(
        {
            "buy_no_ltf_override_enabled": True,
            "entry_timing_window_15m_min": 2.0,
            "entry_timing_window_15m_max": 15.0,
        }
    )

    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    engine._build_ta = lambda *args, **kwargs: TechnicalAnalysis(current_price=100.0)
    engine._get_sol_htf_bias = lambda *args, **kwargs: "BULLISH"
    engine._sol_ltf_strength = lambda *args, **kwargs: (False, 0.0)
    engine._buy_no_ltf_override_replay = lambda *args, **kwargs: (True, "bearish_ltf_override")
    engine._edge_15m_sol = lambda *args, **kwargs: (0.10, 0.6)
    engine._sample_entry_price = lambda: 0.50
    engine._settle_updown_with_live_exit_proxy = lambda **kwargs: (
        5.0,
        "WIN",
        1.0,
        100.0,
        100.1,
        "settlement_yes",
    )

    result = engine.run(
        data=_ohlcv_fixture(),
        start_date="2026-01-01",
        end_date="2026-01-01",
        window_minutes=15,
        symbol="SOL",
    )

    assert result.windows_entered > 0
    assert result.trades[0].action == "BUY_NO"
    assert result.trades[0].htf_bias == "BULLISH"


def test_eth_backtest_can_use_btc_htf_fallback_when_eth_1h_is_neutral():
    cfg = _config()
    cfg["strategies"]["eth_macro"] = {
        "btc_follow_1h_required": False,
        "neutral_macro_require_spike_or_lag": False,
        "direction_source": "btc",
        "entry_price_min": 0.46,
        "entry_price_max": 0.54,
        "entry_timing_window_15m_min": 2.0,
        "entry_timing_window_15m_max": 15.0,
    }

    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    engine._build_ta = lambda *args, **kwargs: TechnicalAnalysis(current_price=100.0)
    engine._get_htf_bias = lambda *args, **kwargs: "BULLISH"
    engine._alt_1h_trend_from_df = lambda *args, **kwargs: "NEUTRAL"
    engine._sol_ltf_strength = lambda *args, **kwargs: (False, 0.0)
    engine._edge_15m_eth_follow = lambda *args, **kwargs: (0.10, 0.6)
    engine._sample_entry_price = lambda: 0.50
    engine._settle_updown_with_live_exit_proxy = lambda **kwargs: (
        5.0,
        "WIN",
        1.0,
        100.0,
        100.1,
        "settlement_yes",
    )

    result = engine.run(
        data=_ohlcv_fixture(),
        btc_data=_ohlcv_fixture(),
        start_date="2026-01-01",
        end_date="2026-01-01",
        window_minutes=15,
        symbol="ETH",
    )

    assert result.windows_entered > 0
    assert result.skip_counts.get("htf_neutral", 0) == 0


def test_hype_backtest_applies_hard_min_edge_floor_after_edge_calc():
    cfg = _config()
    cfg["strategies"]["hype_macro"] = {
        "hard_min_edge": 0.09,
        "entry_price_min": 0.46,
        "entry_price_max": 0.54,
        "entry_timing_window_15m_min": 2.0,
        "entry_timing_window_15m_max": 15.0,
    }

    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    engine._build_ta = lambda *args, **kwargs: TechnicalAnalysis(current_price=100.0)
    engine._get_sol_htf_bias = lambda *args, **kwargs: "BULLISH"
    engine._sol_ltf_strength = lambda *args, **kwargs: (False, 0.0)
    engine._edge_15m_sol = lambda *args, **kwargs: (0.08, 0.6)

    result = engine.run(
        data=_ohlcv_fixture(),
        start_date="2026-01-01",
        end_date="2026-01-01",
        window_minutes=15,
        symbol="HYPE",
    )

    assert result.windows_entered == 0
    assert result.skip_counts.get("hard_min_edge", 0) > 0


def test_backtest_counts_outside_entry_window_skips():
    cfg = _config()
    cfg["entry_window_15m_min"] = 2.0
    cfg["entry_window_15m_max"] = 10.0
    cfg["entry_window_auto_align"] = False

    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    engine._build_ta = lambda *args, **kwargs: TechnicalAnalysis(current_price=100.0)
    engine._get_htf_bias = lambda *args, **kwargs: "BULLISH"
    engine._ltf_strength = lambda *args, **kwargs: (False, 0.0)
    engine._edge_15m = lambda *args, **kwargs: (0.10, 0.6)

    result = engine.run(
        data=_ohlcv_fixture(),
        start_date="2026-01-01",
        end_date="2026-01-01",
        window_minutes=15,
        symbol="BTC",
    )

    assert result.windows_entered == 0
    assert result.skip_counts.get("outside_entry_window", 0) > 0


def test_backtest_counts_edge_above_cap_skips():
    cfg = _config()
    cfg["strategies"]["bitcoin"]["max_edge_updown"] = 0.12
    cfg["strategies"]["bitcoin"]["entry_timing_window_15m_min"] = 2.0
    cfg["strategies"]["bitcoin"]["entry_timing_window_15m_max"] = 15.0

    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    engine._build_ta = lambda *args, **kwargs: TechnicalAnalysis(current_price=100.0)
    engine._get_htf_bias = lambda *args, **kwargs: "BULLISH"
    engine._ltf_strength = lambda *args, **kwargs: (False, 0.0)
    engine._edge_15m = lambda *args, **kwargs: (0.20, 0.7)

    result = engine.run(
        data=_ohlcv_fixture(),
        start_date="2026-01-01",
        end_date="2026-01-01",
        window_minutes=15,
        symbol="BTC",
    )

    assert result.windows_entered == 0
    assert result.skip_counts.get("edge_above_cap", 0) > 0


def test_30m_backtest_uses_30m_entry_window_not_15m_window():
    cfg = _config()
    cfg["entry_window_15m_min"] = 2.0
    cfg["entry_window_15m_max"] = 16.0
    cfg["entry_window_30m_min"] = 16.0
    cfg["entry_window_30m_max"] = 30.0
    cfg["entry_window_auto_align"] = False
    cfg["strategies"]["bitcoin"]["entry_timing_window_30m_min"] = 16.0
    cfg["strategies"]["bitcoin"]["entry_timing_window_30m_max"] = 30.0

    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    engine._build_ta = lambda *args, **kwargs: TechnicalAnalysis(current_price=100.0)
    engine._get_htf_bias = lambda *args, **kwargs: "BULLISH"
    engine._ltf_strength = lambda *args, **kwargs: (False, 0.0)
    engine._edge_15m = lambda *args, **kwargs: (0.10, 0.6)
    engine._sample_entry_price = lambda: 0.50
    engine._settle_updown_with_live_exit_proxy = lambda **kwargs: (
        5.0,
        "WIN",
        1.0,
        100.0,
        100.1,
        "settlement_yes",
    )

    result = engine.run(
        data=_ohlcv_fixture(),
        start_date="2026-01-01",
        end_date="2026-01-01",
        window_minutes=30,
        symbol="BTC",
    )

    assert result.windows_entered > 0
    assert result.skip_counts.get("outside_entry_window", 0) == 0


def test_btc_30m_default_entry_window_allows_entries_without_explicit_30m_config():
    cfg = _config()
    cfg["entry_window_auto_align"] = False
    cfg["entry_window_align_scan_interval_sec"] = 60
    cfg["entry_window_latency_buffer_sec"] = 12

    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    engine._build_ta = lambda *args, **kwargs: TechnicalAnalysis(current_price=100.0)
    engine._get_htf_bias = lambda *args, **kwargs: "BULLISH"
    engine._ltf_strength = lambda *args, **kwargs: (False, 0.0)
    engine._edge_15m = lambda *args, **kwargs: (0.10, 0.6)
    engine._sample_entry_price = lambda: 0.50
    engine._settle_updown_with_live_exit_proxy = lambda **kwargs: (
        5.0,
        "WIN",
        1.0,
        100.0,
        100.1,
        "settlement_yes",
    )

    result = engine.run(
        data=_ohlcv_fixture(),
        start_date="2026-01-01",
        end_date="2026-01-01",
        window_minutes=30,
        symbol="BTC",
    )

    assert result.windows_entered > 0
    assert result.skip_counts.get("outside_entry_window", 0) == 0


def test_eth_edge_15m_fallback_allows_missing_btc_context_when_follow_not_required():
    engine = UpdownBacktestEngine(config=_config(), initial_bankroll=500.0)
    eth_ta = TechnicalAnalysis(
        current_price=100.0,
        macd_15m=MACDResult(
            macd_line=0.2,
            signal_line=0.1,
            histogram=0.04,
            prev_histogram=0.02,
            crossover="BULLISH_CROSS",
            histogram_rising=True,
            above_zero=True,
        ),
        rsi_14=55.0,
    )

    edge, confidence = engine._edge_15m_eth_follow(
        eth_ta,
        btc_ta=None,
        allowed_side="LONG",
        min_eth_adj=0.04,
        min_btc_hist=0.03,
    )

    assert edge > 0.0
    assert confidence >= 0.55


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
