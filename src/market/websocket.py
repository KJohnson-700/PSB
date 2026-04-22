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

    WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws"

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

    async def connect(self) -> bool:
        """Connect to WebSocket server"""
        try:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = aiohttp.ClientSession()
            self.ws = await self._session.ws_connect(self.WS_ENDPOINT, heartbeat=30)
            self._reconnect_delay = 1
            logger.info("Connected to Polymarket WebSocket")
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

        # Send subscription message
        message = {"type": "subscribe", "channel": channel, "keys": token_ids}

        await self.ws.send_json(message)
        logger.info(f"Subscribed to {channel} for {len(token_ids)} tokens")

    async def unsubscribe(self, channel: str, token_ids: List[str]):
        """Unsubscribe from a channel"""
        if self.ws is None:
            return

        if channel in self.subscriptions:
            self.subscriptions[channel].difference_update(token_ids)

        message = {"type": "unsubscribe", "channel": channel, "keys": token_ids}

        await self.ws.send_json(message)

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
                    if msg.type == aiohttp.WSMsgType.TEXT:
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
                logger.info(f"Reconnecting in {self._reconnect_delay} seconds...")
                await asyncio.sleep(self._reconnect_delay)
                await self.connect()

    async def _handle_message(self, data: Dict[str, Any]):
        """Handle incoming WebSocket message"""
        msg_type = data.get("type")

        if msg_type == "book":
            await self._handle_book_update(data)
        elif msg_type == "price_change":
            await self._handle_price_change(data)
        elif msg_type == "error":
            logger.error(f"Server error: {data.get('message')}")

    async def _handle_book_update(self, data: Dict[str, Any]):
        """Handle order book delta update"""
        token_id = data.get("token_id")
        if not token_id or token_id not in self.order_books:
            return

        book = self.order_books[token_id]

        # Update bids
        if "bids" in data:
            book.bids = self._merge_orders(book.bids, data["bids"])

        # Update asks
        if "asks" in data:
            book.asks = self._merge_orders(book.asks, data["asks"])

        book.last_update = asyncio.get_event_loop().time()

        # Notify callbacks
        for callback in self.callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(token_id, book)
                else:
                    callback(token_id, book)
            except Exception as e:
                logger.error(f"Error in callback: {e}")

    async def _handle_price_change(self, data: Dict[str, Any]):
        """Handle price change notification"""
        token_id = data.get("token_id")
        price = data.get("price")

        if token_id in self.order_books:
            book = self.order_books[token_id]
            # Update best bid/ask based on new price
            if book.bids and price <= book.bids[0]["price"]:
                book.bids[0]["price"] = price
            elif book.asks and price >= book.asks[0]["price"]:
                book.asks[0]["price"] = price

    def _merge_orders(self, existing: List[Dict], updates: List[Dict]) -> List[Dict]:
        """Merge order updates into existing orders"""
        orders = {o["price"]: o["size"] for o in existing}

        for update in updates:
            price = update["price"]
            size = update["size"]

            if size == 0:
                orders.pop(price, None)
            else:
                orders[price] = size

        # Convert back to sorted list
        result = [{"price": p, "size": s} for p, s in orders.items()]
        result.sort(key=lambda x: x["price"], reverse=True)  # Descending for bids

        return result

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Get current order book for a token"""
        return self.order_books.get(token_id)

    def get_spread(self, token_id_yes: str, token_id_no: str) -> Optional[float]:
        """Calculate spread between YES and NO tokens"""
        book_yes = self.order_books.get(token_id_yes)
        book_no = self.order_books.get(token_id_no)

        if book_yes and book_no and book_yes.best_ask and book_no.best_bid:
            # YES price + NO price should equal 1 (minus spread)
            return book_yes.best_ask + (1 - book_no.best_bid)
        return None
