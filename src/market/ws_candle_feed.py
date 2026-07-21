"""Unified WebSocket candle feed (Binance spot + Binance USDM futures for HYPE).

2026-06-21: takes ALL underlying OHLCV off the per-cycle REST hot path. A single
background task streams klines for every traded symbol/interval and keeps an
in-memory store warm; ``SOLBTCService.fetch_klines`` (and the HYPE service) read
the store first and only fall back to REST on miss/staleness/gap.

Fail-safe by construction: any error, staleness, or time-gap returns None and the
caller uses the existing REST path, so the feed can NEVER feed a hole or break
trading. Flag-gated (``trading.ws_candle_feed.enabled``).

2026-06-29 REWIRE (perf): the previous store kept a per-key pandas DataFrame and
``_apply_bar`` upserted with an O(n) boolean-mask ``df.loc[mask] = ...`` on EVERY
ws tick. py-spy showed that single line ate ~52% of process CPU, held the GIL, and
starved the asyncio loop -> 12-17s scan cycles. The store is now a columnar raw-bar
ring buffer (``collections.deque`` of plain tuples): a live tick updates the last
bar in O(1) with NO pandas on the write path. The downstream DataFrame is built
only on read in ``get_klines`` and cached until the next write (dirty flag), so a
scan's burst of reads rebuilds at most once. Output contract is byte-identical to
before (same columns, dtypes, ordering, tail/copy semantics).

Hardening (Codex review 2026-06-21):
  - HYPE streamed from Binance USDM **futures** (same venue as its seed + the
    HyperliquidHypeService Binance-primary rule); no venue mixing.
  - Reseed on every (re)connect before serving that stream's keys.
  - get_klines validates candle CONTINUITY (last open_time within ~1.5 intervals)
    and copies under the lock.
  - _apply_bar upserts by open_time (in-order fast path + out-of-order safe fallback).

Store/return DataFrame columns match Binance REST klines used downstream:
open_time(datetime), open, high, low, close, volume(float), close_time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Set, Tuple

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_SPOT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT"]
_FUTURES_SYMBOLS = ["HYPEUSDT"]
_INTERVALS = ["1m", "5m", "15m", "1h"]
_INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
_MAX_BARS = 320
_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
_SPOT_WS = "wss://stream.binance.com:9443/stream"
_FUT_WS = "wss://fstream.binance.com/stream"
_SPOT_REST = "https://api.binance.com/api/v3/klines"
_FUT_REST = "https://fapi.binance.com/fapi/v1/klines"

# A raw bar: (open_time_ms, open, high, low, close, volume, close_time_ms).
# open_time_ms / close_time_ms are epoch-ms ints; OHLCV are floats. This maps
# positionally onto _COLS; the two time columns are converted to datetime64 only
# when the DataFrame is materialized for a reader.
_Bar = Tuple[int, float, float, float, float, float, int]


class WSCandleFeed:
    def __init__(self) -> None:
        self._bars: Dict[Tuple[str, str], Deque[_Bar]] = {}
        self._df_cache: Dict[Tuple[str, str], pd.DataFrame] = {}
        self._dirty: Dict[Tuple[str, str], bool] = {}
        self._last_update: Dict[Tuple[str, str], float] = {}
        self._ready: Set[Tuple[str, str]] = set()   # only serve keys that have been seeded
        self._lock = threading.Lock()
        self._started = False
        self._stop = False

    # ---- materialization (build downstream DataFrame on read; cached) ------
    def _materialize_locked(self, key: Tuple[str, str]) -> Optional[pd.DataFrame]:
        """Build the Binance-shaped DataFrame from raw bars. CALLER HOLDS _lock.

        Cached until the next write marks the key dirty, so a scan's burst of
        reads rebuilds at most once. The build is a single vectorized
        DataFrame construction + two vectorized to_datetime calls -- orders of
        magnitude cheaper than the old per-tick masked .loc assignment.
        """
        if not self._dirty.get(key, True):
            cached = self._df_cache.get(key)
            if cached is not None:
                return cached
        buf = self._bars.get(key)
        if not buf:
            return None
        df = pd.DataFrame(list(buf), columns=_COLS)
        # epoch-ms ints -> datetime64[ns], identical dtype to the REST seed path
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
        self._df_cache[key] = df
        self._dirty[key] = False
        return df

    # ---- public read API (sync; called from the scan path) ----------------
    def get_klines(self, symbol: str, interval: str, limit: int) -> Optional[pd.DataFrame]:
        """Warm WS klines for (symbol, interval), or None -> caller falls back to REST."""
        key = ((symbol or "").upper(), interval)
        isec = _INTERVAL_SEC.get(interval)
        if isec is None:
            return None  # interval not streamed -> REST
        try:
            with self._lock:
                if key not in self._ready:
                    return None
                buf = self._bars.get(key)
                upd = self._last_update.get(key, 0.0)
                if not buf or len(buf) < min(limit, 30):
                    return None
                # packet freshness AND candle continuity: last bar must be current
                now = time.time()
                if (now - upd) > (isec + 60):
                    return None
                last_ot_ms = buf[-1][0]
                if (now - last_ot_ms / 1000.0) > (1.5 * isec + 60):
                    return None  # gapped/stale grid -> REST
                df = self._materialize_locked(key)
                if df is None or df.empty or len(df) < min(limit, 30):
                    return None
                return df.tail(limit).reset_index(drop=True).copy()  # under lock
        except Exception:
            return None

    # ---- seeding ----------------------------------------------------------
    def _seed(self, symbol: str, interval: str) -> bool:
        url = _FUT_REST if symbol.upper() in _FUTURES_SYMBOLS else _SPOT_REST
        try:
            r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": _MAX_BARS}, timeout=8)
            r.raise_for_status()
            bars: List[_Bar] = [
                (int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), int(x[6]))
                for x in r.json()
            ]
            key = (symbol.upper(), interval)
            with self._lock:
                self._bars[key] = deque(bars, maxlen=_MAX_BARS)
                self._df_cache.pop(key, None)
                self._dirty[key] = True
                self._last_update[key] = time.time()
                self._ready.add(key)
            return True
        except Exception as e:
            logger.warning("[ws_feed] seed failed %s %s: %s", symbol, interval, e)
            return False

    def _seed_many(self, symbols: List[str]) -> None:
        for s in symbols:
            for iv in _INTERVALS:
                self._seed(s, iv)

    def _apply_bar(self, key: Tuple[str, str], ot_ms: int, o: float, h: float, l: float, c: float, v: float, ct_ms: int) -> None:
        ot_ms = int(ot_ms)
        bar: _Bar = (ot_ms, o, h, l, c, v, int(ct_ms))
        with self._lock:
            buf = self._bars.get(key)
            if buf is None:
                buf = deque(maxlen=_MAX_BARS)
                self._bars[key] = buf
            if buf and ot_ms == buf[-1][0]:
                buf[-1] = bar                       # update in-progress bar -- O(1)
            elif (not buf) or ot_ms > buf[-1][0]:
                buf.append(bar)                     # new bar -- O(1) (auto-evicts oldest)
            else:
                # rare: out-of-order / late correction -> sorted upsert by open_time
                merged: Dict[int, _Bar] = {b[0]: b for b in buf}
                merged[ot_ms] = bar
                ordered = [merged[k] for k in sorted(merged)][-_MAX_BARS:]
                buf.clear()
                buf.extend(ordered)
            self._dirty[key] = True
            self._last_update[key] = time.time()

    # ---- background streams (reseed on every (re)connect) -----------------
    async def _run_binance(self, *, futures: bool, symbols: List[str]) -> None:
        import websockets
        base = _FUT_WS if futures else _SPOT_WS
        streams = "/".join(f"{s.lower()}@kline_{iv}" for s in symbols for iv in _INTERVALS)
        url = f"{base}?streams={streams}"
        loop = asyncio.get_event_loop()
        while not self._stop:
            try:
                async with websockets.connect(url, open_timeout=10, ping_interval=20) as ws:
                    # reseed BEFORE serving (gap repair after any downtime)
                    await loop.run_in_executor(None, self._seed_many, symbols)
                    logger.info("[ws_feed] %s connected (%d streams, reseeded)", "futures" if futures else "spot", len(symbols) * len(_INTERVALS))
                    async for raw in ws:
                        m = json.loads(raw)
                        k = (m.get("data") or {}).get("k")
                        if not k:
                            continue
                        self._apply_bar((k["s"].upper(), k["i"]), int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"]), int(k["T"]))
            except Exception as e:
                # mark this stream's keys not-ready so callers REST until reseed
                with self._lock:
                    for s in symbols:
                        for iv in _INTERVALS:
                            self._ready.discard((s.upper(), iv))
                logger.warning("[ws_feed] %s ws dropped (%s); reconnect in 3s", "futures" if futures else "spot", e)
                await asyncio.sleep(3)

    async def run(self) -> None:
        if self._started:
            return
        self._started = True
        await asyncio.gather(
            self._run_binance(futures=False, symbols=_SPOT_SYMBOLS),
            self._run_binance(futures=True, symbols=_FUTURES_SYMBOLS),
        )


_FEED: Optional[WSCandleFeed] = None


def get_feed() -> WSCandleFeed:
    global _FEED
    if _FEED is None:
        _FEED = WSCandleFeed()
    return _FEED
