"""
Main Entry Point
PolyBot AI - Polymarket Trading Bot
"""

import asyncio
import faulthandler
import json
import logging
import atexit
import os
import re
import signal
import sys
import threading
import time
try:
    import fcntl
except ImportError:  # pragma: no cover - local bot runs on Unix/macOS.
    fcntl = None
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
from src.market.websocket import WebSocketClient, UserWebSocketClient
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
from src.strategies._scan_timeout import analysis_with_timeout
from src.execution.clob_client import CLOBClient, RiskManager, Position, OrderStatus
from src.execution.trade_journal import TradeJournal, infer_entry_leg
from src.execution.exposure_manager import ExposureManager
from src.execution import exposure_overrides
from src.execution.resolution_tracker import ResolutionTracker
from src.execution.ctf_redeemer import CTFRedeemer
from src.execution.live_testing import (
    PositionExitManager,
    ExitDecision,
)
from src.execution.final_window_topup_shadow import FinalWindowTopupShadow
from src.analysis.journal_learning import (
    learning_loop_enabled,
    run_learning_cycle,
    log_learning_summary_to_logger,
)
from src.analysis.decision_snapshot import DecisionSnapshot
from src.analysis.lane_identity import build_lane_metadata
from src.analysis.rejected_candidate_log import log_rejected_candidate
from src.analysis import regime_fade
from src.analysis.lane_manager import LaneManager
from src.analysis.circuit_breakers import CircuitBreakerManager
from src.analysis.kelly_sizer import KellySizer, get_kelly_sizer
from src.analysis.lane_tape_adapter import LaneTapeAdapter
from src.analysis.calibration_log import (
    append_calibration_record,
    build_record_from_closed_trade,
)
from src.analysis.active_recommendations import append_active_recommendation
from src.analysis.ghost_calibration import (
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
TRADING_PROCESS_LOCK_FILE = RUNTIME_DIR / "trading_bot.lock"
_HOT_RELOAD_TOP_LEVEL_KEYS = frozenset({"ai", "strategies", "exposure", "lane_management", "direction", "favorite_lane"})

# Strategy modules for CODE hot-reload (option 1, 2026-07-11), in dependency order:
# shared leaves -> sol_macro/bitcoin base -> alt subclasses. Reload in THIS order so each
# subclass re-binds to the freshly-reloaded base when its `from ... import` re-executes.
_HOT_RELOAD_CODE_MODULES = (
    "src.strategies._scan_timeout",
    "src.strategies.strategy_config",
    "src.strategies.strategy_ai_context",
    "src.strategies.btc_updown_5m",
    "src.strategies.sol_macro",
    "src.strategies.bitcoin",
    "src.strategies.eth_macro",
    "src.strategies.hype_macro",
    "src.strategies.xrp_macro",
    "src.strategies.doge_macro",
    "src.strategies.bnb_macro",
)
_HOT_RELOAD_TRADING_KEYS = frozenset(
    {
        "daily_loss_limit",
        "default_position_size",
        "exit_rules",
        "kelly_fraction",
        "max_days_to_resolution",
        "max_exposure_per_trade",
        "max_position_size",
        "min_hours_to_resolution",
        # 2026-07-27: the slippage/depth guard reads self.config.get() per order, so it
        # is safe to hot-reload — add it here so depth_price_ceiling_cents / tolerance
        # tweaks apply on a file edit WITHOUT a restart (this knob just cost a restart).
        "slippage_guard",
    }
)
# Crash-triage breadcrumbs (see _init_fault_handler). HEARTBEAT_FILE is rewritten
# every runtime-status tick; if it is stale (>~90s) when the process is found dead,
# the event loop HUNG before dying. DEATH_MARKER_FILE is written on SIGTERM/normal
# exit; its presence vs absence distinguishes an orderly stop (launchd/operator) or
# graceful exit from a hard SIGKILL/OOM-Jetsam kill (no marker, fresh heartbeat) or
# a native crash (no marker, faulthandler dump in polybot_fault.log).
HEARTBEAT_FILE = RUNTIME_DIR / "bot_heartbeat.json"
DEATH_MARKER_FILE = RUNTIME_DIR / "bot_last_death.json"
_FAULT_HANDLER_STREAM = None
_DEATH_MARKER_WRITTEN = False


def _acquire_trading_process_lock(dry_run: bool):
    """Prevent paper/live trading loops from sharing runtime files in one workspace."""
    if fcntl is None:
        logging.warning("Trading process lock unavailable on this platform; continuing unlocked.")
        return None
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lock_fh = open(TRADING_PROCESS_LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.seek(0)
        owner = lock_fh.read().strip() or "unknown owner"
        print(
            "Another trading bot is already running in this workspace. "
            f"Lock={TRADING_PROCESS_LOCK_FILE} owner={owner}"
        )
        _write_runtime_status(
            phase="startup_blocked",
            clean_shutdown=True,
            detail=f"trading process lock held by {owner}",
        )
        lock_fh.close()
        sys.exit(99)
    lock_fh.seek(0)
    lock_fh.truncate()
    lock_fh.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "mode": "paper" if dry_run else "live",
                "argv": sys.argv[1:],
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            sort_keys=True,
        )
    )
    lock_fh.flush()
    os.fsync(lock_fh.fileno())
    return lock_fh


def _select_hot_reload_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return only config sections safe to apply without restarting the bot."""
    selected: Dict[str, Any] = {}
    for key in _HOT_RELOAD_TOP_LEVEL_KEYS:
        section = (config or {}).get(key)
        if isinstance(section, dict):
            selected[key] = section

    trading = (config or {}).get("trading")
    if isinstance(trading, dict):
        hot_trading = {
            key: trading[key]
            for key in _HOT_RELOAD_TRADING_KEYS
            if key in trading
        }
        if hot_trading:
            selected["trading"] = hot_trading
    return selected


def _build_hot_reload_updates(
    current_config: Dict[str, Any],
    disk_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a partial patch for runtime-safe config changes."""
    current_hot = _select_hot_reload_config(current_config or {})
    disk_hot = _select_hot_reload_config(disk_config or {})
    updates: Dict[str, Any] = {}
    for key, next_value in disk_hot.items():
        if current_hot.get(key) != next_value:
            updates[key] = next_value
    return updates


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


_LIBC = None


def _release_memory_to_os() -> None:
    """Return freed allocator arenas to the OS so RSS stops ratcheting.

    The bot's high-churn json parsing (scan + settle + embedded-dashboard ghost
    reads) allocates and frees millions of small objects; macOS keeps the freed
    arenas, so the RSS high-water mark climbs toward OOM/Jetsam even though live
    memory is small. malloc_zone_pressure_relief(NULL, 0) asks every malloc zone
    to hand idle pages back to the kernel — the direct counter to the ratchet.
    Best-effort, no-op if the symbol is unavailable.
    """
    global _LIBC
    try:
        import ctypes

        if _LIBC is None:
            _LIBC = ctypes.CDLL(None)  # libSystem on macOS / libc on Linux
        fn = getattr(_LIBC, "malloc_zone_pressure_relief", None)
        if fn is not None:
            fn.restype = ctypes.c_size_t
            fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            fn(None, 0)  # all zones, release as much as possible
            return
        trim = getattr(_LIBC, "malloc_trim", None)  # Linux fallback
        if trim is not None:
            trim(0)
    except Exception:
        pass


def _self_rss_mb() -> Optional[float]:
    """CURRENT resident-set size of this process, in MB.

    Must be CURRENT, not peak: the OOM monitor and alert depend on seeing RSS
    fall back down after a transient spike. ru_maxrss is the lifetime PEAK
    (high-water mark, never decreases) — using it made the heartbeat look
    permanently stuck at the worst spike and the OOM alert over-fire. Prefer
    psutil's live RSS; fall back to ru_maxrss only if psutil is unavailable.
    """
    try:
        import psutil

        return round(psutil.Process().memory_info().rss / 1024.0 / 1024.0, 1)
    except Exception:
        try:
            import resource

            ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
            return round(ru / divisor, 1)  # peak fallback
        except Exception:
            return None


_MEM_PROFILE = {"on": False, "baseline": None, "last": 0.0, "interval": 300.0}


def _mem_profile_init() -> None:
    """Arm a tracemalloc leak hunter when PSB_MEM_PROFILE is set.

    Off by default (tracemalloc adds per-alloc overhead). When on, periodically
    diffs against a baseline snapshot and writes the top growing allocation
    sites (file:line, +MB, +count) to data/runtime/mem_profile.jsonl — that
    names the exact code holding the multi-GB leak behind the OOM deaths.
    """
    if not _env_flag_enabled("PSB_MEM_PROFILE"):
        return
    try:
        import tracemalloc

        tracemalloc.start(25)
        _MEM_PROFILE["on"] = True
        try:
            _MEM_PROFILE["interval"] = float(
                os.getenv("PSB_MEM_PROFILE_INTERVAL_SEC", "300") or 300
            )
        except (TypeError, ValueError):
            _MEM_PROFILE["interval"] = 300.0
        logging.warning(
            "PSB_MEM_PROFILE on — tracemalloc+gc leak hunt every %.0fs -> data/runtime/mem_profile.jsonl",
            _MEM_PROFILE["interval"],
        )
        t = threading.Thread(target=_mem_profile_thread, name="mem-profile", daemon=True)
        t.start()
    except Exception as exc:
        logging.warning("mem profile init failed: %s", exc)


def _gc_native_census() -> dict:
    """Census of live objects by type + pandas/numpy native bytes (tracemalloc-blind).

    The leak is native (RSS >> tracemalloc-traced), so the signal is here: count
    live objects per type and sum the C-buffer bytes of DataFrames / ndarrays.
    A growing DataFrame/ndarray count or nbytes names the leaking object class.
    """
    import gc
    from collections import Counter

    type_counts: Counter = Counter()
    df_n = df_bytes = arr_n = arr_bytes = 0
    for o in gc.get_objects():
        try:
            tn = type(o).__name__
            type_counts[tn] += 1
            if tn == "DataFrame":
                df_n += 1
                df_bytes += int(o.memory_usage(deep=True).sum())
            elif tn == "Series":
                df_bytes += int(o.memory_usage(deep=True))
            elif tn == "ndarray":
                arr_n += 1
                arr_bytes += int(o.nbytes)
        except Exception:
            continue
    return {
        "top_types": type_counts.most_common(15),
        "dataframes": df_n,
        "dataframe_mb": round(df_bytes / 1024 / 1024, 1),
        "ndarrays": arr_n,
        "ndarray_mb": round(arr_bytes / 1024 / 1024, 1),
        "gc_tracked": len(gc.get_objects()),
    }


def _mem_profile_tick(phase: str = "timer") -> None:
    """Throttled tracemalloc diff + gc native census -> mem_profile.jsonl."""
    if not _MEM_PROFILE["on"]:
        return
    now = time.monotonic()
    if now - _MEM_PROFILE["last"] < _MEM_PROFILE["interval"]:
        return
    _MEM_PROFILE["last"] = now
    try:
        import tracemalloc

        snap = tracemalloc.take_snapshot().filter_traces(
            (
                tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
                tracemalloc.Filter(False, tracemalloc.__file__),
            )
        )
        base = _MEM_PROFILE["baseline"]
        rows = []
        if base is not None:
            rows = [
                {
                    "site": str(s.traceback[0]) if s.traceback else "?",
                    "growth_mb": round(s.size_diff / 1024 / 1024, 2),
                    "count_diff": s.count_diff,
                }
                for s in snap.compare_to(base, "lineno")[:12]
            ]
        else:
            _MEM_PROFILE["baseline"] = snap
        cur, _peak = tracemalloc.get_traced_memory()
        with (RUNTIME_DIR / "mem_profile.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "phase": phase,
                        "rss_mb": _self_rss_mb(),
                        "traced_mb": round(cur / 1024 / 1024, 1),
                        "native": _gc_native_census(),
                        "top_growth": rows,
                    },
                    default=str,
                )
                + "\n"
            )
    except Exception as exc:
        logging.debug("mem profile tick failed: %s", exc)


def _mem_profile_thread() -> None:
    """Time-based ticker so the census fires even during long/wedged cycles."""
    while _MEM_PROFILE["on"]:
        try:
            time.sleep(min(30.0, _MEM_PROFILE["interval"]))
            _mem_profile_tick("timer")
        except Exception:
            pass


