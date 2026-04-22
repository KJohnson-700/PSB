"""
Execution Module
Order execution and risk management
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from py_clob_client.client import ClobClient as PyClobClient, ApiCreds
from py_clob_client.clob_types import OrderArgs, OrderType

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

    def set_credentials(
        self,
        private_key: str,
        api_key: str = None,
        api_secret: str = None,
        api_passphrase: str = None,
    ):
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

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        market_id: str = None,
        post_only: bool = False,
        dry_run: bool = True,
    ) -> Optional[Order]:
        if dry_run:
            logger.info(f"[DRY RUN] Would place order: {side} {size} @ {price}")
            order = Order(
                order_id=f"dry_{datetime.now().timestamp()}",
                market_id=market_id or "",
                token_id=token_id,
                side=side,
                outcome="YES" if side == "BUY" else "NO",
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

        order_args = OrderArgs(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=OrderType.POST_ONLY if post_only else OrderType.LIMIT,
        )

        try:
            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(
                None, lambda: self.client.create_order(order_args)
            )
            resp = await loop.run_in_executor(
                None, lambda: self.client.post_order(signed_order)
            )

            order = Order(
                order_id=resp["order_id"],
                market_id=market_id or "",
                token_id=token_id,
                side=side,
                outcome="YES" if side == "BUY" else "NO",
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
            # This is a simplified mapping. The actual API response may be more complex.
            if order_data["status"] == "filled":
                return OrderStatus.FILLED
            elif order_data["status"] == "cancelled":
                return OrderStatus.CANCELLED
            else:
                return OrderStatus.PENDING
        except Exception as e:
            logger.error(f"Error getting order status for {order_id}: {e}")
            return None

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
            logger.warning(f"[can_sell_token] CLOB client not initialized — assuming True")
            return True

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
                )
                for p in positions_data
            ]
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []


class RiskManager:
    """Risk Management Engine"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config  # Pass the full config
        risk_config = self.config.get("risk", {})
        self.term_risk_config = self.config.get("term_risk", {})
        self.max_concurrent_positions = risk_config.get("max_concurrent_positions", 10)
        self.max_trades_per_day = risk_config.get("max_trades_per_day", 50)
        self.daily_loss_limit = risk_config.get("daily_loss_limit", 0.15)
        self.emergency_stop_loss = risk_config.get("emergency_stop_loss", 0.25)
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.bankroll = 0.0
        self.last_reset = datetime.now()
        self.emergency_stopped = False
        self.active_positions: Dict[str, Position] = {}

    def can_trade(self, strategy: str = None) -> tuple:
        if self.emergency_stopped:
            return False, "Emergency stop activated"
        if self._should_reset_daily():
            self._reset_daily()
        if (
            self.bankroll > 0
            and self.daily_pnl < -self.bankroll * self.daily_loss_limit
        ):
            return False, f"Daily loss limit reached: {self.daily_pnl:.2f}"
        if self.daily_trades >= self.max_trades_per_day:
            return False, "Daily trade limit reached"

        # Crypto strategies (bitcoin, sol_lag, eth_lag) have their own reserved slots
        # so NEH/fade/arb can't crowd them out
        CRYPTO_STRATEGIES = {"bitcoin", "sol_lag", "eth_lag", "xrp_dump_hedge"}
        CRYPTO_MAX = 12  # reserved slots for crypto strategies

        if strategy in CRYPTO_STRATEGIES:
            crypto_count = sum(
                1
                for p in self.active_positions.values()
                if getattr(p, "strategy", "") in CRYPTO_STRATEGIES
            )
            if crypto_count >= CRYPTO_MAX:
                return (
                    False,
                    f"Crypto position limit reached ({crypto_count}/{CRYPTO_MAX})",
                )
            return True, "OK"

        # Non-crypto strategies share the global pool (minus crypto reserved)
        non_crypto_count = sum(
            1
            for p in self.active_positions.values()
            if getattr(p, "strategy", "") not in CRYPTO_STRATEGIES
        )
        if non_crypto_count >= self.max_concurrent_positions:
            return False, "Max concurrent positions reached"
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
    ) -> tuple:
        """
        Final check before placing order.
        Returns (bool: can_trade, float: position_size, str: reason)

        Crypto strategies (bitcoin, sol_lag, eth_lag) have their own isolated budget
        so event-market positions can't crowd them out.
        """
        CRYPTO_STRATEGIES = {"bitcoin", "sol_lag", "eth_lag", "xrp_dump_hedge"}
        is_crypto = strategy in CRYPTO_STRATEGIES

        term, _ = self._get_market_term(end_date)
        min_edge_map = self.term_risk_config.get("min_edge", {})
        caps_map = self.term_risk_config.get("caps", {})
        sizing_map = self.term_risk_config.get("sizing", {})

        # 1. Check if edge is worth the lockup time
        if current_edge < min_edge_map.get(term, 0.05):
            return (
                False,
                0.0,
                f"Edge {current_edge:.2f} too low for {term} (min: {min_edge_map.get(term, 0.05)})",
            )

        # 2. Check if we have budget left for this category
        # Use dollar cost (size * entry_price) for budget tracking
        current_exposure_dict = {t: 0.0 for t in caps_map.keys()}
        for pos in self.active_positions.values():
            pos_strategy = getattr(pos, "strategy", "")
            pos_is_crypto = pos_strategy in CRYPTO_STRATEGIES
            if is_crypto != pos_is_crypto:
                continue  # skip positions from the other pool
            pos_term, _ = self._get_market_term(pos.end_date)
            cost = pos.size * getattr(pos, "entry_price", 0)
            current_exposure_dict[pos_term] += cost

        category_spent = current_exposure_dict.get(term, 0.0)

        if is_crypto:
            # Crypto gets 15% of bankroll for SHORT_TERM (they resolve in minutes)
            crypto_cap = 0.15
            available_budget = (bankroll * crypto_cap) - category_spent
        else:
            available_budget = (bankroll * caps_map.get(term, 0.0)) - category_spent

        if available_budget <= 0:
            pool_label = "CRYPTO" if is_crypto else term
            logger.warning(f"RISK ALERT: {pool_label} budget full. Saving liquidity.")
            return False, 0.0, f"{pool_label} budget full"

        # 3. Size the position
        standard_size = bankroll * sizing_map.get(term, 0.05)
        final_size = min(standard_size, available_budget)

        return True, final_size, "OK"

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
            p.size * getattr(p, "entry_price", 0)
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
        # Total cost (dollars) = sum of size * entry_price for each position
        total_cost = sum(
            p.size * getattr(p, "entry_price", 0) for p in self.active_positions.values()
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
