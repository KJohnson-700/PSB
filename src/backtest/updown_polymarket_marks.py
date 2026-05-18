"""
Historical Polymarket YES marks for crypto up/down backtest exit replay (option A).

Resolves the same ``{asset}-updown-{5m|15m|30m|1h}-{unix}`` event slugs the live scanner
uses (UTC-aligned window start epoch), then pulls **1m** YES prices from the
PolymarketData API when ``POLYMARKETDATA_API_KEY`` is set.

The free CLOB ``/prices-history`` path is intentionally **not** used here: for
resolved short windows it often returns coarse buckets only (see
``PolymarketLoader.fetch_prices_history`` docstring).

When the key is missing, fetch fails, or coverage is too thin, the engine falls
back to ``_proxy_yes_price_from_underlying``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from src.backtest.data_loader import PolymarketDataLoader

logger = logging.getLogger(__name__)

_SYMBOL_TO_PREFIX = {
    "BTC": "btc",
    "SOL": "sol",
    "ETH": "eth",
    "XRP": "xrp",
    "HYPE": "hype",
}

_WARNED_NO_KEY = False
_WARNED_FETCH_FAIL: set[str] = set()
_WARNED_SPARSE: set[str] = set()


def build_unix_updown_slug(symbol: str, window_open: pd.Timestamp, window_minutes: int) -> str:
    """Gamma event slug for the UTC window containing ``window_open`` (scanner parity)."""
    prefix = _SYMBOL_TO_PREFIX.get(str(symbol).upper())
    if not prefix:
        raise ValueError(f"Unsupported symbol for Polymarket slug: {symbol}")
    w = pd.Timestamp(window_open)
    if w.tzinfo is None:
        w = w.tz_localize("UTC")
    else:
        w = w.tz_convert("UTC")
    step_s = int(window_minutes) * 60
    ts = int(w.timestamp())
    aligned = (ts // step_s) * step_s
    wm = int(window_minutes)
    seg = "1h" if wm >= 45 else f"{wm}m"
    return f"{prefix}-updown-{seg}-{aligned}"


def _cache_path(cache_root: Path, slug: str, window_open: pd.Timestamp, window_close: pd.Timestamp) -> Path:
    safe = re.sub(r"[^\w\-]+", "_", slug)
    t0 = int(pd.Timestamp(window_open).tz_convert("UTC").timestamp())
    t1 = int(pd.Timestamp(window_close).tz_convert("UTC").timestamp())
    return cache_root / f"{safe}_{t0}_{t1}.parquet"


def _yes_series_from_prices_df(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty or "t" not in df.columns:
        return pd.Series(dtype="float64")
    out = df.copy()
    out["t"] = pd.to_datetime(out["t"], utc=True)
    col = "price" if "price" in out.columns else None
    if col is None:
        return pd.Series(dtype="float64")
    s = out.set_index("t")[col].astype(float).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s.clip(0.0, 1.0)


def try_load_yes_series_for_window(
    *,
    symbol: str,
    window_open: pd.Timestamp,
    window_close: pd.Timestamp,
    window_minutes: int,
    cache_root: Path,
    enabled: bool,
    min_points: int = 4,
) -> Optional[pd.Series]:
    """Return YES mid time series indexed by UTC time, or None to use the OHLCV proxy."""
    global _WARNED_NO_KEY

    if not enabled:
        return None

    loader = PolymarketDataLoader()
    if not loader.api_key:
        if not _WARNED_NO_KEY:
            logger.info(
                "backtest.polymarket_marks.enabled is true but POLYMARKETDATA_API_KEY is unset — "
                "using underlying YES proxy for exit marks."
            )
            _WARNED_NO_KEY = True
        return None

    slug = build_unix_updown_slug(symbol, window_open, window_minutes)
    cache_root = Path(cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    cpath = _cache_path(cache_root, slug, window_open, window_close)

    if cpath.is_file():
        try:
            disk = pd.read_parquet(cpath)
            s = _yes_series_from_prices_df(disk)
            if len(s) >= min_points:
                return s
        except Exception as e:
            logger.debug("Polymarket marks cache read failed %s: %s", cpath, e)

    w0 = pd.Timestamp(window_open)
    w1 = pd.Timestamp(window_close)
    if w0.tzinfo is None:
        w0 = w0.tz_localize("UTC")
    else:
        w0 = w0.tz_convert("UTC")
    if w1.tzinfo is None:
        w1 = w1.tz_localize("UTC")
    else:
        w1 = w1.tz_convert("UTC")

    start_ts = w0.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_ts = w1.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        raw = loader.fetch_prices(slug, start_ts, end_ts, resolution="1m")
    except Exception as e:
        if slug not in _WARNED_FETCH_FAIL:
            logger.info("PolymarketData fetch_prices failed for %s: %s", slug, e)
            _WARNED_FETCH_FAIL.add(slug)
        return None

    s = _yes_series_from_prices_df(raw)
    if len(s) < min_points:
        sk = f"{slug}|{start_ts}"
        if sk not in _WARNED_SPARSE:
            logger.debug(
                "PolymarketData sparse YES for %s (%d points) — proxy fallback",
                slug,
                len(s),
            )
            _WARNED_SPARSE.add(sk)
        return None

    try:
        disk = raw.copy()
        if "t" in disk.columns:
            disk["t"] = pd.to_datetime(disk["t"], utc=True)
        disk.to_parquet(cpath, index=False)
    except Exception as e:
        logger.debug("Polymarket marks cache write failed %s: %s", cpath, e)

    return s
