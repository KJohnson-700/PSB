"""Unified WebSocket candle feed (Binance spot + Binance USDM futures for HYPE).

2026-06-21: takes ALL underlying OHLCV off the per-cycle REST hot path. A single
background task streams klines for every traded symbol/interval and keeps an
in-memory store warm; ``SOLBTCService.fetch_klines`` (and the HYPE service) read
the store first and only fall back to REST on miss/staleness/gap.

Fail-safe by construction: any error, staleness, or time-gap returns None and the
caller uses the existing REST path, so the feed can NEVER feed a hole or break
trading. Flag-gated (``trading.ws_candle_feed.enabled``).

Hardening (Codex review 2026-06-21):
  - HYPE streamed from Binance USDM **futures** (same venue as its seed + the
    HyperliquidHypeService Binance-primary rule); no venue mixing.
  - Reseed on every (re)connect before serving that stream's keys.
  - get_klines validates candle CONTINUITY (last open_time within ~1.5 intervals)
    and copies under the lock.
  - _apply_bar upserts by open_time (sort + dedupe + cap) — out-of-order safe.

Store/return DataFrame columns match Binance REST klines used downstream:
open_time(datetime), open, high, low, close, volume(float), close_time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

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


class WSCandleFeed:
    def __init__(self) -> None:
        self._store: Dict[Tuple[str, str], pd.DataFrame] = {}
        self._last_update: Dict[Tuple[str, str], float] = {}
        self._ready: Set[Tuple[str, str]] = set()   # only serve keys that have been seeded
        self._lock = threading.Lock()
        self._started = False
        self._stop = False

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
                df = self._store.get(key)
                upd = self._last_update.get(key, 0.0)
                if df is None or df.empty or len(df) < min(limit, 30):
                    return None
                # packet freshness AND candle continuity: last bar must be current
                now = time.time()
                if (now - upd) > (isec + 60):
                    return None
                last_ot = df["open_time"].iloc[-1].timestamp()
                if (now - last_ot) > (1.5 * isec + 60):
                    return None  # gapped/stale grid -> REST
                return df.tail(limit).reset_index(drop=True).copy()  # under lock
        except Exception:
            return None

    # ---- seeding ----------------------------------------------------------
    def _seed(self, symbol: str, interval: str) -> bool:
        url = _FUT_REST if symbol.upper() in _FUTURES_SYMBOLS else _SPOT_REST
        try:
            r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": _MAX_BARS}, timeout=8)
            r.raise_for_status()
            rows = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), int(x[6])] for x in r.json()]
            df = pd.DataFrame(rows, columns=_COLS)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
            key = (symbol.upper(), interval)
            with self._lock:
                self._store[key] = df
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
        ot = pd.to_datetime(ot_ms, unit="ms")
        ct = pd.to_datetime(ct_ms, unit="ms")
        row = {"open_time": ot, "open": o, "high": h, "low": l, "close": c, "volume": v, "close_time": ct}
        with self._lock:
            df = self._store.get(key)
            if df is None or df.empty:
                self._store[key] = pd.DataFrame([row], columns=_COLS)
            else:
                mask = df["open_time"] == ot
                if mask.any():
                    df.loc[mask, _COLS] = [ot, o, h, l, c, v, ct]   # upsert existing bar
                else:
                    df = pd.concat([df, pd.DataFrame([row], columns=_COLS)], ignore_index=True)
                    df = df.drop_duplicates("open_time").sort_values("open_time").tail(_MAX_BARS).reset_index(drop=True)
                    self._store[key] = df
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
