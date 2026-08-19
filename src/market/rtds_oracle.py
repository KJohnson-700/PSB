"""Polymarket RTDS multi-source oracle — Binance + Chainlink on one WebSocket (2026-08-19).

Source: PSB-CODE-AND-DATA-REFERENCE-2026-08-18 (vault psb-new-research-2026-08).
Polymarket publishes its own real-time data stream at ``wss://ws-live-data.polymarket.com``
carrying BOTH the Binance spot feed and the Chainlink feed — and **Chainlink IS the
resolution oracle** for crypto up/down markets. That makes this one socket three fixes:

  1. FRESHNESS: a second live price source next to our CLOB ws (which was serving
     140s-stale marks at 5m entries on 08-18).
  2. RESOLUTION TRUTH: the Chainlink print at expiry is the number Polymarket settles
     with — consumers can grade/settle expired windows from the same source.
  3. MANIPULATION FADE: Binance-vs-Chainlink divergence > ~0.5% right before a window
     close is the a4385 pump signature (spent $1M on Binance spot, captured $280K on
     Polymarket). ``divergence()`` exposes it; consumers OBSERVE-ONLY for now.

OBSERVE-ONLY BY DESIGN today: nothing reads this for decisions yet. It runs as a
fail-open background task (config ``rtds.enabled``), keeps an in-memory tick cache, and
appends a 1-min snapshot line to ``data/calibration/rtds_snapshots.jsonl`` so the data
loop is CLOSED from the first boot (verify from OUTPUT, not the flag: the probation row
checks the snapshot file advances).

No hard dependency: if ``websockets`` is missing or the socket drops, consumers see
``None`` / stale=True and the bot behaves exactly as before this file existed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

RTDS_URI = "wss://ws-live-data.polymarket.com"
SNAPSHOT_PATH = os.path.join("data", "calibration", "rtds_snapshots.jsonl")

# PSB asset code -> (binance filter symbol, chainlink filter symbol)
SYMBOLS: Dict[str, Tuple[str, str]] = {
    "BTC": ("btcusdt", "btc/usd"),
    "ETH": ("ethusdt", "eth/usd"),
    "SOL": ("solusdt", "sol/usd"),
    "XRP": ("xrpusdt", "xrp/usd"),
    "DOGE": ("dogeusdt", "doge/usd"),
    "BNB": ("bnbusdt", "bnb/usd"),
    # HYPE resolves off Hyperliquid perp data, not Chainlink (existing PSB research);
    # it still gets the Binance leg here when RTDS carries it.
    "HYPE": ("hypeusdt", "hype/usd"),
}


class RtdsOracle:
    """In-memory latest-tick cache per (asset, source) with staleness accounting."""

    def __init__(self, max_age_ms: int = 15000):
        self.max_age_ms = int(max_age_ms)
        # {asset: {source: (price, ts_ms_exchange, ts_ms_received)}}
        self._ticks: Dict[str, Dict[str, Tuple[float, int, int]]] = {}
        self._connected = False
        self._last_msg_monotonic = 0.0
        self._msg_count = 0
        self._last_snapshot = 0.0

    # ── consumer API (all fail-open) ────────────────────────────────────────
    def get(self, asset: str, source: str) -> Optional[Tuple[float, int]]:
        """(price, age_ms) for 'binance' | 'chainlink', or None if absent/stale."""
        try:
            price, _ts_ex, ts_rx = self._ticks[asset.upper()][source]
        except KeyError:
            return None
        age = int(time.time() * 1000) - ts_rx
        if age > self.max_age_ms:
            return None
        return price, age

    def chainlink(self, asset: str) -> Optional[Tuple[float, int]]:
        return self.get(asset, "chainlink")

    def binance(self, asset: str) -> Optional[Tuple[float, int]]:
        return self.get(asset, "binance")

    def divergence(self, asset: str) -> Optional[float]:
        """|binance - chainlink| / chainlink using fresh ticks only; None if either absent."""
        b = self.get(asset, "binance")
        c = self.get(asset, "chainlink")
        if b is None or c is None or not c[0]:
            return None
        return abs(b[0] - c[0]) / c[0]

    def status(self) -> Dict[str, Any]:
        now_ms = int(time.time() * 1000)
        per = {}
        for asset, srcs in self._ticks.items():
            per[asset] = {
                s: {"price": p, "age_ms": now_ms - rx}
                for s, (p, _e, rx) in srcs.items()
            }
        return {
            "connected": self._connected,
            "msg_count": self._msg_count,
            "quiet_sec": round(time.monotonic() - self._last_msg_monotonic, 1)
            if self._last_msg_monotonic else None,
            "assets": per,
        }

    # ── ingest ──────────────────────────────────────────────────────────────
    def _ingest(self, topic: str, payload: Dict[str, Any]) -> None:
        try:
            sym = str(payload.get("symbol") or "").lower()
            price = float(payload.get("value"))
            ts_ex = int(payload.get("timestamp") or 0)
        except (TypeError, ValueError):
            return
        source = "chainlink" if "chainlink" in topic else "binance"
        asset = None
        for code, (b_sym, c_sym) in SYMBOLS.items():
            if sym == (c_sym if source == "chainlink" else b_sym):
                asset = code
                break
        if asset is None:
            return
        self._ticks.setdefault(asset, {})[source] = (price, ts_ex, int(time.time() * 1000))
        self._msg_count += 1
        self._last_msg_monotonic = time.monotonic()

    def _maybe_snapshot(self) -> None:
        """Append a 1-min status line so the loop is verifiable from OUTPUT."""
        now = time.time()
        if now - self._last_snapshot < 60.0:
            return
        self._last_snapshot = now
        try:
            row = {"ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
            row.update(self.status())
            with open(SNAPSHOT_PATH, "a") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError:
            pass

    # ── connection loop ─────────────────────────────────────────────────────
    async def run(self) -> None:
        """Reconnecting subscribe loop. Never raises out; backs off on failure."""
        try:
            import websockets
        except ImportError:
            logger.warning("[rtds] websockets package unavailable — oracle disabled")
            return
        backoff = 2.0
        while True:
            try:
                async with websockets.connect(RTDS_URI, ping_interval=None) as ws:
                    for _code, (b_sym, c_sym) in SYMBOLS.items():
                        await ws.send(json.dumps({
                            "action": "subscribe", "topic": "crypto_prices",
                            "filter": f"symbol={b_sym}",
                        }))
                        await ws.send(json.dumps({
                            "action": "subscribe", "topic": "crypto_prices_chainlink",
                            "filter": f"symbol={c_sym}",
                        }))
                    self._connected = True
                    backoff = 2.0
                    logger.info("[rtds] connected — %d assets x 2 sources subscribed", len(SYMBOLS))

                    async def _heartbeat():
                        while True:
                            await ws.send("PING")
                            await asyncio.sleep(5)

                    hb = asyncio.create_task(_heartbeat())
                    try:
                        async for message in ws:
                            if message == "PONG":
                                continue
                            try:
                                data = json.loads(message)
                            except (TypeError, ValueError):
                                continue
                            topic = str(data.get("topic") or "")
                            if topic.startswith("crypto_prices"):
                                self._ingest(topic, data.get("payload") or {})
                                self._maybe_snapshot()
                    finally:
                        hb.cancel()
            except asyncio.CancelledError:
                self._connected = False
                raise
            except Exception as exc:  # fail-open: log, back off, reconnect
                self._connected = False
                logger.info("[rtds] disconnected (%s: %s) — retry in %.0fs",
                            type(exc).__name__, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(120.0, backoff * 2)


_singleton: Optional[RtdsOracle] = None


def get_oracle() -> Optional[RtdsOracle]:
    """The process-wide oracle, or None if never started."""
    return _singleton


def start(config: Any) -> Optional[RtdsOracle]:
    """Create the singleton + return it; caller schedules ``oracle.run()``.

    Config: ``rtds: {enabled: true, max_age_ms: 15000}``. Returns None when disabled.
    """
    global _singleton
    try:
        get_cfg = config.get if hasattr(config, "get") else (lambda k, d=None: d)
        rcfg = dict(get_cfg("rtds", {}) or {})
        if not bool(rcfg.get("enabled", False)):
            return None
        if _singleton is None:
            _singleton = RtdsOracle(max_age_ms=int(rcfg.get("max_age_ms", 15000) or 15000))
        return _singleton
    except Exception:
        return None
