"""
Hyperliquid HYPE service adapter.

Provides the SOLBTCService interface but sources the alt-coin leg (HYPE) from
Hyperliquid's public candleSnapshot endpoint instead of Binance.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests

from src.analysis.sol_btc_service import SOLBTCService
from src.utils.http_retry import requests_post_with_retries

logger = logging.getLogger(__name__)

# Hyperliquid ``candleSnapshot`` returns at most ~5000 candles per request; asking for
# a multi-month 1m window in one shot often yields ``[]`` (see backtest HYPE runs).
# Paginate in time chunks like Binance/Kraken loaders in ``ohlcv_loader.py``.
_HL_MAX_CANDLES_PER_RANGE_REQUEST = 4000
# 1m is far noisier: wide chunks frequently return ``[]`` while smaller windows succeed.
_HL_MAX_CANDLES_1M_PER_CHUNK = 500
# Stop bisecting when a segment is this many bars or fewer (still empty → give up).
_HL_MIN_BARS_TO_BISECT = 48

# Binance USDM perpetual is the primary live HYPE spot source; Hyperliquid is fallback.
# Matches backtest path in ``src/backtest/ohlcv_loader.py`` so live and backtest see the
# same spot reference (incl. oracle basis vs Chainlink). HL only carries this lane during
# Binance outages / pre-listing windows / geo-block.
_BINANCE_FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
_BINANCE_HYPE_FUTURES_SYMBOL = "HYPEUSDT"
_BINANCE_LIVE_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


def hyperliquid_kwargs_from_config(mapping: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Map ``config['hyperliquid']`` subset to ``HyperliquidHypeService`` keyword args."""
    m = dict(mapping or {})
    out: Dict[str, Any] = {}
    if "request_timeout_sec" in m and m["request_timeout_sec"] is not None:
        out["request_timeout_sec"] = float(m["request_timeout_sec"])
    if "range_request_timeout_sec" in m and m["range_request_timeout_sec"] is not None:
        out["range_request_timeout_sec"] = float(m["range_request_timeout_sec"])
    if "connect_timeout_sec" in m and m["connect_timeout_sec"] is not None:
        out["connect_timeout_sec"] = float(m["connect_timeout_sec"])
    if "max_retries" in m and m["max_retries"] is not None:
        out["max_retries"] = int(m["max_retries"])
    if "retry_backoff_base_sec" in m and m["retry_backoff_base_sec"] is not None:
        out["retry_backoff_base_sec"] = float(m["retry_backoff_base_sec"])
    if "stale_on_error_max_age_sec" in m and m["stale_on_error_max_age_sec"] is not None:
        out["stale_on_error_max_age_sec"] = float(m["stale_on_error_max_age_sec"])
    return out


