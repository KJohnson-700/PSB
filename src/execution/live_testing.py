"""
Live Position Exit Manager & Performance Tracker.

Handles:
1. Position exit logic: TP/SL/time-based exits for active positions
2. Performance aggregation: win rate, Sharpe, equity curve from journal data
3. Drift detection: compare live performance against backtest predictions

Usage:
    exit_mgr = PositionExitManager(config)
    exits = await exit_mgr.check_exits(active_positions, market_prices, clob_client)

    perf = PerformanceTracker(journal_path="data/journal/trade_journal.jsonl")
    metrics = perf.compute_metrics()
    drift = perf.check_drift(backtest_expectations)
"""

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """Represents a decision to exit a position."""

    position_id: str
    market_id: str
    action: str  # EXIT_BUY or EXIT_SELL
    token_id: str
    size: float
    current_price: float
    exit_price: float
    reason: str  # "take_profit", "stop_loss", "time_limit"
    unrealized_pnl: float
    hours_held: float


@dataclass
class PerformanceMetrics:
    """Aggregated performance metrics from live trades."""

    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    avg_edge_predicted: float = 0.0
    avg_edge_realized: float = 0.0
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    by_strategy: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class DriftReport:
    """Compares live performance against backtest expectations."""

    strategy: str
    bt_win_rate: float = 0.0
    live_win_rate: float = 0.0
    win_rate_drift: float = 0.0
    bt_avg_edge: float = 0.0
    live_avg_edge: float = 0.0
    edge_drift: float = 0.0
    bt_trades_per_day: float = 0.0
    live_trades_per_day: float = 0.0
    trade_freq_drift: float = 0.0
    is_diverging: bool = False
    verdict: str = ""


