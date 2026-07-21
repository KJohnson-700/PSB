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
    infer_updown_window_size,
    parse_updown_exit_globals,
    resolve_updown_exit_params,
    resolve_updown_exit_params_for_position,
    scaled_exit_window_mins,
)
from src.execution.fill_sim import polymarket_taker_fee_usdc, simulate_book_fill

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
    # Fill-quality telemetry (set only when realistic_paper_fills walked the book):
    # the pre-fill mark price and the slippage the sweep cost, as a fraction of cost
    # basis (negative = the fill lost us money vs the mark). Lets per-lane fill bleed
    # be measured so size/exit can be tuned per lane.
    fill_mark_price: Optional[float] = None
    fill_slippage_pct: Optional[float] = None
    fill_fee_usdc: Optional[float] = None
    fill_fee_rate: Optional[float] = None
    # Microstructure at exit (Codex 2026-06-22): seconds-to-expiry and book spread at
    # the exit tick. Lets gap-through risk (thin/late book) be split from stop policy —
    # a stop that overshoots when secs_to_expiry is tiny / spread is wide is a
    # microstructure fill problem, not a stop-threshold problem.
    secs_to_expiry_at_exit: Optional[float] = None
    exit_book_spread: Optional[float] = None
    exit_mark_src: Optional[str] = None
    exit_mark_age_ms: Optional[float] = None


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
        self.reload_from_config(config)

    def reload_from_config(self, config: Dict[str, Any]) -> None:
        """Refresh exit-rule config without replacing the manager object."""
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
        # Require the percentage stop to trigger on N consecutive exit ticks before
        # firing (default 2). At the 3s fast-exit cadence this is ~6s of confirmation
        # and prevents a single noisy book read from cutting a winner — the
        # 2026-06-17 BTC BUY_NO phantom stops resolved as WINS (+$17.80, +$30.92) yet
        # were stopped on one bad tick. Counter lives on the position and resets the
        # moment the mark recovers above the stop. Set to 1 to disable (fire on first
        # tick, legacy behavior).
        self._updown_stop_confirm_ticks = max(
            1, int(exit_cfg.get("updown_stop_confirm_ticks", 1) or 1)
        )
        # Mechanism-agnostic phantom-exit guard: refuse the EARLY % stop/TP on an
        # up/down position held fewer than N seconds. The late-window cents/time
        # stop + expiry exits are NOT gated (they fire near resolution and are the
        # real protection). Every phantom exit in the 2026-06-17 audit was a 3–41s
        # hold (impossible 28–52% moves from thin-book mids / the scanner's 0.5
        # placeholder leaking into the 60s scan-loop exit); every correct exit was
        # 14–30 min. A short floor cleanly separates them regardless of which price
        # path misfired. 0 = off (code default; settings.yaml enables it).
        # Per-window aware: on a 5m market a flat 60s floor = 20% of the entire
        # window, so a real fast adverse move gets held to the trough before the
        # stop can fire (2026-06-23 XRP 5m: stop true at 19s, suppressed 7x, dumped
        # at 0.045 = -$13.82 vs ~-$2.4 had it fired at ~20s). Accept either a scalar
        # (applies to all windows, legacy) or a {5m,15m,1h} dict.
        _mh_cfg = exit_cfg.get("updown_min_hold_sec_before_pct_exit", 0.0)
        if isinstance(_mh_cfg, dict):
            self._updown_min_hold_by_window = {
                str(k): max(0.0, float(v or 0.0)) for k, v in _mh_cfg.items()
            }
            self._updown_min_hold_sec_before_pct_exit = max(
                self._updown_min_hold_by_window.values(), default=0.0
            )
        else:
            self._updown_min_hold_by_window = {}
            self._updown_min_hold_sec_before_pct_exit = max(
                0.0, float(_mh_cfg or 0.0)
            )
        # 2026-07-02 Deploy1(U2b): min-hold anchored to window OPEN for pre-open
        # entries (live: 3x stops fired 42-58s after open; pre-open cohort +$770
        # must not lose its grace to the first repricing). RESTORED 2026-07-11:
        # stripped in the 07-09 08:18 file replacement; recurrence same session
        # 07-11 = 3 stops at +30/+42/+53s after window open on pre-open entries.
        # Kill-switch: updown_min_hold_anchor_window_open: false.
        self._min_hold_anchor_window_open = bool(
            exit_cfg.get("updown_min_hold_anchor_window_open", True)
        )
        # 2026-07-12 fresh-mark exemption: let TP/stop through the min-hold when the
        # exit mark is a live WS book update <= N ms old. The floor exists to block
        # phantom exits on thin/stale window-open marks; a fresh WS mark is not that.
        # 07-12 autopsy: suppression turned a +127% MFE into a -47.5% fill and let
        # stops fill 2-3.5x wide. 0 = disabled (pure suppression, pre-07-12 behavior).
        self._min_hold_fresh_mark_exempt_ms = float(
            exit_cfg.get("updown_min_hold_fresh_mark_exempt_ms", 2000) or 0.0
        )
        # Optional: realistic paper fills (default OFF). Paper/dry_run fills every
        # order at the requested price; this walks the book ladder so the recorded
        # exit price + P&L reflect the slippage a real sweep would pay. Covers all
        # exit legs (long-YES bids, long-NO = mirrored YES asks, short-YES asks)
        # when the snapshot carries them; entries still fill at the mark. See
        # check_exits and src/execution/fill_sim.py.
        self._realistic_paper_fills = bool(
            exit_cfg.get("realistic_paper_fills", False)
        )
        fee_cfg = (config.get("trading", {}) or {}).get("execution_fees", {}) or {}
        self._execution_fees_enabled = bool(fee_cfg.get("enabled", False))
        self._crypto_updown_15m_taker_fee_rate = float(
            fee_cfg.get("crypto_updown_15m_taker_fee_rate", 0.0) or 0.0
        )
        # When entries are taker fills, the position round-trips through TWO taker
        # fills, so paper must charge the entry-side taker fee too (not just the
        # exit). Paper models BOTH marketable and hybrid entries as taker (the
        # maker savings of hybrid are live-only and can't be modeled offline), so
        # the entry fee applies unless entries are pure maker. Resolves
        # trading.entry_mode (marketable|maker|hybrid), with the legacy
        # trading.entry_marketable bool as fallback.
        _t = config.get("trading", {}) or {}
        _entry_mode = str(_t.get("entry_mode") or "").lower()
        if not _entry_mode:
            _entry_mode = "marketable" if _t.get("entry_marketable", True) else "maker"
        self._entry_taker = _entry_mode != "maker"

    def _min_hold_floor_secs(self, pos) -> float:
        """Per-window phantom-exit floor. Prefers the position's EXPLICIT
        ``window_size`` (so a late 15m/1h entry with little runway left is NOT
        misclassified as 5m); only falls back to inferring from
        (end_date - opened_at) for legacy reloads with no window_size. Unknown /
        missing window -> the scalar floor (the conservative 60s)."""
        if not self._updown_min_hold_by_window:
            return self._updown_min_hold_sec_before_pct_exit
        label = infer_updown_window_size(
            getattr(pos, "window_size", "") or "",
            opened_at=getattr(pos, "opened_at", None),
            end_date=getattr(pos, "end_date", None),
        )
        return self._updown_min_hold_by_window.get(
            label, self._updown_min_hold_sec_before_pct_exit
        )

    def _preopen_lag_secs(self, pos) -> float:
        """Seconds between entry and the window OPEN for pre-open entries.

        Min-hold is measured from max(entry, window_open) so a position entered
        before its window starts keeps the full phantom-exit grace after the
        first real repricing at open. Kill-switch:
        updown_min_hold_anchor_window_open: false.
        """
        if not getattr(self, "_min_hold_anchor_window_open", True):
            return 0.0
        try:
            end = getattr(pos, "end_date", None)
            opened = getattr(pos, "opened_at", None)
            if end is None or opened is None:
                return 0.0
            # Codex 2026-07-11: UTC-normalize so journal-reloaded naive
            # datetimes don't TypeError into the except -> 0.0 fallback
            # (which would silently degrade to entry-anchored behavior).
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            wl = {"5m": 300.0, "15m": 900.0, "1h": 3600.0}.get(
                str(getattr(pos, "window_size", "") or "").lower()
            )
            if not wl:
                return 0.0
            return max(0.0, (end - opened).total_seconds() - wl)
        except Exception:
            return 0.0

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
        token_map = market_token_ids or {}

        for pos_id, pos in active_positions.items():
            opened_at = pos.opened_at
            tzinfo = getattr(opened_at, "tzinfo", None)
            now = datetime.now(tzinfo) if tzinfo is not None else datetime.now()
            hours_held = (now - opened_at).total_seconds() / 3600.0
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

                # Flatten-before-resolution FIRST: in the final window pre-empt the
                # resolution gap. MUST precede the stop check -- an underwater position is
                # below its stop and would otherwise loop in the stop branch (failing to
                # fill a one-sided near-resolution book) and hold to a binary-zero
                # resolution, which is exactly the gap-through leak. Skip only a winner on
                # a hold-to-resolution lane (let it ride up to 1.0).
                _flat_mins_remaining = None
                if pos.end_date is not None:
                    _flat_end = pos.end_date
                    if _flat_end.tzinfo is None:
                        _flat_end = _flat_end.replace(tzinfo=timezone.utc)
                    _flat_mins_remaining = (
                        _flat_end - datetime.now(timezone.utc)
                    ).total_seconds() / 60.0

                # TP: exit early when price spikes strongly in our favour rather than
                # waiting for binary resolution (captures most of the gain).
                if (
                    _flat_mins_remaining is not None
                    and resolved.updown_flatten_before_resolution_sec > 0
                    and 0.0
                    <= _flat_mins_remaining * 60.0
                    <= resolved.updown_flatten_before_resolution_sec
                    and not (
                        resolved.updown_hold_winners_to_resolution and pnl_pct >= 0
                    )
                ):
                    reason = "updown_flatten_pre_resolution"
                elif (
                    # 2026-07-17 TIME-GATED LATE TP (default OFF; 0.0 => never fires).
                    # Deliberately NOT gated by hold_winners: on a hold lane an ungated
                    # TP/trail cuts the +85%% runner mid-window (failed 07-13 + 07-16),
                    # but a green position inside the final N minutes is a FADE, not a
                    # runner. Fires before the stop so it banks instead of stopping out.
                    resolved.take_profit_late_pct > 0.0
                    and resolved.take_profit_late_gate_mins > 0.0
                    and _flat_mins_remaining is not None
                    and 0.0 <= _flat_mins_remaining <= resolved.take_profit_late_gate_mins
                    and pnl_pct >= resolved.take_profit_late_pct
                ):
                    reason = "take_profit_late"
                elif (
                    not resolved.updown_hold_winners_to_resolution
                    and pnl_pct >= resolved.take_profit_pct
                ):
                    reason = "take_profit"
                elif effective_stop_loss_pct != 0 and stop_pnl_pct <= -effective_stop_loss_pct:
                    # Same-position percentage stop: cuts adverse drift early instead of
                    # waiting for the late-window cents stop, which fires at whatever price
                    # the position has already collapsed to. stop_pnl_pct == pnl_pct unless
                    # stop_use_executable_price is on (then it marks the exit-side bid).
                    # Require N consecutive triggering ticks (default 2) so one noisy
                    # book read can't cut a winner; reset the moment the mark recovers.
                    _confirm = int(getattr(pos, "_stop_confirm_count", 0)) + 1
                    setattr(pos, "_stop_confirm_count", _confirm)
                    if _confirm >= self._updown_stop_confirm_ticks:
                        reason = "updown_stop_loss"
                    else:
                        logger.debug(
                            "Stop pending confirm %d/%d for %s (stop_pnl=%.1f%%)",
                            _confirm, self._updown_stop_confirm_ticks,
                            pos.market_id, stop_pnl_pct * 100,
                        )
                else:
                    # Mark is above the stop this tick — clear any pending stop
                    # confirmation so a transient dip doesn't carry over.
                    if getattr(pos, "_stop_confirm_count", 0):
                        setattr(pos, "_stop_confirm_count", 0)
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

            # Phantom-exit floor: suppress the EARLY % stop/TP until the position has
            # aged past the min-hold. Leaves the late-window cents/time/expiry stops
            # untouched (they only fire near resolution). Resets the stop-confirm
            # count so it re-confirms cleanly once the hold clears.
            _min_hold_floor = self._min_hold_floor_secs(pos)
            _held_eff_secs = hours_held * 3600.0 - self._preopen_lag_secs(pos)
            if (
                is_updown
                and reason in ("take_profit", "updown_stop_loss")
                and _min_hold_floor > 0
                and _held_eff_secs < _min_hold_floor
            ):
                _fx_ms = getattr(self, "_min_hold_fresh_mark_exempt_ms", 0.0)
                _liq = (market_liquidity or {}).get(pos.market_id) or {}
                _mark_age = _liq.get("mark_age_ms")
                if (
                    _fx_ms > 0
                    and _liq.get("mark_src") == "ws"
                    and _mark_age is not None
                    and float(_mark_age) <= _fx_ms
                ):
                    logger.info(
                        "Min-hold fresh-mark exemption: allowing %s for %s at "
                        "held-eff %.0fs (ws mark age %.0fms <= %.0fms)",
                        reason, pos.market_id, _held_eff_secs,
                        float(_mark_age), _fx_ms,
                    )
                else:
                    logger.info(
                        "Suppress %s for %s: held-eff %.0fs < min-hold %.0fs "
                        "(phantom-exit floor, window-open anchored)",
                        reason, pos.market_id, _held_eff_secs,
                        _min_hold_floor,
                    )
                    reason = None
                    if getattr(pos, "_stop_confirm_count", 0):
                        setattr(pos, "_stop_confirm_count", 0)

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
                elif reason in (
                    "updown_flatten_pre_resolution",
                    "updown_expired",
                    "updown_time_stop",
                    # 2026-07-17 (Codex catch): the time-gated late TP fires INSIDE the
                    # final gate window, so it must take the bid NOW (FAK) like the other
                    # near-resolution exits. Left as a resting GTC it would not fill at the
                    # qualifying tick — which is exactly the execution gap the sim assumed
                    # away and where the previous 3 give-back attempts died.
                    "take_profit_late",
                ):
                    # Near/at resolution: take the bid NOW (FAK) rather than resting a
                    # limit that won't fill into a one-sided book and lets the position
                    # gap to a binary-zero resolution. exec_exit_price when available,
                    # else the current exit-side mark.
                    if exec_exit_price is not None:
                        exit_price = exec_exit_price
                    exit_marketable = True

                # Realistic paper fill: replace the requested-price fill with a
                # book-walked sweep so recorded paper P&L reflects slippage instead
                # of filling at the mark. Walks the relevant ladder for the position
                # leg; size beyond captured depth pads at the deepest level
                # (bounded-pessimistic). exit_price stays in the same token space the
                # leg's default branch above used (NO-space for long NO, YES-space
                # otherwise). No-ops when the needed ladder is absent.
                fill_mark_price = None
                fill_slippage_pct = None
                fill_fee_usdc = None
                fill_fee_rate = None
                if self._realistic_paper_fills:
                    _mark_price = exit_price
                    _mark_pnl = unrealized_pnl
                    _liq = (market_liquidity or {}).get(pos.market_id) or {}
                    if entry_leg == "NO":
                        # Long NO: sell NO. NO bids = YES asks mirrored (price 1-a).
                        _levels = [
                            (1.0 - a.get("price"), a.get("size"))
                            for a in (_liq.get("asks") or [])
                            if a.get("price") is not None
                        ]
                        _fill_px, _filled = simulate_book_fill(
                            "SELL", pos.size, _levels, marketable=True,
                            pad_remainder_at_worst=True,
                        )
                        if _filled > 0:
                            exit_price = _fill_px
                            unrealized_pnl = pos.size * (_fill_px - pos.entry_price)
                    elif pos.outcome == "NO":
                        # Short YES: buy back YES -> walk the YES ask ladder.
                        _levels = [(a.get("price"), a.get("size")) for a in (_liq.get("asks") or [])]
                        _fill_px, _filled = simulate_book_fill(
                            "BUY", pos.size, _levels, marketable=True,
                            pad_remainder_at_worst=True,
                        )
                        if _filled > 0:
                            exit_price = _fill_px
                            unrealized_pnl = pos.size * (pos.entry_price - _fill_px)
                    else:
                        # Long YES: sell YES -> walk the YES bid ladder.
                        _levels = [(b.get("price"), b.get("size")) for b in (_liq.get("bids") or [])]
                        _fill_px, _filled = simulate_book_fill(
                            "SELL", pos.size, _levels, marketable=True,
                            pad_remainder_at_worst=True,
                        )
                        if _filled > 0:
                            exit_price = _fill_px
                            unrealized_pnl = pos.size * (_fill_px - pos.entry_price)
                    # Record what the book walk cost vs the mark, normalized by cost
                    # basis (negative = the sweep lost us money). Only when it moved.
                    if exit_price != _mark_price and cost_basis > 0:
                        fill_mark_price = round(_mark_price, 4)
                        fill_slippage_pct = round(
                            (unrealized_pnl - _mark_pnl) / cost_basis, 4
                        )

                # Fee-enabled Polymarket crypto up/down markets charge takers at
                # match time. Paper exits are book-walked as taker fills, so model
                # the fee here instead of letting historical ghosts overstate edge.
                # Live charges the taker fee on ALL crypto up/down windows (5m/15m/1h
                # all return feeSchedule rate 0.07, verified via Gamma 2026-06-13), so
                # do not restrict the paper fee to 15m — that under-charged 5m/1h exits.
                _window = str(getattr(pos, "window_size", "") or "").lower()
                if (
                    self._execution_fees_enabled
                    and str(getattr(pos, "strategy", "") or "") in CRYPTO_UPDOWN_STRATEGIES
                ):
                    _liq = (market_liquidity or {}).get(pos.market_id) or {}
                    _fee_rate = _liq.get("taker_fee_rate")
                    if _fee_rate is None:
                        _fee_rate = self._crypto_updown_15m_taker_fee_rate
                    _fee_rate = float(_fee_rate or 0.0)
                    # The live get_fee_rate_bps endpoint has been returning an
                    # implausible 1000 bps (0.10) on crypto up/down markets — ~10%
                    # round-trip on a $0.50 binary, far above the real venue fee.
                    # That bogus rate alone flipped a +$96 gross session to -$24 net
                    # (and is the entire "R:R inversion" artifact). Treat the
                    # configured crypto taker rate as the authoritative ceiling so a
                    # bad live value can't dominate paper P&L; a genuinely lower
                    # market fee still passes through via min().
                    _cfg_rate = float(self._crypto_updown_15m_taker_fee_rate or 0.0)
                    if _cfg_rate > 0:
                        _fee_rate = min(_fee_rate, _cfg_rate)
                    _exit_fee = polymarket_taker_fee_usdc(
                        pos.size,
                        exit_price,
                        _fee_rate,
                    )
                    # Marketable entries are taker fills too, so charge the entry-side
                    # taker fee (priced at the entry fill) for the full round-trip cost.
                    _entry_fee = (
                        polymarket_taker_fee_usdc(pos.size, pos.entry_price, _fee_rate)
                        if self._entry_taker
                        else 0.0
                    )
                    _fee = _exit_fee + _entry_fee
                    if _fee > 0:
                        fill_fee_usdc = round(_fee, 5)
                        fill_fee_rate = _fee_rate
                        unrealized_pnl -= _fee

                # Microstructure-at-exit telemetry (best-effort; never blocks the exit).
                _secs_to_expiry = None
                try:
                    if pos.end_date is not None:
                        _ed = pos.end_date
                        if _ed.tzinfo is None:
                            _ed = _ed.replace(tzinfo=timezone.utc)
                        _secs_to_expiry = round(
                            (_ed - datetime.now(timezone.utc)).total_seconds(), 1
                        )
                except Exception:
                    _secs_to_expiry = None
                _bb = locals().get("_best_bid")
                _ba = locals().get("_best_ask")
                _exit_spread = (
                    round(float(_ba) - float(_bb), 4)
                    if _bb is not None and _ba is not None
                    else None
                )

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
                        fill_mark_price=fill_mark_price,
                        fill_slippage_pct=fill_slippage_pct,
                        fill_fee_usdc=fill_fee_usdc,
                        fill_fee_rate=fill_fee_rate,
                        secs_to_expiry_at_exit=_secs_to_expiry,
                        exit_book_spread=_exit_spread,
                        exit_mark_src=((market_liquidity or {}).get(pos.market_id) or {}).get("mark_src"),
                        exit_mark_age_ms=((market_liquidity or {}).get(pos.market_id) or {}).get("mark_age_ms"),
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