def _write_heartbeat(phase: str) -> None:
    """Stamp a tiny liveness file every runtime-status tick (hang/OOM detector)."""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "monotonic": time.monotonic(),
                    "pid": os.getpid(),
                    "phase": phase,
                    "rss_mb": _self_rss_mb(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        _mem_profile_tick(phase)
    except Exception:
        pass  # never let a breadcrumb write affect the trading loop


def _write_death_marker(reason: str) -> None:
    """Record an orderly stop/exit BEFORE the runtime tears down.

    Runs from an atexit hook (normal/exception exit) and a SIGTERM handler
    (launchd stop / `kill`). Does NOT run on SIGKILL or a native abort — that
    absence is itself the signal: no marker + a fresh heartbeat == hard kill
    (OOM/Jetsam); no marker + a faulthandler dump == native crash.
    """
    global _DEATH_MARKER_WRITTEN
    if _DEATH_MARKER_WRITTEN:
        return
    _DEATH_MARKER_WRITTEN = True
    try:
        hb = {}
        try:
            hb = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
        except Exception:
            hb = {}
        last_hb_age = None
        try:
            if hb.get("monotonic") is not None:
                last_hb_age = round(time.monotonic() - float(hb["monotonic"]), 1)
        except Exception:
            last_hb_age = None
        DEATH_MARKER_FILE.write_text(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "pid": os.getpid(),
                    "reason": reason,
                    "last_heartbeat": hb,
                    "last_heartbeat_age_sec": last_hb_age,
                    "last_status": _read_runtime_status(),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _init_fault_handler() -> None:
    """Persist fatal-signal Python tracebacks + death breadcrumbs for triage."""
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
    # Fresh process == previous death is now explainable: clear the stale marker so
    # its presence always refers to the most recent stop. Keep the heartbeat so an
    # external monitor can read the last-known-good age across the restart.
    try:
        DEATH_MARKER_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    # SIGTERM (launchd stop / kill) is not a fatal signal faulthandler catches, so
    # record it ourselves then chain to the previous handler so shutdown proceeds.
    try:
        _prev_term = signal.getsignal(signal.SIGTERM)

        def _on_sigterm(signum, frame):
            _write_death_marker("sigterm")
            if callable(_prev_term) and _prev_term not in (
                signal.SIG_DFL,
                signal.SIG_IGN,
            ):
                _prev_term(signum, frame)
            else:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTERM)

        signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception as exc:
        logging.debug("SIGTERM death-marker handler not installed: %s", exc)
    try:
        atexit.register(_write_death_marker, "atexit")
    except Exception:
        pass


def _read_runtime_status() -> Dict[str, Any]:
    try:
        return json.loads(RUNTIME_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _runtime_status_writes_enabled() -> bool:
    """Only the trading process owns the split-mode runtime status file."""
    return "--dashboard-only" not in sys.argv


def _write_runtime_status(
    *,
    phase: str,
    session_id: Optional[str] = None,
    clean_shutdown: Optional[bool] = None,
    detail: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort runtime breadcrumb for external supervision and crash triage."""
    if not _runtime_status_writes_enabled():
        return
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
        _write_heartbeat(phase)
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
    timeout_sec: float = 22.0,
) -> tuple[str, Any, int, bool]:
    """Measure one strategy scan wall time and HARD-CAP it.

    The parallel scan is an ``asyncio.gather`` — it completes only when the
    SLOWEST lane finishes. A single hung lane (e.g. Hyperliquid OHLCV stalling:
    bisection-recursed sync fetches stacking to 38-60s) therefore drags the whole
    cycle to ~60s, freezing exits/dashboard and tripping the status orb. The inner
    ``analysis_with_timeout`` (15s) only caps each get_full_analysis call, not the
    per-lane total. This wait_for bounds the WHOLE lane: on timeout the lane is
    skipped for this cycle (signals come next cycle) so it can't stall the others.
    The offloaded fetch thread finishes in the background; its result is discarded.
    """
    started = time.perf_counter()
    try:
        if timeout_sec and timeout_sec > 0:
            result = await asyncio.wait_for(scan_coro, timeout=timeout_sec)
        else:
            result = await scan_coro
        ok = True
    except asyncio.TimeoutError:
        # Empty signal list (NOT None/exc): downstream iterates the payload, so a
        # skipped lane must stay iterable. ok=True keeps it out of strategy_errors;
        # the warning below is the record of the degraded cycle.
        result = []
        ok = True
        logging.warning(
            "[scan] %s exceeded hard per-lane timeout %.0fs — lane skipped this cycle "
            "(prevents one slow lane from stalling the parallel scan)",
            strategy_name, timeout_sec,
        )
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


def _calibration_scope(config: Dict) -> Dict:
    """The trading.calibration_scope block ({} when unset)."""
    return ((config.get("trading") or {}).get("calibration_scope") or {})


def _calibration_strategy_allowed(config: Dict, strategy: str) -> bool:
    """True if `strategy` may create NEW entries this cycle.

    Scope OFF (or no execution_strategies listed) => every strategy allowed
    (byte-identical to pre-scope behavior). Scope ON => only the listed
    strategies produce entries; the rest have their scan-task skipped. Exits
    run before task-building and are never gated here.
    """
    scope = _calibration_scope(config)
    if not scope.get("enabled"):
        return True
    allowed = set(scope.get("execution_strategies") or [])
    return not allowed or strategy in allowed


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
        self.config_path = self._resolve_config_path(config_path)
        self.config = self._load_config(str(self.config_path))
        self._config_mtime_ns = self._config_file_mtime_ns()
        self._last_config_hot_reload_error_mtime_ns: Optional[int] = None
        # CODE hot-reload sentinel (option 1): touch data/reload_code.flag to hot-swap
        # strategy modules without a restart. Initial mtime captured so a leftover flag
        # at startup does not spuriously reload on the first loop tick.
        _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._code_reload_flag_path = os.path.join(_repo_root, "data", "reload_code.flag")
        self._code_reload_flag_seen_mtime_ns: Optional[int] = self._code_reload_flag_mtime_ns()
        self._code_reload_broken: bool = False
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
        # Let the scanner read pushed WS mids first (REST fallback inside fetch_prices).
        self.market_scanner.ws_client = self.ws_client
        # 2026-07-29 (Phase-2 ④): authenticated USER channel for real fill events →
        # order.filled_size becomes venue truth. creds_provider reads live creds each
        # reconnect (survives re-derivation); callback is observe-only fill accounting.
        # Lazy callbacks: self.clob_client is constructed later in __init__, so both
        # callbacks must defer the attribute access to call time (not construction).
        self.user_ws_client = UserWebSocketClient(
            self.config,
            creds_provider=lambda: self.clob_client.get_ws_creds(),
            on_user_event=lambda event: self.clob_client.apply_user_fill_event(event),
        )
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
        # Per-lane tape adapter: reads each lane's recent net-pnl + green-rate and
        # scales its notional DOWN when the tape turns against that side (net-losing
        # AND fills stop going green), back UP as it recovers. De-size only
        # (max_mult<=1.0) so it can never enlarge a position. mode off|shadow|live.
        self.lane_tape_adapter = LaneTapeAdapter(self.config.get("lane_tape_adapter", {}))
        self.notifier = NotificationManager(self.config)
        # is_paper drives loss_kill_active (the per-lane loss-streak pause is inert in
        # paper). config.trading.dry_run is NOT applied from --live until AFTER __init__
        # (see the session-anchor block below), so reading it here reports True even on a
        # live launch — which silently disabled the loss-kill in every live run. Detect
        # live from argv (timing-independent), matching how the anchor block does it.
        # 2026-07-27.
        is_paper = self.config.get("trading", {}).get("dry_run", True)
        if "--live" in sys.argv:
            is_paper = False
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
        self._ghost_calibration_refresh_inflight = False
        # Taken-EXIT settler throttle. Refreshes trades_settled.jsonl in-process so
        # the exit-policy drift recompute (SL/TP/hold lever) reads fresh ground
        # truth instead of going stale (it had no scheduler and went 2d stale,
        # silently breaking the exit-side calibration loop). Heavier than the ghost
        # settle (Gamma API), and resolutions land hourly, so it runs less often.
        self._last_exit_settle_monotonic: float = 0.0
        # Exit-policy drift alerting: remember the last drift set we pinged so we
        # only re-alert when a new lane drifts or a recommendation flips.
        self._last_exit_drift_sig: frozenset = frozenset()
        # Manual global stop alerting: one Discord burst per kill-switch episode.
        self._manual_global_stop_alert_sent = False

        # Trade journal: every process restart starts a FRESH session at
        # initial_bankroll (500). This ensures every restart = clean test run.
        # Resume only if PAPER_SESSION_ID is explicitly set to an existing session name.
        _forced_session = os.environ.get("PAPER_SESSION_ID")
        _resume_session = os.environ.get("PAPER_RESUME_SESSION", "false").lower() in ("1", "true", "yes")
        # Live launches default to a fresh journal/anchor so each operator live test has
        # clean PnL and daily counters. Resume a live journal only when explicitly asked;
        # otherwise stale closed trades from the previous run can make a "new" session
        # look down/trade-capped before it has placed anything.
        _live_mode = "--live" in sys.argv
        _live_resume = os.environ.get("LIVE_RESUME_SESSION", "false").lower() in ("1", "true", "yes")
        # Backward-compatible escape hatch: LIVE_FRESH_SESSION=1 forces fresh even if
        # LIVE_RESUME_SESSION was accidentally left on.
        _live_fresh = os.environ.get("LIVE_FRESH_SESSION", "false").lower() in ("1", "true", "yes")
        if _forced_session and not _resume_session and (not _live_mode or _live_resume):
            # Explicit session name given — use it (e.g. PAPER_SESSION_ID=reset_20260416).
            # In live mode this must also opt into LIVE_RESUME_SESSION; a stale paper
            # env var should not drag live tests into an old journal/anchor.
            self.journal = TradeJournal(session_id=_forced_session, resume_latest=False)
            self._fresh_session_created = False
            logging.info(f"Forced session via PAPER_SESSION_ID={_forced_session}")
        elif (_resume_session or (_live_mode and _live_resume)) and not _live_fresh:
            # Resume latest only by explicit opt-in.
            self.journal = TradeJournal(resume_latest=True)
            self._fresh_session_created = False
            logging.info(
                "Resuming latest session (%s mode): %s",
                "live" if _live_mode else "resume-opt-in",
                self.journal.session_id,
            )
        else:
            # Default: fresh session every restart (process lifecycle = test cycle).
            # Live fresh path: leave bankroll for refresh_live_wallet_bankroll() so it
            # anchors at the real wallet instead of the paper 500 default.
            new_id = datetime.now().strftime("test_%Y%m%d_%H%M%S")
            self.journal = TradeJournal(session_id=new_id, resume_latest=False)
            self._fresh_session_created = True
            if not _live_mode:
                self.bankroll = float(self.config.get("backtest", {}).get("initial_bankroll", 500.0))
                self.bankroll_source = "config_initial"
                logging.info(f"Fresh session on restart: {new_id} @ ${self.bankroll:.2f}")
            else:
                logging.info(f"Fresh LIVE session {new_id} — bankroll deferred to live wallet refresh")
        self._session_traded_market_ids: Set[str] = self._load_session_traded_market_ids()

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
        # Final-window winner top-up — SHADOW stage (logging-only; default-off via
        # final_window_topup.shadow_enabled). Validates OUR winner-detection accuracy
        # on BTC 5m before any paper/live top-up. See final_window_topup_shadow.py.
        try:
            self.topup_shadow = FinalWindowTopupShadow.from_config(self.config)
        except Exception:
            self.topup_shadow = FinalWindowTopupShadow(enabled=False)
        # Spot-reversal bank — SHADOW stage (logging-only; default off/shadow via
        # spot_reversal_bank.mode). Instruments the exit gap-through leak: flags when an
        # in-profit hold-to-resolution position's UNDERLYING spot reverses before the CLOB
        # book gaps through the trail floor (doge 5m down 2026-07-22: +35.6% -> -11% in one
        # tick). Logging-only until forward-proven; never mutates trading state. See
        # spot_reversal_bank.py.
        _srb = (self.config.get("spot_reversal_bank", {}) or {})
        self._spot_rev_mode = str(_srb.get("mode", "off") or "off").lower()  # off|shadow|live
        try:
            if self._spot_rev_mode in ("shadow", "live"):
                from src.execution.spot_reversal_bank import SpotReversalBank
                self._spot_rev_bank = SpotReversalBank(
                    arm_pct=float(_srb.get("arm_pct", 0.12) or 0.12),
                    reversal_pct=float(_srb.get("reversal_pct", 0.003) or 0.003),
                )
            else:
                self._spot_rev_bank = None
        except Exception:
            self._spot_rev_bank = None
        # Never-green fast-cut — SHADOW stage (logging-only; default off via
        # never_green_cut.mode). Instruments the dominant exit leak: a position whose
        # peak pnl never clears green_threshold within cut_after_secs is (per 07-22 data)
        # 0% WR and rides to −36% avg — separable from winners by MFE-timing, which entry
        # conviction cannot do. Logs NEVER_GREEN_SHADOW would-cut events; never exits. See
        # never_green_cut.py.
        _ngc = (self.config.get("never_green_cut", {}) or {})
        self._never_green_mode = str(_ngc.get("mode", "off") or "off").lower()  # off|shadow|live
        # Per-window cut threshold: 1h lanes develop slower than 5m, so a single 60s
        # threshold false-cuts slow 1h winners. Map window -> seconds; falls back to
        # cut_after_secs when a window is absent. Keys are strings ("5m","15m","1h").
        self._never_green_cut_by_window = {
            str(k): float(v)
            for k, v in (_ngc.get("cut_after_secs_by_window", {}) or {}).items()
        }
        # 2026-08-04 PER-LANE cut_after_secs override (operator GO). Keyed
        # "strategy:window:side" (side=BUY_NO|BUY_YES); an entry's cut_after_secs (when
        # present) overrides the by_window value for that one lane. Absent keys / absent
        # cut_after_secs fall back to by_window. (min_loss_pct in the same by_lane entry is
        # read by the ExitManager severity gate, not here.)
        self._never_green_cut_by_lane = {}
        _ngc_by_lane_cfg = _ngc.get("by_lane", {})
        if isinstance(_ngc_by_lane_cfg, dict):
            for _lk, _lv in _ngc_by_lane_cfg.items():
                if isinstance(_lv, dict) and _lv.get("cut_after_secs") is not None:
                    try:
                        self._never_green_cut_by_lane[str(_lk)] = float(_lv["cut_after_secs"])
                    except (TypeError, ValueError):
                        pass
        try:
            if self._never_green_mode in ("shadow", "live"):
                from src.execution.never_green_cut import NeverGreenCut
                self._never_green_cut = NeverGreenCut(
                    green_threshold_pct=float(_ngc.get("green_threshold_pct", 0.02) or 0.02),
                    cut_after_secs=float(_ngc.get("cut_after_secs", 60.0) or 60.0),
                )
            else:
                self._never_green_cut = None
        except Exception:
            self._never_green_cut = None
        # WIDE final-window sampler: the standalone (not position-bound) version that
        # samples ALL BTC 5m markets in their final window with REAL /midpoint marks.
        # Position-mode shadow is starved (bot exits before expiry); this is where the
        # data + edge live. Refreshed each scan; sampled at the fast-exit cadence.
        _tcfg = (self.config.get("final_window_topup", {}) or {})
        self.topup_sampler_enabled = bool(_tcfg.get("sampler_enabled", False))
        self._topup_sampler_window_mins = float(_tcfg.get("sampler_window_mins", 1.5) or 1.5)
        self._topup_universe = []  # list of (market_id, yes_token, no_token, end_date, question)
        # Throttle: the sampler awaits /midpoint+book per final-window market and shares the
        # httpx pool with the 120-market main scan. Running it every fast-exit tick (~3s) caused
        # event-loop/pool contention (cycle median 11s vs ~5s baseline). Cap to ~20s between runs:
        # the 1.5min window still yields ~4 samples/market, with ~7x less contention.
        self._topup_sampler_min_interval_s = float(_tcfg.get("sampler_min_interval_s", 20.0) or 20.0)
        self._topup_last_run = 0.0

        # Drift-driven runtime feedback cadence (see performance_feedback in settings.yaml)
        self._performance_feedback_cycle = 0
        # Fresh-ask entry repricing (2026-07-29): the guard stashes the live best_ask so
        # marketable BUY entries are priced/sized at the executable ask, not the stale
        # scan-snapshot signal.price (which was killing FAK orders: "no orders to match").
        self._last_fresh_entry_ask: Optional[float] = None
        self._last_fresh_entry_token_id: Optional[str] = None

        # State
        self.running = False
        # Serialize order placement, exits, resolution settlement — one trading loop, same lock.
        self._execution_lock = asyncio.Lock()
        self._dashboard_server = None
        # Handle to the daily coach child process so shutdown can reap it instead
        # of orphaning it past os._exit(0). None when no coach run is in flight.
        self._coach_proc = None
        _initial_bankroll = self.config.get("backtest", {}).get("initial_bankroll", 1000.0)
        # Restore bankroll only for explicit/resumed sessions. A fresh paper startup
        # must stay at initial_bankroll even when older journals contain +PnL.
        if getattr(self, "_fresh_session_created", False):
            self.bankroll = float(_initial_bankroll)
            self.bankroll_source = "config_initial"
            logging.info("Fresh session bankroll set from config: $%s", f"{self.bankroll:,.2f}")
        else:
            # Restore bankroll from last journal snapshot (or last entries line) so
            # explicit resumed sessions do not reset when snapshots.jsonl is sparse.
            _last_snap = self.journal.get_snapshots(limit=1)
            if _last_snap and _last_snap[-1].get("bankroll") is not None:
                self.bankroll = float(_last_snap[-1]["bankroll"])
                self.bankroll_source = "journal_snapshot"
                logging.info(
                    f"Bankroll restored from last snapshot: ${self.bankroll:,.2f} "
                    f"(initial was ${_initial_bankroll:,.2f})"
                )
            else:
                _from_log = self.journal.last_bankroll_from_entries_log()
                if _from_log is not None:
                    self.bankroll = _from_log
                    self.bankroll_source = "journal_entries"
                    logging.info(
                        f"Bankroll restored from journal entries: ${self.bankroll:,.2f} "
                        f"(initial was ${_initial_bankroll:,.2f})"
                    )
                else:
                    self.bankroll = _initial_bankroll
                    self.bankroll_source = "config_initial"
        _cint = self.config.get("trading", {}).get("cycle_interval_sec", 120)
        self.scan_interval = max(30, int(_cint))  # single unified loop: scan + crypto + exits
        self._unified_cycle_count = 0
        # Fast exit monitor: the 60s scan loop checks stops only ~5x over a 5m
        # market's life, so fast adverse moves blow through the stop before it
        # fires (fills land at -60%..-99% on a 14-20% stop). This decoupled loop
        # re-checks TP/SL on held positions every exit_check_interval_sec using a
        # cheap per-token book fetch — no scan/AI — so the stop fires near its
        # threshold. 0/None disables it (falls back to scan-loop-only exits).
        _eint = self.config.get("trading", {}).get("exit_check_interval_sec", 10)
        try:
            self.exit_check_interval = float(_eint) if _eint else 0.0
        except (TypeError, ValueError):
            self.exit_check_interval = 0.0
        # Shared by the scan loop and fast exit loop to serialize exit handling so
        # a position can't be exited twice. Created in start() (needs a live loop).
        self._exit_lock: Optional[asyncio.Lock] = None
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
        # Do not run ghost calibration synchronously in __init__: dashboard binds
        # before PolyBot is attached, so this can leave /api/status at
        # "Bot not running" for minutes on large calibration logs. start() launches
        # the same refresh in a background thread before the trading loop.

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

    def _entry_exec_params(self) -> dict:
        """Resolve the entry execution policy from config.

        trading.entry_mode: marketable | maker | hybrid (default marketable).
        - marketable: FAK taker — fills like paper assumes, pays the fee.
        - maker: post_only GTC — 0 fee but may not fill (starves fast markets).
        - hybrid: maker-first then cross to taker after entry_maker_wait_sec, only on
          entry_hybrid_windows (default 15m/1h); other windows fall back to marketable.
        Back-compat: legacy trading.entry_marketable bool maps to marketable/maker.
        """
        t = self.config.get("trading") or {}
        mode = str(t.get("entry_mode") or "").lower()
        if not mode:
            mode = "marketable" if t.get("entry_marketable", True) else "maker"
        return {
            "entry_mode": mode,
            "maker_wait_sec": float(t.get("entry_maker_wait_sec", 8.0) or 8.0),
            "hybrid_windows": tuple(
                str(x).lower() for x in (t.get("entry_hybrid_windows") or ["15m", "1h"])
            ),
        }

    def _exit_exec_params(self) -> dict:
        """Resolve the EXIT execution policy from config.

        trading.exit_mode: marketable | hybrid (default marketable).
        - marketable (default): today's exact behavior — non-marketable exits rest
          as a plain GTC and urgent exits FAK aggressive-cross. Nothing changes.
        - hybrid: NON-marketable (take-profit / mark-based) exits on
          exit_hybrid_windows go maker-first (post_only GTC 0-fee -> FAK fallback)
          via clob_client.place_exit_order. URGENT (marketable) exits are ALWAYS
          FAK aggressive-cross regardless of this flag — they can't rest.
        """
        t = self.config.get("trading") or {}
        return {
            "exit_mode": str(t.get("exit_mode") or "marketable").lower(),
            "maker_wait_sec": float(t.get("exit_maker_wait_sec", 6.0) or 6.0),
            "hybrid_windows": tuple(
                str(x).lower() for x in (t.get("exit_hybrid_windows") or ["15m", "1h"])
            ),
        }

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
        beta_blend_w_max = float(cal_cfg.get("beta_blend_w_max", 0.60) or 0.60)
        beta_blend_min_bias = float(cal_cfg.get("beta_blend_min_bias", 0.05) or 0.05)
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
                beta_blend_w_max=beta_blend_w_max,
                beta_blend_min_bias=beta_blend_min_bias,
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
        # 60s -> 600s (2026-06-19): the settle path full-re-parses the ~1GB
        # rejected_candidates.jsonl + settled.jsonl every call (json.loads per line,
        # no incremental read). At 60s that churned ~1.25GB of transient JSON
        # allocations per cycle -> glibc arena fragmentation -> RSS +~43MB/min
        # (gc-flat, malloc_trim-proof) = THE leak (memray-confirmed). Settling
        # against resolved outcomes is not time-critical (markets resolve over
        # minutes-hours), so a 10-min cadence cuts the parse churn ~10x.
        if not force and (now_mono - self._last_ghost_calibration_refresh_monotonic) < 600.0:
            return
        # 2026-07-13 operator order: ghost settle produces counterfactual outcomes
        # barred from decisions (live-realized only) while re-parsing ~GB jsonl every
        # 10 min in-process. Gate default-on for back-compat; config sets false.
        _gc_cfg = (self.config.get("ghost_calibration") or {})
        if not bool(_gc_cfg.get("auto_settle_enabled", True)):
            # 2026-07-13 T3 (Codex GO, live-settler-only): the ghost severance
            # early-return collateral-disabled the taken-EXIT settler on OUR OWN
            # closed trades (trades_settled.jsonl went dark — 0 rows this session).
            # Run the live settler here, then bail before any ghost work. Ghost
            # settle stays OFF; rejected candidates are never read on this path.
            try:
                exit_cfg = (self.config.get("lane_exit_policy") or {})
                interval = float(exit_cfg.get("settle_interval_sec", 600) or 600)
                if force or (now_mono - self._last_exit_settle_monotonic) >= interval:
                    from src.analysis.taken_exit_settler import settle as _settle_exits
                    since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
                    _settle_exits(since=since)
                    self._last_exit_settle_monotonic = now_mono
            except Exception as _xe:  # noqa: BLE001 — settle must never break trading
                logging.warning("taken-exit settle refresh skipped: %s", _xe)
            self._last_ghost_calibration_refresh_monotonic = now_mono
            return
        cal_cfg = (self.config.get("lane_calibration") or {})
        ghost_weight = float(cal_cfg.get("ghost_weight", 0.5) or 0.0)
        cal = getattr(self, "lane_calibrator", None)
        settle_summary = settle_rejected_candidates(
            calibrator=cal,
            ghost_weight=ghost_weight,
        )
        # The settle is the heaviest per-interval allocator churn — hand the freed
        # arenas back to the OS so RSS doesn't ratchet toward Jetsam.
        _release_memory_to_os()
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
                    flip_min_n=int(plt_cfg.get("flip_min_n", 80) or 80),
                    flip_wr_max=float(plt_cfg.get("flip_wr_max", 0.40) or 0.40),
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
        # Refresh the taken-EXIT settler so trades_settled.jsonl is current before
        # the exit-policy drift recompute reads it. Closes the exit-side loop
        # (entries.jsonl -> trades_settled -> lane_exit_policy.recompute -> SL/TP/
        # hold drift recommendation). Own throttle (default 600s) since it hits the
        # Gamma API and resolutions arrive hourly; cached outcomes make re-runs
        # cheap. Runs in this worker thread so the blocking fetch is safe.
        exit_settle_summary: Dict[str, Any] = {}
        try:
            exit_cfg = (self.config.get("lane_exit_policy") or {})
            interval = float(exit_cfg.get("settle_interval_sec", 600) or 600)
            if force or (now_mono - self._last_exit_settle_monotonic) >= interval:
                from src.analysis.taken_exit_settler import settle as _settle_exits
                since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
                exit_settle_summary = _settle_exits(since=since) or {}
                self._last_exit_settle_monotonic = now_mono
        except Exception as _xe:  # noqa: BLE001 — calibration refresh must never break trading
            logging.warning("taken-exit settle refresh skipped: %s", _xe)
        # Per-lane EXIT-policy drift: recompute the shadow recommendation from
        # settled trades and Discord-ping when live config disagrees with the
        # data. Recommend-only — never mutates exits. Runs in this worker thread
        # so the blocking webhook POST is safe.
        try:
            self._maybe_alert_exit_policy_drift(settle_summary)
        except Exception as _ee:  # noqa: BLE001 — alerting must never break settle
            logging.warning("exit-policy drift alert skipped: %s", _ee)
        # Adaptive per-lane SIZER shadow recompute (config trading.adaptive_sizer).
        # SHADOW-ONLY: writes adaptive_sizer_state.json + shadow log; moves no real
        # size (resolve_size_mult returns 1.0 unless mode==live). Same worker thread.
        try:
            # gate on the EXIT-settle summary (n_settled) — that is the process that
            # actually rewrites trades_settled.jsonl, which the sizer reads. The ghost
            # settle_summary counter is a different pipeline and can be 0 here.
            self._maybe_recompute_adaptive_sizer(exit_settle_summary)
        except Exception as _se:  # noqa: BLE001 — sizer shadow must never break settle
            logging.warning("adaptive-sizer shadow recompute skipped: %s", _se)
        # allow_jsonl_scan=False: in the live bot NEVER fall back to the 500MB+
        # JSONL scan (the memory balloon we eliminated). DuckDB fast-path or the
        # cached/unavailable status only — even on cold start with a locked db.
        status = build_ghost_calibration_status(allow_jsonl_scan=False)
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

    def _schedule_ghost_calibration_refresh(self, *, force: bool = False) -> None:
        """Run ghost settlement off the trading hot path.

        Settled/rejected ghost logs can be large, so scan cadence must not await
        this work. Single-flight avoids piling up repeated settlement workers.
        """
        if self._ghost_calibration_refresh_inflight:
            logging.debug("Ghost calibration refresh already running; skipping schedule.")
            return
        self._ghost_calibration_refresh_inflight = True

        async def _runner() -> None:
            try:
                await asyncio.to_thread(self._refresh_ghost_calibration_state, force=force)
            except Exception as e:
                logging.warning("Rejected-candidate tracker refresh failed: %s", e)
            finally:
                self._ghost_calibration_refresh_inflight = False

        self._spawn_bg(_runner(), name="ghost_calibration_refresh")

    def _warmup_feed_ready(self, wcfg: Dict[str, Any]) -> bool:
        """True when the scanner is pricing a healthy slice of the universe with FRESH
        data (signal-based, not a timer).

        Readiness = the most recent scan priced >= ``min_universe`` candidate tokens
        whose price is within ``price_max_age_sec``, AND the scan phase is healthy.
        SOURCE-AGNOSTIC on purpose: a REST-fallback price 3s old is exactly as tradeable
        as a WS one — that is what ``price_max_age_sec`` (=8) already guarantees, and it
        is the same freshness contract the entry path itself trades on.

        History: the first version required 80% WS-source coverage. But the WS market
        subscription is DEFERRED until the scanner universe primes, so ``ws_cov=0.000``
        for the entire warmup window (log: ``FEED_PRICE_SRC ws_hit=0 rest=614``). That bar
        was unreachable, so release ALWAYS fell to the 180s time backstop — the blind
        timer the operator rejected. Keying on freshness instead fires in ~ready_cycles
        scans (REST serves fresh from cycle 1), and still holds if the feed is truly dead.
        ``_last_price_src[tid] = (src, ts, age_ms)``: ts is loop-time when priced this
        cycle; age_ms is data staleness (REST=0.0, WS only stamped when already <=max_age).
        """
        try:
            sc = getattr(self, "market_scanner", None)
            src_map = dict(getattr(sc, "_last_price_src", {}) or {})
            if not src_map:
                return False
            # MUST use the same clock the scanner stamps ts with (asyncio loop time,
            # scanner.py ~918/1023) — mixing epoch vs loop-monotonic makes every record
            # look ancient and the signal never fires (prior Codex catch, preserved).
            import asyncio as _aio
            try:
                now = _aio.get_running_loop().time()
            except Exception:
                import time as _t
                now = _t.monotonic()
            # price_max_age_sec is THE freshness contract the bot trades on; warmup may
            # override via warmup.max_price_age_sec but defaults to the same value.
            cw = ((self.config.get("trading") or {}).get("clob_ws") or {})
            max_age_s = float(wcfg.get("max_price_age_sec", cw.get("price_max_age_sec", 8)) or 8)
            # Fresh-as-of-now = last priced within max_age AND underlying data age within
            # max_age. Stamp sites already guarantee both, so this counts tokens currently
            # priced fresh (REST-served ones included); prior-cycle records still within
            # max_age legitimately count — that IS the freshness contract the entry uses.
            fresh = [
                v for v in src_map.values()
                if (now - float(v[1])) <= max_age_s and float(v[2]) <= max_age_s * 1000.0
            ]
            if len(fresh) < int(wcfg.get("min_universe", 10)):
                return False  # not enough of the universe priced fresh yet
            meta = (getattr(self, "last_ai_scan_stats", {}) or {}).get("scanner", {}) or {}
            sync_ms = float(meta.get("sync_phase_elapsed_ms", 0) or 0)
            if sync_ms and sync_ms > float(wcfg.get("max_sync_ms", 8000)):
                return False  # scan phase not healthy yet
            return True
        except Exception:
            return True  # never let a readiness-check bug hard-block trading

    def _warmup_tick(self) -> None:
        """Advance warmup readiness ONCE PER SCAN CYCLE (signal-driven release).

        Called from the main trading loop after each scan — INDEPENDENT of whether
        any candidate survives to the entry gate. This is the fix for the ready-streak
        only accumulating when a candidate reached ``_warmup_blocks_entry``: with the
        June-tight gates filtering most candidates, the streak never grew and release
        fell to the 180s time backstop (a blind timer the operator explicitly rejected).
        Now the streak tracks the ACTUAL feed-sync signal (WS coverage across the
        universe + healthy scan phase), so entries release the moment the feed is
        genuinely warm — usually well under the backstop. Fail-OPEN on any error.
        """
        wcfg = ((self.config.get("trading") or {}).get("warmup"))
        if not isinstance(wcfg, dict) or not bool(wcfg.get("enabled", False)):
            return
        if getattr(self, "_warmup_cleared", False):
            return
        try:
            import time as _t
            if getattr(self, "_warmup_start_mono", None) is None:
                self._warmup_start_mono = _t.monotonic()
                self._warmup_ready_streak = 0
                self._warmup_last_cycle = -1
            cyc = int(getattr(self, "_unified_cycle_count", 0) or 0)
            if cyc == self._warmup_last_cycle:
                return  # already advanced this cycle (guard against a double-call)
            self._warmup_last_cycle = cyc
            self._warmup_ready_streak = (self._warmup_ready_streak + 1) if self._warmup_feed_ready(wcfg) else 0
            need = int(wcfg.get("ready_cycles", 3))
            max_sec = float(wcfg.get("max_warmup_sec", 180))
            if self._warmup_ready_streak >= need:
                self._warmup_cleared = True
                logging.info(
                    "[warmup] feed synced (%d ready cycles, %.0fs) — entries enabled",
                    self._warmup_ready_streak, _t.monotonic() - self._warmup_start_mono,
                )
            elif (_t.monotonic() - self._warmup_start_mono) >= max_sec:
                self._warmup_cleared = True
                logging.warning(
                    "[warmup] max_warmup_sec %.0fs reached — entries enabled WITHOUT full sync (backstop)", max_sec,
                )
        except Exception:
            self._warmup_cleared = True  # never let a warmup bug hard-block trading

    def _warmup_blocks_entry(self) -> bool:
        """Return True to BLOCK a new entry while the post-restart feed hasn't synced.

        Pure read of the warmup state that ``_warmup_tick()`` maintains once per scan
        cycle (signal-based release with a hard max-time backstop). Config-gated + fully
        reversible via ``trading.warmup.enabled``. Exits are never gated — only new entries.
        """
        wcfg = ((self.config.get("trading") or {}).get("warmup"))
        if not isinstance(wcfg, dict) or not bool(wcfg.get("enabled", False)):
            return False
        return not bool(getattr(self, "_warmup_cleared", False))

    def _maybe_recompute_adaptive_sizer(self, settle_summary: Dict[str, Any]) -> None:
        """Recompute the adaptive per-lane sizer shadow state from settled trades.

        Gated by ``trading.adaptive_sizer``. SHADOW-ONLY: writes
        ``adaptive_sizer_state.json`` + ``adaptive_sizer_shadow.jsonl`` and never
        mutates real position size (``resolve_size_mult`` returns 1.0 while
        ``mode != 'live'``). Throttled on new settles so it does not churn.
        """
        cfg = ((self.config.get("trading") or {}).get("adaptive_sizer")) or {}
        if not bool(cfg.get("enabled", False)):
            return
        min_new = int(cfg.get("recompute_min_new_settles", 10) or 10)
        # exit settler summary uses ``n_settled`` (newly resolved this run).
        if int(settle_summary.get("n_settled", 0) or 0) < min_new:
            return
        from src.analysis.adaptive_lane_sizer import build as _sizer_build, write as _sizer_write
        state = _sizer_build(self.config)
        _sizer_write(state)
        top = sorted(state.get("lanes", []), key=lambda l: -abs(l.get("ema_mult", 1.0) - 1.0))[:5]
        logging.info(
            "[adaptive-sizer] shadow recomputed mode=%s lanes=%d top_mults=%s",
            state.get("mode"), len(state.get("lanes", [])),
            {l["lane"]: l["ema_mult"] for l in top if l.get("ema_mult") != 1.0},
        )

    def _maybe_alert_exit_policy_drift(self, settle_summary: Dict[str, Any]) -> None:
        """Recompute the exit-policy shadow recommendation; queue drift for review.

        Gated by config ``lane_exit_policy``. Recommend-only: writes
        ``lane_exit_policy.json`` and ``docs/ACTIVE_RECOMMENDATIONS.md`` when the
        live exit config disagrees with the settled-data recommendation. Queue
        entries de-dup on drift signature so the same drift is not repeated every
        cycle.
        """
        cfg = (self.config.get("lane_exit_policy") or {})
        if not bool(cfg.get("enabled", True)):
            return
        min_new = int(cfg.get("recompute_min_new_settles", 25) or 25)
        if int(settle_summary.get("newly_settled", 0) or 0) < min_new:
            return
        from src.analysis.lane_exit_policy import (
            recompute,
            drift_signature,
            format_drift_message,
        )
        _payload, drift = recompute(min_n=int(cfg.get("min_lane_n", 5) or 5))
        sig = drift_signature(drift)
        if sig == self._last_exit_drift_sig:
            return  # nothing new since last alert
        self._last_exit_drift_sig = sig
        if not drift:
            return
        logging.info("[exit-policy] drift detected on %d lane(s)", len(drift))
        rec_path = append_active_recommendation(
            source="lane_exit_policy",
            title="Exit-policy drift",
            body=format_drift_message(drift),
            details={
                "drift_lanes": len(drift),
                "artifact": "data/calibration/lane_exit_policy.json",
            },
            links=["[[PSB Active Recommendations]]", "[[lane_exit_policy]]"],
        )
        logging.info("[exit-policy] drift recommendation written to %s", rec_path)

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
        config_path = self._resolve_config_path(config_path)

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

    def _resolve_config_path(self, config_path: str = None) -> Path:
        if config_path is None:
            return Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        return Path(config_path)

    def _config_file_mtime_ns(self) -> Optional[int]:
        path = getattr(self, "config_path", None)
        if path is None:
            return None
        try:
            return Path(path).stat().st_mtime_ns
        except OSError:
            return None

    def _code_reload_flag_mtime_ns(self) -> Optional[int]:
        try:
            return os.stat(self._code_reload_flag_path).st_mtime_ns
        except OSError:
            return None

    async def _maybe_hot_reload_code(self) -> None:
        """Hot-swap STRATEGY-module code without a restart, on explicit sentinel touch.

        Called ONLY from the trading-loop top, where the previous scan cycle has drained
        (no strategy scan/entry coroutine is in-flight). Serialized by _execution_lock so
        no entry executes during the swap. The concurrent fast-exit loop is strategy-free,
        so it is unaffected by reloading src/strategies. Scope: src/strategies/ only —
        edits to main.py / market / execution still need a restart. Never raises.
        """
        mtime_ns = self._code_reload_flag_mtime_ns()
        if mtime_ns is None or mtime_ns == getattr(self, "_code_reload_flag_seen_mtime_ns", None):
            return
        async with self._execution_lock:
            self._apply_code_reload(mtime_ns)

    def _apply_code_reload(self, mtime_ns) -> None:
        """Synchronous reload body (no awaits => atomic w.r.t. the event loop).

        Stage 1 (compile-check ALL) aborts UNTOUCHED on any syntax error. Stages 2-4
        (reload/rebind/rebuild) mutate module dicts in place, so a failure there can leave
        MIXED code — that path FAILS CLOSED: set _code_reload_broken, which halts new
        entries via the _unified_cycle guard (exits keep running), and page for a restart.
        """
        import importlib
        import sys as _sys
        import time as _t
        t0 = _t.monotonic()
        # 1) syntax-check every target file first — abort before touching anything
        try:
            for _name in _HOT_RELOAD_CODE_MODULES:
                _m = _sys.modules.get(_name)
                _f = getattr(_m, "__file__", None) if _m is not None else None
                if _f:
                    with open(_f, "r") as _fh:
                        compile(_fh.read(), _f, "exec")
        except Exception as e:
            self._code_reload_flag_seen_mtime_ns = mtime_ns
            logging.error("CODE_RELOAD_FAILED stage=compile err=%s — running code UNCHANGED (safe)", e)
            return
        # 2-4) reload -> rebind -> rebuild. ANY failure here => FAIL CLOSED.
        try:
            reloaded = []
            for _name in _HOT_RELOAD_CODE_MODULES:
                _m = _sys.modules.get(_name)
                if _m is not None:
                    importlib.reload(_m)
                    reloaded.append(_name)
            g = globals()
            _b = _sys.modules["src.strategies.bitcoin"]
            g["BitcoinStrategy"] = _b.BitcoinStrategy
            g["BitcoinSignal"] = _b.BitcoinSignal
            _s = _sys.modules["src.strategies.sol_macro"]
            g["SolMacroStrategy"] = _s.SolMacroStrategy
            g["SolMacroSignal"] = _s.SolMacroSignal
            g["ETHMacroStrategy"] = _sys.modules["src.strategies.eth_macro"].ETHMacroStrategy
            g["HYPEMacroStrategy"] = _sys.modules["src.strategies.hype_macro"].HYPEMacroStrategy
            g["XRPMacroStrategy"] = _sys.modules["src.strategies.xrp_macro"].XRPMacroStrategy
            g["DOGEMacroStrategy"] = _sys.modules["src.strategies.doge_macro"].DOGEMacroStrategy
            g["BNBMacroStrategy"] = _sys.modules["src.strategies.bnb_macro"].BNBMacroStrategy
            g["analysis_with_timeout"] = _sys.modules["src.strategies._scan_timeout"].analysis_with_timeout
            self._rebuild_runtime_config_dependents()
        except Exception as e:
            self._code_reload_broken = True
            self._code_reload_flag_seen_mtime_ns = mtime_ns
            logging.critical(
                "CODE_RELOAD_BROKEN stage=apply err=%s — NEW ENTRIES HALTED, exits continue, RESTART REQUIRED",
                e, exc_info=True,
            )
            return
        self._code_reload_flag_seen_mtime_ns = mtime_ns
        logging.info(
            "CODE_RELOAD ok modules=%d strategies=7 dur_ms=%.0f", len(reloaded), (_t.monotonic() - t0) * 1000.0
        )

    def _maybe_hot_reload_config_file(self) -> None:
        """Apply runtime-safe settings.yaml edits without restarting the bot."""
        path = getattr(self, "config_path", None)
        if path is None:
            return
        try:
            mtime_ns = Path(path).stat().st_mtime_ns
        except OSError as exc:
            logging.debug("config hot-reload stat failed: %s", exc)
            return
        if mtime_ns == getattr(self, "_config_mtime_ns", None):
            return

        try:
            with open(path, "r") as f:
                disk_config = yaml.safe_load(f) or {}
            updates = _build_hot_reload_updates(self.config, disk_config)
            if not updates:
                self._config_mtime_ns = mtime_ns
                self._last_config_hot_reload_error_mtime_ns = None
                logging.info("config hot-reload noticed settings.yaml change; no runtime-safe keys changed")
                return
            self.apply_config_updates(updates)
            self._config_mtime_ns = mtime_ns
            self._last_config_hot_reload_error_mtime_ns = None
            logging.warning(
                "config hot-reload applied without restart: sections=%s",
                sorted(updates),
            )
        except Exception as exc:
            if getattr(self, "_last_config_hot_reload_error_mtime_ns", None) != mtime_ns:
                logging.error("config hot-reload failed: %s", exc, exc_info=True)
                self._last_config_hot_reload_error_mtime_ns = mtime_ns

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
        # Rebuild the tape adapter from the (possibly hot-reloaded) config, but
        # PRESERVE its learned per-lane close history across reloads.
        _prev_adapter = getattr(self, "lane_tape_adapter", None)
        self.lane_tape_adapter = LaneTapeAdapter(self.config.get("lane_tape_adapter", {}))
        if _prev_adapter is not None and getattr(_prev_adapter, "_lanes", None):
            # hot-reload: keep the learned per-lane close history in-process.
            self.lane_tape_adapter._lanes = _prev_adapter._lanes
        else:
            # fresh process (restart): warm-start the adapter + Kelly streaks from
            # recent closed trades so the adaptive layer doesn't cold-start blind.
            self._hydrate_adaptive_state()
        if hasattr(self, "market_scanner") and self.market_scanner is not None:
            self.market_scanner.reload_from_config(self.config)
        if hasattr(self, "exit_manager") and self.exit_manager is not None:
            self.exit_manager.reload_from_config(self.config)
        # never-green-cut: refresh mode on config reload (Codex #4) so flipping it back to
        # shadow/off DISABLES the live cut without a restart. On disable, clear the pending
        # set + the exit_manager cut ids so no stale cut fires. (off->live still needs a
        # restart to construct the observer; the common disable path works live.)
        if hasattr(self, "_never_green_mode"):
            try:
                _ngm = str(
                    (self.config.get("never_green_cut", {}) or {}).get("mode", "off") or "off"
                ).lower()
                if _ngm != self._never_green_mode:
                    self._never_green_mode = _ngm
                    if _ngm != "live":
                        self._never_green_cut_pending = set()
                        if hasattr(self, "exit_manager") and self.exit_manager is not None:
                            self.exit_manager._never_green_cut_ids = set()
            except Exception:
                pass
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
        buy_no_cb = getattr(self, "_buy_no_skip_callback", None)
        self.bitcoin_strategy.buy_no_skip_callback = buy_no_cb
        self.sol_macro_strategy.buy_no_skip_callback = buy_no_cb
        self.eth_macro_strategy.buy_no_skip_callback = buy_no_cb
        self.hype_macro_strategy.buy_no_skip_callback = buy_no_cb
        self.xrp_macro_strategy.buy_no_skip_callback = buy_no_cb
        self.doge_macro_strategy.buy_no_skip_callback = buy_no_cb
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
                    condition_id=str(pos_data.get("condition_id") or ""),
                    market_slug=str(pos_data.get("market_slug") or ""),
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
        today = datetime.now(timezone.utc).date()
        daily_pnl = 0.0
        daily_trades = 0
        _seen_exit_trade_ids: set[str] = set()
        try:
            for entry in self.journal.get_all_entries(limit=5000):
                ts_str = entry.get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    ts_date = ts.astimezone(timezone.utc).date()
                except (ValueError, TypeError):
                    continue
                if ts_date != today:
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
        self.clob_client.set_olympus_credentials(
            api_key=api_keys.get("OLYMPUS_API_KEY"),
        )
        polymarket_key = api_keys.get("PRIVATE_KEY") or api_keys.get(
            "POLYMARKET_PRIVATE_KEY"
        )
        # In dry_run/paper mode we never place real orders, so skip live CLOB
        # credential init entirely — read-only book reads use a key-less L0 client.
        # (This also avoids needing the proxy funder/signature_type until live.)
        dry_run = self.config.get("trading", {}).get("dry_run", True)
        if self.clob_client.using_olympus():
            if not self.clob_client.olympus_configured():
                logging.warning(
                    "trading.execution_provider=olympus but OLYMPUS_API_KEY is not configured."
                )
            logging.info(
                "trading.execution_provider=olympus — skipping direct CLOB credential init."
            )
            return
        if dry_run:
            logging.info(
                "dry_run=true — skipping live CLOB credential init (paper mode "
                "uses key-less read-only book access; set polymarket.signature_type "
                "+ funder_address before going live on CLOB V2)."
            )
        elif polymarket_key:
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

    async def refresh_live_wallet_bankroll(self) -> bool:
        """Use authenticated Polymarket wallet collateral as bankroll in live mode."""
        if self.config.get("trading", {}).get("dry_run", True):
            return False
        try:
            # Use total account VALUE (cash + open positions = Olympus EQUITY) so the
            # bankroll matches what's actually in the account, not just free cash.
            balance = await asyncio.wait_for(
                self.clob_client.get_account_value(),
                timeout=float(self.config.get("trading", {}).get("wallet_balance_timeout_sec", 10)),
            )
        except asyncio.TimeoutError:
            logging.error("Timed out fetching Polymarket wallet bankroll.")
            return False
        if balance is None:
            logging.error(
                "Live bankroll could not be refreshed from Polymarket wallet; "
                "bankroll_source remains %s.",
                getattr(self, "bankroll_source", "unknown"),
            )
            return False
        self.bankroll = float(balance)
        self.bankroll_source = "live_wallet"
        self.risk_manager.bankroll = self.bankroll
        # Anchor the live-run start equity (persisted) and push equity-based P&L into
        # the journal so every display shows the REAL account change, not the journal's
        # trade-only sum (which misses manual trades / on-chain resolutions / fees).
        self._ensure_live_run_anchor(self.bankroll)
        anchor = getattr(self, "_live_run_equity_anchor", None)
        run_pnl = None
        if anchor is not None:
            run_pnl = round(float(self.bankroll) - float(anchor), 2)
            try:
                self.journal._live_pnl_override = run_pnl
            except Exception:
                pass
        logging.info(
            "Live bankroll refreshed from Olympus: $%s | run P&L $%s (anchor $%s)",
            f"{self.bankroll:,.2f}",
            f"{run_pnl:+,.2f}" if run_pnl is not None else "n/a",
            f"{anchor:,.2f}" if anchor is not None else "n/a",
        )
        return True

    def _ensure_live_run_anchor(self, current_equity: float) -> None:
        """Load (or initialize) the persisted live-run start-equity anchor used for
        equity-based P&L. Persisted so it survives restarts; delete the file to reset
        the run baseline. Falls back to current equity on first run."""
        if getattr(self, "_live_run_equity_anchor", None) is not None:
            return
        path = Path("data/runtime/live_run_equity_anchor.json")
        # A FRESH live session must anchor P&L at THIS run's starting equity — never a
        # stale persisted anchor from a prior run. That stale-anchor bug made a 0-trade
        # fresh launch display run P&L = current_wallet - old_anchor (e.g. $125.82 -
        # $198.55 from 2026-06-14 = -$72.73), which read as "session P&L" on every
        # screen. Fresh session => re-seed the anchor to current equity and overwrite the
        # file. RESUMED sessions keep the persisted anchor so their P&L stays continuous
        # across restarts (the original intent). 2026-07-27.
        _fresh = bool(getattr(self, "_fresh_session_created", False))
        if not _fresh:
            try:
                if path.exists():
                    self._live_run_equity_anchor = float(json.loads(path.read_text())["start_equity"])
                    logging.info("Live-run P&L anchor loaded: $%.2f", self._live_run_equity_anchor)
                    return
            except Exception as exc:
                logging.warning("Could not read live-run anchor (%s); re-seeding.", exc)
        self._live_run_equity_anchor = float(current_equity)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "start_equity": self._live_run_equity_anchor,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "note": "live Olympus run start equity",
            }))
            logging.info(
                "Live-run P&L anchor %s: $%.2f",
                "re-seeded (fresh session)" if _fresh else "initialized",
                self._live_run_equity_anchor,
            )
        except Exception as exc:
            logging.warning("Could not persist live-run anchor: %s", exc)

    async def reconcile_open_positions_with_venue(self) -> None:
        """Reconcile resumed journal positions against the live venue (Olympus).

        On a live restart the journal is resumed with its open positions; this drops
        any that are no longer open on the venue (resolved or manually closed) so the
        bot never tries to sell shares it doesn't hold, and keeps the ones still open
        (the bot manages their exits). Fail-SAFE: if the venue fetch fails we change
        nothing — a harmless rejected sell beats wrongly abandoning a real position.
        """
        if self.config.get("trading", {}).get("dry_run", True):
            return
        # Venue-agnostic: Olympus via portfolio, direct CLOB via the Data API
        # (data-api.polymarket.com/positions). Either returns None on fetch failure
        # so we fail SAFE (keep all journal positions) rather than abandon a real one.
        if self.clob_client.using_olympus():
            live_cids = await self.clob_client.olympus_open_condition_ids()
        else:
            live_cids = await self.clob_client.clob_open_condition_ids()
        if live_cids is None:
            logging.warning(
                "Position reconcile skipped: could not fetch venue positions "
                "(keeping all journal positions)."
            )
            return

        def _cid(pos: dict) -> str:
            return str(
                pos.get("condition_id")
                or (pos.get("entry_signal") or {}).get("condition_id")
                or ""
            ).lower()

        import time as _time
        from datetime import datetime as _dt, timezone as _tz

        _now = _time.time()
        _grace_sec = float(
            (self.config.get("trading") or {}).get("position_reconcile_grace_sec", 120)
            or 120
        )

        def _opened_epoch(pos: dict):
            raw = pos.get("opened_at") or pos.get("timestamp")
            if not raw:
                return None
            try:
                t = _dt.fromisoformat(str(raw).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=_tz.utc)
                return t.timestamp()
            except Exception:
                return None

        # Consecutive-absence tracker (Codex-hardened): a single spurious empty/absent
        # Data-API response (200 [] glitch) must NOT wipe real positions. Require N
        # consecutive successful snapshots with the position absent before dropping.
        _absent = getattr(self, "_reconcile_absent_rounds", None)
        if _absent is None:
            _absent = self._reconcile_absent_rounds = {}
        _confirm_rounds = int(
            (self.config.get("trading") or {}).get(
                "position_reconcile_confirm_rounds", 2
            )
            or 2
        )
        kept = dropped = skipped_young = skipped_unconfirmed = 0
        _seen_tids = set()
        for pos in list(self.journal.get_open_positions()):
            tid = pos.get("trade_id")
            _seen_tids.add(tid)
            if _cid(pos) and _cid(pos) in live_cids:
                kept += 1
                _absent.pop(tid, None)  # present on venue -> reset absence streak
                continue
            # Age grace: never drop a just-placed position (Data-API index lag), and
            # never drop one whose age can't be parsed (unknown age = fail-safe keep).
            _oe = _opened_epoch(pos)
            if _oe is None or (_now - _oe) < _grace_sec:
                skipped_young += 1
                _absent.pop(tid, None)  # can't confirm -> reset streak
                continue
            # Absent from a SUCCESSFUL, aged snapshot. Require N consecutive absent
            # snapshots before the destructive drop.
            _cnt = _absent.get(tid, 0) + 1
            _absent[tid] = _cnt
            if _cnt < _confirm_rounds:
                skipped_unconfirmed += 1
                continue
            _absent.pop(tid, None)
            dropped += 1
            self.journal.log_reconcile_drop(
                tid,
                bankroll=float(getattr(self, "bankroll", 0.0) or 0.0),
                reason="venue_absent_reconcile_drop",
                extra={
                    "absent_rounds": _cnt,
                    "confirm_rounds": _confirm_rounds,
                    "condition_id": _cid(pos),
                },
            )
            try:
                self.risk_manager.active_positions.pop(tid, None)
            except Exception:
                pass
            logging.info(
                "Reconcile: dropped phantom/closed position %s (%s) — absent from venue "
                "%d consecutive snapshots.",
                tid, str(pos.get("market_question"))[:40], _cnt,
            )
        # Prune the absence tracker for positions no longer in the journal.
        for _k in list(_absent.keys()):
            if _k not in _seen_tids:
                _absent.pop(_k, None)
        journal_cids = {_cid(p) for p in self.journal.get_open_positions()}
        unmanaged = [c for c in live_cids if c not in journal_cids]
        logging.info(
            "Venue position reconcile: kept=%d dropped=%d skipped_young=%d "
            "skipped_unconfirmed=%d venue_unmanaged=%d",
            kept, dropped, skipped_young, skipped_unconfirmed, len(unmanaged),
        )
        if unmanaged:
            logging.warning(
                "Venue has %d open position(s) the bot is NOT tracking "
                "(manual/pre-restart) — manage these yourself: %s",
                len(unmanaged), [c[:14] for c in unmanaged],
            )

    async def ensure_live_credentials_ready(self) -> bool:
        """
        Proactive startup credential check for live mode.

        Polymarket L2 creds expire ~7 days after derivation, fail silently, and
        `set_credentials` only records when we *loaded* the .env creds — not how
        old they actually are. Run this before any authenticated call (bankroll
        fetch, orders) so a stale set is caught at boot, not mid-session.

        When `polymarket.rederive_credentials_on_start` is true, force an
        idempotent L1 re-derive (restart-safe bootstrap) so the bot always boots
        with freshly minted creds. Default off so explicitly-provisioned .env API
        keys are not silently overridden. Returns True when creds are usable;
        paper mode is always True.
        """
        if self.config.get("trading", {}).get("dry_run", True):
            return True
        if self.clob_client.using_olympus():
            return self.clob_client.olympus_configured()
        force = bool(
            (self.config.get("polymarket", {}) or {}).get(
                "rederive_credentials_on_start", False
            )
        )
        return await self.clob_client.ensure_fresh_credentials(force_rederive=force)

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

    def _ws_price_age_ms(self, token_id) -> Optional[float]:
        """Age (ms) of the cached WS book for ``token_id`` at call time — the
        entry/exit DESYNC proxy: how stale the quote we acted on was. None when
        there is no WS book (REST-only / not subscribed / no two-sided book).
        Read-only, never raises. Compared across environments (Mac+VPN vs the
        Montreal VPS) this quantifies whether lower latency tightens entries/exits.
        """
        try:
            tid = str(token_id or "").strip()
            if not tid:
                return None
            ws = getattr(self, "ws_client", None)
            books = getattr(ws, "order_books", None) if ws is not None else None
            book = books.get(tid) if books else None
            if book is None or not getattr(book, "last_update", 0):
                return None
            age = (asyncio.get_event_loop().time() - float(book.last_update)) * 1000.0
            return round(age, 1) if age >= 0 else None
        except Exception:
            return None

    def _entry_price_provenance(self, token_id) -> Dict[str, Any]:
        """Observe-only entry price provenance (2026-07-11): which source
        priced this token in the last scan ("ws" fresh book vs "rest"
        /midpoint fallback) and how old that price is NOW in ms (WS: age at
        pricing + elapsed since; REST: elapsed since the fetch — a 200
        /midpoint is server-fresh at response time). Distinguishes 'no WS
        book' from 'REST fallback succeeded'; ws_price_age_ms alone is null
        for both. Never raises; unknown -> None/None."""
        out: Dict[str, Any] = {"price_src": None, "price_asof_age_ms": None}
        try:
            tid = str(token_id or "").strip()
            if not tid:
                return out
            meta = getattr(self.market_scanner, "_last_price_src", None)
            rec = meta.get(tid) if meta else None
            if not rec:
                return out
            src, ts, age_at_pricing = rec
            now = asyncio.get_event_loop().time()
            out["price_src"] = src
            out["price_asof_age_ms"] = round(
                max(0.0, (now - float(ts)) * 1000.0) + float(age_at_pricing or 0.0), 1
            )
        except Exception:
            pass
        return out

    async def _entry_book_features(self, token_yes, token_no, leg_hint) -> Dict[str, Any]:
        """Book microstructure of the HELD leg at entry via a REST snapshot.

        ``leg_hint`` is the order ACTION (``BUY_NO``/``BUY_YES``), NOT the
        ``BUY``/``SELL`` execution side. The WS cache is empty for most tokens, so
        the prior WS-cache version returned no_ws_book ~always — this fetches the
        public CLOB book. Timeout-bounded; never raises; returns {} on any miss.
        Called ONLY from the fire-and-forget recorder (never inline under the
        execution lock).
        """
        try:
            s = str(leg_hint or "").upper()
            held = token_no if "NO" in s else token_yes
            tid = str(held or "").strip()
            if not tid:
                return {}
            book = await asyncio.wait_for(
                self.clob_client.fetch_order_book_snapshot(tid), timeout=3.0
            )
            if not book:
                return {"entry_book": "no_rest_book"}
            bids = list(book.get("bids") or [])
            asks = list(book.get("asks") or [])

            def _best(levels, want_high):
                px = None
                for r in levels:
                    try:
                        p = float(r.get("price"))
                    except Exception:
                        continue
                    if px is None or (p > px) == want_high:
                        px = p
                return px

            def _depth(levels, is_bid, n=5):
                try:
                    rows = sorted(
                        levels, key=lambda r: float(r.get("price", 0)), reverse=is_bid
                    )
                except Exception:
                    rows = levels
                tot = 0.0
                for r in rows[:n]:
                    try:
                        tot += float(r.get("size", 0))
                    except Exception:
                        pass
                return round(tot, 2)

            bb = _best(bids, True)
            ba = _best(asks, False)
            spread = round(ba - bb, 4) if (bb is not None and ba is not None) else None
            return {
                "entry_book_held_leg": "NO" if "NO" in s else "YES",
                "entry_book_best_bid": bb,
                "entry_book_best_ask": ba,
                "entry_book_spread": spread,
                "entry_book_bid_depth5": _depth(bids, True),
                "entry_book_ask_depth5": _depth(asks, False),
                "entry_book_n_bid": len(bids),
                "entry_book_n_ask": len(asks),
                "entry_book_one_sided": (len(bids) == 0 or len(asks) == 0),
                "entry_book_src": "rest",
            }
        except Exception:
            return {}

    async def _record_entry_book_async(
        self, trade_id, token_yes, token_no, leg_hint
    ) -> None:
        """Fire-and-forget: write held-leg book microstructure at entry to
        data/calibration/entry_book_shadow.jsonl keyed by trade_id (joins to the
        exit's mfe_pct). Spawned via _spawn_bg OUTSIDE the execution lock, so it
        adds no trading latency. Never raises."""
        try:
            feats = await self._entry_book_features(token_yes, token_no, leg_hint)
            if not feats:
                return
            import json as _json
            import os as _os
            import time as _time
            rec = {"trade_id": str(trade_id or ""), "ts": _time.time(), **feats}
            path = "data/calibration/entry_book_shadow.jsonl"
            _os.makedirs(_os.path.dirname(path), exist_ok=True)
            with open(path, "a") as _f:
                _f.write(_json.dumps(rec) + "\n")
        except Exception:
            pass

    def _write_live_scan_snapshot(self, opportunities: dict) -> None:
        """Publish the current scan's candidate markets to data/live_scans/scan_<ts>.json
        for the dashboard scanner + watchlist panels. Maps each up/down market to its
        strategy by asset keyword. Pruned to the last 3 files so it never accumulates
        toward OOM. Best-effort, never raises.
        """
        import glob as _glob
        scan_dir = os.path.join("data", "live_scans")
        os.makedirs(scan_dir, exist_ok=True)

        def _strat_for(q: str):
            ql = (q or "").lower()
            if "bitcoin" in ql or "btc" in ql:
                return "bitcoin"
            if "solana" in ql or " sol" in ql or "sol/" in ql:
                return "sol_macro"
            if "ethereum" in ql or "eth" in ql:
                return "eth_macro"
            if "ripple" in ql or "xrp" in ql:
                return "xrp_macro"
            if "dogecoin" in ql or "doge" in ql:
                return "doge_macro"
            if "binance coin" in ql or "bnb" in ql:
                return "bnb_macro"
            if "hyperliquid" in ql or "hype" in ql:
                return "hype_macro"
            return None

        seen: Set[str] = set()
        signals: List[Dict[str, Any]] = []
        for key in ("updown_5m", "updown_15m", "updown_1h", "high_liquidity"):
            for m in (opportunities.get(key) or []):
                mid = str(getattr(m, "id", "") or "").strip()
                if not mid or mid in seen:
                    continue
                q = str(getattr(m, "question", "") or "")
                strat = _strat_for(q)
                if not strat:
                    continue
                seen.add(mid)
                signals.append({
                    "strategy": strat,
                    "market_id": mid,
                    "market_question": q,
                    "price": float(getattr(m, "yes_price", 0) or 0),
                    "action": getattr(m, "action", None),
                })
        path = os.path.join(scan_dir, f"scan_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"ts": datetime.now(timezone.utc).isoformat(), "signals": signals}, fh)
        # prune: keep only the 3 most recent snapshots
        files = sorted(_glob.glob(os.path.join(scan_dir, "scan_*.json")),
                       key=os.path.getmtime, reverse=True)
        for old in files[3:]:
            try:
                os.remove(old)
            except Exception:
                pass

    def _wanted_clob_book_token_ids(self) -> List[str]:
        """CLOB token ids to keep subscribed on the WS book channel.

        Always includes open-position tokens (exit-path L2). When the scanner
        price overlay is on, also includes the tokens the scanner last priced
        (the scan universe) so mids stream in for the whole universe. Capped and
        diffed with stale-unsubscribe (see _sync_clob_ws_book_subscriptions) so
        the OrderBook map stays bounded — this is the path that OOM'd before.
        """
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
        ws_cfg = (self.config.get("trading") or {}).get("clob_ws") or {}
        if ws_cfg.get("subscribe_universe", False):
            cap = int(ws_cfg.get("universe_subscribe_cap", 400) or 400)
            # 2026-07-11 ws_cov fix: imminence-ordered universe (accumulated
            # across concurrent hydrate batches) so the cap keeps the current/
            # near windows; the old unordered set sliced arbitrarily + churned
            # every 15s. Fallback to the legacy set if the accessor is missing
            # (stale module mix) — never worse than the old behavior.
            _want_fn = getattr(self.market_scanner, "ws_want_token_ids", None)
            _universe = _want_fn() if callable(_want_fn) else (
                getattr(self.market_scanner, "_last_priced_token_ids", None) or set()
            )
            for tid in _universe:
                tid = str(tid or "").strip()
                if tid and tid not in seen:
                    seen.add(tid)
                    out.append(tid)
                    if len(out) >= cap:
                        break
        return out

    async def _sync_clob_ws_book_subscriptions(self, channel: str) -> None:
        """Subscribe WS to books for open-position tokens; unsubscribe stale ids.

        PRIORITY-FIRST + PACED (2026-07-29). _wanted_clob_book_token_ids() returns
        an ORDERED list (open-position tokens first, then imminence-ranked universe).
        The old code did `set(wanted)` and a set-difference, which DISCARDED that
        order — so on a reconnect the whole ~400-token universe re-added in arbitrary
        hash order and open-position/near-expiry books were NOT subscribed first.
        Diagnosed churn driver (session 180002): unsubscribed sockets die ~6s idle,
        subscribed sockets live ~21s — so getting book frames flowing FAST after a
        reconnect is what keeps the socket up. Fix: preserve priority order so the
        first chunk (which becomes the initial type:market subscription) carries open
        positions + nearest-expiry, and PACE the remaining chunks with a small sleep
        so a full re-add streams as a gentle feed instead of a back-to-back burst.
        Breadth is NOT reduced (coverage keeps sockets alive); only ORDER + PACING
        change. Chunk/pause are config-tunable.
        """
        ws = self.ws_client
        if ws.ws is None:
            return
        # 2026-07-13 restart passenger (operator GO, Codex GO): re-apply the 07-01 WSS
        # post-reconnect deferral lost in the 07-02 strip. The first type:market subscribe
        # after a (re)connect REPLACES the server-side subscription set; if the scanner
        # universe hasn't primed yet, coverage collapses to that partial set and most
        # markets sit on stale WS mids until additive subscribes refill it. Defer until
        # primed. Loop retries every ~15s; the log line below is the starvation watchdog.
        if channel == "market" and not getattr(ws, "_sent_initial_market_subscription", True):
            _ws_cfg = (self.config.get("trading") or {}).get("clob_ws") or {}
            if _ws_cfg.get("subscribe_universe", False):
                _priced = getattr(self.market_scanner, "_last_priced_token_ids", None) or set()
                if not _priced:
                    logging.info("clob_ws: deferring initial market subscription until scanner universe primes (avoid partial type:market replace)")
                    return
        wanted = self._wanted_clob_book_token_ids()  # ORDERED: open positions, then imminence
        have = set(ws.subscriptions.get(channel, set()))
        # Preserve priority order (a set-difference would lose it): first-seen wins.
        to_add: List[str] = []
        _add_seen: Set[str] = set()
        for t in wanted:
            if t and t not in have and t not in _add_seen:
                _add_seen.add(t)
                to_add.append(t)
        want_set = set(wanted)
        to_remove = [t for t in have - want_set if t]
        # 2026-07-11 Codex hardening: send subscriptions in chunks — a single
        # ~400-asset frame could be silently dropped server-side, leaving
        # ws_cov at 0 while bookkeeping says subscribed.
        _cw = (self.config.get("trading") or {}).get("clob_ws") or {}
        _chunk = max(1, int(_cw.get("subscribe_chunk_size", 50) or 50))
        _pause = float(_cw.get("subscribe_chunk_pause_sec", 0.4) or 0.0)
        if to_add:
            for _i in range(0, len(to_add), _chunk):
                try:
                    await ws.subscribe(channel, to_add[_i:_i + _chunk])
                except Exception as e:
                    logging.debug("clob ws subscribe: %s", e)
                # Pace between chunks so the socket streams a gentle feed — the
                # priority batch (open positions + nearest-expiry) already went in
                # chunk 1. No pause after the last chunk; skipped if _pause<=0.
                if _pause > 0 and (_i + _chunk) < len(to_add):
                    await asyncio.sleep(_pause)
        if to_remove:
            for _i in range(0, len(to_remove), _chunk):
                try:
                    await ws.unsubscribe(channel, to_remove[_i:_i + _chunk])
                except Exception as e:
                    logging.debug("clob ws unsubscribe: %s", e)

    async def _clob_ws_subscription_loop(self) -> None:
        ws_cfg = (self.config.get("trading") or {}).get("clob_ws") or {}
        channel = str(ws_cfg.get("book_channel", "market"))
        interval = float(ws_cfg.get("subscribe_interval_sec", 15))
        _fast = float(ws_cfg.get("subscribe_fast_resub_sec", 2.0) or 0.0)
        await asyncio.sleep(3)
        while self.running:
            try:
                await self._sync_clob_ws_book_subscriptions(channel)
            except Exception as e:
                logging.debug("clob ws subscription loop: %s", e)
            # 2026-07-29 ADAPTIVE CADENCE — root fix for the market-WS 1006 churn.
            # connect() clears the subscription set on every (re)connect, and an
            # UNSUBSCRIBED socket idle-dies in ~6s (measured: idle sockets die median
            # 5.7s vs 20.8s once subscribed). The fixed 15s loop re-subscribed too late —
            # the socket was already dead — so the loop never caught a live socket to feed,
            # a self-sustaining reconnect cycle (~106 reconnects/hr, ws_cov stuck ~0.50).
            # Poll FAST (~2s) while the channel has NO live subscriptions so a reconnect
            # re-subscribes before the idle-death window, then fall back to the steady 15s
            # delta cadence once subscribed. Fresh re-reads config so it's tunable/off (0).
            _delay = max(5.0, interval)
            _reason = "steady"
            if _fast > 0:
                _ws = getattr(self, "ws_client", None)
                _live = _ws is not None and getattr(_ws, "ws", None) is not None
                _has_subs = bool((getattr(_ws, "subscriptions", {}) or {}).get(channel))
                if _live and not _has_subs:
                    _delay = _fast
                    _reason = "fast_resub_unsubscribed"
                elif not _live:
                    _reason = "no_ws"
            # 2026-07-29 diagnostic (log-only): stamp the cadence decision onto the ws
            # client so its WS_RECV_ENDED death line shows whether fast-resub was active
            # when the socket died (proves the loop-phase race Codex flagged).
            _wsc = getattr(self, "ws_client", None)
            if _wsc is not None:
                try:
                    _wsc._last_subloop_delay = _delay
                    _wsc._last_subloop_reason = _reason
                except Exception:
                    pass
            await asyncio.sleep(_delay)

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
            extra={
                "mode": "paper" if self.config.get("trading", {}).get("dry_run", True) else "live",
                "exposure_managers": self._exposure_status_payload(),
            },
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
        # Never await it on startup or scan cycles.
        self._schedule_ghost_calibration_refresh(force=True)

        # Start the async-decoupled AI decision broker. After this returns,
        # strategies can enqueue/lookup decisions via self.ai_broker.
        if self.ai_broker is not None:
            await self.ai_broker.start()

        ws_cfg = (self.config.get("trading") or {}).get("clob_ws") or {}
        if ws_cfg.get("enabled", True):
            # 2026-07-29 Fix B: wire subscribe-on-open + cold-start connect deferral so the
            # market socket never idles unsubscribed (Polymarket 1006-kills those ~9s).
            # on_connect_subscribe: subscribe the instant the socket opens (also speeds
            # reconnect re-subscribe). subscription_ready_check: gate the FIRST connect
            # until the scanner token universe primes. Market channel only; user channel
            # keeps connect-immediately. Fail-open via connect_defer_max_sec in websocket.py.
            try:
                _mkt_chan = str(ws_cfg.get("book_channel", "market"))
                self.ws_client.on_connect_subscribe = (
                    lambda _c=_mkt_chan: self._sync_clob_ws_book_subscriptions(_c)
                )
                self.ws_client.subscription_ready_check = (
                    lambda: bool(getattr(self.market_scanner, "_last_priced_token_ids", None))
                )
            except Exception as _e:
                logging.debug("clob_ws Fix-B wiring skipped: %s", _e)
            self._spawn_bg(self.ws_client.listen())
            _ws_wd = getattr(self.ws_client, "silence_watchdog", None)
            if callable(_ws_wd):
                self._spawn_bg(_ws_wd())
            _ws_ka = getattr(self.ws_client, "keepalive", None)
            if callable(_ws_ka):
                self._spawn_bg(_ws_ka())
            self._spawn_bg(self._clob_ws_subscription_loop())
            try:
                if self.config.get("trading", {}).get("ws_candle_feed", {}).get("enabled"):
                    from src.market import ws_candle_feed as _wcf
                    self._spawn_bg(_wcf.get_feed().run(), name="ws_candle_feed")
            except Exception as _e:
                logging.warning("ws_candle_feed spawn failed (REST fallback): %s", _e)

            # 2026-07-29 (Phase-2 ④): user-channel fills — LIVE only (paper has no L2
            # creds), config-gated, and only when creds are actually present. Observe /
            # correctness-only; fails open (REST fill inference still runs if WS is down).
            _dry = self.config.get("trading", {}).get("dry_run", True)
            if (
                ws_cfg.get("user_channel_enabled", True)
                and not _dry
                and getattr(self, "user_ws_client", None) is not None
                and self.clob_client.get_ws_creds() is not None
            ):
                self._spawn_bg(self.user_ws_client.listen(), name="user_ws_listen")
                self._spawn_bg(self.user_ws_client.keepalive(), name="user_ws_keepalive")
                logging.info("Phase-2 ④ user-channel WS started (real fill events → filled_size truth)")
            elif not _dry and ws_cfg.get("user_channel_enabled", True):
                logging.warning(
                    "user-channel WS not started: creds_present=%s — fill accounting "
                    "falls back to REST inference.",
                    self.clob_client.get_ws_creds() is not None,
                )

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
            extra={"exposure_managers": self._exposure_status_payload()},
        )
        # Create the exit lock on the running loop, then start the fast exit
        # monitor alongside the scan loop (no-op if exit_check_interval_sec<=0).
        if self._exit_lock is None:
            self._exit_lock = asyncio.Lock()
        if self.exit_check_interval and self.exit_check_interval > 0:
            self._spawn_bg(self._fast_exit_loop(), name="fast_exit_loop")

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
                await self._maybe_hot_reload_code()
                self._unified_cycle_count += 1
                cycle_started = time.monotonic()
                await self._unified_cycle()  # _warmup_tick() runs inside, post-scan
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
                    sys.executable, "scripts/strategy_coach.py", "--days-back", "30",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                self._coach_proc = proc
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
                    output = stdout.decode(errors="replace") if stdout else ""
                    logging.info(f"[coach] Analysis complete:\n{output[-2000:]}")
                except asyncio.TimeoutError:
                    # wait_for cancels communicate() but leaves the child alive — kill it
                    # so a hung coach run does not linger past this loop or shutdown.
                    logging.warning("[coach] Daily analysis timed out after 5 minutes; killing coach process")
                    self._terminate_coach_proc()
                finally:
                    self._coach_proc = None
            except Exception as e:
                logging.error(f"[coach] Daily analysis error: {e}", exc_info=True)

    def _terminate_coach_proc(self) -> None:
        """Best-effort terminate/kill of an in-flight daily coach child process."""
        proc = self._coach_proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            logging.exception("[coach] failed to kill coach process")

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
            # 2026-07-30: stamp live-vs-paper so the Lane Pocket Lab can isolate LIVE-realized
            # rows (decisions = live realized only). dry_run True => paper; the lab further splits
            # paper into old_paper/live_like_paper by execution-field presence. Fail-safe: this
            # whole method is wrapped in try/except, and a missing key falls back to the heuristic.
            record["mode"] = (
                "paper" if self.config.get("trading", {}).get("dry_run", True) else "live"
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

    def _hydrate_adaptive_state(self) -> None:
        """Warm-start the tape adapter + Kelly streaks from recent closed trades.

        A process restart otherwise cold-starts both (empty ``_lanes`` / empty streak
        buffers), throwing away the tape knowledge the prior session accumulated —
        Codex flagged this as a strong amplifier of the post-restart WR collapse.
        Reads at most the last ``hydrate_max_age_hours`` of closed trades from
        trades.jsonl (chronological), replays them into the adapter (de-size-only, so
        replaying a stale close can never enlarge a position) and into Kelly streaks.
        Only called from the fresh-process branch of the rebuild; a hot-reload keeps
        ``_lanes`` in memory and never reaches here. Fully guarded — never raises.
        """
        try:
            adapter = getattr(self, "lane_tape_adapter", None)
            if adapter is None:
                return
            tcfg = self.config.get("lane_tape_adapter", {}) or {}
            if not bool(tcfg.get("hydrate_on_start", True)):
                return
            try:
                max_age_h = float(tcfg.get("hydrate_max_age_hours", 12.0) or 12.0)
            except (TypeError, ValueError):
                max_age_h = 12.0
            path = os.path.join("data", "calibration", "trades.jsonl")
            if not os.path.exists(path):
                return
            try:
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_h)).isoformat()
            except Exception:
                cutoff = ""
            rows = []
            with open(path) as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = str(d.get("ts") or "")
                    # ISO8601 UTC strings sort lexicographically; skip anything older
                    # than the cutoff so hydration reflects the CURRENT tape only.
                    if cutoff and ts and ts < cutoff:
                        continue
                    rows.append(d)
            # chronological (oldest -> newest) so record_close keeps the latest k/lane
            rows.sort(key=lambda d: str(d.get("ts") or ""))
            closes = []
            kelly = getattr(self, "kelly_sizer", None)
            for d in rows:
                strat = d.get("strategy")
                window = d.get("window") or d.get("window_size")
                side = d.get("action") or d.get("side")
                if not strat or not window or not side:
                    continue
                # Require a numeric pnl — a malformed row must be skipped, never
                # hydrated as a phantom never-green loss (Codex 2026-07-26).
                pnl = d.get("pnl")
                if pnl is None:
                    continue
                try:
                    pnl = float(pnl)
                except (TypeError, ValueError):
                    continue
                mfe = d.get("mfe_pct")
                closes.append((strat, window, side, mfe, pnl))
                if kelly is not None:
                    try:
                        win = bool(d.get("win")) if d.get("win") is not None else (pnl > 0)
                        kelly.record_outcome(strat, win, window)
                    except Exception:
                        pass
            ingested = adapter.hydrate(closes)
            # Persist immediately so the strategy readers (get_tape_admission_delta,
            # RSI-floor) see the hydrated deltas at once instead of a stale/missing
            # file — even on 0 rows, which clears stale >12h deltas to neutral
            # (Codex 2026-07-26 NO-GO fix).
            try:
                adapter.persist_state()
            except Exception:
                pass
            logging.info(
                "[tape-adapter] hydrated %d recent closes across %d lanes "
                "(<=%.1fh) on restart", ingested, len(adapter._lanes), max_age_h,
            )
        except Exception as _hexc:  # noqa: BLE001 — warm-start is best-effort
            logging.warning("adaptive-state hydrate skipped: %s", _hexc)

    def _apply_tape_adapter_size(
        self, final_size: float, strategy: str, window_size, action: str
    ) -> float:
        """Scale an entry's notional by the per-lane tape multiplier.

        No-op when the adapter is off/shadow (size_multiplier returns 1.0). The
        adapter can only REDUCE size (max_mult<=1.0), never enlarge it. Logs every
        active de-size so each decision is auditable on live fills. Fully guarded so
        a sizing edit can never raise inside the execute path.
        """
        try:
            adapter = getattr(self, "lane_tape_adapter", None)
            if adapter is None:
                return final_size
            mult = adapter.size_multiplier(strategy, str(window_size or ""), action or "")
            if mult < 0.999:
                logging.info(
                    "[tape-adapter] %s %s %s size %.2f -> %.2f (mult=%.2f) %s",
                    strategy, window_size, action, final_size,
                    final_size * mult, mult,
                    adapter.explain(strategy, str(window_size or ""), action or ""),
                )
                return final_size * mult
            # shadow mode still logs the intended cut without applying it
            if adapter.mode == "shadow":
                raw = adapter.raw_multiplier(strategy, str(window_size or ""), action or "")
                if raw < 0.999:
                    logging.info(
                        "[tape-adapter:shadow] %s %s %s WOULD size x%.2f",
                        strategy, window_size, action, raw,
                    )
        except Exception as _e:  # never break execution on a sizing helper
            logging.error("[tape-adapter] size apply error: %s", _e, exc_info=True)
        return final_size

    def _apply_adaptive_realized_size(
        self, final_size: float, strategy: str, window_size, action: str
    ) -> float:
        """Scale an entry's notional by the per-lane REALIZED-ROI multiplier (2c).

        Reads adaptive_lane_sizer.resolve_size_mult, which returns 1.0 unless
        trading.adaptive_sizer.mode == 'live'. So this is a NO-OP in shadow/off — safe
        to ship dark; it only moves size once the operator flips mode:live (2d). In
        live it applies the EMA-smoothed [floor,ceil] per-lane multiplier learned from
        recent realized ROI, then RE-CLAMPS the UP side to max_position_size /
        max_exposure_per_trade (a >1.0 mult must never blow past the risk caps). The
        DOWN side is allowed to shrink to the venue floor (~$1) — that IS the intended
        de-size of a losing lane. Fully guarded so a sizing edit can never raise inside
        the execute path. Applied AFTER _apply_tape_adapter_size (which is off) so the
        realized sizer is the single per-lane outcome layer (see 2b consolidation).
        """
        try:
            from src.analysis.adaptive_lane_sizer import resolve_size_mult, resolve_lane_cap
            t = self.config.get("trading", {}) or {}
            _sz = t.get("adaptive_sizer", {})
            _sizer_live = isinstance(_sz, dict) and _sz.get("mode") == "live"
            # 2026-08-06 (Codex MED R1): when the sizer is NOT live, return UNCHANGED — a true no-op. This
            # also avoids the max(1.0, ...) venue-floor touching a sub-$1 size in shadow/off. All the
            # per-lane cap + dust-floor logic below therefore runs ONLY in live.
            if not _sizer_live:
                return final_size
            mult = float(resolve_size_mult(
                self.config, strategy=strategy,
                window=str(window_size or ""), action=action or "",
            ) or 1.0)
            # 2026-08-06 NOTE: the old `mult==1.0 -> return final_size` early-out was REMOVED so the
            # per-lane CEILING and NO-DUST FLOOR always bind, even for a lane whose realized mult is 1.0
            # (a fade long at mult 1.0 must still be clamped DOWN to its $12 ceiling).
            new_size = final_size * mult
            # De-size venue floor FIRST (~$1)...
            new_size = max(1.0, new_size)
            _max = float(t.get("max_position_size", 0.0) or 0.0)
            _max_exp = float(t.get("max_exposure_per_trade", 0.0) or 0.0)
            # 2026-08-06 PER-LANE CEILING (operator sizing model, supersedes the 08-05 uniform proven cap):
            # resolve_lane_cap returns this lane's explicit $ ceiling (fade long $12 / catch long $17 /
            # good short $28 / proven short $40) when set, else the proven-gated uniform cap. Used AS the
            # cap — it may be HIGHER or LOWER than max_position_size (a fade long clamps DOWN to $12). The
            # realized MULT still gates the actual climb toward it.
            try:
                _pcap = float(resolve_lane_cap(
                    self.config, strategy=strategy,
                    window=str(window_size or ""), action=action or "") or 0.0)
                if _pcap > 0:
                    _max = _pcap
            except Exception:
                pass
            # 2026-08-06 (Codex HIGH): build ONE effective cap = min(lane/position cap, bankroll-exposure
            # cap). BOTH are hard risk limits; the dust floor below must never push size above EITHER. The
            # prior code clamped the floor only to the lane cap, so at low bankroll an $8 exposure cap could
            # be floored back up to $11 — a risk-cap violation.
            _eff_cap = _max if _max > 0 else None
            if _max_exp > 0:
                _exp_cap = float(self.bankroll or 0.0) * _max_exp
                _eff_cap = _exp_cap if _eff_cap is None else min(_eff_cap, _exp_cap)
            if _eff_cap is not None:
                new_size = min(new_size, _eff_cap)
            # 2026-08-06 NO-DUST FLOOR (operator: "no $1 trades; lowest $10-12"). Floor every admitted trade
            # to trading.min_live_notional, but NEVER above the effective risk cap. REPLACES the wr_gate ~$1
            # near-sitout (now disabled): a losing-but-kept lane rides this floor and climbs only on realized
            # proof; a true loser is SAT OUT via disable_buy_* flags. If the effective cap is itself below the
            # floor (tiny bankroll), the cap wins — the trade stays at the cap, not floored above it.
            _dust = float(t.get("min_live_notional", 0.0) or 0.0)
            if _dust > 0 and new_size > 0:
                new_size = max(new_size, _dust)
                if _eff_cap is not None:
                    new_size = min(new_size, _eff_cap)
            logging.info(
                "[adaptive-sizer:live] %s %s %s size %.2f -> %.2f (mult=%.2f)",
                strategy, window_size, action, final_size, new_size, mult,
            )
            return round(new_size, 2)
        except Exception as _e:  # never break execution on a sizing helper
            logging.error("[adaptive-sizer] size apply error: %s", _e, exc_info=True)
        return final_size

    def _lane_breaker_blocks(self, strategy: str, window_size, action: str) -> bool:
        """2026-08-05 PER-LANE BREAKER admission check. True => this lane is in cooldown
        (k consecutive stops), skip the entry. No-op unless trading.lane_breaker.enabled AND
        the lane is in its allow-list. Fail-open (never blocks on error)."""
        try:
            from src.analysis import lane_breaker
            if lane_breaker.is_blocked(self.config, strategy=strategy,
                                       window=window_size, action=action):
                logging.info("LANE_BREAKER cooldown skip %s|%s|%s", strategy, window_size, action)
                return True
        except Exception as _e:
            logging.debug("lane_breaker check error: %s", _e)
        return False

    def _apply_realized_pnl_to_bankroll(self, pnl: float) -> float:
        """Apply realized PnL to paper/live bankroll with a hard floor at zero."""
        # 2026-07-20 CREDITOR AUDIT: caught live — a fresh session's cash was credited
        # +$2.77 with ZERO journal exits (a settle of a PREVIOUS session's orphaned
        # position paid into the new bankroll). Log EVERY credit with its caller so the
        # next phantom names itself; the watcher pages BANKROLL_DRIFT when
        # bot.bankroll != initial + journal-realized.
        import traceback as _tb
        _caller = "?"
        try:
            _stack = _tb.extract_stack(limit=4)
            _caller = " <- ".join(f"{f.name}:{f.lineno}" for f in _stack[:-1][-2:])
        except Exception:
            pass
        logging.warning(
            "[bankroll-credit] pnl=%+.2f bankroll %.2f -> %.2f | caller: %s",
            float(pnl), float(self.bankroll),
            max(0.0, float(self.bankroll) + float(pnl)), _caller,
        )
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

    def _exposure_status_payload(self) -> Dict[str, Any]:
        """Serializable exposure-manager snapshot for split dashboard mode."""
        out: Dict[str, Any] = {
            "_source": "trading_runtime",
            "_ts": datetime.now(timezone.utc).isoformat(),
        }
        for key, attr in (
            ("btc", "btc_exposure_manager"),
            ("sol", "sol_exposure_manager"),
            ("eth", "eth_exposure_manager"),
            ("hype", "hype_exposure_manager"),
            ("xrp", "xrp_exposure_manager"),
            ("doge", "doge_exposure_manager"),
            ("bnb", "bnb_exposure_manager"),
        ):
            mgr = getattr(self, attr, None)
            if mgr is None:
                continue
            try:
                status = dict(mgr.get_status())
            except Exception as exc:  # noqa: BLE001 - runtime status must not break trading
                status = {"error": str(exc)}
            status["key"] = key
            out[key] = status
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
                        side=s.get("action", ""),  # BUY_YES/BUY_NO → up/down lane
                    )
                self.kelly_sizer.record_outcome(strat, s["pnl"] > 0, window)
                try:
                    # Resolution exits carry no excursion; a resolved winner reached
                    # favorable (price -> 1.0), a loser never did -> green from pnl sign.
                    self.lane_tape_adapter.record_close(
                        strat, window, s.get("action", ""),
                        mfe_pct=(1.0 if s["pnl"] > 0 else 0.0),
                        pnl=float(s["pnl"] or 0.0),
                    )
                    self.lane_tape_adapter.persist_state()
                except Exception as _e:
                    logging.error("[tape-adapter] record_close (settle) error: %s", _e)
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

        # Update live (unrealized) marks on open positions. Price them off the
        # SAME CLOB /midpoint used for entry + exit so the dashboard mark rides one
        # ruler; Gamma is only a fallback inside check_price_updates for markets we
        # couldn't price here. Display-only — does not drive exits or resolution.
        clob_marks: Dict[str, float] = {}
        for _pos in list(self.risk_manager.active_positions.values()):
            _mid = getattr(_pos, "market_id", "") or ""
            _tok = getattr(_pos, "token_id_yes", "") or ""
            if not _mid or not _tok or _mid in clob_marks:
                continue
            _mp = await self.clob_client.fetch_midpoint(_tok)
            if _mp is not None:
                clob_marks[_mid] = _mp
        updated = await asyncio.to_thread(
            self.resolution_tracker.check_price_updates,
            self.journal, self.bankroll, True, clob_marks
        )
        price_update_markets: Dict[str, float] = {}
        if isinstance(updated, tuple):
            updated_count, price_update_markets = updated
        else:
            updated_count = int(updated or 0)
        if updated_count:
            logging.info(f"{label} Updated prices on {updated_count} open positions")
            # NOTE: do NOT run exit checks here. _run_exit_checks serializes on
            # _exit_lock and is already driven by TWO callers (the 60s scan cycle +
            # the 10s fast-exit loop, both on executable bid/ask prices w/ liquidity).
            # A third caller on this price-update path (added 2026-06-14, reverted) made
            # the cycle block on _exit_lock while the fast-exit loop held it mid-exit,
            # hanging the whole trading loop after "Updated prices…". The marks logged
            # above are enough; the fast-exit loop handles the actual exits.

        # Snapshot
        self.journal.take_snapshot(self.bankroll)

    def _kill_switch_active(self) -> bool:
        """Return True if the manual global stop file exists (do not place new trades)."""
        return KILL_SWITCH_FILE.exists()

    def _reconcile_exposure_overrides(self) -> None:
        """Apply the split dashboard's pause controls to our in-process exposure
        managers. The dashboard (separate process) writes
        data/runtime/exposure_overrides.json; we reconcile each manager's manual
        pause to match every cycle. Same disk-coupled pattern as KILL_SWITCH so the
        dashboard pause/resume buttons work without an in-process bot reference.
        Fail-safe: any read/apply error leaves managers as-is (never raises)."""
        try:
            ov = exposure_overrides.read_overrides()
        except Exception:
            return
        managers = (
            self.btc_exposure_manager, self.sol_exposure_manager,
            self.eth_exposure_manager, self.hype_exposure_manager,
            self.xrp_exposure_manager, self.doge_exposure_manager,
            self.bnb_exposure_manager,
        )
        for mgr in managers:
            try:
                desired = exposure_overrides.lane_is_paused(
                    getattr(mgr, "lane_name", ""), overrides=ov
                )
                is_manual = bool(getattr(mgr, "_manual_pause", False))
                if desired and not is_manual:
                    mgr.manual_pause()
                    logging.warning(
                        "Exposure override: PAUSED lane=%s (from dashboard)",
                        getattr(mgr, "lane_name", "?"),
                    )
                elif not desired and is_manual:
                    mgr.manual_resume()
                    logging.warning(
                        "Exposure override: RESUMED lane=%s (from dashboard)",
                        getattr(mgr, "lane_name", "?"),
                    )
            except Exception:
                logging.debug("exposure override reconcile failed for one lane", exc_info=True)

    def _notify_manual_global_stop_once(self) -> None:
        """Send the manual-stop Discord embeds once per kill-switch activation."""
        if getattr(self, "_manual_global_stop_alert_sent", False):
            return
        self._manual_global_stop_alert_sent = True
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
        window = str(
            lane_meta.get("lane_window")
            or lane_meta.get("window_size")
            or lane_meta.get("window")
            or ""
        )
        decision = self.circuit_breakers.can_enter(
            action=action,
            active_positions=self.risk_manager.active_positions.values(),
            strategy=strategy,
            window=window,
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

    def _observe_spot_reversal(self, market_prices: Dict[str, float]) -> None:
        """SHADOW: flag in-profit updown positions whose UNDERLYING spot has reversed
        before the CLOB book gaps through the trail floor. Logging-only; never exits,
        never mutates any position/risk state. Inert unless spot_reversal_bank.mode set."""
        bank = getattr(self, "_spot_rev_bank", None)
        if bank is None:
            return
        try:
            from src.execution.spot_reversal_bank import symbol_for_strategy
            from src.market import ws_candle_feed as _wcf
            live_ids = set()
            for pos_id, pos in list(self.risk_manager.active_positions.items()):
                strat = str(getattr(pos, "strategy", "") or "")
                sym = symbol_for_strategy(strat)
                if not sym:
                    continue
                cy = market_prices.get(pos.market_id)
                entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
                if cy is None or entry <= 0:
                    continue
                live_ids.add(pos_id)
                cy = float(cy)
                entry_leg = getattr(pos, "entry_leg", "YES") or "YES"
                outcome = getattr(pos, "outcome", "") or ""
                if entry_leg == "NO":
                    down_bet, pnl_pct = True, (1.0 - cy - entry) / entry
                elif outcome == "NO":
                    down_bet = True
                    pnl_pct = (entry - cy) / (1.0 - entry) if entry < 1.0 else 0.0
                else:
                    down_bet, pnl_pct = False, (cy - entry) / entry
                spot = None
                try:
                    _df = _wcf.get_feed().get_klines(sym, "1m", 1)
                    if _df is not None and len(_df):
                        spot = float(_df["close"].iloc[-1])
                except Exception:
                    spot = None
                ev = bank.observe(
                    position_id=pos_id, down_bet=down_bet,
                    current_spot=spot, current_pnl_pct=pnl_pct,
                )
                if ev:
                    win = getattr(pos, "window_size", "") or getattr(pos, "updown_window", "") or "?"
                    logging.info(
                        "SPOT_REVERSAL_SHADOW %s|%s mode=%s peak=%+.1f%% now=%+.1f%% "
                        "giveback=%.1f%% rev=%.2f%% would_bank_at=%+.1f%% (LIVE exit follows separately)",
                        strat.replace("_macro", ""), win, self._spot_rev_mode,
                        ev["peak_pnl_pct"] * 100, ev["current_pnl_pct"] * 100,
                        ev["giveback_pct"] * 100, ev["reversal_pct"] * 100,
                        ev["current_pnl_pct"] * 100,
                    )
            for pid in list(getattr(bank, "_state", {}).keys()):
                if pid not in live_ids:
                    bank.drop(pid)
        except Exception as e:
            logging.debug("spot-reversal shadow error: %s", e)

    def _observe_never_green(self, market_prices: Dict[str, float]) -> None:
        """SHADOW: flag held updown positions that stay never-green past cut_after_secs.
        Logging-only; never exits, never mutates any position/risk state. Inert unless
        never_green_cut.mode set."""
        ng = getattr(self, "_never_green_cut", None)
        if ng is None:
            return
        try:
            # Persistent pending-cut set (Codex #3): a qualifying 5m/15m position stays
            # in the live cut set until it actually CLOSES, not just the one tick the
            # observer event fires (the observer fires once via st.fired). Pruned to live
            # positions each tick below.
            _pending = getattr(self, "_never_green_cut_pending", None)
            if _pending is None:
                _pending = set()
                self._never_green_cut_pending = _pending
            _ng_green_thr = float(
                (self.config.get("never_green_cut", {}) or {}).get("green_threshold_pct", 0.02)
                or 0.02
            )
            from datetime import datetime, timezone
            from src.execution.updown_exit_shared import CRYPTO_UPDOWN_STRATEGIES
            live_ids = set()
            for pos_id, pos in list(self.risk_manager.active_positions.items()):
                strat = str(getattr(pos, "strategy", "") or "")
                if strat not in CRYPTO_UPDOWN_STRATEGIES:
                    continue
                cy = market_prices.get(pos.market_id)
                entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
                if cy is None or entry <= 0:
                    continue
                opened = getattr(pos, "opened_at", None)
                if opened is None:
                    continue
                if isinstance(opened, str):
                    # defensive: a serialized/reloaded position may carry an ISO string
                    try:
                        opened = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                    except Exception:
                        continue
                tz = getattr(opened, "tzinfo", None)
                now = datetime.now(tz) if tz is not None else datetime.now()
                hold_s = (now - opened).total_seconds()
                if hold_s < 0:
                    continue
                live_ids.add(pos_id)
                # winner-safety (Codex #1): if this position EVER reached green on the
                # CANONICAL high-water (pos.peak_token_price — what check_exits + the
                # wide-book tracker update), it can NEVER be a never-green cut. Drop it
                # from the pending set every tick, even after the observer already fired.
                _cpk0 = getattr(pos, "peak_token_price", None)
                _cpk0 = float(_cpk0) if _cpk0 else entry
                if entry > 0 and ((_cpk0 - entry) / entry) >= _ng_green_thr:
                    _pending.discard(pos_id)
                cy = float(cy)
                entry_leg = getattr(pos, "entry_leg", "YES") or "YES"
                outcome = getattr(pos, "outcome", "") or ""
                if entry_leg == "NO":
                    pnl_pct = (1.0 - cy - entry) / entry
                elif outcome == "NO":
                    pnl_pct = (entry - cy) / (1.0 - entry) if entry < 1.0 else 0.0
                else:
                    pnl_pct = (cy - entry) / entry
                win = getattr(pos, "window_size", "") or getattr(pos, "updown_window", "") or "?"
                # 2026-08-04 PER-LANE cut_after_secs: prefer a "strategy:window:side" override,
                # else fall back to the by_window value (then the class default inside observe()).
                _ng_side = "BUY_NO" if (entry_leg or "YES") == "NO" else "BUY_YES"
                _ng_lane_key = f"{strat}:{win}:{_ng_side}"
                _cut_secs = self._never_green_cut_by_lane.get(_ng_lane_key)
                if _cut_secs is None:
                    _cut_secs = self._never_green_cut_by_window.get(str(win))
                ev = ng.observe(
                    position_id=pos_id, hold_seconds=hold_s, current_pnl_pct=pnl_pct,
                    cut_after_secs=_cut_secs,
                )
                if ev:
                    logging.info(
                        "NEVER_GREEN_SHADOW %s|%s mode=%s hold=%.0fs (thr=%.0fs) peak=%+.1f%% "
                        "would_cut_at=%+.1f%% (LIVE exit follows separately)",
                        strat.replace("_macro", ""), win, self._never_green_mode,
                        ev["hold_seconds"], ev["cut_after_secs"], ev["peak_pnl_pct"] * 100,
                        ev["would_cut_pnl_pct"] * 100,
                    )
                    # GRADUATED 2026-07-26: in LIVE mode, actually CUT 5m/15m never-green
                    # positions (1h stays shadow — its slow winners get false-cut). The
                    # close is executed by exit_manager.check_exits, which reads this set.
                    # 2026-07-29 per-lane exemption: some (strategy, window, side) lanes
                    # recover to resolution and the cut destroys them. ETH 5m BUY_NO settled
                    # 10/13 in-favor (+$61.66 left on the table by cutting). Config key
                    # never_green_cut.exempt_lanes = ["strategy|window|LEG"], LEG in {YES,NO}
                    # (YES=BUY_YES, NO=BUY_NO). Read live so it hot-reloads. Exempt lanes fall
                    # through to normal exit logic instead of the never-green cut.
                    _ngc_exempt = {
                        str(x) for x in (
                            (self.config.get("never_green_cut", {}) or {}).get("exempt_lanes", []) or []
                        )
                    }
                    _ng_lane_key = f"{strat}|{win}|{entry_leg}"
                    if (
                        self._never_green_mode == "live"
                        and str(win) in ("5m", "15m")
                        and _ng_lane_key not in _ngc_exempt
                    ):
                        _cpk = getattr(pos, "peak_token_price", None)
                        _cpk = float(_cpk) if _cpk else entry
                        if entry <= 0 or ((_cpk - entry) / entry) < _ng_green_thr:
                            _pending.add(pos_id)
                    # 2026-07-23: persist a STRUCTURED record so the would-cut event
                    # joins cleanly to its exit outcome. pos_id == entries.jsonl trade_id
                    # (position_id is set from trade_id, main.py:1855); ts in UTC so there
                    # is no PT/UTC mismatch. Shadow-only: append to a jsonl, never touches
                    # trading state. Nested try so one bad write can't abort the loop.
                    try:
                        import json as _json
                        _rec = {
                            "trade_id": str(pos_id),
                            "market_id": str(getattr(pos, "market_id", "") or ""),
                            "strategy": strat.replace("_macro", ""),
                            "window": str(win),
                            "ts_utc": datetime.now(timezone.utc).isoformat(),
                            "hold_seconds": ev["hold_seconds"],
                            "cut_after_secs": ev["cut_after_secs"],
                            "peak_pnl_pct": ev["peak_pnl_pct"],
                            "would_cut_pnl_pct": ev["would_cut_pnl_pct"],
                            "mode": self._never_green_mode,
                        }
                        with open("data/calibration/never_green_shadow.jsonl", "a") as _f:
                            _f.write(_json.dumps(_rec) + "\n")
                    except Exception as _we:
                        logging.warning(
                            "NEVER_GREEN_SHADOW write error: %s", _we, exc_info=True
                        )
            for pid in list(getattr(ng, "_state", {}).keys()):
                if pid not in live_ids:
                    ng.drop(pid)
            # Prune to ACTUAL open positions (Codex #3 re-review): prune against the real
            # active-position ids, NOT live_ids (which only holds positions observed with a
            # valid price THIS tick) — else a transient price-miss `continue` would drop a
            # still-open position from the one-shot pending set and it would never re-cut.
            try:
                _pending &= set(self.risk_manager.active_positions.keys())
            except Exception:
                _pending &= live_ids
            try:
                self.exit_manager._never_green_cut_ids = set(_pending)
            except Exception:
                pass
        except Exception as e:
            # 2026-07-23: was logging.debug (invisible at INFO) which hid why the shadow
            # logged 0 events despite qualifying positions. Surface it so the next session
            # shows the exact throw. On any error, clear the cut set so a failed tick can
            # never leave a STALE set that cuts positions that no longer qualify.
            try:
                self.exit_manager._never_green_cut_ids = set()
            except Exception:
                pass
            logging.warning("NEVER_GREEN_SHADOW error: %s", e, exc_info=True)

    async def _run_exit_checks(
        self,
        market_prices: Dict[str, float],
        market_token_ids: Dict[str, Any],
        market_liquidity: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Evaluate + handle exits for held positions under the exit lock.

        Shared by the 60s scan loop and the fast exit loop. The lock serializes the
        check+handle sequence between the two callers so a position can't produce two
        ExitDecisions (and two close orders) from concurrent passes. Returns the
        number of exits handled.
        """
        if self._exit_lock is None:
            self._exit_lock = asyncio.Lock()
        async with self._exit_lock:
            exits = self.exit_manager.check_exits(
                self.risk_manager.active_positions,
                market_prices,
                market_token_ids,
                market_liquidity,
            )
            for exit_decision in exits:
                await self._handle_exit_decision(exit_decision)
            return len(exits)

    def _advance_held_peak_from_yes_mid(self, pos, yes_mid: float) -> None:
        """Advance a held position's peak high-water from a YES /midpoint WITHOUT
        firing any exit. Requires TWO consecutive sane reads to confirm a new high
        (defeats phantom single-print spikes on thin books) while still capturing
        real sustained right-way moves regardless of entry price. Never raises;
        never writes market_prices."""
        try:
            ym = float(yes_mid)
            if not (0.0 < ym < 1.0):
                return
            entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
            if entry <= 0.0:
                return
            leg = str(getattr(pos, "entry_leg", "YES") or "YES").upper()
            token_price = (1.0 - ym) if leg == "NO" else ym
            if not (0.0 < token_price < 1.0):
                return
            prev = getattr(pos, "_pending_peak_mid", None)
            setattr(pos, "_pending_peak_mid", token_price)
            if prev is None:
                return  # need a second confirming read before trusting the high
            confirmed = min(float(prev), token_price)  # the level both reads agree on
            peak = float(getattr(pos, "peak_token_price", 0.0) or entry)
            if confirmed > peak:
                setattr(pos, "peak_token_price", confirmed)
        except Exception:
            return

    def _ws_mark_note(self, reason: str) -> None:
        """Count WS exit-mark path outcomes (used/absent/stale/one_sided/spread_wide/
        bad_mid) so the path is never a silent zero. Logged as WS_MARK_STATS ~300s."""
        st = getattr(self, "_ws_mark_stats", None)
        if st is None:
            st = {}
            self._ws_mark_stats = st
        st[reason] = st.get(reason, 0) + 1

    def _ws_subscribe_held_now(self, position) -> None:
        """Immediately WS-subscribe a just-filled position's YES/NO tokens (2026-07-11
        gap-through fix). The 15s subscription sync loop left a window where a fresh fill
        had NO WS book (WS_MARK absent) -> exit marks fell to lagging REST /midpoint and
        stops gapped through (stop 17% -> realized -38%). Fire-and-forget via _spawn_bg;
        never raises; no-op when clob_ws disabled or socket not up (sync loop covers it).
        """
        try:
            ws_cfg = (self.config.get("trading") or {}).get("clob_ws") or {}
            if not ws_cfg.get("enabled", True):
                return
            ws = getattr(self, "ws_client", None)
            if ws is None or ws.ws is None:
                return
            channel = str(ws_cfg.get("book_channel", "market"))
            toks = [t for t in (
                str(getattr(position, "token_id_yes", "") or "").strip(),
                str(getattr(position, "token_id_no", "") or "").strip(),
            ) if t]
            if toks:
                self._spawn_bg(ws.subscribe(channel, toks), name="ws_sub_on_fill")
        except Exception:
            logging.debug("ws subscribe-on-fill failed (ignored)", exc_info=True)

    def _ws_fresh_yes_mid(self, yes_token, max_age_sec):
        """Fresh WS book mid for a held YES token, or (None, None).

        Returns (mid, age_ms) only when the pushed CLOB WS book is (a) newer than
        ``max_age_sec``, (b) two-sided, (c) spread <= exit_max_book_spread, and
        (d) 0 < mid < 1. Sub-100ms exit-mark source (Option A, 2026-07-11); callers
        sanity-check it against the REST book mid and fall back to REST on any miss.
        Read-only, never raises.
        """
        try:
            tid = str(yes_token or "").strip()
            if not tid:
                return None, None
            ws = getattr(self, "ws_client", None)
            books = getattr(ws, "order_books", None) if ws is not None else None
            book = books.get(tid) if books else None
            if book is None or not getattr(book, "last_update", 0):
                self._ws_mark_note("absent")
                return None, None
            age_ms = (asyncio.get_event_loop().time() - float(book.last_update)) * 1000.0
            if age_ms < 0 or age_ms > float(max_age_sec) * 1000.0:
                self._ws_mark_note("stale")
                return None, None
            bb = book.best_bid
            ba = book.best_ask
            if bb is None or ba is None:
                self._ws_mark_note("one_sided")
                return None, None
            _excfg = (self.config.get("trading", {}) or {}).get("exit_rules", {}) or {}
            _max_spread = float(_excfg.get("exit_max_book_spread", 0.30) or 0.30)
            if (ba - bb) > _max_spread:
                self._ws_mark_note("spread_wide")
                return None, None
            mid = (bb + ba) / 2.0
            if mid <= 0.0 or mid >= 1.0:
                self._ws_mark_note("bad_mid")
                return None, None
            self._ws_mark_note("used")
            return mid, round(age_ms, 1)
        except Exception:
            return None, None

    async def _fetch_held_market_prices(self):
        """Build (market_prices, market_token_ids) for currently held markets only.

        Uses a cheap per-token CLOB book snapshot (no scan, no AI) to get a fresh YES
        mid for each market we hold a position in — this is what lets the fast exit
        loop run far more often than the scan loop without the scan cost.
        """
        market_prices: Dict[str, float] = {}
        market_token_ids: Dict[str, Any] = {}
        market_liquidity: Dict[str, Any] = {}
        seen: set = set()
        # 2026-07-11 Option C: pre-fetch all held YES-token books CONCURRENTLY (was a
        # sequential per-position await). Cuts exit-tick wall-clock when >1 position is
        # open; single-position = same cost. Fail-safe: any miss/exc -> per-pos REST below.
        _held_yts = []
        _seen_yt = set()
        for _p in list(self.risk_manager.active_positions.values()):
            _yt = getattr(_p, "token_id_yes", "") or ""
            if _yt and _yt not in _seen_yt:
                _seen_yt.add(_yt)
                _held_yts.append(_yt)
        _prefetched_books: Dict[str, Any] = {}
        if _held_yts:
            try:
                _bres = await asyncio.gather(
                    *[self.clob_client.fetch_order_book_snapshot(_t) for _t in _held_yts],
                    return_exceptions=True,
                )
                for _t, _r in zip(_held_yts, _bres):
                    _prefetched_books[_t] = None if isinstance(_r, Exception) else _r
            except Exception:
                _prefetched_books = {}
        for pos in list(self.risk_manager.active_positions.values()):
            mid = getattr(pos, "market_id", "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            yes_token = getattr(pos, "token_id_yes", "") or ""
            no_token = getattr(pos, "token_id_no", "") or ""
            if not yes_token:
                continue
            book = _prefetched_books.get(yes_token)
            if book is None:
                book = await self.clob_client.fetch_order_book_snapshot(yes_token)
            if not book:
                continue
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            best_bid = max((b["price"] for b in bids), default=None)
            best_ask = min((a["price"] for a in asks), default=None)
            # Phantom-stop guard. Thin up/down binaries near expiry often show a
            # ONE-SIDED book — a lone resting bid (e.g. 0.145) became the "price" and
            # fired a phantom stop while the true price was ~0.505 (2026-06-14
            # incident). Keep the spread guard for two-sided books. For one-sided
            # books, only evaluate exits when CLOB /midpoint gives a sane mark; never
            # use the lone bid/ask as the exit mark. (2026-06-26: one-sided fillable
            # books were being SKIPPED -> TP/stop never fired -> winners round-tripped.)
            _excfg = (self.config.get("trading", {}) or {}).get("exit_rules", {}) or {}
            _require_two_sided = bool(_excfg.get("exit_require_two_sided_book", True))
            _use_clob_midpoint = bool(_excfg.get("exit_mark_use_clob_midpoint", True))
            # 2026-07-11 Option A: fresh WS mid for the held token (sub-100ms) as the exit
            # mark, sanity-checked vs the REST book mid fetched above. ws_mark stays None
            # (=> REST fallback below) unless two-sided, spread-sane, age<=gate, non-divergent.
            _prefer_ws = bool(_excfg.get("exit_mark_prefer_ws", True))
            _ws_gate = float(_excfg.get("exit_ws_mark_max_age_sec", 1.5) or 1.5)
            ws_mark, ws_age_ms = (
                self._ws_fresh_yes_mid(yes_token, _ws_gate) if _prefer_ws else (None, None)
            )
            if ws_mark is not None and best_bid is not None and best_ask is not None:
                _rbmid = (best_bid + best_ask) / 2.0
                _maxdiv = float(_excfg.get("exit_ws_mark_max_divergence", 0.10) or 0.10)
                if abs(ws_mark - _rbmid) > _maxdiv:
                    logging.info(
                        "exit WS-mark distrust %s: ws=%.3f rest_book_mid=%.3f age=%.0fms — using REST",
                        mid, ws_mark, _rbmid, (ws_age_ms if ws_age_ms is not None else -1),
                    )
                    ws_mark = None
            # 2026-07-11 P3 telemetry: which source prices this exit tick (ws/midpoint/book_mid)
            _mark_src = "book_mid"
            if best_bid is not None and best_ask is not None:
                if _require_two_sided:
                    _spread = best_ask - best_bid
                    _max_spread = float(_excfg.get("exit_max_book_spread", 0.30) or 0.30)
                    if _spread > _max_spread:
                        # 2026-07-11 give-back fix (fix-a): wide book -> do NOT fire
                        # a stop (phantom-stop guard), but advance the trailing
                        # high-water from /midpoint so a right-direction winner's
                        # peak survives thin near-resolution stretches.
                        _hw_mid = ws_mark
                        if _hw_mid is None:
                            try:
                                _hw_mid = await self.clob_client.fetch_midpoint(yes_token)
                            except Exception:
                                _hw_mid = None
                        if _hw_mid is not None:
                            self._advance_held_peak_from_yes_mid(pos, float(_hw_mid))
                        # 2026-07-21 WIDE-BOOK HOLD-TO-RESOLUTION (operator GO; default 0=off).
                        # bnb 5m -$12.47 (mkt 3006688): a dead/illiquid updown book went WIDE
                        # (spread 0.32->0.49) on a FLAT underlying (BNB $572.43->572.44); the
                        # Polymarket book swung ~70% (mae -0.70) with NO real spot move, then
                        # the stop fired at -51% when the spread briefly tightened. When the
                        # book is wide AND resolution is imminent, the true 0/1 settle lands in
                        # seconds -- strictly better than marking an aligned position against a
                        # junk wide book. Scoped near-resolution so a FAR-from-resolution real
                        # collapse still hits the wide_book_stop_through path below. Uses
                        # pos.end_date (verified sane: secs_to_expiry_at_exit=128.2 on the
                        # incident; the -25120s held-eff bug is in the min-hold window-OPEN
                        # anchor, NOT end_date). Takes precedence over stop-through when close.
                        _wb_hold_secs = float(
                            _excfg.get("wide_book_hold_to_resolution_secs", 0.0) or 0.0
                        )
                        # Scope to updown lanes only (Codex 2026-07-21): the near-resolution
                        # hold is an updown-book behaviour; identify via the canonical
                        # window_size ('5m'/'15m'/'1h'), fall back to the "Up or Down" market
                        # name. A non-updown binary near expiry must NOT skip its exit here.
                        _wb_is_updown = (
                            str(getattr(pos, "window_size", "") or "").lower()
                            in ("5m", "15m", "1h")
                            or "up or down" in str(getattr(pos, "market_question", "") or "").lower()
                        )
                        if (
                            _wb_hold_secs > 0.0
                            and _wb_is_updown
                            and getattr(pos, "end_date", None) is not None
                        ):
                            try:
                                _wb_ed = pos.end_date
                                if _wb_ed.tzinfo is None:
                                    _wb_ed = _wb_ed.replace(tzinfo=timezone.utc)
                                _wb_secs_to_res = (
                                    _wb_ed - datetime.now(timezone.utc)
                                ).total_seconds()
                            except Exception:
                                _wb_secs_to_res = None
                            if (
                                _wb_secs_to_res is not None
                                and 0.0 < _wb_secs_to_res <= _wb_hold_secs
                            ):
                                logging.info(
                                    "wide-book hold-to-resolution: spread %.3f > %.3f for %s, "
                                    "%.0fs to settle <= %.0fs — HOLDING to settle (skip wide-book exit)",
                                    _spread, _max_spread, mid, _wb_secs_to_res, _wb_hold_secs,
                                )
                                continue
                        # 2026-07-21 WIDE-BOOK STOP-THROUGH (config-gated, default OFF).
                        # A position collapsing into resolution has a WIDE book BECAUSE
                        # it is collapsing; the unconditional skip below let it ride PAST
                        # its stop to ~0 (doge 1h|up -$10.47, 0.53->0.16, 07-21; 4 of 5
                        # doge losses gapped through the 0.30 stop on NORMAL latency —
                        # not a lag/gap-through-fill, the stop was never EVALUATED).
                        # When enabled, do NOT skip: fall through to exit eval, which
                        # marks on the SANE /midpoint (below, ~L3431) — the existing
                        # updown_stop_confirm_ticks (needs N consecutive ticks) and
                        # stop_use_executable_price guards prevent a one-tick phantom
                        # stop, and a /midpoint still above the stop leaves a genuine
                        # winner UNcut. Off => byte-identical legacy behavior (skip).
                        if not bool(
                            _excfg.get("wide_book_stop_through_enabled", False)
                        ):
                            logging.debug(
                                "exit price guard: spread %.3f > %.3f for %s — skip exit eval this tick",
                                _spread, _max_spread, mid,
                            )
                            continue
                        logging.info(
                            "wide-book stop-through: spread %.3f > %.3f for %s — evaluating exit on /midpoint",
                            _spread, _max_spread, mid,
                        )
                        _mark_src = "midpoint_wide_book_stopthrough"
                yes_price = (best_bid + best_ask) / 2.0
            elif _require_two_sided:
                if not _use_clob_midpoint:
                    logging.debug(
                        "exit price guard: one-sided book for %s (bid=%s ask=%s) and /midpoint disabled — skip exit eval this tick",
                        mid, best_bid, best_ask,
                    )
                    continue
                yes_price = await self.clob_client.fetch_midpoint(yes_token)
                if yes_price is None:
                    logging.debug(
                        "exit price guard: one-sided book for %s (bid=%s ask=%s) and no /midpoint — skip exit eval this tick",
                        mid, best_bid, best_ask,
                    )
                    continue
                _mark_src = "midpoint"
                logging.debug(
                    "exit price guard: one-sided book for %s (bid=%s ask=%s) — using /midpoint %.3f for exit eval",
                    mid, best_bid, best_ask, yes_price,
                )
            elif best_bid is not None:
                yes_price = best_bid
            elif best_ask is not None:
                yes_price = best_ask
            else:
                continue
            # Mark on the SAME CLOB /midpoint the scanner uses for entry, so entry,
            # mark, and stop ride one ruler. The hand-rolled (best_bid+best_ask)/2
            # above diverged from the entry midpoint on thin up/down books at window
            # open, cutting BUY_NO winners with phantom stops (2026-06-17). Keep the
            # book (fetched above) for the two-sided/spread guard + liquidity; only
            # the mark value switches. Fall back to the book mid if /midpoint is
            # unavailable. Opt-out: exit_mark_use_clob_midpoint: false.
            if ws_mark is not None:
                _mark_src = "ws"
                if abs(ws_mark - yes_price) > 0.05:
                    logging.info(
                        "exit mark WS<-book %s: ws=%.3f book/mid=%.3f age=%.0fms — using WS",
                        mid, ws_mark, yes_price, (ws_age_ms if ws_age_ms is not None else -1),
                    )
                yes_price = ws_mark
            elif _use_clob_midpoint and best_bid is not None and best_ask is not None:
                _mp = await self.clob_client.fetch_midpoint(yes_token)
                if _mp is not None:
                    _mark_src = "midpoint"
                    if abs(_mp - yes_price) > 0.05:
                        logging.info(
                            "exit mark divergence %s: /midpoint=%.3f vs book-mid=%.3f "
                            "(bid=%s ask=%s) — using /midpoint",
                            mid, _mp, yes_price, best_bid, best_ask,
                        )
                    yes_price = _mp
            market_prices[mid] = yes_price
            market_token_ids[mid] = (yes_token, no_token)
            # Final-window winner top-up SHADOW (logging-only; default-off). Reuses the
            # /midpoint mark + two-sided book already fetched above — no extra fetch.
            try:
                if getattr(self, "topup_shadow", None) is not None and self.topup_shadow.enabled:
                    _end = getattr(pos, "market_end_at", None) or getattr(pos, "end_date", None)
                    _mins_left = None
                    if _end is not None:
                        try:
                            _enddt = _end if isinstance(_end, datetime) else datetime.fromisoformat(
                                str(_end).replace("Z", "+00:00")
                            )
                            if _enddt.tzinfo is None:
                                _enddt = _enddt.replace(tzinfo=timezone.utc)
                            _mins_left = (_enddt - datetime.now(timezone.utc)).total_seconds() / 60.0
                        except Exception:
                            _mins_left = None
                    self.topup_shadow.observe(
                        trade_id=str(getattr(pos, "trade_id", "") or getattr(pos, "position_id", "")),
                        market_id=str(mid),
                        strategy=str(getattr(pos, "strategy", "") or ""),
                        window=str(getattr(pos, "window_size", "") or ""),
                        entry_leg=str(getattr(pos, "entry_leg", "YES") or "YES"),
                        entry_price=float(getattr(pos, "entry_price", 0.0) or 0.0),
                        yes_mark=float(yes_price),
                        best_ask_yes=best_ask,
                        best_bid_yes=best_bid,
                        mins_left=_mins_left,
                        oracle_basis_bps=getattr(pos, "oracle_basis_bps", None),
                        market_end_at=_end,
                    )
            except Exception:
                logging.debug("topup_shadow observe wiring failed (ignored)", exc_info=True)
            taker_fee_rate = await self.clob_client.fetch_taker_fee_rate(yes_token)
            # Compact YES-side liquidity snapshot for the (optional, default-off)
            # bid-depth exit. Sell-side support = the top YES bids we'd exit into.
            top_bids = sorted(
                (
                    (float(b.get("price", 0)), float(b.get("size", 0)))
                    for b in bids
                ),
                key=lambda pb: pb[0],
                reverse=True,
            )[:5]
            # Top YES asks (cheapest first) — the buy-side ladder. Lets realistic
            # paper fills walk long-NO exits (NO bid = 1 - YES ask) and short-YES
            # cover (buy back YES) instead of marking them at the midpoint.
            top_asks = sorted(
                (
                    (float(a.get("price", 0)), float(a.get("size", 0)))
                    for a in asks
                ),
                key=lambda pa: pa[0],
            )[:5]
            market_liquidity[mid] = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": (best_ask - best_bid)
                if (best_bid is not None and best_ask is not None)
                else None,
                "taker_fee_rate": taker_fee_rate,
                "mark_src": _mark_src,
                "mark_age_ms": (ws_age_ms if _mark_src == "ws" else None),
                "bids": [{"price": p, "size": s} for p, s in top_bids],
                "asks": [{"price": p, "size": s} for p, s in top_asks],
            }
        # 2026-07-11 WS-mark telemetry: surface why the WS exit mark is/isn't engaging.
        _st = getattr(self, "_ws_mark_stats", None)
        if _st and (time.monotonic() - getattr(self, "_ws_mark_stats_logged", 0.0)) > 300:
            self._ws_mark_stats_logged = time.monotonic()
            logging.info("WS_MARK_STATS %s", dict(_st))
        return market_prices, market_token_ids, market_liquidity

    async def _run_topup_wide_sampler(self) -> None:
        """Sample ALL BTC 5m markets in their final window for the standalone top-up
        shadow, using REAL /midpoint marks (never the scanner 0.5 placeholder). Log-only,
        no execution. Fetches only for markets actually inside the final window (small set),
        so the per-tick cost is 1-3 /midpoint+book calls. Wrapped; never raises into the loop.
        """
        if not (self.topup_sampler_enabled and getattr(self, "topup_shadow", None) is not None
                and self.topup_shadow.enabled):
            return
        # Skip while _execution_lock is held (an entry is executing OR a strategy code
        # hot-reload is swapping self.bitcoin_strategy). The sampler reads that object, so
        # do not run it mid-swap. Shadow/log-only: skipping a tick costs nothing.
        if self._execution_lock.locked():
            return
        universe = list(self._topup_universe or [])
        if not universe:
            return
        # throttle: skip if we ran too recently (avoids per-tick pool contention)
        mono = time.monotonic()
        if mono - self._topup_last_run < self._topup_sampler_min_interval_s:
            return
        self._topup_last_run = mono
        now = datetime.now(timezone.utc)
        win = self._topup_sampler_window_mins
        # 2026-06-20: compute the BTC oracle basis ONCE per run (spot vs Chainlink) —
        # identical across all btc 5m markets at this instant. This wires the oracle
        # gate the sampler previously stubbed to None (oracle_ok was 0/N). Best-effort:
        # any failure leaves basis None (no worse than before); never blocks the loop.
        _btc_basis_bps = None
        _btc_oracle_price = None
        _btc_spot = None
        _btc_oracle_age_s = None
        try:
            _bsvc = getattr(self.bitcoin_strategy, "btc_service", None)
            # _shared_btc_ta is a flat TechnicalAnalysis (current_price), NOT a
            # container with a .sol sub-object; and BTCPriceService exposes
            # get_chainlink_price(), not get_chainlink_btc_price() (that lives on
            # SolBtcService). The old code hit both names -> basis was always None
            # -> oracle_ok never fired in the top-up shadow. (fix 2026-06-21)
            _ta = getattr(self, "_shared_btc_ta", None)
            _spot = float(getattr(_ta, "current_price", 0.0) or 0.0)
            _btc_spot = _spot if _spot > 0 else None
            if _bsvc is not None and _spot > 0:
                _cl, _cl_ts = await asyncio.to_thread(_bsvc.get_chainlink_price)
                if _cl and float(_cl) > 0:
                    _btc_oracle_price = float(_cl)
                    _btc_basis_bps = ((_spot - float(_cl)) / float(_cl)) * 10000.0
                    # oracle feed freshness — research flags stale-quote windows as the
                    # ones where the apparent edge has already evaporated.
                    if _cl_ts is not None:
                        _ts = _cl_ts if _cl_ts.tzinfo else _cl_ts.replace(tzinfo=timezone.utc)
                        _btc_oracle_age_s = (now - _ts.astimezone(timezone.utc)).total_seconds()
        except Exception:
            logging.debug("topup sampler: BTC oracle basis calc failed (ignored)", exc_info=True)
        for market_id, yes_token, no_token, end_date, question in universe:
            try:
                if not end_date or not yes_token:
                    continue
                ed = end_date if isinstance(end_date, datetime) else datetime.fromisoformat(
                    str(end_date).replace("Z", "+00:00"))
                if ed.tzinfo is None:
                    ed = ed.replace(tzinfo=timezone.utc)
                mins_left = (ed - now).total_seconds() / 60.0
                if mins_left < 0 or mins_left > win:
                    continue  # only fetch for markets actually in the final window
                mp = await self.clob_client.fetch_midpoint(yes_token)
                if mp is None:
                    continue  # no real mark -> skip (the whole point: never sample a placeholder)
                book = await self.clob_client.fetch_order_book_snapshot(yes_token)
                best_bid = best_ask = None
                if book:
                    bids = book.get("bids") or []
                    asks = book.get("asks") or []
                    best_bid = max((b["price"] for b in bids), default=None)
                    best_ask = min((a["price"] for a in asks), default=None)
                self.topup_shadow.observe_market(
                    market_id=str(market_id),
                    strategy="bitcoin",
                    window="5m",
                    yes_mark=float(mp),
                    best_ask_yes=best_ask,
                    best_bid_yes=best_bid,
                    mins_left=mins_left,
                    oracle_basis_bps=_btc_basis_bps,
                    oracle_price=_btc_oracle_price,
                    spot_price=_btc_spot,
                    oracle_age_s=_btc_oracle_age_s,
                    market_end_at=ed,
                )
            except Exception:
                logging.debug("topup wide sampler: market %s failed (ignored)", market_id, exc_info=True)

    def _sample_entry_tape_spots(self) -> None:
        """SHADOW: sample every asset's live spot into the entry-tape ring buffer.

        Called off the 3s fast-exit tick. O(1) per asset (candle-feed live last bar),
        fully fail-open, gated on trading.entry_tape_shadow.enabled. Never touches
        trading state — it only feeds the never-green fill-instant micro-move shadow."""
        try:
            cfg = (self.config.get("trading", {}) or {}).get("entry_tape_shadow", {}) or {}
            if not bool(cfg.get("enabled", False)):
                return
            from src.market import ws_candle_feed as _wcf
            from src.analysis import entry_tape_shadow as _ets
            feed = _wcf.get_feed()
            now = time.time()
            for _sym in set(_ets.STRATEGY_SYMBOL.values()):
                px = feed.get_last_price(_sym)
                if px is not None:
                    _ets.sample_spot(_sym, px, now)
        except Exception:
            return

    def _capture_entry_tape(self, *, trade_id, strategy, window, action, extra=None) -> None:
        """SHADOW: log the fill-instant spot micro-move for this entry (fail-open).

        Gated on trading.entry_tape_shadow.enabled. Reads the in-memory spot ring buffer
        and appends one row to tape_entry_shadow.jsonl keyed by trade_id — never affects
        the entry, size, or exit."""
        try:
            cfg = (self.config.get("trading", {}) or {}).get("entry_tape_shadow", {}) or {}
            if not bool(cfg.get("enabled", False)):
                return
            from src.analysis import entry_tape_shadow as _ets
            lookbacks = cfg.get("lookbacks_sec") or [10, 20, 30]
            # Capture the fill-instant timestamp NOW, then do the buffer-read + jsonl
            # append off-thread so a disk stall can never add latency to the entry
            # coroutine (Codex hardening; timing is preserved via the passed now_ts).
            _now_ts = time.time()
            self._spawn_bg(asyncio.to_thread(
                _ets.capture_entry,
                trade_id=str(trade_id),
                strategy=strategy,
                window=window,
                action=action,
                now_ts=_now_ts,
                lookbacks_sec=[float(x) for x in lookbacks],
                extra=extra or {},
            ))
        except Exception:
            return

    async def _fast_exit_loop(self) -> None:
        """Decoupled TP/SL monitor — see exit_check_interval in __init__.

        Runs only the exit path on held positions at a fast cadence so the stop fires
        near its configured threshold instead of after a fast-moving short-window
        contract has already collapsed. Skips entirely when no positions are open.
        """
        interval = self.exit_check_interval
        if not interval or interval <= 0:
            logging.info("[fast-exit] disabled (exit_check_interval_sec<=0)")
            return
        if self._exit_lock is None:
            self._exit_lock = asyncio.Lock()
        logging.info("[fast-exit] monitor active: every %.0fs", interval)
        await asyncio.sleep(5)
        while self.running:
            try:
                self._maybe_hot_reload_config_file()
                if self.risk_manager.active_positions:
                    market_prices, market_token_ids, market_liquidity = await self._fetch_held_market_prices()
                    if market_prices and self.risk_manager.active_positions:
                        self._observe_spot_reversal(market_prices)
                        self._observe_never_green(market_prices)
                        n = await self._run_exit_checks(market_prices, market_token_ids, market_liquidity)
                        if n:
                            logging.info("[fast-exit] handled %d exit(s)", n)
                # WIDE top-up sampler — standalone, runs every tick regardless of held
                # positions (the position-mode shadow is starved; this is the real one).
                await self._run_topup_wide_sampler()
                # SHADOW: sample each asset's live spot into the entry-tape ring buffer
                # (fail-open, gated on trading.entry_tape_shadow.enabled). Never affects
                # trading; feeds the never-green fill-instant micro-move analysis.
                self._sample_entry_tape_spots()
                # Periodic venue reconcile (2026-07-27) — runs AFTER exit checks so a slow
                # Data API never delays an exit. Keeps the bot's open set matched to the
                # actual account (detect manual closes / resolutions / phantoms → right
                # count, no chasing gone positions). Throttled, fail-safe, age-graced, and
                # requires N consecutive absent snapshots before dropping. Zero disables.
                _rec_iv = float(
                    self.config.get("trading", {}).get(
                        "position_reconcile_interval_sec", 120
                    )
                    or 0
                )
                if _rec_iv > 0 and (
                    time.monotonic() - getattr(self, "_last_pos_reconcile_m", 0.0)
                ) >= _rec_iv:
                    self._last_pos_reconcile_m = time.monotonic()
                    try:
                        await self.reconcile_open_positions_with_venue()
                    except Exception as _re:
                        logging.warning("[reconcile] periodic error: %s", _re)
            except Exception as e:  # never let the monitor die
                logging.error("[fast-exit] error: %s", e, exc_info=True)
            await asyncio.sleep(interval)

    async def _handle_exit_decision(self, exit_decision: ExitDecision) -> None:
        """Exit order + journal + risk updates (serialized with other execution)."""
        async with self._execution_lock:
            try:
                pos = self.risk_manager.active_positions.get(
                    exit_decision.position_id
                )
                if pos is None:
                    logging.warning(
                        "Exit skipped: position %s is no longer active",
                        exit_decision.position_id,
                    )
                    return

                dry_run = self.config.get("trading", {}).get("dry_run", True)
                pending_exit_order_id = str(
                    getattr(pos, "pending_exit_order_id", "") or ""
                )
                if pending_exit_order_id and not dry_run:
                    status = await self.clob_client.get_order_status(
                        pending_exit_order_id
                    )
                    if status == OrderStatus.FILLED:
                        setattr(pos, "pending_exit_order_id", "")
                        setattr(pos, "exit_pending_ticks", 0)
                        order = True
                    elif status in (OrderStatus.CANCELLED, OrderStatus.FAILED, None):
                        logging.warning(
                            "Prior exit order %s for %s is %s; retrying close",
                            pending_exit_order_id,
                            exit_decision.position_id,
                            status.value if isinstance(status, OrderStatus) else status,
                        )
                        setattr(pos, "pending_exit_order_id", "")
                        order = None
                    else:
                        # 2026-07-27 DIAGNOSTIC (operator-approved "A", READ-ONLY): the
                        # ride-to-zero freeze hinges on whether a stuck marketable exit
                        # order is RESTING (active on the book) or a KILLED FAK misreported
                        # as pending. Log the full CLOB order record ONCE per order_id so
                        # the next stuck loser tells us definitively. No order actions.
                        _dbg_ids = getattr(self, "_stuck_exit_dbg_ids", None)
                        if _dbg_ids is None:
                            _dbg_ids = self._stuck_exit_dbg_ids = set()
                        if pending_exit_order_id not in _dbg_ids:
                            _dbg_ids.add(pending_exit_order_id)
                            try:
                                _rec = await self.clob_client.debug_stuck_order(
                                    pending_exit_order_id
                                )
                                logging.warning(
                                    "STUCK-EXIT DEBUG pos=%s marketable=%s action=%s "
                                    "exit_price=%s size=%s reason=%s :: %s",
                                    exit_decision.position_id,
                                    getattr(exit_decision, "marketable", None),
                                    getattr(exit_decision, "action", None),
                                    getattr(exit_decision, "exit_price", None),
                                    getattr(exit_decision, "size", None),
                                    getattr(exit_decision, "reason", None),
                                    _rec,
                                )
                            except Exception as _dbg_e:
                                logging.warning(
                                    "STUCK-EXIT DEBUG fetch failed for %s: %s",
                                    pending_exit_order_id,
                                    _dbg_e,
                                )
                        # 2026-07-27 RIDE-TO-ZERO FIX (killed-FAK-misreported-as-PENDING):
                        # a marketable (FAK) exit CANNOT rest — it fills or is killed. But
                        # a KILLED FAK leaves an empty /data/order record + no trade, and
                        # _recover_status_from_trades then returns PENDING (clob_client.py
                        # tail), so the old code froze here forever and the loser rode to
                        # resolution. Fix: after a GRACE window (enough ticks for a real
                        # fill to propagate to trade history and return FILLED above), a
                        # marketable exit still reading PENDING is a KILLED FAK -> cancel
                        # the dead order (idempotent) and RE-ARM a fresh FAK this tick.
                        # DOUBLE-SELL-SAFE: exits SELL the held token, so the venue caps
                        # the fill at actual holdings; and any real/partial fill would have
                        # returned FILLED (trade-history match), not this PENDING path.
                        # Restricted to SELL exits; a non-SELL close keeps the old wait.
                        _mk = bool(getattr(exit_decision, "marketable", False))
                        _is_sell = "SELL" in str(getattr(exit_decision, "action", "")).upper()
                        _pend = int(getattr(pos, "exit_pending_ticks", 0) or 0) + 1
                        setattr(pos, "exit_pending_ticks", _pend)
                        _grace = int(
                            (self.config.get("trading") or {}).get(
                                "exit_fak_pending_grace_ticks", 3
                            )
                            or 3
                        )
                        if _mk and _is_sell and _pend > _grace:
                            try:
                                await self.clob_client.cancel_order(pending_exit_order_id)
                            except Exception as _ce:
                                logging.warning(
                                    "stale-exit cancel failed %s: %s",
                                    pending_exit_order_id,
                                    _ce,
                                )
                            setattr(pos, "pending_exit_order_id", "")
                            setattr(pos, "exit_pending_ticks", 0)
                            logging.warning(
                                "Stale FAK SELL exit %s for %s PENDING %d ticks (> grace "
                                "%d) => killed FAK; cancel + re-arm fresh FAK",
                                pending_exit_order_id,
                                exit_decision.position_id,
                                _pend,
                                _grace,
                            )
                            order = None
                        else:
                            logging.warning(
                                "Exit order still pending for %s: order_id=%s status=%s "
                                "(pending_ticks=%d/%d)",
                                exit_decision.position_id,
                                pending_exit_order_id,
                                status.value if isinstance(status, OrderStatus) else status,
                                _pend,
                                _grace,
                            )
                            return
                else:
                    order = None

                if order is None:
                    _marketable = bool(getattr(exit_decision, "marketable", False))
                    _limit_price = exit_decision.exit_price
                    # A+ (2026-07-27 ride-to-zero fix, Codex-flagged): a marketable exit
                    # must CROSS the book NOW, not rest at a mark that can sit behind the
                    # best bid/ask (FAK-at-mark prevents resting but not no-fill). Submit
                    # an AGGRESSIVE crossing limit for live FAK exits — SELL accepts any
                    # bid (floor ~= 1 tick), BUY accepts any ask (~= 1 - tick). Matching
                    # still fills at the REAL best bid/ask, so this never fills worse than
                    # the book. Recorded P&L uses exit_decision.unrealized_pnl (mark), NOT
                    # this limit (main.py ~4250/4273), so aggressive pricing does not
                    # corrupt accounting. Entries are unaffected (they price their own FAK
                    # in clob_client.place_entry_order). dry_run keeps the mark so paper
                    # fills stay realistic.
                    if _marketable and not dry_run:
                        try:
                            _tick = float(await self.clob_client.fetch_tick_size(exit_decision.token_id))
                        except (TypeError, ValueError):
                            _tick = 0.01
                        if not _tick or _tick <= 0:
                            _tick = 0.01
                        if "SELL" in str(exit_decision.action).upper():
                            _limit_price = _tick
                        else:  # BUY close
                            _limit_price = round(1.0 - _tick, 4)
                    # 2026-07-30 MAKER-FIRST EXIT (operator GO; default OFF via
                    # trading.exit_mode: marketable). ONLY non-marketable (take-profit /
                    # mark-based) exits on exit_hybrid_windows divert to the maker-first
                    # path; urgent/marketable exits keep the FAK aggressive-cross above
                    # UNCHANGED. When exit_mode != hybrid this branch is never taken, so
                    # behavior is byte-identical to before until the operator flips it.
                    _exit_params = self._exit_exec_params()
                    _exit_hybrid = (
                        not _marketable
                        and not dry_run
                        and _exit_params["exit_mode"] == "hybrid"
                    )
                    if _exit_hybrid:
                        # Aggressive taker fallback price so the FAK leg is guaranteed to
                        # cross if the maker leg doesn't fill (same crossing logic as the
                        # marketable path). The maker leg itself rests at the mark limit.
                        try:
                            _xtick = float(
                                await self.clob_client.fetch_tick_size(exit_decision.token_id)
                            )
                        except (TypeError, ValueError):
                            _xtick = 0.01
                        if not _xtick or _xtick <= 0:
                            _xtick = 0.01
                        _taker_px = (
                            _xtick
                            if "SELL" in str(exit_decision.action).upper()
                            else round(1.0 - _xtick, 4)
                        )
                        order = await self.clob_client.place_exit_order(
                            token_id=exit_decision.token_id,
                            side=exit_decision.action,
                            price=_limit_price,
                            size=exit_decision.size,
                            window=(getattr(pos, "window_size", "") or None),
                            market_id=exit_decision.market_id,
                            dry_run=dry_run,
                            taker_price=_taker_px,
                            maker_wait_sec=_exit_params["maker_wait_sec"],
                            hybrid_windows=_exit_params["hybrid_windows"],
                            market_title=getattr(pos, "market_question", None),
                            market_slug=getattr(pos, "market_slug", None),
                            condition_id=getattr(pos, "condition_id", None),
                        )
                    else:
                        order = await self.clob_client.place_order(
                            token_id=exit_decision.token_id,
                            side=exit_decision.action,
                            price=_limit_price,
                            size=exit_decision.size,
                            market_id=exit_decision.market_id,
                            dry_run=dry_run,
                            # Loss-cutting/near-resolution exits are marketable -> FAK
                            # (take the bid now). Other exits keep the GTC default.
                            order_type="FAK" if _marketable else "GTC",
                            market_title=getattr(pos, "market_question", None),
                            market_slug=getattr(pos, "market_slug", None),
                            condition_id=getattr(pos, "condition_id", None),
                        )
                    # 2026-07-30 (Codex HIGH fix): a maker-first exit can return an EXPLICIT
                    # PARTIAL (some shares sold at 0 fee, resting remainder already cancelled
                    # inside place_exit_order). The coarse get_order_status() recheck below
                    # returns FILLED on ANY trade-history match, so without this guard a
                    # PARTIAL would fall through to full-close accounting (realized PnL for
                    # the whole size, journal exit, position deleted) = phantom flat while
                    # the venue still holds size-filled. Treat PARTIAL as "position NOT
                    # flat": clear the (already-cancelled) pending id and keep the position
                    # open so the exit checker re-evaluates and works the remainder next tick
                    # (escalating to a marketable FAK if urgency rises). Do NOT book a close.
                    if (
                        _exit_hybrid
                        and order is not None
                        and not isinstance(order, bool)
                        and getattr(order, "status", None) == OrderStatus.PARTIAL
                        and not dry_run
                    ):
                        # Scoped to _exit_hybrid: place_exit_order ALWAYS cancels the resting
                        # remainder before returning PARTIAL, so clearing pending_exit_order_id
                        # is correct here. A plain (non-hybrid) GTC partial may still be resting
                        # live and keeps its existing pending_exit_order_id tracking below.
                        setattr(pos, "pending_exit_order_id", "")
                        setattr(pos, "exit_pending_ticks", 0)
                        logging.warning(
                            "Maker exit PARTIAL for %s (filled=%.4f/%.4f); position NOT "
                            "flat, keeping open, remainder retried next tick (no close booked).",
                            exit_decision.position_id,
                            float(getattr(order, "filled_size", 0.0) or 0.0),
                            float(exit_decision.size or 0.0),
                        )
                        return
                    if order and not dry_run:
                        status = await self.clob_client.get_order_status(order.order_id)
                        if status != OrderStatus.FILLED:
                            setattr(pos, "pending_exit_order_id", order.order_id)
                            logging.warning(
                                "Exit order accepted but not filled; keeping position open: "
                                "trade_id=%s order_id=%s status=%s",
                                exit_decision.position_id,
                                order.order_id,
                                status.value if isinstance(status, OrderStatus) else status,
                            )
                            return

                if order:
                    order_execution = (
                        dict(getattr(order, "execution", {}) or {})
                        if not isinstance(order, bool)
                        else {}
                    )
                    logging.info(
                        f"EXIT {exit_decision.reason}: {exit_decision.position_id[:12]} "
                        f"PnL=${exit_decision.unrealized_pnl:+.2f}"
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
                            side=(getattr(pos, "action", "") or getattr(pos, "outcome", "")) if pos else "",
                        )
                    self.kelly_sizer.record_outcome(strat, exit_pnl > 0, window)
                    try:
                        from src.analysis import lane_breaker
                        # Codex HIGH: Position has no .action and stores outcome as YES/NO — use the
                        # normalizer so the lane key matches config/admission (BUY_YES/BUY_NO).
                        lane_breaker.record_exit(
                            self.config, strategy=strat, window=window,
                            action=breaker_action,
                            exit_reason=exit_decision.reason,
                        )
                    except Exception:
                        pass
                    try:
                        self.lane_tape_adapter.record_close(
                            strat, window,
                            (getattr(pos, "action", "") or getattr(pos, "outcome", "")) if pos else "",
                            mfe_pct=float(getattr(exit_decision, "mfe_pct", 0.0) or 0.0),
                            pnl=float(exit_pnl or 0.0),
                        )
                        self.lane_tape_adapter.persist_state()
                    except Exception as _e:
                        logging.error("[tape-adapter] record_close error: %s", _e)
                    self.journal.log_exit(
                        trade_id=exit_decision.position_id,
                        exit_price=exit_decision.exit_price,
                        bankroll=self.bankroll,
                        reason=exit_decision.reason,
                        # 2026-07-30 PnL TRUTH FIX (Codex): book the same realized cash
                        # delta already applied to bankroll/exposure/kelly, not a mark
                        # recompute — closes the journal-vs-bankroll accounting gap.
                        realized_pnl=exit_pnl,
                        exit_telemetry={
                            "mae_pct": exit_decision.mae_pct,
                            "mfe_pct": exit_decision.mfe_pct,
                            "pnl_pct_at_exit": exit_decision.pnl_pct_at_exit,
                            "effective_stop_loss_pct": exit_decision.effective_stop_loss_pct,
                            "ws_price_age_ms": self._ws_price_age_ms(getattr(pos, "token_id_yes", None)),
                            # Per-lane fill quality (realistic_paper_fills): the mark
                            # the exit would have booked at vs what the sweep cost.
                            "fill_mark_price": getattr(exit_decision, "fill_mark_price", None),
                            "fill_slippage_pct": getattr(exit_decision, "fill_slippage_pct", None),
                            "fill_fee_usdc": getattr(exit_decision, "fill_fee_usdc", None),
                            "fill_fee_rate": getattr(exit_decision, "fill_fee_rate", None),
                            # PAPER CALIB Phase 3.6: signal vs execution PnL split (gap =
                            # exit slippage + fees). Judge lanes on execution_adjusted_pnl.
                            "raw_signal_pnl": getattr(exit_decision, "raw_signal_pnl", None),
                            "execution_adjusted_pnl": getattr(exit_decision, "execution_adjusted_pnl", None),
                            "secs_to_expiry_at_exit": getattr(exit_decision, "secs_to_expiry_at_exit", None),
                            "exit_book_spread": getattr(exit_decision, "exit_book_spread", None),
                            "exit_best_bid": getattr(exit_decision, "exit_best_bid", None),
                            "exit_best_ask": getattr(exit_decision, "exit_best_ask", None),
                            "exit_depth_at_limit": getattr(exit_decision, "exit_depth_at_limit", None),
                            "exit_fill_ratio": getattr(exit_decision, "exit_fill_ratio", None),
                            "exit_mark_src": getattr(exit_decision, "exit_mark_src", None),
                            "exit_mark_age_ms": getattr(exit_decision, "exit_mark_age_ms", None),
                            **order_execution,
                        },
                    )
                    self.circuit_breakers.record_exit(
                        reason=exit_decision.reason,
                        action=breaker_action,
                        strategy=strat,
                        window=window,
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
        if getattr(self, "_code_reload_broken", False):
            if not getattr(self, "_code_reload_broken_logged", False):
                logging.critical(
                    "CODE_RELOAD_BROKEN: skipping scan/entries every cycle until restart "
                    "(exits continue via fast-exit loop)"
                )
                self._code_reload_broken_logged = True
            return
        cycle_wall_start = time.perf_counter()
        cycle_timings_ms: Dict[str, Any] = {}
        logging.info("Starting trading cycle...")
        _write_runtime_status(
            phase="cycle_start",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
            extra={
                "cycle_count": int(self._unified_cycle_count or 0),
                "exposure_managers": self._exposure_status_payload(),
            },
        )
        self._maybe_hot_reload_config_file()

        from src.ops_pulse import _scan_skip_digest, _side_selection_digest, log_ops_pulse

        if self._kill_switch_active():
            logging.warning(
                "Manual global stop active (data/KILL_SWITCH present). Skipping trading cycle."
            )
            log_ops_pulse(self, "main")
            self._notify_manual_global_stop_once()
            return
        self._manual_global_stop_alert_sent = False

        # Apply the split dashboard's pause/resume controls (disk-coupled) to our
        # in-process exposure managers before scanning this cycle.
        self._reconcile_exposure_overrides()

        # Live: re-sync bankroll + run-P&L to the real venue equity every cycle so the
        # dashboard reflects the actual account (manual trades, on-chain resolutions,
        # and hidden fees never show in the journal's trade-only P&L). No-op in paper.
        if not self._is_dry_run_mode():
            try:
                await self.refresh_live_wallet_bankroll()
            except Exception as exc:
                logging.warning("Live bankroll re-sync failed this cycle: %s", exc)

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

        # Loop 3 — self-healing supervisor. Runs immediately AFTER perf-feedback
        # (which clobbers _runtime_feedback) so it can re-inject its loosen overrides.
        _sh = self.config.get("self_healing") or {}
        _shn = max(1, int(_sh.get("run_every_n_cycles", 6)))
        if bool(_sh.get("enabled", False)) and (self._performance_feedback_cycle % _shn == 0):
            try:
                from src.analysis.self_healing import SelfHealingSupervisor

                _sh_result = SelfHealingSupervisor(self.config).run()
                _sh_msgs = _sh_result.notify_messages
                if _sh_msgs:
                    _max = int((self.config.get("self_healing") or {}).get("escalation", {}).get("max_notify_per_run", 5))
                    _shown = _sh_msgs[:_max]
                    _batched = "\n".join(_shown)
                    if len(_sh_msgs) > _max:
                        _batched += f"\n...+{len(_sh_msgs) - _max} more suppressed (max_notify_per_run={_max})"
                    try:
                        rec_path = append_active_recommendation(
                            source="self_healing",
                            title="Self-healing escalation",
                            body=_batched,
                            details={
                                "messages": len(_sh_msgs),
                                "queue_dir": (self.config.get("self_healing") or {}).get("escalation", {}).get("queue_dir", "data/learning/escalations"),
                            },
                            links=["[[PSB Active Recommendations]]", "[[self_healing]]"],
                        )
                        logging.info("self_healing recommendation written to %s", rec_path)
                    except Exception as _ne:  # noqa: BLE001 — recommendation logging is best-effort
                        logging.debug("self_healing recommendation log failed: %s", _ne)
            except Exception as e:
                logging.warning("self_healing run failed: %s", e, exc_info=True)

        _write_runtime_status(
            phase="scanner_sync",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
            extra={"exposure_managers": self._exposure_status_payload()},
        )
        scanner_started = time.perf_counter()
        opportunities = await self.market_scanner.scan_for_opportunities()
        cycle_timings_ms["scanner_sync_ms"] = int(
            (time.perf_counter() - scanner_started) * 1000
        )
        high_liquidity = opportunities.get("high_liquidity", [])
        scanner_meta = opportunities.get("scanner_meta", {})
        # Refresh the BTC 5m universe for the wide top-up sampler (token ids + end times;
        # the sampler fetches its OWN fresh /midpoint, never uses m.yes_price which can be
        # the 0.5 placeholder). Cheap: just stash identifiers.
        try:
            if self.topup_sampler_enabled:
                self._topup_universe = [
                    (m.id, m.token_id_yes, m.token_id_no, m.end_date, m.question)
                    for m in (opportunities.get("updown_5m") or [])
                    if ("bitcoin" in (m.question or "").lower() or "btc" in (m.question or "").lower())
                    and m.token_id_yes and m.end_date is not None
                ]
        except Exception:
            logging.debug("topup universe refresh failed (ignored)", exc_info=True)
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

        # Signal-driven warmup: advance feed-readiness HERE — after the scan has
        # refreshed _last_price_src + last_ai_scan_stats['scanner'] and BEFORE any
        # strategy executes entries below. This gives same-cycle release: the cycle
        # whose streak reaches ready_cycles unblocks its own candidates, instead of
        # waiting one more scan (Codex nit). Idempotent per cycle; fail-open.
        self._warmup_tick()

        # Publish a live-scan snapshot for the dashboard scanner/watchlist panels
        # (they read data/live_scans/scan_*.json — nothing wrote it before, so the
        # panels were always blank). Bounded to the last few files (OOM-safe);
        # log-only, never raises into the scan loop.
        try:
            self._write_live_scan_snapshot(opportunities)
        except Exception:
            logging.debug("live-scan snapshot write failed (ignored)", exc_info=True)

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

        # Check active positions for exit conditions (TP/SL/time).
        # Price held markets via _fetch_held_market_prices (CLOB /midpoint + the
        # two-sided/spread guard), NOT the scanner's m.yes_price — the scanner
        # defaults yes_price to 0.5 when a /midpoint fetch misses (thin/just-opened
        # 5m/1h book), and that placeholder leaking into the exit check fired phantom
        # TP/stops on held positions (2026-06-17 audit: the window-size divide). This
        # also unifies the 60s scan-loop exit onto the same ruler as the 3s fast loop.
        try:
            exit_started = time.perf_counter()
            market_prices, market_token_ids, market_liquidity = (
                await self._fetch_held_market_prices()
            )
            if market_prices:
                await self._run_exit_checks(
                    market_prices, market_token_ids, market_liquidity
                )
            cycle_timings_ms["cycle_exit_check_ms"] = int(
                (time.perf_counter() - exit_started) * 1000
            )
        except Exception as e:
            cycle_timings_ms["cycle_exit_check_error"] = type(e).__name__
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

        # Compute BTC analysis ONCE per cycle and inject into the alt/eth lanes as
        # read-only DIAGNOSTIC context. Alts are decided by alt-native indicators
        # (_btc_trade_inputs_enabled() == False); btc_ta only feeds their logs +
        # signal metadata. Previously each of the 6 alt/eth lanes ran a full BTC
        # get_full_analysis every cycle purely to log it (~5x redundant). Using the
        # bitcoin strategy's service warms its 60s kline cache, so bitcoin's own
        # in-lane recompute is cheap. Off-loop + timeout-guarded like the lanes.
        _btc_diag_to = float(
            (self.config.get("strategies", {}).get("bitcoin", {}) or {}).get(
                "scan_analysis_timeout_sec", 15.0
            ) or 15.0
        )
        shared_btc_ta = await analysis_with_timeout(
            self.bitcoin_strategy.btc_service.get_full_analysis,
            lane="shared_btc_diag",
            timeout_sec=_btc_diag_to,
        )
        # 2026-06-20: cache for the final-window top-up sampler so it can compute the
        # BTC oracle basis (spot vs Chainlink) — previously the sampler passed
        # oracle_basis_bps=None so oracle_ok could never fire.
        self._shared_btc_ta = shared_btc_ta
        for _alt_strat in (
            self.sol_macro_strategy,
            getattr(self, "eth_macro_strategy", None),
            getattr(self, "xrp_macro_strategy", None),
            getattr(self, "hype_macro_strategy", None),
            getattr(self, "doge_macro_strategy", None),
            getattr(self, "bnb_macro_strategy", None),
        ):
            if _alt_strat is not None:
                _alt_strat._injected_btc_ta = shared_btc_ta
                _alt_strat._btc_ta_inject_set = True

        # ALL strategies always SCAN (calibration scope gates EXECUTION, not scanning —
        # so non-BTC lanes keep producing rejected-candidate / ghost / shadow evidence
        # during a BTC-only sprint; see the per-strategy execution loops + the
        # _shadow_log_blocked_admit shadow of would-be entries below).
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
        cycle_timings_ms["strategy_scan_total_ms"] = int(
            (time.perf_counter() - scan_started) * 1000
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
        cycle_timings_ms["strategy_scan_by_name_ms"] = dict(strategy_scan_timings_ms)
        if strategy_errors:
            logging.warning(
                "[TRADING] Strategy scan task errors encountered: %s",
                {name: type(err).__name__ for name, err in strategy_errors.items()},
            )

        try:
            btc_signals = strategy_signals.get("bitcoin", [])
            if isinstance(btc_signals, Exception):
                raise btc_signals
            _now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
            _now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
            if _calibration_strategy_allowed(self.config, "sol_macro"):
                for signal in sol_signals:
                    await self._execute_sol_macro_signal(signal)
            else:
                for signal in sol_signals:
                    self._shadow_log_blocked_admit("sol_macro", signal)
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
                _now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
                if _calibration_strategy_allowed(self.config, "eth_macro"):
                    for signal in eth_signals:
                        await self._execute_sol_macro_signal(signal)
                else:
                    for signal in eth_signals:
                        self._shadow_log_blocked_admit("eth_macro", signal)
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
                _now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
                if _calibration_strategy_allowed(self.config, "hype_macro"):
                    for signal in hype_signals:
                        await self._execute_sol_macro_signal(signal)
                else:
                    for signal in hype_signals:
                        self._shadow_log_blocked_admit("hype_macro", signal)
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
                _now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
                if _calibration_strategy_allowed(self.config, "xrp_macro"):
                    for signal in xrp_signals:
                        await self._execute_xrp_macro_signal(signal)
                else:
                    for signal in xrp_signals:
                        self._shadow_log_blocked_admit("xrp_macro", signal)
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
                _now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
                if _calibration_strategy_allowed(self.config, "doge_macro"):
                    for signal in doge_signals:
                        await self._execute_sol_macro_signal(signal)
                else:
                    for signal in doge_signals:
                        self._shadow_log_blocked_admit("doge_macro", signal)
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
                _now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
                if _calibration_strategy_allowed(self.config, "bnb_macro"):
                    for signal in bnb_signals:
                        await self._execute_sol_macro_signal(signal)
                else:
                    for signal in bnb_signals:
                        self._shadow_log_blocked_admit("bnb_macro", signal)
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
            extra={"cycle_timings_ms": cycle_timings_ms},
        )
        try:
            resolution_started = time.perf_counter()
            async with self._execution_lock:
                await self._run_resolution_check(label="[TRADING]")
            cycle_timings_ms["resolution_check_ms"] = int(
                (time.perf_counter() - resolution_started) * 1000
            )
        except Exception as e:
            cycle_timings_ms["resolution_check_error"] = type(e).__name__
            logging.error(f"Resolution tracking error: {e}")

        calibration_started = time.perf_counter()
        self._schedule_ghost_calibration_refresh()
        cycle_timings_ms["calibration_schedule_ms"] = int(
            (time.perf_counter() - calibration_started) * 1000
        )

        positions = len(self.risk_manager.active_positions)
        daily = self.risk_manager.daily_trades
        trade_limit = self.risk_manager.effective_max_trades_per_day()
        cycle_timings_ms["cycle_elapsed_ms"] = int(
            (time.perf_counter() - cycle_wall_start) * 1000
        )
        cycle_timings_ms["cycle_interval_ms"] = int(float(self.scan_interval) * 1000)
        cycle_timings_ms["cycle_overrun_ms"] = max(
            0,
            cycle_timings_ms["cycle_elapsed_ms"] - cycle_timings_ms["cycle_interval_ms"],
        )
        logging.info(
            f"Cycle complete. Positions: {positions}, Daily trades: {daily}/{trade_limit}"
        )
        try:
            _wps = getattr(self.market_scanner, "_ws_price_stats", None)
            if _wps:
                _h = int(_wps.get("ws_hit", 0))
                _r = int(_wps.get("rest", 0))
                _tot = _h + _r
                if _tot > 0:
                    logging.info(
                        "FEED_PRICE_SRC ws_hit=%d rest=%d ws_cov=%.3f "
                        "(cumulative; fraction of priced candidate tokens from "
                        "fresh WS vs REST fallback)",
                        _h, _r, _h / _tot,
                    )
        except Exception:
            pass
        # Return scan-cycle churn arenas to the OS EVERY cycle (cheap — a single
        # malloc_zone_pressure_relief, sub-ms). 2026-07-20: measured this session
        # ratcheting 230MB -> 840MB in ~15min/9 cycles (prior session hit 1676MB)
        # BECAUSE relief ran only every 5 cycles and could not keep up with the
        # per-market json/DataFrame allocation churn. Every-cycle relief reclaims the
        # arenas right after the scan that created them, before they compound. It is a
        # MITIGATION, not a cure: to eliminate the churn at its source, run with
        # PSB_MEM_PROFILE=1 (arms tracemalloc + native gc census -> mem_profile.jsonl)
        # to name the exact allocation site, then reuse buffers there.
        # Instrumented (Codex condition): log reclaimed MB + call ms for the first 12
        # cycles and every 20th after, so we can confirm it lowers the plateau without
        # adding scan latency; if relief_ms is ever non-trivial, revert to adaptive gating.
        # 2026-07-31 (Codex root-cause): the relief above reclaimed 0.0MB EVERY cycle because
        # this cycle's heavy scan locals (Market lists, pandas TA frames, signal lists) are all
        # DEAD by here (last refs <=5657) yet stay BOUND to function scope until return — so
        # malloc_zone_pressure_relief found no idle pages and RSS pinned near the per-cycle
        # pandas/native peak (macOS never trims). Drop the refs FIRST so the freed pages become
        # reclaimable. Rebind-to-None (never raises, unlike del on a maybe-unbound name); all
        # confirmed unused after this point. This is the cure for the RSS ratchet, not the
        # deeper per-scan pandas churn (that needs TA caching / no ws-candle .copy()).
        strategy_markets = scan_results = strategy_signals = strategy_tasks = shared_btc_ta = None  # noqa: F841
        _rel_rss0 = _self_rss_mb()
        _rel_t0 = time.monotonic()
        _release_memory_to_os()
        _rel_ms = (time.monotonic() - _rel_t0) * 1000.0
        _rel_cyc = int(getattr(self, "cycle_count", 0) or 0)
        if _rel_cyc < 12 or _rel_cyc % 20 == 0:
            _rel_rss1 = _self_rss_mb()
            if _rel_rss0 is not None and _rel_rss1 is not None:
                logging.info(
                    "[mem-relief] cycle=%d rss %.0f->%.0fMB (reclaimed %.1fMB) in %.2fms",
                    _rel_cyc, _rel_rss0, _rel_rss1, _rel_rss0 - _rel_rss1, _rel_ms,
                )
        _write_runtime_status(
            phase="cycle_complete",
            session_id=getattr(self.journal, "session_id", None),
            clean_shutdown=False,
            extra={
                "open_positions": positions,
                "daily_trades": daily,
                "cycle_timings_ms": cycle_timings_ms,
            },
        )
        self._append_scan_diagnostics_annotation(
            scan_skip_digest=_scan_skip_digest(self.last_ai_scan_stats),
            side_selection=_side_selection_digest(self.last_ai_scan_stats),
        )
        log_ops_pulse(self, "main")

        # 2026-07-27 ADAPTIVE SCANNER (Codex-scoped): feed this cycle's per-strategy
        # signal counts to the scanner so the NEXT cycle's slug fetch orders deeper
        # lookahead by EMA productivity (producers first -> the inner-fetch timeout
        # cuts unproductive deep-lookahead tails, never a producer's near window).
        # Guarded no-op unless trading.scanner_adaptive_slug_order is enabled.
        try:
            if hasattr(self.market_scanner, "update_asset_productivity"):
                self.market_scanner.update_asset_productivity(
                    dict(self.last_signal_counts or {})
                )
        except Exception:
            pass

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
        return DecisionSnapshot(
            strategy="",
            market_id="",
            market_question="",
            signal_reason=signal_reason,
            lane_meta=lane_meta,
        ).skip_extra(
            skip_reason=skip_reason,
            dry_run=dry_run,
            matched_rule=matched_rule,
        )

    def _log_decision_skip(
        self,
        decision: DecisionSnapshot,
        reason: str,
        *,
        log_reason: Optional[str] = None,
    ) -> None:
        self.journal.log_skip(
            decision.market_id,
            decision.market_question,
            decision.strategy,
            log_reason or reason,
            self.bankroll,
            extra=decision.skip_extra(skip_reason=reason),
        )

    @staticmethod
    def _resolve_execution_intent(signal: Any, *, strategy: str) -> Optional[tuple[str, str]]:
        if signal.action == "BUY_YES":
            return signal.token_id_yes, "BUY"
        if signal.action == "BUY_NO":
            return signal.token_id_no, "BUY"
        if signal.action == "SELL_YES":
            return signal.token_id_yes, "SELL"
        logging.error(
            "%s skip: unexpected action %r (expected BUY_YES, BUY_NO, or SELL_YES)",
            strategy,
            signal.action,
        )
        return None

    async def _fresh_book_slippage_ok(
        self,
        *,
        token_id: str,
        side: str,
        intended_price: float,
        requested_size: Optional[float] = None,
        strategy: str,
        decision: Any,
        market_question: str,
        signal_edge: Optional[float] = None,
    ) -> bool:
        """Live-only pre-order slippage guard.

        Between scan and place_order the CLOB book can move. We re-read the live
        book for the token we are about to BUY and compare best_ask to the price
        we are about to send (signal.price). If the ask has moved more than
        ``max_slippage_cents`` above our price, or the spread/depth are too weak
        for the configured smoke-test constraints, skip rather than place a stale
        or thin-book order.

        Returns True to proceed, False to skip. No-op while dry_run (paper fills
        are priced by fiat — never suppress calibration trades) and for SELL legs.
        Fail-OPEN: any missing book / read error proceeds, since we already passed
        the can_sell_token bid check and a transient REST hiccup must not starve
        live entries. Default mode is ``observe`` (log only); ``enforce`` skips.
        """
        cfg = (self.config.get("trading", {}) or {}).get("slippage_guard", {}) or {}
        self._last_fresh_entry_ask = None
        self._last_fresh_entry_token_id = None
        if not cfg.get("enabled", True):
            return True
        # 2026-07-30 PAPER CALIB Phase 2.4 (operator): the guard was dry_run-bypassed so
        # paper never felt live's fillability discipline (overstating frequency + PnL).
        # When slippage_guard.apply_in_paper is set, run the SAME guard in paper; the
        # effective mode becomes paper_mode (default = mirror live `mode`). Live behavior
        # is unchanged when dry_run is False. _paper_guard drives the empty-book skip below.
        _dry = bool(self.config.get("trading", {}).get("dry_run", True))
        _paper_guard = _dry and bool(cfg.get("apply_in_paper", False))
        if _dry and not _paper_guard:
            return True
        if side != "BUY":
            return True
        try:
            tol = float(cfg.get("max_slippage_cents", 0.02))
            # 2026-07-29 STALE-SNAPSHOT FIX: the signal's edge was computed on the
            # per-cycle scan snapshot (market.yes_price), which is ~15-20s old by the
            # time this lane emits+executes (sequential lane scan). Rather than blocking
            # on the raw ask-vs-stale-price gap (which counts half-spread + benign drift
            # as "slippage" and starved live entries), re-validate the EDGE at the FRESH
            # book here: residual_edge = signal.edge - slip (slip = fresh_ask - intended).
            # Buying `slip` higher erodes edge 1:1, so this is the true edge at the price
            # we'd actually pay. Block only when the move ate the edge below the floor
            # (real adverse move / pump) — edge-intact stale signals proceed. Side-
            # agnostic: signal.edge already encodes YES/NO. Fail-open if edge unknown.
            edge_floor = float(cfg.get("min_residual_edge", 0.0))
            mode = str(cfg.get("mode", "observe")).strip().lower()
            if _paper_guard:
                # In paper, honor paper_mode (default: mirror live `mode`) so paper
                # fill-rate reflects live fillability.
                mode = str(cfg.get("paper_mode", mode)).strip().lower()
            max_spread = float(cfg.get("max_spread_cents", 0.03))
            require_full_depth = bool(cfg.get("require_full_depth", True))
            depth_ceiling = float(cfg.get("depth_price_ceiling_cents", 0.0))
            book = await self.clob_client.fetch_order_book_snapshot(token_id)
            bids = (book or {}).get("bids") or []
            asks = (book or {}).get("asks") or []
            bid_levels = sorted(
                (
                    (float(b["price"]), float(b.get("size", 0)))
                    for b in bids
                    if float(b.get("price", 0)) > 0 and float(b.get("size", 0)) > 0
                ),
                key=lambda lvl: lvl[0],
                reverse=True,
            )
            ask_levels = sorted(
                (
                    (float(a["price"]), float(a.get("size", 0)))
                    for a in asks
                    if float(a.get("price", 0)) > 0 and float(a.get("size", 0)) > 0
                ),
                key=lambda lvl: lvl[0],
            )
            best_ask = min(
                (float(a["price"]) for a in asks if float(a.get("price", 0)) > 0),
                default=None,
            )
            if best_ask is None:
                # Fail-OPEN on empty asks in BOTH live and paper. In live a transient REST
                # hiccup must not starve a real entry. In paper (Codex 2026-07-30): skipping
                # here would make paper frequency reflect public-book READ QUALITY, not true
                # venue fillability — the confound we're trying to remove. Genuine no-liquidity
                # is already caught downstream by the paper fresh-fill layer (clob_client:1435:
                # empty ask book / sub-$1 notional -> no-fill, logged), so the guard defers the
                # empty_book case to it and only enforces the read-robust spread/edge signals.
                logging.warning(
                    "%s slippage-guard: no live asks for token=%s — proceeding (fail-open)",
                    strategy,
                    token_id[:20],
                )
                return True
            self._last_fresh_entry_ask = float(best_ask)
            self._last_fresh_entry_token_id = str(token_id)
            best_bid = bid_levels[0][0] if bid_levels else None
            slip = best_ask - float(intended_price)
            spread = None if best_bid is None else best_ask - best_bid
            depth_limit = float(intended_price) + depth_ceiling
            depth_at_limit = sum(size for price, size in ask_levels if price <= depth_limit)

            block_reason = None
            block_detail = ""
            if signal_edge is not None:
                # 2026-07-30 FEE-GATE FRESH-BOOK FIX (Codex HIGH). The fee-aware SCAN gate
                # (sol/eth) admits on NET-of-fee edge, but this fresh-book re-check used RAW
                # edge - slip, so an adverse ask move between scan and entry could let a
                # fee-negative trade through (raw-slip>0 while raw-slip-fee<0). Re-subtract the
                # taker-fee hurdle at the FRESH ask for the fee-aware lanes (all except btc,
                # which is intentionally fee-deferred). fee_aware_edge_hurdle is venue/config
                # gated → returns 0.0 on olympus or when fee_aware_edge is disabled, so this is
                # a pure no-op unless the fee gate is on.
                _fee_hurdle = 0.0
                if str(strategy or "").lower() != "bitcoin":
                    try:
                        from src.strategies.fee_util import fee_aware_edge_hurdle
                        _fee_hurdle = float(fee_aware_edge_hurdle(self.config, best_ask) or 0.0)
                    except Exception:
                        _fee_hurdle = 0.0
                residual_edge = float(signal_edge) - slip - _fee_hurdle
                if residual_edge < edge_floor:
                    block_reason = "buy_edge_eroded"
                    block_detail = (
                        f"slip={slip:+.4f} edge={float(signal_edge):.4f} fee={_fee_hurdle:.4f} "
                        f"residual_edge={residual_edge:.4f} floor={edge_floor:.4f}"
                    )
            elif slip > tol:
                # Fallback (signal.edge unavailable): legacy absolute-slippage cap.
                block_reason = "buy_slippage_block"
                block_detail = f"slip={slip:+.4f} tol={tol:.4f}"
            if block_reason is None and spread is not None and spread > max_spread:
                block_reason = "buy_spread_block"
                block_detail = f"spread={spread:.4f} max_spread={max_spread:.4f}"
            if block_reason is None and (
                require_full_depth
                and requested_size is not None
                and float(requested_size) > 0
                and depth_at_limit + 1e-9 < float(requested_size)
            ):
                block_reason = "buy_depth_block"
                block_detail = (
                    f"depth_at_limit={depth_at_limit:.4f} requested={float(requested_size):.4f} "
                    f"limit={depth_limit:.4f}"
                )

            if block_reason is None:
                return True

            best_bid_msg = f"{best_bid:.4f}" if best_bid is not None else "NA"
            msg = f"{strategy} slippage-guard {mode}: best_bid={best_bid_msg}"
            msg += (
                f" best_ask={best_ask:.4f} intended={float(intended_price):.4f} "
                f"{block_detail} '{market_question[:40]}'"
            )
            if mode == "enforce":
                logging.warning("%s — SKIP", msg)
                self._log_decision_skip(decision, block_reason)
                return False
            logging.info("%s — observe (proceeding)", msg)
            return True
        except Exception as e:
            logging.warning(
                "%s slippage-guard error (%s) — proceeding (fail-open)", strategy, e
            )
            return True

    def _check_lane_execution(
        self,
        *,
        strategy: str,
        signal_reason: str,
        lane_meta: Dict[str, Any],
        market_id: str,
        market_question: str,
    ) -> bool:
        # 2026-07-25 PER-LANE KILL SWITCH (operator GO): FINAL-action gate at the shared
        # execution choke — both the bitcoin impl and every alt impl route through here,
        # and lane_meta carries the post-all-flips lane_window/lane_side, so (unlike the
        # earlier in-loop placement, which Codex caught as fail-open on flipped lanes)
        # this can't attribute to the wrong lane. Skip placing the order if this exact
        # lane (window|side) is loss-paused. Per-lane not per-asset. Live-only (inert in
        # paper unless exposure.loss_kill_apply_in_paper).
        _km = self._get_exposure_manager_for(strategy)
        if _km is not None:
            try:
                _lk_paused, _lk_reason = _km.lane_paused(
                    lane_meta.get("lane_window"), lane_meta.get("lane_side")
                )
            except Exception:
                # FAIL CLOSED: this switch is the smoke's risk net (chosen over size
                # reduction), so if we cannot determine the lane's pause state, do NOT
                # place the entry. Loud log so a real bug surfaces immediately.
                logging.exception(
                    "[LANE-KILL] lane_paused() errored for %s %s|%s — failing CLOSED (blocking entry)",
                    strategy, lane_meta.get("lane_window"), lane_meta.get("lane_side"),
                )
                return False
            if _lk_paused:
                logging.info(
                    "[LANE-KILL] %s %s|%s paused — %s (skipping entry)",
                    strategy, lane_meta.get("lane_window"),
                    lane_meta.get("lane_side"), _lk_reason,
                )
                return False
        lane_id = str(lane_meta.get("lane_id") or "").strip()
        dry_run = bool(self.config.get("trading", {}).get("dry_run", True))
        allowed, reason, state, matched_key = self.lane_manager.can_execute(lane_id, dry_run=dry_run)
        lane_meta["lane_config_state"] = state
        lane_meta["lane_state_matched_key"] = matched_key
        lane_meta["lane_enforcement_enabled"] = bool(
            getattr(self.lane_manager, "execution_enforcement_enabled", False)
        )
        # In advisory mode, config.default_state="paper" should not make live fills look
        # paper-scoped. Preserve the config state separately and label actual live
        # execution as live.
        if not dry_run and not lane_meta["lane_enforcement_enabled"]:
            lane_meta["promotion_state"] = "live"
        else:
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

    def _mark_exposure_resume_window_green(self, strategy: str) -> None:
        """Keep pause/resume state permissive now that regime gates are purged."""
        em = self._get_exposure_manager_for(strategy)
        if em is not None and hasattr(em, "update_resume_window"):
            em.update_resume_window(green_window=True)

    def _check_regime_fade(
        self,
        *,
        strategy: str,
        signal: Any,
        lane_meta: Dict[str, Any],
    ) -> bool:
        """Regime fade filter — sit out the mis-ranked mid-confidence band
        ([band_low, band_high)) when its rolling realized win rate has collapsed
        (momentum edge inverting in chop); protects the genuine >=band_high
        winners. See ``src/analysis/regime_fade.py``. Returns True to proceed,
        False to suppress (ghost-logged). Fail-open by construction (a missing
        config block / read error yields an inactive state)."""
        try:
            _fade_lane = regime_fade.lane_key(
                strategy,
                getattr(signal, "window_size", None),
                getattr(signal, "action", None),
            )
            state = regime_fade.evaluate(self.config, lane=_fade_lane)
        except Exception as exc:  # never let the filter starve entries
            logging.debug("regime_fade evaluate raised (proceeding): %s", exc)
            return True
        if not state.active:
            return True

        p_win = regime_fade.predicted_p_win(
            getattr(signal, "action", None), getattr(signal, "est_prob", None)
        )
        suppress, reason = regime_fade.should_suppress(
            state, p_win, self.config, edge=getattr(signal, "edge", None)
        )
        if not suppress:
            return True

        extra = self._lane_skip_extra(
            lane_meta=lane_meta,
            signal_reason=getattr(signal, "reason", None),
            skip_reason=reason,
        )
        extra.update(
            {
                "regime_fade_active": True,
                "regime_fade_rolling_wr": state.rolling_wr,
                "regime_fade_n_band": state.n_band,
                "regime_fade_n_window": state.n_window,
                "regime_fade_band_low": state.band_low,
                "regime_fade_band_high": state.band_high,
                "regime_fade_pred_p_win": (round(p_win, 4) if p_win is not None else None),
                "regime_fade_action": state.action,
            }
        )
        self.journal.log_skip(
            getattr(signal, "market_id", ""),
            getattr(signal, "market_question", ""),
            strategy,
            "regime_fade_mid_band_chop",
            self.bankroll,
            extra=extra,
        )
        logging.info(
            "%s regime-fade suppressed: market=%s p_win=%s band_wr=%.3f n_band=%d band=[%.2f,%.2f) reason=%s",
            strategy,
            getattr(signal, "market_id", ""),
            (f"{p_win:.3f}" if p_win is not None else "na"),
            (state.rolling_wr if state.rolling_wr is not None else float("nan")),
            state.n_band,
            state.band_low,
            state.band_high,
            reason,
        )
        return False

    def _check_fresh_entry_window(
        self,
        *,
        strategy: str,
        signal: Any,
        lane_meta: Dict[str, Any],
    ) -> bool:
        """Reject signals that became too stale while slow scans/AI finished."""
        end_date = getattr(signal, "end_date", None)
        if end_date is None:
            return True
        try:
            end_dt = end_date
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            seconds_left = (end_dt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            return True

        window = str(getattr(signal, "window_size", "") or lane_meta.get("lane_window") or "")
        policy = getattr(signal, "entry_policy", None) or {}
        configured_min = None
        try:
            configured_min = float(policy.get("entry_window_min"))
        except (TypeError, ValueError, AttributeError):
            configured_min = None
        guard_cfg = ((self.config.get("trading") or {}).get("entry_execution_guard") or {})
        default_min_by_window = {"5m": 1.0, "15m": 2.0, "1h": 3.0}
        configured_by_window = guard_cfg.get("min_mins_left") or {}
        try:
            guard_min = float(configured_by_window.get(window, default_min_by_window.get(window, 1.0)))
        except (TypeError, ValueError, AttributeError):
            guard_min = default_min_by_window.get(window, 1.0)
        min_mins_left = max(configured_min or 0.0, guard_min)
        min_seconds_left = max(0.0, min_mins_left * 60.0)
        if seconds_left >= min_seconds_left:
            return True

        reason = "stale_signal_window"
        extra = self._lane_skip_extra(
            lane_meta=lane_meta,
            signal_reason=getattr(signal, "reason", None),
            skip_reason=reason,
        )
        extra.update(
            {
                "window_size": window,
                "seconds_left_at_execution": round(seconds_left, 3),
                "min_seconds_left_at_execution": round(min_seconds_left, 3),
            }
        )
        self.journal.log_skip(
            signal.market_id,
            signal.market_question,
            strategy,
            reason,
            self.bankroll,
            extra=extra,
        )
        logging.warning(
            "%s stale signal blocked: market=%s window=%s seconds_left=%.1f min=%.1f",
            strategy,
            signal.market_id,
            window,
            seconds_left,
            min_seconds_left,
        )
        return False

    async def _execute_bitcoin_signal(self, signal: BitcoinSignal):
        """Execute a Bitcoin Up/Down trade signal."""
        async with self._execution_lock:
            await self._execute_bitcoin_signal_impl(signal)

    async def _execute_bitcoin_signal_impl(self, signal: BitcoinSignal):
        """Bitcoin entry (holds _execution_lock via caller)."""
        if self._lane_breaker_blocks("bitcoin", signal.window_size, signal.action):
            return
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
        self._mark_exposure_resume_window_green("bitcoin")
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
        if not self._check_fresh_entry_window(
            strategy="bitcoin",
            signal=signal,
            lane_meta=lane_meta,
        ):
            return
        if not self._check_regime_fade(
            strategy="bitcoin",
            signal=signal,
            lane_meta=lane_meta,
        ):
            return
        decision = DecisionSnapshot.from_signal(
            strategy="bitcoin",
            signal=signal,
            entry_leg=_entry_leg,
            lane_meta=lane_meta,
        )
        can_trade, reason = self.risk_manager.can_trade(strategy="bitcoin")
        if not can_trade:
            logging.warning(f"Bitcoin trade risk check failed: {reason}")
            self._log_decision_skip(decision, reason)
            return

        # 2026-08-06 (Codex HIGH): apply per-lane adaptive sizing BEFORE the risk/exposure check so the risk
        # manager sees the TRUE notional. The sizer can UPsize $15->~$37.50 for a proven short; running it
        # AFTER evaluate_entry meant topic/category exposure was validated at the pre-upsize size and a trade
        # approved at $15 could place at $37.50 unchecked. Now evaluate_entry has final say on the real size.
        # 2026-08-07 FAVORITE-LANE SIZING BYPASS (Codex NO-GO fix) — see general path. Favorite
        # stake is verbatim; risk-manager exposure/position cap below still applies.
        if getattr(signal, "side_source", "") == "favorite_lane":
            _req_size = float(signal.size or 0.0)
        else:
            _req_size = self._apply_tape_adapter_size(
                signal.size, "bitcoin", signal.window_size, signal.action
            )
            _req_size = self._apply_adaptive_realized_size(
                _req_size, "bitcoin", signal.window_size, signal.action
            )
        # Term-based risk check across the active crypto-only bot.
        can_trade, final_size, reason = self.risk_manager.evaluate_entry(
            end_date=signal.end_date,
            current_edge=signal.edge,
            bankroll=self.bankroll,
            strategy="bitcoin",
            requested_size=_req_size,
            market_id=signal.market_id,
            action=signal.action,
            direction=signal.direction,
        )
        if not can_trade:
            logging.warning(f"Bitcoin trade risk evaluation failed: {reason}")
            skip_reason = (
                "topic_exposure_limit"
                if str(reason).startswith("topic_exposure_limit")
                else f"term_risk: {reason}"
            )
            self._log_decision_skip(decision, skip_reason)
            return

        intent = self._resolve_execution_intent(signal, strategy="bitcoin")
        if intent is None:
            return
        token_id, side = intent

        if side == "BUY":
            order_size = final_size / max(0.01, signal.price)
        else:
            order_size = final_size / max(0.01, 1.0 - signal.price)
        pos_size = order_size

        # ── T1-1: Unsellable token guard ─────────────────────────────────────
        # Before placing any order, verify the position can be exited.
        # BUY_YES / BUY_NO: test the token we are acquiring; SELL_YES tests YES.
        token_to_test = token_id
        if not await self.clob_client.can_sell_token(token_to_test, signal.market_id):
            logging.warning(
                f"Bitcoin unsellable-token skip '{signal.market_question[:40]}' "
                f"— token={token_to_test[:20]} has no bids"
            )
            self._log_decision_skip(decision, "unsellable_token")
            return

        # ── Pre-order fresh-book slippage guard (live-only; observe by default) ──
        if not await self._fresh_book_slippage_ok(
            token_id=token_id,
            side=side,
            intended_price=signal.price,
            requested_size=order_size,
            strategy="bitcoin",
            decision=decision,
            market_question=signal.market_question,
            signal_edge=getattr(signal, "edge", None),
        ):
            return

        if self._warmup_blocks_entry():
            self._log_decision_skip(decision, "warmup_feed_not_synced")
            return
        entry_params = self._entry_exec_params()
        entry_price = float(signal.price)
        dry_run = self.config.get("trading", {}).get("dry_run", True)
        entry_mode = str(entry_params.get("entry_mode") or "marketable").lower()
        try:
            hybrid_windows = {str(x).lower() for x in (entry_params.get("hybrid_windows") or ())}
        except TypeError:
            hybrid_windows = {"15m", "1h"}
        window = str(getattr(signal, "window_size", "") or "").lower()
        marketable_entry = entry_mode == "marketable" or (
            entry_mode == "hybrid" and window not in hybrid_windows
        )
        if (
            not dry_run
            and side == "BUY"
            and marketable_entry
            and self._last_fresh_entry_token_id == token_id
            and self._last_fresh_entry_ask is not None
        ):
            entry_price = float(self._last_fresh_entry_ask)
            order_size = final_size / max(0.01, entry_price)
            pos_size = order_size
        logging.info(
            f"Executing BITCOIN trade: {signal.action} {final_size:.2f} @ {entry_price} ({signal.direction})"
        )
        order = await self.clob_client.place_entry_order(
            token_id=token_id,
            side=side,
            price=entry_price,
            size=order_size,
            window=getattr(signal, "window_size", None),
            market_id=signal.market_id,
            dry_run=dry_run,
            order_outcome=("YES" if signal.action == "BUY_YES" else "NO"),
            market_title=signal.market_question,
            market_slug=getattr(signal, "market_slug", None),
            condition_id=getattr(signal, "condition_id", None),
            outcome_label=(
                getattr(signal, "outcome_label_yes", None)
                if signal.action == "BUY_YES"
                else getattr(signal, "outcome_label_no", None)
            ),
            # Entry fill policy (marketable | maker | hybrid). Hybrid = maker-first
            # then cross to taker; 5m falls back to marketable. See _entry_exec_params.
            **entry_params,
        )

        # P0 (Olympus async entry confirmation): Olympus queues trades async
        # (QUEUED -> PROCESSING -> SUCCEEDED/FAILED). If the fill has not reached a
        # terminal SUCCEEDED within the poll window, place_order returns the order
        # still PENDING with filled_size=0. Journaling THAT as an active position
        # creates a phantom (or mis-sized/mis-priced) position if the fill completes
        # later or FAILS. Require a terminal FILLED before journaling on the live
        # Olympus path; the unconfirmed trade stays in clob_client.pending_orders and
        # is reconciled by reconcile_open_positions_with_venue. Paper fills are
        # synchronous, so this guard is a no-op in dry_run.
        if (
            order is not None
            and hasattr(order, "order_id")
            and not self.config.get("trading", {}).get("dry_run", True)
            and self.clob_client.using_olympus()
            and getattr(order, "status", None) != OrderStatus.FILLED
        ):
            _pend_status = getattr(order, "status", None)
            logging.warning(
                "Olympus entry accepted but NOT terminally filled (status=%s) — NOT "
                "journaling as active position (phantom-guard); trade_id=%s size_req=%s. "
                "Left in pending_orders for venue reconciliation.",
                _pend_status.value if isinstance(_pend_status, OrderStatus) else _pend_status,
                order.order_id,
                order_size,
            )
            return

        if order and hasattr(order, "order_id"):
            # Record the ACTUAL filled share count, not the requested size. Olympus
            # (and marketable CLOB) can fill at a better price -> more shares; using
            # the requested size left an unsold residual on every exit (the bot's
            # sell closed fewer shares than it held, orphaning the remainder).
            # order.filled_size is the real position quantity after the fill.
            try:
                _filled_sz = float(getattr(order, "filled_size", 0) or 0)
            except (TypeError, ValueError):
                _filled_sz = 0.0
            if _filled_sz > 0:
                pos_size = _filled_sz
            outcome = "YES" if signal.action == "BUY_YES" else "NO"
            entry_fill_price = float(getattr(order, "price", signal.price) or signal.price)
            order_execution = dict(getattr(order, "execution", {}) or {})
            position = Position(
                position_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                outcome=outcome,
                size=pos_size,
                entry_price=entry_fill_price,
                current_price=entry_fill_price,
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
                entry_signal=decision.entry_signal({
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
                    **order_execution,
                }),
                condition_id=str(getattr(signal, "condition_id", "") or ""),
                market_slug=str(getattr(signal, "market_slug", "") or ""),
            )
            self.risk_manager.add_position(position)
            self._ws_subscribe_held_now(position)
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
                entry_price=entry_fill_price,
                bankroll=self.bankroll,
                edge=signal.edge,
                confidence=signal.confidence,
                reason=_entry_reason,
                token_id_yes=str(getattr(signal, "token_id_yes", "") or ""),
                token_id_no=str(getattr(signal, "token_id_no", "") or ""),
                condition_id=str(getattr(signal, "condition_id", "") or ""),
                market_slug=str(getattr(signal, "market_slug", "") or ""),
                extra={
                    # PAPER CALIB Phase 2.5: entry executability proof (book_walk fill vs
                    # signal mark + book state). None on live/non-fresh-fill entries ->
                    # such rows are lower-confidence for the fillability analyzer.
                    "paper_fill_quality": order_execution.get("paper_fill_quality"),
                    "hour_utc": signal.hour_utc,
                    "window_size": signal.window_size,
                    "ws_price_age_ms": self._ws_price_age_ms(getattr(signal, "token_id_yes", None)),
                    **self._entry_price_provenance(getattr(signal, "token_id_yes", None)),
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
                    **order_execution,
                    **lane_meta,
                },
                market_end_at=signal.end_date,
                entry_leg=_entry_leg,
            )
            self._spawn_bg(self._record_entry_book_async(
                order.order_id,
                getattr(signal, "token_id_yes", None),
                getattr(signal, "token_id_no", None),
                getattr(signal, "action", None),
            ))
            self._capture_entry_tape(
                trade_id=order.order_id, strategy="bitcoin",
                window=getattr(signal, "window_size", None), action=signal.action,
                extra={"edge": getattr(signal, "edge", None),
                       "est_prob": getattr(signal, "est_prob", None),
                       "side_source": getattr(signal, "side_source", None)},
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

    def _shadow_log_blocked_admit(self, strategy_name: str, signal: Any) -> None:
        """Calibration scope: a non-execution strategy produced an admitted signal that
        we are NOT executing this BTC-only sprint. Route it into the ghost / rejected-
        candidate pipeline (reason=calibration_scope_shadow) so the settler scores its
        win/loss and the lane's would-have-entered PICKS accrue as settleable evidence
        for a later promotion decision. Never raises — shadow logging must not break the
        trade cycle. Non-BTC signals share the sol-family shape (market_id/market_slug/
        end_date/est_prob/window_size/action/price)."""
        try:
            from types import SimpleNamespace

            action = str(getattr(signal, "action", "") or "").upper()
            if action not in ("BUY_YES", "BUY_NO"):
                return
            side = "LONG" if action == "BUY_YES" else "SHORT"
            # signal.price is the ORDER price of the TRADED token: yes_price for BUY_YES
            # but (1 - yes_price) for BUY_NO (sol_macro.py:6873). Recover the canonical
            # YES/NO prices so the ghost settler scores BUY_NO realized return correctly
            # (it reads market.no_price for BUY_NO).
            order_price = float(getattr(signal, "price", 0.0) or 0.0)
            if action == "BUY_YES":
                yes_price = order_price
                no_price = (1.0 - order_price) if order_price else 0.0
            else:  # BUY_NO — order_price IS the NO price
                no_price = order_price
                yes_price = (1.0 - order_price) if order_price else 0.0
            _mkt = SimpleNamespace(
                id=getattr(signal, "market_id", None),
                question=getattr(signal, "market_question", None),
                slug=getattr(signal, "market_slug", None),
                end_date=getattr(signal, "end_date", None),
                token_id_yes=getattr(signal, "token_id_yes", None),
                token_id_no=getattr(signal, "token_id_no", None),
                no_price=no_price,
            )
            log_rejected_candidate(
                strategy=strategy_name,
                window=str(getattr(signal, "window_size", "") or ""),
                side=side,
                action=action,
                reason="calibration_scope_shadow",
                market=_mkt,
                yes_price=yes_price,
                est_prob_up=getattr(signal, "est_prob", None),
                raw_est_prob=getattr(signal, "raw_est_prob", None),
                htf_bias=getattr(signal, "htf_bias", None),
                primary_htf_bias=getattr(signal, "primary_htf_bias", None),
                btc_1h_regime=getattr(signal, "btc_1h_regime", None),
                stage="calibration_scope",
                context={
                    "calibration_scope_shadow": True,
                    "blocked_execution": True,
                    "edge": getattr(signal, "edge", None),
                    "size": getattr(signal, "size", None),
                    "confidence": getattr(signal, "confidence", None),
                },
            )
        except Exception as exc:  # noqa: BLE001 — shadow logging must not block the cycle
            logging.debug("calibration shadow-log failed (%s): %s", strategy_name, exc)

    async def _execute_sol_macro_signal(self, signal: SolMacroSignal):
        """Execute a SOL or ETH macro trade signal (same execution path)."""
        async with self._execution_lock:
            await self._execute_sol_macro_signal_impl(signal)

    async def _execute_sol_macro_signal_impl(self, signal: SolMacroSignal):
        """SOL/ETH macro entry (holds _execution_lock via caller)."""
        strat = signal.strategy_name
        if self._lane_breaker_blocks(strat, signal.window_size, signal.action):
            return
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
        self._mark_exposure_resume_window_green(strat)
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
        if not self._check_fresh_entry_window(
            strategy=strat,
            signal=signal,
            lane_meta=lane_meta,
        ):
            return
        if not self._check_regime_fade(
            strategy=strat,
            signal=signal,
            lane_meta=lane_meta,
        ):
            return
        decision = DecisionSnapshot.from_signal(
            strategy=strat,
            signal=signal,
            entry_leg=_entry_leg,
            lane_meta=lane_meta,
        )
        can_trade, reason = self.risk_manager.can_trade(strategy=strat)
        if not can_trade:
            logging.warning(f"{strat} trade risk check failed: {reason}")
            self._log_decision_skip(decision, reason)
            return

        # 2026-08-06 (Codex HIGH): apply per-lane adaptive sizing BEFORE the risk/exposure check so the risk
        # manager sees the TRUE notional. The sizer can UPsize $15->~$37.50 for a proven short; running it
        # AFTER evaluate_entry meant topic/category exposure was validated at the pre-upsize size and a trade
        # approved at $15 could place at $37.50 unchecked. Now evaluate_entry has final say on the real size.
        # 2026-08-07 FAVORITE-LANE SIZING BYPASS (Codex NO-GO fix): the favorite lane's size is a
        # deliberate structural stake — NOT subject to the realized-ROI mult, the direction-lane $
        # ceilings, or the dust floor (which had floored $8->$11). Use signal.size verbatim; the
        # risk manager's exposure/position cap below STILL applies (fan-out protection).
        if getattr(signal, "side_source", "") == "favorite_lane":
            _req_size = float(signal.size or 0.0)
        else:
            _req_size = self._apply_tape_adapter_size(
                signal.size, strat, signal.window_size, signal.action
            )
            _req_size = self._apply_adaptive_realized_size(
                _req_size, strat, signal.window_size, signal.action
            )
        # Term-based risk check across the active crypto-only bot.
        can_trade, final_size, reason = self.risk_manager.evaluate_entry(
            end_date=signal.end_date,
            current_edge=signal.edge,
            bankroll=self.bankroll,
            strategy=strat,
            requested_size=_req_size,
            market_id=signal.market_id,
            action=signal.action,
            direction=signal.direction,
        )
        if not can_trade:
            logging.warning(f"{strat} trade risk evaluation failed: {reason}")
            skip_reason = (
                "topic_exposure_limit"
                if str(reason).startswith("topic_exposure_limit")
                else f"term_risk: {reason}"
            )
            self._log_decision_skip(decision, skip_reason)
            return

        intent = self._resolve_execution_intent(signal, strategy=strat)
        if intent is None:
            return
        token_id, side = intent

        if side == "BUY":
            order_size = final_size / max(0.01, signal.price)
        else:
            order_size = final_size / max(0.01, 1.0 - signal.price)
        pos_size = order_size

        # ── T1-1: Unsellable token guard ─────────────────────────────────────
        # BUY_YES / SELL_YES / BUY_NO — test the token we hold after fill (YES for sell-yes, else buy leg).
        token_to_test = token_id
        if not await self.clob_client.can_sell_token(token_to_test, signal.market_id):
            logging.warning(
                f"{strat} unsellable-token skip '{signal.market_question[:40]}' "
                f"— token={token_to_test[:20]} has no bids"
            )
            self._log_decision_skip(decision, "unsellable_token")
            return

        # ── Pre-order fresh-book slippage guard (live-only; observe by default) ──
        if not await self._fresh_book_slippage_ok(
            token_id=token_id,
            side=side,
            intended_price=signal.price,
            requested_size=order_size,
            strategy=strat,
            decision=decision,
            market_question=signal.market_question,
            signal_edge=getattr(signal, "edge", None),
        ):
            return

        if self._warmup_blocks_entry():
            self._log_decision_skip(decision, "warmup_feed_not_synced")
            return
        # Per-strategy ENTRY-FRESHNESS cap (2026-07-28): the staleness sweet spot is
        # asset-specific — XRP's edge dies in the 20s+ price-age bucket (n21 -$16.6 good
        # sessions) while it's +EV at 3-20s, and ETH/BNB/BTC/SOL still earn out to 45s.
        # So this is PER-ASSET (config trading.entry_freshness_caps.<strategy> in seconds;
        # unset = no cap = global price_max_age governs). Skip when the mark the edge was
        # priced on is older than the lane's cap. Fail-open on missing age.
        _fc = ((self.config.get("trading") or {}).get("entry_freshness_caps") or {})
        _cap_s = _fc.get(strat)
        if _cap_s:
            _age_ms = (self._entry_price_provenance(
                getattr(signal, "token_id_yes", None)) or {}).get("price_asof_age_ms")
            if _age_ms is not None and float(_age_ms) > float(_cap_s) * 1000.0:
                self._log_decision_skip(decision, "entry_price_too_stale")
                return
        entry_params = self._entry_exec_params()
        entry_price = float(signal.price)
        dry_run = self.config.get("trading", {}).get("dry_run", True)
        entry_mode = str(entry_params.get("entry_mode") or "marketable").lower()
        try:
            hybrid_windows = {str(x).lower() for x in (entry_params.get("hybrid_windows") or ())}
        except TypeError:
            hybrid_windows = {"15m", "1h"}
        window = str(getattr(signal, "window_size", "") or "").lower()
        marketable_entry = entry_mode == "marketable" or (
            entry_mode == "hybrid" and window not in hybrid_windows
        )
        if (
            not dry_run
            and side == "BUY"
            and marketable_entry
            and self._last_fresh_entry_token_id == token_id
            and self._last_fresh_entry_ask is not None
        ):
            entry_price = float(self._last_fresh_entry_ask)
            order_size = final_size / max(0.01, entry_price)
            pos_size = order_size
        logging.info(
            f"Executing {strat} trade: {signal.action} {final_size:.2f} @ {entry_price} ({signal.direction})"
        )
        order = await self.clob_client.place_entry_order(
            token_id=token_id,
            side=side,
            price=entry_price,
            size=order_size,
            window=getattr(signal, "window_size", None),
            market_id=signal.market_id,
            dry_run=dry_run,
            order_outcome=("YES" if signal.action == "BUY_YES" else "NO"),
            market_title=signal.market_question,
            market_slug=getattr(signal, "market_slug", None),
            condition_id=getattr(signal, "condition_id", None),
            outcome_label=(
                getattr(signal, "outcome_label_yes", None)
                if signal.action == "BUY_YES"
                else getattr(signal, "outcome_label_no", None)
            ),
            # Entry fill policy (marketable | maker | hybrid). Hybrid = maker-first
            # then cross to taker; 5m falls back to marketable. See _entry_exec_params.
            **entry_params,
        )

        # P0 (Olympus async entry confirmation): Olympus queues trades async
        # (QUEUED -> PROCESSING -> SUCCEEDED/FAILED). If the fill has not reached a
        # terminal SUCCEEDED within the poll window, place_order returns the order
        # still PENDING with filled_size=0. Journaling THAT as an active position
        # creates a phantom (or mis-sized/mis-priced) position if the fill completes
        # later or FAILS. Require a terminal FILLED before journaling on the live
        # Olympus path; the unconfirmed trade stays in clob_client.pending_orders and
        # is reconciled by reconcile_open_positions_with_venue. Paper fills are
        # synchronous, so this guard is a no-op in dry_run.
        if (
            order is not None
            and hasattr(order, "order_id")
            and not self.config.get("trading", {}).get("dry_run", True)
            and self.clob_client.using_olympus()
            and getattr(order, "status", None) != OrderStatus.FILLED
        ):
            _pend_status = getattr(order, "status", None)
            logging.warning(
                "Olympus entry accepted but NOT terminally filled (status=%s) — NOT "
                "journaling as active position (phantom-guard); trade_id=%s size_req=%s. "
                "Left in pending_orders for venue reconciliation.",
                _pend_status.value if isinstance(_pend_status, OrderStatus) else _pend_status,
                order.order_id,
                order_size,
            )
            return

        if order and hasattr(order, "order_id"):
            # Record the ACTUAL filled share count, not the requested size. Olympus
            # (and marketable CLOB) can fill at a better price -> more shares; using
            # the requested size left an unsold residual on every exit (the bot's
            # sell closed fewer shares than it held, orphaning the remainder).
            # order.filled_size is the real position quantity after the fill.
            try:
                _filled_sz = float(getattr(order, "filled_size", 0) or 0)
            except (TypeError, ValueError):
                _filled_sz = 0.0
            if _filled_sz > 0:
                pos_size = _filled_sz
            outcome = "YES" if signal.action == "BUY_YES" else "NO"
            entry_fill_price = float(getattr(order, "price", signal.price) or signal.price)
            order_execution = dict(getattr(order, "execution", {}) or {})
            _entry_reason = f"{_entry_reason} | {signal.reason[:120]}"
            position = Position(
                position_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                outcome=outcome,
                size=pos_size,
                entry_price=entry_fill_price,
                current_price=entry_fill_price,
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
                entry_signal=decision.entry_signal({
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
                    **order_execution,
                }),
                condition_id=str(getattr(signal, "condition_id", "") or ""),
                market_slug=str(getattr(signal, "market_slug", "") or ""),
            )
            self.risk_manager.add_position(position)
            self._ws_subscribe_held_now(position)
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
                entry_price=entry_fill_price,
                bankroll=self.bankroll,
                edge=signal.edge,
                confidence=signal.confidence,
                reason=_entry_reason,
                token_id_yes=str(getattr(signal, "token_id_yes", "") or ""),
                token_id_no=str(getattr(signal, "token_id_no", "") or ""),
                condition_id=str(getattr(signal, "condition_id", "") or ""),
                market_slug=str(getattr(signal, "market_slug", "") or ""),
                extra={
                    # PAPER CALIB Phase 2.5: entry executability proof (book_walk fill vs
                    # signal mark + book state). None on live/non-fresh-fill entries ->
                    # such rows are lower-confidence for the fillability analyzer.
                    "paper_fill_quality": order_execution.get("paper_fill_quality"),
                    "hour_utc": signal.hour_utc,
                    "window_size": signal.window_size,
                    "ws_price_age_ms": self._ws_price_age_ms(getattr(signal, "token_id_yes", None)),
                    **self._entry_price_provenance(getattr(signal, "token_id_yes", None)),
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
                    **order_execution,
                    **lane_meta,
                },
                market_end_at=signal.end_date,
                entry_leg=_entry_leg,
            )
            self._spawn_bg(self._record_entry_book_async(
                order.order_id,
                getattr(signal, "token_id_yes", None),
                getattr(signal, "token_id_no", None),
                getattr(signal, "action", None),
            ))
            self._capture_entry_tape(
                trade_id=order.order_id, strategy=strat,
                window=getattr(signal, "window_size", None), action=signal.action,
                extra={"edge": getattr(signal, "edge", None),
                       "est_prob": getattr(signal, "est_prob", None),
                       "side_source": getattr(signal, "side_source", None)},
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
        _user_ws = getattr(self, "user_ws_client", None)
        if _user_ws is not None:
            try:
                await _user_ws.disconnect()
            except Exception as _e:
                logging.debug("user WS disconnect on shutdown: %r", _e)
        await self.notifier.close()
        if self.ai_broker is not None:
            try:
                await self.ai_broker.stop()
            except Exception:
                logging.exception("ai_broker stop failed")

        # Reap the daily coach child so it does not survive os._exit(0) as an orphan.
        self._terminate_coach_proc()

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
        port = int(os.environ.get("DASHBOARD_PORT") or dashboard_config.get("dashboard_port", 8080))

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
            # HARD CONNECTION CAP: a runaway/stuck browser tab can fire unlimited
            # retries at the loopback dashboard and pile 100s of CLOSE-WAIT sockets,
            # wedging the single-worker loop. limit_concurrency makes uvicorn return
            # 503 + close once N requests are in flight (normal peak ~26: fetchAll
            # batch + SSE), so a flood is rejected instead of freezing the dashboard.
            # timeout_keep_alive trims idle keep-alive sockets faster.
            uvicorn.Config(app, host=host, port=port, log_level="warning",
                           limit_concurrency=64, timeout_keep_alive=5)
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
    _mem_profile_init()
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

    trading_lock = None
    if "--dashboard-only" not in sys.argv:
        trading_lock = _acquire_trading_process_lock(bool(dry_run))

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

    bootstrap_config = _bootstrap_config()
    print_startup_banner(
        config=bootstrap_config,
        dry_run=bool(dry_run if dry_run is not None else True),
        session_id="initializing",
    )

    # Bind HTTP + /health before PolyBot() (journal replay can take minutes on large sessions).
    _dash_holder = _DashboardConfigShim(bootstrap_config)
    if "--no-dashboard" not in sys.argv:
        start_dashboard(_dash_holder)
    else:
        logging.info("--no-dashboard passed; dashboard server will not be started by this process.")

    # Dashboard-only mode is intentionally lightweight for local split-process
    # runs: serve FastAPI from its own process and read journal/runtime data from
    # disk, while the trading bot runs separately with --no-dashboard.
    if "--dashboard-only" in sys.argv:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        shutdown_signal = {"sig": signal.SIGINT}

        def _dashboard_only_signal_handler(sig, frame):
            shutdown_signal["sig"] = sig
            srv = getattr(_dash_holder, "_dashboard_server", None)
            if srv:
                srv.should_exit = True
            loop.call_soon_threadsafe(stop_event.set)

        signal.signal(signal.SIGINT, _dashboard_only_signal_handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _dashboard_only_signal_handler)
        logging.info("Dashboard-only mode — serving dashboard without trading bot initialization.")
        await stop_event.wait()
        print_shutdown_banner(shutdown_signal["sig"])
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    # Now that environment is loaded, we can initialize the bot
    bot = PolyBot()
    _write_runtime_status(
        phase="bot_initialized",
        session_id=getattr(bot.journal, "session_id", None),
        clean_shutdown=False,
    )
    if dry_run is not None:
        bot.config.setdefault("trading", {})["dry_run"] = dry_run
        # 2026-07-27 (Codex live-debug P0): several sub-components CACHE dry_run in their
        # own __init__ — which ran during PolyBot() above, BEFORE this CLI/confirm-live
        # step, when config still held the paper default. The worst is OlympusClient:
        # _enforce_smoke_limits() RAISES "called in dry_run" for any live order while its
        # cached _is_dry_run is True, silently blocking EVERY live entry/exit. Propagate
        # the CONFIRMED dry_run to those cached readers now (this is the correct point —
        # after confirmation, so an unconfirmed --live stays paper).
        try:
            _oc = getattr(getattr(bot, "clob_client", None), "olympus_client", None)
            if _oc is not None and hasattr(_oc, "_is_dry_run"):
                _oc._is_dry_run = bool(dry_run)
            if getattr(bot, "ctf_redeemer", None) is not None:
                bot.ctf_redeemer.dry_run = bool(dry_run)
            for _mattr in (
                "btc_exposure_manager", "sol_exposure_manager", "eth_exposure_manager",
                "hype_exposure_manager", "xrp_exposure_manager", "doge_exposure_manager",
                "bnb_exposure_manager",
            ):
                _m = getattr(bot, _mattr, None)
                if _m is not None and hasattr(_m, "is_paper"):
                    _m.is_paper = bool(dry_run)
            # 2026-07-31 (Codex): PositionExitManager caches _paper_mode (+ the paper-realism
            # exit knobs) from config.trading.dry_run in reload_from_config, which ran during
            # PolyBot() ABOVE with the paper-default config — so a paper-realism exit block would
            # silently NOT fire in paper until the first hot-reload. Refresh it now against the
            # CONFIRMED dry_run so _paper_mode is correct before the first trade.
            if getattr(bot, "exit_manager", None) is not None:
                bot.exit_manager.reload_from_config(bot.config)
            logging.warning(
                "Runtime dry_run=%s propagated to Olympus broker guard + CTF redeemer + "
                "exposure managers + exit manager (fixes cached-paper-in-live order block).",
                dry_run,
            )
        except Exception as _exc:
            logging.error("Failed to propagate runtime dry_run to sub-components: %s", _exc)

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
        "OLYMPUS_API_KEY": os.getenv("OLYMPUS_API_KEY"),
    }
    api_keys = {k: v for k, v in api_keys.items() if v is not None}

    _paper = bot.config.get("trading", {}).get("dry_run", True)
    _provider = str(
        (bot.config.get("trading", {}) or {}).get("execution_provider") or "clob"
    ).lower()
    if not api_keys.get("PRIVATE_KEY") and _provider != "olympus":
        if _paper:
            logging.info(
                "Paper mode: PRIVATE_KEY / POLYMARKET_PRIVATE_KEY not set — OK until you enable live trading."
            )
        else:
            logging.critical(
                "CRITICAL: PRIVATE_KEY or POLYMARKET_PRIVATE_KEY is required when dry_run is false."
            )

    bot.set_api_keys(api_keys=api_keys)

    # Proactively verify/refresh live L2 credentials before any authenticated
    # call. Derived creds expire ~7 days with no rotation and fail silently;
    # catch/heal it at boot rather than discovering it on the first live order
    # (or a silently-failing bankroll fetch below).
    creds_ready = await bot.ensure_live_credentials_ready()
    if not _paper and not creds_ready:
        logging.critical(
            "Live trading requires usable Polymarket L2 credentials and they "
            "could not be refreshed (L1 re-derive failed or unavailable). "
            "Refusing to start trading loop."
        )
        sys.exit(1)

    live_wallet_ok = await bot.refresh_live_wallet_bankroll()
    if not bot.config.get("trading", {}).get("dry_run", True) and not live_wallet_ok:
        logging.critical(
            "Live trading requires a verified Polymarket wallet bankroll. "
            "Refusing to start trading loop with bankroll_source=%s.",
            getattr(bot, "bankroll_source", "unknown"),
        )
        sys.exit(1)

    # Live restart: reconcile resumed journal positions against the venue so we
    # never orphan a real position or chase a phantom one (and the bot's view
    # matches the Olympus account that set the bankroll above).
    await bot.reconcile_open_positions_with_venue()

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
    # Only the in-process dashboard path owns a uvicorn handle. In split mode the
    # bot runs --no-dashboard (the dashboard is a separate psb-dashboard service),
    # so don't try to grab a handle that will never exist — that previously logged a
    # misleading "Uvicorn server handle missing" warning on every bot startup.
    if (
        "--no-dashboard" not in sys.argv
        and bot.config.get("dashboard", {}).get("enabled", False)
    ):
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
            _write_death_marker("shutdown_timeout")
            os._exit(1)

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    shutdown_state = {"signal": None}

    def signal_handler(sig, frame):
        _write_death_marker(f"signal_{int(sig)}")
        if shutdown_state["signal"] is not None:
            # Second Ctrl-C / SIGTERM: the first graceful pass is already running but
            # something is wedged. Give the operator a hard escape hatch instead of the
            # old behavior (log "waiting" and ignore — which left the process unkillable
            # by Ctrl-C when asyncio teardown hung, observed 2026-06-07).
            logging.warning("Received repeated shutdown signal %s; forcing immediate exit.", sig)
            _write_death_marker(f"forced_repeat_signal_{int(sig)}")
            os._exit(1)
        shutdown_state["signal"] = sig
        bot._terminal_shutdown_sig = sig
        # SHUTDOWN WATCHDOG (2026-07-27): arm an independent hard-kill timer on the
        # FIRST stop signal. The graceful path (_graceful_shutdown_or_exit, ~8s) only
        # begins AFTER `await bot.start()` unblocks from the cancel below — but if the
        # main loop is wedged in a blocking sync/executor call, that cancel never
        # lands, the graceful timeout never even arms, and the process ignores a
        # single Ctrl-C until a SECOND signal (this is the "stop command doesn't work"
        # symptom). This daemon Timer fires from its own thread regardless of main-loop
        # state, so ONE stop signal always terminates the process. A normal graceful
        # shutdown finishes well under the deadline and the os._exit(0) at the end of
        # main() tears this thread down before it can fire, so it never kills a healthy
        # shutdown. Deadline sits below the supervisor's 15s child-wait so the child
        # self-terminates before the supervisor escalates to SIGTERM.
        try:
            _wd_deadline = max(12.0, _shutdown_timeout_seconds() + 4.0)

            def _watchdog_force_exit():
                try:
                    _write_death_marker("watchdog_force_exit")
                except Exception:
                    pass
                os._exit(1)

            _wd = threading.Timer(_wd_deadline, _watchdog_force_exit)
            _wd.daemon = True
            _wd.start()
        except Exception:
            pass
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
            # Graceful shutdown is complete and all state is persisted (journal +
            # runtime_status). Hard-exit now instead of returning up through
            # asyncio.run()'s teardown: on Python 3.11 that teardown finalizes async
            # generators + the default executor with NO timeout, and hangs forever when
            # a lingering aiohttp/uvloop stream reader or run_in_executor call won't
            # unblock (observed 2026-06-07: process stuck *after* "shutdown_complete",
            # ignoring Ctrl-C). Mirrors the existing _os._exit(0) fast-path.
            _write_death_marker("graceful_exit")
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