class PositionExitManager:
    """Checks active positions for exit conditions (TP/SL/time).

    Config under `trading.exit_rules` in settings.yaml:
        take_profit_pct: 0.20   # exit at +20% unrealized
        stop_loss_pct: 0.15     # exit at -15% unrealized
        max_hold_hours: 72      # exit after 72 hours
    """

    # Strategies whose positions resolve in minutes (5m/15m updown markets).
    # Stop losses destroy value on these — they convert potential winners into
    # guaranteed losers because the market temporarily moves against you but
    # resolves correctly.  Keep take-profit; skip stop-loss.
    _CRYPTO_UPDOWN_STRATEGIES = frozenset(
        {
            "bitcoin",
            "sol_macro",
            "eth_macro",
            "hype_macro",
            "xrp_macro",
            "xrp_dump_hedge",
        }
    )

    def __init__(self, config: Dict[str, Any]):
        exit_cfg = config.get("trading", {}).get("exit_rules", {})
        self.take_profit_pct = exit_cfg.get("take_profit_pct", 0.20)
        self.stop_loss_pct = exit_cfg.get("stop_loss_pct", 0.15)
        self.max_hold_hours = exit_cfg.get("max_hold_hours", 72)
        self.enabled = exit_cfg.get("enabled", False)

    def check_exits(
        self,
        active_positions: Dict[str, Any],
        market_prices: Dict[str, float],
        market_token_ids: Optional[Dict[str, Tuple[str, str]]] = None,
    ) -> List[ExitDecision]:
        """Check all active positions for exit conditions.

        Args:
            active_positions: Dict of position_id -> Position objects
            market_prices: Dict of market_id -> current YES price
            market_token_ids: Optional dict of market_id -> (token_id_yes, token_id_no)
        """
        if not self.enabled:
            return []

        exits = []
        now = datetime.now()
        token_map = market_token_ids or {}

        for pos_id, pos in active_positions.items():
            hours_held = (now - pos.opened_at).total_seconds() / 3600.0

            # Get current market price
            current_yes_price = market_prices.get(pos.market_id)
            if current_yes_price is None:
                continue

            # entry_price is ALWAYS stored as the YES token price regardless of side.
            # PnL direction depends on whether we are LONG (BUY_YES) or SHORT (SELL_YES).
            #
            # BUY_YES  → profit when YES price rises  → pnl = size × (yes_now - yes_entry)
            # SELL_YES → profit when YES price falls  → pnl = size × (yes_entry - yes_now)
            #
            # Position.side does not exist on the Position dataclass — using pos.outcome
            # instead:  outcome=="NO" → we are SHORT (SELL_YES), outcome=="YES" → LONG.
            #
            # Also guard against scanner returning the NO token price for sports/championship
            # markets where token ordering is inverted (yes_price ≈ 1 - entry_price).
            # If the price appears to have jumped by >0.50 from entry, the market data is
            # mis-ordered; skip rather than book a massive phantom loss.
            if abs(current_yes_price - pos.entry_price) > 0.50:
                logger.debug(
                    f"Skip exit check {pos.market_id}: price delta implausible "
                    f"({pos.entry_price:.3f} → {current_yes_price:.3f}); "
                    f"likely inverted token ordering in scanner"
                )
                continue

            is_short = (pos.outcome == "NO")
            if is_short:
                unrealized_pnl = pos.size * (pos.entry_price - current_yes_price)
                # cost_basis = max possible loss (price rising to 1.0)
                cost_basis = (1.0 - pos.entry_price) * pos.size
            else:
                unrealized_pnl = pos.size * (current_yes_price - pos.entry_price)
                cost_basis = pos.entry_price * pos.size

            if cost_basis <= 0:
                continue

            pnl_pct = unrealized_pnl / cost_basis

            # Check exit conditions
            reason = None
            is_updown = (
                getattr(pos, "strategy", "") in self._CRYPTO_UPDOWN_STRATEGIES
                and "up or down" in getattr(pos, "market_question", "").lower()
            )
            if pnl_pct >= self.take_profit_pct:
                reason = "take_profit"
            elif pnl_pct <= -self.stop_loss_pct and not is_updown:
                # Skip stop-loss for crypto updown — markets self-resolve in
                # minutes; stop losses only convert possible wins to sure losses.
                reason = "stop_loss"
            elif hours_held >= self.max_hold_hours:
                reason = "time_limit"

            if reason:
                # Get token IDs from market data
                token_yes, token_no = token_map.get(pos.market_id, ("", ""))

                # exit_price must be the YES token price in both cases so that
                # log_exit (which also stores entry_price as YES price) computes
                # PnL correctly as (entry - exit)*size for SELL or (exit - entry)*size for BUY.
                exit_price = current_yes_price  # always YES price — consistent with entry_price

                if pos.outcome == "YES":
                    exit_action = "SELL"
                    exit_token_id = token_yes
                else:
                    # SELL_YES entries short the YES token; close by buying back YES.
                    exit_action = "BUY"
                    exit_token_id = token_yes

                exits.append(
                    ExitDecision(
                        position_id=pos_id,
                        market_id=pos.market_id,
                        action=exit_action,
                        token_id=exit_token_id,
                        size=pos.size,
                        current_price=current_yes_price,
                        exit_price=exit_price,
                        reason=reason,
                        unrealized_pnl=round(unrealized_pnl, 2),
                        hours_held=round(hours_held, 1),
                    )
                )

        if exits:
            logger.info(f"Exit manager: {len(exits)} positions ready to exit")
            for e in exits:
                logger.info(
                    f"  EXIT {e.reason}: {e.position_id[:12]}... "
                    f"PnL=${e.unrealized_pnl:+.2f} ({e.hours_held}h)"
                )

        return exits


