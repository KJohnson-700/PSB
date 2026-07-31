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
        # 2026-07-29 WS PING/PONG DIAGNOSTIC (log-only, no behavior change): prove
        # whether the 1006 deaths land before/after the app-level keepalive and
        # whether Polymarket actually answers our app-PING. Per-connection counters,
        # reset in connect(); surfaced on the WS_RECV_ENDED death line.
        self._connect_mono: float = 0.0
        self._ping_sent_mono: float = 0.0
        self._pong_recv_mono: float = 0.0
        self._ping_count: int = 0
        self._pong_count: int = 0
        # Last cadence decision from main._clob_ws_subscription_loop (log-only); the
        # loop stamps these so the death line can show whether fast-resub was active.
        self._last_subloop_delay: Optional[float] = None
        self._last_subloop_reason: str = ""
        # 2026-07-29 Fix B (subscribe-on-open + defer cold-start connect). Polymarket
        # kills a market socket that opens without subscribing promptly (official
        # agent-skills/websocket.md: "send subscription right after open to avoid
        # immediate disconnection") — our cold-start sockets idle-died ~9s (subs=0,
        # NO_PONG). main.py wires these: on_connect_subscribe subscribes the instant the
        # socket opens; subscription_ready_check gates the FIRST connect until the scanner
        # token universe is primed so we never idle an unsubscribed socket. Both optional
        # (unset = current connect-immediately behavior, e.g. the user channel).
        self.on_connect_subscribe: Optional[Callable[[], Any]] = None
        self.subscription_ready_check: Optional[Callable[[], bool]] = None
        self._connect_defer_started_mono: float = 0.0

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
            # 2026-07-29 diagnostic: reset per-connection PING/PONG counters so the
            # next WS_RECV_ENDED reflects THIS socket's keepalive history only.
            self._connect_mono = self.last_frame_mono
            self._ping_sent_mono = 0.0
            self._pong_recv_mono = 0.0
            self._ping_count = 0
            self._pong_count = 0
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
        """Subscribe to a channel for specific tokens.

        2026-07-29 SUBSCRIPTION TRUTH: build the frame FIRST, and only commit
        subscription state / initial-market flag / order books / success log AFTER
        _safe_send_json confirms the send went out. Previously state was mutated
        (and "Subscribed" logged) BEFORE the send, so a failed send into a closing
        transport left the bot believing tokens were subscribed while the server
        never got them — bookkeeping lied. That defeated the fast-resub loop (it saw
        subscriptions non-empty, backed off to 15s, and the un-fed socket idle-died)
        and is a prime suspect for the 1006 churn / ws_cov stuck ~0.52. On a failed
        send we leave state untouched so the next sync retries the same chunk.
        """
        if self.ws is None:
            logger.error("WebSocket not connected")
            return

        # Polymarket market channel: first message uses type "market"; later deltas use operation.
        id_key = self._asset_ids_key()
        _initial_market = channel == "market" and not self._sent_initial_market_subscription
        if channel == "market":
            # 2026-07-29 Fix A (official Polymarket agent-skills/websocket.md): the
            # initial type:market subscribe requires custom_feature_enabled — all three
            # fields (type, assets_ids, custom_feature_enabled) are documented as required;
            # it also enables best_bid_ask/new_market/market_resolved events. We were
            # sending only {type, assets_ids}. Delta (operation:subscribe) frames add
            # tokens to an already-featured connection, so the flag is only on the initial.
            message: Dict[str, Any] = (
                {"type": "market", id_key: token_ids, "custom_feature_enabled": True}
                if _initial_market
                else {"operation": "subscribe", id_key: token_ids}
            )
        else:
            message = {"type": "subscribe", "channel": channel, id_key: token_ids}

        ok = await self._safe_send_json(message)
        if not ok:
            logger.warning(
                "WS_SUBSCRIBE_FAILED %s: %d tokens NOT sent (closing transport) — "
                "state unchanged, next sync retries",
                channel, len(token_ids),
            )
            return

        # Send confirmed — NOW commit state.
        if _initial_market:
            self._sent_initial_market_subscription = True
        if channel not in self.subscriptions:
            self.subscriptions[channel] = set()
        self.subscriptions[channel].update(token_ids)
        for token_id in token_ids:
            if token_id not in self.order_books:
                self.order_books[token_id] = OrderBook(token_id=token_id)
        # Keep the ORIGINAL "Subscribed to <channel> for N tokens" wording so BOTH the
        # local watchers and the VPS watcher (scripts/vps_watch.sh WSS_STATE = greps
        # "Subscribed to market for [0-9]+ token") keep working — the truthfulness fix is
        # that this now only fires AFTER a confirmed send (failed sends log WS_SUBSCRIBE_FAILED
        # above instead of falsely logging "Subscribed"). Don't rename this string.
        logger.info("Subscribed to %s for %d tokens", channel, len(token_ids))

    async def unsubscribe(self, channel: str, token_ids: List[str]):
        """Unsubscribe from a channel"""
        if self.ws is None:
            return

        id_key = self._asset_ids_key()
        if channel == "market":
            message: Dict[str, Any] = {"operation": "unsubscribe", id_key: token_ids}
        else:
            message = {"type": "unsubscribe", "channel": channel, id_key: token_ids}

        # 2026-07-29 SUBSCRIPTION TRUTH (mirror of subscribe): send FIRST.
        ok = await self._safe_send_json(message)

        # ALWAYS free the per-token OrderBook — memory safety wins over send status.
        # subscribe() creates one per token; without this pop, books for expired markets
        # (new token_ids every 5/15/60-min window) accumulate forever = THE ~600MB
        # MALLOC_TINY/NANO OOM/Jetsam leak. A stray post-unsubscribe frame only recreates
        # it transiently.
        for _tid in token_ids:
            self.order_books.pop(_tid, None)

        # But only DROP the subscription-set membership when the unsubscribe actually
        # went out. On a failed send the token stays in `subscriptions` (=`have`) so the
        # next sync's to_remove = have - want RETRIES the unsubscribe, instead of silently
        # forgetting it while the server keeps streaming.
        if ok:
            if channel in self.subscriptions:
                self.subscriptions[channel].difference_update(token_ids)
        else:
            logger.warning(
                "WS_UNSUBSCRIBE_FAILED %s: %d tokens send failed — membership kept, "
                "next sync retries",
                channel, len(token_ids),
            )

    def add_callback(self, callback: Callable):
        """Add callback for order book updates"""
        self.callbacks.append(callback)

    def _should_defer_connect(self) -> bool:
        """2026-07-29 Fix B2: hold off opening the market socket until the token universe
        is ready, so we never idle an unsubscribed socket into a ~9s/1006 kill. Only
        active when main.py wired subscription_ready_check (market channel). FAIL-OPEN:
        after connect_defer_max_sec of waiting, connect anyway — a readiness-callback bug
        must never cause a permanent WS outage (REST carries pricing meanwhile)."""
        check = self.subscription_ready_check
        if check is None:
            return False
        try:
            ready = bool(check())
        except Exception:
            return False  # provider error -> don't block; connect
        now = asyncio.get_event_loop().time()
        if ready:
            self._connect_defer_started_mono = 0.0
            return False
        if not self._connect_defer_started_mono:
            self._connect_defer_started_mono = now
        cap = float(self._clob_ws_cfg().get("connect_defer_max_sec", 45.0) or 0.0)
        if cap > 0 and (now - self._connect_defer_started_mono) > cap:
            logger.warning(
                "clob_ws: connect-defer cap %.0fs exceeded with no token universe — "
                "connecting anyway (fail-open)", cap,
            )
            self._connect_defer_started_mono = 0.0
            return False
        return True

    async def _subscribe_on_open(self) -> None:
        """2026-07-29 Fix B1: subscribe the instant the socket opens (official spec:
        "send subscription right after open to avoid immediate disconnection") instead
        of waiting up to the subscription-loop cadence. No-op if unwired (user channel)."""
        cb = self.on_connect_subscribe
        if cb is None:
            return
        try:
            res = cb()
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            logger.debug("subscribe-on-open failed: %r", e)

    async def listen(self):
        """Listen for WebSocket messages"""
        self.running = True

        while self.running:
            if self.ws is None:
                # 2026-07-29 Fix B2: don't open an unsubscribed market socket during
                # cold start — Polymarket kills it ~9s (1006, NO_PONG). Wait until the
                # token universe primes, then connect+subscribe together. Reconnects are
                # unaffected (universe already primed -> ready True -> connect at once).
                if self._should_defer_connect():
                    await asyncio.sleep(float(self._clob_ws_cfg().get("connect_defer_poll_sec", 1.0) or 1.0))
                    continue
                connected = await self.connect()
                if not connected:
                    await asyncio.sleep(self._reconnect_delay)
                    continue
                # Fix B1: subscribe immediately on open (close the connect->subscribe gap).
                await self._subscribe_on_open()

            try:
                async for msg in self.ws:
                    self.last_frame_mono = asyncio.get_event_loop().time()
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if msg.data in ("PONG", "PING"):
                            # 2026-07-12 Polymarket app-level keepalive echo, not market data.
                            # 2026-07-29 diagnostic: record PONG arrival so the death line can
                            # prove the server is (or is NOT) answering our app-PING.
                            if msg.data == "PONG":
                                self._pong_recv_mono = self.last_frame_mono
                                self._pong_count += 1
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
                # 2026-07-29 PING/PONG DIAGNOSTIC: enrich the death line so we can prove
                # whether a 1006 fires BEFORE or AFTER the expected ~10s app-PING, and
                # whether Polymarket ever answered it on this socket. NO_PONG_THIS_SOCKET
                # => server never PONGed => keepalive isn't reaching it (server-TTL kill);
                # ping_sent age > ping interval near death => our PING stopped going out.
                _now = asyncio.get_event_loop().time()
                _age = lambda _t: (round(_now - _t, 1) if _t else None)
                logger.warning(
                    "WS_RECV_ENDED close_code=%s exception=%r | sock_life=%ss "
                    "last_frame_age=%ss pings=%d(last_age=%ss) pongs=%d(last_age=%ss) "
                    "subs=%d subloop=%s(%s) %s",
                    _cc, _exc,
                    _age(self._connect_mono),
                    _age(self.last_frame_mono),
                    self._ping_count, _age(self._ping_sent_mono),
                    self._pong_count, _age(self._pong_recv_mono),
                    sum(len(v) for v in self.subscriptions.values()),
                    self._last_subloop_delay, self._last_subloop_reason,
                    ("NO_PONG_THIS_SOCKET" if self._pong_count == 0 else "pong_ok"),
                )
                logger.info(f"Reconnecting in {self._reconnect_delay} seconds...")
                await asyncio.sleep(self._reconnect_delay)
                # 2026-07-29 Fix B (Codex): route reconnects through the TOP-of-loop connect
                # path instead of calling connect() directly here — so a reopened socket also
                # gets _subscribe_on_open() (subscribe immediately, no idle gap). Dropping
                # self.ws makes the next `while` iteration hit `if self.ws is None:` and
                # reconnect+subscribe uniformly. connect() closes the stale session itself.
                self.ws = None

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
                    # 2026-07-29 diagnostic: stamp PING send so the death line shows
                    # how long before a 1006 the last app-PING actually went out.
                    self._ping_sent_mono = asyncio.get_event_loop().time()
                    self._ping_count += 1
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


