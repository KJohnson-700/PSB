"""
Main Entry Point
PolyBot AI - Polymarket Trading Bot
"""

import asyncio
import faulthandler
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List, Set
import yaml

# Add src and project root to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.market.scanner import (
    MarketScanner,
    Market,
    is_crypto_updown_market,
    resolved_updown_window_minutes,
)
from src.market.websocket import WebSocketClient
from src.analysis.ai_agent import AIAgent
from src.analysis.ai_decision_broker import AIDecisionBroker
from src.analysis.math_utils import PositionSizer
from src.strategies.bitcoin import BitcoinStrategy, BitcoinSignal
from src.strategies.sol_macro import SolMacroStrategy, SolMacroSignal
from src.strategies.eth_macro import ETHMacroStrategy
from src.strategies.hype_macro import HYPEMacroStrategy
from src.strategies.xrp_macro import XRPMacroStrategy
from src.strategies.doge_macro import DOGEMacroStrategy
from src.strategies.bnb_macro import BNBMacroStrategy
from src.execution.clob_client import CLOBClient, RiskManager, Position
from src.execution.trade_journal import TradeJournal, infer_entry_leg
from src.execution.exposure_manager import ExposureManager
from src.execution.resolution_tracker import ResolutionTracker
from src.execution.ctf_redeemer import CTFRedeemer
from src.execution.live_testing import (
    PositionExitManager,
    ExitDecision,
)
from src.analysis.journal_learning import (
    learning_loop_enabled,
    run_learning_cycle,
    log_learning_summary_to_logger,
)
from src.analysis.lane_identity import build_lane_metadata
from src.analysis.rejected_candidate_log import log_rejected_candidate
from src.analysis.lane_manager import LaneManager
from src.analysis.circuit_breakers import CircuitBreakerManager
from src.analysis.kelly_sizer import KellySizer, get_kelly_sizer
from src.analysis.calibration_log import (
    append_calibration_record,
    build_record_from_closed_trade,
)
from src.analysis.ghost_calibration import (
    DEFAULT_REGIME_LOG,
    build_ghost_calibration_status,
    settle_rejected_candidates,
)
from src.analysis.lane_calibration import LaneCalibrator
from src.notifications.notification_manager import (
    NotificationManager,
    merge_discord_webhook_from_env,
    format_discord_notifications_log_line,
)
from src.env_bootstrap import load_project_dotenv
from src.terminal_banners import print_shutdown_banner, print_startup_banner

# Manual global stop: if this file exists, the bot will not place new trades (paper or live).
KILL_SWITCH_FILE = Path(__file__).resolve().parent.parent / "data" / "KILL_SWITCH"
RUNTIME_DIR = Path(__file__).resolve().parent.parent / "data" / "runtime"
RUNTIME_STATUS_FILE = RUNTIME_DIR / "bot_runtime_status.json"
FAULT_LOG_FILE = RUNTIME_DIR / "polybot_fault.log"
_FAULT_HANDLER_STREAM = None