class PerformanceTracker:
    """Computes performance metrics from live trade journal data."""

    def __init__(self, journal_path: str = None):
        # Default to the most recent paper_trades session if no path given
        if journal_path:
            self.journal_path = Path(journal_path)
        else:
            from src.execution.trade_journal import JOURNAL_DIR
            self.journal_path = None
            if JOURNAL_DIR.exists():
                sessions = sorted([d for d in JOURNAL_DIR.iterdir() if d.is_dir()], reverse=True)
                if sessions:
                    self.journal_path = sessions[0] / "entries.jsonl"

    def _load_trades(self) -> List[Dict]:
        """Load EXIT entries from journal (actual closed trades with pnl)."""
        trades = []
        if not self.journal_path or not self.journal_path.exists():
            return trades
        try:
            with open(self.journal_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        trades.append(entry)
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.warning(f"Failed to load journal: {e}")
        return trades

    def compute_metrics(self, strategy: Optional[str] = None) -> PerformanceMetrics:
        """Compute performance metrics from journal trades.

        Args:
            strategy: If set, only compute for this strategy. Otherwise all.
        """
        all_trades = self._load_trades()
        # Sanity guard: exclude any EXIT record whose |pnl| exceeds $200.
        # Phantom exits from the pre-fix token-ordering bug produced -$26 to -$466
        # per trade, which would contaminate equity curves and win-rate calculations.
        # A legitimate $5-max-position trade cannot produce a |pnl| anywhere near $200.
        _MAX_PLAUSIBLE_PNL = 200.0
        trades = [
            t
            for t in all_trades
            if t.get("event") == "EXIT"
            and t.get("pnl") is not None
            and abs(t.get("pnl", 0)) <= _MAX_PLAUSIBLE_PNL
        ]
        if strategy:
            trades = [
                t for t in trades if t.get("strategy", "").lower() == strategy.lower()
            ]

        metrics = PerformanceMetrics(total_trades=len(trades))

        if not trades:
            return metrics

        # Win/loss
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]
        metrics.wins = len(wins)
        metrics.losses = len(losses)
        metrics.win_rate = (
            metrics.wins / metrics.total_trades if metrics.total_trades else 0
        )

        # PnL
        total_win_pnl = sum(t.get("pnl", 0) for t in wins)
        total_loss_pnl = abs(sum(t.get("pnl", 0) for t in losses))
        metrics.avg_win = total_win_pnl / len(wins) if wins else 0
        metrics.avg_loss = total_loss_pnl / len(losses) if losses else 0
        metrics.profit_factor = (
            total_win_pnl / total_loss_pnl if total_loss_pnl > 0 else float("inf")
        )
        metrics.total_pnl = sum(t.get("pnl", 0) for t in trades)

        # Equity curve
        cumulative = 0
        equity = []
        for t in sorted(trades, key=lambda x: x.get("timestamp", "")):
            cumulative += t.get("pnl", 0)
            equity.append(
                {
                    "timestamp": t.get("timestamp", ""),
                    "cumulative_pnl": round(cumulative, 2),
                    "strategy": t.get("strategy", ""),
                }
            )
        metrics.equity_curve = equity

        # Max drawdown
        peak = 0
        max_dd = 0
        for point in equity:
            if point["cumulative_pnl"] > peak:
                peak = point["cumulative_pnl"]
            dd = peak - point["cumulative_pnl"]
            if dd > max_dd:
                max_dd = dd
        metrics.max_drawdown = max_dd

        # Sharpe (simplified: mean return / std dev of returns)
        returns = [t.get("pnl", 0) for t in trades]
        if len(returns) > 1:
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
            std_dev = math.sqrt(variance) if variance > 0 else 0
            metrics.sharpe_ratio = mean_ret / std_dev if std_dev > 0 else 0

        # Edge: predicted vs realized
        edges = [t.get("edge", 0) for t in trades if t.get("edge") is not None]
        metrics.avg_edge_predicted = sum(edges) / len(edges) if edges else 0

        # Per-strategy breakdown
        strategies = set(t.get("strategy", "unknown") for t in trades)
        for strat in strategies:
            strat_trades = [t for t in trades if t.get("strategy", "") == strat]
            strat_wins = sum(1 for t in strat_trades if t.get("pnl", 0) > 0)
            strat_pnl = sum(t.get("pnl", 0) for t in strat_trades)
            metrics.by_strategy[strat] = {
                "trades": len(strat_trades),
                "win_rate": strat_wins / len(strat_trades) if strat_trades else 0,
                "total_pnl": round(strat_pnl, 2),
            }

        return metrics

    def check_drift(
        self,
        backtest_expectations: Dict[str, Dict[str, float]],
    ) -> List[DriftReport]:
        """Compare live performance against backtest predictions.

        Args:
            backtest_expectations: Dict of strategy -> {
                "win_rate": float,
                "avg_edge": float,
                "trades_per_day": float
            }

        Returns:
            List of DriftReport per strategy
        """
        reports = []
        all_trades = self._load_trades()
        live_trades = [
            t
            for t in all_trades
            if t.get("event") == "EXIT"
            and t.get("pnl") is not None
            and abs(t.get("pnl", 0)) <= 200.0
        ]

        for strategy, bt_exp in backtest_expectations.items():
            strat_trades = [
                t
                for t in live_trades
                if t.get("strategy", "").lower() == strategy.lower()
            ]
            if not strat_trades:
                continue

            # Live metrics
            wins = sum(1 for t in strat_trades if t.get("pnl", 0) > 0)
            live_win_rate = wins / len(strat_trades) if strat_trades else 0
            live_edges = [
                t.get("edge", 0) for t in strat_trades if t.get("edge") is not None
            ]
            live_avg_edge = sum(live_edges) / len(live_edges) if live_edges else 0

            # Trade frequency
            timestamps = [
                t.get("timestamp", "") for t in strat_trades if t.get("timestamp")
            ]
            if len(timestamps) >= 2:
                first = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
                last = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
                days = max(1, (last - first).total_seconds() / 86400)
                live_trades_per_day = len(strat_trades) / days
            else:
                live_trades_per_day = 0

            bt_win_rate = bt_exp.get("win_rate", 0)
            bt_avg_edge = bt_exp.get("avg_edge", 0)
            bt_trades_per_day = bt_exp.get("trades_per_day", 0)

            report = DriftReport(
                strategy=strategy,
                bt_win_rate=bt_win_rate,
                live_win_rate=live_win_rate,
                win_rate_drift=live_win_rate - bt_win_rate,
                bt_avg_edge=bt_avg_edge,
                live_avg_edge=live_avg_edge,
                edge_drift=live_avg_edge - bt_avg_edge,
                bt_trades_per_day=bt_trades_per_day,
                live_trades_per_day=live_trades_per_day,
                trade_freq_drift=live_trades_per_day - bt_trades_per_day,
            )

            # Flag if win rate dropped >15% or edge dropped >50%
            win_rate_bad = report.win_rate_drift < -0.15
            edge_bad = bt_avg_edge > 0 and report.edge_drift < -bt_avg_edge * 0.5
            report.is_diverging = win_rate_bad or edge_bad

            if report.is_diverging:
                reasons = []
                if win_rate_bad:
                    reasons.append(
                        f"win rate {live_win_rate:.0%} vs BT {bt_win_rate:.0%}"
                    )
                if edge_bad:
                    reasons.append(f"edge {live_avg_edge:.4f} vs BT {bt_avg_edge:.4f}")
                report.verdict = f"DIVERGING: {', '.join(reasons)}"
            else:
                report.verdict = "OK"

            reports.append(report)

        return reports


