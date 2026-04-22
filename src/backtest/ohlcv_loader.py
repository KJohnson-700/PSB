"""
Historical OHLCV loader for crypto (BTC, SOL, ETH, XRP, HYPE) from Binance public API
with Kraken fallback for symbols that Binance geo-blocks (e.g. XRP in certain regions).

Downloads, chunks large date ranges, and caches results to Parquet so
subsequent runs only fetch what's missing.  No API key required.

Usage
-----
    loader = OHLCVLoader()
    btc = loader.load_all("BTC", "2025-01-01", "2025-03-20")
    # btc["4h"], btc["15m"], btc["5m"], btc["1m"] are pd.DataFrames
    sol = loader.load_all("SOL", "2025-01-01", "2025-03-20")
    # sol["1h"], sol["15m"], sol["5m"], sol["1m"]
    eth = loader.load_all("ETH", "2025-01-01", "2025-03-20")
    # same intervals as SOL (1h HTF for updown_engine alt path)
    xrp = loader.load_all("XRP", "2025-01-01", "2025-02-01")
    # xrp["1h"], xrp["15m"], xrp["5m"], xrp["1m"] — fetched from Kraken if Binance 451s
    hype = loader.load_all("HYPE", "2026-01-20", "2026-04-20")
    # hype["1h"], hype["15m"], hype["5m"], hype["1m"] — fetched from Hyperliquid directly
    # (HYPEUSDT is not listed on Binance, routed through HyperliquidHypeService)
"""
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)


def _get_hyperliquid_service():
    """Lazy-import HyperliquidHypeService to avoid circular deps."""
    from src.analysis.hyperliquid_hype_service import HyperliquidHypeService
    return HyperliquidHypeService()

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "backtest" / "ohlcv"

# Milliseconds per interval
_INTERVAL_MS: Dict[str, int] = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
}

_BINANCE_MAX_LIMIT = 1000  # Binance hard cap per request
_KRAKEN_MAX_LIMIT  = 720   # Kraken hard cap per request

_OHLCV_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_vol", "trades", "tbv", "tbq", "ignore",
]

# Kraken pair mapping for symbols Binance geo-blocks
# Keys are Binance USDT symbols, values are Kraken pair names
_KRAKEN_PAIR_MAP: Dict[str, str] = {
    "XRPUSDT":  "XRPUSD",
    "BTCUSDT":  "XBTUSD",
    "ETHUSDT":  "ETHUSD",
    "SOLUSDT":  "SOLUSD",
}

# Symbols not listed on Binance — routed to alternative sources
_HYPERLIQUID_SYMBOLS = {"HYPE"}
_BINANCE_FALLBACK_SYMBOLS = {"XRP"}

# Kraken interval mapping (minutes)
_KRAKEN_INTERVALS: Dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}


