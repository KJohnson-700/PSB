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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from src.execution.updown_exit_shared import (
    CRYPTO_UPDOWN_STRATEGIES,
    adverse_for_updown_cents_time_stop,
    cents_stop_for_entry_price,
    effective_updown_stop_loss_pct,
    parse_updown_exit_globals,
    resolve_updown_exit_params,
    resolve_updown_exit_params_for_position,
    scaled_exit_window_mins,
)

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
    # Exit-calibration telemetry (additive; None on legacy/reload paths).
    # mae_pct/mfe_pct: worst/best token-price excursion vs entry over the hold.
    # pnl_pct_at_exit: pnl_pct at the moment the exit fired.
    # effective_stop_loss_pct: the (dynamic) stop threshold in force at exit — lets
    # us measure stop OVERSHOOT (how far past the threshold the fill landed).
    mae_pct: Optional[float] = None
    mfe_pct: Optional[float] = None
    pnl_pct_at_exit: Optional[float] = None
    effective_stop_loss_pct: Optional[float] = None
    # True when exit_price was set to the executable bid (stop_use_executable_price):
    # the close must be placed FAK/marketable so it actually takes that liquidity
    # instead of resting at the bid and re-gapping. Default False (GTC limit).
    marketable: bool = False


@dataclass
class PerformanceMetrics:
    """Aggregated performance metrics from live trades."""

    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: Optional[float] = 0.0
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
    live_sample_size: int = 0


