"""Slug + loader wiring + fetch/cache behavior for Polymarket YES marks (crypto up/down backtest)."""

from unittest.mock import MagicMock, patch

import pandas as pd

import src.backtest.updown_polymarket_marks as pm_marks
from src.backtest.updown_polymarket_marks import (
    _cache_path,
    _yes_series_from_prices_df,
    build_unix_updown_slug,
    try_load_yes_series_for_window,
)


def test_build_unix_updown_slug_matches_utc_floor():
    w = pd.Timestamp("2026-06-15 14:37:22", tz="UTC")
    step = 15 * 60
    aligned = (int(w.timestamp()) // step) * step
    assert build_unix_updown_slug("BTC", w, 15) == f"btc-updown-15m-{aligned}"


def test_build_unix_updown_slug_5m_hype():
    w = pd.Timestamp("2026-01-10 08:04:00", tz="UTC")
    step = 5 * 60
    aligned = (int(w.timestamp()) // step) * step
    assert build_unix_updown_slug("HYPE", w, 5) == f"hype-updown-5m-{aligned}"


def test_try_load_disabled_returns_none(tmp_path):
    assert (
        try_load_yes_series_for_window(
            symbol="BTC",
            window_open=pd.Timestamp("2026-01-01", tz="UTC"),
            window_close=pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=15),
            window_minutes=15,
            cache_root=tmp_path,
            enabled=False,
        )
        is None
    )


def _window_15m():
    w0 = pd.Timestamp("2026-04-10 18:00:00", tz="UTC")
    w1 = w0 + pd.Timedelta(minutes=15)
    return w0, w1


def test_yes_series_from_prices_df_dedupes_sorts_clips():
    t0 = pd.Timestamp("2026-04-10 18:00:00", tz="UTC")
    df = pd.DataFrame(
        {
            "t": [t0, t0 + pd.Timedelta(minutes=1), t0],
            "price": [0.55, 1.5, 0.60],
        }
    )
    s = _yes_series_from_prices_df(df)
    assert len(s) == 2
    assert s.loc[t0] == 0.60
    assert float(s.iloc[-1]) == 1.0


def test_yes_series_from_prices_df_empty_or_malformed():
    assert len(_yes_series_from_prices_df(pd.DataFrame())) == 0
    assert len(_yes_series_from_prices_df(pd.DataFrame({"x": [1]}))) == 0


def test_try_load_no_api_key_returns_none_without_fetch(tmp_path):
    w0, w1 = _window_15m()
    with patch.object(pm_marks, "PolymarketDataLoader") as ML:
        inst = MagicMock()
        inst.api_key = None
        ML.return_value = inst
        out = try_load_yes_series_for_window(
            symbol="BTC",
            window_open=w0,
            window_close=w1,
            window_minutes=15,
            cache_root=tmp_path,
            enabled=True,
        )
    assert out is None
    inst.fetch_prices.assert_not_called()


def test_try_load_cache_hit_skips_fetch(tmp_path):
    w0, w1 = _window_15m()
    slug = build_unix_updown_slug("BTC", w0, 15)
    cpath = _cache_path(tmp_path, slug, w0, w1)
    rows = pd.DataFrame(
        {
            "t": pd.date_range(w0, periods=4, freq="1min", tz="UTC"),
            "price": [0.51, 0.52, 0.53, 0.54],
        }
    )
    rows.to_parquet(cpath, index=False)

    with patch.object(pm_marks, "PolymarketDataLoader") as ML:
        inst = MagicMock()
        inst.api_key = "test-key"
        ML.return_value = inst
        out = try_load_yes_series_for_window(
            symbol="BTC",
            window_open=w0,
            window_close=w1,
            window_minutes=15,
            cache_root=tmp_path,
            enabled=True,
        )
    assert out is not None
    assert len(out) == 4
    inst.fetch_prices.assert_not_called()


def test_try_load_cache_too_sparse_falls_through_to_fetch(tmp_path):
    w0, w1 = _window_15m()
    slug = build_unix_updown_slug("BTC", w0, 15)
    cpath = _cache_path(tmp_path, slug, w0, w1)
    sparse = pd.DataFrame(
        {
            "t": pd.date_range(w0, periods=2, freq="1min", tz="UTC"),
            "price": [0.51, 0.52],
        }
    )
    sparse.to_parquet(cpath, index=False)

    full = pd.DataFrame(
        {
            "t": pd.date_range(w0, periods=5, freq="1min", tz="UTC"),
            "price": [0.51, 0.52, 0.53, 0.54, 0.55],
        }
    )

    with patch.object(pm_marks, "PolymarketDataLoader") as ML:
        inst = MagicMock()
        inst.api_key = "test-key"
        inst.fetch_prices = MagicMock(return_value=full)
        ML.return_value = inst
        out = try_load_yes_series_for_window(
            symbol="BTC",
            window_open=w0,
            window_close=w1,
            window_minutes=15,
            cache_root=tmp_path,
            enabled=True,
            min_points=4,
        )
    assert out is not None
    assert len(out) == 5
    inst.fetch_prices.assert_called_once()


def test_try_load_fetch_sparse_returns_none(tmp_path):
    w0, w1 = _window_15m()
    thin = pd.DataFrame(
        {
            "t": pd.date_range(w0, periods=2, freq="1min", tz="UTC"),
            "price": [0.51, 0.52],
        }
    )
    with patch.object(pm_marks, "PolymarketDataLoader") as ML:
        inst = MagicMock()
        inst.api_key = "test-key"
        inst.fetch_prices = MagicMock(return_value=thin)
        ML.return_value = inst
        out = try_load_yes_series_for_window(
            symbol="BTC",
            window_open=w0,
            window_close=w1,
            window_minutes=15,
            cache_root=tmp_path,
            enabled=True,
            min_points=4,
        )
    assert out is None
    slug = build_unix_updown_slug("BTC", w0, 15)
    cpath = _cache_path(tmp_path, slug, w0, w1)
    assert not cpath.is_file()


def test_try_load_fetch_success_writes_parquet_cache(tmp_path):
    w0, w1 = _window_15m()
    slug = build_unix_updown_slug("BTC", w0, 15)
    cpath = _cache_path(tmp_path, slug, w0, w1)
    full = pd.DataFrame(
        {
            "t": pd.date_range(w0, periods=4, freq="1min", tz="UTC"),
            "price": [0.41, 0.42, 0.43, 0.44],
        }
    )
    with patch.object(pm_marks, "PolymarketDataLoader") as ML:
        inst = MagicMock()
        inst.api_key = "test-key"
        inst.fetch_prices = MagicMock(return_value=full.copy())
        ML.return_value = inst
        out = try_load_yes_series_for_window(
            symbol="BTC",
            window_open=w0,
            window_close=w1,
            window_minutes=15,
            cache_root=tmp_path,
            enabled=True,
        )
    assert out is not None
    assert cpath.is_file()
    disk = pd.read_parquet(cpath)
    assert len(disk) == 4
    assert "t" in disk.columns


def test_try_load_corrupt_cache_triggers_fetch(tmp_path):
    w0, w1 = _window_15m()
    slug = build_unix_updown_slug("BTC", w0, 15)
    cpath = _cache_path(tmp_path, slug, w0, w1)
    cpath.write_text("not a parquet file")

    full = pd.DataFrame(
        {
            "t": pd.date_range(w0, periods=4, freq="1min", tz="UTC"),
            "price": [0.31, 0.32, 0.33, 0.34],
        }
    )
    with patch.object(pm_marks, "PolymarketDataLoader") as ML:
        inst = MagicMock()
        inst.api_key = "test-key"
        inst.fetch_prices = MagicMock(return_value=full)
        ML.return_value = inst
        out = try_load_yes_series_for_window(
            symbol="BTC",
            window_open=w0,
            window_close=w1,
            window_minutes=15,
            cache_root=tmp_path,
            enabled=True,
        )
    assert out is not None
    inst.fetch_prices.assert_called_once()


def test_try_load_fetch_exception_returns_none(tmp_path):
    w0, w1 = _window_15m()
    with patch.object(pm_marks, "PolymarketDataLoader") as ML:
        inst = MagicMock()
        inst.api_key = "test-key"
        inst.fetch_prices = MagicMock(side_effect=RuntimeError("network"))
        ML.return_value = inst
        out = try_load_yes_series_for_window(
            symbol="BTC",
            window_open=w0,
            window_close=w1,
            window_minutes=15,
            cache_root=tmp_path,
            enabled=True,
        )
    assert out is None
