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
from typing import Dict, List, Optional, Any, Tuple, Set

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
try:
    from src.analysis.tape_map import latest_tape_state as _latest_tape_state
except Exception:  # pragma: no cover - defensive; shadow simply no-ops if unavailable
    _latest_tape_state = None  # type: ignore

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
    # data-loop C (2026-07-30): exit book-quality symmetric to entry_paper_fill_quality.
    exit_best_bid: Optional[float] = None
    exit_best_ask: Optional[float] = None
    exit_depth_at_limit: Optional[float] = None
    exit_fill_ratio: Optional[float] = None
    exit_mark_src: Optional[str] = None
    exit_mark_age_ms: Optional[float] = None
    # 2026-07-30 PAPER CALIB Phase 3.6: separate SIGNAL wins from EXECUTION wins.
    # raw_signal_pnl = mark-to-mark PnL with NO exit slippage and NO fee (what the
    # signal "earned"); execution_adjusted_pnl = the realized PnL actually booked
    # (book-walked exit fill minus taker fees). Their gap = execution drag. Judge
    # strategies by execution_adjusted_pnl. Both None when realistic_paper_fills is off.
    raw_signal_pnl: Optional[float] = None
    execution_adjusted_pnl: Optional[float] = None
    # 2026-08-01 regular-TP executable-net guard telemetry (set only on reason=="take_profit"
    # when the guard is enabled): the MIDPOINT-mark pnl that triggered TP vs the pnl we'd
    # actually realize at the exit-side price (gross before fee, and net after round-trip
    # taker fee). tp_trigger_mark_pnl_pct − tp_executable_net_pnl_pct = the mark-vs-executable
    # gap this guard exists to catch. None on non-TP exits / guard off / no book.
    tp_trigger_mark_pnl_pct: Optional[float] = None
    tp_executable_exit_price: Optional[float] = None
    tp_executable_gross_pnl_pct: Optional[float] = None
    tp_executable_net_pnl_pct: Optional[float] = None


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
        # 2026-07-28 SEVERITY-GATED CUTS (real never-green fix). never_green_cut and
        # updown_time_stop only fire when the position is ACTUALLY deeply negative, not
        # merely never-green / adverse. Realized: cut-but-would-win median pnl_at_exit
        # -3.5%; cut-and-would-lose-more -9.7%. Gating never_green_cut at pnl<=-8% saves
        # +$33.61 with 0 false-cut winners (vs current +$18.24); time_stop at <=-20% saves
        # +$19.62. 0.0 = OFF (fires exactly as before, backward-compatible).
        self.never_green_cut_min_loss_pct = float(exit_cfg.get("never_green_cut_min_loss_pct", 0.0) or 0.0)
        self.updown_time_stop_min_loss_pct = float(exit_cfg.get("updown_time_stop_min_loss_pct", 0.0) or 0.0)
        # 2026-08-04 PER-LANE ngc severity gate (operator GO). The global default seeds from the
        # top-level never_green_cut.min_loss_pct_default (falls back to the legacy exit_rules value,
        # then 0.0=off). Per-lane overrides keyed "strategy:window:side" carry their own min_loss_pct.
        # ngc cuts only when pnl_pct <= -min_loss_pct(lane). cut_after_secs per-lane lives in the
        # main.py observer; this half is the severity gate.
        _ngc_top = (config.get("never_green_cut", {}) or {})
        self._ngc_min_loss_default = float(
            _ngc_top.get("min_loss_pct_default", self.never_green_cut_min_loss_pct) or 0.0
        )
        self._ngc_by_lane_min_loss = {}
        _ngc_by_lane_cfg = _ngc_top.get("by_lane", {})
        if isinstance(_ngc_by_lane_cfg, dict):
            for _lk, _lv in _ngc_by_lane_cfg.items():
                if isinstance(_lv, dict) and _lv.get("min_loss_pct") is not None:
                    try:
                        self._ngc_by_lane_min_loss[str(_lk)] = float(_lv["min_loss_pct"])
                    except (TypeError, ValueError):
                        pass
        # 2026-08-04 TAPE-CONDITIONED STOP DEFERRAL (per-lane, LONG-only). A LONG whose asset
        # tape reads UP (HTF-confirmed) is riding an up-tape dip, not a wrong-side loss — defer its
        # %-stop while the loss is SHALLOW (above floor_pct). Past the floor, or in a DOWN/FLAT tape,
        # the stop fires exactly as before (the bull<->bear self-flip). Keyed "strategy:BUY_YES"
        # (side-isolated). Loser-floor is mandatory (see holdmeanshold_no_loser_floor blowup) and the
        # -50% catastrophic backstop is untouched. LIVE-forward validated (ghosts don't cover exits).
        _th_top = (config.get("tape_hold_stop", {}) or {})
        self._tape_hold_enabled = bool(_th_top.get("enabled", False))
        self._tape_hold_by_lane = {}
        _th_by_lane_cfg = _th_top.get("by_lane", {})
        if isinstance(_th_by_lane_cfg, dict):
            for _tk, _tv in _th_by_lane_cfg.items():
                if not isinstance(_tv, dict):
                    continue
                try:
                    self._tape_hold_by_lane[str(_tk)] = {
                        "floor_pct": abs(float(_tv.get("floor_pct", 0.15) or 0.15)),
                        "conf_min": float(_tv.get("conf_min", 0.60) or 0.60),
                        "max_age_s": float(_tv.get("max_age_s", 90.0) or 90.0),
                        "require_1h_macd": int(_tv.get("require_1h_macd", 1) or 0),
                    }
                except (TypeError, ValueError):
                    pass
        # 2026-08-01 TIME-UNDERWATER SHADOW (Codex audit; BTC 1h BUY_YES). LOG-ONLY, no behavior
        # change. 1h holds rode the hour: losers 25%WR/-$43 (RESOLVED:NO + catastrophic), BUT the
        # winners ALSO ride underwater then take_profit_late, and the code deliberately excludes 1h
        # from never_green_cut (winners develop slowly, get false-cut). So a live time-cut is unsafe
        # until measured. This logs what a cut WOULD do at the moment it would fire, so an offline
        # join to final outcome measures winner-false-cut vs loser-save BEFORE we ever enable it.
        # Never sets an exit reason. Enable the live cut only after the shadow proves it clean.
        self._tu_shadow_enabled = bool(exit_cfg.get("time_underwater_1h_shadow_enabled", True))
        self._tu_min_held_min = float(exit_cfg.get("time_underwater_1h_min_held_min", 12.0) or 12.0)
        self._tu_max_mfe_pct = float(exit_cfg.get("time_underwater_1h_max_mfe_pct", 0.03) or 0.03)
        self._tu_max_pnl_pct = float(exit_cfg.get("time_underwater_1h_max_pnl_pct", 0.0) or 0.0)
        # 2026-08-03 TAPE-AWARE FASTER-STOP shadow (operator GO). LOG-ONLY: when a LOSING open
        # position has the tape_map turned AGAINST its side (long/YES vs tape DOWN, short/NO vs
        # tape UP) at conf>=min, record a would-cut-here row (never sets an exit reason). Offline
        # join to the final outcome then measures would-SAVE (rode further down) vs FALSE-CUT
        # (recovered to a win) by window — the pass/fail read before any live tape-stop. Tape is
        # only 53% directional @15m / 60% @60m, so a live cut MUST be measured first (the 47%
        # false-cut risk at 15m is exactly why this is a shadow, not a live cut).
        self._tape_stop_shadow_enabled = bool(exit_cfg.get("tape_stop_shadow_enabled", True))
        self._tape_stop_conf_min = float(exit_cfg.get("tape_stop_conf_min", 0.6) or 0.6)
        self._tape_stop_min_loss_pct = float(exit_cfg.get("tape_stop_min_loss_pct", 0.03) or 0.03)
        self._tape_stop_max_age_s = float(exit_cfg.get("tape_stop_max_age_s", 90.0) or 90.0)
        # 2026-07-29 HOLD MEANS HOLD (operator GO + Codex proof). A true hold-to-resolution
        # lane must ride to resolution — the soft exits (stop/trail/in-profit/time/flatten/
        # never-green) were cutting hold lanes early (sol 5m down: wins 11/11 +$252 when
        # resolved but only 8.6% get there; early cuts -$84). When enforced, hold lanes
        # suppress every premature exit, leaving ONLY expiry/resolution and a CATASTROPHIC
        # stop. hold_means_hold_enforce hot-reloads OFF; catastrophic pct is fraction (0.5=-50%).
        self._hold_means_hold_enforce = bool(exit_cfg.get("hold_means_hold_enforce", True))
        self._hold_catastrophic_stop_pct = float(exit_cfg.get("hold_catastrophic_stop_pct", 0.5) or 0.0)
        # 2026-08-07 FAVORITE PRE-SETTLEMENT DE-RISK: a %-stop CANNOT catch a favorite gapping
        # ~0.85->$0 in the final settlement candle (the sol -$70.81 rode entry 0.87 to RESOLVED:NO).
        # For a favorite HOLD (identified by a high our-side entry >= floor-0.03, since favorites
        # enter >=0.85 while direction trades enter near 0.5), in the final N secs, if our side has
        # FLIPPED to <= presettle_derisk_price (now the underdog = losing), exit to salvage instead
        # of riding to $0. Winners mark toward 1.0 near settlement so they are NEVER touched.
        _fav_cfg = (config.get("favorite_lane", {}) or {})
        self._fav_presettle_secs = float(_fav_cfg.get("presettle_derisk_secs", 0.0) or 0.0)
        self._fav_presettle_price = float(_fav_cfg.get("presettle_derisk_price", 0.5) or 0.5)
        self._fav_derisk_min_entry = max(0.0, float(_fav_cfg.get("floor", 0.85) or 0.85) - 0.03)
        # 2026-08-08 FAVORITE CONTINUOUS HARD-STOP (operator: "losers too big for this to work").
        # The two existing favorite exits both leave the SMALLEST loss at ~-$30: hold_catastrophic
        # can't even trigger until the mark hits entry*(1-0.55)~0.38 (a -55% loss), and the presettle
        # de-risk only fires in the final _fav_presettle_secs. So a favorite winning +8..18% pays a
        # -30..-66 loser — break-even needs ~80% WR (measured 81% WR still -$43/session). This cuts a
        # favorite the moment its our-side mark walks down to hard_stop_price at ANY point in the hold,
        # capping the loss early (0.70 from 0.87 = -20%, not -55%): break-even WR = (p-x)/(1-x) falls
        # 79%->57%. 0.70 sits DEEP inside the plausible band (delta 0.17 << 0.50) so it fires on the
        # real walk-down, never on an inverted/junk print. OFF unless favorite_lane.hard_stop_price>0.
        self._fav_hard_stop_price = float(_fav_cfg.get("hard_stop_price", 0.0) or 0.0)
        # 2026-08-08 WINDOW-CONDITIONAL EARLY STOPS (operator GO; held-projection this session).
        # The early hold-lane cuts (favorite_hard_stop + the independent -catastrophic backstop)
        # GAP THROUGH on fast windows: the stop-hold sweep showed 5m/15m winners routinely dip to
        # -20..-46% MAE mid-window and STILL resolve GREEN, so those stops fire on recoverable
        # noise and lock a gap-through loss (hold beats stop on sol/xrp/hype 5m+15m; doge 1h is the
        # LONE lane where holding LOSES ~-35). Gate the early cuts to the windows where holding
        # actually loses -> default {"1h"}. On 5m/15m the position rides to resolution; the
        # final-seconds presettle de-risk (favorite gap-to-0 salvage) is a SEPARATE block that
        # stays active on EVERY window. Tokens: "all" => keep the stop on every window (pre-change
        # behavior); [] => keep it nowhere. RESTART-CLASS (execution code; not hot-reloadable).
        _esw = _fav_cfg.get("early_stop_windows", None)
        if _esw is None:
            _esw = exit_cfg.get("hold_early_stop_windows", ["1h"])
        self._hold_early_stop_windows = {str(w).lower() for w in (_esw or [])}
        # 2026-08-09 MFE-CONDITIONAL STOP (operator GO; ghost-confirmed; SUPERSEDES the window axis).
        # Ghost-settling the 130 hold_catastrophic_stop trades held-to-resolution: 88% (105/120) would
        # STILL resolve to $0 — they were wrong-side entries that never went green (100% had MFE<5%).
        # Only 12% recovered, and those dipped BELOW -55% then came back, so the -55% stop already cut
        # them regardless. Root regression vs the +$450 June era: June cut losers at ~-34% (b=1.2-1.7);
        # Aug rides them to -47%+ (b=0.49). The WINDOW axis (1h stops / 5m-15m ride) is the wrong
        # discriminator — it lets never-green 5m/15m losers ride to -100%. The right axis is MFE:
        #   * NEVER went green (peak_pnl_pct < arm) => wrong-side entry => cut SHALLOW (~June -34%).
        #   * went green THEN dipped (peak >= arm)  => recoverable winner => ride; deep -cat backstop only.
        # When enabled this REPLACES _window_keeps_early_stop for the catastrophic + favorite-hard cuts.
        # Falls back to the window axis when disabled. RESTART-CLASS (execution; not hot-reloadable).
        self._mfe_cond_stop_enabled = bool(exit_cfg.get("hold_mfe_conditional_stop_enabled", True))
        self._hold_never_green_mfe_arm = float(exit_cfg.get("hold_never_green_mfe_arm", 0.08) or 0.0)
        self._hold_never_green_stop_pct = float(exit_cfg.get("hold_never_green_stop_pct", 0.34) or 0.0)
        # 2026-08-18 PER-WINDOW never-green trigger (loop finding F3 / fix-order #3). The -34%
        # TRIGGER delivered -39..-82% of stake on 5m gap-through (12 era cuts; CLOB history
        # confirmed the gaps are real tape, not eval lag). The winning-era invariant is losses
        # cut -27..-42 DELIVERED, so the fast-window trigger must sit shallower than the target
        # depth. Absent => global hold_never_green_stop_pct (15m/1h unchanged without their own
        # evidence — same per-window discipline as tp_giveback_retrace_pct_by_window above).
        # RESTART-CLASS (cached here).
        _ngw = exit_cfg.get("hold_never_green_stop_pct_by_window") or {}
        self._hold_never_green_by_window = (
            {str(k).lower(): float(v) for k, v in _ngw.items()}
            if isinstance(_ngw, dict) else {}
        )
        # 2026-08-17 EVER-GREEN GIVE-BACK FLOOR (operator GO, Codex GO). Live post-restore data
        # killed the blanket ever-green exemption on fast windows: 5/5 ever-green riders (all 5m,
        # peak MFE +13.7..+32.4%) resolved to a -103% full forfeit, -$49.76, zero rider wins. The
        # "riders recover 99%" claim below was measured inside the broken-era exit stack. On the
        # configured windows, once peak MFE >= the never-green arm, cut when pnl gives back
        # `giveback_points` from the peak (floor clamped to [breakeven .. never-green -34%]).
        # 1h stays exempt (the sol-1h peak+45.7%-cut-at--55.4% would-have-won class). RESTART-CLASS.
        self._hold_evergreen_giveback_enabled = bool(exit_cfg.get("hold_evergreen_giveback_enabled", False))
        self._hold_evergreen_giveback_points = float(exit_cfg.get("hold_evergreen_giveback_points", 0.40) or 0.40)
        # 2026-08-19 EXPIRY SETTLE DEFER (operator GO). The past-expiry branch used to SELL AT
        # THE LAST MARK the instant a market expired ("updown_expired" at exit_price 0.49 —
        # impossible for a binary settle), beating the ResolutionTracker's 60s poll to the
        # position and recording fiction (10 fake settles on 08-18 alone; in live it would
        # GIVE AWAY held positions at the mid). Hold through this grace so the tracker settles
        # binary; past grace, mark-close under the HONEST label updown_expired_mark_fallback
        # (endswith check keeps it OUT of the resolution-graded family).
        self._updown_expiry_grace_mins = float(exit_cfg.get("updown_expiry_grace_mins", 10.0) or 10.0)
        _gbw = exit_cfg.get("hold_evergreen_giveback_windows", ["5m", "15m"])
        self._hold_evergreen_giveback_windows = {str(w).lower() for w in (_gbw or [])}
        # 2026-08-06 GIVE-BACK TRAILING TP (the missing banking mechanism under hold_all). hold_all sets
        # updown_hold_winners_to_resolution=True, which DISABLES the regular take_profit (_tp_mark_ready
        # requires `not hold_winners`) — so a winner that peaks then reverses has NO way to bank and rides
        # to resolution or the catastrophic. Data (12:51 audit): 17 exits peaked avg +49% MFE then round-
        # tripped to -65%. This TRAILING TP fires UNDER hold: once peak MFE >= arm, exit if price retraces
        # >= retrace from the peak — banks a REVERSING winner (peak+30/retrace20 => exit ~+10%, still green)
        # WITHOUT cutting a STILL-CLIMBING runner (no retrace => no fire; that's what sank the 07-13/07-16
        # fixed-level trails). Enabled:false => off. RESTART-CLASS.
        self._tp_giveback_enabled = bool(exit_cfg.get("tp_giveback_enabled", False))
        self._tp_giveback_arm_pct = float(exit_cfg.get("tp_giveback_arm_pct", 0.30) or 0.30)
        self._tp_giveback_retrace_pct = float(exit_cfg.get("tp_giveback_retrace_pct", 0.20) or 0.20)
        # 2026-08-12 PER-WINDOW retrace override (see _tpgb_retrace_for). Absent => global.
        _tpgb_bw = exit_cfg.get("tp_giveback_retrace_pct_by_window") or {}
        self._tp_giveback_retrace_by_window = (
            {str(k).lower(): float(v) for k, v in _tpgb_bw.items()}
            if isinstance(_tpgb_bw, dict) else {}
        )
        # 2026-07-31 LOSER-FLOOR (operator GO; restart-class). Root cause of the dominant
        # loss bucket: hold-enforce suppressed a hold lane's OWN configured stop, so a loser
        # rode from entry to -catastrophic (hold_catastrophic_stop, default -50%) before
        # exiting. When enabled, honor each hold lane's own updown_stop_loss_pct as a
        # LOSER-FLOOR: let its % stop FIRE (exit ~-lane_stop) instead of riding to the
        # catastrophic backstop. Hold-for-winners is preserved (the % stop only ever set
        # reason=updown_stop_loss on a position already at -lane_stop; a green/developing
        # position never enters that branch). Default OFF: shipping is behavior-neutral until
        # the operator sets per-lane stops ABOVE each lane's winner-MAE tail and flips this
        # true (winner-clip risk lives in the CONFIGURED VALUE, not this code). Re-read here,
        # so it hot-reloads the same way hold_means_hold_enforce does.
        self._hold_lane_loser_floor_enabled = bool(
            exit_cfg.get("hold_lane_loser_floor_enabled", False)
        )
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
        # 2026-08-01 REGULAR-TP EXECUTABLE-NET GUARD (default OFF => legacy behavior).
        # The regular `take_profit` branch fires on the YES-MIDPOINT pnl, but a TP that
        # sells into the exit-side bid can close flat/red once the fill + round-trip
        # taker fee land. When enabled, the TP only fires if the EXECUTABLE net pnl
        # (exit-side price minus round-trip taker fee) is >= take_profit_net_min_pct.
        # Fail-open to the midpoint gate when no book snapshot exists (never strand a
        # winner on a missing book). Deliberately does NOT touch take_profit_late,
        # which is performing well. See the take_profit branch in check_exits.
        self._tp_require_executable_net = bool(
            exit_cfg.get("take_profit_require_executable_net", False)
        )
        self._tp_net_min_pct = float(
            exit_cfg.get("take_profit_net_min_pct", 0.0) or 0.0
        )
        _hold_fixed_tp = exit_cfg.get("hold_fixed_take_profit", {}) or {}
        self._hold_fixed_tp_enabled = bool(_hold_fixed_tp.get("enabled", False))
        self._hold_fixed_tp_pct = float(_hold_fixed_tp.get("threshold_pct", 0.40) or 0.40)
        _hold_fixed_lanes = _hold_fixed_tp.get("lanes", []) or []
        self._hold_fixed_tp_lanes: Set[str] = {
            str(lane).strip()
            for lane in _hold_fixed_lanes
            if str(lane).strip()
        }
        # Wide-book winner guard (2026-07-21). When the executable-price stop is
        # evaluated on a book WIDER than exit_max_book_spread, also require the YES
        # midpoint to confirm the loss before firing — the executable bid can sit far
        # below a healthy midpoint on a wide book and phantom-stop a genuine winner
        # (the risk wide_book_stop_through opens). Uses the existing (previously
        # unwired) stop_dual_confirm_midpoint flag. No-ops on tight books and whenever
        # wide_book_stop_through is OFF (those wide-book ticks never reach the stop).
        self._stop_dual_confirm_midpoint = bool(
            exit_cfg.get("stop_dual_confirm_midpoint", False)
        )
        self._exit_max_book_spread = float(
            exit_cfg.get("exit_max_book_spread", 0.30) or 0.30
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
        # 2026-07-24 gap-through bypass (STAGED): fire the % stop on the FIRST
        # triggering tick -- skipping the N-tick confirm -- when the FRESH mark is
        # already this far past the stop (a real collapse, not the single-noisy-read
        # the confirm guards). 0.0 = off. See the stop branch in check_exits.
        self._updown_stop_gap_bypass_pct = float(
            exit_cfg.get("updown_stop_gap_bypass_pct", 0.0) or 0.0
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
        # 2026-08-08 MAKER-FIRST FEE WEIGHTING (#1, operator go). The exit/entry fee block
        # below charged the FULL taker fee on BOTH legs for every crypto up/down lane — but
        # LIVE runs maker-first on the hybrid windows (15m/1h), where a maker leg that fills
        # pays 0%. So paper was systematically PESSIMISTIC on 15m/1h fees (see the block's own
        # KNOWN-CROSS-LANE-BIAS #2 note). This weights each leg's fee by (1 - maker_fill_rate)
        # on the hybrid windows ONLY (5m is FAK/taker -> unchanged, still fair vs live). The
        # rate is MEASURED from data/calibration/order_lifecycle.jsonl (maker_full/partial/only
        # vs marketable/zero_cross_fak = ~0.56 over n=27; config set to a conservative 0.50),
        # NOT a fabricated constant. 0.0 => OFF (byte-identical to the old full-taker behavior).
        self._hybrid_maker_fill_rate = float(fee_cfg.get("hybrid_maker_fill_rate", 0.0) or 0.0)
        # 2026-07-31 STAGED paper execution-realism knobs (#4b + #1). ALL gated below on
        # self._paper_mode so LIVE (dry_run=false) exit accounting is byte-for-byte unchanged.
        # Read here (with the rest of exit_rules) so they HOT-RELOAD; dry_run itself hot-reloads
        # the same way is_paper does in main.py. self._crypto_updown_15m_taker_fee_rate etc.
        # already live above; these join them.
        self._paper_mode = bool(_t.get("dry_run", True))
        # #4b missing-book spread-cross (see settings.yaml trading.exit_rules).
        self._paper_missing_book_haircut_enabled = bool(
            exit_cfg.get("paper_missing_book_haircut_enabled", True)
        )
        self._paper_missing_book_haircut_cents = float(
            exit_cfg.get("paper_missing_book_haircut_cents", 0.02) or 0.0
        )
        # #1 submission-latency / adverse-selection slip (bps of fill price) under trading.*;
        # larger on 15m/1h hybrid windows (they wait ~8s for the maker leg before crossing).
        self._paper_latency_slip_bps = float(_t.get("paper_latency_slip_bps", 5.0) or 0.0)
        self._paper_latency_slip_bps_hybrid = float(
            _t.get("paper_latency_slip_bps_hybrid", 25.0) or 0.0
        )

        # 2026-08-03 P0 LATE-ONLY STOP (Codex-planned bundle). Realized forensics: the early %
        # stop (updown_stop_loss) was 63% of exits at 3% WR / -$92 — it knifed winners that would
        # resolve green (a 15m/1h binary can be -15..-30% mid-window and still settle 1.0). On
        # 15m/1h, SUPPRESS the % stop until `earliest_mins` into the window; only a DEEP collapse
        # on a FRESH ws mark cuts early, and the -50% catastrophic backstop below is untouched.
        # Reversible: updown_late_stop_enabled=false restores the old always-on behavior. Keys are
        # __init__-frozen (restart-class). 5m is NOT gated (kept as-is).
        self._late_stop_enabled = bool(_t.get("updown_late_stop_enabled", True))
        _lse = _t.get("updown_pct_stop_earliest_mins") or {"15m": 9.0, "1h": 45.0}
        self._late_stop_earliest_mins = {str(k): float(v) for k, v in _lse.items()}
        _lsd = _t.get("updown_late_stop_deep_pct") or {"15m": 0.35, "1h": 0.45}
        self._late_stop_deep_pct = {str(k): float(v) for k, v in _lsd.items()}
        self._WINDOW_TOTAL_MINS = {"5m": 5.0, "15m": 15.0, "30m": 30.0, "1h": 60.0}

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

    def _window_keeps_early_stop(self, pos) -> bool:
        """Whether this position's window still fires the early hold-lane cuts
        (favorite_hard_stop + independent -catastrophic backstop). Default: 1h
        only — 5m/15m ride to resolution because their mid-window drawdown is
        recoverable noise (see reload_from_config note). "all" => every window;
        empty set => none. Robust to a missing window_size via inference."""
        ws = getattr(self, "_hold_early_stop_windows", None)
        if ws is None or "all" in ws:
            return True
        label = infer_updown_window_size(
            getattr(pos, "window_size", "") or "",
            opened_at=getattr(pos, "opened_at", None),
            end_date=getattr(pos, "end_date", None),
        )
        return str(label).lower() in ws

    def _hold_fixed_tp_matches(self, pos, entry_signal: Dict[str, Any]) -> bool:
        if not self._hold_fixed_tp_lanes:
            return False
        window = str(
            getattr(pos, "window_size", "")
            or entry_signal.get("window_size")
            or ""
        ).strip()
        side = "down" if str(getattr(pos, "entry_leg", "") or "") == "NO" else "up"
        candidates = [
            str(entry_signal.get("lane_id") or "").strip(),
            f"{getattr(pos, 'strategy', '')}|{window}|{side}",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            for prefix in self._hold_fixed_tp_lanes:
                if candidate == prefix or candidate.startswith(prefix):
                    return True
        return False

    def _hold_never_green_pct_for(self, pos) -> float:
        """Never-green cut trigger for THIS position's window. Global default unless the
        window has an explicit override (hold_never_green_stop_pct_by_window).

        2026-08-18 (loop fix-order #3). Same shape and same discipline as _tpgb_retrace_for:
        per-window, evidence-gated, fallback to the global knob. The 5m override exists
        because the global -34% trigger DELIVERED -45% avg (-39..-82%) through 5m gaps,
        outside the winning-era -27..-42 band; 15m/1h keep the global value until they
        have their own delivered-depth evidence."""
        base = float(getattr(self, "_hold_never_green_stop_pct", 0.0) or 0.0)
        by_win = getattr(self, "_hold_never_green_by_window", None)
        if not by_win:
            return base
        try:
            label = str(infer_updown_window_size(
                getattr(pos, "window_size", "") or "",
                opened_at=getattr(pos, "opened_at", None),
                end_date=getattr(pos, "end_date", None),
            )).lower()
            val = by_win.get(label)
            return float(val) if val is not None else base
        except Exception:
            return base

    def _tpgb_retrace_for(self, pos) -> float:
        """Give-back retrace for THIS position's window. Global default unless the window
        has an explicit override.

        2026-08-12 (Codex NO-GO on a GLOBAL 0.20->0.10; this is the per-lane form it
        sanctioned instead). Measured session test_20260812_154419: every winner exited
        take_profit_giveback surrendering 24-42 points of peak, and the 1h lanes ran
        furthest and bled most (bnb 1h peak +64.3% -> banked +40.5%; xrp 1h +61.9% ->
        +20.2%; sol 1h +38.6% -> +12.5%). That matches the DURABLE prior finding
        "1h give-back = time-gated TP is the fix" — so 1h is the lane with standing
        evidence, not a 5-trade hunch.

        5m/15m deliberately KEEP 0.20: Codex's runner-chop objection stands there — at
        0.42-0.47 entries a 10-point retrace is only 4-5 cents of token price, plausibly
        inside normal intra-window noise, and I have no tick path proving those winners
        did not wobble that much while climbing. Do not tighten a window without its own
        evidence.

        NOTE this does NOT address the gap-through: fills land 3-10 cents BELOW the
        trigger (xrp trigger 0.596 -> filled 0.500), which may dominate the economics.
        That is a separate defect and the reason this change is expected to help, not fix.
        """
        base = float(getattr(self, "_tp_giveback_retrace_pct", 0.20) or 0.20)
        by_win = getattr(self, "_tp_giveback_retrace_by_window", None)
        if not by_win:
            return base
        try:
            label = str(infer_updown_window_size(
                getattr(pos, "window_size", "") or "",
                opened_at=getattr(pos, "opened_at", None),
                end_date=getattr(pos, "end_date", None),
            )).lower()
            val = by_win.get(label)
            return float(val) if val is not None else base
        except Exception:
            return base

    def _hold_ever_green(self, pos, peak_pnl_pct: float) -> bool:
        """True if this position ever went green past the MFE arm — i.e. a recoverable
        winner that dipped, not a wrong-side entry. Ghost-confirmed discriminator: 88% of
        NEVER-green catastrophic-stopped trades resolve to $0; ever-green dippers recover."""
        arm = float(getattr(self, "_hold_never_green_mfe_arm", 0.08) or 0.0)
        return float(peak_pnl_pct or 0.0) >= arm

    def _hold_stop_pct_for(self, pos, base_cat_pct: float, peak_pnl_pct: float):
        """Loss fraction at which to cut this hold position, or None to ride. MFE-conditional
        (2026-08-09, ghost-confirmed — SUPERSEDES the window axis):
          * never went green -> cut SHALLOW at hold_never_green_stop_pct (~June -34%);
          * went green then dipped -> deep -base_cat backstop only (ride otherwise).
        Falls back to the legacy window axis when hold_mfe_conditional_stop_enabled is off."""
        base = float(base_cat_pct or 0.0)
        if not getattr(self, "_mfe_cond_stop_enabled", True):
            # legacy window-conditional behavior
            return base if (base > 0.0 and self._window_keeps_early_stop(pos)) else None
        # The never-green shallow cut applies ONLY to non-favorite entries. A FAVORITE
        # (entry >= _fav_derisk_min_entry) wins by settling to $1 WITHOUT ever going green —
        # it routinely dips to 0.60-0.70 mid-window and still resolves 1.0 (settings.yaml
        # favorite hard_stop_price note: a mark-stop there cut 3 winners for -$72). So
        # "never green" is NOT a wrong-side signal for favorites; they keep the deep -cat
        # backstop + presettle de-risk (their proven config). The ghost-confirmed never-green
        # population was entirely mid/low-priced entries (0.27-0.61), zero favorites.
        _fav_min = float(getattr(self, "_fav_derisk_min_entry", 0.82) or 0.82)
        _is_fav = float(getattr(pos, "entry_price", 0.0) or 0.0) >= _fav_min
        if not _is_fav and not self._hold_ever_green(pos, peak_pnl_pct):
            ng = float(self._hold_never_green_pct_for(pos) or 0.0)
            if ng > 0.0:
                # shallow cut; never deeper than the configured catastrophic backstop
                return min(ng, base) if base > 0.0 else ng
            return base if base > 0.0 else None
        # 2026-08-11 REGRESSION FIX: an EVER-GREEN non-favorite must ride exactly as the
        # window axis did BEFORE MFE-cond (5m/15m ride to resolution; only 1h keeps the
        # -cat backstop). Returning a flat `base` here (the original MFE-cond behavior)
        # ADDED a -55% catastrophic cut to ever-green 5m/15m positions that used to ride
        # free — and those recover to a WIN 99% of the time (peak 8-30% held, n=296). That
        # flat cut chopped the fresh-session eth-5m / xrp-15m trades (peak +15% -> cut -57%)
        # right before they'd have recovered. The never-green shallow cut above is the ONLY
        # intended change vs the window axis; ever-green defers to the window axis unchanged.
        # 2026-08-17 SUPERSEDED on the give-back windows: live post-restore riders went 0/5
        # (-$49.76, every one a -103% forfeit) — see __init__ giveback block. NOT a flat cut:
        # the floor is peak-relative (peak +31% -> exit -9%), so a still-climbing runner or a
        # shallow dip is untouched; only a full round-trip through `giveback_points` exits.
        if _is_fav:
            return base if base > 0.0 else None
        if getattr(self, "_hold_evergreen_giveback_enabled", False):
            _gb_label = str(infer_updown_window_size(
                getattr(pos, "window_size", "") or "",
                opened_at=getattr(pos, "opened_at", None),
                end_date=getattr(pos, "end_date", None),
            )).lower()
            if _gb_label in getattr(self, "_hold_evergreen_giveback_windows", set()):
                _gb = self._hold_evergreen_giveback_points - float(peak_pnl_pct or 0.0)
                # 2026-08-18: cap follows the WINDOW's never-green depth (per-window map),
                # so 5m's shallower max-cut applies to the floor too — one "deepest cut
                # for this window" semantic across both never-green and give-back paths.
                _gb_cap = float(self._hold_never_green_pct_for(pos) or 0.34)
                if base > 0.0:
                    _gb_cap = min(_gb_cap, base)
                _gb = max(0.0, min(_gb, _gb_cap))
                setattr(pos, "_hold_giveback_floor", _gb)
                return _gb
        return base if (base > 0.0 and self._window_keeps_early_stop(pos)) else None

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

    def _mark_plausible_band(self, entry_price: float) -> float:
        """Max |mark - entry| still treated as a REAL price (vs inverted token ordering).

        2026-08-08 CAP-UNREACHABLE FIX (autowake: doge 15m BUY_NO -$44.46 bust, mkt 3398369).
        The band was a flat 0.50, which is BELOW where the catastrophic loss cap fires on a
        favorite. Cap trigger mark = entry*(1-cat); blind edge = entry-0.50. At cat=0.55:

            entry 0.85 -> cap 0.3825 vs blind 0.3500   (3.3c of visible headroom)
            entry 0.88 -> cap 0.3960 vs blind 0.3800   (1.6c  <- the bust)
            entry 0.90 -> cap 0.4050 vs blind 0.4000   (0.5c)
            entry>=0.909 -> cap BELOW blind edge       (cap can NEVER fire in-band)

        So for the whole favorite_lane price range (floor 0.85 .. price_max 0.93) the -55%
        cap was either unreachable or reachable only inside a 1-2 cent slice. A collapsing
        favorite steps straight from "cap not triggered" to "invisible to every exit", and
        the cap only fires later, after the out-of-band confirm ticks, at whatever worse
        price the tape has reached. Measured on the bust: exits evaluated every ~3.5s, last
        in-band mark 0.81 (-8%), cap trigger 0.396 never observed, next evaluated mark 0.35
        (-60.2%) one confirm cycle later. That is the cap MISSING, not the tape gapping.

        Fix: give the cap real headroom below its trigger (entry*cat + 0.10), while keeping
        the band strictly INSIDE the inverted-ordering signature. Inversion reads the other
        leg, i.e. |mark - entry| == |1 - 2*entry| exactly; staying 0.05 under that keeps
        every inversion the old 0.50 band caught still caught (verified: with cat=0.55 the
        band only rises above 0.50 for entry > ~0.775, and inversion delta there is already
        > 0.55). Lower entries keep the original flat 0.50 — no change outside favorites.

        NOTE this returns the OUTER (visibility) edge only. Marks between 0.50 and this band
        are still confirm-gated in _mark_delta_blind_skip; 0.50 stays the evaluate-immediately
        line. So widening buys VISIBILITY for the cap, not a faster trigger on a junk print.
        """
        cat = float(getattr(self, "_hold_catastrophic_stop_pct", 0.0) or 0.0)
        if cat <= 0.0 or entry_price <= 0.0:
            return 0.50
        # Headroom the cap needs to be observable, capped just inside the inversion delta.
        want = entry_price * cat + 0.10
        inversion_guard = abs(1.0 - 2.0 * entry_price) - 0.05
        return max(0.50, min(want, inversion_guard))

    def _mark_delta_blind_skip(self, pos, mark: float, leg_label: str) -> bool:
        """Implausible-mark guard that can no longer go BLIND on a real collapse.

        2026-08-08 GAP-THROUGH BLINDNESS FIX (autowake: doge 1h BUY_NO -$32.00 bust).
        The old guard was a bare `continue` whenever |mark - entry| > the plausible band
        (see _mark_plausible_band; was a flat 0.50). It exists to
        catch INVERTED TOKEN ORDERING from the scanner, but `continue` skips the ENTIRE
        exit evaluation for that tick — loss cap (hold_catastrophic_stop_pct), favorite
        pre-settlement de-risk AND `updown_expired` included. Consequence: a favorite
        entered at 0.87 goes INVISIBLE to every exit the moment its mark drops below 0.37
        — which is exactly the settlement gap-through those caps were built to catch.
        Measured on the doge 6AM 2026-08-08 window: our NO mark left the band at 10:56:52,
        8 SECONDS before the 180s presettle window opened, so no de-risk and no cap could
        ever evaluate; MAE froze at -51.15% (the last in-band tick, hence just under the
        -55% cap) and expiry finally fired 796s LATE at a 0.50 mark. The bnb -$32.39 and
        sol -$70.81 "rode to resolution" losses have the same signature — they were
        misdiagnosed as threshold problems and nudged, when the exits were simply blind.

        Discriminator: inverted ordering is wrong from the FIRST tick and STAYS wrong; a
        real collapse WALKS DOWN through the plausible band first. So once a position has
        been marked in-band at least once, the feed is provably not inverted for it and a
        later out-of-band mark is REAL — evaluate exits instead of skipping. Never having
        seen an in-band mark keeps the original skip (fails closed, unchanged behavior).

        Returns True to skip this position's exit check (caller `continue`s).
        """
        entry_price = float(getattr(pos, "entry_price", 0.0) or 0.0)
        _delta = abs(mark - entry_price)
        band = self._mark_plausible_band(entry_price)
        # Inside the ORIGINAL flat 0.50: unambiguously a real price. Evaluate immediately —
        # this is where a favorite's cap trigger now lives (entry 0.88 -> trigger delta 0.484),
        # so the cap fires on the FIRST tick that reaches it, with no confirm delay.
        if _delta <= 0.50:
            setattr(pos, "_mark_ever_in_band", True)
            if getattr(pos, "_mark_out_of_band_ticks", 0):
                setattr(pos, "_mark_out_of_band_ticks", 0)
            return False

        # Between 0.50 and the widened band: real-but-deep. The position stays VISIBLE (that
        # is the headroom the cap needs), but a single spurious mark must not cut a winner
        # here, so it goes through the same N-tick confirm as a true out-of-band mark
        # (~7s at exit_check_interval_sec=3, vs the ~56s scan-cycle blindness it replaces).
        # Only a delta beyond the widened band still carries the inverted-ordering signature.
        _inversion_suspect = _delta > band

        # Past-expiry computed FIRST: an EXPIRED position must ALWAYS be evaluated for
        # close, regardless of in-band history — else a never-in-band position (incl. one
        # reloaded after a restart, which loses the in-memory _mark_ever_in_band flag)
        # would ride past expiry forever. (Codex fix.)
        _past_expiry = False
        try:
            _end = getattr(pos, "end_date", None)
            if _end is not None:
                if _end.tzinfo is None:
                    _end = _end.replace(tzinfo=timezone.utc)
                _past_expiry = (_end - datetime.now(timezone.utc)).total_seconds() < 0
        except Exception:
            _past_expiry = False

        if (
            _inversion_suspect
            and not getattr(pos, "_mark_ever_in_band", False)
            and not _past_expiry
        ):
            # Never once marked in band AND not expired => the inverted-token-ordering
            # signature this guard was built for. Keep skipping (original fail-closed).
            logger.debug(
                f"Skip exit check {pos.market_id}: {leg_label} price delta implausible "
                f"({entry_price:.3f} → {mark:.3f}); likely inverted token ordering in scanner"
            )
            return True

        # Provably-real collapse (ever-in-band) OR expired-must-close. Require N consecutive
        # out-of-band ticks (the same confirm count the % stop uses) so ONE junk print can't
        # cut a winner. Past expiry there is nothing left to confirm — close now.
        _n = int(getattr(pos, "_mark_out_of_band_ticks", 0)) + 1
        setattr(pos, "_mark_out_of_band_ticks", _n)
        _need = max(1, int(getattr(self, "_updown_stop_confirm_ticks", 2) or 2))
        if not _past_expiry and _n < _need:
            logger.debug(
                "Out-of-band mark pending confirm %d/%d for %s (%s %.3f → %.3f)",
                _n, _need, pos.market_id, leg_label, entry_price, mark,
            )
            return True

        logger.warning(
            "MARK COLLAPSE (real, not inverted ordering): %s %s entry=%.3f mark=%.3f "
            "delta=%.3f confirm=%d/%d past_expiry=%s — EVALUATING exits (this tick used "
            "to be skipped blind, which is what let the caps miss)",
            pos.market_id, leg_label, entry_price, mark,
            abs(mark - entry_price), _n, _need, _past_expiry,
        )
        return False

    def _resolve_updown_exit_params(self, strategy_name: str) -> Tuple[float, float, float, float]:
        """Return per-strategy updown exit params with global defaults as fallback."""
        return resolve_updown_exit_params(self._ude, strategy_name)

    def _tu_shadow_write(self, pos_id, pos, held_min, pnl_pct, mfe_pct, mae_pct):
        """Append a time-underwater shadow row (LOG-ONLY; never affects an exit).

        Records the position state at the moment a hypothetical 1h-underwater cut WOULD fire.
        An offline join to the trade's final EXIT (in entries.jsonl) then measures whether a real
        cut would have saved a loser or false-cut a slow take_profit_late winner, before we enable it.
        """
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "trade_id": getattr(pos, "trade_id", pos_id),
            "market_id": getattr(pos, "market_id", None),
            "strategy": getattr(pos, "strategy", None),
            "action": getattr(pos, "action", None),
            "window_size": "1h",
            "entry_price": getattr(pos, "entry_price", None),
            "size": getattr(pos, "size", None),
            "held_min_at_shadow": round(held_min, 1),
            "pnl_pct_at_shadow": round(pnl_pct, 4),
            "mfe_pct_at_shadow": round(mfe_pct, 4),
            "mae_pct_at_shadow": round(mae_pct, 4),
        }
        p = (
            Path(__file__).resolve().parent.parent.parent
            / "data" / "calibration" / "time_underwater_shadow.jsonl"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        logger.info(
            f"TIME-UNDERWATER SHADOW: {rec['trade_id']} 1h held={held_min:.0f}m "
            f"pnl={pnl_pct:+.1%} mfe={mfe_pct:+.1%} (would-cut; log-only, no behavior change)"
        )

    def _tape_stop_shadow_write(self, pos_id, pos, window, side, tape_dir, tape_conf,
                                held_min, pnl_pct, peak_pnl_pct, mae_pct):
        """Append a tape-aware-stop shadow row (LOG-ONLY; never affects an exit).

        Records position state at the first tick a LOSING position has the tape turned against
        its side. An offline join to the trade's final outcome (entries.jsonl EXIT) measures
        would-SAVE (final loss worse than here) vs FALSE-CUT (recovered to a win) by window,
        so the tape-stop is proven per-window before any live cut (tape is only 53% @15m).
        """
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "trade_id": getattr(pos, "trade_id", pos_id),
            "market_id": getattr(pos, "market_id", None),
            "strategy": getattr(pos, "strategy", None),
            "action": getattr(pos, "action", None),
            "window_size": window,
            "side": side,                       # LONG/YES or SHORT/NO
            "tape_dir": tape_dir,
            "tape_conf": round(float(tape_conf), 3),
            "entry_price": getattr(pos, "entry_price", None),
            "size": getattr(pos, "size", None),
            "held_min_at_shadow": round(held_min, 1),
            "pnl_pct_at_shadow": round(pnl_pct, 4),      # would-cut here (live mark) <-- the counterfactual exit
            "mfe_pct_at_shadow": round(peak_pnl_pct, 4),
            "mae_pct_at_shadow": round(mae_pct, 4),
        }
        p = (
            Path(__file__).resolve().parent.parent.parent
            / "data" / "calibration" / "tape_stop_shadow.jsonl"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        logger.info(
            f"TAPE-STOP SHADOW: {rec['trade_id']} {window} {side} tape={tape_dir}"
            f"(c{tape_conf:.2f}) pnl={pnl_pct:+.1%} held={held_min:.0f}m "
            f"(would-cut; log-only, no behavior change)"
        )

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
            # 2026-08-11 (Codex NO-GO fix): _hold_policy_applied is sticky — set only in a
            # few hold branches and never reset — so a later exit (updown_expired,
            # take_profit_*, etc.) could log a STALE tag from a prior suppressed tick. Clear
            # it at the top of every per-position evaluation so the tag on each closed row
            # reflects THIS exit only. (The never-green measurement was already safe via the
            # reason=="hold_catastrophic_stop" co-filter, but this keeps ALL rows honest.)
            if getattr(pos, "_hold_policy_applied", None) is not None:
                setattr(pos, "_hold_policy_applied", None)
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
                if self._mark_delta_blind_skip(pos, current_no_price, "NO"):
                    continue
                unrealized_pnl = pos.size * (current_no_price - pos.entry_price)
                cost_basis = pos.entry_price * pos.size
            elif pos.outcome == "NO":
                # Short YES: lent/sold YES; mark in YES space.
                if self._mark_delta_blind_skip(pos, current_yes_price, "short-YES"):
                    continue
                unrealized_pnl = pos.size * (pos.entry_price - current_yes_price)
                cost_basis = (1.0 - pos.entry_price) * pos.size
            else:
                if self._mark_delta_blind_skip(pos, current_yes_price, "YES"):
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
            # 2026-08-11 (Codex NO-GO fix for tp_giveback): peak_token_price is a running
            # max updated from a SINGLE accepted mark — one junk high mark inside the
            # plausible band permanently corrupts it, and a fire-confirm can't help because
            # the corrupted peak stays high every subsequent tick. So the give-back trail
            # arms/fires off a CONFIRMED peak = the SECOND-highest token mark seen (top1 =
            # highest, top2 = highest from a *different* tick). A one-tick spike becomes
            # top1 only; top2 stays the real sustained peak. A genuine walk-down (the BNB
            # +108%/+72% give-backs) climbs through many marks so top2 ~= top1 and the trail
            # fires correctly. Confirmed-peak is used ONLY by the give-back trail; the raw
            # peak_pnl_pct still drives MFE telemetry + the never-green discriminator.
            _tpgb_t1 = float(getattr(pos, "_tpgb_top1_token", 0.0) or 0.0)
            _tpgb_t2 = float(getattr(pos, "_tpgb_top2_token", 0.0) or 0.0)
            if current_token_price > _tpgb_t1:
                _tpgb_t2 = _tpgb_t1  # old top1 was seen on a prior tick => now confirmed as top2
                _tpgb_t1 = current_token_price
                setattr(pos, "_tpgb_top1_token", _tpgb_t1)
                setattr(pos, "_tpgb_top2_token", _tpgb_t2)
            elif current_token_price > _tpgb_t2:
                _tpgb_t2 = current_token_price
                setattr(pos, "_tpgb_top2_token", _tpgb_t2)
            tpgb_confirmed_peak_pct = (
                (_tpgb_t2 - pos.entry_price) / pos.entry_price
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

            # Regular-TP executable-net guard (default OFF via _tp_require_executable_net):
            # the pnl we'd REALIZE if we took profit right now — exit-side price minus the
            # round-trip taker fee — so the take_profit branch can require it be net-green
            # instead of firing on the midpoint mark. Independent of the stop's
            # executable-price flag. None => no book snapshot (TP branch fails open to the
            # midpoint gate). Same leg logic + fee model as the stop mark and realized-fill
            # fee block. cost_basis>0 guarded (division).
            _tp_exec_price: Optional[float] = None
            _tp_exec_gross_pnl_pct: Optional[float] = None
            _tp_exec_net_pnl_pct: Optional[float] = None
            if self._tp_require_executable_net and cost_basis > 0:
                _tpliq = (market_liquidity or {}).get(pos.market_id) or {}
                _tp_bid = _tpliq.get("best_bid")
                _tp_ask = _tpliq.get("best_ask")
                if _tp_bid is not None and _tp_ask is not None:
                    if entry_leg == "NO":
                        # Long NO: sell NO -> NO bid = 1 - YES ask
                        _tp_exec_price = 1.0 - float(_tp_ask)
                        _tp_gross = pos.size * (_tp_exec_price - pos.entry_price)
                    elif pos.outcome == "NO":
                        # Short YES: buy back YES -> pay YES ask
                        _tp_exec_price = float(_tp_ask)
                        _tp_gross = pos.size * (pos.entry_price - _tp_exec_price)
                    else:
                        # Long YES: sell YES -> YES bid
                        _tp_exec_price = float(_tp_bid)
                        _tp_gross = pos.size * (_tp_exec_price - pos.entry_price)
                    _tp_exec_gross_pnl_pct = _tp_gross / cost_basis
                    _tp_fee = 0.0
                    if (
                        self._execution_fees_enabled
                        and str(getattr(pos, "strategy", "") or "")
                        in CRYPTO_UPDOWN_STRATEGIES
                    ):
                        _tp_rate = _tpliq.get("taker_fee_rate")
                        if _tp_rate is None:
                            _tp_rate = self._crypto_updown_15m_taker_fee_rate
                        _tp_rate = float(_tp_rate or 0.0)
                        _tp_cfg_rate = float(self._crypto_updown_15m_taker_fee_rate or 0.0)
                        if _tp_cfg_rate > 0:
                            _tp_rate = min(_tp_rate, _tp_cfg_rate)
                        _tp_fee = polymarket_taker_fee_usdc(
                            pos.size, _tp_exec_price, _tp_rate
                        )
                        if self._entry_taker:
                            _tp_fee += polymarket_taker_fee_usdc(
                                pos.size, pos.entry_price, _tp_rate
                            )
                    _tp_exec_net_pnl_pct = (_tp_gross - _tp_fee) / cost_basis

            # Wide-book winner guard (2026-07-21): only fire the executable-price stop
            # on a book wider than exit_max_book_spread if the YES MIDPOINT also
            # confirms the loss. Prevents a thin low bid from cutting a genuine winner
            # once wide_book_stop_through lets wide-book ticks reach the stop. False on
            # tight books => the stop condition below is byte-identical to legacy.
            _wide_book_stop_needs_mid = False
            if (
                self._stop_dual_confirm_midpoint
                and self._stop_use_executable_price
                and exec_exit_price is not None
            ):
                _liqw = (market_liquidity or {}).get(pos.market_id) or {}
                _bbw = _liqw.get("best_bid")
                _baw = _liqw.get("best_ask")
                if (
                    _bbw is not None
                    and _baw is not None
                    and (float(_baw) - float(_bbw)) > self._exit_max_book_spread
                ):
                    _wide_book_stop_needs_mid = True

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

                # 2026-08-03 P0 LATE-ONLY STOP gate (see __init__). On 15m/1h, suppress the early
                # % stop while > (window_total - earliest_mins) minutes remain — i.e. until we are
                # `earliest_mins` into the window. A DEEP collapse (>= deep_pct) on a FRESH ws mark
                # still cuts; TP / take_profit_late / flatten-pre-resolution / the -50% catastrophic
                # backstop are all unaffected. Hold lanes (stop_pct==0) never reach the stop branch,
                # so they are untouched. 5m has no entry in the maps -> never suppressed.
                # Codex review 2026-08-03 (P0 fix): NEVER suppress a hold-to-resolution lane. Those
                # lanes manage loss via their own loser-floor + the -50% catastrophic backstop, both
                # of which fire through the reason=="updown_stop_loss" path — suppressing it would let
                # a hold lane ride past its floor/catastrophic (esp. on a stale mark). The late-only
                # gate targets the NORMAL %-stop directional lanes (the 36-trade/-92 leak), not holds.
                _late_stop_suppressed = False
                if (
                    self._late_stop_enabled
                    and _flat_mins_remaining is not None
                    and not resolved.updown_hold_winners_to_resolution
                ):
                    _ls_win = infer_updown_window_size(
                        getattr(pos, "window_size", "") or "",
                        opened_at=getattr(pos, "opened_at", None),
                        end_date=getattr(pos, "end_date", None),
                    )
                    _ls_earliest = self._late_stop_earliest_mins.get(_ls_win)
                    _ls_total = self._WINDOW_TOTAL_MINS.get(_ls_win)
                    if _ls_earliest is not None and _ls_total is not None:
                        if _flat_mins_remaining > (_ls_total - _ls_earliest):
                            _ls_liq = (market_liquidity or {}).get(pos.market_id) or {}
                            _ls_age = _ls_liq.get("mark_age_ms")
                            _ls_fresh = (
                                _ls_liq.get("mark_src") == "ws"
                                and isinstance(_ls_age, (int, float))
                                and float(_ls_age) <= 2000.0
                            )
                            _ls_deep = self._late_stop_deep_pct.get(_ls_win, 0.35)
                            if not (stop_pnl_pct <= -_ls_deep and _ls_fresh):
                                _late_stop_suppressed = True

                # 2026-08-04 TAPE-CONDITIONED STOP DEFERRAL (per-lane LONG; see __init__). Suppress
                # the %-stop ONLY when: (a) LONG/BUY_YES lane with a by_lane entry, (b) loss still
                # SHALLOW (pnl_pct above -floor_pct — the loser floor), (c) this asset's tape reads
                # UP, HTF-confirmed (dscore>=2 AND 1h-MACD up), at conf>=conf_min and fresh. In a
                # DOWN/FLAT tape, or once loss breaches the floor, this stays False and the stop
                # fires as normal. Deferring drops to the else-branch (time-stop near expiry still
                # runs) — the position keeps being managed, it just isn't %-stopped mid-up-tape.
                # Codex 2026-08-04: strict LONG = BUY_YES leg on a YES outcome (entry_leg!="NO"
                # with outcome=="NO" is the short-YES representation — must NOT be deferred). Floor
                # is gated on the WORSE of midpoint/executable pnl so stop_use_executable_price can't
                # slip a -16% executable loss past a -15% floor. Whole parse fails closed (=False).
                # 2026-08-05 GENERALIZED to the SHORT side (BUY_NO). The LONG path (entry_leg==YES,
                # outcome==YES) is UNCHANGED — same eth_macro:BUY_YES cfg, UP tape, dscore>=2, 1h-MACD
                # up (>=require_1h_macd). The MIRROR: a strict BUY_NO short (entry_leg==NO, outcome==NO)
                # with a {strategy}:BUY_NO by_lane cfg is deferred while the loss is SHALLOW AND this
                # asset's tape reads DOWN, HTF-confirmed (dscore<=-2 AND 1h-MACD down <=-require_1h_macd)
                # at conf>=conf_min and fresh. This is the "shallow bounce in a downtrend" analogue of
                # the long's "shallow dip in an uptrend" — the stopped shorts that would have WON if
                # held (xrp 1h/eth 15m BUY_NO, holdΔ +$33 / last-6-sess). In an UP/FLAT tape, once the
                # 1h MACD isn't down, or once the loss breaches floor_pct, this stays False and the stop
                # fires as normal (self-flips). Side-isolated: ONLY lanes explicitly in by_lane are
                # touched; the short-YES representation (entry_leg==YES, outcome==NO) is NOT a canonical
                # BUY_NO and is skipped (fails closed). Whole parse fails closed (=no suppression).
                _tape_hold_suppressed = False
                _th_side = None
                if entry_leg == "YES" and pos.outcome == "YES":
                    _th_side = "BUY_YES"
                elif entry_leg == "NO" and pos.outcome == "NO":
                    _th_side = "BUY_NO"
                if (
                    self._tape_hold_enabled
                    and _latest_tape_state is not None
                    and _th_side is not None
                ):
                    _th_cfg = self._tape_hold_by_lane.get(f"{strategy_name}:{_th_side}")
                    _th_floor_pnl_pct = min(pnl_pct, stop_pnl_pct)
                    if _th_cfg is not None and _th_floor_pnl_pct > -_th_cfg["floor_pct"]:
                        try:
                            _th_tm = _latest_tape_state(strategy_name) or {}
                            _th_dir = str(_th_tm.get("direction") or "").upper()
                            _th_conf = float(_th_tm.get("confidence", 0.0) or 0.0)
                            _th_dscore = int(_th_tm.get("dscore", 0) or 0)
                            _th_signs = _th_tm.get("macd_signs") or [0, 0, 0]
                            _th_m1h = int(_th_signs[2]) if len(_th_signs) >= 3 else 0
                            _th_age = (
                                datetime.now(timezone.utc).timestamp()
                                - float(_th_tm.get("ts", 0.0) or 0.0)
                            )
                        except Exception:
                            _th_dir, _th_conf, _th_dscore, _th_m1h, _th_age = "", 0.0, 0, 0, 1e9
                        _req_macd = int(_th_cfg["require_1h_macd"])
                        if _th_side == "BUY_YES":
                            _th_agree = (
                                _th_dir == "UP" and _th_dscore >= 2 and _th_m1h >= _req_macd
                            )
                        else:  # BUY_NO short mirror: DOWN tape, negative dscore, 1h-MACD down
                            _th_agree = (
                                _th_dir == "DOWN" and _th_dscore <= -2 and _th_m1h <= -_req_macd
                            )
                        if (
                            _th_agree
                            and _th_conf >= _th_cfg["conf_min"]
                            and _th_age <= _th_cfg["max_age_s"]
                        ):
                            _tape_hold_suppressed = True
                            logging.info(
                                "TAPE-HOLD deferred stop on %s (%s %s): pnl=%.1f%% "
                                "stop_pnl=%.1f%% tape=%s conf=%.2f dscore=%d m1h=%d age=%.0fs floor=%.0f%%",
                                pos.market_id, strategy_name, _th_side, pnl_pct * 100.0,
                                stop_pnl_pct * 100.0, _th_dir, _th_conf, _th_dscore, _th_m1h, _th_age,
                                _th_cfg["floor_pct"] * 100.0,
                            )

                # Regular-TP net gate: the midpoint mark says take-profit, but when the
                # executable-net guard is on, only fire if the pnl we'd actually realize
                # (exit-side price minus round-trip taker fee) clears take_profit_net_min_pct.
                # Fails OPEN when the guard is off or no book snapshot exists (legacy). A
                # suppressed TP logs mark-vs-executable so a hold-instead-of-TP is visible.
                # take_profit_late is intentionally NOT gated (it performs well).
                _tp_mark_ready = (
                    not resolved.updown_hold_winners_to_resolution
                    and pnl_pct >= resolved.take_profit_pct
                )
                _hold_fixed_tp_ready = (
                    resolved.updown_hold_winners_to_resolution
                    and self._hold_fixed_tp_enabled
                    and pnl_pct + 1e-9 >= self._hold_fixed_tp_pct
                    and self._hold_fixed_tp_matches(pos, entry_signal)
                )
                _tp_net_ok = True
                if (
                    (_tp_mark_ready or _hold_fixed_tp_ready)
                    and self._tp_require_executable_net
                    and _tp_exec_net_pnl_pct is not None
                ):
                    _tp_net_ok = _tp_exec_net_pnl_pct >= self._tp_net_min_pct
                    if not _tp_net_ok:
                        logging.info(
                            "TP NET-GUARD suppressed take_profit on %s (%s %s): "
                            "trigger_mark_pnl=%.1f%% executable_net=%.1f%% < min=%.1f%% "
                            "exec_price=%.3f — holding instead of TP",
                            pos.market_id,
                            strategy_name,
                            getattr(pos, "window_size", ""),
                            pnl_pct * 100.0,
                            _tp_exec_net_pnl_pct * 100.0,
                            self._tp_net_min_pct * 100.0,
                            _tp_exec_price if _tp_exec_price is not None else -1.0,
                        )

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
                    # 2026-08-06 GIVE-BACK TRAILING TP — fires UNDER hold (not gated by hold_winners) so a
                    # winner that peaked then REVERSED is banked instead of round-tripping to catastrophic.
                    # Arms only once the position was clearly green (peak MFE >= arm), then triggers on a
                    # real retrace from the peak (>= retrace). A still-climbing runner never retraces, so
                    # it is NOT cut (the failure mode of the old fixed-level trails). arm 0.30 + retrace
                    # 0.20 => banks at >= +10% (still green). NOT gated by the executable-net guard (a
                    # retracing winner should exit even into a thinning book — realistic_paper_fills models
                    # the slippage).
                    # 2026-08-11: arm/fire off the CONFIRMED peak (2nd-highest mark), not the
                    # raw running-max — a single junk high mark can't create a fake peak that
                    # cuts a still-climbing runner (Codex NO-GO fix). tpgb_confirmed_peak_pct
                    # ~= peak_pnl_pct for a real sustained peak (the give-backs we target).
                    self._tp_giveback_enabled
                    and tpgb_confirmed_peak_pct >= self._tp_giveback_arm_pct
                    and pnl_pct <= (tpgb_confirmed_peak_pct - self._tpgb_retrace_for(pos))
                    # 2026-08-11 (Codex NO-GO fix #2): only BANK GREEN. If an ever-green
                    # position GAPPED straight past catastrophic (peak +30% -> -60% in one
                    # tick), pnl_pct <= peak-retrace is still true at -60% and giveback would
                    # PREEMPT hold_catastrophic_stop, mislabeling a catastrophic loss as a TP.
                    # Require the exit to still be in profit so the give-back only ever banks
                    # a reversing WINNER; a gap into the red is left to the catastrophic stop.
                    and pnl_pct > 0.0
                ):
                    reason = "take_profit_giveback"
                    # 2026-08-12 GIVE-BACK TELEMETRY. The surrendered peak is invisible in
                    # the trade row today (only the realized pct lands), so the leak had to
                    # be reconstructed by hand. Emit peak / effective retrace / THEORETICAL
                    # trigger vs the mark we actually fired at: the delta between them IS the
                    # gap-through, which Codex flagged as possibly dominating the economics.
                    try:
                        _tpgb_r = self._tpgb_retrace_for(pos)
                        _tpgb_ep = float(getattr(pos, "entry_price", 0.0) or 0.0)
                        logger.info(
                            "TPGB_EXIT market=%s peak_pct=%.3f retrace=%.3f trigger_pct=%.3f "
                            "fired_pct=%.3f surrendered_pct=%.3f trigger_px=%.4f entry_px=%.4f",
                            getattr(pos, "market_id", "?"), tpgb_confirmed_peak_pct, _tpgb_r,
                            tpgb_confirmed_peak_pct - _tpgb_r, pnl_pct,
                            tpgb_confirmed_peak_pct - pnl_pct,
                            _tpgb_ep * (1.0 + tpgb_confirmed_peak_pct - _tpgb_r), _tpgb_ep,
                        )
                        setattr(pos, "_tpgb_peak_pct", tpgb_confirmed_peak_pct)
                        setattr(pos, "_tpgb_retrace_used", _tpgb_r)
                    except Exception:
                        pass
                elif (_tp_mark_ready or _hold_fixed_tp_ready) and _tp_net_ok:
                    reason = (
                        "hold_fixed_take_profit"
                        if _hold_fixed_tp_ready
                        else "take_profit"
                    )
                elif (
                    effective_stop_loss_pct != 0
                    and stop_pnl_pct <= -effective_stop_loss_pct
                    and not _late_stop_suppressed  # 2026-08-03 P0 late-only stop gate
                    and not _tape_hold_suppressed  # 2026-08-04 tape-conditioned LONG deferral
                    and (
                        not _wide_book_stop_needs_mid
                        or pnl_pct <= -effective_stop_loss_pct
                    )
                ):
                    # Same-position percentage stop: cuts adverse drift early instead of
                    # waiting for the late-window cents stop, which fires at whatever price
                    # the position has already collapsed to. stop_pnl_pct == pnl_pct unless
                    # stop_use_executable_price is on (then it marks the exit-side bid).
                    # Require N consecutive triggering ticks (default 2) so one noisy
                    # book read can't cut a winner; reset the moment the mark recovers.
                    _confirm = int(getattr(pos, "_stop_confirm_count", 0)) + 1
                    setattr(pos, "_stop_confirm_count", _confirm)
                    # Gap-through bypass: a fresh mark already well past the stop is a
                    # real collapse (median 16pt overshoot on tight books) -- do not wait
                    # the 2nd confirm tick (~3s) and hand back another ~8-16pt. Near-stop
                    # ticks (within the margin) still require the full N-tick confirm.
                    # Codex 2026-07-24: bypass ONLY on a FRESH WS mark. A stale/junk/
                    # midpoint print far past the stop must still take the N-tick confirm
                    # (that is exactly the single-noisy-read the confirm guards). Session
                    # data: 150/154 gap-throughs were mark_src=ws, age<=1500ms.
                    _bypass_liq = (market_liquidity or {}).get(pos.market_id) or {}
                    _bypass_mark_age = _bypass_liq.get("mark_age_ms")
                    _bypass_mark_fresh = (
                        _bypass_liq.get("mark_src") == "ws"
                        and isinstance(_bypass_mark_age, (int, float))
                        and float(_bypass_mark_age) <= 2000.0
                    )
                    _gap_bypass = (
                        self._updown_stop_gap_bypass_pct > 0.0
                        and _bypass_mark_fresh
                        and stop_pnl_pct
                        <= -(effective_stop_loss_pct + self._updown_stop_gap_bypass_pct)
                    )
                    if _confirm >= self._updown_stop_confirm_ticks or _gap_bypass:
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
                        # Past expiry: DEFER to the ResolutionTracker's binary settle for the
                        # grace window (it polls every 60s; Polymarket auto-resolves via
                        # Chainlink seconds after expiry). Mark-closing here recorded fake
                        # settles at the mid (exit_price 0.49 on a binary market) and in live
                        # would give the position away instead of collecting the resolution.
                        _grace = float(getattr(self, "_updown_expiry_grace_mins", 10.0) or 10.0)
                        if -mins_remaining >= _grace:
                            # No resolution after the full grace — close at mark under an
                            # HONEST label (never counted as a resolution by graders).
                            reason = "updown_expired_mark_fallback"
                    elif mins_remaining is not None and mins_remaining <= effective_exit_window:
                        adverse = adverse_for_updown_cents_time_stop(
                            entry_leg=entry_leg,
                            outcome=pos.outcome,
                            current_yes=current_yes_price,
                            current_no=current_no_price,
                            entry_price=pos.entry_price,
                            up_stop_cents=up_stop_cents,
                        )
                        _ts_min = getattr(self, "updown_time_stop_min_loss_pct", 0.0)
                        if adverse and (_ts_min <= 0.0 or pnl_pct <= -_ts_min):
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

            # 2026-07-26 NEVER-GREEN CUT (graduated from shadow, 5m/15m only). main.py's
            # _observe_never_green (which tracks the true per-position peak via the
            # NeverGreenCut observer) passes the set of positions that stayed never-green
            # past cut_after_secs — the dominant loss driver (77% of loss $, +$91.92 saved
            # in shadow at 84% true-positive). Cut them here at the current price instead
            # of riding to the stop. Only fires when nothing higher priority (TP / stop /
            # time / bid-depth) already set a reason. 1h is deliberately EXCLUDED (main.py
            # only adds 5m/15m ids) because 1h winners develop slowly and get false-cut.
            # 2026-08-04 PER-LANE severity gate: resolve min_loss_pct for THIS lane
            # (strategy:window:side), falling back to the global seeded default (-8%).
            _ngc_side = "BUY_NO" if str(entry_leg) == "NO" else "BUY_YES"
            _ngc_win = str(getattr(pos, "window_size", "") or getattr(pos, "updown_window", "") or "")
            _ngc_key = f"{getattr(pos, 'strategy', '') or ''}:{_ngc_win}:{_ngc_side}"
            _ngc_min = self._ngc_by_lane_min_loss.get(_ngc_key, self._ngc_min_loss_default)
            if (
                reason is None
                and pos_id in getattr(self, "_never_green_cut_ids", ())
                and (_ngc_min <= 0.0 or pnl_pct <= -_ngc_min)
            ):
                reason = "never_green_cut"

            # 2026-08-01 TIME-UNDERWATER SHADOW (Codex audit; BTC 1h BUY_YES; LOG-ONLY). Never sets
            # reason -> zero behavior change. Fires once per position when a 1h long has been held
            # past the floor AND never got meaningfully green (mfe < thresh) AND is still not green.
            # Offline join to the trade's FINAL outcome (in entries.jsonl EXIT) then tells us whether
            # a real cut here would have SAVED a loser or FALSE-CUT a slow take_profit_late winner.
            if (
                getattr(self, "_tu_shadow_enabled", False)
                and entry_leg == "YES"
                and strategy_name == "bitcoin"
                and not getattr(pos, "_tu_shadowed", False)
            ):
                _tu_win = str(
                    (entry_signal or {}).get("window_size")
                    or getattr(pos, "window_size", "")
                    or ""
                )
                if _tu_win == "1h":
                    _held_min = hours_held * 60.0
                    if (
                        _held_min >= self._tu_min_held_min
                        and peak_pnl_pct < self._tu_max_mfe_pct
                        and pnl_pct <= self._tu_max_pnl_pct
                    ):
                        try:
                            self._tu_shadow_write(
                                pos_id, pos, _held_min, pnl_pct, peak_pnl_pct, mae_pct
                            )
                            # Mark shadowed only after a successful write so a transient
                            # file error retries next cycle instead of silently dropping the row.
                            setattr(pos, "_tu_shadowed", True)
                        except Exception:
                            pass

            # 2026-08-03 TAPE-AWARE FASTER-STOP SHADOW (operator GO; LOG-ONLY, mirrors _tu_shadow).
            # Fire once per position at the first tick it is (a) LOSING past min_loss AND (b) the
            # tape_map for its asset has turned AGAINST its side (long/YES vs DOWN, short/NO vs UP)
            # at conf>=min and fresh. Records the would-cut mark (live pnl_pct); an offline join to
            # the final outcome measures would-SAVE vs FALSE-CUT by window before any live cut.
            # NEVER sets `reason` -> zero behavior change. Tape is 53% @15m / 60% @60m, so the
            # per-window false-cut rate MUST be measured here before a live tape-stop is enabled.
            if (
                is_updown
                and getattr(self, "_tape_stop_shadow_enabled", False)
                and _latest_tape_state is not None
                and not getattr(pos, "_tape_stop_shadowed", False)
                and pnl_pct <= -abs(getattr(self, "_tape_stop_min_loss_pct", 0.03))
            ):
                _ts_win = str(
                    (entry_signal or {}).get("window_size")
                    or getattr(pos, "window_size", "")
                    or ""
                ).lower()
                _ts_side = "SHORT" if entry_leg == "NO" else "LONG"
                _ts_against = "UP" if entry_leg == "NO" else "DOWN"
                try:
                    _ts_tm = _latest_tape_state(strategy_name) or {}
                except Exception:
                    _ts_tm = {}
                _ts_dir = str(_ts_tm.get("direction") or "").upper()
                _ts_conf = float(_ts_tm.get("confidence", 0.0) or 0.0)
                try:
                    _ts_age = datetime.now(timezone.utc).timestamp() - float(_ts_tm.get("ts", 0.0) or 0.0)
                except Exception:
                    _ts_age = 1e9
                if (
                    _ts_dir == _ts_against
                    and _ts_conf >= getattr(self, "_tape_stop_conf_min", 0.6)
                    and _ts_age <= getattr(self, "_tape_stop_max_age_s", 90.0)
                ):
                    try:
                        self._tape_stop_shadow_write(
                            pos_id, pos, _ts_win or "?", _ts_side, _ts_dir, _ts_conf,
                            hours_held * 60.0, pnl_pct, peak_pnl_pct, mae_pct,
                        )
                        setattr(pos, "_tape_stop_shadowed", True)
                    except Exception:
                        pass

            # 2026-07-29 HOLD MEANS HOLD (operator GO + Codex proof). The hold flag only
            # blocked take_profit; stop/trail/in-profit/time/flatten/never-green kept cutting
            # hold lanes early (sol 5m down: 8.6% reach resolution, resolved +$252 / early
            # -$84). For a TRUE hold-to-resolution lane, suppress EVERY premature exit and
            # ride to resolution — leaving only expiry (updown_expired / updown_time_limit)
            # and a CATASTROPHIC drawdown. Single override covering all leak paths at once.
            if (
                is_updown
                and resolved.updown_hold_winners_to_resolution
                and getattr(self, "_hold_means_hold_enforce", True)
                and reason in (
                    "updown_stop_loss",
                    "updown_time_stop",
                    # 2026-07-30 (operator GO, Codex-found): take_profit_late is EXEMPT from
                    # hold-enforce. It is a TIME-GATED bank that fires ONLY in the final
                    # take_profit_late_gate_mins of the window (a fade near resolution, NOT a
                    # premature exit) and ONLY above take_profit_late_pct — the designed fix for
                    # 1h-up round-trip giveback on hold lanes (btc 1h up TP0.40/gate5, memory
                    # reference_1h_giveback_time_gated_tp). Leaving it in the list nulled it out
                    # and defeated the whole mechanism, so a green >=+40% in the last 5 min rode
                    # to the catastrophic stop instead of banking. Only lanes that SET
                    # take_profit_late_pct>0 (currently btc 1h up alone) are affected.
                    "updown_flatten_pre_resolution",
                    "never_green_cut",
                    # 2026-07-29 (Codex hold-fix review): bid-depth exit is disabled today
                    # but must also be suppressed on hold lanes if ever enabled.
                    "buy_yes_bid_depth_drop",
                )
            ):
                _cat = getattr(self, "_hold_catastrophic_stop_pct", 0.5) or 0.0
                # 2026-07-31 LOSER-FLOOR (operator GO; restart-class). Honor a hold lane's
                # OWN configured % stop as a loser-floor: when this stop (updown_stop_loss)
                # is the trigger and the lane has a real stop below the catastrophic backstop
                # (0 < lane_stop < _cat), let it FIRE at ~-lane_stop instead of suppressing to
                # -_cat. ONLY the lane's own % stop is floored — time/flatten/never-green/
                # bid-depth stay fully suppressed (those are premature on a hold lane). Lanes
                # with updown_stop_loss_pct==0 (e.g. sol 5m down) never set reason=
                # updown_stop_loss, so they are unchanged until a floor is configured for them.
                # Hold-for-winners intact: a green/developing position (pnl_pct > -lane_stop)
                # is never in the stop branch. WINNER-CLIP RISK is per-lane and set by the
                # CONFIGURED VALUE, not here — keep each lane's stop above its winner-MAE tail.
                _lane_stop = float(getattr(resolved, "updown_stop_loss_pct", 0.0) or 0.0)
                _eff_stop = float(effective_stop_loss_pct or 0.0)
                if (
                    getattr(self, "_hold_lane_loser_floor_enabled", False)
                    and reason == "updown_stop_loss"
                    and 0.0 < _lane_stop < _cat
                    # 2026-07-31 (Codex NO-GO fix): floor a GENUINE raw-loser stop ONLY — never a
                    # trailing/in-profit-tightened stop on a formerly-green position. reason=
                    # updown_stop_loss (set at :634) fires off the EFFECTIVE stop, which trail/
                    # in-profit logic can tighten BELOW the raw lane stop; without these guards the
                    # floor would cut a winner that ran up, armed its trail, then pulled back.
                    and _eff_stop > 0.0
                    and abs(_eff_stop - _lane_stop) < 1e-6   # no trail/in-profit tightening active
                    and pnl_pct <= -_lane_stop               # actually at/below the loser floor
                ):
                    setattr(pos, "_hold_policy_applied", "loser_floor")
                    logger.info(
                        "HOLD LOSER-FLOOR: honoring lane stop %.0f%% on %s (pnl=%.1f%%) — "
                        "cutting loser at floor instead of riding to -%.0f%% catastrophic",
                        _lane_stop * 100.0, pos.market_id, pnl_pct * 100.0, _cat * 100.0,
                    )
                    # leave reason == "updown_stop_loss": the lane's % stop fires at the floor.
                elif (
                    _cat > 0.0
                    and (_eff_cat := self._hold_stop_pct_for(pos, _cat, peak_pnl_pct)) is not None
                    and pnl_pct <= -_eff_cat
                ):
                    _shallow = _eff_cat < _cat - 1e-9
                    _gbf = getattr(pos, "_hold_giveback_floor", None)
                    _is_gb = _gbf is not None and abs(_eff_cat - _gbf) < 1e-9
                    setattr(pos, "_hold_policy_applied",
                            "evergreen_giveback_stop" if _is_gb
                            else ("never_green_stop" if _shallow else "catastrophic_stop"))
                    reason = "hold_catastrophic_stop"
                    if _is_gb:
                        logger.info(
                            "HOLD EVER-GREEN GIVE-BACK: %s peaked +%.1f%% then gave back — cutting at "
                            "-%.0f%% (pnl=%.1f%%) instead of riding to resolution",
                            pos.market_id, peak_pnl_pct * 100.0, _eff_cat * 100.0, pnl_pct * 100.0,
                        )
                    elif _shallow:
                        logger.info(
                            "HOLD NEVER-GREEN STOP: %s never went green (peak=%.1f%%) — cutting at "
                            "-%.0f%% (pnl=%.1f%%) instead of riding to -%.0f%% catastrophic",
                            pos.market_id, peak_pnl_pct * 100.0, _eff_cat * 100.0,
                            pnl_pct * 100.0, _cat * 100.0,
                        )
                else:
                    logger.info(
                        "HOLD-TO-RESOLUTION: suppressed premature '%s' on %s (pnl=%.1f%%) — riding to resolution",
                        reason, pos.market_id, pnl_pct * 100.0,
                    )
                    setattr(pos, "_hold_policy_applied", "hold_to_resolution")
                    setattr(pos, "_hold_suppressed_exit_reason", reason)
                    reason = None

            # 2026-08-07 CATASTROPHIC-STOP INDEPENDENCE (Codex NO-GO fix). Under hold_all the
            # normal % stop is zeroed (updown_stop_loss_pct->0), so `reason` never becomes
            # updown_stop_loss and the hold-enforce block above is NEVER entered — a loser rode
            # all the way to -100% past the -cat backstop (the cap was inert). Fire the
            # catastrophic cut INDEPENDENTLY on any hold-to-resolution lane whenever pnl_pct<=-cat,
            # regardless of whether a premature reason was set. The cap sits below the observed
            # winner worst-MAE (favorite winners dipped to -51%), so it cuts genuine failures, not
            # recoverable dips. This is what makes hold_catastrophic_stop_pct a REAL loss cap.
            if (
                is_updown
                and resolved.updown_hold_winners_to_resolution
                and getattr(self, "_hold_means_hold_enforce", True)
                and reason is None
            ):
                _cat_ind = float(getattr(self, "_hold_catastrophic_stop_pct", 0.0) or 0.0)
                _eff_cat_ind = self._hold_stop_pct_for(pos, _cat_ind, peak_pnl_pct)
                if _cat_ind > 0.0 and _eff_cat_ind is not None and pnl_pct <= -_eff_cat_ind:
                    _shallow = _eff_cat_ind < _cat_ind - 1e-9
                    _gbf = getattr(pos, "_hold_giveback_floor", None)
                    _is_gb = _gbf is not None and abs(_eff_cat_ind - _gbf) < 1e-9
                    setattr(pos, "_hold_policy_applied",
                            "evergreen_giveback_stop" if _is_gb
                            else ("never_green_stop" if _shallow else "catastrophic_stop"))
                    reason = "hold_catastrophic_stop"
                    logger.info(
                        "HOLD %s (independent, hold_all): %s pnl=%.1f%% <= -%.0f%% peak=%.1f%% "
                        "— cutting loser (was riding to -100%%)",
                        "EVER-GREEN GIVE-BACK" if _is_gb
                        else ("NEVER-GREEN STOP" if _shallow else "CATASTROPHIC-STOP"),
                        pos.market_id, pnl_pct * 100.0, _eff_cat_ind * 100.0, peak_pnl_pct * 100.0,
                    )

            # 2026-08-08 FAVORITE CONTINUOUS HARD-STOP (see __init__). Fires BEFORE the presettle
            # de-risk so it can cut the loser early — any tick in the hold, not just the final
            # window — the moment the our-side mark walks down to hard_stop_price. Same favorite
            # identification as the presettle block (high entry). Bypasses the min-hold floor like
            # the catastrophic/presettle cuts (a collapsing favorite must be cuttable immediately).
            if (
                is_updown
                and resolved.updown_hold_winners_to_resolution
                and reason is None
                and float(getattr(self, "_fav_hard_stop_price", 0.0) or 0.0) > 0.0
                and float(getattr(pos, "entry_price", 0.0) or 0.0) >= float(getattr(self, "_fav_derisk_min_entry", 0.82))
                and self._window_keeps_early_stop(pos)
            ):
                _hs_entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
                _hs_mark = _hs_entry * (1.0 + pnl_pct)
                if _hs_mark <= self._fav_hard_stop_price:
                    setattr(pos, "_hold_policy_applied", "favorite_hard_stop")
                    reason = "favorite_hard_stop"
                    logger.info(
                        "FAVORITE HARD-STOP: %s entry=%.2f mark=%.2f (<= %.2f) — capping loss early "
                        "(pnl=%.1f%%)",
                        pos.market_id, _hs_entry, _hs_mark, self._fav_hard_stop_price, pnl_pct * 100.0,
                    )

            # 2026-08-07 FAVORITE PRE-SETTLEMENT DE-RISK (the gap-through fix — see __init__).
            # Catches the loss a %-stop can't: a favorite priced ~0.85 for the whole window that
            # gaps straight to $0 at settlement. Fires ONLY on a favorite-priced HOLD (high entry)
            # whose our-side mark has flipped to <= presettle price in the final seconds — a winner
            # is marking toward 1.0 there, so this never cuts one.
            if (
                is_updown
                and resolved.updown_hold_winners_to_resolution
                and reason is None
                and float(getattr(self, "_fav_presettle_secs", 0.0) or 0.0) > 0.0
                and float(getattr(pos, "entry_price", 0.0) or 0.0) >= float(getattr(self, "_fav_derisk_min_entry", 0.82))
                and pos.end_date is not None
            ):
                _fd_end = pos.end_date
                if _fd_end.tzinfo is None:
                    _fd_end = _fd_end.replace(tzinfo=timezone.utc)
                _fd_secs_left = (_fd_end - datetime.now(timezone.utc)).total_seconds()
                _fd_entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
                _fd_mark = _fd_entry * (1.0 + pnl_pct)
                if 0.0 < _fd_secs_left <= self._fav_presettle_secs and _fd_mark <= self._fav_presettle_price:
                    setattr(pos, "_hold_policy_applied", "favorite_presettle_derisk")
                    reason = "favorite_presettle_derisk"
                    logger.info(
                        "FAVORITE PRE-SETTLEMENT DE-RISK: %s entry=%.2f mark=%.2f (<= %.2f) %.0fs left "
                        "— salvaging before settlement gap-to-0 (pnl=%.1f%%)",
                        pos.market_id, _fd_entry, _fd_mark, self._fav_presettle_price,
                        _fd_secs_left, pnl_pct * 100.0,
                    )

            # Phantom-exit floor: suppress the EARLY % stop/TP until the position has
            # aged past the min-hold. Leaves the late-window cents/time/expiry stops
            # untouched (they only fire near resolution). Resets the stop-confirm
            # count so it re-confirms cleanly once the hold clears.
            _min_hold_floor = self._min_hold_floor_secs(pos)
            _held_eff_secs = hours_held * 3600.0 - self._preopen_lag_secs(pos)
            # 2026-07-27 BLEED FIX: held_eff = now - (end_date - window_len) = now - window_open.
            # A resumed position carries a serialized end_date (main.py:1906, never re-validated)
            # whose derived window_open can be in the FUTURE -> held_eff goes hugely NEGATIVE
            # (~-7.8h observed) -> this floor then suppressed EVERY stop/TP/never_green_cut and the
            # loser rode to full resolution loss. A negative held_eff is definitionally a corrupt
            # anchor (a real position's time-since-window-open is >= 0), and is exactly the case
            # that MUST be allowed to cut. Require a real NON-NEGATIVE hold below the floor: fresh
            # fills in [0, floor) stay protected; a bogus-negative anchor no longer suppresses.
            if (
                is_updown
                and reason in ("take_profit", "hold_fixed_take_profit", "updown_stop_loss", "never_green_cut")
                and _min_hold_floor > 0
                and 0.0 <= _held_eff_secs < _min_hold_floor
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
                if reason in (
                    # 2026-07-27 (ride-to-zero fix): updown_stop_loss was previously gated
                    # `and exec_exit_price is not None`, so a stop firing into a thin/one-
                    # sided book (no resolvable bid — exactly the ride-to-zero case) fell
                    # through to marketable=False -> GTC resting limit -> rode 3 losing
                    # binaries to $0 on the CLOB. INVERTED: the missing-price case is the
                    # MOST urgent to cross NOW, not rest. Stop-loss now joins the other
                    # loss-cutting/near-resolution exits as UNCONDITIONALLY marketable.
                    "updown_stop_loss",
                    "never_green_cut",  # 2026-07-26: take the bid NOW (FAK) — a stuck
                    # never-green position must actually exit, not rest a limit that
                    # won't fill; mirrors the stop/time-stop marketable behavior.
                    "updown_flatten_pre_resolution",
                    "updown_expired",
                    "updown_expired_mark_fallback",  # 2026-08-19 grace-expired mark-close must cross NOW
                    "updown_time_stop",
                    # 2026-07-29 (live evidence, session 180002): a regular take_profit was
                    # placed marketable=False -> resting GTC that sat ~20s unfilled then
                    # decayed (XRP 15m trade 0x02b450, the winner's gains handed back). A TP
                    # that HIT must cross the bid NOW (FAK) like take_profit_late and the
                    # loss-cutting exits, not rest a limit that may never fill in a fast
                    # binary. Same reasoning as take_profit_late immediately below.
                    "take_profit",
                    "hold_fixed_take_profit",
                    # 2026-07-17 (Codex catch): the time-gated late TP fires INSIDE the
                    # final gate window, so it must take the bid NOW (FAK) like the other
                    # near-resolution exits. Left as a resting GTC it would not fill at the
                    # qualifying tick — which is exactly the execution gap the sim assumed
                    # away and where the previous 3 give-back attempts died.
                    "take_profit_late",
                    # 2026-08-09 (Codex HIGH on the giveback enable): the give-back trailing TP
                    # banks a REVERSING winner on a retrace from peak — it MUST cross the bid NOW
                    # (FAK) like take_profit/take_profit_late. Left non-marketable it would rest a
                    # GTC/maker limit and hand back the very gains this exit exists to protect,
                    # exactly the failure the give-back TP was built to fix.
                    "take_profit_giveback",
                    # 2026-07-29 (Codex hold-fix review): the catastrophic stop is the ONLY
                    # exit left on a hold lane — it fires on a deep drawdown and must cross
                    # NOW (FAK), not rest as a GTC limit that hands back more.
                    "hold_catastrophic_stop",
                ):
                    # Loss-cutting / near-resolution: take the bid NOW (FAK) rather than
                    # resting a limit that won't fill into a one-sided book and lets the
                    # position gap to a binary-zero resolution. exec_exit_price when
                    # available, else the current exit-side mark.
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
                # PAPER CALIB Phase 3.6: reset per position so raw_signal_pnl can never
                # inherit a prior iteration's value if the walk block is ever skipped.
                _mark_pnl = None
                # data-loop C: same discipline for the exit book-walk outputs so
                # exit_fill_ratio / exit_depth_at_limit can never carry a prior position's
                # walk when realistic_paper_fills is off or a branch is skipped.
                _filled = None
                _levels = None
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
                    # --- 2026-07-31 STAGED paper execution-realism haircuts (#4b, #1) ---
                    # PAPER-ONLY (self._paper_mode). LIVE (dry_run=false) skips this entire
                    # block, so the live exit price / unrealized_pnl / bankroll accounting is
                    # byte-for-byte unchanged. Two honest drags the raw book-walk misses:
                    #   #4b  when the fresh top-of-book ladder was empty/absent the walk
                    #        captured nothing (_filled==0) and exit_price is still the MARK
                    #        with zero slippage -> cross the last-known spread instead.
                    #   #1   a submission-latency / adverse-selection slip (bps of fill
                    #        price), larger on the 15m/1h hybrid windows that eat the ~8s
                    #        maker-wait before the order reaches the venue.
                    # Applied BEFORE the slippage telemetry below so fill_slippage_pct
                    # captures the haircut, and BEFORE the fee block so the taker fee is
                    # priced on the realistic fill.
                    if self._paper_mode:
                        # SELL legs = long YES (else branch) and long NO; the only BUY exit
                        # leg is the short-YES cover (outcome==NO and entry_leg!="NO").
                        _exit_is_sell = not (entry_leg != "NO" and pos.outcome == "NO")
                        _px = exit_price
                        if self._paper_missing_book_haircut_enabled and not _filled:
                            _sp = _liq.get("spread")
                            _hair = (
                                float(_sp)
                                if (_sp is not None and float(_sp) > 0)
                                else self._paper_missing_book_haircut_cents
                            )
                            _px = (_px - _hair) if _exit_is_sell else (_px + _hair)
                        _win_l = str(getattr(pos, "window_size", "") or "").lower()
                        _slip_bps = (
                            self._paper_latency_slip_bps_hybrid
                            if _win_l in ("15m", "1h")
                            else self._paper_latency_slip_bps
                        )
                        if _slip_bps > 0:
                            _d = _px * (float(_slip_bps) / 10000.0)
                            _px = (_px - _d) if _exit_is_sell else (_px + _d)
                        _px = min(0.99, max(0.01, _px))
                        if _px != exit_price:
                            exit_price = _px
                            if _exit_is_sell:
                                unrealized_pnl = pos.size * (exit_price - pos.entry_price)
                            else:
                                unrealized_pnl = pos.size * (pos.entry_price - exit_price)

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
                    # 2026-08-08 MAKER-FIRST FEE WEIGHTING (#1, see __init__). Codex-revised:
                    # (1) PAPER-ONLY (self._paper_mode) so LIVE (dry_run=false) bankroll/journal
                    #     accounting is byte-for-byte unchanged.
                    # (2) ENTRY LEG ONLY. Live entries on 15m/1h ALWAYS route maker-first
                    #     (entry_mode: hybrid), so the entry-side taker fee is systematically
                    #     over-charged by the maker-fill share. The EXIT leg is left at FULL
                    #     taker: live only routes NON-urgent exits maker-first (stops/TP/time/
                    #     pre-resolution stay FAK/taker), so weighting all hybrid exits would
                    #     under-charge the taker exits — full-taker exit is the safe, conservative
                    #     choice. rate 0.0 (or non-15m/1h, or live) => factor 1.0 => old behavior.
                    _maker_factor = 1.0
                    _mfr = min(1.0, max(0.0, self._hybrid_maker_fill_rate))
                    if self._paper_mode and _window in ("15m", "1h") and _mfr > 0.0:
                        _maker_factor = 1.0 - _mfr
                    _exit_fee = polymarket_taker_fee_usdc(
                        pos.size,
                        exit_price,
                        _fee_rate,
                    )
                    # Marketable entries are taker fills too, so charge the entry-side
                    # taker fee (priced at the entry fill) for the full round-trip cost.
                    #
                    # 2026-07-31 KNOWN CROSS-LANE BIAS (#2 — documented, deliberately NOT
                    # modeled): paper charges the 0.07 taker fee on BOTH legs for every
                    # crypto up/down lane. But LIVE runs maker-first on 15m/1h (entry_mode
                    # /exit_mode: hybrid), where a maker leg that fills pays 0% fee. So on
                    # 15m/1h ONLY, paper is systematically PESSIMISTIC on fees relative to
                    # live — a cross-lane distortion (5m paper vs 5m live is fair; 15m/1h
                    # paper over-charges vs 15m/1h live by the maker-fill share). The
                    # operator chose NOT to fabricate a maker-fill probability here. The
                    # correct fix is to weight the entry (and hybrid-exit) fee by the REAL
                    # maker-fill RATE measured from the order-lifecycle logger
                    # (data/calibration/order_lifecycle.jsonl, clob_client _lc/_order_lifecycle),
                    # NOT a guessed constant. Until that rate is wired, leave the full
                    # taker fee on both legs and read 15m/1h paper fee as an upper bound.
                    _entry_fee = (
                        polymarket_taker_fee_usdc(pos.size, pos.entry_price, _fee_rate) * _maker_factor
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
                # data-loop C (2026-07-30): exit book-quality symmetric to the entry side
                # (entry_paper_fill_quality has best_ask/best_bid/spread/depth/fill_ratio).
                # Previously _best_bid/_best_ask were never assigned, so exit_book_spread was
                # ALWAYS None. Compute best bid/ask from the YES book at exit (paper AND live),
                # plus fill-ratio + depth from the book-walk when realistic_paper_fills ran.
                _exit_liq = (market_liquidity or {}).get(pos.market_id) or {}
                _yes_bids = [b.get("price") for b in (_exit_liq.get("bids") or []) if b.get("price") is not None]
                _yes_asks = [a.get("price") for a in (_exit_liq.get("asks") or []) if a.get("price") is not None]
                _best_bid = max(_yes_bids) if _yes_bids else None
                _best_ask = min(_yes_asks) if _yes_asks else None
                _exit_spread = (
                    round(float(_best_ask) - float(_best_bid), 4)
                    if _best_bid is not None and _best_ask is not None
                    else None
                )
                _exit_best_bid = round(float(_best_bid), 4) if _best_bid is not None else None
                _exit_best_ask = round(float(_best_ask), 4) if _best_ask is not None else None
                _walk_filled = locals().get("_filled")
                _exit_fill_ratio = (
                    round(float(_walk_filled) / float(pos.size), 4)
                    if _walk_filled is not None and pos.size
                    else None
                )
                _walk_levels = locals().get("_levels")
                _exit_depth_at_limit = (
                    round(sum(float(s) for _p, s in _walk_levels if s), 4)
                    if _walk_levels
                    else None
                )
                # 2026-07-31 STAGED (#3, PAPER-ONLY): the book-walk pads any size beyond
                # captured depth at the worst level (pad_remainder_at_worst=True), so
                # _filled == size ALWAYS and the ratio above is ~1.0 by construction, hiding
                # partial-fill risk. Overwrite it in paper with the TRUE ratio =
                # captured top-of-book depth / size (capped 1.0); depth 0 -> ratio 0.0. The
                # worst-pad EXIT PRICE (bounded-pessimistic) is intentionally KEPT. LIVE is
                # untouched (guarded by _paper_mode) so its journal field is byte-for-byte
                # unchanged. FOLLOW-UP (FLAGGED, invasive): carry the true residual to the
                # next tick instead of padding — needs partial-exit plumbing across the sync
                # check_exits boundary and main.py's close/position-delete accounting.
                if self._paper_mode and _walk_levels is not None:
                    _cap_depth = round(sum(float(s) for _p, s in _walk_levels if s), 4)
                    _exit_depth_at_limit = _cap_depth
                    _exit_fill_ratio = (
                        round(min(1.0, _cap_depth / float(pos.size)), 4)
                        if pos.size
                        else None
                    )

                # PAPER CALIB Phase 3.6: raw (mark, no slippage/fee) vs execution-adjusted
                # (realized) PnL. _mark_pnl exists only when realistic_paper_fills walked
                # the book; without it we cannot separate the two, so leave both None.
                _raw_signal_pnl = locals().get("_mark_pnl")
                _exec_adj_pnl = (
                    round(unrealized_pnl, 2) if _raw_signal_pnl is not None else None
                )
                if _raw_signal_pnl is not None:
                    _raw_signal_pnl = round(float(_raw_signal_pnl), 2)

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
                        exit_best_bid=_exit_best_bid,
                        exit_best_ask=_exit_best_ask,
                        exit_depth_at_limit=_exit_depth_at_limit,
                        exit_fill_ratio=_exit_fill_ratio,
                        exit_mark_src=((market_liquidity or {}).get(pos.market_id) or {}).get("mark_src"),
                        exit_mark_age_ms=((market_liquidity or {}).get(pos.market_id) or {}).get("mark_age_ms"),
                        raw_signal_pnl=_raw_signal_pnl,
                        execution_adjusted_pnl=_exec_adj_pnl,
                        tp_trigger_mark_pnl_pct=(
                            round(pnl_pct, 4) if reason == "take_profit" else None
                        ),
                        tp_executable_exit_price=(
                            round(_tp_exec_price, 4)
                            if reason == "take_profit" and _tp_exec_price is not None
                            else None
                        ),
                        tp_executable_gross_pnl_pct=(
                            round(_tp_exec_gross_pnl_pct, 4)
                            if reason == "take_profit"
                            and _tp_exec_gross_pnl_pct is not None
                            else None
                        ),
                        tp_executable_net_pnl_pct=(
                            round(_tp_exec_net_pnl_pct, 4)
                            if reason == "take_profit"
                            and _tp_exec_net_pnl_pct is not None
                            else None
                        ),
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