def _env_flag_enabled(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _shutdown_timeout_seconds(default: float = 8.0) -> float:
    raw = os.getenv("PSB_SHUTDOWN_TIMEOUT_SECONDS")
    if raw is None:
        return default
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        logging.warning(
            "Invalid PSB_SHUTDOWN_TIMEOUT_SECONDS=%r; using default %.1fs",
            raw,
            default,
        )
        return default


def market_regime_gate_decision(
    *,
    gate_config: Dict[str, Any],
    latest_regime: Optional[Dict[str, Any]],
    convergence_score: Optional[float],
) -> tuple[bool, str, Dict[str, Any]]:
    """Block weak-convergence entries only when the tracker says deadzone."""
    if not bool(gate_config.get("enabled", False)):
        return True, "disabled", {}
    if not latest_regime:
        return True, "no_regime_snapshot", {}

    combined = str(latest_regime.get("combined_regime") or "")
    poly = str(latest_regime.get("polymarket_regime") or "")
    price = str(latest_regime.get("price_regime") or "")
    extra = {
        "price_regime": price or None,
        "polymarket_regime": poly or None,
        "combined_regime": combined or None,
        "regime_ts": latest_regime.get("ts"),
        "regime_source": "market_regime",
    }
    if not combined.startswith("deadzone"):
        return True, "not_deadzone", extra

    threshold = float(gate_config.get("deadzone_min_convergence", 0.55) or 0.55)
    block_missing = bool(gate_config.get("block_missing_convergence_in_deadzone", True))
    try:
        score = float(convergence_score) if convergence_score is not None else None
    except (TypeError, ValueError):
        score = None
    extra["convergence_score"] = score
    extra["deadzone_min_convergence"] = threshold

    if score is None and block_missing:
        return False, "market_deadzone_missing_convergence", extra
    if score is not None and score < threshold:
        return False, "market_deadzone_low_convergence", extra
    return True, "deadzone_convergence_ok", extra


def _init_fault_handler() -> None:
    """Persist fatal-signal Python tracebacks for post-mortem debugging."""
    global _FAULT_HANDLER_STREAM
    if _FAULT_HANDLER_STREAM is not None:
        return
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _FAULT_HANDLER_STREAM = FAULT_LOG_FILE.open("a", encoding="utf-8")
        _FAULT_HANDLER_STREAM.write(
            f"\n=== fault-handler armed {datetime.now(timezone.utc).isoformat()} pid={os.getpid()} ===\n"
        )
        _FAULT_HANDLER_STREAM.flush()
        faulthandler.enable(file=_FAULT_HANDLER_STREAM, all_threads=True)
    except Exception as exc:
        logging.warning("Failed to initialize fault handler: %s", exc)


def _read_runtime_status() -> Dict[str, Any]:
    try:
        return json.loads(RUNTIME_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_runtime_status(
    *,
    phase: str,
    session_id: Optional[str] = None,
    clean_shutdown: Optional[bool] = None,
    detail: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort runtime breadcrumb for external supervision and crash triage."""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        payload = _read_runtime_status()
        payload.update(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
                "phase": phase,
                "argv": sys.argv[1:],
            }
        )
        if session_id is not None:
            payload["session_id"] = session_id
        if clean_shutdown is not None:
            payload["clean_shutdown"] = bool(clean_shutdown)
        if detail is not None:
            payload["detail"] = detail
        if extra:
            payload.update(extra)
        RUNTIME_STATUS_FILE.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        logging.debug("runtime status write failed: %s", exc)


def _compute_trading_cycle_sleep(
    scan_interval_sec: float,
    elapsed_sec: float,
    overrun_recovery_sleep_sec: Optional[float] = None,
) -> float:
    """Return the sleep budget after one unified trading cycle.

    Normal case: preserve the configured cycle interval.
    Overrun case: insert a short bounded recovery pause instead of either
    chaining the next cycle immediately or skipping a full extra interval.
    """
    interval = max(0.0, float(scan_interval_sec))
    elapsed = max(0.0, float(elapsed_sec))
    if elapsed < interval:
        return interval - elapsed
    if overrun_recovery_sleep_sec is None:
        overrun_recovery_sleep_sec = min(5.0, max(1.0, interval * 0.1))
    return max(0.0, min(float(overrun_recovery_sleep_sec), interval))


async def _time_strategy_scan(
    strategy_name: str,
    scan_coro,
) -> tuple[str, Any, int, bool]:
    """Measure one strategy scan wall time without changing its behavior."""
    started = time.perf_counter()
    try:
        result = await scan_coro
        ok = True
    except Exception as exc:
        result = exc
        ok = False
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return strategy_name, result, elapsed_ms, ok


def _detect_window_from_question(question: str) -> str:
    """Infer 5m / 15m / 30m / 1h bucket from Polymarket question text.

    "April 21, 1:30AM-1:35AM ET" → "5m"
    "April 21, 1:30AM-1:45AM ET" → "15m"
    "April 21, 1:30AM-2:00AM ET" → "30m"
    "May 17, 1AM ET" → "1h"
    """
    m = re.search(r'(\d+):(\d+)(AM|PM)[–\-](\d+):(\d+)(AM|PM)', question, re.IGNORECASE)
    if not m:
        if re.search(r"\b\d{1,2}(?::\d{2})?\s*(AM|PM)\s*ET\b", question, re.IGNORECASE):
            return "1h"
        return "15m"
    h1, m1, p1, h2, m2, p2 = m.groups()
    h1, m1, h2, m2 = int(h1), int(m1), int(h2), int(m2)
    if p1.upper() == 'PM' and h1 != 12:
        h1 += 12
    elif p1.upper() == 'AM' and h1 == 12:
        h1 = 0
    if p2.upper() == 'PM' and h2 != 12:
        h2 += 12
    elif p2.upper() == 'AM' and h2 == 12:
        h2 = 0
    start_min = h1 * 60 + m1
    end_min = h2 * 60 + m2
    delta = abs(end_min - start_min)
    if delta <= 6:
        return "5m"
    if delta >= 45:
        return "1h"
    if delta >= 23:
        return "30m"
    return "15m"


def _in_resolution_window(
    market, max_days: float, min_hours: float
) -> bool:
    """True if market resolves within [min_hours, max_days] from now. Used to cut noise from end-of-year markets."""
    if market.end_date is None:
        return False
    now = datetime.now(timezone.utc)
    end = market.end_date
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    td = end - now
    hours = td.total_seconds() / 3600
    if hours < min_hours:
        return False
    if td.days > max_days:
        return False
    return True


def _filter_short_horizon(markets, config: Dict) -> list:
    """Filter to markets resolving within the configured window. Crypto (15m) markets are exempt."""
    max_days = config.get("trading", {}).get("max_days_to_resolution", 14)
    min_hours = config.get("trading", {}).get("min_hours_to_resolution", 24)
    result = []
    for m in markets:
        if _is_crypto_market(m):
            result.append(m)  # Crypto 15m markets always included
        elif _in_resolution_window(m, max_days, min_hours):
            result.append(m)
    return result


def _is_hourly_crypto_market(market) -> bool:
    """True for crypto Up/Down products whose active trade window is hourly."""
    if not _is_crypto_market(market):
        return False
    try:
        return resolved_updown_window_minutes(market) >= 60
    except Exception:
        return False


def _should_include_hourly_crypto_markets(config: Dict, cycle_number: int) -> bool:
    """Throttle hourly crypto scans to every N unified cycles.

    The local loop runs every 60s by default, while hourly products do not need
    a full rescan every minute. ``1`` disables throttling.
    """
    trading_cfg = (config.get("trading", {}) or {})
    raw = trading_cfg.get("crypto_hourly_scan_every_n_cycles", 3)
    try:
        every_n = max(1, int(raw))
    except (TypeError, ValueError):
        every_n = 3
    cycle_n = max(1, int(cycle_number or 1))
    return ((cycle_n - 1) % every_n) == 0


def _filter_crypto_hourly_markets(markets, include_hourly: bool) -> list:
    """Optionally drop hourly crypto markets from the active scan universe."""
    if include_hourly:
        return list(markets)
    return [m for m in markets if not _is_hourly_crypto_market(m)]


def _is_crypto_market(market) -> bool:
    """Crypto up/down and short-candle markets.

    Uses the same slug/question/group rules as the scanner.
    """
    if is_crypto_updown_market(market):
        return True
    window_minutes = getattr(market, "window_minutes", None)
    if window_minutes is not None:
        return window_minutes <= 45
    return False


class PolyBot:
    """Main trading bot orchestrator"""

    def __init__(self, config_path: str = None):
        # Load configuration
        self.config = self._load_config(config_path)
        # Strong refs to fire-and-forget background tasks: without this the event
        # loop only weakly references tasks and may GC them mid-flight (silently
        # killing e.g. the price websocket). See _spawn_bg.
        self._bg_tasks: set = set()
        self.lane_manager = LaneManager(self.config)
        self.lane_calibrator = self._build_lane_calibrator()
        self._apply_exposure_env_overrides()

        # Initialize components
        self.market_scanner = MarketScanner(self.config)
        self.ws_client = WebSocketClient(self.config)
        self.ai_agent = AIAgent(self.config)
        # Async-decoupled AI decision broker: strategies enqueue here instead of
        # awaiting the provider in their per-market loop. See the broker module
        # docstring for the full design.
        _broker_cfg = (
            (self.config.get("ai") or {}).get("decision_broker") or {}
        ) if isinstance(self.config.get("ai"), dict) else {}
        from pathlib import Path as _Path
        _log_path = _Path("data/ai_pipeline/pending_ai_decisions.jsonl")
        self.ai_broker = AIDecisionBroker(
            ai_agent=self.ai_agent,
            max_decision_age_sec=float(_broker_cfg.get("max_decision_age_sec", 120.0)),
            max_pending_decisions=int(_broker_cfg.get("max_pending_decisions", 24)),
            price_drift_threshold=float(_broker_cfg.get("price_drift_threshold", 0.03)),
            cycle_counter_ref=lambda: getattr(self, "_performance_feedback_cycle", 0),
            log_path=_log_path,
            log_jsonl=bool(_broker_cfg.get("log_jsonl", True)),
        )
        self.position_sizer = PositionSizer(
            kelly_fraction=self.config.get("trading", {}).get("kelly_fraction", 0.25),
            max_position_pct=self.config.get("trading", {}).get(
                "max_exposure_per_trade", 0.05
            ),
            min_position=self.config.get("trading", {}).get(
                "default_position_size", 10
            ),
            max_position=self.config.get("trading", {}).get("max_position_size", 15),
        )
        self.kelly_sizer = get_kelly_sizer(self.config)
        self.notifier = NotificationManager(self.config)
        is_paper = self.config.get("trading", {}).get("dry_run", True)
        # Each crypto strategy gets its OWN exposure manager so losses
        # in one don't pause the other.
        self.btc_exposure_manager = ExposureManager(self.config, is_paper=is_paper, notifications=self.notifier, lane_name='BTC')
        self.sol_exposure_manager = ExposureManager(self.config, is_paper=is_paper, notifications=self.notifier, lane_name='SOL')
        self.eth_exposure_manager = ExposureManager(self.config, is_paper=is_paper, notifications=self.notifier, lane_name='ETH')
        self.hype_exposure_manager = ExposureManager(self.config, is_paper=is_paper, notifications=self.notifier, lane_name='HYPE')
        self.xrp_exposure_manager = ExposureManager(self.config, is_paper=is_paper, notifications=self.notifier, lane_name='XRP')
        self.doge_exposure_manager = ExposureManager(self.config, is_paper=is_paper, notifications=self.notifier, lane_name='DOGE')
        self.bnb_exposure_manager = ExposureManager(self.config, is_paper=is_paper, notifications=self.notifier, lane_name='BNB')
        # Keep a reference for resolution tracker settlements
        self.exposure_manager = self.btc_exposure_manager

        self._dead_zone_skip_callback = None
        self._rebuild_runtime_config_dependents()
        self.clob_client = CLOBClient(self.config)
        self.risk_manager = RiskManager(self.config)
        self.circuit_breakers = CircuitBreakerManager(self.config)

        # Track last signal counts per strategy (for dashboard)
        self.last_signal_counts = {
            "bitcoin": 0,
            "sol_macro": 0,
            "eth_macro": 0,
            "hype_macro": 0,
            "xrp_macro": 0,
            "doge_macro": 0,
            "bnb_macro": 0,
        }
        # ISO timestamp of the last time each strategy completed a cycle
        self.last_cycle_times: Dict[str, str] = {}
        # Running total of signals ever generated (never resets, lets dashboard show cumulative activity)
        self.cumulative_signal_counts: Dict[str, int] = {}
        # Per-strategy scan diagnostics (AI usage + skip buckets) for observability.
        self.last_ai_scan_stats: Dict[str, Dict[str, Any]] = {}
        self.last_buy_no_skip_counts: Dict[str, Dict[str, int]] = {}
        self.last_buy_no_skip_samples: Dict[str, Dict[str, Any]] = {}
        self.ghost_calibration_status: Dict[str, Any] = {}
        self._last_ghost_calibration_refresh_monotonic: float = 0.0

        # Trade journal: every process restart starts a FRESH session at
        # initial_bankroll (500). This ensures every restart = clean test run.
        # Resume only if PAPER_SESSION_ID is explicitly set to an existing session name.
        _forced_session = os.environ.get("PAPER_SESSION_ID")
        _resume_session = os.environ.get("PAPER_RESUME_SESSION", "false").lower() in ("1", "true", "yes")
        if _forced_session and not _resume_session:
            # Explicit session name given — use it (e.g. PAPER_SESSION_ID=reset_20260416)
            self.journal = TradeJournal(session_id=_forced_session, resume_latest=False)
            logging.info(f"Forced session via PAPER_SESSION_ID={_forced_session}")
        elif _resume_session:
            # Opt-in to resume: PAPER_RESUME_SESSION=true + no session name
            self.journal = TradeJournal(resume_latest=True)
            logging.info(f"Resuming latest session: {self.journal.session_id}")
        else:
            # Default: fresh session every restart (process lifecycle = test cycle)
            new_id = datetime.now().strftime("test_%Y%m%d_%H%M%S")
            self.journal = TradeJournal(session_id=new_id, resume_latest=False)
            self.bankroll = float(self.config.get("backtest", {}).get("initial_bankroll", 500.0))
            logging.info(f"Fresh session on restart: {new_id} @ ${self.bankroll:.2f}")
        self._session_traded_market_ids: Set[str] = self._load_session_traded_market_ids()

        def _dead_zone_skip_callback(
            *,
            strategy: str,
            market: Market,
            action: str,
            edge: float,
            hour_utc: int,
            blocked_hours: list,
            bankroll: float,
            metadata: Optional[Dict[str, Any]] = None,
        ) -> None:
            payload = dict(metadata or {})
            payload.update(
                build_lane_metadata(
                    strategy=strategy,
                    window_size=payload.get("window_size"),
                    action=action,
                    direction=payload.get("direction"),
                    side_source=payload.get("side_source"),
                    resolver_path=payload.get("resolver_path"),
                    ai_used=bool(payload.get("ai_used", False)),
                    reason=payload.get("reason"),
                    signal_reason=payload.get("signal_reason"),
                    htf_bias=payload.get("htf_bias"),
                    primary_htf_bias=payload.get("primary_htf_bias"),
                    alt_htf_bias=payload.get("alt_htf_bias"),
                    btc_1h_regime=payload.get("btc_1h_regime"),
                )
            )
            self.journal.log_dead_zone_skip(
                market_id=market.id,
                market_question=market.question,
                strategy=strategy,
                action=action,
                hour_utc=hour_utc,
                blocked_hours=blocked_hours,
                bankroll=bankroll,
                edge=edge,
                extra=payload,
            )

        def _buy_no_skip_callback(
            *,
            strategy: str,
            market: Market,
            bankroll: float,
            payload: Dict[str, Any],
        ) -> None:
            skip_reason = str(payload.get("skip_reason") or "unknown")
            lane_payload = dict(payload)
            lane_payload.update(
                build_lane_metadata(
                    strategy=strategy,
                    window_size=lane_payload.get("window_size"),
                    action="BUY_NO",
                    direction=lane_payload.get("direction", "DOWN"),
                    side_source=lane_payload.get("side_source"),
                    resolver_path=lane_payload.get("resolver_path"),
                    ai_used=bool(lane_payload.get("ai_used", False)),
                    reason=skip_reason,
                    signal_reason=lane_payload.get("signal_reason"),
                    htf_bias=lane_payload.get("htf_bias"),
                    primary_htf_bias=lane_payload.get("primary_htf_bias"),
                    alt_htf_bias=lane_payload.get("alt_1h_trend"),
                    btc_1h_regime=lane_payload.get("btc_1h_regime"),
                )
            )
            self.journal.log_buy_no_skip(
                market_id=market.id,
                market_question=market.question,
                strategy=strategy,
                bankroll=bankroll,
                skip_reason=skip_reason,
                window_size=str(lane_payload.get("window_size") or ""),
                yes_price=float(lane_payload.get("yes_price", 0.0) or 0.0),
                edge=float(lane_payload.get("edge", 0.0) or 0.0),
                effective_min_edge=float(lane_payload.get("effective_min_edge", 0.0) or 0.0),
                rsi=float(lane_payload.get("rsi", 0.0) or 0.0),
                htf_bias=str(lane_payload.get("htf_bias") or ""),
                signal_reason=str(lane_payload.get("signal_reason") or ""),
                alt_1h_trend=lane_payload.get("alt_1h_trend"),
                extra=lane_payload,
            )
            # Ghost-log BUY_NO suppressions that are NOT already written to the
            # ghost log via a sibling log_rejected_candidate/_log_skip_reject call
            # (flagged with _ghost_blind at the emit site). This routes blind
            # BUY_NO guards — sol_15m_bull_regime_expensive_short,
            # quant_disagree_flip_buy_no_disabled, bull_regime_*, rsi_hard_blocked,
            # lane_price_band, lane_size_too_small — into the settle pipeline so
            # their counterfactual win rate becomes measurable. Reasons that
            # already ghost-log elsewhere never set the flag, so no double-logging.
            if lane_payload.get("_ghost_blind"):
                try:
                    log_rejected_candidate(
                        strategy=strategy,
                        window=str(lane_payload.get("window_size") or ""),
                        side=str(lane_payload.get("side") or "SHORT"),
                        action="BUY_NO",
                        reason=skip_reason,
                        market=market,
                        yes_price=float(lane_payload.get("yes_price", 0.0) or 0.0),
                        est_prob_up=0.5,
                        htf_bias=(str(lane_payload.get("htf_bias")) or None),
                        context=dict(lane_payload),
                        stage="buy_no_skip",
                        btc_1h_regime=lane_payload.get("btc_1h_regime"),
                    )
                except Exception as exc:  # noqa: BLE001 — telemetry must not block scan
                    logging.debug("buy_no ghost-log failed: %s", exc)

        self._dead_zone_skip_callback = _dead_zone_skip_callback
        self._buy_no_skip_callback = _buy_no_skip_callback
        self._wire_strategy_callbacks()

        if learning_loop_enabled(self.config):
            try:
                _lcfg = self.config.get("learning_loop") or {}
                _vp = _lcfg.get("vault_path")
                _vp_path = Path(_vp) if _vp else None
                payload = run_learning_cycle(
                    self.config,
                    include_archive=bool(_lcfg.get("include_archive", True)),
                    vault_path=_vp_path,
                )
                log_learning_summary_to_logger(payload)
            except Exception as e:
                logging.warning("Learning loop skipped: %s", e, exc_info=True)

        # Resolution tracker — fetches REAL outcomes from Polymarket API
        # Resolution check every 60s — crypto candle markets resolve in 15 minutes
        self.resolution_tracker = ResolutionTracker(check_interval_seconds=60)

        # CTF Redeemer — claims resolved on-chain positions (live mode only).
        # In dry_run (paper) mode this logs DRY RUN messages and never touches the chain.
        # In live mode it calls redeemPositions() on the Polygon CTF contract after each win.
        _dry_run = self.config.get("trading", {}).get("dry_run", True)
        self.ctf_redeemer = CTFRedeemer(
            dry_run=_dry_run,
            private_key=os.environ.get("WALLET_PRIVATE_KEY"),
            rpc_url=os.environ.get("RPC_URL"),
        )

        # Position exit manager — checks active positions for TP/SL/time exits
        self.exit_manager = PositionExitManager(self.config)

        # Drift-driven runtime feedback cadence (see performance_feedback in settings.yaml)
        self._performance_feedback_cycle = 0

        # State
        self.running = False
        # Serialize order placement, exits, resolution settlement — one trading loop, same lock.
        self._execution_lock = asyncio.Lock()
        self._dashboard_server = None
        _initial_bankroll = self.config.get("backtest", {}).get("initial_bankroll", 1000.0)
        # Restore bankroll from last journal snapshot (or last entries line) so restarts
        # don't reset to initial_bankroll when snapshots.jsonl is sparse.
        _last_snap = self.journal.get_snapshots(limit=1)
        if _last_snap and _last_snap[-1].get("bankroll") is not None:
            self.bankroll = float(_last_snap[-1]["bankroll"])
            logging.info(
                f"Bankroll restored from last snapshot: ${self.bankroll:,.2f} "
                f"(initial was ${_initial_bankroll:,.2f})"
            )
        else:
            _from_log = self.journal.last_bankroll_from_entries_log()
            if _from_log is not None:
                self.bankroll = _from_log
                logging.info(
                    f"Bankroll restored from journal entries: ${self.bankroll:,.2f} "
                    f"(initial was ${_initial_bankroll:,.2f})"
                )
            else:
                self.bankroll = _initial_bankroll
        _cint = self.config.get("trading", {}).get("cycle_interval_sec", 120)
        self.scan_interval = max(30, int(_cint))  # single unified loop: scan + crypto + exits
        self._unified_cycle_count = 0
        _recovery_sleep = (
            self.config.get("trading", {}).get("overrun_recovery_sleep_sec")
        )
        if _recovery_sleep is None:
            self.overrun_recovery_sleep_sec = min(
                5.0, max(1.0, float(self.scan_interval) * 0.1)
            )
        else:
            self.overrun_recovery_sleep_sec = max(0.0, float(_recovery_sleep))

        # Sync open positions AFTER bankroll is known so risk manager has correct baseline
        self._sync_journal_to_risk_manager()
        self.risk_manager.bankroll = self.bankroll

        # Restore today's daily_pnl and daily_trades from journal so mid-day restarts
        # don't reset loss-limit checks to zero (bug: bot could breach daily limit, restart,
        # and immediately trade again as if no losses occurred)
        self._restore_daily_stats()
        self._sync_exposure_managers_portfolio_pnl()

        # Setup logging
        self._setup_logging()
        em0 = self.btc_exposure_manager
        logging.warning(
            "EXPOSURE per-lane: loss_kill_switch_enabled=%s max_consecutive_losses=%s pause_cycles=%s "
            "(btc/sol/eth/xrp each have separate streaks). "
            "This is the loss-streak lane pause, not the manual global stop. "
            "If a stale image is running, set EXPOSURE_LOSS_KILL_SWITCH_ENABLED=false in the environment and restart.",
            em0.loss_kill_switch_enabled,
            em0.max_consecutive_losses,
            em0.pause_cycles,
        )
        self._refresh_ghost_calibration_state(force=True)

    def _apply_exposure_env_overrides(self) -> None:
        """Apply exposure toggles from env before ExposureManager construction.

        Docker bakes ``config/settings.yaml`` at **build** time. Without a redeploy,
        production can still have an old ``loss_kill_switch_enabled`` value even after Git
        changes. Environment variables override at **process start**:

        - ``EXPOSURE_LOSS_KILL_SWITCH_ENABLED=true`` (or 1/yes/on) → force ON
        - ``false`` / ``0`` / ``no`` / ``off`` → force OFF
        """
        raw = os.environ.get("EXPOSURE_LOSS_KILL_SWITCH_ENABLED", "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            self.config.setdefault("exposure", {})["loss_kill_switch_enabled"] = True
        elif raw in ("0", "false", "no", "off"):
            self.config.setdefault("exposure", {})["loss_kill_switch_enabled"] = False

    def _is_dry_run_mode(self) -> bool:
        return bool((self.config.get("trading") or {}).get("dry_run", True))

    def _lane_calibration_shadow_mode(self) -> bool:
        """Resolve calibration mode from paper/live trading mode.

        ``lane_calibration.shadow_mode`` remains the legacy fallback, but the
        preferred contract is:
        - ``paper_shadow_mode`` for paper/dry-run sessions
        - ``live_shadow_mode`` for live sessions
        """
        cal_cfg = (self.config.get("lane_calibration") or {})
        default_shadow = bool(cal_cfg.get("shadow_mode", True))
        if self._is_dry_run_mode():
            return bool(cal_cfg.get("paper_shadow_mode", default_shadow))
        return bool(cal_cfg.get("live_shadow_mode", default_shadow))

    def _ai_entry_attribution(
        self,
        *,
        ai_consulted: bool,
        ai_verdict: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Journal AI decision-gate state without enabling the gate."""
        ai_cfg = self.config.get("ai") or {}
        gate_cfg = ai_cfg.get("decision_layer") or self.config.get("decision_gates") or {}
        gate_enabled = bool(gate_cfg.get("enabled", False))
        consulted = bool(ai_consulted)
        if ai_verdict is None:
            if not consulted:
                verdict = "not_consulted"
            elif gate_enabled:
                verdict = "approved"
            else:
                verdict = "analytics_consulted_gate_off"
        else:
            verdict = str(ai_verdict)
        return {
            # Historical request: "AI on/off" means pre-entry decision-gate on/off.
            "ai_enabled_at_entry": gate_enabled,
            "ai_decision_gate_enabled_at_entry": gate_enabled,
            "ai_analytics_enabled_at_entry": bool(ai_cfg.get("enabled", False)),
            "ai_live_inferencing_at_entry": bool(ai_cfg.get("live_inferencing", True)),
            "ai_consulted": consulted,
            "ai_verdict": verdict,
            "ai_influenced_decision": bool(gate_enabled and consulted),
        }

    def _build_lane_calibrator(self) -> LaneCalibrator:
        """Construct the per-lane probability calibrator.

        Paper and live can resolve different calibration modes so the same bot can
        paper-trade in observation mode while using calibrated probabilities live.
        """
        shadow = self._lane_calibration_shadow_mode()
        cal_cfg = (self.config.get("lane_calibration") or {})
        min_samples_live = max(
            0, int(cal_cfg.get("min_samples_to_apply_live", 15) or 0)
        )
        # β_mean veto thresholds — config-overridable. Defaults set in
        # lane_calibration.py (max_mean=0.40, min_n=30). Set max_mean=0 or
        # min_n=0 in config to disable.
        from src.analysis.lane_calibration import (
            BETA_VETO_MAX_MEAN as _DEFAULT_BETA_VETO_MAX_MEAN,
            BETA_VETO_MIN_N as _DEFAULT_BETA_VETO_MIN_N,
            BETA_BLEND_N_FLOOR as _DEFAULT_BLEND_N_FLOOR,
            BETA_BLEND_N_FULL as _DEFAULT_BLEND_N_FULL,
        )
        beta_veto_max_mean = float(
            cal_cfg.get("beta_veto_max_mean", _DEFAULT_BETA_VETO_MAX_MEAN)
        )
        beta_veto_min_n = int(
            cal_cfg.get("beta_veto_min_n", _DEFAULT_BETA_VETO_MIN_N) or 0
        )
        beta_blend_enabled = bool(cal_cfg.get("beta_blend_enabled", True))
        beta_blend_n_floor = int(
            cal_cfg.get("beta_blend_n_floor", _DEFAULT_BLEND_N_FLOOR) or _DEFAULT_BLEND_N_FLOOR
        )
        beta_blend_n_full = int(
            cal_cfg.get("beta_blend_n_full", _DEFAULT_BLEND_N_FULL) or _DEFAULT_BLEND_N_FULL
        )
        posterior_version = str(
            cal_cfg.get("posterior_version") or "lane_identity_v2_source_resolver"
        ).strip()
        # Per-lane threshold overrides (ghost-derived). Off by default —
        # operator inspects lane_thresholds.json then flips
        # `lane_calibration.per_lane_thresholds.enabled: true` in config.
        plt_cfg = (cal_cfg.get("per_lane_thresholds") or {})
        plt_enabled = bool(plt_cfg.get("enabled", False))
        per_lane_thresholds: Dict[str, Any] = {}
        if plt_enabled:
            try:
                from src.analysis.lane_thresholds import (
                    load_lane_thresholds,
                    DEFAULT_THRESHOLDS_PATH,
                )
                path_str = plt_cfg.get("path") or str(DEFAULT_THRESHOLDS_PATH)
                per_lane_thresholds = load_lane_thresholds(Path(path_str))
                logging.info(
                    "lane_calibration: per-lane overrides ENABLED, loaded %d entries",
                    len(per_lane_thresholds),
                )
            except Exception as exc:  # noqa: BLE001 — init must not block startup
                logging.warning(
                    "lane_calibration: per-lane override load failed: %s", exc
                )
                per_lane_thresholds = {}
        try:
            return LaneCalibrator(
                shadow_mode=shadow,
                min_samples_to_apply=(0 if shadow else min_samples_live),
                beta_veto_max_mean=beta_veto_max_mean,
                beta_veto_min_n=beta_veto_min_n,
                per_lane_thresholds_enabled=plt_enabled,
                per_lane_thresholds=per_lane_thresholds,
                posterior_version=posterior_version,
                beta_blend_enabled=beta_blend_enabled,
                beta_blend_n_floor=beta_blend_n_floor,
                beta_blend_n_full=beta_blend_n_full,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry init must not block startup
            logging.warning("lane_calibration init failed: %s", exc)
            return LaneCalibrator(shadow_mode=True, min_samples_to_apply=0)

    def _refresh_ghost_calibration_state(self, *, force: bool = False) -> None:
        """Auto-settle rejected candidates and publish a runtime status snapshot.

        Passes the calibrator into ``settle_rejected_candidates`` so newly-settled
        ghosts feed β at reduced weight — the self-healing path for β-vetoed
        lanes. Weight is config-overridable via ``lane_calibration.ghost_weight``
        (default 0.5).
        """
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_ghost_calibration_refresh_monotonic) < 60.0:
            return
        cal_cfg = (self.config.get("lane_calibration") or {})
        ghost_weight = float(cal_cfg.get("ghost_weight", 0.5) or 0.0)
        cal = getattr(self, "lane_calibrator", None)
        settle_summary = settle_rejected_candidates(
            calibrator=cal,
            ghost_weight=ghost_weight,
        )
        # Persist β/α updates so the self-heal survives restarts.
        if cal is not None and settle_summary.get("ghost_beta_updates", 0) > 0:
            try:
                cal._flush()
            except Exception as _fe:  # noqa: BLE001 — telemetry only
                logging.warning("calibrator flush after ghost-feed skipped: %s", _fe)
        # Periodic per-lane threshold recompute (gated by config; default off).
        # Reruns the ghost-derived threshold derivation, writes lane_thresholds.json,
        # and hot-reloads it into the calibrator. Independent of whether the
        # overrides are *applied* — derivation can run for inspection-only mode.
        plt_cfg = (cal_cfg.get("per_lane_thresholds") or {})
        if bool(plt_cfg.get("recompute_on_settle", False)) and settle_summary.get(
            "ghost_beta_updates", 0
        ) >= int(plt_cfg.get("recompute_min_new_settles", 25) or 25):
            try:
                from src.analysis.lane_thresholds import (
                    compute_lane_thresholds,
                    write_lane_thresholds,
                    load_lane_thresholds,
                    DEFAULT_THRESHOLDS_PATH,
                )
                payload = compute_lane_thresholds(
                    min_bucket_n=int(plt_cfg.get("min_bucket_n", 100) or 100),
                    wr_veto_threshold=float(
                        plt_cfg.get("wr_veto_threshold", 0.40) or 0.40
                    ),
                    recommended_max_mean=float(
                        plt_cfg.get("recommended_max_mean", 0.40) or 0.40
                    ),
                    live_mature_n=int(plt_cfg.get("live_mature_n", 50) or 50),
                )
                tpath = Path(plt_cfg.get("path") or str(DEFAULT_THRESHOLDS_PATH))
                write_lane_thresholds(payload, path=tpath)
                # Hot-reload into the live calibrator only if overrides are
                # actually enabled — otherwise just write the file for review.
                if bool(plt_cfg.get("enabled", False)) and cal is not None:
                    cal.per_lane_thresholds = load_lane_thresholds(tpath)
                    logging.info(
                        "lane_thresholds recomputed and hot-reloaded "
                        "(%d lanes)", len(cal.per_lane_thresholds)
                    )
                else:
                    logging.info(
                        "lane_thresholds recomputed (inspection-only mode, "
                        "%d lane buckets); enable via config to apply",
                        len(payload.get("thresholds", {})),
                    )
            except Exception as _te:  # noqa: BLE001 — telemetry only
                logging.warning("lane_thresholds recompute skipped: %s", _te)
        status = build_ghost_calibration_status()
        status["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
        status["last_settle_summary"] = settle_summary
        self.ghost_calibration_status = status
        self._last_ghost_calibration_refresh_monotonic = now_mono
        if settle_summary.get("newly_settled", 0):
            logging.info(
                "Rejected-candidate tracker settled %s new candidates "
                "(unresolved=%s total_settled=%s ghost_β_updates=%s)",
                settle_summary.get("newly_settled", 0),
                status.get("unresolved", 0),
                status.get("total_settled", 0),
                settle_summary.get("ghost_beta_updates", 0),
            )

    def _validate_lane_calibration_runtime(self) -> None:
        """Fail fast if calibration mode and strategy wiring disagree."""
        cal_cfg = (self.config.get("lane_calibration") or {})
        if not bool(cal_cfg.get("enabled", False)):
            return
        cal = getattr(self, "lane_calibrator", None)
        if cal is None:
            raise RuntimeError("lane_calibration enabled but lane_calibrator is missing")
        required = (
            ("bitcoin", getattr(self, "bitcoin_strategy", None)),
            ("sol_macro", getattr(self, "sol_macro_strategy", None)),
            ("eth_macro", getattr(self, "eth_macro_strategy", None)),
            ("hype_macro", getattr(self, "hype_macro_strategy", None)),
            ("xrp_macro", getattr(self, "xrp_macro_strategy", None)),
            ("doge_macro", getattr(self, "doge_macro_strategy", None)),
            ("bnb_macro", getattr(self, "bnb_macro_strategy", None)),
        )
        missing = [
            name for name, strategy in required
            if strategy is None or getattr(strategy, "lane_calibrator", None) is not cal
        ]
        if missing:
            raise RuntimeError(
                "lane_calibration wiring incomplete for strategies: "
                + ", ".join(missing)
            )
        logging.info(
            "Lane calibration ready: mode=%s strategies=%s",
            "SHADOW" if bool(cal.shadow_mode) else "LIVE",
            ",".join(name for name, _ in required),
        )

    def _load_config(self, config_path: str = None) -> Dict[str, Any]:
        """Load configuration from YAML file"""
        if config_path is None:
            config_path = (
                Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
            )

        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            logging.info(f"Loaded config from {config_path}")
            # Merge DISCORD_WEBHOOK_URL from env if not set in YAML
            notifications = config.setdefault("notifications", {})
            if not notifications.get("discord_webhook") and os.getenv(
                "DISCORD_WEBHOOK_URL"
            ):
                notifications["discord_webhook"] = os.getenv("DISCORD_WEBHOOK_URL")
            return config
        except Exception as e:
            logging.warning(f"Could not load config: {e}, using defaults")
            return self._default_config()

    def apply_config_updates(self, updates: Dict[str, Any]) -> None:
        """Merge partial config (e.g. dashboard POST /api/config) into the running bot."""
        from src.config_merge import deep_merge_config

        deep_merge_config(self.config, updates)
        self.lane_manager = LaneManager(self.config)
        # Refresh calibrator's shadow-mode flag if config touched it; posteriors stay.
        if updates.get("lane_calibration") or updates.get("trading"):
            prev = getattr(self, "lane_calibrator", None)
            shadow = self._lane_calibration_shadow_mode()
            if prev is not None and hasattr(prev, "shadow_mode"):
                prev.shadow_mode = shadow
            else:
                self.lane_calibrator = self._build_lane_calibrator()
        merge_discord_webhook_from_env(self.config)
        self.notifier.reload_from_config(self.config)
        self._rebuild_runtime_config_dependents()
        self.ai_agent.refresh_from_config(self.config.get("ai", {}))
        if updates.get("exposure"):
            exp = self.config.get("exposure") or {}
            for attr in (
                "btc_exposure_manager",
                "sol_exposure_manager",
                "eth_exposure_manager",
                "hype_exposure_manager",
                "xrp_exposure_manager",
                "doge_exposure_manager",
                "bnb_exposure_manager",
            ):
                mgr = getattr(self, attr, None)
                if mgr is not None:
                    mgr.reload_from_config(exp)
        self._log_effective_sizing_config(context="config_update")

    def _rebuild_runtime_config_dependents(self) -> None:
        """Refresh live objects that cache config-derived fields at init time."""
        trading_cfg = self.config.get("trading", {}) or {}
        self.position_sizer = PositionSizer(
            kelly_fraction=trading_cfg.get("kelly_fraction", 0.25),
            max_position_pct=trading_cfg.get("max_exposure_per_trade", 0.05),
            min_position=trading_cfg.get("default_position_size", 10),
            max_position=trading_cfg.get("max_position_size", 15),
        )
        if hasattr(self, "kelly_sizer") and self.kelly_sizer is not None:
            self.kelly_sizer.reload_from_config(self.config)
        else:
            self.kelly_sizer = KellySizer(self.config)
        if hasattr(self, "market_scanner") and self.market_scanner is not None:
            self.market_scanner.reload_from_config(self.config)
        self.bitcoin_strategy = BitcoinStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.btc_exposure_manager,
            ai_broker=getattr(self, "ai_broker", None),
        )
        self.sol_macro_strategy = SolMacroStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.sol_exposure_manager,
            ai_broker=getattr(self, "ai_broker", None),
        )
        self.eth_macro_strategy = ETHMacroStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.eth_exposure_manager,
            ai_broker=getattr(self, "ai_broker", None),
        )
        self.hype_macro_strategy = HYPEMacroStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.hype_exposure_manager,
            ai_broker=getattr(self, "ai_broker", None),
        )
        self.xrp_macro_strategy = XRPMacroStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.xrp_exposure_manager,
            ai_broker=getattr(self, "ai_broker", None),
        )
        self.doge_macro_strategy = DOGEMacroStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.doge_exposure_manager,
            ai_broker=getattr(self, "ai_broker", None),
        )
        self.bnb_macro_strategy = BNBMacroStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.bnb_exposure_manager,
            ai_broker=getattr(self, "ai_broker", None),
        )
        for strategy in (
            self.bitcoin_strategy,
            self.sol_macro_strategy,
            self.eth_macro_strategy,
            self.hype_macro_strategy,
            self.xrp_macro_strategy,
            self.doge_macro_strategy,
            self.bnb_macro_strategy,
        ):
            strategy.lane_calibrator = self.lane_calibrator
        self._validate_lane_calibration_runtime()
        self._wire_strategy_callbacks()
        self._log_effective_sizing_config(context="runtime_rebuild")

    def _log_effective_sizing_config(self, context: str = "runtime") -> None:
        """Log effective sizing knobs so runtime behavior is auditable from logs."""
        trading_cfg = self.config.get("trading", {}) or {}
        exposure_cfg = self.config.get("exposure", {}) or {}
        logging.info(
            "Sizing config (%s): trading[min=$%.2f max=$%.2f kelly=%.4f max_exposure_per_trade=%.4f] "
            "exposure[full=$%.2f moderate=$%.2f minimal=$%.2f min_trade_usd=$%.2f]",
            context,
            float(trading_cfg.get("default_position_size", 10) or 10),
            float(trading_cfg.get("max_position_size", 15) or 15),
            float(trading_cfg.get("kelly_fraction", 0.25) or 0.25),
            float(trading_cfg.get("max_exposure_per_trade", 0.05) or 0.05),
            float(exposure_cfg.get("full_size", 15.0) or 15.0),
            float(exposure_cfg.get("moderate_size", 13.0) or 13.0),
            float(exposure_cfg.get("minimal_size", 10.0) or 10.0),
            float(exposure_cfg.get("min_trade_usd", 0.0) or 0.0),
        )

    def _wire_strategy_callbacks(self) -> None:
        cb = getattr(self, "_dead_zone_skip_callback", None)
        buy_no_cb = getattr(self, "_buy_no_skip_callback", None)
        self.bitcoin_strategy.dead_zone_skip_callback = cb
        self.bitcoin_strategy.buy_no_skip_callback = buy_no_cb
        self.sol_macro_strategy.dead_zone_skip_callback = cb
        self.sol_macro_strategy.buy_no_skip_callback = buy_no_cb
        self.eth_macro_strategy.dead_zone_skip_callback = cb
        self.eth_macro_strategy.buy_no_skip_callback = buy_no_cb
        self.hype_macro_strategy.dead_zone_skip_callback = cb
        self.hype_macro_strategy.buy_no_skip_callback = buy_no_cb
        self.xrp_macro_strategy.dead_zone_skip_callback = cb
        self.xrp_macro_strategy.buy_no_skip_callback = buy_no_cb
        self.doge_macro_strategy.dead_zone_skip_callback = cb
        self.doge_macro_strategy.buy_no_skip_callback = buy_no_cb
        self.bnb_macro_strategy.dead_zone_skip_callback = cb
        self.bnb_macro_strategy.buy_no_skip_callback = buy_no_cb

    def _default_config(self) -> Dict[str, Any]:
        """Default configuration"""
        return {
            "polymarket": {"min_liquidity": 10000, "max_spread": 0.05},
            "trading": {
                "dry_run": True,
                "kelly_fraction": 0.25,
                "max_exposure_per_trade": 0.05,
            },
            "strategies": {},
            "ai": {"provider": "openai", "model": "gpt-4o"},
            "notifications": {"enabled": False},
            "risk": {"max_concurrent_positions": 10, "daily_loss_limit": 0.15},
        }

    def _sync_journal_to_risk_manager(self):
        """Load open positions from journal into risk manager so we respect limits on restart.

        NOTE: We add positions directly to the dict instead of calling add_position()
        because add_position() increments daily_trades counter. Synced positions are
        historical — they should NOT count toward today's trade limit.
        """
        for pos_data in self.journal.get_open_positions():
            try:
                opened_at = datetime.now()
                if pos_data.get("opened_at"):
                    try:
                        opened_at = datetime.fromisoformat(pos_data["opened_at"])
                    except (ValueError, TypeError):
                        pass
                end_date = None
                raw_end = pos_data.get("market_end_at") or (pos_data.get("entry_signal") or {}).get("market_end_at")
                if raw_end:
                    try:
                        end_date = datetime.fromisoformat(str(raw_end))
                    except (ValueError, TypeError):
                        end_date = None
                position = Position(
                    position_id=pos_data["trade_id"],
                    market_id=pos_data["market_id"],
                    market_question=pos_data.get("market_question", ""),
                    outcome=pos_data.get("outcome", "YES"),
                    size=pos_data.get("size", 0),
                    entry_price=pos_data.get("entry_price", 0),
                    current_price=pos_data.get(
                        "current_price", pos_data.get("entry_price", 0)
                    ),
                    pnl=pos_data.get("pnl", 0),
                    opened_at=opened_at,
                    end_date=end_date,
                strategy=pos_data.get("strategy", "unknown"),
                entry_leg=infer_entry_leg(pos_data),
                window_size=str(
                    pos_data.get("window_size")
                    or (pos_data.get("entry_signal") or {}).get("window_size")
                        or ""
                    ),
                    peak_token_price=float(
                        pos_data.get("peak_token_price")
                        or pos_data.get("current_price")
                        or pos_data.get("entry_price", 0)
                    ),
                    token_id_yes=str(pos_data.get("token_id_yes") or ""),
                    token_id_no=str(pos_data.get("token_id_no") or ""),
                    edge=float(pos_data.get("edge", 0.0) or 0.0),
                    confidence=float(pos_data.get("confidence", 0.0) or 0.0),
                    entry_signal=dict(pos_data.get("entry_signal") or {}),
                )
                # Add directly to dict — do NOT call add_position() as it increments daily_trades
                self.risk_manager.active_positions[position.position_id] = position
            except Exception as e:
                logging.warning(
                    f"Could not sync position {pos_data.get('trade_id')}: {e}"
                )
        synced = len(self.risk_manager.active_positions)
        if synced:
            logging.info(
                f"Synced {synced} open positions from journal to risk manager (daily_trades NOT incremented)"
            )

    def _restore_daily_stats(self):
        """Restore daily_pnl and daily_trades from today's journal EXIT entries.

        Without this, a mid-day restart resets the daily loss limit check to zero,
        allowing the bot to keep trading after already breaching its loss limit.
        """
        today = datetime.now().date()
        daily_pnl = 0.0
        daily_trades = 0
        _seen_exit_trade_ids: set[str] = set()
        try:
            for entry in self.journal.get_all_entries(limit=5000):
                ts_str = entry.get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str).date()
                except (ValueError, TypeError):
                    continue
                if ts != today:
                    continue
                if entry.get("event") == "EXIT":
                    pnl = entry.get("pnl", 0) or 0
                    # Sanity guard: skip obviously-buggy EXIT records caused by the
                    # pre-fix token-ordering mismatch on SELL_YES positions.
                    # Bound phantom EXIT PnL using max_position_size (USD) from config.
                    max_plausible = self.config.get("trading", {}).get("max_position_size", 15) * 40
                    if abs(pnl) > max_plausible:
                        logging.debug(
                            f"_restore_daily_stats: skipping anomalous EXIT "
                            f"pnl={pnl:+.2f} (>{max_plausible:.0f}) "
                            f"strategy={entry.get('strategy','?')}"
                        )
                        continue
                    _tid = entry.get("trade_id", "")
                    if _tid in _seen_exit_trade_ids:
                        continue  # skip duplicate EXIT for same trade_id
                    _seen_exit_trade_ids.add(_tid)
                    daily_pnl += pnl
                elif entry.get("event") == "ENTRY":
                    daily_trades += 1
            if daily_pnl != 0 or daily_trades > 0:
                self.risk_manager.daily_pnl = daily_pnl
                self.risk_manager.daily_trades = daily_trades
                logging.info(
                    f"Restored daily stats from journal: "
                    f"daily_pnl=${daily_pnl:+.2f}, daily_trades={daily_trades}"
                )
        except Exception as e:
            logging.warning(f"Could not restore daily stats: {e}")

    def _setup_logging(self):
        """Setup logging configuration.

        Uses force=True to override any prior basicConfig call (e.g., from early imports).
        """
        log_config = self.config.get("logging", {})
        level = getattr(logging, log_config.get("level", "INFO"))

        handlers = []
        if log_config.get("console", True):
            handlers.append(logging.StreamHandler())
        if log_config.get("file", True):
            log_dir = Path(__file__).resolve().parent.parent / "data" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.FileHandler(
                    log_dir / f"polybot_{datetime.now().strftime('%Y%m%d')}.log"
                )
            )

        # force=True removes any existing handlers/config so our setup actually takes effect.
        logging.basicConfig(
            level=level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=handlers,
            force=True,
        )

    def set_api_keys(self, api_keys: Dict[str, str]):
        """Set required API keys for all services."""
        # Pass all keys to the AI agent, which will select the ones it needs
        self.ai_agent.set_api_keys(api_keys)

        # Set credentials for the CLOB client (either key name — env loader may use either)
        polymarket_key = api_keys.get("PRIVATE_KEY") or api_keys.get(
            "POLYMARKET_PRIVATE_KEY"
        )
        if polymarket_key:
            self.clob_client.set_credentials(
                private_key=polymarket_key,
                api_key=api_keys.get("POLYMARKET_API_KEY"),
                api_secret=api_keys.get("POLYMARKET_API_SECRET"),
                api_passphrase=api_keys.get("POLYMARKET_API_PASSPHRASE"),
            )
        else:
            logging.warning(
                "Polymarket private key (PRIVATE_KEY or POLYMARKET_PRIVATE_KEY) not found in .env / config/secrets.env."
            )

    async def _run_startup_narrators(self) -> None:
        """Run AI narrators against the previous session and write each block
        as an ANNOTATION event into the *current* session's journal entries.jsonl.
        Fire-and-forget from start() — never blocks the trading loop. Self-gates
        on ai.session_summary.enabled."""
        try:
            summary_cfg = (self.config.get("ai", {}) or {}).get("session_summary", {}) or {}
            if not summary_cfg.get("enabled", False):
                return
            initial_delay = float(summary_cfg.get("startup_delay_seconds", 90) or 0)
            if initial_delay > 0:
                await asyncio.sleep(initial_delay)
                if not self.running:
                    return
            if not getattr(self, "ai_agent", None) or not self.ai_agent.is_available():
                return

            from pathlib import Path as _Path
            from src.execution.trade_journal import JOURNAL_DIR as _JD
            from src.analysis.ai_narrators import (
                aggregate_skip_exit_distributions,
                detect_calibration_drift,
                explain_strategy_conflict,
                load_closed_trades_from_summary,
                load_shadow_records,
                summarize_skip_exit_reasons,
                summarize_underperformance,
            )
            from src.execution.trade_journal import TradeJournal as _TJ

            current_dir = _Path(self.journal.session_dir)
            sessions = sorted(
                [
                    p for p in _Path(_JD).iterdir()
                    if p.is_dir() and p.name < current_dir.name and _TJ.session_dir_has_activity(p)
                ],
                key=lambda p: p.name,
                reverse=True,
            )
            if not sessions:
                logging.info("No previous session found; skipping startup narrators.")
                return
            prev = sessions[0]
            timeout = float(summary_cfg.get("timeout_seconds", 30))

            def _record(narrator_kind: str, text: str) -> None:
                if not text:
                    return
                self.journal.append_annotation(
                    trade_id=f"__session_summary__::{narrator_kind}",
                    text=text,
                    strategy="session_summary",
                    extra={
                        "source": "session_summary",
                        "narrator": narrator_kind,
                        "previous_session": prev.name,
                    },
                )

            # Underperformance narrator (best-effort: looks for a recent saved report)
            if summary_cfg.get("include_underperformance", True):
                report_paths = sorted(
                    (_Path(__file__).resolve().parents[1] / "docs" / "session_reports").glob("*underperformance*.md"),
                    reverse=True,
                )
                if report_paths:
                    try:
                        report_dict = {"_raw_markdown": report_paths[0].read_text(encoding="utf-8", errors="replace")}
                        text = await summarize_underperformance(report_dict, self.ai_agent, timeout=timeout)
                        _record("underperformance", text)
                    except Exception as e:
                        logging.debug("startup narrator (underperf) failed: %s", e)

            if summary_cfg.get("include_skip_exit_reasons", True):
                try:
                    dist = aggregate_skip_exit_distributions(prev / "entries.jsonl")
                    text = await summarize_skip_exit_reasons(
                        dist.get("skip", {}), dist.get("exit", {}), self.ai_agent, timeout=timeout
                    )
                    _record("skip_exit_reasons", text)
                except Exception as e:
                    logging.debug("startup narrator (skip/exit) failed: %s", e)

            if summary_cfg.get("include_calibration_drift", True):
                try:
                    shadow_path = _Path(__file__).resolve().parents[1] / "data" / "logs" / "ai_pipeline" / "shadow_pipeline.jsonl"
                    shadow = load_shadow_records(shadow_path)
                    closed = load_closed_trades_from_summary(prev / "summary.json")
                    text = await detect_calibration_drift(shadow, closed, self.ai_agent, timeout=timeout)
                    _record("calibration_drift", text)
                except Exception as e:
                    logging.debug("startup narrator (calibration) failed: %s", e)

            if summary_cfg.get("include_strategy_conflict", True):
                scan_summaries_path = prev / "scan_summaries.json"
                if scan_summaries_path.exists():
                    try:
                        import json as _json
                        with open(scan_summaries_path, encoding="utf-8") as f:
                            scan_summaries = _json.load(f) or {}
                        if scan_summaries:
                            text = await explain_strategy_conflict(scan_summaries, self.ai_agent, timeout=timeout)
                            _record("strategy_conflict", text)
                    except Exception as e:
                        logging.debug("startup narrator (conflict) failed: %s", e)

            logging.info("Startup AI narrators wrote ANNOTATION events into %s", current_dir / "entries.jsonl")
        except Exception as e:
            logging.debug("startup narrators failed: %s", e)

    def _wanted_clob_book_token_ids(self) -> List[str]:
        """YES/NO CLOB token ids for all open positions (for WS L2 subscribe)."""
        out: List[str] = []
        seen: Set[str] = set()
        for p in self.risk_manager.active_positions.values():
            for tid in (
                str(getattr(p, "token_id_yes", "") or "").strip(),
                str(getattr(p, "token_id_no", "") or "").strip(),
            ):
                if tid and tid not in seen:
                    seen.add(tid)
                    out.append(tid)
        return out

    async def _sync_clob_ws_book_subscriptions(self, channel: str) -> None:
        """Subscribe WS to books for open-position tokens; unsubscribe stale ids."""
        ws = self.ws_client
        if ws.ws is None:
            return
        want = set(self._wanted_clob_book_token_ids())
        have = set(ws.subscriptions.get(channel, set()))
        to_add = [t for t in want - have if t]
        to_remove = [t for t in have - want if t]
        if to_add:
            try:
                await ws.subscribe(channel, to_add)
            except Exception as e:
                logging.debug("clob ws subscribe: %s", e)
        if to_remove:
            try:
                await ws.unsubscribe(channel, to_remove)
            except Exception as e:
                logging.debug("clob ws unsubscribe: %s", e)

    async def _clob_ws_subscription_loop(self) -> None:
        ws_cfg = (self.config.get("trading") or {}).get("clob_ws") or {}
        channel = str(ws_cfg.get("book_channel", "market"))
        interval = float(ws_cfg.get("subscribe_interval_sec", 15))
        await asyncio.sleep(3)
        while self.running:
            try:
                await self._sync_clob_ws_book_subscriptions(channel)
            except Exception as e:
                logging.debug("clob ws subscription loop: %s", e)
            await asyncio.sleep(max(5.0, interval))

    def _spawn_bg(self, coro, name: str = ""):
        """Create a tracked background task.

        Keeps a strong reference (so the task can't be garbage-collected before it
        finishes) and attaches a done-callback that logs any exception — otherwise
        fire-and-forget tasks fail silently (surfacing only as "Task exception was
        never retrieved" at GC time).
        """
        task = asyncio.create_task(coro)
        label = name or getattr(coro, "__name__", "") or "bg_task"
        self._bg_tasks.add(task)

        def _on_done(t: "asyncio.Task") -> None:
            self._bg_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logging.error(
                    "Background task '%s' failed: %s", label, exc, exc_info=exc
                )

        task.add_done_callback(_on_done)
        return task

    async def start(self):
        """Start the trading bot"""
        self.running = True
        _write_runtime_status(
            phase="bot_start",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
            extra={"mode": "paper" if self.config.get("trading", {}).get("dry_run", True) else "live"},
        )
        logging.info("=" * 50)
        logging.info("PolyBot AI Starting...")
        logging.info(
            f"Dry Run Mode: {self.config.get('trading', {}).get('dry_run', True)}"
        )
        logging.info("=" * 50)

        # Fire-and-forget: narrate the previous session into the new session dir.
        # Off the trading hot path; never awaited.
        self._spawn_bg(self._run_startup_narrators())
        # Ghost-settle reads the full (~GB) rejected/settled calibration logs.
        # Do NOT block the trading loop or dashboard on it at boot: background it.
        # It also runs every cycle (_unified_cycle) and self-heals, so a
        # backgrounded first pass changes only timing, never settle correctness.
        self._spawn_bg(
            asyncio.to_thread(self._refresh_ghost_calibration_state, force=True)
        )

        # Start the async-decoupled AI decision broker. After this returns,
        # strategies can enqueue/lookup decisions via self.ai_broker.
        if self.ai_broker is not None:
            await self.ai_broker.start()

        ws_cfg = (self.config.get("trading") or {}).get("clob_ws") or {}
        if ws_cfg.get("enabled", True):
            self._spawn_bg(self.ws_client.listen())
            self._spawn_bg(self._clob_ws_subscription_loop())

        from src.ops_pulse import log_ops_startup

        log_ops_startup(self)

        # Notify started (optional — off by default to reduce Discord noise)
        if getattr(self.notifier, "alert_on_status", False):
            await self.notifier.notify_status(
                {"positions": 0, "daily_pnl": 0, "trades_today": 0, "running": True}
            )

        # Single trading loop (scan, legacy strategies if enabled, crypto, resolution) + daily coach
        _write_runtime_status(
            phase="loops_running",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
        )
        await asyncio.gather(
            self._unified_trading_loop(),
            self._daily_coach_loop(),
        )

        # Cleanup
        await self.shutdown()

        # Force exit — uvicorn daemon thread may hold process on Windows
        import os as _os

        _os._exit(0)

    async def _unified_trading_loop(self):
        """Single loop: one scan per cycle_interval, exits + optional legacy + crypto + resolution."""
        await asyncio.sleep(30)
        while self.running:
            try:
                self._unified_cycle_count += 1
                cycle_started = time.monotonic()
                await self._unified_cycle()
                elapsed = time.monotonic() - cycle_started
                sleep_for = _compute_trading_cycle_sleep(
                    self.scan_interval,
                    elapsed,
                    self.overrun_recovery_sleep_sec,
                )
                if elapsed > self.scan_interval:
                    logging.warning(
                        "Trading cycle overran configured interval: elapsed=%.1fs interval=%ss recovery_sleep=%.1fs",
                        elapsed,
                        self.scan_interval,
                        sleep_for,
                    )
                await asyncio.sleep(sleep_for)
            except Exception as e:
                logging.error(f"Error in trading cycle: {e}", exc_info=True)
                try:
                    await self.notifier.notify_error(str(e))
                except Exception as notify_err:
                    logging.error(f"Failed to send error notification: {notify_err}")
                await asyncio.sleep(30)

    async def _daily_coach_loop(self):
        """Run the strategy coach once per day at UTC 06:00 to analyze yesterday's trades."""
        import subprocess
        while self.running:
            try:
                now = datetime.now(timezone.utc)
                # Target 06:00 UTC daily
                next_run = now.replace(hour=6, minute=0, second=0, microsecond=0)
                if next_run <= now:
                    next_run += timedelta(days=1)
                wait_sec = (next_run - now).total_seconds()
                logging.info(f"[coach] Next analysis run in {wait_sec/3600:.1f}h (UTC 06:00)")
                await asyncio.sleep(wait_sec)

                if not self.running:
                    break
                logging.info("[coach] Running daily strategy analysis...")
                proc = await asyncio.create_subprocess_exec(
                    "python", "scripts/strategy_coach.py", "--days-back", "30",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
                output = stdout.decode(errors="replace") if stdout else ""
                logging.info(f"[coach] Analysis complete:\n{output[-2000:]}")
            except asyncio.TimeoutError:
                logging.warning("[coach] Daily analysis timed out after 5 minutes")
            except Exception as e:
                logging.error(f"[coach] Daily analysis error: {e}", exc_info=True)

    def _get_exposure_manager_for(self, strategy: str):
        """Return the correct exposure manager for a given strategy."""
        if strategy == "bitcoin":
            return getattr(self, "btc_exposure_manager", None)
        elif strategy == "sol_macro":
            return getattr(self, "sol_exposure_manager", None)
        elif strategy == "eth_macro":
            return getattr(self, "eth_exposure_manager", None)
        elif strategy == "hype_macro":
            return getattr(self, "hype_exposure_manager", None)
        elif strategy == "xrp_macro":
            return getattr(self, "xrp_exposure_manager", None)
        elif strategy == "doge_macro":
            return getattr(self, "doge_exposure_manager", None)
        elif strategy == "bnb_macro":
            return getattr(self, "bnb_exposure_manager", None)
        return None

    def _log_closed_trade_for_calibration(self, trade_id: str) -> None:
        """Write a calibration-log row + update lane posteriors for one closed trade.

        Best-effort; never raises into the caller. Used by both the take-profit/
        stop-loss exit path and the market-resolution exit path so trades.jsonl
        never undercounts vs the paper-session summary.
        """
        try:
            closed_row = next(
                (
                    t
                    for t in reversed(self.journal.closed_trades)
                    if t.get("trade_id") == trade_id
                ),
                None,
            )
            if closed_row is None:
                return
            record = build_record_from_closed_trade(
                closed_row, session_id=self.journal.session_id
            )
            cal = getattr(self, "lane_calibrator", None)
            if cal is not None:
                try:
                    snap = cal.record(
                        lane_id=record.get("lane_id") or "",
                        stated_est_prob=record.get("stated_est_prob"),
                        realized_pct=record.get("realized_pct") or 0.0,
                        win=bool(record.get("win")),
                    )
                    record["posterior_n"] = snap.get("n")
                    record["posterior_mean"] = round(
                        snap.get("beta_mean") or 0.0, 6
                    )
                    record["alpha_used"] = round(snap.get("alpha") or 1.0, 6)
                    record["alpha_raw"] = (
                        round(snap["alpha_raw"], 6)
                        if snap.get("alpha_raw") is not None
                        else None
                    )
                    record["shadow_mode"] = bool(cal.shadow_mode)
                except Exception as _pe:  # noqa: BLE001
                    logging.warning("lane_calibrator.record skipped: %s", _pe)
            append_calibration_record(record)
        except Exception as _cal_exc:  # noqa: BLE001 — telemetry only
            logging.warning("calibration_log skipped: %s", _cal_exc)

    def _apply_realized_pnl_to_bankroll(self, pnl: float) -> float:
        """Apply realized PnL to paper/live bankroll with a hard floor at zero."""
        self.bankroll = max(0.0, float(self.bankroll) + float(pnl))
        self.risk_manager.update_pnl(float(pnl))
        self.risk_manager.bankroll = self.bankroll
        self._sync_exposure_managers_portfolio_pnl()
        return self.bankroll

    def _all_exposure_managers(self):
        names = (
            "btc_exposure_manager",
            "sol_exposure_manager",
            "eth_exposure_manager",
            "hype_exposure_manager",
            "xrp_exposure_manager",
            "doge_exposure_manager",
            "bnb_exposure_manager",
        )
        out = []
        for name in names:
            mgr = getattr(self, name, None)
            if mgr is not None:
                out.append(mgr)
        return out

    def _sync_exposure_managers_portfolio_pnl(self) -> None:
        """Keep lane exposure managers aligned to current daily realized PnL."""
        daily_pnl = float(getattr(self.risk_manager, "daily_pnl", 0.0) or 0.0)
        for em in self._all_exposure_managers():
            try:
                em.update_portfolio_pnl(daily_pnl)
            except Exception:
                continue

    async def _run_resolution_check(self, label: str = ""):
        """Shared resolution check — routes settlements to the correct exposure manager."""
        # We pass exposure_manager=None so the tracker doesn't call record_trade.
        # We'll route it ourselves afterward.
        settled = await asyncio.to_thread(
            self.resolution_tracker.check_and_settle,
            journal=self.journal,
            risk_manager=self.risk_manager,
            exposure_manager=None,  # we route manually below
            bankroll=self.bankroll,
            ctf_redeemer=self.ctf_redeemer,
        )
        if settled:
            total_pnl = sum(s["pnl"] for s in settled)
            self._apply_realized_pnl_to_bankroll(total_pnl)

            # Route each settlement to the same lane/performance trackers used by live exits.
            for s in settled:
                strat = s.get("strategy", "")
                em = self._get_exposure_manager_for(strat)
                window = str(s.get("window_size") or "") or _detect_window_from_question(
                    str(s.get("market_question") or "")
                )
                if em is not None:
                    em.record_trade(
                        pnl=s["pnl"],
                        strategy=strat,
                        market_id=s.get("market_id", ""),
                        window_size=window,
                    )
                self.kelly_sizer.record_outcome(strat, s["pnl"] > 0, window)
                # Phase 0 calibration log + Phase 6 posterior update for
                # market-resolution exits. Without this, trades.jsonl
                # silently undercounts vs the paper-session summary.
                self._log_closed_trade_for_calibration(s.get("trade_id", ""))

            crypto_settled = [
                s
                for s in settled
                if s.get("strategy")
                in (
                    "bitcoin",
                    "sol_macro",
                    "eth_macro",
                    "hype_macro",
                    "xrp_macro",
                    "doge_macro",
                    "bnb_macro",
                )
            ]
            event_settled = [
                s
                for s in settled
                if s.get("strategy")
                not in (
                    "bitcoin",
                    "sol_macro",
                    "eth_macro",
                    "hype_macro",
                    "xrp_macro",
                    "doge_macro",
                    "bnb_macro",
                )
            ]
            if crypto_settled:
                crypto_pnl = sum(s["pnl"] for s in crypto_settled)
                logging.info(
                    f"{label} Settled {len(crypto_settled)} crypto positions, "
                    f"PnL=${crypto_pnl:+.2f}, bankroll=${self.bankroll:,.2f}"
                )
            if event_settled:
                event_pnl = sum(s["pnl"] for s in event_settled)
                logging.info(
                    f"{label} Settled {len(event_settled)} event positions, "
                    f"PnL=${event_pnl:+.2f}, bankroll=${self.bankroll:,.2f}"
                )

        # Update live prices on open positions
        updated = await asyncio.to_thread(
            self.resolution_tracker.check_price_updates,
            self.journal, self.bankroll
        )
        if updated:
            logging.info(f"{label} Updated prices on {updated} open positions")

        # Snapshot
        self.journal.take_snapshot(self.bankroll)

    def _kill_switch_active(self) -> bool:
        """Return True if the manual global stop file exists (do not place new trades)."""
        return KILL_SWITCH_FILE.exists()

    def _load_session_traded_market_ids(self) -> Set[str]:
        """Markets already entered this session; used to prevent short-window re-entry."""
        market_ids: Set[str] = set()
        try:
            for pos in self.journal.get_open_positions():
                mid = str(pos.get("market_id") or "").strip()
                if mid:
                    market_ids.add(mid)
        except Exception:
            pass
        try:
            for trade in self.journal.get_closed_trades():
                mid = str(trade.get("market_id") or "").strip()
                if mid:
                    market_ids.add(mid)
        except Exception:
            pass
        try:
            for entry in self.journal.get_all_entries(limit=20000):
                if entry.get("event") != "ENTRY":
                    continue
                mid = str(entry.get("market_id") or "").strip()
                if mid:
                    market_ids.add(mid)
        except Exception:
            pass
        return market_ids

    def _check_session_market_reentry(
        self,
        *,
        strategy: str,
        market_id: str,
        market_question: str,
        lane_meta: Dict[str, Any],
        signal_reason: Optional[str],
    ) -> bool:
        """Block multiple entries into the same Polymarket market in one session."""
        mid = str(market_id or "").strip()
        if not mid:
            return True
        traded = getattr(self, "_session_traded_market_ids", None)
        if traded is None:
            traded = self._load_session_traded_market_ids()
            self._session_traded_market_ids = traded
        if mid not in traded:
            return True
        reason = "duplicate_session_market"
        self.journal.log_skip(
            mid,
            market_question,
            strategy,
            reason,
            self.bankroll,
            extra=self._lane_skip_extra(
                lane_meta=lane_meta,
                signal_reason=signal_reason,
                skip_reason=reason,
            ),
        )
        logging.warning(
            "%s blocked duplicate session market entry: market_id=%s question=%s",
            strategy,
            mid,
            market_question[:80],
        )
        return False

    def _remember_session_market_entry(self, market_id: str) -> None:
        mid = str(market_id or "").strip()
        if not mid:
            return
        traded = getattr(self, "_session_traded_market_ids", None)
        if traded is None:
            traded = self._load_session_traded_market_ids()
            self._session_traded_market_ids = traded
        traded.add(mid)

    def _check_circuit_breakers(
        self,
        *,
        strategy: str,
        action: str,
        market_id: str,
        market_question: str,
        signal_reason: Optional[str],
        lane_meta: Dict[str, Any],
        btc_price: Optional[float] = None,
    ) -> bool:
        if not hasattr(self, "circuit_breakers"):
            self.circuit_breakers = CircuitBreakerManager(self.config)
        decision = self.circuit_breakers.can_enter(
            action=action,
            active_positions=self.risk_manager.active_positions.values(),
            btc_price=btc_price,
        )
        if decision.allowed:
            return True
        reason = decision.reason or "circuit_breaker_halt"
        logging.warning(
            "%s circuit breaker blocked %s: %s",
            strategy,
            action,
            reason,
        )
        self.journal.log_skip(
            market_id,
            market_question,
            strategy,
            reason,
            self.bankroll,
            extra=self._lane_skip_extra(
                lane_meta=lane_meta,
                signal_reason=signal_reason,
                skip_reason=reason,
            ),
        )
        return False

    async def _handle_exit_decision(self, exit_decision: ExitDecision) -> None:
        """Exit order + journal + risk updates (serialized with other execution)."""
        async with self._execution_lock:
            try:
                order = await self.clob_client.place_order(
                    token_id=exit_decision.token_id,
                    side=exit_decision.action,
                    price=exit_decision.exit_price,
                    size=exit_decision.size,
                    market_id=exit_decision.market_id,
                    dry_run=self.config.get("trading", {}).get("dry_run", True),
                )
                if order:
                    logging.info(
                        f"EXIT {exit_decision.reason}: {exit_decision.position_id[:12]} "
                        f"PnL=${exit_decision.unrealized_pnl:+.2f}"
                    )
                    pos = self.risk_manager.active_positions.get(
                        exit_decision.position_id
                    )
                    strat = getattr(pos, "strategy", "unknown") if pos else "unknown"
                    mq = getattr(pos, "market_question", "N/A") if pos else "N/A"
                    breaker_action = (
                        self.circuit_breakers.action_from_position(pos) if pos else ""
                    )
                    entry_price_snap = (
                        float(getattr(pos, "entry_price", 0) or 0) if pos else 0.0
                    )
                    _tid = str(exit_decision.position_id or "")
                    trade_id_tail = _tid[-14:] if _tid else ""
                    em = self._get_exposure_manager_for(strat)
                    window = str(getattr(pos, "window_size", "") or "") or _detect_window_from_question(mq)
                    exit_pnl = exit_decision.unrealized_pnl
                    self._apply_realized_pnl_to_bankroll(exit_pnl)
                    if em is not None:
                        em.record_trade(
                            pnl=exit_pnl,
                            strategy=strat,
                            market_id=exit_decision.market_id,
                            window_size=window,
                        )
                    self.kelly_sizer.record_outcome(strat, exit_pnl > 0, window)
                    self.journal.log_exit(
                        trade_id=exit_decision.position_id,
                        exit_price=exit_decision.exit_price,
                        bankroll=self.bankroll,
                        reason=exit_decision.reason,
                    )
                    self.circuit_breakers.record_exit(
                        reason=exit_decision.reason,
                        action=breaker_action,
                    )
                    self._log_closed_trade_for_calibration(exit_decision.position_id)
                    if exit_decision.position_id in self.risk_manager.active_positions:
                        del self.risk_manager.active_positions[
                            exit_decision.position_id
                        ]
                    await self.notifier.notify_exit(
                        {
                            "question": mq,
                            "strategy": strat,
                            "pnl": exit_pnl,
                            "reason": exit_decision.reason,
                            "price": exit_decision.exit_price,
                            "side": exit_decision.action,
                            "size": exit_decision.size,
                            "market_id": exit_decision.market_id,
                            "entry_price": entry_price_snap,
                            "trade_id_tail": trade_id_tail,
                        }
                    )
            except Exception as e:
                logging.error(
                    f"Exit order failed for {exit_decision.position_id}: {e}"
                )

    async def _unified_cycle(self):
        """One scan per interval: TP/SL exits, crypto strategies, resolution.

        No separate fast loop — `trading.cycle_interval_sec` controls cadence (default 120s).
        """
        logging.info("Starting trading cycle...")
        _write_runtime_status(
            phase="cycle_start",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
            extra={"cycle_count": int(self._unified_cycle_count or 0)},
        )

        from src.ops_pulse import _scan_skip_digest, _side_selection_digest, log_ops_pulse

        if self._kill_switch_active():
            logging.warning(
                "Manual global stop active (data/KILL_SWITCH present). Skipping trading cycle."
            )
            log_ops_pulse(self, "main")
            for st in (
                "bitcoin",
                "sol_macro",
                "eth_macro",
                "hype_macro",
                "xrp_macro",
                "xrp_dump_hedge",
            ):
                self._spawn_bg(
                    self.notifier.notify_kill_global(st, "manual global stop")
                )
            return

        self._performance_feedback_cycle += 1
        _pf = self.config.get("performance_feedback") or {}
        _n = max(1, int(_pf.get("refresh_every_n_cycles", 1)))
        if bool(_pf.get("enabled", False)) and (self._performance_feedback_cycle % _n == 0):
            try:
                from src.execution.performance_feedback import refresh_performance_feedback

                _jp = getattr(self.journal, "session_dir", None)
                _jp_path = (_jp / "entries.jsonl") if _jp else None
                refresh_performance_feedback(self.config, journal_path=_jp_path)
            except Exception as e:
                logging.warning(
                    "performance_feedback refresh failed: %s", e, exc_info=True
                )

        _write_runtime_status(
            phase="scanner_sync",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
        )
        opportunities = await self.market_scanner.scan_for_opportunities()
        high_liquidity = opportunities.get("high_liquidity", [])
        scanner_meta = opportunities.get("scanner_meta", {})
        if scanner_meta:
            self.last_ai_scan_stats["scanner"] = dict(scanner_meta)
            logging.info(
                "[TRADING] Scanner lookahead: 15m=%s 5m=%s 1h=%s | counts: 15m=%s 5m=%s 1h=%s hype_alt=%s",
                scanner_meta.get("look_ahead_15m"),
                scanner_meta.get("look_ahead_5m"),
                scanner_meta.get("look_ahead_1h"),
                scanner_meta.get("updown_15m_count"),
                scanner_meta.get("updown_5m_count"),
                scanner_meta.get("updown_1h_count"),
                scanner_meta.get("updown_hype_alt_count"),
            )

        if not high_liquidity:
            logging.info("No high liquidity markets found")
            log_ops_pulse(self, "main")
            return

        # Filter out markets we already have positions in (avoid duplicates)
        held_market_ids = set()
        for pos in self.risk_manager.active_positions.values():
            held_market_ids.add(pos.market_id)
        for pos in self.journal.get_open_positions():
            held_market_ids.add(pos.get("market_id", ""))

        # Check active positions for exit conditions (TP/SL/time)
        try:
            market_prices = {m.id: m.yes_price for m in high_liquidity}
            market_token_ids = {
                m.id: (m.token_id_yes, m.token_id_no) for m in high_liquidity
            }
            exits = self.exit_manager.check_exits(
                self.risk_manager.active_positions, market_prices, market_token_ids
            )
            for exit_decision in exits:
                await self._handle_exit_decision(exit_decision)
        except Exception as e:
            logging.error(f"Exit check error: {e}")

        available_markets = [m for m in high_liquidity if m.id not in held_market_ids]
        short_horizon = _filter_short_horizon(available_markets, self.config)
        self._unified_cycle_count = max(1, int(self._unified_cycle_count or 0))
        include_hourly_crypto = _should_include_hourly_crypto_markets(
            self.config,
            self._unified_cycle_count,
        )
        strategy_markets = _filter_crypto_hourly_markets(
            short_horizon,
            include_hourly=include_hourly_crypto,
        )
        logging.info(
            "Markets: %d total, %d held, %d available, %d in resolution window, %d strategy-scan | hourly_crypto=%s cycle=%d",
            len(high_liquidity),
            len(held_market_ids),
            len(available_markets),
            len(short_horizon),
            len(strategy_markets),
            "included" if include_hourly_crypto else "skipped",
            self._unified_cycle_count,
        )

        # Crypto: Bitcoin, SOL/ETH/HYPE macro, XRP macro
        open_positions_snapshot = list(self.risk_manager.active_positions.values())
        self.bitcoin_strategy._open_positions_snapshot = open_positions_snapshot
        self.sol_macro_strategy._open_positions_snapshot = open_positions_snapshot

        strategy_tasks: list[Any] = [
            _time_strategy_scan(
                "bitcoin",
                self.bitcoin_strategy.scan_and_analyze(
                    markets=strategy_markets,
                    bankroll=self.bankroll,
                ),
            ),
            _time_strategy_scan(
                "sol_macro",
                self.sol_macro_strategy.scan_and_analyze(
                    markets=strategy_markets,
                    bankroll=self.bankroll,
                ),
            ),
        ]

        eth_macro_cfg = self.config.get("strategies", {}).get("eth_macro", {})
        if eth_macro_cfg.get("enabled", False):
            self.eth_macro_strategy._open_positions_snapshot = open_positions_snapshot
            strategy_tasks.append(
                _time_strategy_scan(
                    "eth_macro",
                    self.eth_macro_strategy.scan_and_analyze(
                        markets=strategy_markets,
                        bankroll=self.bankroll,
                    ),
                )
            )

        hype_macro_cfg = self.config.get("strategies", {}).get("hype_macro", {})
        if hype_macro_cfg.get("enabled", False):
            self.hype_macro_strategy._open_positions_snapshot = open_positions_snapshot
            strategy_tasks.append(
                _time_strategy_scan(
                    "hype_macro",
                    self.hype_macro_strategy.scan_and_analyze(
                        markets=strategy_markets,
                        bankroll=self.bankroll,
                    ),
                )
            )

        xrp_cfg = self.config.get("strategies", {}).get("xrp_macro", {})
        if xrp_cfg.get("enabled", False) and self.xrp_macro_strategy:
            self.xrp_macro_strategy._open_positions_snapshot = open_positions_snapshot
            strategy_tasks.append(
                _time_strategy_scan(
                    "xrp_macro",
                    self.xrp_macro_strategy.scan_and_analyze(
                        markets=strategy_markets,
                        bankroll=self.bankroll,
                    ),
                )
            )

        doge_cfg = self.config.get("strategies", {}).get("doge_macro", {})
        if doge_cfg.get("enabled", False) and self.doge_macro_strategy:
            self.doge_macro_strategy._open_positions_snapshot = open_positions_snapshot
            strategy_tasks.append(
                _time_strategy_scan(
                    "doge_macro",
                    self.doge_macro_strategy.scan_and_analyze(
                        markets=strategy_markets,
                        bankroll=self.bankroll,
                    ),
                )
            )

        bnb_cfg = self.config.get("strategies", {}).get("bnb_macro", {})
        if bnb_cfg.get("enabled", False) and self.bnb_macro_strategy:
            self.bnb_macro_strategy._open_positions_snapshot = open_positions_snapshot
            strategy_tasks.append(
                _time_strategy_scan(
                    "bnb_macro",
                    self.bnb_macro_strategy.scan_and_analyze(
                        markets=strategy_markets,
                        bankroll=self.bankroll,
                    ),
                )
            )

        scan_started = time.perf_counter()
        _write_runtime_status(
            phase="strategy_scans_running",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
            extra={"strategy_task_count": len(strategy_tasks)},
        )
        scan_results = await asyncio.gather(
            *strategy_tasks,
            return_exceptions=True,
        )
        strategy_signals: dict[str, Any] = {}
        strategy_scan_timings_ms: dict[str, int] = {}
        strategy_errors: dict[str, Exception] = {}
        for result in scan_results:
            if isinstance(result, Exception):
                strategy_errors[f"task_{len(strategy_errors) + 1}"] = result
                continue
            name, payload, elapsed_ms, ok = result
            strategy_scan_timings_ms[name] = elapsed_ms
            if ok:
                strategy_signals[name] = payload
            else:
                strategy_signals[name] = payload
                strategy_errors[name] = payload
        logging.info(
            "[TRADING] Crypto parallel scan phase complete in %dms (%d strategies) timings_ms=%s",
            int((time.perf_counter() - scan_started) * 1000),
            len(strategy_tasks),
            strategy_scan_timings_ms,
        )
        if strategy_errors:
            logging.warning(
                "[TRADING] Strategy scan task errors encountered: %s",
                {name: type(err).__name__ for name, err in strategy_errors.items()},
            )

        try:
            btc_signals = strategy_signals.get("bitcoin", [])
            if isinstance(btc_signals, Exception):
                raise btc_signals
            _now_iso = datetime.now().isoformat(timespec="seconds")
            self.last_signal_counts["bitcoin"] = len(btc_signals)
            self.last_cycle_times["bitcoin"] = _now_iso
            self.cumulative_signal_counts["bitcoin"] = (
                self.cumulative_signal_counts.get("bitcoin", 0) + len(btc_signals)
            )
            self.last_ai_scan_stats["bitcoin"] = dict(
                getattr(self.bitcoin_strategy, "last_scan_stats", {}) or {}
            )
            self.last_buy_no_skip_counts["bitcoin"] = dict(
                self.last_ai_scan_stats["bitcoin"].get("buy_no_skip_counts", {}) or {}
            )
            self.last_buy_no_skip_samples["bitcoin"] = dict(
                self.last_ai_scan_stats["bitcoin"].get("last_buy_no_skip_sample", {}) or {}
            )
            for signal in btc_signals:
                await self._execute_bitcoin_signal(signal)
            if btc_signals:
                logging.info(f"[TRADING] Crypto BTC: {len(btc_signals)} signals")
            else:
                logging.info("[TRADING] Crypto BTC: No signals this cycle")
            _btc_stats = self.last_ai_scan_stats.get("bitcoin", {})
            if _btc_stats:
                logging.info(
                    "[TRADING] BTC diagnostics: ai_calls=%s assists=%s vetos=%s holds=%s actions=%s top_skips=%s buy_no_skips=%s last_buy_no=%s",
                    _btc_stats.get("ai_calls", 0),
                    _btc_stats.get("ai_assists", 0),
                    _btc_stats.get("ai_vetos", 0),
                    _btc_stats.get("ai_holds", 0),
                    _btc_stats.get("action_counts", {}),
                    _btc_stats.get("top_skip_reasons", {}),
                    self.last_buy_no_skip_counts.get("bitcoin", {}),
                    self.last_buy_no_skip_samples.get("bitcoin", {}),
                )
        except Exception as e:
            logging.error(f"Crypto BTC error: {e}", exc_info=True)

        try:
            sol_signals = strategy_signals.get("sol_macro", [])
            if isinstance(sol_signals, Exception):
                raise sol_signals
            _now_iso = datetime.now().isoformat(timespec="seconds")
            self.last_signal_counts["sol_macro"] = len(sol_signals)
            self.last_cycle_times["sol_macro"] = _now_iso
            self.cumulative_signal_counts["sol_macro"] = (
                self.cumulative_signal_counts.get("sol_macro", 0) + len(sol_signals)
            )
            self.last_ai_scan_stats["sol_macro"] = dict(
                getattr(self.sol_macro_strategy, "last_scan_stats", {}) or {}
            )
            self.last_buy_no_skip_counts["sol_macro"] = dict(
                self.last_ai_scan_stats["sol_macro"].get("buy_no_skip_counts", {}) or {}
            )
            self.last_buy_no_skip_samples["sol_macro"] = dict(
                self.last_ai_scan_stats["sol_macro"].get("last_buy_no_skip_sample", {}) or {}
            )
            for signal in sol_signals:
                await self._execute_sol_macro_signal(signal)
            if sol_signals:
                logging.info(f"[TRADING] Crypto SOL: {len(sol_signals)} signals")
            else:
                logging.info("[TRADING] Crypto SOL: No signals this cycle")
            _sol_stats = self.last_ai_scan_stats.get("sol_macro", {})
            if _sol_stats:
                logging.info(
                    "[TRADING] SOL diagnostics: actions=%s side_sources=%s top_skips=%s buy_no_skips=%s last_buy_no=%s",
                    _sol_stats.get("action_counts", {}),
                    _sol_stats.get("side_source_counts", {}),
                    _sol_stats.get("top_skip_reasons", {}),
                    self.last_buy_no_skip_counts.get("sol_macro", {}),
                    self.last_buy_no_skip_samples.get("sol_macro", {}),
                )
        except Exception as e:
            logging.error(f"Crypto SOL error: {e}", exc_info=True)

        try:
            if "eth_macro" in strategy_signals:
                eth_signals = strategy_signals["eth_macro"]
                if isinstance(eth_signals, Exception):
                    raise eth_signals
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["eth_macro"] = len(eth_signals)
                self.last_cycle_times["eth_macro"] = _now_iso
                self.cumulative_signal_counts["eth_macro"] = (
                    self.cumulative_signal_counts.get("eth_macro", 0) + len(eth_signals)
                )
                self.last_ai_scan_stats["eth_macro"] = dict(
                    getattr(self.eth_macro_strategy, "last_scan_stats", {}) or {}
                )
                self.last_buy_no_skip_counts["eth_macro"] = dict(
                    self.last_ai_scan_stats["eth_macro"].get("buy_no_skip_counts", {}) or {}
                )
                self.last_buy_no_skip_samples["eth_macro"] = dict(
                    self.last_ai_scan_stats["eth_macro"].get("last_buy_no_skip_sample", {}) or {}
                )
                for signal in eth_signals:
                    await self._execute_sol_macro_signal(signal)
                if eth_signals:
                    logging.info(f"[TRADING] Crypto ETH: {len(eth_signals)} signals")
                else:
                    logging.info("[TRADING] Crypto ETH: No signals this cycle")
                _eth_stats = self.last_ai_scan_stats.get("eth_macro", {})
                if _eth_stats:
                    logging.info(
                        "[TRADING] ETH diagnostics: actions=%s side_sources=%s top_skips=%s buy_no_skips=%s last_buy_no=%s",
                        _eth_stats.get("action_counts", {}),
                        _eth_stats.get("side_source_counts", {}),
                        _eth_stats.get("top_skip_reasons", {}),
                        self.last_buy_no_skip_counts.get("eth_macro", {}),
                        self.last_buy_no_skip_samples.get("eth_macro", {}),
                    )
        except Exception as e:
            logging.error(f"Crypto ETH error: {e}", exc_info=True)

        try:
            if "hype_macro" in strategy_signals:
                hype_signals = strategy_signals["hype_macro"]
                if isinstance(hype_signals, Exception):
                    raise hype_signals
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["hype_macro"] = len(hype_signals)
                self.last_cycle_times["hype_macro"] = _now_iso
                self.cumulative_signal_counts["hype_macro"] = (
                    self.cumulative_signal_counts.get("hype_macro", 0) + len(hype_signals)
                )
                self.last_ai_scan_stats["hype_macro"] = dict(
                    getattr(self.hype_macro_strategy, "last_scan_stats", {}) or {}
                )
                self.last_buy_no_skip_counts["hype_macro"] = dict(
                    self.last_ai_scan_stats["hype_macro"].get("buy_no_skip_counts", {}) or {}
                )
                self.last_buy_no_skip_samples["hype_macro"] = dict(
                    self.last_ai_scan_stats["hype_macro"].get("last_buy_no_skip_sample", {}) or {}
                )
                for signal in hype_signals:
                    await self._execute_sol_macro_signal(signal)
                if hype_signals:
                    logging.info(f"[TRADING] Crypto HYPE: {len(hype_signals)} signals")
                else:
                    logging.info("[TRADING] Crypto HYPE: No signals this cycle")
                _hype_stats = self.last_ai_scan_stats.get("hype_macro", {})
                if _hype_stats:
                    logging.info(
                        "[TRADING] HYPE diagnostics: actions=%s side_sources=%s top_skips=%s buy_no_skips=%s last_buy_no=%s",
                        _hype_stats.get("action_counts", {}),
                        _hype_stats.get("side_source_counts", {}),
                        _hype_stats.get("top_skip_reasons", {}),
                        self.last_buy_no_skip_counts.get("hype_macro", {}),
                        self.last_buy_no_skip_samples.get("hype_macro", {}),
                    )
        except Exception as e:
            logging.error(f"Crypto HYPE error: {e}", exc_info=True)

        try:
            if "xrp_macro" in strategy_signals:
                xrp_signals = strategy_signals["xrp_macro"]
                if isinstance(xrp_signals, Exception):
                    raise xrp_signals
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["xrp_macro"] = len(xrp_signals)
                self.last_cycle_times["xrp_macro"] = _now_iso
                self.cumulative_signal_counts["xrp_macro"] = (
                    self.cumulative_signal_counts.get("xrp_macro", 0) + len(xrp_signals)
                )
                self.last_ai_scan_stats["xrp_macro"] = dict(
                    getattr(self.xrp_macro_strategy, "last_scan_stats", {}) or {}
                )
                self.last_buy_no_skip_counts["xrp_macro"] = dict(
                    self.last_ai_scan_stats["xrp_macro"].get("buy_no_skip_counts", {}) or {}
                )
                self.last_buy_no_skip_samples["xrp_macro"] = dict(
                    self.last_ai_scan_stats["xrp_macro"].get("last_buy_no_skip_sample", {}) or {}
                )
                for signal in xrp_signals:
                    await self._execute_xrp_macro_signal(signal)
                if xrp_signals:
                    logging.info(f"[TRADING] Crypto XRP macro: {len(xrp_signals)} signals")
                else:
                    logging.info("[TRADING] Crypto XRP macro: No signals this cycle")
                _xrp_stats = self.last_ai_scan_stats.get("xrp_macro", {})
                if _xrp_stats:
                    logging.info(
                        "[TRADING] XRP diagnostics: actions=%s side_sources=%s top_skips=%s buy_no_skips=%s last_buy_no=%s",
                        _xrp_stats.get("action_counts", {}),
                        _xrp_stats.get("side_source_counts", {}),
                        _xrp_stats.get("top_skip_reasons", {}),
                        self.last_buy_no_skip_counts.get("xrp_macro", {}),
                        self.last_buy_no_skip_samples.get("xrp_macro", {}),
                    )
        except Exception as e:
            logging.error(f"Crypto XRP macro error: {e}", exc_info=True)

        try:
            if "doge_macro" in strategy_signals:
                doge_signals = strategy_signals["doge_macro"]
                if isinstance(doge_signals, Exception):
                    raise doge_signals
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["doge_macro"] = len(doge_signals)
                self.last_cycle_times["doge_macro"] = _now_iso
                self.cumulative_signal_counts["doge_macro"] = (
                    self.cumulative_signal_counts.get("doge_macro", 0) + len(doge_signals)
                )
                self.last_ai_scan_stats["doge_macro"] = dict(
                    getattr(self.doge_macro_strategy, "last_scan_stats", {}) or {}
                )
                self.last_buy_no_skip_counts["doge_macro"] = dict(
                    self.last_ai_scan_stats["doge_macro"].get("buy_no_skip_counts", {}) or {}
                )
                self.last_buy_no_skip_samples["doge_macro"] = dict(
                    self.last_ai_scan_stats["doge_macro"].get("last_buy_no_skip_sample", {}) or {}
                )
                for signal in doge_signals:
                    await self._execute_sol_macro_signal(signal)
                if doge_signals:
                    logging.info(f"[TRADING] Crypto DOGE macro: {len(doge_signals)} signals")
                else:
                    logging.info("[TRADING] Crypto DOGE macro: No signals this cycle")
                _doge_stats = self.last_ai_scan_stats.get("doge_macro", {})
                if _doge_stats:
                    logging.info(
                        "[TRADING] DOGE diagnostics: actions=%s side_sources=%s top_skips=%s buy_no_skips=%s last_buy_no=%s",
                        _doge_stats.get("action_counts", {}),
                        _doge_stats.get("side_source_counts", {}),
                        _doge_stats.get("top_skip_reasons", {}),
                        self.last_buy_no_skip_counts.get("doge_macro", {}),
                        self.last_buy_no_skip_samples.get("doge_macro", {}),
                    )
        except Exception as e:
            logging.error(f"Crypto DOGE macro error: {e}", exc_info=True)

        try:
            if "bnb_macro" in strategy_signals:
                bnb_signals = strategy_signals["bnb_macro"]
                if isinstance(bnb_signals, Exception):
                    raise bnb_signals
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["bnb_macro"] = len(bnb_signals)
                self.last_cycle_times["bnb_macro"] = _now_iso
                self.cumulative_signal_counts["bnb_macro"] = (
                    self.cumulative_signal_counts.get("bnb_macro", 0) + len(bnb_signals)
                )
                self.last_ai_scan_stats["bnb_macro"] = dict(
                    getattr(self.bnb_macro_strategy, "last_scan_stats", {}) or {}
                )
                self.last_buy_no_skip_counts["bnb_macro"] = dict(
                    self.last_ai_scan_stats["bnb_macro"].get("buy_no_skip_counts", {}) or {}
                )
                self.last_buy_no_skip_samples["bnb_macro"] = dict(
                    self.last_ai_scan_stats["bnb_macro"].get("last_buy_no_skip_sample", {}) or {}
                )
                for signal in bnb_signals:
                    await self._execute_sol_macro_signal(signal)
                if bnb_signals:
                    logging.info(f"[TRADING] Crypto BNB macro: {len(bnb_signals)} signals")
                else:
                    logging.info("[TRADING] Crypto BNB macro: No signals this cycle")
                _bnb_stats = self.last_ai_scan_stats.get("bnb_macro", {})
                if _bnb_stats:
                    logging.info(
                        "[TRADING] BNB diagnostics: actions=%s side_sources=%s top_skips=%s buy_no_skips=%s last_buy_no=%s",
                        _bnb_stats.get("action_counts", {}),
                        _bnb_stats.get("side_source_counts", {}),
                        _bnb_stats.get("top_skip_reasons", {}),
                        self.last_buy_no_skip_counts.get("bnb_macro", {}),
                        self.last_buy_no_skip_samples.get("bnb_macro", {}),
                    )
        except Exception as e:
            logging.error(f"Crypto BNB macro error: {e}", exc_info=True)

        _write_runtime_status(
            phase="resolution_and_calibration",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
        )
        try:
            async with self._execution_lock:
                await self._run_resolution_check(label="[TRADING]")
        except Exception as e:
            logging.error(f"Resolution tracking error: {e}")

        try:
            await asyncio.to_thread(self._refresh_ghost_calibration_state)
        except Exception as e:
            logging.warning("Rejected-candidate tracker refresh failed: %s", e)

        positions = len(self.risk_manager.active_positions)
        daily = self.risk_manager.daily_trades
        trade_limit = self.risk_manager.effective_max_trades_per_day()
        logging.info(
            f"Cycle complete. Positions: {positions}, Daily trades: {daily}/{trade_limit}"
        )
        _write_runtime_status(
            phase="cycle_complete",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
            extra={"open_positions": positions, "daily_trades": daily},
        )
        self._append_scan_diagnostics_annotation(
            scan_skip_digest=_scan_skip_digest(self.last_ai_scan_stats),
            side_selection=_side_selection_digest(self.last_ai_scan_stats),
        )
        log_ops_pulse(self, "main")

        # AI broker housekeeping + diagnostic. Sweep expired/stale entries
        # and emit one line summarising broker state so we can see at a glance
        # whether decisions are landing in time.
        if self.ai_broker is not None:
            try:
                open_ids = {
                    *(p.market_id for p in self.risk_manager.active_positions.values()),
                    *(p.get("market_id", "") for p in self.journal.get_open_positions()),
                }
                self.ai_broker.sweep_expired(open_ids)
                _bs = self.ai_broker.stats()
                logging.info(
                    "ai_broker pending=%d inflight=%d resolved=%d consumed=%d expired=%d "
                    "failed=%d rejected=%d oldest=%.1fs alive=%s",
                    _bs["pending"], _bs["inflight"], _bs["resolved_alive"],
                    _bs["consumed"], _bs["expired"], _bs["failed"],
                    _bs["rejected_overflow"], _bs["oldest_age_sec"], _bs["worker_alive"],
                )
            except Exception:
                logging.exception("ai_broker housekeeping failed")

    def _append_scan_diagnostics_annotation(
        self,
        *,
        scan_skip_digest: Dict[str, Any],
        side_selection: Dict[str, Any],
    ) -> None:
        """Persist no-entry scan reasons so session review includes silent lanes."""
        try:
            if not getattr(self, "journal", None):
                return
            compact_stats: Dict[str, Any] = {}
            for strategy, stats in (self.last_ai_scan_stats or {}).items():
                if not isinstance(stats, dict):
                    continue
                compact_stats[strategy] = {
                    "enabled": stats.get("enabled"),
                    "signals": stats.get("signals"),
                    "markets_considered": stats.get("markets_considered")
                    or stats.get("btc_markets_considered"),
                    "allowed_side": stats.get("allowed_side"),
                    "action_counts": stats.get("action_counts") or {},
                    "side_source_counts": stats.get("side_source_counts") or {},
                    "top_skip_reasons": stats.get("top_skip_reasons") or {},
                }
            if not compact_stats:
                return
            cycle = int(self._unified_cycle_count or 0)
            blocked = {
                strategy: data
                for strategy, data in compact_stats.items()
                if int(data.get("signals") or 0) == 0 and data.get("top_skip_reasons")
            }
            text = (
                f"Cycle {cycle} scan diagnostics: "
                f"{len(blocked)} no-signal strategies had recorded skip reasons."
            )
            self.journal.append_annotation(
                trade_id=f"__scan_diagnostics__::{cycle}",
                text=text,
                strategy="scan_diagnostics",
                extra={
                    "source": "scan_diagnostics",
                    "cycle": cycle,
                    "last_signal_counts": dict(self.last_signal_counts),
                    "cumulative_signal_counts": dict(self.cumulative_signal_counts),
                    "scan_skip_digest": scan_skip_digest,
                    "side_selection": side_selection,
                    "per_strategy": compact_stats,
                },
            )
        except Exception as e:
            logging.warning("scan diagnostics journal annotation failed: %s", e)

    def _build_correlation_context(self, *, current_strategy: str, current_action: str) -> str:
        """Compact one-line summary of currently-open positions for the post-trade
        annotation prompt. Used when ai.post_trade_annotation.include_correlation_check
        is true so the AI can flag highly correlated exposure."""
        try:
            positions = list(self.risk_manager.active_positions.values())
        except Exception:
            return ""
        if not positions:
            return ""
        parts = []
        for p in positions[:20]:
            strat = getattr(p, "strategy", "?")
            action = getattr(p, "action", "?")
            mq = (getattr(p, "market_question", "") or "")[:40]
            parts.append(f"{strat}/{action}:{mq}")
        return f"Open positions ({len(positions)}): " + " | ".join(parts)

    async def _annotate_entry_async(
        self,
        *,
        trade_id: str,
        market_id: str,
        market_question: str,
        strategy: str,
        action: str,
        edge: float,
        confidence: float,
        yes_price: float,
    ) -> None:
        """Fire-and-forget post-trade AI annotation. Writes a short narrative
        into the journal as an ANNOTATION event. Never raises into the caller."""
        try:
            ai_cfg = (self.config.get("ai", {}) or {}).get("post_trade_annotation", {}) or {}
            if not ai_cfg.get("enabled", False):
                return
            if not getattr(self, "ai_agent", None) or not self.ai_agent.is_available():
                return
            timeout_s = float(ai_cfg.get("timeout_seconds", 20))
            include_corr = bool(ai_cfg.get("include_correlation_check", False))
            corr_ctx = ""
            if include_corr:
                corr_ctx = self._build_correlation_context(
                    current_strategy=strategy, current_action=action
                )
            prompt = (
                f"Trade just entered: {strategy} {action} on '{market_question[:80]}'.\n"
                f"Edge={edge:.4f} confidence={confidence:.2f}.\n"
            )
            if corr_ctx:
                prompt += f"{corr_ctx}\n"
                prompt += (
                    "If this entry is highly correlated with existing exposure, "
                    "flag it explicitly in one sentence. "
                )
            prompt += (
                "In 3-5 sentences: thesis, expectations, key levels or invalidation."
            )
            try:
                result = await asyncio.wait_for(
                    self.ai_agent.analyze_market(
                        market_question=market_question,
                        market_description=prompt,
                        current_yes_price=yes_price,
                        market_id=market_id,
                        strategy_hint=f"{strategy}_postentry",
                    ),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                logging.debug("post-trade annotation timeout trade=%s", trade_id)
                return
            if not result or not getattr(result, "reasoning", None):
                return
            self.journal.append_annotation(
                trade_id=trade_id,
                text=str(result.reasoning),
                strategy=strategy,
                market_id=market_id,
                market_question=market_question,
                extra={
                    "source": "post_trade_annotation",
                    "ai_confidence": float(getattr(result, "confidence_score", 0.0) or 0.0),
                    "correlation_context": corr_ctx,
                },
            )
        except Exception as exc:
            logging.debug("post-trade annotation failed trade=%s: %s", trade_id, exc)

    @staticmethod
    def _annotation_yes_price(action: str, entry_price: float) -> float:
        """Return the YES mid expected by post-trade annotation."""
        price = float(entry_price or 0.5)
        return 1.0 - price if action == "BUY_NO" else price

    def _lane_skip_extra(
        self,
        *,
        lane_meta: Dict[str, Any],
        signal_reason: Optional[str] = None,
        skip_reason: Optional[str] = None,
        dry_run: Optional[bool] = None,
        matched_rule: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = dict(lane_meta or {})
        if signal_reason:
            payload["signal_reason"] = signal_reason
        if skip_reason:
            payload["skip_reason"] = skip_reason
        if dry_run is not None:
            payload["dry_run"] = bool(dry_run)
        if matched_rule:
            payload["lane_rule_match"] = matched_rule
        return payload

    def _check_lane_execution(
        self,
        *,
        strategy: str,
        signal_reason: str,
        lane_meta: Dict[str, Any],
        market_id: str,
        market_question: str,
    ) -> bool:
        lane_id = str(lane_meta.get("lane_id") or "").strip()
        dry_run = bool(self.config.get("trading", {}).get("dry_run", True))
        allowed, reason, state, matched_key = self.lane_manager.can_execute(lane_id, dry_run=dry_run)
        lane_meta["promotion_state"] = state
        if allowed:
            return True
        self.journal.log_skip(
            market_id,
            market_question,
            strategy,
            reason,
            self.bankroll,
            extra=self._lane_skip_extra(
                lane_meta=lane_meta,
                signal_reason=signal_reason,
                skip_reason=reason,
                dry_run=dry_run,
                matched_rule=matched_key,
            ),
        )
        logging.warning(
            "%s lane execution blocked: %s lane=%s state=%s match=%s",
            strategy,
            reason,
            lane_id or "unknown",
            state,
            matched_key or "<default>",
        )
        return False

    def _load_latest_market_regime_snapshot(self) -> Optional[Dict[str, Any]]:
        cfg = (
            (self.config.get("trading") or {}).get("market_regime_gate")
            if isinstance(self.config.get("trading"), dict)
            else {}
        ) or {}
        path = Path(cfg.get("regime_log") or DEFAULT_REGIME_LOG)
        if not path.exists():
            return None
        last: Optional[Dict[str, Any]] = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        last = obj
        except OSError as exc:
            logging.warning("market regime gate could not read %s: %s", path, exc)
            return None
        if not last:
            return None

        max_age_sec = float(cfg.get("max_snapshot_age_sec", 1800) or 1800)
        try:
            ts = datetime.fromisoformat(str(last.get("ts")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = abs((datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None
        if age > max_age_sec:
            return None
        last["regime_match_age_sec"] = round(age, 3)
        return last

    def _check_market_regime_execution(
        self,
        *,
        strategy: str,
        signal: Any,
        lane_meta: Dict[str, Any],
    ) -> bool:
        gate_config = ((self.config.get("trading") or {}).get("market_regime_gate") or {})
        latest = self._load_latest_market_regime_snapshot()
        allowed, reason, regime_extra = market_regime_gate_decision(
            gate_config=gate_config,
            latest_regime=latest,
            convergence_score=getattr(signal, "convergence_score", None),
        )
        if regime_extra:
            lane_meta.update(regime_extra)
        combined_regime = str(regime_extra.get("combined_regime") or "")
        in_deadzone = combined_regime.startswith("deadzone") if combined_regime else None
        em = self._get_exposure_manager_for(strategy)
        if em is not None and hasattr(em, "update_resume_window"):
            em.update_resume_window(
                green_window=bool(allowed),
                in_deadzone=in_deadzone,
            )
        if allowed:
            return True

        self.journal.log_skip(
            signal.market_id,
            signal.market_question,
            strategy,
            reason,
            self.bankroll,
            extra=self._lane_skip_extra(
                lane_meta=lane_meta,
                signal_reason=getattr(signal, "reason", None),
                skip_reason=reason,
            ),
        )
        logging.warning(
            "%s market-regime execution blocked: %s combined=%s convergence=%s threshold=%s",
            strategy,
            reason,
            regime_extra.get("combined_regime"),
            regime_extra.get("convergence_score"),
            regime_extra.get("deadzone_min_convergence"),
        )
        return False

    async def _execute_bitcoin_signal(self, signal: BitcoinSignal):
        """Execute a Bitcoin Up/Down trade signal."""
        async with self._execution_lock:
            await self._execute_bitcoin_signal_impl(signal)

    async def _execute_bitcoin_signal_impl(self, signal: BitcoinSignal):
        """Bitcoin entry (holds _execution_lock via caller)."""
        _entry_leg = "NO" if signal.action == "BUY_NO" else "YES"
        _entry_reason = f"btc_{signal.direction} ai={signal.ai_used}"
        lane_meta = build_lane_metadata(
            strategy="bitcoin",
            window_size=signal.window_size,
            action=signal.action,
            direction=signal.direction,
            entry_leg=_entry_leg,
            side_source=getattr(signal, "side_source", None),
            resolver_path=getattr(signal, "resolver_path", None),
            ai_used=bool(signal.ai_used),
            reason=_entry_reason,
            signal_reason=signal.reason,
            htf_bias=signal.htf_bias,
        )
        if not self._check_lane_execution(
            strategy="bitcoin",
            signal_reason=signal.reason,
            lane_meta=lane_meta,
            market_id=signal.market_id,
            market_question=signal.market_question,
        ):
            return
        if not self._check_market_regime_execution(
            strategy="bitcoin",
            signal=signal,
            lane_meta=lane_meta,
        ):
            return
        if not self._check_session_market_reentry(
            strategy="bitcoin",
            market_id=signal.market_id,
            market_question=signal.market_question,
            lane_meta=lane_meta,
            signal_reason=signal.reason,
        ):
            return
        if not self._check_circuit_breakers(
            strategy="bitcoin",
            action=signal.action,
            market_id=signal.market_id,
            market_question=signal.market_question,
            signal_reason=signal.reason,
            lane_meta=lane_meta,
            btc_price=getattr(signal, "btc_current", None),
        ):
            return
        can_trade, reason = self.risk_manager.can_trade(strategy="bitcoin")
        if not can_trade:
            logging.warning(f"Bitcoin trade risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "bitcoin",
                reason,
                self.bankroll,
                extra=self._lane_skip_extra(
                    lane_meta=lane_meta,
                    signal_reason=signal.reason,
                    skip_reason=reason,
                ),
            )
            return

        # Term-based risk check (crypto-isolated budget)
        can_trade, final_size, reason = self.risk_manager.evaluate_entry(
            end_date=signal.end_date,
            current_edge=signal.edge,
            bankroll=self.bankroll,
            strategy="bitcoin",
            requested_size=signal.size,
        )
        if not can_trade:
            logging.warning(f"Bitcoin trade term risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "bitcoin",
                f"term_risk: {reason}",
                self.bankroll,
                extra=self._lane_skip_extra(
                    lane_meta=lane_meta,
                    signal_reason=signal.reason,
                    skip_reason=f"term_risk: {reason}",
                ),
            )
            return

        if signal.action == "BUY_YES":
            token_id = signal.token_id_yes
            side = "BUY"
        elif signal.action == "BUY_NO":
            token_id = signal.token_id_no
            side = "BUY"
        elif signal.action == "SELL_YES":
            token_id = signal.token_id_yes
            side = "SELL"
        else:
            logging.error(
                f"Bitcoin skip: unexpected action {signal.action!r} "
                f"(expected BUY_YES, BUY_NO, or SELL_YES)"
            )
            return

        # ── T1-1: Unsellable token guard ─────────────────────────────────────
        # Before placing any order, verify the position can be exited.
        # BUY_YES / BUY_NO: test the token we are acquiring; SELL_YES tests YES.
        token_to_test = token_id
        if not await self.clob_client.can_sell_token(token_to_test, signal.market_id):
            logging.warning(
                f"Bitcoin unsellable-token skip '{signal.market_question[:40]}' "
                f"— token={token_to_test[:20]} has no bids"
            )
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "bitcoin",
                "unsellable_token",
                self.bankroll,
                extra=self._lane_skip_extra(
                    lane_meta=lane_meta,
                    signal_reason=signal.reason,
                    skip_reason="unsellable_token",
                ),
            )
            return

        logging.info(
            f"Executing BITCOIN trade: {signal.action} {final_size:.2f} @ {signal.price} ({signal.direction})"
        )
        if side == "BUY":
            order_size = final_size / max(0.01, signal.price)
        else:
            order_size = final_size / max(0.01, 1.0 - signal.price)
        pos_size = order_size

        order = await self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=signal.price,
            size=order_size,
            market_id=signal.market_id,
            post_only=True,
            dry_run=self.config.get("trading", {}).get("dry_run", True),
            order_outcome=("YES" if signal.action == "BUY_YES" else "NO"),
        )

        if order and hasattr(order, "order_id"):
            outcome = "YES" if signal.action == "BUY_YES" else "NO"
            position = Position(
                position_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                current_price=signal.price,
                pnl=0.0,
                opened_at=datetime.now(),
                end_date=signal.end_date,
                strategy="bitcoin",
                entry_leg=_entry_leg,
                window_size=str(getattr(signal, "window_size", "") or ""),
                token_id_yes=str(getattr(signal, "token_id_yes", "") or ""),
                token_id_no=str(getattr(signal, "token_id_no", "") or ""),
                edge=float(signal.edge or 0.0),
                confidence=float(signal.confidence or 0.0),
                entry_signal={
                    "window_size": signal.window_size,
                    "htf_bias": signal.htf_bias,
                    "btc_1h_regime": getattr(signal, "btc_1h_regime", None),
                    "side_source": getattr(signal, "side_source", None),
                    "conflict_type": getattr(signal, "conflict_type", None),
                    "resolver_path": getattr(signal, "resolver_path", None),
                    "htf_side": getattr(signal, "htf_side", None),
                    "quant_side": getattr(signal, "quant_side", None),
                    "momentum_side": getattr(signal, "momentum_side", None),
                    "convergence_score": getattr(signal, "convergence_score", None),
                    "entry_volatility": getattr(signal, "entry_volatility", None),
                    **lane_meta,
                },
            )
            self.risk_manager.add_position(position)
            self._remember_session_market_entry(signal.market_id)

            self.journal.log_entry(
                trade_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                strategy="bitcoin",
                action=signal.action,
                side=side,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                bankroll=self.bankroll,
                edge=signal.edge,
                confidence=signal.confidence,
                reason=_entry_reason,
                token_id_yes=str(getattr(signal, "token_id_yes", "") or ""),
                token_id_no=str(getattr(signal, "token_id_no", "") or ""),
                extra={
                    "hour_utc": signal.hour_utc,
                    "window_size": signal.window_size,
                    "htf_bias": signal.htf_bias,
                    "btc_1h_regime": getattr(signal, "btc_1h_regime", None),
                    "btc_htf": signal.htf_bias,   # alias expected by journal analysis
                    "macro_leg": None,             # bitcoin path has no alt-lag leg
                    "ai_used": signal.ai_used,
                    "ai_confidence": signal.confidence if signal.ai_used else None,
                    **self._ai_entry_attribution(ai_consulted=bool(signal.ai_used)),
                    "yes_price": (
                        round(1.0 - signal.price, 4)
                        if signal.action == "BUY_NO"
                        else float(signal.price)
                    ),
                    "btc_price": signal.btc_current,
                    "edge": signal.edge,
                    "est_prob": signal.est_prob,   # prob of YES at entry; key for edge validation
                    "raw_est_prob": getattr(signal, "raw_est_prob", signal.est_prob),
                    "rsi": signal.rsi,
                    "side_source": getattr(signal, "side_source", None),
                    "conflict_type": getattr(signal, "conflict_type", None),
                    "resolver_path": getattr(signal, "resolver_path", None),
                    "htf_side": getattr(signal, "htf_side", None),
                    "quant_side": getattr(signal, "quant_side", None),
                    "momentum_side": getattr(signal, "momentum_side", None),
                    "oracle_basis_bps": getattr(signal, "oracle_basis_bps", None),
                    "convergence_score": getattr(signal, "convergence_score", None),
                    "entry_volatility": getattr(signal, "entry_volatility", None),
                    "entry_policy": getattr(signal, "entry_policy", None),
                    "indicator_snapshot": getattr(signal, "indicator_snapshot", None),
                    "probability_model": "indicator_score_v1",
                    # Learning context: direction, threshold, and full signal reason
                    # so exit records can explain why a trade was entered.
                    "direction": signal.direction,
                    "btc_threshold": signal.btc_threshold,
                    "signal_reason": signal.reason,
                    **lane_meta,
                },
                market_end_at=signal.end_date,
                entry_leg=_entry_leg,
            )
            self._spawn_bg(self._annotate_entry_async(
                trade_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                strategy="bitcoin",
                action=signal.action,
                edge=float(signal.edge or 0.0),
                confidence=float(signal.confidence or 0.0),
                yes_price=self._annotation_yes_price(signal.action, signal.price),
            ))

            await self.notifier.notify_trade(
                {
                    "question": signal.market_question,
                    "side": side,
                    "outcome": outcome,
                    "size": final_size,
                    "price": signal.price,
                    "auto_execute": True,
                    "strategy": "bitcoin",
                }
            )

    async def _execute_sol_macro_signal(self, signal: SolMacroSignal):
        """Execute a SOL or ETH macro trade signal (same execution path)."""
        async with self._execution_lock:
            await self._execute_sol_macro_signal_impl(signal)

    async def _execute_sol_macro_signal_impl(self, signal: SolMacroSignal):
        """SOL/ETH macro entry (holds _execution_lock via caller)."""
        strat = signal.strategy_name
        _entry_leg = "NO" if signal.action == "BUY_NO" else "YES"
        _entry_reason = (
            f"{strat}_{signal.direction} macro_leg={signal.lag_magnitude} "
            f"side={signal.action} ai={signal.ai_used}"
        )
        lane_meta = build_lane_metadata(
            strategy=strat,
            window_size=signal.window_size,
            action=signal.action,
            direction=signal.direction,
            entry_leg=_entry_leg,
            side_source=getattr(signal, "side_source", None),
            resolver_path=getattr(signal, "resolver_path", None),
            ai_used=bool(signal.ai_used),
            reason=_entry_reason,
            signal_reason=signal.reason,
            primary_htf_bias=getattr(signal, "primary_htf_bias", None) or signal.htf_bias,
            alt_htf_bias=getattr(signal, "alt_htf_bias", None) or signal.htf_bias,
            btc_1h_regime=signal.btc_1h_regime,
        )
        if not self._check_lane_execution(
            strategy=strat,
            signal_reason=signal.reason,
            lane_meta=lane_meta,
            market_id=signal.market_id,
            market_question=signal.market_question,
        ):
            return
        if not self._check_market_regime_execution(
            strategy=strat,
            signal=signal,
            lane_meta=lane_meta,
        ):
            return
        if not self._check_session_market_reentry(
            strategy=strat,
            market_id=signal.market_id,
            market_question=signal.market_question,
            lane_meta=lane_meta,
            signal_reason=signal.reason,
        ):
            return
        if not self._check_circuit_breakers(
            strategy=strat,
            action=signal.action,
            market_id=signal.market_id,
            market_question=signal.market_question,
            signal_reason=signal.reason,
            lane_meta=lane_meta,
            btc_price=getattr(signal, "btc_current", None),
        ):
            return
        can_trade, reason = self.risk_manager.can_trade(strategy=strat)
        if not can_trade:
            logging.warning(f"{strat} trade risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                strat,
                reason,
                self.bankroll,
                extra=self._lane_skip_extra(
                    lane_meta=lane_meta,
                    signal_reason=signal.reason,
                    skip_reason=reason,
                ),
            )
            return

        # Term-based risk check (crypto-isolated budget)
        can_trade, final_size, reason = self.risk_manager.evaluate_entry(
            end_date=signal.end_date,
            current_edge=signal.edge,
            bankroll=self.bankroll,
            strategy=strat,
            requested_size=signal.size,
        )
        if not can_trade:
            logging.warning(f"{strat} trade term risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                strat,
                f"term_risk: {reason}",
                self.bankroll,
                extra=self._lane_skip_extra(
                    lane_meta=lane_meta,
                    signal_reason=signal.reason,
                    skip_reason=f"term_risk: {reason}",
                ),
            )
            return

        # Side / token_id MUST be set before any read of `side` (e.g. order_size).
        # A later `side =` makes `side` a local for the whole function; placing
        # `order_size = ... if side == "SELL"` above the assignment → UnboundLocalError.
        if signal.action == "BUY_YES":
            token_id = signal.token_id_yes
            side = "BUY"
        elif signal.action == "SELL_YES":
            token_id = signal.token_id_yes
            side = "SELL"
        elif signal.action == "BUY_NO":
            token_id = signal.token_id_no
            side = "BUY"
        else:
            logging.error(
                f"{strat} skip: unexpected action {signal.action!r} "
                f"(expected BUY_YES, SELL_YES, or BUY_NO)"
            )
            return

        # ── T1-1: Unsellable token guard ─────────────────────────────────────
        # BUY_YES / SELL_YES / BUY_NO — test the token we hold after fill (YES for sell-yes, else buy leg).
        token_to_test = token_id
        if not await self.clob_client.can_sell_token(token_to_test, signal.market_id):
            logging.warning(
                f"{strat} unsellable-token skip '{signal.market_question[:40]}' "
                f"— token={token_to_test[:20]} has no bids"
            )
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                strat,
                "unsellable_token",
                self.bankroll,
                extra=self._lane_skip_extra(
                    lane_meta=lane_meta,
                    signal_reason=signal.reason,
                    skip_reason="unsellable_token",
                ),
            )
            return

        logging.info(
            f"Executing {strat} trade: {signal.action} {final_size:.2f} @ {signal.price} ({signal.direction})"
        )
        if side == "BUY":
            order_size = final_size / max(0.01, signal.price)
        else:
            order_size = final_size / max(0.01, 1.0 - signal.price)
        pos_size = order_size

        order = await self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=signal.price,
            size=order_size,
            market_id=signal.market_id,
            post_only=True,
            dry_run=self.config.get("trading", {}).get("dry_run", True),
            order_outcome=("YES" if signal.action == "BUY_YES" else "NO"),
        )

        if order and hasattr(order, "order_id"):
            outcome = "YES" if signal.action == "BUY_YES" else "NO"
            _entry_reason = f"{_entry_reason} | {signal.reason[:120]}"
            position = Position(
                position_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                current_price=signal.price,
                pnl=0.0,
                opened_at=datetime.now(),
                end_date=signal.end_date,
                strategy=strat,
                entry_leg=_entry_leg,
                window_size=str(getattr(signal, "window_size", "") or ""),
                token_id_yes=str(getattr(signal, "token_id_yes", "") or ""),
                token_id_no=str(getattr(signal, "token_id_no", "") or ""),
                edge=float(signal.edge or 0.0),
                confidence=float(signal.confidence or 0.0),
                entry_signal={
                    "window_size": signal.window_size,
                    "htf_bias": signal.htf_bias,
                    "primary_htf_bias": getattr(signal, "primary_htf_bias", None),
                    "alt_htf_bias": getattr(signal, "alt_htf_bias", None),
                    "btc_htf_bias": getattr(signal, "btc_htf_bias", None),
                    "btc_1h_regime": getattr(signal, "btc_1h_regime", None),
                    "side_source": getattr(signal, "side_source", None),
                    "conflict_type": getattr(signal, "conflict_type", None),
                    "resolver_path": getattr(signal, "resolver_path", None),
                    "htf_side": getattr(signal, "htf_side", None),
                    "quant_side": getattr(signal, "quant_side", None),
                    "momentum_side": getattr(signal, "momentum_side", None),
                    "convergence_score": getattr(signal, "convergence_score", None),
                    "entry_volatility": getattr(signal, "entry_volatility", None),
                    **lane_meta,
                },
            )
            self.risk_manager.add_position(position)
            self._remember_session_market_entry(signal.market_id)

            self.journal.log_entry(
                trade_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                strategy=strat,
                action=signal.action,
                side=side,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                bankroll=self.bankroll,
                edge=signal.edge,
                confidence=signal.confidence,
                reason=_entry_reason,
                token_id_yes=str(getattr(signal, "token_id_yes", "") or ""),
                token_id_no=str(getattr(signal, "token_id_no", "") or ""),
                extra={
                    "hour_utc": signal.hour_utc,
                    "window_size": signal.window_size,
                    "htf_bias": signal.htf_bias,
                    "primary_htf_bias": getattr(signal, "primary_htf_bias", None),
                    "alt_htf_bias": getattr(signal, "alt_htf_bias", None),
                    "btc_htf_bias": getattr(signal, "btc_htf_bias", None),
                    "btc_1h_regime": signal.btc_1h_regime,
                    "ai_used": signal.ai_used,
                    "ai_confidence": signal.confidence if signal.ai_used else None,
                    **self._ai_entry_attribution(ai_consulted=bool(signal.ai_used)),
                    "yes_price": (
                        round(1.0 - signal.price, 4)
                        if signal.action == "BUY_NO"
                        else float(signal.price)
                    ),
                    signal.spot_price_journal_key(): signal.sol_current,
                    "btc_price": signal.btc_current,
                    "lag_magnitude": signal.lag_magnitude,
                    "edge": signal.edge,
                    "est_prob": signal.est_prob,   # prob of YES at entry; key for edge validation
                    "raw_est_prob": getattr(signal, "raw_est_prob", signal.est_prob),
                    "rsi": signal.rsi,
                    "corr_1h": signal.corr_1h,
                    "side_source": getattr(signal, "side_source", None),
                    "conflict_type": getattr(signal, "conflict_type", None),
                    "resolver_path": getattr(signal, "resolver_path", None),
                    "htf_side": getattr(signal, "htf_side", None),
                    "quant_side": getattr(signal, "quant_side", None),
                    "momentum_side": getattr(signal, "momentum_side", None),
                    "oracle_basis_bps": getattr(signal, "oracle_basis_bps", None),
                    "convergence_score": getattr(signal, "convergence_score", None),
                    "entry_volatility": getattr(signal, "entry_volatility", None),
                    "entry_policy": getattr(signal, "entry_policy", None),
                    "indicator_snapshot": getattr(signal, "indicator_snapshot", None),
                    "probability_model": "indicator_score_v1",
                    # Learning context: direction and full signal reason
                    "direction": signal.direction,
                    "signal_reason": signal.reason,
                    **lane_meta,
                },
                market_end_at=signal.end_date,
                entry_leg=_entry_leg,
            )
            self._spawn_bg(self._annotate_entry_async(
                trade_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                strategy=strat,
                action=signal.action,
                edge=float(signal.edge or 0.0),
                confidence=float(signal.confidence or 0.0),
                yes_price=self._annotation_yes_price(signal.action, signal.price),
            ))

            await self.notifier.notify_trade(
                {
                    "question": signal.market_question,
                    "side": side,
                    "outcome": outcome,
                    "size": final_size,
                    "price": signal.price,
                    "auto_execute": True,
                    "strategy": strat,
                }
            )

    async def _execute_xrp_macro_signal(self, signal: SolMacroSignal):
        """Execute an XRP macro trade signal (same execution path as SOL macro)."""
        async with self._execution_lock:
            await self._execute_sol_macro_signal_impl(signal)

    async def shutdown(self):
        """Shutdown the bot gracefully"""
        logging.info("Shutting down PolyBot...")
        _write_runtime_status(
            phase="shutdown_start",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
        )
        self.running = False

        await self.market_scanner.close()
        await self.ws_client.disconnect()
        await self.notifier.close()
        if self.ai_broker is not None:
            try:
                await self.ai_broker.stop()
            except Exception:
                logging.exception("ai_broker stop failed")

        # Stop dashboard server
        if self._dashboard_server:
            self._dashboard_server.should_exit = True

        # Flush data loggers before any forced/hard exit (_os._exit / os._exit on
        # timeout) can skip Python teardown and lose buffered rows.
        try:
            from src.analysis.rejected_candidate_log import _flush_pending
            _flush_pending()
        except Exception:
            logging.exception("reject-log flush on shutdown failed")
        try:
            for _h in list(logging.getLogger().handlers):
                _h.flush()
        except Exception:
            pass

        logging.info("PolyBot shutdown complete")
        _write_runtime_status(
            phase="shutdown_complete",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=True,
        )

    def stop(self):
        """Stop the bot"""
        self.running = False
        _write_runtime_status(
            phase="stop_requested",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
        )
        if self._dashboard_server:
            self._dashboard_server.should_exit = True


def start_dashboard(bot: Optional["PolyBot"]):
    """Starts the Uvicorn server in a separate thread if enabled in config.

    ``bot`` may be ``None`` during bootstrap: bind HTTP + /health before
    ``PolyBot()`` journal I/O. Call ``set_bot_instance(bot)`` after the bot is ready.
    """
    import time
    import socket as _socket

    dashboard_config = bot.config.get("dashboard", {})
    if not dashboard_config.get("enabled", False):
        logging.info("Dashboard is disabled in the configuration.")
        return

    # PaaS hosts set PORT — bind 0.0.0.0 and ignore dashboard_port.
    if os.environ.get("PORT"):
        port = int(os.environ["PORT"])
        host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    else:
        host = dashboard_config.get("host", "127.0.0.1")
        port = int(dashboard_config.get("dashboard_port", 8080))

    if host not in ("127.0.0.1", "::1", "localhost") and not os.environ.get("DASHBOARD_API_KEY"):
        raise SystemExit(
            "FATAL: dashboard bound to non-loopback without DASHBOARD_API_KEY. "
            "Set DASHBOARD_API_KEY or bind the dashboard to 127.0.0.1."
        )

    # Local socket checks / browser must target a real address, not 0.0.0.0.
    connect_host = "127.0.0.1" if host == "0.0.0.0" else host

    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        pd = os.environ["RAILWAY_PUBLIC_DOMAIN"].strip()
        display_url = pd if pd.startswith("http") else f"https://{pd}"
    elif host == "0.0.0.0":
        display_url = f"http://127.0.0.1:{port}"
    else:
        display_url = f"http://{host}:{port}"

    skip_browser = (
        os.environ.get("PORT") is not None
        or os.environ.get("RAILWAY_ENVIRONMENT") is not None
        or os.environ.get("DASHBOARD_OPEN_BROWSER", "").lower() in ("0", "false", "no")
    )

    def _open_browser(target_url: str):
        """Open browser using the most reliable method for the current OS."""
        import subprocess
        import sys as _sys
        try:
            if _sys.platform == "win32":
                subprocess.Popen(
                    ["cmd", "/c", "start", "", target_url],
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            elif _sys.platform == "darwin":
                subprocess.Popen(["open", target_url])
            else:
                subprocess.Popen(["xdg-open", target_url])
        except Exception:
            import webbrowser
            webbrowser.open(target_url)

    def _port_in_use() -> bool:
        """Return True if something is already listening on host:port."""
        try:
            with _socket.create_connection((connect_host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            return False

    def _wait_until_port_accepts(timeout: float = 90.0) -> bool:
        """Block until TCP accepts on connect_host:port (PaaS health checks need this)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with _socket.create_connection((connect_host, port), timeout=0.75):
                    logging.info(
                        "Dashboard is accepting connections on %s:%s — probes can pass",
                        connect_host,
                        port,
                    )
                    return True
            except (ConnectionRefusedError, OSError):
                time.sleep(0.2)
        logging.error(
            "Dashboard did not accept on %s:%s within %.0fs — "
            "platform health checks may hang in Initializing (see thread/import errors above).",
            connect_host,
            port,
            timeout,
        )
        return False

    # If something is already on the port, kill it so THIS bot instance
    # takes over as the server and set_bot_instance(bot) is properly called.
    # A stale --dashboard-only process has no bot reference and will show zeros.
    # On PaaS (PORT set), never run fuser/taskkill: ephemeral port reuse / sidecars can
    # cause false positives; binding is the source of truth.
    if _port_in_use() and not os.environ.get("PORT"):
        if not _env_flag_enabled("PSB_EVICT_DASHBOARD_PORT"):
            raise SystemExit(
                f"FATAL: dashboard port {port} already in use on {connect_host}. "
                "Stop the existing process first or set PSB_EVICT_DASHBOARD_PORT=1 "
                "to evict it explicitly."
            )
        logging.warning(
            "Dashboard port %s already in use on %s; evicting existing listener "
            "because PSB_EVICT_DASHBOARD_PORT is enabled.",
            port,
            connect_host,
        )
        try:
            import subprocess as _sp
            if sys.platform == "win32":
                result = _sp.run(
                    f'netstat -ano | findstr ":{port} " | findstr LISTENING',
                    shell=True, capture_output=True, text=True
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if parts:
                        pid = parts[-1]
                        _sp.run(
                            f"taskkill /PID {pid} /F /T",
                            shell=True,
                            stdout=_sp.DEVNULL,
                            stderr=_sp.DEVNULL,
                        )
            else:
                _sp.run(f"fuser -k {port}/tcp", shell=True,
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception as e:
            logging.warning(f"Could not evict stale dashboard: {e}")
        # Wait for port to free
        for _ in range(10):
            time.sleep(0.5)
            if not _port_in_use():
                break
        if _port_in_use():
            raise SystemExit(
                f"FATAL: dashboard port {port} remained busy on {connect_host} "
                "after eviction attempt."
            )

    def run_server():
        logging.info(f"Starting dashboard server (bind {host}:{port}) — open: {display_url}")
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError(
                "Dashboard server requires uvicorn. Install project requirements "
                "or disable dashboard.enabled in config/settings.yaml."
            ) from exc
        from src.dashboard.server import app, set_bot_instance, register_dashboard_uvicorn_server

        set_bot_instance(bot)
        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning")
        )
        register_dashboard_uvicorn_server(server)
        if bot is not None:
            bot._dashboard_server = server
        server.run()

    def open_when_ready():
        for _ in range(40):
            try:
                with _socket.create_connection((connect_host, port), timeout=1):
                    print(f"  Dashboard ready -> {display_url}")
                    if not skip_browser:
                        _open_browser(display_url)
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)
        logging.warning(
            f"Dashboard did not start within 20s -- open manually: {display_url}"
        )

    def log_when_ready_no_browser():
        for _ in range(40):
            try:
                with _socket.create_connection((connect_host, port), timeout=1):
                    logging.info(f"Dashboard listening — {display_url}")
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)
        logging.warning(f"Dashboard did not start within 20s: {display_url}")

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    if skip_browser:
        # Before heavy PolyBot() / journal replay, ensure HTTP is up so PaaS
        # /health probes succeed (otherwise Initializing can time out while main hogs CPU/GIL).
        if os.environ.get("PORT") or os.environ.get("RAILWAY_ENVIRONMENT"):
            _wait_until_port_accepts(timeout=95.0)
        threading.Thread(target=log_when_ready_no_browser, daemon=True).start()
    else:
        threading.Thread(target=open_when_ready, daemon=True).start()


def _wait_for_dashboard_server_handle(
    holder: Optional[Any],
    take_server,
    *,
    timeout_sec: float = 5.0,
):
    """Return the dashboard Uvicorn server after the startup thread registers it."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        srv = take_server()
        if srv:
            return srv
        srv = getattr(holder, "_dashboard_server", None)
        if srv:
            return srv
        time.sleep(0.05)
    return take_server() or getattr(holder, "_dashboard_server", None)


def _parse_run_args():
    """Parse --paper, --live, --confirm-live, --emergency-stop, --resume-trading. Returns (dry_run, run_bot)."""
    argv = sys.argv[1:]
    if "--emergency-stop" in argv:
        KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_SWITCH_FILE.touch()
        print(
            "Manual global stop enabled: data/KILL_SWITCH created. Bot will not place new trades until you run with --resume-trading."
        )
        return None, False
    if "--resume-trading" in argv:
        if KILL_SWITCH_FILE.exists():
            KILL_SWITCH_FILE.unlink()
            print("Manual global stop removed. Trading can resume.")
        else:
            print("Manual global stop file was not present. No change.")
        return None, False

    live = "--live" in argv
    paper = "--paper" in argv
    confirm_live = "--confirm-live" in argv

    if live and not confirm_live:
        print("Live trading requires confirmation. Run with: --live --confirm-live")
        sys.exit(1)
    if confirm_live and not live:
        print("--confirm-live has no effect without --live.")
    if live and confirm_live:
        try:
            ans = input("Type YES (exactly) to enable live trading: ").strip()
        except EOFError:
            ans = ""
        if ans != "YES":
            print("Confirmation failed. Exiting.")
            sys.exit(1)
        dry_run = False
    elif paper or not live:
        dry_run = True
    else:
        dry_run = True
    return dry_run, True


async def main():
    """Main entry point"""
    _init_fault_handler()
    load_project_dotenv(Path(__file__).resolve().parent.parent)
    _write_runtime_status(phase="bootstrap", clean_shutdown=False)

    dry_run, run_bot = _parse_run_args()
    if not run_bot:
        _write_runtime_status(
            phase="cli_noop",
            clean_shutdown=True,
            detail="management command completed without starting bot",
        )
        return

    if "--backtest" in sys.argv:
        # Run the backtester (PolymarketData-based engine)
        import subprocess

        script_path = (
            Path(__file__).resolve().parent.parent / "scripts" / "run_backtest.py"
        )
        backtest_args = [a for a in sys.argv[1:] if a != "--backtest"]
        args = [sys.executable, str(script_path)] + backtest_args
        sys.exit(subprocess.run(args).returncode)

    def _bootstrap_config() -> Dict[str, Any]:
        """Lightweight settings load so the dashboard can bind before PolyBot journal I/O."""
        config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
        except Exception as e:
            print(f"WARNING: Bootstrap could not load settings.yaml: {e}")
            return {"dashboard": {"enabled": True}, "notifications": {}}
        if not isinstance(config, dict):
            return {"dashboard": {"enabled": True}, "notifications": {}}
        notifications = config.setdefault("notifications", {})
        if not notifications.get("discord_webhook") and os.getenv("DISCORD_WEBHOOK_URL"):
            notifications["discord_webhook"] = os.getenv("DISCORD_WEBHOOK_URL")
        return config

    class _DashboardConfigShim:
        __slots__ = ("config", "_dashboard_server")

        def __init__(self, config: Dict[str, Any]):
            self.config = config
            self._dashboard_server = None

    # Bind HTTP + /health before PolyBot() (journal replay can take minutes on large sessions).
    _dash_holder = _DashboardConfigShim(_bootstrap_config())
    start_dashboard(_dash_holder)

    # Now that environment is loaded, we can initialize the bot
    bot = PolyBot()
    _write_runtime_status(
        phase="bot_initialized",
        session_id=getattr(bot.journal, "session_id", None),
        clean_shutdown=False,
    )
    if dry_run is not None:
        bot.config.setdefault("trading", {})["dry_run"] = dry_run

    # Load API keys before dashboard so /api/status shows correct AI readiness (incl. dashboard-only).
    api_keys = {
        "PRIVATE_KEY": os.getenv("PRIVATE_KEY") or os.getenv("POLYMARKET_PRIVATE_KEY"),
        "POLYMARKET_API_KEY": os.getenv("POLYMARKET_API_KEY"),
        "POLYMARKET_API_SECRET": os.getenv("POLYMARKET_API_SECRET"),
        "POLYMARKET_API_PASSPHRASE": os.getenv("POLYMARKET_API_PASSPHRASE"),
        "ETHERSCAN_API_KEY": os.getenv("ETHERSCAN_API_KEY"),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
        "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY"),
        "GOOGLE_API_KEY": os.getenv("GOOGLE_API_KEY"),
        "MINIMAX_API_KEY": (
            os.getenv("MINIMAX_API_KEY")
            or os.getenv("MINIMAX_KEY")
            or os.getenv("MINMAX_API_KEY")
        ),
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY"),
        "GROQ_API_KEY": os.getenv("GROQ_API_KEY"),
        "GOOGLE_PROJECT_ID": os.getenv("GOOGLE_PROJECT_ID"),
        "GOOGLE_LOCATION": os.getenv("GOOGLE_LOCATION"),
    }
    api_keys = {k: v for k, v in api_keys.items() if v is not None}

    _paper = bot.config.get("trading", {}).get("dry_run", True)
    if not api_keys.get("PRIVATE_KEY"):
        if _paper:
            logging.info(
                "Paper mode: PRIVATE_KEY / POLYMARKET_PRIVATE_KEY not set — OK until you enable live trading."
            )
        else:
            logging.critical(
                "CRITICAL: PRIVATE_KEY or POLYMARKET_PRIVATE_KEY is required when dry_run is false."
            )

    bot.set_api_keys(api_keys=api_keys)

    print_startup_banner(
        config=bot.config,
        dry_run=bool(bot.config.get("trading", {}).get("dry_run", True)),
        session_id=getattr(bot.journal, "session_id", None),
    )

    from src.ai_status import compute_ai_status, format_ai_log_line

    _ai_st = compute_ai_status(bot.config, bot.ai_agent.api_keys)
    logging.info(format_ai_log_line(_ai_st))
    logging.info(format_discord_notifications_log_line(bot.config))
    if not _ai_st.get("ready"):
        logging.warning(
            "LLM calls are disabled until AI is ready — check ai.enabled, "
            "provider_chain, and secrets in .env or config/secrets.env (see AI STATUS log above)."
        )

    from src.dashboard.server import set_bot_instance, take_dashboard_uvicorn_server

    set_bot_instance(bot)
    if bot.config.get("dashboard", {}).get("enabled", False):
        srv = _wait_for_dashboard_server_handle(
            _dash_holder,
            take_dashboard_uvicorn_server,
        )
        if srv:
            bot._dashboard_server = srv
        else:
            logging.warning(
                "Dashboard enabled but Uvicorn server handle missing — shutdown may not stop dashboard thread."
            )

    async def _graceful_shutdown_or_exit() -> None:
        timeout_sec = _shutdown_timeout_seconds()
        try:
            await asyncio.wait_for(bot.shutdown(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            _write_runtime_status(
                phase="shutdown_forced",
                session_id=getattr(bot.journal, "session_id", None),
                clean_shutdown=False,
                detail=f"graceful shutdown exceeded {timeout_sec:.1f}s timeout",
            )
            logging.error(
                "Graceful shutdown exceeded %.1fs timeout; forcing process exit.",
                timeout_sec,
            )
            os._exit(1)

    # Dashboard-only mode: serve dashboard + backtests, no trading loop
    if "--dashboard-only" in sys.argv:
        logging.info("Dashboard-only mode — trading disabled. Run backtests from the dashboard.")
        _write_runtime_status(
            phase="dashboard_only_idle",
            session_id=getattr(bot.journal, "session_id", None),
            clean_shutdown=False,
        )
        try:
            while True:
                await asyncio.sleep(30)
        except (KeyboardInterrupt, asyncio.CancelledError):
            bot._terminal_shutdown_sig = signal.SIGINT
        await _graceful_shutdown_or_exit()
        if getattr(bot, "_terminal_shutdown_sig", None) is not None:
            print_shutdown_banner(bot._terminal_shutdown_sig)
        return

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    shutdown_state = {"signal": None}

    def signal_handler(sig, frame):
        if shutdown_state["signal"] is not None:
            logging.info("Received repeated shutdown signal %s; waiting for shutdown.", sig)
            return
        shutdown_state["signal"] = sig
        bot._terminal_shutdown_sig = sig
        _write_runtime_status(
            phase="signal_received",
            session_id=getattr(bot.journal, "session_id", None),
            clean_shutdown=False,
            detail=str(sig),
        )
        logging.info("Received shutdown signal — stopping bot and cancelling main loop.")
        bot.stop()
        if main_task is not None and not main_task.done():
            loop.call_soon_threadsafe(main_task.cancel)

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    try:
        await bot.start()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await _graceful_shutdown_or_exit()
        finally:
            sig = getattr(bot, "_terminal_shutdown_sig", None)
            if sig is not None:
                print_shutdown_banner(sig)


if __name__ == "__main__":
    asyncio.run(main())