class UserWebSocketClient:
    """Polymarket CLOB *user* channel — real fill/order events for our own orders.

    2026-07-29 (Phase-2 ④): the market channel (WebSocketClient) carries the public
    order book; this sibling carries authenticated USER events (order lifecycle +
    trade MATCHED/MINED/CONFIRMED) so ``order.filled_size`` becomes venue truth instead
    of a post_order-response inference. OBSERVE/CORRECTNESS ONLY — it updates fill
    accounting + logs for the journal-vs-venue cross-check; it never places, cancels,
    or gates anything. Modeled on WebSocketClient (app-level PING keepalive, reconnect).

    Auth: L2 creds (apiKey/secret/passphrase). Because creds can be re-derived mid-run
    (ensure_fresh_credentials), we take a ``creds_provider`` callable and re-read it on
    every (re)connect rather than snapshotting stale creds.
    """

    _WS_HOST = "wss://ws-subscriptions-clob.polymarket.com"

    def __init__(
        self,
        config: Dict[str, Any],
        creds_provider: Callable[[], Optional[Any]],
        on_user_event: Callable[[Dict[str, Any]], Any],
    ):
        self.config = config
        self._creds_provider = creds_provider
        self._on_user_event = on_user_event
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self.running = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self.last_frame_mono: float = 0.0

    def _clob_ws_cfg(self) -> Dict[str, Any]:
        return (self.config.get("trading") or {}).get("clob_ws") or {}

    def _ws_url(self) -> str:
        ws_cfg = self._clob_ws_cfg()
        explicit = ws_cfg.get("user_wss_url")
        if explicit:
            return str(explicit).rstrip("/")
        return f"{self._WS_HOST}/ws/user"

    def _auth_payload(self) -> Optional[Dict[str, Any]]:
        """Build the user-channel subscribe frame from FRESH creds, or None if unavailable."""
        try:
            creds = self._creds_provider() if callable(self._creds_provider) else None
        except Exception as exc:
            logger.debug("user WS creds_provider raised: %r", exc)
            creds = None
        if not creds:
            return None
        # creds may be an (api_key, secret, passphrase) tuple or an ApiCreds-like object.
        if isinstance(creds, (tuple, list)) and len(creds) >= 3:
            api_key, secret, passphrase = creds[0], creds[1], creds[2]
        else:
            api_key = getattr(creds, "api_key", None)
            secret = getattr(creds, "api_secret", None)
            passphrase = getattr(creds, "api_passphrase", None)
        if not (api_key and secret and passphrase):
            return None
        # All-account mode: OMIT `markets` entirely (the official example only includes
        # it to FILTER to specific condition ids; an empty list can be read as "no
        # markets"). Optionally filter via config user_markets when explicitly set.
        payload: Dict[str, Any] = {
            "type": "user",
            "auth": {"apiKey": str(api_key), "secret": str(secret), "passphrase": str(passphrase)},
        }
        _mkts = self._clob_ws_cfg().get("user_markets")
        if isinstance(_mkts, (list, tuple)) and len(_mkts) > 0:
            payload["markets"] = [str(m) for m in _mkts]
        return payload

    async def connect(self) -> bool:
        try:
            if self._session and not self._session.closed:
                await self._session.close()
            payload = self._auth_payload()
            if payload is None:
                logger.warning("user WS: no L2 creds available yet — deferring connect")
                return False
            self._session = aiohttp.ClientSession()
            url = self._ws_url()
            _raw_hb = self._clob_ws_cfg().get("ws_heartbeat_sec")
            _hb = float(_raw_hb) if _raw_hb not in (None, "", 0, "0") else None
            self.ws = await self._session.ws_connect(url, heartbeat=_hb)
            await self.ws.send_json(payload)  # authenticate + subscribe
            self._reconnect_delay = 1
            self.last_frame_mono = asyncio.get_event_loop().time()
            logger.info("Connected to Polymarket USER WebSocket (%s)", url)
            return True
        except Exception as e:
            logger.error("Failed to connect to user WebSocket: %s", e)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
            return False

    async def disconnect(self):
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("Disconnected from user WebSocket")

    async def keepalive(self):
        """App-level text 'PING' every ~10s (server replies 'PONG'). The user channel is
        QUIET (no book frames between fills), so unlike the market channel it cannot lean
        on inbound traffic to stay alive — without this PING Polymarket drops the socket
        (close 1006). Dedicated key user_ws_ping_sec (default 10; NOT the market channel's
        ws_app_ping_sec, which is 0/off)."""
        while True:
            try:
                iv = float(self._clob_ws_cfg().get("user_ws_ping_sec", 10) or 0)
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
                    logger.debug("user WS keepalive PING skipped: %r", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("user WS keepalive: %s", e)

    async def listen(self):
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
                            continue
                        try:
                            await self._dispatch(json.loads(msg.data))
                        except Exception as e:
                            logger.debug("user WS message parse/dispatch error: %r", e)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error("user WS error: %s", msg.data)
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSE:
                        logger.warning("user WS closed by server")
                        break
            except Exception as e:
                logger.error("Error in user WS listen loop: %s", e)
            if self.running:
                _cc = getattr(self.ws, "close_code", None) if self.ws else None
                logger.warning("USER_WS_RECV_ENDED close_code=%s; reconnecting in %ss", _cc, self._reconnect_delay)
                # Drop the socket so connect() re-auths with fresh creds on the next loop.
                try:
                    if self.ws:
                        await self.ws.close()
                except Exception:
                    pass
                self.ws = None
                await asyncio.sleep(self._reconnect_delay)

    async def _dispatch(self, data: Any):
        """Route a user-channel frame (single object or batch list) to the callback."""
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._dispatch(item)
            return
        if not isinstance(data, dict):
            return
        cb = self._on_user_event
        try:
            if asyncio.iscoroutinefunction(cb):
                await cb(data)
            else:
                cb(data)
        except Exception as e:
            logger.error("user WS callback error: %s", e)
