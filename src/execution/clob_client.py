"""
Execution Module
Order execution and risk management
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
try:
    from py_clob_client.client import ClobClient as PyClobClient, ApiCreds
    from py_clob_client.clob_types import (
        AssetType,
        BalanceAllowanceParams,
        OrderArgs,
        OrderType,
    )
except ImportError:
    PyClobClient = None
    ApiCreds = None
    AssetType = None
    BalanceAllowanceParams = None
    OrderArgs = None
    OrderType = None

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Order:
    """Represents a trade order"""

    order_id: str
    market_id: str
    token_id: str
    side: str
    outcome: str
    price: float
    size: float
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = None
    updated_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()


@dataclass
class Position:
    """Represents an open position"""

    position_id: str
    market_id: str
    market_question: str
    outcome: str
    size: float
    entry_price: float
    current_price: float
    pnl: float
    opened_at: datetime
    end_date: Optional[datetime]
    strategy: str = "unknown"
    # YES = entry_price quotes YES token (BUY_YES, SELL_YES). NO = BUY_NO (NO token).
    entry_leg: str = "YES"
    window_size: str = ""
    # Highest token mark seen while the position was open; used for peak-aware
    # profit protection on up/down exits.
    peak_token_price: float = 0.0
    # CLOB outcome token ids (from market clobTokenIds); used for book/mid by token and dashboard.
    token_id_yes: str = ""
    token_id_no: str = ""
    edge: float = 0.0
    confidence: float = 0.0
    entry_signal: Dict[str, Any] = field(default_factory=dict)


class CLOBClient:
    """CLOB Client Wrapper for Polymarket"""

    def __init__(self, config: Dict[str, Any]):
        # Root config: trading.* lives at top level, not under polymarket.
        self._root_config = config
        self.config = config.get("polymarket", {})
        self.api_endpoint = self.config.get(
            "api_endpoint", "https://clob.polymarket.com"
        )
        self.chain_id = self.config.get("chain_id", 137)
        self.private_key = None
        self.creds = None
        self.client = None
        self.pending_orders: Dict[str, Order] = {}
        self.order_history: List[Order] = []
        self._max_order_history = 1000
        # Level-0 client for public `get_order_book` when no signer/trading keys set.
        self._readonly_py_client: Optional[Any] = None
        # Derived L2 credentials expire ~7 days after creation; Polymarket does not
        # rotate them and auth calls fail silently once stale. Track when they were
        # set and refuse to trade past a configurable age so we fail loud, not silent.
        self._creds_set_at: Optional[datetime] = None
        self._creds_max_age = timedelta(
            hours=float(self.config.get("creds_max_age_hours", 144))  # 6 days
        )
        # When creds go stale, re-derive L2 from the L1 signer the py-clob-client
        # already holds (idempotent, no raw-key re-handling) instead of refusing.
        self._auto_rederive_credentials = bool(
            self.config.get("auto_rederive_credentials", True)
        )

    def set_credentials(
        self,
        private_key: str,
        api_key: str = None,
        api_secret: str = None,
        api_passphrase: str = None,
    ):
        if PyClobClient is None or ApiCreds is None:
            raise RuntimeError(
                "py-clob-client is required for live CLOB execution. "
                "Install project requirements before running with dry_run=false."
            )
        creds = ApiCreds(api_key, api_secret, api_passphrase)
        self.client = PyClobClient(
            host=self.api_endpoint,
            chain_id=self.chain_id,
            key=private_key,
            creds=creds,
        )
        # Clear plaintext copies — PyClobClient holds its own internal copy
        del private_key
        self.private_key = None
        self._creds_set_at = datetime.now()

    def credentials_age(self) -> Optional[timedelta]:
        """Time since L2 credentials were set, or None if never set."""
        if self._creds_set_at is None:
            return None
        return datetime.now() - self._creds_set_at

    def credentials_expired(self) -> bool:
        """
        True when derived L2 creds are older than the configured max age.

        Polymarket's L2 credentials expire ~7 days after derivation with no
        rotation and no expiry signal — calls just start failing with auth
        errors. We treat creds past `creds_max_age_hours` (default 6 days) as
        expired so the failure is loud and pre-trade, not a silent mid-session
        auth death on day ~8. If creds were never set we cannot judge age, so
        this returns False and the existing `self.client` guards take over.
        """
        age = self.credentials_age()
        if age is None:
            return False
        return age >= self._creds_max_age

    async def _rederive_l2_credentials(self) -> bool:
        """
        Re-derive L2 API creds from the L1 signer the py-clob-client already holds.

        `set_credentials` hands the L1 private key to `PyClobClient(key=...)` and
        then deletes its own plaintext copy. The client keeps an internal signer,
        so we can mint fresh L2 creds via an L1 signature without ever re-handling
        the raw key. `create_or_derive_api_creds` is idempotent (create if absent,
        derive if present) — the same restart-safe bootstrap pattern the D8-X SDK
        documents. Returns True on success and resets the expiry clock.
        """
        if not self.client:
            logger.error("Cannot re-derive credentials: CLOB client not initialized.")
            return False
        derive = getattr(self.client, "create_or_derive_api_creds", None)
        set_creds = getattr(self.client, "set_api_creds", None)
        if not callable(derive) or not callable(set_creds):
            logger.error(
                "Installed py-clob-client lacks create_or_derive_api_creds/"
                "set_api_creds — cannot self-heal expired credentials. "
                "Re-derive manually (L1 sign) before trading."
            )
            return False
        try:
            loop = asyncio.get_event_loop()
            new_creds = await loop.run_in_executor(None, derive)
            await loop.run_in_executor(None, lambda: set_creds(new_creds))
        except Exception as exc:
            logger.error("Failed to re-derive L2 credentials: %s", exc)
            return False
        self._creds_set_at = datetime.now()
        logger.info("Re-derived L2 credentials from L1 signer; expiry clock reset.")
        return True

    async def ensure_fresh_credentials(self, force_rederive: bool = False) -> bool:
        """
        Guarantee usable L2 creds before a live action.

        Returns True when creds are fresh, or when a stale set was successfully
        re-derived. Returns False only when creds are stale and could not be
        refreshed (auto-rederive disabled, no signer, or derive failed) — the
        caller should then refuse to trade rather than fire a silent auth error.

        `force_rederive=True` mints fresh creds unconditionally (idempotent
        bootstrap pattern) regardless of the tracked age — used at startup, where
        `_creds_set_at` only records when we *loaded* the .env creds, not when
        they were actually derived, so they could already be near expiry.
        """
        if force_rederive and self.client:
            if await self._rederive_l2_credentials():
                return True
            logger.warning(
                "Forced credential re-derive failed; falling back to staleness check."
            )
        if not self.credentials_expired():
            return True
        if not self._auto_rederive_credentials:
            logger.warning(
                "L2 credentials are stale and auto-rederive is disabled; "
                "refusing to use them."
            )
            return False
        return await self._rederive_l2_credentials()

    @staticmethod
    def _normalize_usdc_amount(raw: Any) -> Optional[float]:
        """Normalize CLOB integer micro-USDC or decimal-string balances to dollars."""
        if raw is None:
            return None
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if value < 0:
            return None
        # CLOB balance-allowance returns collateral in USDC base units on current
        # py-clob-client builds. Small decimal strings are already human USDC.
        if value == value.to_integral_value() and value >= Decimal("100000"):
            value = value / Decimal("1000000")
        return float(value)

    @classmethod
    def _extract_cash_balance(cls, payload: Any) -> Optional[float]:
        if not isinstance(payload, dict):
            return None
        for key in ("balance", "cash", "collateral", "collateral_balance"):
            amount = cls._normalize_usdc_amount(payload.get(key))
            if amount is not None:
                return amount
        return None

    async def get_cash_balance(self) -> Optional[float]:
        """Fetch authenticated Polymarket collateral balance in USDC."""
        if not self.client:
            logger.error("CLOB client not initialized — cannot fetch live wallet bankroll.")
            return None
        if BalanceAllowanceParams is None or AssetType is None:
            logger.error("py-clob-client balance types unavailable — cannot fetch wallet bankroll.")
            return None
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            loop = asyncio.get_event_loop()
            payload = await loop.run_in_executor(
                None,
                lambda: self.client.get_balance_allowance(params),
            )
        except Exception as exc:
            logger.error("Error fetching Polymarket wallet bankroll: %s", exc)
            return None
        balance = self._extract_cash_balance(payload)
        if balance is None:
            logger.error("Could not parse Polymarket wallet bankroll payload: %s", payload)
        return balance

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        market_id: str = None,
        post_only: bool = False,
        dry_run: bool = True,
        order_outcome: Optional[str] = None,
        order_type: str = "GTC",
    ) -> Optional[Order]:
        _outcome = (
            order_outcome
            if order_outcome in ("YES", "NO")
            else ("YES" if side == "BUY" else "NO")
        )
        if dry_run:
            logger.info(
                f"[DRY RUN] Would place order: {side} {size} @ {price} ({order_type})"
            )
            order = Order(
                order_id=f"dry_{datetime.now().timestamp()}",
                market_id=market_id or "",
                token_id=token_id,
                side=side,
                outcome=_outcome,
                price=price,
                size=size,
                filled_size=size,
                status=OrderStatus.FILLED,
            )
            self.order_history.append(order)
            if len(self.order_history) > self._max_order_history:
                self.order_history = self.order_history[-self._max_order_history :]
            return order

        if not self.client:
            logger.error("CLOB client not initialized. Call set_credentials first.")
            return None
        if not await self.ensure_fresh_credentials():
            age = self.credentials_age()
            age_h = age.total_seconds() / 3600 if age else 0
            logger.error(
                "Refusing live order: L2 credentials are %.1fh old (max %.1fh) "
                "and could not be refreshed. Re-derive credentials (L1 sign) "
                "before trading — Polymarket auth fails silently once creds "
                "expire (~7 days).",
                age_h,
                self._creds_max_age.total_seconds() / 3600,
            )
            return None
        if OrderArgs is None or OrderType is None:
            logger.error("py-clob-client order types unavailable — cannot place live order")
            return None

        # py-clob-client takes the time-in-force on post_order (GTC/FAK/FOK/GTD), NOT
        # on OrderArgs (which has no order_type/post_only fields — passing them there
        # raises). GTC = resting limit (entries); FAK = fill-and-kill / marketable
        # (take resting liquidity now, cancel the remainder) for stop/market exits so
        # the close actually fills instead of resting at a stale bid and re-gapping.
        _ot = getattr(OrderType, str(order_type).upper(), OrderType.GTC)

        order_args = OrderArgs(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
        )

        try:
            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(
                None, lambda: self.client.create_order(order_args)
            )
            resp = await loop.run_in_executor(
                None, lambda: self.client.post_order(signed_order, _ot, post_only)
            )

            order = Order(
                order_id=resp["order_id"],
                market_id=market_id or "",
                token_id=token_id,
                side=side,
                outcome=_outcome,
                price=price,
                size=size,
                status=OrderStatus.PENDING,
            )
            self.pending_orders[order.order_id] = order
            self.order_history.append(order)
            if len(self.order_history) > self._max_order_history:
                self.order_history = self.order_history[-self._max_order_history :]
            return order
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        if not self.client:
            logger.error("CLOB client not initialized.")
            return False

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: self.client.cancel(order_id))
            if order_id in self.pending_orders:
                self.pending_orders[order_id].status = OrderStatus.CANCELLED
                self.pending_orders[order_id].updated_at = datetime.now()
            return True
        except Exception as e:
            logger.error(f"Error canceling order {order_id}: {e}")
            return False

    async def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        if not self.client:
            logger.error("CLOB client not initialized.")
            return None

        try:
            loop = asyncio.get_event_loop()
            order_data = await loop.run_in_executor(
                None, lambda: self.client.get_order(order_id)
            )
            # /data/order/{id} only returns *active* orders. A filled or cancelled
            # order comes back empty (or without a usable status) — the endpoint
            # "lies by omission." Treating that empty response as PENDING would
            # strand a position that already filled, so fall back to trade history.
            status = order_data.get("status") if isinstance(order_data, dict) else None
            if status == "filled":
                return OrderStatus.FILLED
            if status == "cancelled":
                return OrderStatus.CANCELLED
            if status:
                return OrderStatus.PENDING
            # Empty/omitted: reconcile against /data/trades by venue order id.
            return await self._recover_status_from_trades(order_id)
        except Exception as e:
            logger.error(f"Error getting order status for {order_id}: {e}")
            # The order endpoint can also raise on an empty/filled order. Try the
            # trade-history fallback before giving up.
            try:
                return await self._recover_status_from_trades(order_id)
            except Exception as e2:
                logger.error(f"Trade-history fallback failed for {order_id}: {e2}")
                return None

    async def _recover_status_from_trades(
        self, order_id: str
    ) -> Optional[OrderStatus]:
        """
        Recover an order's terminal status from /data/trades when the order
        endpoint returns empty (filled/cancelled orders are dropped from it).

        Returns FILLED if any trade references this order id, else PENDING
        (resting/unmatched — not yet terminal). Never raises into the caller's
        happy path; raising here is caught and logged by get_order_status.
        """
        loop = asyncio.get_event_loop()
        trades = await loop.run_in_executor(
            None, lambda: self.client.get_trades()
        )
        # Trade payloads vary by py-clob-client version; an order id can appear
        # under several keys depending on whether we were maker or taker.
        id_keys = ("order_id", "maker_order_id", "taker_order_id")
        for trade in trades or []:
            if not isinstance(trade, dict):
                continue
            for key in id_keys:
                if trade.get(key) == order_id:
                    logger.info(
                        "Reconciled order %s as FILLED from trade history "
                        "(order endpoint returned empty).",
                        order_id,
                    )
                    return OrderStatus.FILLED
            # Some builds nest the order ids inside maker/taker sub-objects.
            for nested_key in ("maker_orders", "taker_order"):
                nested = trade.get(nested_key)
                entries = nested if isinstance(nested, list) else [nested]
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("order_id") == order_id:
                        logger.info(
                            "Reconciled order %s as FILLED from nested trade "
                            "history (order endpoint returned empty).",
                            order_id,
                        )
                        return OrderStatus.FILLED
        # No matching trade — order is resting/unmatched, not yet terminal.
        return OrderStatus.PENDING

    async def can_sell_token(self, token_id: str, market_id: str) -> bool:
        """
        Test whether a token can be sold (post-only limit) without placing a real order.
        Used as a pre-trade guard to detect 'unsellable token' risk — the failure mode
        that destroyed a bot going from $23 to $1.50 in 46 hours.

        Polls the orderbook for the given token. If bids exist at a non-zero price,
        the token is sellable. Returns False if the book is empty or only has bids
        at zero price (indicating the market maker won't take the other side).

        Args:
            token_id: The outcome token ID to test
            market_id: The parent market ID (used for logging)

        Returns:
            True if the token can likely be sold, False otherwise.
        """
        if dry_run := self._root_config.get("trading", {}).get("dry_run", True):
            return True

        if not self.client:
            logger.error("[can_sell_token] CLOB client not initialized — refusing live trade")
            return False

        try:
            loop = asyncio.get_event_loop()
            book = await loop.run_in_executor(
                None, lambda: self.client.get_order_book(token_id)
            )
            bids = book.get("bids", []) or []
            asks = book.get("asks", []) or []
            bid_count = sum(1 for b in bids if isinstance(b, dict) and b.get("price", 0) > 0)
            ask_count = sum(1 for a in asks if isinstance(a, dict) and a.get("price", 0) > 0)
            logger.debug(
                f"[can_sell_token] {market_id[:20]} token={token_id[:20]} "
                f"bids={bid_count} asks={ask_count}"
            )
            return bid_count > 0
        except Exception as e:
            logger.warning(f"[can_sell_token] check failed for {token_id[:20]} — treating as unsellable: {e}")
            return False

    def _py_client_for_public_reads(self):
        """Return authenticated client if present, else a level-0 host-only client."""
        if self.client:
            return self.client
        if PyClobClient is None:
            return None
        if self._readonly_py_client is None:
            self._readonly_py_client = PyClobClient(
                host=self.api_endpoint,
                chain_id=self.chain_id,
            )
        return self._readonly_py_client

    async def fetch_order_book_snapshot(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Public CLOB GET order book (REST). Works without trading keys."""
        pc = self._py_client_for_public_reads()
        if not pc or not token_id:
            return None
        try:
            loop = asyncio.get_event_loop()
            summary = await loop.run_in_executor(
                None, lambda: pc.get_order_book(token_id)
            )
            bids = [
                {"price": float(o.price), "size": float(o.size)}
                for o in (summary.bids or [])
            ]
            asks = [
                {"price": float(o.price), "size": float(o.size)}
                for o in (summary.asks or [])
            ]
            return {
                "token_id": token_id,
                "asset_id": getattr(summary, "asset_id", None),
                "market": getattr(summary, "market", None),
                "timestamp": getattr(summary, "timestamp", None),
                "bids": bids,
                "asks": asks,
            }
        except Exception as e:
            logger.warning("[fetch_order_book_snapshot] %s", e)
            return None

    async def get_positions(self) -> List[Position]:
        if not self.client:
            logger.error("CLOB client not initialized.")
            return []

        try:
            loop = asyncio.get_event_loop()
            positions_data = await loop.run_in_executor(None, self.client.get_positions)
            # This is a simplified mapping. The actual API response may be more complex.
            return [
                Position(
                    position_id=p["position_id"],
                    market_id=p["market_id"],
                    market_question=p.get("market_question", "N/A"),
                    outcome=p["outcome"],
                    size=p["size"],
                    entry_price=p["entry_price"],
                    current_price=p.get("current_price", p["entry_price"]),
                    pnl=p.get("pnl", 0.0),
                    opened_at=datetime.fromisoformat(p["opened_at"]),
                    end_date=datetime.fromisoformat(p["end_date"])
                    if p.get("end_date")
                    else None,
                    strategy=p.get("strategy", "unknown"),
                    entry_leg=p.get("entry_leg", "YES"),
                    window_size=str(p.get("window_size") or ""),
                    token_id_yes=str(p.get("token_id_yes") or ""),
                    token_id_no=str(p.get("token_id_no") or ""),
                )
                for p in positions_data
            ]
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []


class RiskManager:
    """Risk Management Engine"""

    @staticmethod
    def position_entry_notional(p: Any) -> float:
        """Approximate USD cost at entry for exposure caps (matches evaluate_entry logic)."""
        entry_price = float(getattr(p, "entry_price", 0) or 0)
        sz = float(getattr(p, "size", 0) or 0)
        return sz * entry_price

    def __init__(self, config: Dict[str, Any]):
        self.config = config  # Pass the full config
        risk_config = self.config.get("risk", {})
        trading_config = self.config.get("trading", {})
        self.term_risk_config = self.config.get("term_risk", {})
        self.max_concurrent_positions = risk_config.get("max_concurrent_positions", 10)
        self.max_trades_per_day = risk_config.get("max_trades_per_day", 50)
        self.paper_max_trades_per_day = risk_config.get(
            "paper_max_trades_per_day", self.max_trades_per_day
        )
        self.daily_loss_limit = risk_config.get("daily_loss_limit", 0.15)
        self.emergency_stop_loss = risk_config.get("emergency_stop_loss", 0.25)
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.bankroll = 0.0
        self.last_reset = datetime.now()
        self.emergency_stopped = False
        self.active_positions: Dict[str, Position] = {}

    def effective_max_trades_per_day(self) -> int:
        if self.config.get("trading", {}).get("dry_run", True):
            return int(self.paper_max_trades_per_day)
        return int(self.max_trades_per_day)

    def can_trade(self, strategy: str = None) -> tuple:
        if self.emergency_stopped:
            return False, "Emergency stop activated"
        if self._should_reset_daily():
            self._reset_daily()
        # Only enforce daily loss limit in live trading — paper/ghost sessions need the data
        if not self.config.get("trading", {}).get("dry_run", True):
            if (
                self.bankroll > 0
                and self.daily_pnl < -self.bankroll * self.daily_loss_limit
            ):
                return False, f"Daily loss limit reached: {self.daily_pnl:.2f}"
        if self.daily_trades >= self.effective_max_trades_per_day():
            return False, "Daily trade limit reached"

        if len(self.active_positions) >= self.max_concurrent_positions:
            return False, "Max concurrent positions reached"
        if strategy:
            strategy_cfg = (
                (self.config.get("strategies", {}) or {}).get(strategy, {}) or {}
            )
            raw_strategy_limit = strategy_cfg.get("max_concurrent_positions")
            if raw_strategy_limit is not None:
                try:
                    strategy_limit = int(raw_strategy_limit)
                except (TypeError, ValueError):
                    strategy_limit = 0
                if strategy_limit > 0:
                    strategy_positions = sum(
                        1
                        for p in self.active_positions.values()
                        if getattr(p, "strategy", "") == strategy
                    )
                    if strategy_positions >= strategy_limit:
                        return (
                            False,
                            f"Max concurrent positions reached for {strategy}",
                        )
        return True, "OK"

    def _get_market_term(self, end_date: Optional[datetime]) -> tuple:
        """Classifies market based on time to resolution."""
        if not end_date:
            return "SHORT_TERM", 0

        days_left = (end_date - datetime.now(end_date.tzinfo)).days

        if days_left >= 14:
            return "LONG_TERM", days_left
        if 7 <= days_left < 14:
            return "MID_TERM", days_left
        return "SHORT_TERM", days_left

    def evaluate_entry(
        self,
        end_date: Optional[datetime],
        current_edge: float,
        bankroll: float,
        strategy: str = None,
        requested_size: float = 0.0,
    ) -> tuple:
        """
        Final check before placing order.
        Returns (bool: can_trade, float: position_size, str: reason)

        PSB's active execution surface is crypto up/down. All positions share
        the same term budget so new crypto assets cannot bypass or get stranded
        in stale legacy buckets.
        """
        term, _ = self._get_market_term(end_date)
        min_edge_map = self.term_risk_config.get("min_edge", {})
        caps_map = self.term_risk_config.get("caps", {})

        # 1. Check if edge is worth the lockup time
        if current_edge < min_edge_map.get(term, 0.05):
            return (
                False,
                0.0,
                f"Edge {current_edge:.2f} too low for {term} (min: {min_edge_map.get(term, 0.05)})",
            )

        # 2. Check if we have budget left for this category
        current_exposure_dict = {t: 0.0 for t in caps_map.keys()}
        for pos in self.active_positions.values():
            pos_term, _ = self._get_market_term(pos.end_date)
            current_exposure_dict[pos_term] += self.position_entry_notional(pos)

        category_spent = current_exposure_dict.get(term, 0.0)
        available_budget = (bankroll * caps_map.get(term, 0.0)) - category_spent

        if available_budget <= 0:
            logger.warning(f"RISK ALERT: {term} budget full. Saving liquidity.")
            return False, 0.0, f"{term} budget full"

        # 3. Return Kelly-computed size, capped only by remaining budget.
        final_size = min(requested_size, available_budget) if requested_size > 0 else available_budget
        if final_size <= 0:
            return False, 0.0, "Entry size resolved to zero"

        return True, round(final_size, 2), "OK"

    def check_strategy_risk(
        self, strategy_name: str, trade_size: float, bankroll: float
    ) -> tuple:
        strategy_config = self.config.get("strategies", {}).get(strategy_name, {})
        max_exposure_pct = strategy_config.get("max_strategy_exposure_pct", 0.05)
        max_trade_size_pct = strategy_config.get("max_trade_size_pct", 0.01)

        # Check max trade size
        if trade_size > (bankroll * max_trade_size_pct):
            return False, f"Trade size exceeds max for {strategy_name}"

        # Check max strategy exposure (dollar cost)
        current_exposure = sum(
            self.position_entry_notional(p)
            for p in self.active_positions.values()
            if getattr(p, "strategy", "") == strategy_name
        )
        if (current_exposure + trade_size) > (bankroll * max_exposure_pct):
            return False, f"Strategy exposure limit reached for {strategy_name}"

        return True, "OK"

    def check_position_risk(
        self, market_id: str, topic: str, current_positions: Dict[str, float]
    ) -> tuple:
        if market_id in self.active_positions:
            return False, "Already have position in this market"
        topic_exposure = current_positions.get(topic, 0.0)
        max_topic_exposure = self.config.get("max_topic_exposure", 0.20)
        if topic_exposure >= max_topic_exposure:
            return False, f"Topic exposure limit reached for {topic}"
        return True, "OK"

    def add_position(self, position: Position):
        self.active_positions[position.position_id] = position
        self.daily_trades += 1
        logger.info(f"Added position: {position.position_id}")

    def remove_position(self, position_id: str):
        if position_id in self.active_positions:
            del self.active_positions[position_id]

    def update_pnl(self, pnl: float):
        self.daily_pnl += pnl
        if (
            self.bankroll > 0
            and self.daily_pnl < -self.bankroll * self.emergency_stop_loss
        ):
            self.trigger_emergency_stop()

    def trigger_emergency_stop(self):
        self.emergency_stopped = True
        logger.critical("EMERGENCY STOP TRIGGERED")

    def reset_emergency_stop(self):
        self.emergency_stopped = False

    def _should_reset_daily(self) -> bool:
        return (datetime.now() - self.last_reset).days >= 1

    def _reset_daily(self):
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.last_reset = datetime.now()

    def get_portfolio_summary(self, total_bankroll: float) -> Dict[str, Any]:
        # Total cost (dollars) — entry-leg aware for BUY_NO vs SELL_YES
        total_cost = sum(
            self.position_entry_notional(p) for p in self.active_positions.values()
        )
        total_exposure = total_cost
        return {
            "total_positions": len(self.active_positions),
            "total_exposure": total_exposure,
            "total_cost": round(total_exposure, 2),
            "exposure_pct": total_exposure / total_bankroll
            if total_bankroll > 0
            else 0,
            "daily_pnl": self.daily_pnl,
            "daily_trades": self.daily_trades,
            "emergency_stopped": self.emergency_stopped,
        }