class OHLCVLoader:
    """Downloads and disk-caches historical OHLCV from Binance (no auth required).

    Data is stored as Parquet in ``data/backtest/ohlcv/`` and merged on
    subsequent calls so re-runs only download missing date ranges.
    """

    def __init__(self, cache_dir: Optional[Path] = None, no_cache: bool = False):
        self.cache_dir = cache_dir or _CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.no_cache = no_cache

    # ── cache helpers ──────────────────────────────────────────────────────

    def _cache_path(self, symbol: str, interval: str) -> Path:
        return self.cache_dir / f"{symbol}_{interval}.parquet"

    def _try_cache(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> Optional[pd.DataFrame]:
        """Return cached slice if it fully covers [start_ms, end_ms]."""
        if self.no_cache:
            return None
        p = self._cache_path(symbol, interval)
        if not p.exists():
            return None
        try:
            df = pd.read_parquet(p)
            if df.empty:
                return None
            # open_time stored as tz-aware UTC datetime
            # Use astype('int64') — view("int64") is deprecated in pandas 2.x and returns
            # wrong values on tz-aware datetime columns, causing cache misses.
            ot_ms = df["open_time"].astype("int64") // 1_000_000
            step  = _INTERVAL_MS.get(interval, 60_000)
            # Accept cache if it reaches within 24h of end_ms AND has meaningful overlap
            # with the requested range. Allow cache start to be later than start_ms —
            # e.g. ETH cache starts Mar 1 but request starts Jan 13; we still use it and
            # just return fewer bars (the backtest handles a shorter warmup period fine).
            # Strict end_ms check causes misses when end_date is today but cache was last
            # written earlier in the day (e.g. cache ends 08:00, end_ms = 23:59:59).
            _ONE_DAY_MS = 86_400_000
            cache_min = int(ot_ms.min())
            cache_max = int(ot_ms.max())
            covers_end   = cache_max >= end_ms - _ONE_DAY_MS
            has_overlap  = cache_min < end_ms and cache_max > start_ms
            if covers_end and has_overlap:
                mask = (ot_ms >= start_ms) & (ot_ms <= end_ms)
                sub  = df[mask].reset_index(drop=True)
                if not sub.empty:
                    if cache_min > start_ms:
                        logger.info(
                            f"Cache partial hit {symbol}/{interval}: {len(sub)} bars "
                            f"(cache starts {cache_min}, requested {start_ms} — using available)"
                        )
                    else:
                        logger.info(f"Cache hit {symbol}/{interval}: {len(sub)} bars")
                    return sub
        except Exception as e:
            logger.debug(f"Cache read error {symbol}/{interval}: {e}")
        return None

    def _write_cache(self, symbol: str, interval: str, df: pd.DataFrame) -> None:
        if self.no_cache or df.empty:
            return
        p = self._cache_path(symbol, interval)
        try:
            if p.exists():
                existing = pd.read_parquet(p)
                df = (
                    pd.concat([existing, df])
                    .drop_duplicates(subset=["open_time"])
                    .sort_values("open_time")
                    .reset_index(drop=True)
                )
            df.to_parquet(p, index=False)
        except Exception as e:
            logger.warning(f"Cache write error {symbol}/{interval}: {e}")

    # ── Binance fetch ──────────────────────────────────────────────────────

    def _fetch_binance(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> pd.DataFrame:
        """Fetch OHLCV from Binance, chunking into 1000-bar requests."""
        step_ms  = _INTERVAL_MS.get(interval, 60_000)
        all_rows = []
        cur_ms   = start_ms

        while cur_ms < end_ms:
            chunk_end = min(cur_ms + _BINANCE_MAX_LIMIT * step_ms, end_ms)
            batch = []
            for attempt in range(3):
                try:
                    r = requests.get(
                        BINANCE_KLINES_URL,
                        params={
                            "symbol":    symbol,
                            "interval":  interval,
                            "startTime": cur_ms,
                            "endTime":   chunk_end - 1,
                            "limit":     _BINANCE_MAX_LIMIT,
                        },
                        timeout=15,
                    )
                    r.raise_for_status()
                    batch = r.json()
                    break
                except Exception as exc:
                    if attempt == 2:
                        logger.warning(f"Binance {symbol}/{interval} failed: {exc}")
                    else:
                        time.sleep(1.5)

            if not batch:
                break

            all_rows.extend(batch)
            cur_ms = int(batch[-1][0]) + step_ms
            time.sleep(0.08)   # ~12.5 req/s — well within 1200/min limit

        if not all_rows:
            logger.warning(f"Binance returned no data for {symbol}/{interval}")
            return pd.DataFrame()

        df = pd.DataFrame(all_rows, columns=_OHLCV_COLS)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        df["open_time"]  = pd.to_datetime(df["open_time"],  unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        df = df[["open_time", "close_time", "open", "high", "low", "close", "volume"]].copy()
        logger.info(f"Binance: fetched {len(df)} {symbol}/{interval} bars")
        return df

    def _binance_blocked(self, symbol: str, interval: str, start_ms: int) -> bool:
        """Quick probe: return True if Binance returns 451 (geo-blocked)."""
        try:
            r = requests.get(
                BINANCE_KLINES_URL,
                params={"symbol": symbol, "interval": interval,
                        "startTime": start_ms, "limit": 1},
                timeout=8,
            )
            return r.status_code == 451
        except Exception:
            return False

    def _fetch_kraken(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> pd.DataFrame:
        """Fetch OHLCV from Kraken public API — fallback when Binance is geo-blocked.

        Supports the same interval strings as Binance (1m, 5m, 15m, 1h, 4h).
        Kraken returns up to 720 rows per call; we chunk like Binance.
        """
        pair = _KRAKEN_PAIR_MAP.get(symbol)
        if not pair:
            logger.warning(f"No Kraken pair mapping for {symbol}")
            return pd.DataFrame()
        kraken_interval = _KRAKEN_INTERVALS.get(interval)
        if kraken_interval is None:
            logger.warning(f"Kraken: unsupported interval {interval}")
            return pd.DataFrame()

        step_ms   = _INTERVAL_MS.get(interval, 60_000)
        chunk_ms  = _KRAKEN_MAX_LIMIT * step_ms
        all_rows: list = []
        cur_ms = start_ms

        logger.info(f"Kraken fallback: fetching {symbol}/{interval} "
                    f"({start_ms} -> {end_ms})")

        while cur_ms < end_ms:
            since_sec = cur_ms // 1000
            try:
                r = requests.get(
                    KRAKEN_OHLC_URL,
                    params={"pair": pair, "interval": kraken_interval, "since": since_sec},
                    timeout=15,
                )
                r.raise_for_status()
                data = r.json()
                if data.get("error"):
                    logger.warning(f"Kraken error for {pair}: {data['error']}")
                    break
                result = data.get("result", {})
                # Kraken returns pair data under its internal pair name (e.g. XXRPZUSD, not XRPUSD)
                # Find the first key that isn't the metadata "last" timestamp
                ohlc_key = next((k for k in result if k != "last"), None)
                rows = result.get(ohlc_key, []) if ohlc_key else []
                if not rows:
                    break
                # Kraken row: [time(s), open, high, low, close, vwap, volume, count]
                # Note: Kraken returns rows anchored to its available lookback window,
                # not necessarily starting at `since` — caller's date mask handles filtering.
                for row in rows:
                    ts_ms = int(row[0]) * 1000
                    close_ms = ts_ms + step_ms - 1
                    all_rows.append({
                        "open_time":  ts_ms,
                        "close_time": close_ms,
                        "open":  float(row[1]),
                        "high":  float(row[2]),
                        "low":   float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[6]),
                    })
                # Advance past last row
                last_ts_ms = int(rows[-1][0]) * 1000
                if last_ts_ms <= cur_ms:
                    break  # no progress
                cur_ms = last_ts_ms + step_ms
                time.sleep(0.5)  # Kraken: 1 req/s public rate limit
            except Exception as exc:
                logger.warning(f"Kraken {pair}/{interval} failed: {exc}")
                break

        if not all_rows:
            logger.warning(f"Kraken returned no data for {symbol}/{interval}")
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        df["open_time"]  = pd.to_datetime(df["open_time"],  unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        df = df[["open_time", "close_time", "open", "high", "low", "close", "volume"]]
        df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
        logger.info(f"Kraken: fetched {len(df)} {symbol}/{interval} bars")
        return df

    # ── public API ─────────────────────────────────────────────────────────

    def load(
        self,
        symbol: str,
        interval: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """Return OHLCV DataFrame for *symbol*/*interval* over [start_date, end_date].

        Columns: open_time (UTC), close_time (UTC), open, high, low, close, volume

        Data source priority:
          1. Disk cache (Parquet)
          2. Hyperliquid (HYPE only — not on Binance)
          3. Binance public API
          4. Kraken public API (fallback if Binance returns 451 geo-block)
        """
        tz   = timezone.utc
        s_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz)
        e_dt = datetime.strptime(end_date,   "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=tz
        )
        s_ms = int(s_dt.timestamp() * 1000)
        e_ms = int(e_dt.timestamp() * 1000)

        cached = self._try_cache(symbol, interval, s_ms, e_ms)
        if cached is not None:
            return cached

        # HYPE is not on Binance — route through HyperliquidHypeService
        if symbol in _HYPERLIQUID_SYMBOLS:
            logger.info(f"HYPE not on Binance — fetching via Hyperliquid for {symbol}/{interval}")
            svc = _get_hyperliquid_service()
            df = svc.fetch_klines_range(interval=interval, start_date=start_date, end_date=end_date)
            if not df.empty:
                # Hyperliquid returns tz-naive; localize to UTC for consistent comparisons
                df["open_time"] = df["open_time"].dt.tz_localize("UTC")
                df["close_time"] = df["close_time"].dt.tz_localize("UTC")
                self._write_cache(symbol, interval, df)
                mask = (df["open_time"] >= s_dt) & (df["open_time"] <= e_dt)
                return df[mask].reset_index(drop=True)
            return pd.DataFrame()

        # Try Binance first; if geo-blocked (451) fall back to Kraken
        df = self._fetch_binance(symbol, interval, s_ms, e_ms)
        if df.empty and self._binance_blocked(symbol, interval, s_ms):
            logger.info(f"Binance geo-blocked for {symbol}/{interval} — trying Kraken")
            df = self._fetch_kraken(symbol, interval, s_ms, e_ms)

        if not df.empty:
            self._write_cache(symbol, interval, df)
            mask = (df["open_time"] >= s_dt) & (df["open_time"] <= e_dt)
            df   = df[mask].reset_index(drop=True)
        return df

    def load_all(
        self, symbol: str, start_date: str, end_date: str
    ) -> Dict[str, pd.DataFrame]:
        """Load all timeframes needed for the crypto updown backtest.

        BTC  → 1m, 5m, 15m, 4h
        SOL  → 1m, 5m, 15m, 1h
        ETH  → 1m, 5m, 15m, 1h  (same HTF structure as SOL in UpdownBacktestEngine)
        XRP  → 1m, 5m, 15m, 1h  (fetched from Kraken if Binance geo-blocks XRPUSDT)
        HYPE → 1m, 5m, 15m, 1h  (fetched from Hyperliquid — not on Binance)
        """
        if symbol == "BTC":
            intervals = ["1m", "5m", "15m", "4h"]
        else:
            intervals = ["1m", "5m", "15m", "1h"]
        result: Dict[str, pd.DataFrame] = {}
        for iv in intervals:
            # HYPE is not on Binance — pass bare "HYPE" so load() can detect it
            loader_symbol = "HYPE" if symbol == "HYPE" else f"{symbol}USDT"
            logger.info(f"Loading {loader_symbol} {iv}  ({start_date} -> {end_date}) ...")
            result[iv] = self.load(loader_symbol, iv, start_date, end_date)
            logger.info(f"  -> {len(result[iv])} bars")
        return result