class PositionExitManager:
    """Checks active positions for exit conditions (TP/SL/time).

    Config under `trading.exit_rules` in settings.yaml:
        take_profit_pct: 0.15   # exit at +15% unrealized
        stop_loss_pct: 0.30     # exit at -30% unrealized
        max_hold_hours: 72      # exit after 72 hours

    Crypto up/down exit parameters are parsed via ``parse_updown_exit_globals`` so
    the same keys apply in ``UpdownBacktestEngine`` (see ``updown_exit_shared``).
    """

    def __init__(self, config: Dict[str, Any]):
        exit_cfg = config.get("trading", {}).get("exit_rules", {}) or {}
        self.enabled = bool(exit_cfg.get("enabled", False))
        required = {"take_profit_pct", "stop_loss_pct", "max_hold_hours"}
        missing = required - set(exit_cfg)
        if self.enabled and missing:
            logger.warning(
                "PositionExitManager exit_rules missing %s; using settings.yaml fallbacks",
                sorted(missing),
            )
        self._ude = parse_updown_exit_globals(exit_cfg)
        self.take_profit_pct = self._ude.take_profit_pct
        self.stop_loss_pct = exit_cfg.get("stop_loss_pct", 0.30)
        self.max_hold_hours = exit_cfg.get("max_hold_hours", 72)
        self.updown_stop_cents = self._ude.updown_stop_cents
        self.updown_exit_window_mins = self._ude.updown_exit_window_mins
        self.updown_max_hold_mins = self._ude.updown_max_hold_mins
        self.updown_stop_loss_pct = self._ude.updown_stop_loss_pct
        self.updown_exit_window_max_fraction = self._ude.updown_exit_window_max_fraction
        self.updown_stop_cents_high_entry = self._ude.updown_stop_cents_high_entry
        self.updown_high_entry_threshold = self._ude.updown_high_entry_threshold
        self.updown_in_profit_stop_trigger_pct = self._ude.updown_in_profit_stop_trigger_pct
        self.updown_in_profit_stop_tighten_to_pct = self._ude.updown_in_profit_stop_tighten_to_pct
        # Optional BUY_YES bid-depth deterioration exit (default OFF). See settings.yaml
        # trading.exit_rules.bid_depth_exit and _maybe_bid_depth_exit below.
        self._bid_depth_exit = exit_cfg.get("bid_depth_exit", {}) or {}
        # Optional: evaluate the updown STOP trigger on the executable exit-side price
        # (the bid we'd actually sell into) instead of the YES midpoint (default OFF).
        # The midpoint mark fires the stop at mid -X% but the realized fill gaps to the
        # bid (-25% to -45% near resolution); marking the stop on the executable price
        # fires it at a true -X% and fills near there. See check_exits below.
        self._stop_use_executable_price = bool(
            exit_cfg.get("stop_use_executable_price", False)
        )

    def _resolve_updown_exit_params(self, strategy_name: str) -> Tuple[float, float, float, float]:
        """Return per-strategy updown exit params with global defaults as fallback."""
        return resolve_updown_exit_params(self._ude, strategy_name)

    def check_exits(
        self,
        active_positions: Dict[str, Any],
        market_prices: Dict[str, float],
        market_token_ids: Optional[Dict[str, Tuple[str, str]]] = None,
        market_liquidity: Optional[Dict[str, Any]] = None,
    ) -> List[ExitDecision]:
        """Check all active positions for exit conditions.

        Args:
            active_positions: Dict of position_id -> Position objects
            market_prices: Dict of market_id -> current YES price
            market_token_ids: Optional dict of market_id -> (token_id_yes, token_id_no)
            market_liquidity: Optional dict of market_id -> compact YES-book snapshot
                ({best_bid, best_ask, spread, bids[]}); only consumed by the
                default-off bid-depth exit. None elsewhere (e.g. the scan loop).
        """
        if not self.enabled:
            return []

        exits = []
        now = datetime.now()
        token_map = market_token_ids or {}

        for pos_id, pos in active_positions.items():
            hours_held = (now - pos.opened_at).total_seconds() / 3600.0
            strategy_name = getattr(pos, "strategy", "") or ""

            # Get current market price
            current_yes_price = market_prices.get(pos.market_id)
            if current_yes_price is None:
                continue

            # entry_price is the traded token price at entry (YES for BUY_YES; NO for BUY_NO;
            # YES for short YES / SELL_YES). Scanner passes YES mids. entry_leg disambiguates
            # long NO (BUY_NO) from short YES (same outcome flag on some paths).

            entry_leg = getattr(pos, "entry_leg", "YES") or "YES"
            if entry_leg not in ("YES", "NO"):
                entry_leg = "YES"
            current_no_price = 1.0 - current_yes_price

            if entry_leg == "NO":
                if abs(current_no_price - pos.entry_price) > 0.50:
                    logger.debug(
                        f"Skip exit check {pos.market_id}: NO price delta implausible "
                        f"({pos.entry_price:.3f} → {current_no_price:.3f})"
                    )
                    continue
                unrealized_pnl = pos.size * (current_no_price - pos.entry_price)
                cost_basis = pos.entry_price * pos.size
            elif pos.outcome == "NO":
                # Short YES: lent/sold YES; mark in YES space.
                if abs(current_yes_price - pos.entry_price) > 0.50:
                    logger.debug(
                        f"Skip exit check {pos.market_id}: price delta implausible "
                        f"({pos.entry_price:.3f} → {current_yes_price:.3f}); "
                        f"likely inverted token ordering in scanner"
                    )
                    continue
                unrealized_pnl = pos.size * (pos.entry_price - current_yes_price)
                cost_basis = (1.0 - pos.entry_price) * pos.size
            else:
                if abs(current_yes_price - pos.entry_price) > 0.50:
                    logger.debug(
                        f"Skip exit check {pos.market_id}: price delta implausible "
                        f"({pos.entry_price:.3f} → {current_yes_price:.3f}); "
                        f"likely inverted token ordering in scanner"
                    )
                    continue
                unrealized_pnl = pos.size * (current_yes_price - pos.entry_price)
                cost_basis = pos.entry_price * pos.size

            if cost_basis <= 0:
                continue

            pnl_pct = unrealized_pnl / cost_basis
            current_token_price = (
                current_no_price if entry_leg == "NO" else current_yes_price
            )
            peak_token_price = float(
                getattr(pos, "peak_token_price", 0.0) or pos.entry_price
            )
            if current_token_price > peak_token_price:
                peak_token_price = current_token_price
                setattr(pos, "peak_token_price", peak_token_price)
            peak_pnl_pct = (
                (peak_token_price - pos.entry_price) / pos.entry_price
                if pos.entry_price > 0
                else 0.0
            )
            # Mirror the peak tracker to capture max adverse excursion (MAE): the
            # lowest token price the position touched. Persisted on the position so
            # it accumulates across cycles, same as peak_token_price.
            trough_token_price = float(
                getattr(pos, "trough_token_price", 0.0) or pos.entry_price
            )
            if current_token_price < trough_token_price:
                trough_token_price = current_token_price
                setattr(pos, "trough_token_price", trough_token_price)
            mae_pct = (
                (trough_token_price - pos.entry_price) / pos.entry_price
                if pos.entry_price > 0
                else 0.0
            )
            entry_signal = dict(getattr(pos, "entry_signal", {}) or {})

            # Executable exit-side mark for the STOP trigger (default off). `pnl_pct`
            # above marks at the YES midpoint; a stop sells into the bid, so near
            # resolution the realized fill gaps well past the trigger. When enabled and
            # a book snapshot exists, evaluate the stop on the price we'd actually
            # realize and fill there. Fail-safe to midpoint if the book is missing.
            stop_pnl_pct = pnl_pct
            exec_exit_price: Optional[float] = None
            if self._stop_use_executable_price:
                _liq = (market_liquidity or {}).get(pos.market_id) or {}
                _best_bid = _liq.get("best_bid")
                _best_ask = _liq.get("best_ask")
                if _best_bid is not None and _best_ask is not None:
                    if entry_leg == "NO":
                        # Long NO: sell NO -> NO bid = 1 - YES ask
                        _exec_no = 1.0 - float(_best_ask)
                        stop_pnl_pct = (pos.size * (_exec_no - pos.entry_price)) / cost_basis
                        exec_exit_price = _exec_no
                    elif pos.outcome == "NO":
                        # Short YES: buy back YES -> pay YES ask
                        stop_pnl_pct = (
                            pos.size * (pos.entry_price - float(_best_ask))
                        ) / cost_basis
                        exec_exit_price = float(_best_ask)
                    else:
                        # Long YES: sell YES -> YES bid
                        stop_pnl_pct = (
                            pos.size * (float(_best_bid) - pos.entry_price)
                        ) / cost_basis
                        exec_exit_price = float(_best_bid)

            # Check exit conditions
            reason = None
            # Stop threshold in force at exit (for overshoot telemetry); set in the
            # branch that actually evaluates the stop below.
            effective_stop_for_log: Optional[float] = None
            is_updown = (
                strategy_name in CRYPTO_UPDOWN_STRATEGIES
                and "up or down" in getattr(pos, "market_question", "").lower()
            )

            if is_updown:
                resolved = resolve_updown_exit_params_for_position(
                    self._ude,
                    strategy_name=strategy_name,
                    window_size=getattr(pos, "window_size", ""),
                    entry_leg=entry_leg,
                    outcome=pos.outcome,
                    opened_at=pos.opened_at,
                    end_date=pos.end_date,
                    btc_1h_regime=entry_signal.get("btc_1h_regime"),
                )

                up_stop_cents = cents_stop_for_entry_price(
                    resolved.updown_stop_cents,
                    pos.entry_price,
                    high_threshold=resolved.updown_high_entry_threshold,
                    high_stop_cents=resolved.updown_stop_cents_high_entry,
                )

                effective_stop_loss_pct = effective_updown_stop_loss_pct(
                    resolved.updown_stop_loss_pct,
                    pnl_pct,
                    peak_pnl_pct=peak_pnl_pct,
                    in_profit_trigger_pct=resolved.updown_in_profit_stop_trigger_pct,
                    tighten_to_pct=resolved.updown_in_profit_stop_tighten_to_pct,
                    trail_arm_pct=resolved.updown_trail_arm_pct,
                    trail_gap_pct=resolved.updown_trail_gap_pct,
                    dynamic_stop_enabled=resolved.dynamic_stop_enabled,
                    btc_1h_regime=entry_signal.get("btc_1h_regime"),
                    entry_volatility=entry_signal.get("entry_volatility"),
                    convergence_score=(
                        entry_signal.get("convergence_score")
                        if entry_signal.get("convergence_score") is not None
                        else getattr(pos, "confidence", None)
                    ),
                    dynamic_stop_bull_mult=resolved.dynamic_stop_bull_mult,
                    dynamic_stop_range_mult=resolved.dynamic_stop_range_mult,
                    dynamic_stop_bear_mult=resolved.dynamic_stop_bear_mult,
                    dynamic_stop_high_vol_mult=resolved.dynamic_stop_high_vol_mult,
                    dynamic_stop_volatility_threshold=resolved.dynamic_stop_volatility_threshold,
                    dynamic_stop_low_convergence_mult=resolved.dynamic_stop_low_convergence_mult,
                    dynamic_stop_high_convergence_mult=resolved.dynamic_stop_high_convergence_mult,
                    dynamic_stop_low_convergence_threshold=resolved.dynamic_stop_low_convergence_threshold,
                    dynamic_stop_high_convergence_threshold=resolved.dynamic_stop_high_convergence_threshold,
                )
                effective_stop_for_log = effective_stop_loss_pct

                # TP: exit early when price spikes strongly in our favour rather than
                # waiting for binary resolution (captures most of the gain).
                if (
                    not resolved.updown_hold_winners_to_resolution
                    and pnl_pct >= resolved.take_profit_pct
                ):
                    reason = "take_profit"
                elif effective_stop_loss_pct != 0 and stop_pnl_pct <= -effective_stop_loss_pct:
                    # Same-position percentage stop: cuts adverse drift early instead of
                    # waiting for the late-window cents stop, which fires at whatever price
                    # the position has already collapsed to. stop_pnl_pct == pnl_pct unless
                    # stop_use_executable_price is on (then it marks the exit-side bid).
                    reason = "updown_stop_loss"
                else:
                    # Time-based stop: when near expiry and price has moved against us,
                    # exit at the current partial-loss price rather than holding to a
                    # binary zero resolution.
                    mins_remaining = None
                    if pos.end_date is not None:
                        _end = pos.end_date
                        if _end.tzinfo is None:
                            _end = _end.replace(tzinfo=timezone.utc)
                        mins_remaining = (
                            _end - datetime.now(timezone.utc)
                        ).total_seconds() / 60.0

                    effective_exit_window = resolved.updown_exit_window_mins
                    if pos.end_date is not None:
                        _end_e = pos.end_date
                        if _end_e.tzinfo is None:
                            _end_e = _end_e.replace(tzinfo=timezone.utc)
                        opened = pos.opened_at
                        if opened.tzinfo is None:
                            opened = opened.replace(tzinfo=timezone.utc)
                        mins_at_entry = (_end_e - opened).total_seconds() / 60.0
                        effective_exit_window = scaled_exit_window_mins(
                            resolved.updown_exit_window_mins,
                            resolved.updown_exit_window_max_fraction,
                            mins_at_entry,
                        )

                    if mins_remaining is not None and mins_remaining < 0:
                        # Market already past expiry but still open — exit immediately.
                        reason = "updown_expired"
                    elif mins_remaining is not None and mins_remaining <= effective_exit_window:
                        adverse = adverse_for_updown_cents_time_stop(
                            entry_leg=entry_leg,
                            outcome=pos.outcome,
                            current_yes=current_yes_price,
                            current_no=current_no_price,
                            entry_price=pos.entry_price,
                            up_stop_cents=up_stop_cents,
                        )
                        if adverse:
                            reason = "updown_time_stop"
                    elif mins_remaining is None and hours_held >= resolved.updown_max_hold_mins / 60.0:
                        # Safety valve for journal-reloaded positions that have no
                        # end_date: if still open after updown_max_hold_mins, exit.
                        reason = "updown_time_limit"
            else:
                effective_stop_for_log = self.stop_loss_pct
                if pnl_pct >= self.take_profit_pct:
                    reason = "take_profit"
                elif pnl_pct <= -self.stop_loss_pct:
                    reason = "stop_loss"
                elif hours_held >= self.max_hold_hours:
                    reason = "time_limit"

            # Optional BUY_YES bid-depth deterioration exit — only as a fallback,
            # never overriding TP/SL/time-stop reasons already set above.
            if reason is None:
                reason = self._maybe_bid_depth_exit(
                    pos=pos,
                    entry_leg=entry_leg,
                    pnl_pct=pnl_pct,
                    market_liquidity=market_liquidity,
                )

            if reason:
                token_yes, token_no = token_map.get(pos.market_id, ("", ""))

                if entry_leg == "NO":
                    exit_price = current_no_price
                    exit_action = "SELL"
                    exit_token_id = token_no
                elif pos.outcome == "YES":
                    exit_price = current_yes_price
                    exit_action = "SELL"
                    exit_token_id = token_yes
                else:
                    exit_price = current_yes_price
                    exit_action = "BUY"
                    exit_token_id = token_yes

                # When the executable-price stop fired, realize at that exit-side price
                # (same token space as exit_price above) so the fill matches the trigger,
                # and mark the close marketable so it is placed FAK (takes the bid now)
                # rather than resting as a limit that may not fill.
                exit_marketable = False
                if reason == "updown_stop_loss" and exec_exit_price is not None:
                    exit_price = exec_exit_price
                    exit_marketable = True

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
                        mae_pct=round(mae_pct, 4),
                        mfe_pct=round(peak_pnl_pct, 4),
                        pnl_pct_at_exit=round(pnl_pct, 4),
                        effective_stop_loss_pct=(
                            round(effective_stop_for_log, 4)
                            if effective_stop_for_log is not None
                            else None
                        ),
                        marketable=exit_marketable,
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

    def _maybe_bid_depth_exit(
        self,
        *,
        pos: Any,
        entry_leg: str,
        pnl_pct: float,
        market_liquidity: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Close a long-YES position when YES bid depth (sell-side support) collapses.

        Default OFF. ``observe`` mode logs would-fire events without acting, so the
        first runs build a record of how often this would trigger vs. real outcomes
        — the ghost log cannot score exit changes, so this is the only honest way to
        validate it. Only long YES (entry_leg=YES, outcome=YES) is in scope: that's
        the leg whose YES book we snapshot. Fail-OPEN: a missing snapshot never
        forces an exit.
        """
        cfg = self._bid_depth_exit
        if not cfg.get("enabled", False):
            return None
        if not market_liquidity:
            return None
        if entry_leg != "YES" or getattr(pos, "outcome", "") != "YES":
            return None
        if cfg.get("only_when_losing", True) and pnl_pct > 0:
            return None

        liq = market_liquidity.get(getattr(pos, "market_id", "")) or {}
        bids = liq.get("bids") or []
        if not bids:
            return None

        # Time gate: only fire when MORE than min_mins_remaining is left, so the
        # late-window time stop owns end-of-life exits and we don't double-fire.
        min_mins = float(cfg.get("min_mins_remaining", 5.0))
        end_date = getattr(pos, "end_date", None)
        if end_date is not None:
            _end = end_date if end_date.tzinfo else end_date.replace(tzinfo=timezone.utc)
            mins_remaining = (_end - datetime.now(timezone.utc)).total_seconds() / 60.0
            if mins_remaining <= min_mins:
                return None

        levels = int(cfg.get("depth_levels", 3))
        depth_usd = sum(
            float(b.get("price", 0)) * float(b.get("size", 0)) for b in bids[:levels]
        )
        floor = float(cfg.get("min_bid_depth_usd", 150.0))
        if depth_usd >= floor:
            return None

        mode = str(cfg.get("mode", "observe")).strip().lower()
        msg = (
            f"bid-depth-exit {mode}: market={str(getattr(pos, 'market_id', ''))[:18]} "
            f"depth_usd={depth_usd:.0f} floor={floor:.0f} "
            f"pnl_pct={pnl_pct:+.3f} leg=YES"
        )
        if mode == "enforce":
            logger.warning("%s — EXIT", msg)
            return "buy_yes_bid_depth_drop"
        logger.info("%s — observe (holding)", msg)
        return None


class PerformanceTracker:
    """Computes performance metrics from live trade journal data."""

    def __init__(self, journal_path: str = None):
        # Default to the most recent paper_trades session if no path given
        if journal_path:
            self.journal_path = Path(journal_path)
        else:
            from src.execution.trade_journal import TradeJournal
            self.journal_path = None
            chosen = TradeJournal.newest_resumable_session_dir()
            if chosen:
                self.journal_path = chosen / "entries.jsonl"

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
            round(total_win_pnl / total_loss_pnl, 2) if total_loss_pnl > 0 else None
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
        min_live_sample: Optional[int] = None,
    ) -> List[DriftReport]:
        """Compare live performance against backtest predictions.

        Args:
            backtest_expectations: Dict of strategy -> {
                "win_rate": float,
                "avg_edge": float,
                "trades_per_day": float
            }
            min_live_sample: Minimum EXIT count before divergence checks apply (default 15).

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

        from src.execution.backtest_expectations import live_trades_for_expectation

        for strategy, bt_exp in backtest_expectations.items():
            strat_trades = live_trades_for_expectation(live_trades, strategy)
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
                parsed_ts = sorted(
                    datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    for ts in timestamps
                )
                elapsed_secs = (parsed_ts[-1] - parsed_ts[0]).total_seconds()
                live_trades_per_day = (
                    len(strat_trades) * 86400 / elapsed_secs
                    if elapsed_secs > 0
                    else 0
                )
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
                live_sample_size=len(strat_trades),
            )

            min_drift_sample = 15 if min_live_sample is None else int(min_live_sample)
            if len(strat_trades) < min_drift_sample:
                report.is_diverging = False
                report.verdict = (
                    f"INSUFFICIENT_DATA ({len(strat_trades)}/{min_drift_sample})"
                )
            else:
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
    pf = f"{metrics.profit_factor:.2f}" if metrics.profit_factor is not None else "-"
    print(f"  Profit Factor: {pf}")
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
