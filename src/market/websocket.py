"""
WebSocket Module
Real-time order book streaming from Polymarket
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Callable, Any, Set
import aiohttp
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class OrderBook:
    """Represents an order book for a market"""

    token_id: str
    bids: List[Dict[str, float]] = field(default_factory=list)  # [{price, size}]
    asks: List[Dict[str, float]] = field(default_factory=list)  # [{price, size}]
    last_update: float = 0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0]["price"] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0]["price"] if self.asks else None

    @property
    def spread(self) -> float:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return float("inf")

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None


class WebSocketClient:
    """WebSocket client for Polymarket real-time data"""

    # Per Polymarket CLOB docs: path is /ws/market (public orderbook), not bare /ws (404).
    _WS_HOST = "wss://ws-subscriptions-clob.polymarket.com"

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.subscriptions: Dict[str, Set[str]] = {}
        self.order_books: Dict[str, OrderBook] = {}
        self.running = False
        self.callbacks: List[Callable] = []
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._session: Optional[aiohttp.ClientSession] = None
        self._sent_initial_market_subscription = False
        # monotonic time of the last frame received; silence_watchdog reads it
        self.last_frame_mono: float = 0.0

    def _clob_ws_cfg(self) -> Dict[str, Any]:
        return (self.config.get("trading") or {}).get("clob_ws") or {}

    def _ws_url(self) -> str:
        ws_cfg = self._clob_ws_cfg()
        explicit = ws_cfg.get("wss_url")
        if explicit:
            return str(explicit).rstrip("/")
        channel = str(ws_cfg.get("book_channel", "market"))
        return f"{self._WS_HOST}/ws/{channel}"

    def _asset_ids_key(self) -> str:
        return self._clob_ws_cfg().get("asset_ids_json_key", "assets_ids")

    async def connect(self) -> bool:
        """Connect to WebSocket server"""
        try:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = aiohttp.ClientSession()
            url = self._ws_url()
            # 2026-07-12: heartbeat was hardcoded 30 -> aiohttp tore the socket
            # down ~every 30s (Polymarket app-level keepalive doesn't satisfy
            # aiohttp protocol PING/PONG) = reconnect churn (85/session, ws_cov
            # stuck 31%). Default None(off) -> rely on silence_watchdog(120s).
            # Reversible: trading.clob_ws.ws_heartbeat_sec. Codex GO 2026-07-12.
            _raw_hb = self._clob_ws_cfg().get("ws_heartbeat_sec")
            _hb = float(_raw_hb) if _raw_hb not in (None, "", 0, "0") else None
            self.ws = await self._session.ws_connect(url, heartbeat=_hb)
            self._reconnect_delay = 1
            self._sent_initial_market_subscription = False
            self.subscriptions.clear()
            self.last_frame_mono = asyncio.get_event_loop().time()
            logger.info("Connected to Polymarket WebSocket (%s)", url)
            return True
        except Exception as e:
            logger.error(f"Failed to connect to WebSocket: {e}")
            self._reconnect_delay = min(
                self._reconnect_delay * 2, self._max_reconnect_delay
            )
            return False

    async def disconnect(self):
        """Disconnect from WebSocket server"""
        self.running = False
        if self.ws:
            await self.ws.close()
            self.ws = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("Disconnected from WebSocket")

    async def subscribe(self, channel: str, token_ids: List[str]):
        """Subscribe to a channel for specific tokens"""
        if self.ws is None:
            logger.error("WebSocket not connected")
            return

        # Store subscription
        if channel not in self.subscriptions:
            self.subscriptions[channel] = set()
        self.subscriptions[channel].update(token_ids)

        # Initialize order books
        for token_id in token_ids:
            if token_id not in self.order_books:
                self.order_books[token_id] = OrderBook(token_id=token_id)

        # Polymarket market channel: first message uses type "market"; later deltas use operation.
        id_key = self._asset_ids_key()
        if channel == "market":
            if not self._sent_initial_market_subscription:
                message: Dict[str, Any] = {"type": "market", id_key: token_ids}
                self._sent_initial_market_subscription = True
            else:
                message = {"operation": "subscribe", id_key: token_ids}
        else:
            message = {"type": "subscribe", "channel": channel, id_key: token_ids}

        await self._safe_send_json(message)
        logger.info(f"Subscribed to {channel} for {len(token_ids)} tokens")

    async def unsubscribe(self, channel: str, token_ids: List[str]):
        """Unsubscribe from a channel"""
        if self.ws is None:
            return

        if channel in self.subscriptions:
            self.subscriptions[channel].difference_update(token_ids)

        # Free the per-token OrderBook on unsubscribe. subscribe() creates one in
        # self.order_books for every token; without this pop, books for expired
        # markets (new token_ids every 5/15/60-min window) accumulate forever,
        # each retaining bid/ask price-ladder dicts of small str/float values.
        # That was THE memory leak: ~600MB of MALLOC_TINY/NANO growth -> OOM/Jetsam.
        for _tid in token_ids:
            self.order_books.pop(_tid, None)

        id_key = self._asset_ids_key()
        if channel == "market":
            message: Dict[str, Any] = {"operation": "unsubscribe", id_key: token_ids}
        else:
            message = {"type": "unsubscribe", "channel": channel, id_key: token_ids}

        await self._safe_send_json(message)

    def add_callback(self, callback: Callable):
        """Add callback for order book updates"""
        self.callbacks.append(callback)

    async def listen(self):
        """Listen for WebSocket messages"""
        self.running = True

        while self.running:
            if self.ws is None:
                connected = await self.connect()
                if not connected:
                    await asyncio.sleep(self._reconnect_delay)
                    continue

            try:
                async for msg in self.ws:
                    self.last_frame_mono = asyncio.get_event_loop().time()
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if msg.data in ("PONG", "PING"):
                            pass  # 2026-07-12 Polymarket app-level keepalive echo, not market data
                        else:
                            await self._handle_message(json.loads(msg.data))
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"WebSocket error: {msg.data}")
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSE:
                        logger.warning("WebSocket closed by server")
                        break
            except Exception as e:
                logger.error(f"Error in WebSocket listen loop: {e}")

            # Attempt reconnect
            if self.running:
                # 2026-07-12: capture WHY the recv loop ended (heartbeat teardown
                # vs server close vs error). close_code 1006 = abnormal/internal.
                _cc = getattr(self.ws, "close_code", None) if self.ws else None
                try:
                    _exc = self.ws.exception() if self.ws else None
                except Exception:
                    _exc = None
                logger.warning("WS_RECV_ENDED close_code=%s exception=%r", _cc, _exc)
                logger.info(f"Reconnecting in {self._reconnect_delay} seconds...")
                await asyncio.sleep(self._reconnect_delay)
                await self.connect()

    async def _safe_send_json(self, message: Any) -> bool:
        """Send JSON guarded against a closing transport. 2026-07-12: the 15s
        re-subscribe raced a server-closing socket -> ClientConnectionResetError
        ('Cannot write to closing transport') -> 1006 churn. Swallow instead."""
        ws = self.ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_json(message)
            return True
        except (ConnectionResetError, aiohttp.ClientError, RuntimeError) as e:
            logger.debug("ws send_json skipped (closing transport): %r", e)
            return False

    async def keepalive(self):
        """2026-07-12: Polymarket CLOB market WS requires a client APP-LEVEL text
        'PING' every ~10s (server replies 'PONG'); without it the server drops the
        socket (close 1006) -- the residual churn after the aiohttp protocol-
        heartbeat fix (protocol PING is the WRONG kind; Polymarket ignores it).
        Config: trading.clob_ws.ws_app_ping_sec (default 10, 0=off)."""
        while True:
            try:
                iv = float(self._clob_ws_cfg().get("ws_app_ping_sec", 10) or 0)
                if iv <= 0:
                    await asyncio.sleep(30)
                    continue
                await asyncio.sleep(iv)
                ws = self.ws
                if ws is None or ws.closed or not self.running:
                    continue
                try:
                    await ws.send_str("PING")
                except (ConnectionResetError, aiohttp.ClientError, RuntimeError) as e:
                    logger.debug("keepalive PING send skipped: %r", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("keepalive: %s", e)

    async def silence_watchdog(self):
        """2026-07-11 (py-clob-client #292): the CLOB WS can accept the
        connection and subscriptions yet send ZERO book data for hours — no
        error, no close — so listen()'s async-for waits forever while the bot
        silently degrades to REST-only pricing. After silence_reconnect_sec
        (trading.clob_ws, default 120, 0=off) with no frames WHILE
        subscriptions exist, force-close the socket; listen() reconnects and
        the 15s sync loop re-subscribes (chunked). Never touches a socket
        that is receiving frames."""
        while True:
            try:
                thr = float(self._clob_ws_cfg().get("silence_reconnect_sec", 120) or 0)
                await asyncio.sleep(min(30.0, thr) if thr > 0 else 60.0)
                if thr <= 0 or not self.running or self.ws is None:
                    continue
                if not any(self.subscriptions.values()):
                    continue
                now = asyncio.get_event_loop().time()
                if self.last_frame_mono and (now - self.last_frame_mono) > thr:
                    logger.warning(
                        "WS_SILENCE_WATCHDOG: no frames for %.0fs with %d subscriptions — forcing reconnect",
                        now - self.last_frame_mono,
                        sum(len(v) for v in self.subscriptions.values()),
                    )
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("silence_watchdog: %s", e)

    async def _handle_message(self, data: Any):
        """Handle incoming WebSocket message.

        Polymarket can send either a single event object or a batch list of
        event objects in one websocket frame.
        """
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._handle_message(item)
                else:
                    logger.debug("Ignoring non-dict websocket batch item: %r", item)
            return

        if not isinstance(data, dict):
            logger.debug("Ignoring unexpected websocket payload type: %r", type(data).__name__)
            return

        msg_type = data.get("event_type") or data.get("type")

        if msg_type == "book":
            await self._handle_book_update(data)
        elif msg_type == "price_change":
            await self._handle_price_change(data)
        elif msg_type == "error":
            logger.error(f"Server error: {data.get('message')}")

    def _token_id_from_event(self, data: Dict[str, Any]) -> Optional[str]:
        token_id = data.get("asset_id") or data.get("token_id")
        return str(token_id) if token_id else None

    def _parse_orders(self, orders: Any, *, bids: bool) -> List[Dict[str, float]]:
        """Parse CLOB price levels. Bids: highest price first. Asks: lowest price first."""
        if not isinstance(orders, list):
            return []

        parsed: List[Dict[str, float]] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            try:
                price = float(order["price"])
                size = float(order["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if size > 0:
                parsed.append({"price": price, "size": size})

        parsed.sort(key=lambda x: x["price"], reverse=bids)
        return parsed

    async def _notify_book_update(self, token_id: str, book: OrderBook):
        for callback in self.callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(token_id, book)
                else:
                    callback(token_id, book)
            except Exception as e:
                logger.error(f"Error in callback: {e}")

    async def _handle_book_update(self, data: Dict[str, Any]):
        """Handle full order book snapshot."""
        token_id = self._token_id_from_event(data)
        if not token_id:
            return

        if token_id not in self.order_books:
            self.order_books[token_id] = OrderBook(token_id=token_id)

        book = self.order_books[token_id]

        if "bids" in data:
            book.bids = self._parse_orders(data["bids"], bids=True)

        if "asks" in data:
            book.asks = self._parse_orders(data["asks"], bids=False)

        book.last_update = asyncio.get_event_loop().time()
        await self._notify_book_update(token_id, book)

    async def _handle_price_change(self, data: Dict[str, Any]):
        """Handle order book delta update."""
        changes = data.get("price_changes")
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict):
                    event = {**data, **change}
                    event.pop("price_changes", None)
                    await self._handle_price_change(event)
            return

        token_id = self._token_id_from_event(data)
        if not token_id:
            return

        if token_id not in self.order_books:
            self.order_books[token_id] = OrderBook(token_id=token_id)

        book = self.order_books[token_id]
        updated = False

        if "bids" in data:
            book.bids = self._merge_orders(book.bids, data["bids"], bids=True)
            updated = True
        if "asks" in data:
            book.asks = self._merge_orders(book.asks, data["asks"], bids=False)
            updated = True

        if not updated:
            side = str(data.get("side") or "").upper()
            price = data.get("price")
            size = data.get("size", data.get("new_size", 0))
            if side in {"BUY", "BID"}:
                book.bids = self._merge_orders(
                    book.bids, [{"price": price, "size": size}], bids=True
                )
                updated = True
            elif side in {"SELL", "ASK"}:
                book.asks = self._merge_orders(
                    book.asks, [{"price": price, "size": size}], bids=False
                )
                updated = True

        if updated:
            book.last_update = asyncio.get_event_loop().time()
            await self._notify_book_update(token_id, book)

    def _merge_orders(
        self, existing: List[Dict], updates: List[Dict], *, bids: bool
    ) -> List[Dict]:
        """Merge L2 deltas. Bids: highest price first. Asks: lowest price first."""
        orders: Dict[float, float] = {}
        for o in existing:
            try:
                orders[float(o["price"])] = float(o["size"])
            except (KeyError, TypeError, ValueError):
                continue

        for update in updates:
            try:
                price = float(update["price"])
                size = float(update["size"])
            except (KeyError, TypeError, ValueError):
                continue

            if size == 0:
                orders.pop(price, None)
            else:
                orders[price] = size

        result = [{"price": p, "size": s} for p, s in orders.items()]
        result.sort(key=lambda x: x["price"], reverse=bids)
        return result

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Get current order book for a token"""
        return self.order_books.get(token_id)

    def snapshot_order_book_json(
        self, token_id: str, max_levels: int = 12
    ) -> Optional[Dict[str, Any]]:
        """Serialize cached WS book for dashboard JSON (bids high→low, asks low→high)."""
        book = self.order_books.get(token_id)
        if not book:
            return None

        def _take(levels: List[Dict[str, float]], n: int) -> List[Dict[str, float]]:
            out: List[Dict[str, float]] = []
            for row in levels[:n]:
                try:
                    out.append(
                        {
                            "price": float(row["price"]),
                            "size": float(row["size"]),
                        }
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            return out

        return {
            "token_id": token_id,
            "bids": _take(book.bids, max_levels),
            "asks": _take(book.asks, max_levels),
            "ws_last_update_mono": book.last_update,
        }

    def get_spread(self, token_id_yes: str, token_id_no: str) -> Optional[float]:
        """Calculate spread between YES and NO tokens"""
        book_yes = self.order_books.get(token_id_yes)
        book_no = self.order_books.get(token_id_no)

        if book_yes and book_no and book_yes.best_ask and book_no.best_bid:
            # YES price + NO price should equal 1 (minus spread)
            return book_yes.best_ask + (1 - book_no.best_bid)
        return None