def print_performance_report(metrics: PerformanceMetrics):
    """Print formatted performance metrics."""
    print("\n" + "=" * 60)
    print("LIVE PERFORMANCE REPORT")
    print("=" * 60)
    print(f"  Total Trades:  {metrics.total_trades}")
    print(
        f"  Win Rate:      {metrics.win_rate:.0%} ({metrics.wins}W / {metrics.losses}L)"
    )
    print(f"  Avg Win:       ${metrics.avg_win:+.2f}")
    print(f"  Avg Loss:      ${metrics.avg_loss:.2f}")
    print(f"  Profit Factor: {metrics.profit_factor:.2f}")
    print(f"  Total PnL:     ${metrics.total_pnl:+.2f}")
    print(f"  Max Drawdown:  ${metrics.max_drawdown:.2f}")
    print(f"  Sharpe Ratio:  {metrics.sharpe_ratio:.2f}")

    if metrics.by_strategy:
        print(f"\n  --- By Strategy ---")
        for strat, data in metrics.by_strategy.items():
            n = int(data["trades"] or 0)
            tw = "trade" if n == 1 else "trades"
            print(
                f"  {strat}: {n} {tw}, win={data['win_rate']:.0%}, PnL=${data['total_pnl']:+.2f}"
            )

    print("=" * 60)


def print_drift_report(reports: List[DriftReport]):
    """Print formatted drift detection report."""
    print("\n" + "=" * 60)
    print("DRIFT DETECTION REPORT")
    print("=" * 60)
    for r in reports:
        status = "WARNING" if r.is_diverging else "OK"
        print(f"\n  [{status}] {r.strategy}")
        print(
            f"    Win Rate:  BT={r.bt_win_rate:.0%}  Live={r.live_win_rate:.0%}  Drift={r.win_rate_drift:+.0%}"
        )
        print(
            f"    Avg Edge:  BT={r.bt_avg_edge:.4f}  Live={r.live_avg_edge:.4f}  Drift={r.edge_drift:+.4f}"
        )
        print(
            f"    Freq:      BT={r.bt_trades_per_day:.1f}/day  Live={r.live_trades_per_day:.1f}/day"
        )
        print(f"    Verdict:   {r.verdict}")
    print("=" * 60)
