"""
Backtest engine: iterates historical data, runs strategies, simulates fills and PnL.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable

import pandas as pd

from src.market.scanner import Market
from src.backtest.backtest_ai import BacktestAIAgent
from src.analysis.math_utils import PositionSizer
from src.strategies.weather_models import WeatherSignal

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """Record of a simulated trade."""

    timestamp: pd.Timestamp
    market_id: str
    action: str
    price: float
    size: float
    fill_price: float  # After slippage
    strategy: str
    edge: float
    slippage_cost: float = 0.0  # $ lost to slippage
    fee_cost: float = 0.0  # $ lost to fees


@dataclass
class BacktestResult:
    """Aggregate backtest results."""

    strategy: str
    start_date: str
    end_date: str
    initial_bankroll: float
    final_bankroll: float
    trades: List[BacktestTrade] = field(default_factory=list)
    gross_pnl: float = 0.0
    execution_cost_total: float = 0.0
    blocked_trade_count: int = 0
    blocked_by_reason: Dict[str, int] = field(default_factory=dict)
    data_row_count: int = 0
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def net_pnl(self) -> float:
        return self.final_bankroll - self.initial_bankroll

    @property
    def total_return_pct(self) -> float:
        if self.initial_bankroll <= 0:
            return 0.0
        return (self.net_pnl / self.initial_bankroll) * 100

    @property
    def num_trades(self) -> int:
        return len(self.trades)


def _weighted_fill(levels: List, target_size: float) -> tuple:
    """Simulate fill by walking order book. levels: [[price, size], ...]"""
    remaining = float(target_size)
    filled = notional = 0.0
    for item in levels:
        price = (
            float(item[0])
            if isinstance(item, (list, tuple))
            else float(item.get("price", 0))
        )
        size = (
            float(item[1])
            if isinstance(item, (list, tuple))
            else float(item.get("size", 0))
        )
        take = min(remaining, size)
        notional += take * price
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if filled == 0:
        return None, 0.0, float(target_size)
    return notional / filled, filled, max(0.0, float(target_size) - filled)


class BacktestEngine:
    """
    Runs strategies on historical data with fill simulation.
    Uses midpoint fill by default; optional L2 book simulation if books provided.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        strategy_name: str = "weather",
        initial_bankroll: float = 10000.0,
        *,
        slippage_mult: float = 1.0,
        fee_bps_override: Optional[int] = None,
    ):
        self.config = config
        self.strategy_name = strategy_name
        self.initial_bankroll = initial_bankroll
        self.bankroll = initial_bankroll
        self.trades: List[BacktestTrade] = []
        self.blocked_count = 0
        self.blocked_by_reason: Dict[str, int] = {}

        # Use backtest AI (no real LLM calls)
        self.backtest_ai = BacktestAIAgent(config)
        self.position_sizer = PositionSizer(
            kelly_fraction=config.get("trading", {}).get("kelly_fraction", 0.25),
            max_position_pct=config.get("trading", {}).get(
                "max_exposure_per_trade", 0.05
            ),
            min_position=config.get("trading", {}).get("default_position_size", 50),
            max_position=config.get("trading", {}).get("max_position_size", 200),
        )
        if strategy_name == "weather":
            from src.strategies.weather import WeatherStrategy

            self.strategy = WeatherStrategy(config, self.position_sizer)
        else:
            raise ValueError(f"Unknown strategy: {strategy_name}")

        self.max_spread = config.get("polymarket", {}).get("max_spread", 0.05)

        # Slippage and fees for execution realism (stress tests via overrides)
        bt_cfg = config.get("backtest", {})
        slip_cfg = bt_cfg.get("slippage", {})
        base_bps = slip_cfg.get("default_bps", 25)
        self._slippage_mult = slippage_mult
        self.slippage_bps = int(base_bps * slippage_mult)
        self.use_spread_slippage = slip_cfg.get("use_spread_when_available", True)
        self.fee_bps = (
            fee_bps_override
            if fee_bps_override is not None
            else bt_cfg.get("fee_bps", 0)
        )

    def _row_to_market(
        self,
        row: pd.Series,
        market_id: str,
        slug: str,
        end_date: Optional[datetime] = None,
    ) -> Market:
        """Convert a data row to a Market object. Uses real end_date if provided, else +90 days."""
        from datetime import timedelta

        price = float(row.get("price", row.get("p", 0.5)))
        spread = float(row.get("spread", 0.02))
        if end_date is None:
            end_date = datetime.now() + timedelta(days=90)
        return Market(
            id=market_id,
            question=f"Backtest market {slug}",
            description="",
            volume=0,
            liquidity=0,
            yes_price=price,
            no_price=1.0 - price,
            spread=spread,
            end_date=end_date,
            token_id_yes=f"{slug}_yes",
            token_id_no=f"{slug}_no",
            group_item_title="",
            slug=slug,
        )

    def _simulate_fill(
        self,
        price: float,
        size: float,
        side: str,
        spread: float = 0.0,
        books_df: Optional[pd.DataFrame] = None,
        ts: Optional[pd.Timestamp] = None,
    ) -> tuple:
        """
        Return (fill_price, slippage_cost, fee_cost).
        Applies slippage: BUY pays more, SELL receives less.
        """
        # L2 book simulation (most accurate when available)
        if books_df is not None and not books_df.empty and ts is not None:
            try:
                idx = books_df.index.get_indexer([ts], method="nearest")[0]
                snap = books_df.iloc[idx]
                levels = snap["asks"] if side == "buy" else snap["bids"]
                avg_fill, filled, _ = _weighted_fill(levels, size)
                if avg_fill:
                    fill_price = avg_fill
                    slippage_pct = abs(fill_price - price) / price if price > 0 else 0
                    slippage_cost = abs(fill_price - price) * size
                    fee_cost = (self.fee_bps / 10_000) * fill_price * size
                    return (fill_price, slippage_cost, fee_cost)
            except Exception:
                pass

        # No L2: apply configurable slippage (stress: _slippage_mult scales both paths)
        if self.use_spread_slippage and spread > 0:
            slippage_pct = (spread / 2) * getattr(
                self, "_slippage_mult", 1.0
            )  # Half-spread to cross
        else:
            slippage_pct = self.slippage_bps / 10_000

        # Size-dependent scaling: larger sizes face more slippage (sqrt model)
        # $50 = 1.0x, $200 = ~1.4x, $500 = ~1.6x base slippage
        if size > 50:
            import math

            size_scale = math.sqrt(size / 50.0)
            slippage_pct *= min(size_scale, 2.0)  # cap at 2x

        slip_usd = max(0.005, slippage_pct * price)

        if side == "buy":
            fill_price = min(0.99, price + slip_usd)
            slippage_cost = (fill_price - price) * size
        else:
            fill_price = max(0.01, price - slip_usd)
            slippage_cost = (price - fill_price) * size

        fee_cost = (self.fee_bps / 10_000) * fill_price * size
        return (fill_price, slippage_cost, fee_cost)

    def _settle_positions(self, positions: List[tuple], yes_won: bool) -> tuple:
        """
        Settle positions at market resolution. Returns (payout, fee_cost).
        BUY_YES: +size if YES won. SELL_YES: -size if YES won. BUY_NO: +size if NO won.
        positions: (action, size, fill_price) or (action, size, fill_price, entry_ts)
        Fees are applied on settlement (exit fee).
        """
        payout = 0.0
        fee_cost = 0.0
        for pos in positions:
            action, size, fill_price = pos[0], pos[1], pos[2]
            if action == "BUY_YES":
                payout += size if yes_won else 0.0
            elif action == "SELL_YES":
                payout -= size if yes_won else 0.0
            elif action == "BUY_NO":
                payout += size if not yes_won else 0.0
            # Apply exit fee on the settlement payout
            fee_cost += (
                (self.fee_bps / 10_000) * abs(payout) if abs(payout) > 0 else 0.0
            )
        return payout, fee_cost

    def _block_trade(self, reason: str):
        """Track a blocked trade with its reason."""
        self.blocked_count += 1
        self.blocked_by_reason[reason] = self.blocked_by_reason.get(reason, 0) + 1

    async def run(
        self,
        data: pd.DataFrame,
        slug: str,
        books_df: Optional[pd.DataFrame] = None,
        max_spread: Optional[float] = None,
        on_progress: Optional[Callable] = None,
        resolution_outcome: Optional[bool] = None,
        max_trades_per_market: Optional[int] = None,
        end_date: Optional[datetime] = None,
    ) -> BacktestResult:
        """
        Run backtest over historical data.
        data: DataFrame with index=t, columns=price, spread
        resolution_outcome: True=YES won, False=NO won, None=infer from final price
        on_progress: Optional callback(current, total, trades, bankroll, last_trade)
        """
        import asyncio

        spread_threshold = max_spread or self.max_spread
        market_id = slug
        total = len(data)
        last_trade = None
        execution_cost_total = 0.0
        equity_curve: List[Dict[str, Any]] = []
        # positions: (action, size, fill_price, entry_ts)
        positions: List[tuple] = []
        bt_cfg = self.config.get("backtest", {})
        exit_strategy = bt_cfg.get("exit_strategy", "hold_to_settlement")
        max_hold_hours = bt_cfg.get("max_hold_hours", 72)
        take_profit_pct = bt_cfg.get("take_profit_pct", 0.20)
        stop_loss_pct = bt_cfg.get("stop_loss_pct", 0.15)

        for current, (ts, row) in enumerate(data.iterrows()):
            if self.bankroll <= 0:
                self._block_trade("bankroll_ruined")
                break
            if (
                max_trades_per_market is not None
                and len(self.trades) >= max_trades_per_market
            ):
                self._block_trade("max_trades_reached")
                break
            if row.get("spread", 0) > spread_threshold:
                self._block_trade("spread_too_wide")
                continue

            # Capture equity snapshot every ~5% of bars or at trade events
            snapshot_interval = max(1, total // 20)
            if current % snapshot_interval == 0:
                equity_curve.append(
                    {
                        "bar": current,
                        "timestamp": str(ts),
                        "bankroll": round(self.bankroll, 2),
                        "trades": len(self.trades),
                        "positions": len(positions),
                    }
                )

            # Set weather proxy forecast from resolution outcome (once, before first trade)
            if self.strategy_name == "weather" and resolution_outcome is not None:
                if not hasattr(self, "_weather_proxy_set"):
                    self.strategy._backtest_proxy_forecast = (
                        0.99 if resolution_outcome else 0.01
                    )
                    self._weather_proxy_set = True

            current_price = float(row.get("price", row.get("p", 0.5)))
            current_no = 1.0 - current_price
            spread = float(row.get("spread", 0.02))

            # Early exit check (when exit_strategy != hold_to_settlement)
            if exit_strategy == "time_and_target" and positions:
                to_remove = []
                for i, pos in enumerate(positions):
                    action, size, fill_price = pos[0], pos[1], pos[2]
                    entry_ts = pos[3] if len(pos) >= 4 else data.index[0]
                    hours_held = (ts - entry_ts).total_seconds() / 3600.0
                    if action == "BUY_YES":
                        unrealized_pnl = size * (current_price - fill_price)
                        cost_basis = fill_price * size
                    elif action == "SELL_YES":
                        unrealized_pnl = size * (fill_price - current_price)
                        cost_basis = fill_price * size
                    else:
                        unrealized_pnl = size * (current_no - fill_price)
                        cost_basis = fill_price * size
                    pnl_pct = unrealized_pnl / cost_basis if cost_basis > 0 else 0.0
                    exit_reason = None
                    if hours_held >= max_hold_hours:
                        exit_reason = "time"
                    elif pnl_pct >= take_profit_pct:
                        exit_reason = "profit"
                    elif pnl_pct <= -stop_loss_pct:
                        exit_reason = "stop"
                    if exit_reason:
                        exit_side = "sell" if "BUY" in action else "buy"
                        exit_mid = current_price if "YES" in action else current_no
                        exit_price, slip_cost, fee_cost = self._simulate_fill(
                            exit_mid,
                            size,
                            exit_side,
                            spread=spread,
                            books_df=books_df,
                            ts=ts,
                        )
                        # Only account for the EXIT leg — entry cost is already in bankroll.
                        # BUY positions: we sell back -> receive exit_price * size
                        # SELL positions: we buy back -> pay exit_price * size
                        if "BUY" in action:
                            self.bankroll += exit_price * size - slip_cost - fee_cost
                        else:
                            self.bankroll -= exit_price * size + slip_cost + fee_cost
                        execution_cost_total += slip_cost + fee_cost
                        to_remove.append(i)
                        last_trade = BacktestTrade(
                            timestamp=ts,
                            market_id=market_id,
                            action=f"EXIT_{action}",
                            price=exit_mid,
                            size=size,
                            fill_price=exit_price,
                            strategy=self.strategy_name,
                            edge=0,
                            slippage_cost=slip_cost,
                            fee_cost=fee_cost,
                        )
                        self.trades.append(last_trade)
                for i in reversed(to_remove):
                    positions.pop(i)

            if hasattr(self.strategy, "reset_processed"):
                self.strategy.reset_processed()
            market = self._row_to_market(row, market_id, slug, end_date=end_date)
            signals = await self.strategy.scan_and_analyze([market], self.bankroll)

            for sig in signals:
                if isinstance(sig, WeatherSignal):
                    action = sig.action
                    price = sig.price
                    size = sig.size
                    edge = sig.gap
                else:
                    action = sig.action
                    price = sig.price
                    size = sig.size
                    edge = sig.edge

                side = "buy" if "BUY" in action else "sell"
                spread = float(row.get("spread", 0.02))
                fill_price, slippage_cost, fee_cost = self._simulate_fill(
                    price, size, side, spread=spread, books_df=books_df, ts=ts
                )

                # Update bankroll: position cost + execution costs
                cost = fill_price * size if "BUY" in action else -fill_price * size
                self.bankroll -= cost
                self.bankroll -= slippage_cost + fee_cost
                execution_cost_total += slippage_cost + fee_cost

                positions.append((action, size, fill_price, ts))
                last_trade = BacktestTrade(
                    timestamp=ts,
                    market_id=market_id,
                    action=action,
                    price=price,
                    size=size,
                    fill_price=fill_price,
                    strategy=self.strategy_name,
                    edge=edge,
                    slippage_cost=slippage_cost,
                    fee_cost=fee_cost,
                )
                self.trades.append(last_trade)

            if on_progress:
                on_progress(
                    current + 1, total, len(self.trades), self.bankroll, last_trade
                )

        # Settlement at resolution
        if positions:
            if resolution_outcome is None and not data.empty:
                final_price = float(data["price"].iloc[-1])
                resolution_outcome = final_price > 0.5
            if resolution_outcome is not None:
                settlement, settle_fee = self._settle_positions(
                    positions, resolution_outcome
                )
                self.bankroll += settlement - settle_fee
                execution_cost_total += settle_fee

        # Cap at 0: you cannot lose more than you have (ruin)
        self.bankroll = max(0.0, self.bankroll)
        gross_pnl = self.bankroll - self.initial_bankroll

        # Final equity snapshot
        equity_curve.append(
            {
                "bar": total,
                "timestamp": str(data.index.max()) if not data.empty else "",
                "bankroll": round(self.bankroll, 2),
                "trades": len(self.trades),
                "positions": 0,
            }
        )

        return BacktestResult(
            strategy=self.strategy_name,
            start_date=str(data.index.min().date()) if not data.empty else "",
            end_date=str(data.index.max().date()) if not data.empty else "",
            initial_bankroll=self.initial_bankroll,
            final_bankroll=self.bankroll,
            trades=self.trades,
            gross_pnl=gross_pnl,
            execution_cost_total=execution_cost_total,
            blocked_trade_count=self.blocked_count,
            blocked_by_reason=dict(self.blocked_by_reason),
            data_row_count=len(data),
            equity_curve=equity_curve,
        )
