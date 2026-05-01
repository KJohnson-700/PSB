"""
Hyperliquid HYPE service adapter.

Provides the SOLBTCService interface but sources the alt-coin leg (HYPE) from
Hyperliquid's public candleSnapshot endpoint instead of Binance.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

import pandas as pd
import requests

from src.analysis.sol_btc_service import SOLBTCService

logger = logging.getLogger(__name__)


class HyperliquidHypeService(SOLBTCService):
    """SOLBTCService-compatible adapter for HYPE candles from Hyperliquid."""

    HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
    HYPE_COIN = "HYPE"
    _INTERVAL_MAP = {
        "1m": ("1m", 60_000),
        "5m": ("5m", 300_000),
        "15m": ("15m", 900_000),
        "1h": ("1h", 3_600_000),
        "4h": ("4h", 14_400_000),
        "1d": ("1d", 86_400_000),
    }

    def __init__(
        self,
        polygon_rpc: str = None,
        alt_symbol: str = "HYPEUSDT",
        *,
        dynamic_beta_min: float = 0.8,
        dynamic_beta_max: float = 3.0,
        dynamic_beta_extreme_max: float = 5.0,
        btc_spike_floor_pct_5m: float = 0.3,
        btc_spike_floor_pct_15m: float = 0.8,
        lag_signal_min_pct: float = 0.2,
    ):
        super().__init__(
            polygon_rpc=polygon_rpc,
            alt_symbol=alt_symbol,
            dynamic_beta_min=dynamic_beta_min,
            dynamic_beta_max=dynamic_beta_max,
            dynamic_beta_extreme_max=dynamic_beta_extreme_max,
            btc_spike_floor_pct_5m=btc_spike_floor_pct_5m,
            btc_spike_floor_pct_15m=btc_spike_floor_pct_15m,
            lag_signal_min_pct=lag_signal_min_pct,
        )
        self._hype_cache: Dict[str, Tuple[float, pd.DataFrame]] = {}
        self._hype_cache_ttl = 30  # seconds

    def _fetch_hype_klines(self, interval: str, limit: int) -> pd.DataFrame:
        """Fetch HYPE candles from Hyperliquid (for live non-backtest use)."""
        mapped = self._INTERVAL_MAP.get(interval)
        if not mapped:
            logger.warning(f"Hyperliquid HYPE unsupported interval: {interval}")
            return pd.DataFrame()

        hl_interval, interval_ms = mapped
        now_ms = int(time.time() * 1000)
        lookback_bars = max(5, min(500, limit + 5))
        start_ms = now_ms - (lookback_bars * interval_ms)

        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": self.HYPE_COIN,
                "interval": hl_interval,
                "startTime": start_ms,
                "endTime": now_ms,
            },
        }

        try:
            resp = requests.post(self.HYPERLIQUID_INFO_URL, json=payload, timeout=10)
            resp.raise_for_status()
            rows = resp.json() or []
            if not isinstance(rows, list) or not rows:
                return pd.DataFrame()

            parsed = []
            for row in rows:
                try:
                    open_ms = int(row.get("t"))
                    close_ms = int(row.get("T", open_ms + interval_ms))
                    parsed.append(
                        {
                            "open_time": pd.to_datetime(open_ms, unit="ms"),
                            "open": float(row.get("o", 0.0)),
                            "high": float(row.get("h", 0.0)),
                            "low": float(row.get("l", 0.0)),
                            "close": float(row.get("c", 0.0)),
                            "volume": float(row.get("v", 0.0)),
                            "close_time": pd.to_datetime(close_ms, unit="ms"),
                        }
                    )
                except Exception:
                    continue

            if not parsed:
                return pd.DataFrame()

            df = pd.DataFrame(parsed).sort_values("open_time").drop_duplicates(subset=["open_time"])
            return df.tail(limit).reset_index(drop=True)
        except Exception as e:
            logger.error(f"Hyperliquid HYPE candles unavailable ({interval}): {e}")
            return pd.DataFrame()

    def fetch_klines_range(
        self,
        interval: str = "1h",
        start_date: str = None,
        end_date: str = None,
        limit: int = 2000,
    ) -> pd.DataFrame:
        """Fetch HYPE klines for a date range (used by backtest OHLCV loader).

        Hyperliquid's candleSnapshot does not support startTime/endTime filtering
        — it returns the most recent N candles. For backtesting we fetch a large
        lookback and filter client-side to the requested window.
        """
        mapped = self._INTERVAL_MAP.get(interval)
        if not mapped:
            logger.warning(f"Hyperliquid HYPE unsupported interval: {interval}")
            return pd.DataFrame()

        hl_interval, interval_ms = mapped
        now_ms = int(time.time() * 1000)
        lookback_ms = min(90 * 24 * 3600 * 1000, now_ms)
        start_ms = now_ms - lookback_ms

        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": self.HYPE_COIN,
                "interval": hl_interval,
                "startTime": start_ms,
                "endTime": now_ms,
            },
        }

        try:
            resp = requests.post(self.HYPERLIQUID_INFO_URL, json=payload, timeout=15)
            resp.raise_for_status()
            rows = resp.json() or []
            if not isinstance(rows, list) or not rows:
                return pd.DataFrame()

            parsed = []
            for row in rows:
                try:
                    open_ms = int(row.get("t"))
                    close_ms = int(row.get("T", open_ms + interval_ms))
                    parsed.append(
                        {
                            "open_time": pd.to_datetime(open_ms, unit="ms"),
                            "open": float(row.get("o", 0.0)),
                            "high": float(row.get("h", 0.0)),
                            "low": float(row.get("l", 0.0)),
                            "close": float(row.get("c", 0.0)),
                            "volume": float(row.get("v", 0.0)),
                            "close_time": pd.to_datetime(close_ms, unit="ms"),
                        }
                    )
                except Exception:
                    continue

            if not parsed:
                return pd.DataFrame()

            df = pd.DataFrame(parsed).sort_values("open_time").drop_duplicates(subset=["open_time"])
            df = df.reset_index(drop=True)

            if start_date:
                start_dt = pd.to_datetime(start_date, utc=True)
                df = df[df["open_time"].dt.tz_localize(None) >= start_dt.replace(tzinfo=None)]
            if end_date:
                end_dt = pd.to_datetime(end_date, utc=True)
                df = df[df["open_time"].dt.tz_localize(None) <= end_dt.replace(tzinfo=None)]

            return df.reset_index(drop=True)
        except Exception as e:
            logger.error(f"Hyperliquid HYPE fetch_klines_range failed ({interval}): {e}")
            return pd.DataFrame()

    def fetch_klines(self, symbol: str, interval: str = "1h", limit: int = 200) -> pd.DataFrame:
        """Fetch klines; route HYPE to Hyperliquid and others to Binance."""
        if symbol.upper() != self.alt_symbol.upper():
            return super().fetch_klines(symbol=symbol, interval=interval, limit=limit)

        cache_key = f"hype_{interval}_{limit}"
        if cache_key in self._hype_cache:
            ts, df = self._hype_cache[cache_key]
            if time.time() - ts < self._hype_cache_ttl:
                return df

        df = self._fetch_hype_klines(interval=interval, limit=limit)
        self._hype_cache[cache_key] = (time.time(), df)
        return df

    def get_current_price(self, symbol: str = "HYPEUSDT") -> Optional[float]:
        """Get latest HYPE price from Hyperliquid; others from Binance."""
        if symbol.upper() != self.alt_symbol.upper():
            return super().get_current_price(symbol=symbol)
        df = self.fetch_klines(symbol=self.alt_symbol, interval="1m", limit=1)
        if df.empty:
            return None
        try:
            return float(df["close"].iloc[-1])
        except Exception:
            return None
