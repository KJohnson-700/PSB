"""Oracle replay tests for crypto backtests."""

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from src.backtest.oracle_loader import OracleHistoryLoader
from src.backtest.updown_engine import UpdownBacktestEngine


def _base_config() -> dict:
    return {
        "trading": {
            "default_position_size": 10.0,
            "max_position_size": 15.0,
        },
        "strategies": {
            "bitcoin": {"kelly_fraction": 0.15, "min_4h_hist_magnitude": 20.0},
            "sol_macro": {"kelly_fraction": 0.15},
            "eth_macro": {
                "kelly_fraction": 0.15,
                "btc_follow_1h_hist_min": 8.0,
                "btc_follow_15m_hist_min": 0.03,
                "eth_follow_15m_min_adj": 0.05,
                "oracle_max_basis_bps": 10.0,
            },
            "xrp_macro": {"kelly_fraction": 0.15},
            "hype_macro": {"kelly_fraction": 0.15},
        },
        "backtest": {
            "min_edge_eth_15m": 0.01,
            "min_edge_sol_15m": 0.01,
        },
    }


def _ohlcv_1m() -> pd.DataFrame:
    ts = pd.date_range("2025-01-01T00:00:00Z", periods=15, freq="1min")
    return pd.DataFrame(
        {
            "open_time": ts,
            "close_time": ts,
            "open": [100.0] * len(ts),
            "high": [101.0] * len(ts),
            "low": [99.0] * len(ts),
            "close": [100.5] * len(ts),
            "volume": [1.0] * len(ts),
        }
    )


def _blank_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time": pd.to_datetime([], utc=True),
            "close_time": pd.to_datetime([], utc=True),
            "open": pd.Series(dtype=float),
            "high": pd.Series(dtype=float),
            "low": pd.Series(dtype=float),
            "close": pd.Series(dtype=float),
            "volume": pd.Series(dtype=float),
        }
    )


def _oracle_df(price: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "updated_at": pd.to_datetime(["2024-12-31T23:55:00Z"], utc=True),
            "price": [price],
            "round_id": [1],
            "network": ["polygon"],
            "address": ["0xfeed"],
        }
    )


def _eth_ta(price: float):
    return SimpleNamespace(current_price=price)


def _btc_ta():
    return SimpleNamespace(current_price=95000.0)


def test_oracle_loader_resolves_eth_feed():
    loader = OracleHistoryLoader()
    spec = loader.resolve_feed("ETH")
    assert spec is not None
    assert spec.symbol == "ETHUSDT"
    assert spec.network == "polygon"
    assert spec.address.startswith("0x")


def test_eth_backtest_skips_when_oracle_basis_exceeds_cap():
    engine = UpdownBacktestEngine(config=_base_config(), initial_bankroll=500.0)
    data = {"1m": _ohlcv_1m(), "5m": _blank_df(), "15m": _blank_df(), "1h": _blank_df()}
    btc_data = {"1m": _blank_df(), "5m": _blank_df(), "15m": _blank_df(), "4h": _blank_df()}

    with patch.object(
        engine,
        "_build_ta",
        side_effect=lambda t, dataset, htf_key: _btc_ta() if dataset is btc_data else _eth_ta(2000.0),
    ), patch.object(engine, "_get_htf_bias", return_value="BULLISH"), patch.object(
        engine, "_eth_follow_1h_ok", return_value=True
    ), patch.object(engine, "_sol_ltf_strength", return_value=(False, 0.0)), patch.object(
        engine, "_edge_15m_eth_follow", return_value=(0.10, 0.60)
    ), patch.object(engine, "_sample_entry_price", return_value=0.5):
        result = engine.run(
            data=data,
            start_date="2025-01-01",
            end_date="2025-01-01",
            window_minutes=15,
            symbol="ETH",
            btc_data=btc_data,
            oracle_history=_oracle_df(1990.0),
        )

    assert result.oracle_history_loaded is True
    assert result.oracle_basis_skips > 0
    assert result.windows_entered == 0


def test_eth_backtest_enters_when_oracle_basis_is_within_cap():
    engine = UpdownBacktestEngine(config=_base_config(), initial_bankroll=500.0)
    data = {"1m": _ohlcv_1m(), "5m": _blank_df(), "15m": _blank_df(), "1h": _blank_df()}
    btc_data = {"1m": _blank_df(), "5m": _blank_df(), "15m": _blank_df(), "4h": _blank_df()}

    with patch.object(
        engine,
        "_build_ta",
        side_effect=lambda t, dataset, htf_key: _btc_ta() if dataset is btc_data else _eth_ta(2000.0),
    ), patch.object(engine, "_get_htf_bias", return_value="BULLISH"), patch.object(
        engine, "_eth_follow_1h_ok", return_value=True
    ), patch.object(engine, "_sol_ltf_strength", return_value=(False, 0.0)), patch.object(
        engine, "_edge_15m_eth_follow", return_value=(0.10, 0.60)
    ), patch.object(engine, "_sample_entry_price", return_value=0.5):
        result = engine.run(
            data=data,
            start_date="2025-01-01",
            end_date="2025-01-01",
            window_minutes=15,
            symbol="ETH",
            btc_data=btc_data,
            oracle_history=_oracle_df(1999.5),
        )

    assert result.oracle_history_loaded is True
    assert result.oracle_basis_skips == 0
    assert result.windows_entered == 1


def test_symbols_without_basis_cap_keep_existing_behavior():
    cfg = _base_config()
    cfg["strategies"]["sol_macro"].pop("oracle_max_basis_bps", None)
    engine = UpdownBacktestEngine(config=cfg, initial_bankroll=500.0)
    data = {"1m": _ohlcv_1m(), "5m": _blank_df(), "15m": _blank_df(), "1h": _blank_df()}

    with patch.object(engine, "_build_ta", return_value=_eth_ta(200.0)), patch.object(
        engine, "_get_sol_htf_bias", return_value="BULLISH"
    ), patch.object(engine, "_sol_ltf_strength", return_value=(False, 0.0)), patch.object(
        engine, "_edge_15m_sol", return_value=(0.08, 0.60)
    ), patch.object(engine, "_sample_entry_price", return_value=0.5):
        result = engine.run(
            data=data,
            start_date="2025-01-01",
            end_date="2025-01-01",
            window_minutes=15,
            symbol="SOL",
            oracle_history=_oracle_df(150.0),
        )

    assert result.oracle_history_loaded is True
    assert result.oracle_basis_skips == 0
    assert result.windows_entered == 1
