from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.model.orders.limit import LimitOrder
from nautilus_trader.model.position import Position
from nautilus_trader.trading.strategy import Strategy

from src.main import PolyBot
from src.market.scanner import Market


class PolyBotStrategyAdapter(Strategy):
    """
    An adapter to make a PolyBot strategy compatible with the Nautilus backtesting engine.
    """

    def __init__(self, poly_bot: PolyBot, strategy_name: str, instrument_id: InstrumentId):
        super().__init__()
        self.poly_bot = poly_bot
        self.strategy_name = strategy_name
        self.instrument_id = instrument_id

        if self.strategy_name == "fade":
            self.strategy = self.poly_bot.fade_strategy
        elif self.strategy_name == "arbitrage":
            self.strategy = self.poly_bot.arbitrage_strategy
        else:
            raise ValueError(f"Unknown strategy: {self.strategy_name}")

    def on_start(self):
        """Register the instruments and bars to be used."""
        self.subscribe_quote_ticks(instrument_id=self.instrument_id)

    async def on_quote_tick(self, tick: QuoteTick):
        """Called when a new quote tick is received."""
        # Create a Market object from the tick
        market = Market(
            id=tick.instrument_id.value,
            question="", # Not available in tick data
            description="", # Not available in tick data
            token_id_yes=tick.instrument_id.value, # Assuming the instrument ID is the token ID
            token_id_no="", # Not available in tick data
            is_binary=True,
            yes_price=float(tick.bid_price),
            no_price=1.0 - float(tick.ask_price),
            spread=float(tick.ask_price - tick.bid_price),
            liquidity=0.0, # Not available in tick data
        )

        # Run the strategy
        signals = await self.strategy.scan_and_analyze(
            markets=[market],
            bankroll=self.portfolio.total_equity().amount,
        )

        # Execute signals
        for signal in signals:
            if signal.action == "BUY_YES":
                order_side = OrderSide.BUY
            elif signal.action == "SELL_YES":
                order_side = OrderSide.SELL
            else:
                continue

            order = LimitOrder(
                instrument_id=self.instrument_id,
                order_side=order_side,
                quantity=Quantity.from_str(str(signal.size)),
                limit_price=Price.from_str(str(signal.price)),
                time_in_force=TimeInForce.GTC,
            )
            self.exec_client.submit_order(order)

    def on_order_filled(self, event: OrderFilled):
        """Called when an order is filled."""
        self.log.info(f"Order filled: {event.order}")

    def on_stop(self):
        """Called when the strategy is stopped."""
        self.log.info("Strategy stopped")