class HyperliquidHypeService(SOLBTCService):
    """SOLBTCService-compatible adapter for HYPE candles from Hyperliquid."""

    HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
    HYPE_COIN = "HYPE"
    _INTERVAL_MAP = {
        "1m": ("1m", 60_000),
        "5m": ("5m", 300_000),
        "30m": ("30m", 1_800_000),
        "15m": ("15m", 900_000),
        "1h": ("1h", 3_600_000),
        "4h": ("4h", 14_400_000),
        "1d": ("1d", 86_400_000),
    }

    @staticmethod
    def _empty_klines_df() -> pd.DataFrame:
        """Return an empty klines frame with the columns downstream code expects."""
        return pd.DataFrame(
            {
                "open_time": pd.Series(dtype="datetime64[ns, UTC]"),
                "open": pd.Series(dtype="float64"),
                "high": pd.Series(dtype="float64"),
                "low": pd.Series(dtype="float64"),
                "close": pd.Series(dtype="float64"),
                "volume": pd.Series(dtype="float64"),
                "close_time": pd.Series(dtype="datetime64[ns, UTC]"),
            }
        )

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
        request_timeout_sec: float = 25.0,
        range_request_timeout_sec: float = 30.0,
        connect_timeout_sec: float = 5.0,
        max_retries: int = 4,
        retry_backoff_base_sec: float = 0.5,
        stale_on_error_max_age_sec: float = 180.0,
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
        self._hype_cache_ttl = 30  # seconds — fresh-window TTL
        self._request_timeout_sec = max(5.0, float(request_timeout_sec))
        self._range_request_timeout_sec = max(5.0, float(range_request_timeout_sec))
        self._connect_timeout_sec = max(2.0, float(connect_timeout_sec))
        self._max_retries = max(1, int(max_retries))
        self._retry_backoff_base_sec = max(0.05, float(retry_backoff_base_sec))
        # Stale-on-error: when a fetch fails, keep returning the last good cached
        # frame for up to this age. Beats returning empty (which kills HYPE signals).
        self._stale_on_error_max_age_sec = max(0.0, float(stale_on_error_max_age_sec))
        # Last-good cache, separate from the TTL cache so it survives expiry.
        self._hype_last_good: Dict[str, Tuple[float, pd.DataFrame]] = {}
        # Connection-reused HTTP session — avoids per-call TLS handshake.
        self._http_session: requests.Session = requests.Session()
        self._http_session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "psb-main/hyperliquid-hype-service",
            }
        )

    def _hl_bisect_fetch_chunk(
        self,
        fetch_chunk,
        lo_ms: int,
        hi_ms: int,
        interval_ms: int,
        depth: int = 0,
    ) -> pd.DataFrame:
        """Retry empty candleSnapshot responses by splitting the window (HL often returns [] for wide 1m spans)."""
        df = fetch_chunk(lo_ms, hi_ms)
        if not df.empty:
            return df
        df = fetch_chunk(lo_ms, hi_ms)
        if not df.empty:
            return df
        if hi_ms < lo_ms:
            return self._empty_klines_df()
        n_bars = (hi_ms - lo_ms) // interval_ms + 1
        if n_bars <= _HL_MIN_BARS_TO_BISECT or depth > 16:
            return self._empty_klines_df()
        half = max(1, n_bars // 2)
        left_hi = lo_ms + half * interval_ms - 1
        right_lo = lo_ms + half * interval_ms
        if left_hi < lo_ms or right_lo > hi_ms:
            return self._empty_klines_df()
        L = self._hl_bisect_fetch_chunk(fetch_chunk, lo_ms, left_hi, interval_ms, depth + 1)
        R = self._hl_bisect_fetch_chunk(fetch_chunk, right_lo, hi_ms, interval_ms, depth + 1)
        if L.empty and R.empty:
            return L
        if L.empty:
            return R.reset_index(drop=True)
        if R.empty:
            return L.reset_index(drop=True)
        out = pd.concat([L, R], ignore_index=True)
        return out.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)

    def _post_candles(self, payload: dict, *, timeout: float) -> requests.Response:
        # Tuple form: (connect_timeout, read_timeout). Slow DNS/TLS no longer
        # eats into the read budget — connect must succeed within 5s, then we
        # have the full read budget for the candleSnapshot response.
        timeout_tuple = (self._connect_timeout_sec, float(timeout))
        return requests_post_with_retries(
            self.HYPERLIQUID_INFO_URL,
            json=payload,
            timeout=timeout_tuple,
            max_retries=self._max_retries,
            backoff_base=self._retry_backoff_base_sec,
            log_name="hyperliquid.hype",
            session=self._http_session,
        )

    def _stale_fallback(self, cache_key: str, *, reason: str) -> pd.DataFrame:
        """Return last good frame if fresh enough, else empty.

        Logged at WARNING so blackouts surface in ops logs (the prior code
        swallowed errors at logger.error level only on the very last retry).
        """
        rec = self._hype_last_good.get(cache_key)
        if not rec:
            logger.warning(
                "Hyperliquid HYPE fetch failed (%s); no stale cache available — returning empty",
                reason,
            )
            return self._empty_klines_df()
        ts, df = rec
        age = time.time() - ts
        if age > self._stale_on_error_max_age_sec:
            logger.warning(
                "Hyperliquid HYPE fetch failed (%s); last good cache is %.1fs old (>%s) — returning empty",
                reason,
                age,
                self._stale_on_error_max_age_sec,
            )
            return self._empty_klines_df()
        logger.warning(
            "Hyperliquid HYPE fetch failed (%s); serving stale cache %.1fs old",
            reason,
            age,
        )
        return df.copy()

    def _oracle_reference_spot(self, fallback: float) -> Optional[float]:
        """Use Hyperliquid native mid for the Chainlink oracle-basis comparison.

        Klines / current_price come from Binance USDM HYPEUSDT (deep, fast), but
        the Chainlink Arbitrum HYPE feed references Hyperliquid native spot —
        comparing Binance-USDM to Chainlink-Arbitrum produces ~20-30 bps of
        venue dispersion that isn't a real oracle staleness signal. Pulling the
        HL mid here makes the basis gate measure what it was designed to:
        oracle-vs-source-of-truth divergence.

        ``allMids`` returns one float per coin; cheap (~1 KB) and 30s-cached.
        Falls back to the Binance kline ``fallback`` if HL is unreachable.
        """
        now = time.time()
        cached = getattr(self, "_hl_mid_cache", None)
        if cached and (now - cached[0]) < 30.0:
            return cached[1]
        try:
            resp = self._post_candles(
                {"type": "allMids"},
                timeout=self._request_timeout_sec,
            )
            resp.raise_for_status()
            mids = resp.json() or {}
            raw = mids.get(self.HYPE_COIN)
            if raw is None:
                return fallback
            mid = float(raw)
            self._hl_mid_cache = (now, mid)
            return mid
        except Exception as e:
            logger.info("Hyperliquid allMids unavailable (%s); using Binance kline for basis", e)
            return fallback

    def _fetch_binance_hype_klines(self, interval: str, limit: int) -> pd.DataFrame:
        """Primary live source: Binance USDM perpetual ``HYPEUSDT`` klines.

        Returns empty frame on any failure (caller falls through to Hyperliquid).
        Single-shot request — live ``limit`` is bounded (caller passes ≤500) so no
        chunking needed here; backtest range pagination lives in ``ohlcv_loader``.
        """
        if interval not in _BINANCE_LIVE_INTERVALS:
            return self._empty_klines_df()
        params = {
            "symbol": _BINANCE_HYPE_FUTURES_SYMBOL,
            "interval": interval,
            "limit": int(max(1, min(1000, limit))),
        }
        try:
            resp = self._http_session.get(
                _BINANCE_FUTURES_KLINES_URL,
                params=params,
                timeout=(self._connect_timeout_sec, self._request_timeout_sec),
            )
            resp.raise_for_status()
            rows = resp.json() or []
        except Exception as e:
            logger.info("Binance USDM HYPE %s unavailable (%s); falling back to Hyperliquid", interval, e)
            return self._empty_klines_df()
        if not isinstance(rows, list) or not rows:
            return self._empty_klines_df()
        parsed = []
        for row in rows:
            try:
                parsed.append(
                    {
                        "open_time": pd.to_datetime(int(row[0]), unit="ms", utc=True),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                        "close_time": pd.to_datetime(int(row[6]), unit="ms", utc=True),
                    }
                )
            except Exception:
                continue
        if not parsed:
            return self._empty_klines_df()
        df = pd.DataFrame(parsed).sort_values("open_time").drop_duplicates(subset=["open_time"])
        return df.tail(limit).reset_index(drop=True)

    def _fetch_hype_klines(self, interval: str, limit: int) -> pd.DataFrame:
        """Fetch HYPE candles, Binance USDM first then Hyperliquid fallback.

        Behavior:
          - Try Binance futures ``HYPEUSDT`` first (parity with backtest loader).
          - On empty/error, fall through to Hyperliquid ``candleSnapshot``.
          - Hyperliquid path retains existing retry + stale-cache fallback.
        """
        binance_df = self._fetch_binance_hype_klines(interval=interval, limit=limit)
        if not binance_df.empty:
            cache_key = f"hype_{interval}_{limit}"
            self._hype_last_good[cache_key] = (time.time(), binance_df)
            return binance_df
        mapped = self._INTERVAL_MAP.get(interval)
        if not mapped:
            logger.warning(f"Hyperliquid HYPE unsupported interval: {interval}")
            return self._empty_klines_df()

        hl_interval, interval_ms = mapped
        cache_key = f"hype_{interval}_{limit}"

        def _build_payload() -> dict:
            now_ms = int(time.time() * 1000)
            lookback_bars = max(5, min(500, limit + 5))
            start_ms = now_ms - (lookback_bars * interval_ms)
            return {
                "type": "candleSnapshot",
                "req": {
                    "coin": self.HYPE_COIN,
                    "interval": hl_interval,
                    "startTime": start_ms,
                    "endTime": now_ms,
                },
            }

        def _attempt() -> pd.DataFrame:
            resp = self._post_candles(_build_payload(), timeout=self._request_timeout_sec)
            resp.raise_for_status()
            rows = resp.json() or []
            if not isinstance(rows, list) or not rows:
                return self._empty_klines_df()

            parsed = []
            for row in rows:
                try:
                    open_ms = int(row.get("t"))
                    close_ms = int(row.get("T", open_ms + interval_ms))
                    parsed.append(
                        {
                            "open_time": pd.to_datetime(open_ms, unit="ms", utc=True),
                            "open": float(row.get("o", 0.0)),
                            "high": float(row.get("h", 0.0)),
                            "low": float(row.get("l", 0.0)),
                            "close": float(row.get("c", 0.0)),
                            "volume": float(row.get("v", 0.0)),
                            "close_time": pd.to_datetime(close_ms, unit="ms", utc=True),
                        }
                    )
                except Exception:
                    continue

            if not parsed:
                return self._empty_klines_df()

            df = pd.DataFrame(parsed).sort_values("open_time").drop_duplicates(subset=["open_time"])
            return df.tail(limit).reset_index(drop=True)

        # First attempt
        try:
            df = _attempt()
            if not df.empty:
                self._hype_last_good[cache_key] = (time.time(), df)
                return df
            # Empty-result retry (one extra shot — Hyperliquid intermittently returns [])
            logger.warning("Hyperliquid HYPE empty response (%s); retrying once", interval)
            df2 = _attempt()
            if not df2.empty:
                self._hype_last_good[cache_key] = (time.time(), df2)
                return df2
            return self._stale_fallback(cache_key, reason=f"empty {interval}")
        except Exception as e:
            logger.error("Hyperliquid HYPE candles unavailable (%s): %s", interval, e)
            return self._stale_fallback(cache_key, reason=f"exception {interval}: {e}")

    def fetch_klines_range(
        self,
        interval: str = "1h",
        start_date: str = None,
        end_date: str = None,
        limit: int = 2000,
    ) -> pd.DataFrame:
        """Fetch HYPE klines for a date range (used by backtest OHLCV loader).

        Hyperliquid's ``candleSnapshot`` only returns a limited number of candles per
        call (commonly capped around 5000). Wide ``startTime``/``endTime`` windows
        (especially multi-month **1m**) often return an empty list; this method
        **pages** forward in ``_HL_MAX_CANDLES_PER_RANGE_REQUEST``-bar windows and
        concatenates, mirroring Binance chunking in ``ohlcv_loader``.

        Build the request from the actual requested window so historical backtests
        do not silently drift with wall-clock time.
        """
        mapped = self._INTERVAL_MAP.get(interval)
        if not mapped:
            logger.warning(f"Hyperliquid HYPE unsupported interval: {interval}")
            return self._empty_klines_df()

        hl_interval, interval_ms = mapped
        start_dt = (
            pd.to_datetime(start_date, utc=True)
            if start_date
            else pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=90)
        )
        end_dt = (
            pd.to_datetime(end_date, utc=True) + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
            if end_date
            else pd.Timestamp.now(tz="UTC")
        )
        if end_dt < start_dt:
            logger.warning(
                "Hyperliquid HYPE invalid range request: start=%s end=%s",
                start_date,
                end_date,
            )
            return self._empty_klines_df()

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        def _parse_rows(rows: Any) -> pd.DataFrame:
            if not isinstance(rows, list) or not rows:
                return self._empty_klines_df()
            parsed = []
            for row in rows:
                try:
                    open_ms = int(row.get("t"))
                    close_ms = int(row.get("T", open_ms + interval_ms))
                    parsed.append(
                        {
                            "open_time": pd.to_datetime(open_ms, unit="ms", utc=True),
                            "open": float(row.get("o", 0.0)),
                            "high": float(row.get("h", 0.0)),
                            "low": float(row.get("l", 0.0)),
                            "close": float(row.get("c", 0.0)),
                            "volume": float(row.get("v", 0.0)),
                            "close_time": pd.to_datetime(close_ms, unit="ms", utc=True),
                        }
                    )
                except Exception:
                    continue
            if not parsed:
                return self._empty_klines_df()
            df = pd.DataFrame(parsed).sort_values("open_time").drop_duplicates(subset=["open_time"])
            return df.reset_index(drop=True)

        def _fetch_chunk(chunk_start_ms: int, chunk_end_ms: int) -> pd.DataFrame:
            payload = {
                "type": "candleSnapshot",
                "req": {
                    "coin": self.HYPE_COIN,
                    "interval": hl_interval,
                    "startTime": int(chunk_start_ms),
                    "endTime": int(chunk_end_ms),
                },
            }
            resp = self._post_candles(payload, timeout=self._range_request_timeout_sec)
            resp.raise_for_status()
            return _parse_rows(resp.json())

        # Range-fetch failure paths intentionally return empty (no cache fallback for
        # backtests — they re-run, unlike live which can't replay a missed candle).
        try:
            max_candles = (
                _HL_MAX_CANDLES_1M_PER_CHUNK
                if interval == "1m"
                else _HL_MAX_CANDLES_PER_RANGE_REQUEST
            )
            chunk_span_ms = max(interval_ms, max_candles * interval_ms)
            all_parts: list[pd.DataFrame] = []
            chunk_start = start_ms
            max_steps = int((end_ms - start_ms) / interval_ms) + 50
            steps = 0
            while chunk_start <= end_ms and steps < max_steps:
                steps += 1
                chunk_end = min(chunk_start + chunk_span_ms - 1, end_ms)
                df_chunk = self._hl_bisect_fetch_chunk(
                    _fetch_chunk, chunk_start, chunk_end, interval_ms
                )
                if df_chunk.empty:
                    logger.warning(
                        "Hyperliquid HYPE range empty chunk %s/%s ms (%s); stopping pagination",
                        chunk_start,
                        chunk_end,
                        interval,
                    )
                    break
                all_parts.append(df_chunk)
                try:
                    last_open = df_chunk["open_time"].iloc[-1]
                    last_open_ms = int(pd.Timestamp(last_open).timestamp() * 1000)
                except Exception:
                    break
                nxt = last_open_ms + interval_ms
                if nxt <= chunk_start:
                    break
                chunk_start = nxt
                if last_open_ms >= end_ms - interval_ms:
                    break
                time.sleep(0.08)

            if not all_parts:
                return self._empty_klines_df()

            df = pd.concat(all_parts, ignore_index=True)
            df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)

            if start_date:
                start_dt_f = pd.to_datetime(start_date, utc=True)
                df = df[df["open_time"] >= start_dt_f]
            if end_date:
                end_dt_f = (
                    pd.to_datetime(end_date, utc=True)
                    + pd.Timedelta(days=1)
                    - pd.Timedelta(milliseconds=1)
                )
                df = df[df["open_time"] <= end_dt_f]

            return df.reset_index(drop=True)
        except Exception as e:
            logger.error("Hyperliquid HYPE fetch_klines_range failed (%s): %s", interval, e)
            return self._empty_klines_df()

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
