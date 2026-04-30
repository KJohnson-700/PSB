"""
Dashboard Server
FastAPI server for monitoring bot usage, strategy metrics, live scans, and test results.

Architecture (disk-first):
  Bot writes  → entries.jsonl  (always)
              → positions.json (always)
              → summary.json   (on startup + every 60 s via log_price_update)

  Dashboard reads → summary.json  (fast path, always fresh)
                  → positions.json (fast)
                  → entries.jsonl  (only for trade history, cached by mtime)

  bot_instance is optional and only used for:
    - running=True/False
    - bankroll (live value)
    - real-time signal counts / cycle times
    - BTC/SOL technical analysis objects
    - exposure manager objects
"""

import os
import asyncio
import time as _time_mod
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import yaml
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
import uuid

from src.analysis.usage_tracker import usage_tracker
from src.analysis.btc_price_service import BTCPriceService as _BTCPriceService
from src.config_merge import deep_merge_config as _deep_merge
from src.ai_status import compute_ai_status

bot_instance: Optional["PolyBot"] = None

# Uvicorn server started from main.py before PolyBot finishes heavy init (Railway health checks).
_dashboard_uvicorn_server: Optional["uvicorn.Server"] = None


def take_dashboard_uvicorn_server() -> Optional["uvicorn.Server"]:
    """Return the dashboard Uvicorn server instance if the dashboard thread has started it."""
    return _dashboard_uvicorn_server


def register_dashboard_uvicorn_server(server: "uvicorn.Server") -> None:
    """Called from main.py when the dashboard thread creates the Uvicorn server."""
    global _dashboard_uvicorn_server
    _dashboard_uvicorn_server = server


def _is_full_bot(bot: Any) -> bool:
    """True only after PolyBot has finished init; bootstrap shims are partial."""
    return (
        bot is not None
        and hasattr(bot, "config")
        and hasattr(bot, "risk_manager")
        and hasattr(bot, "journal")
    )


def _full_bot_instance() -> Optional["PolyBot"]:
    return bot_instance if _is_full_bot(bot_instance) else None

# ── AI summary cache (keyed by session_id) ────────────────────────────────────
_ai_summary_cache: Dict[str, str] = {}

# ── Background BTC analysis cache ─────────────────────────────────────────────
# get_full_analysis() takes ~9s (4 Binance fetches). Run it in a background
# thread every 60s and serve the cached result instantly so HTTP never blocks.
_btc_analysis_cache: Optional[object] = None       # last TechnicalAnalysis result
_btc_analysis_ts: float = 0.0                      # unix time of last successful refresh
_btc_analysis_refreshing: bool = False             # prevent concurrent refresh calls
_btc_svc_singleton: Optional[_BTCPriceService] = None


def _get_btc_svc() -> _BTCPriceService:
    global _btc_svc_singleton
    if _btc_svc_singleton is None:
        _btc_svc_singleton = _BTCPriceService()
    return _btc_svc_singleton


def _refresh_btc_cache():
    """Runs in a daemon thread. Fetches full BTC analysis and stores in cache."""
    global _btc_analysis_cache, _btc_analysis_ts, _btc_analysis_refreshing
    if _btc_analysis_refreshing:
        return
    _btc_analysis_refreshing = True
    try:
        # Prefer the bot's own service (already has warm Binance cache)
        bot = _full_bot_instance()
        if bot and hasattr(bot, "bitcoin_strategy"):
            svc = bot.bitcoin_strategy.btc_service
        else:
            svc = _get_btc_svc()
        ta = svc.get_full_analysis()
        if ta:
            _btc_analysis_cache = ta
            import time as _time
            _btc_analysis_ts = _time.time()
    except Exception as e:
        logger.warning(f"BTC cache refresh error: {e}")
    finally:
        _btc_analysis_refreshing = False


def _maybe_trigger_refresh(max_age: float = 55.0):
    """Kick off a background refresh if the cache is stale, without blocking."""
    import time as _time, threading
    if _time.time() - _btc_analysis_ts > max_age and not _btc_analysis_refreshing:
        t = threading.Thread(target=_refresh_btc_cache, daemon=True)
        t.start()


def set_bot_instance(bot: "PolyBot"):
    global bot_instance
    bot_instance = bot
    if not _is_full_bot(bot):
        return
    # Pre-warm the cache when bot starts so first dashboard load is instant
    _maybe_trigger_refresh(max_age=0)
    auto_bts = _maybe_start_auto_backtests("startup")
    if auto_bts:
        session_id = getattr(getattr(bot, "journal", None), "session_id", None)
        for auto_bt in auto_bts:
            status = auto_bt.get("status")
            name = auto_bt.get("name", "unknown")
            if status == "started":
                logger.info(
                    "Dashboard startup auto-backtest: %s started for session=%s job_id=%s pid=%s",
                    name,
                    session_id,
                    auto_bt.get("job_id"),
                    auto_bt.get("pid"),
                )
            elif status == "skipped":
                logger.info(
                    "Dashboard startup auto-backtest: %s skipped for session=%s reason=%s",
                    name,
                    session_id,
                    auto_bt.get("reason"),
                )
            elif status == "error":
                logger.warning(
                    "Dashboard startup auto-backtest: %s error for session=%s reason=%s",
                    name,
                    session_id,
                    auto_bt.get("reason"),
                )


logger = logging.getLogger(__name__)
DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"


def _classify_updown_trade(question: str, strategy: str, market_id: str = "") -> str:
    """Map a closed trade to a stable updown bucket key (e.g. ETH_updown_15m).

    Journal rows may omit the full Polymarket question wording; fall back to
    ``strategy`` and ``market_id`` so ETH/XRP gate WR panels still populate.
    """
    ql = (question or "").lower()
    mid = (market_id or "").lower()
    blob = ql + " " + mid

    if "up or down" in ql:
        times = re.findall(r"(\d+):(\d+)(AM|PM)", question or "")
        window = 15
        if len(times) >= 2:

            def _abs(h, m, p):
                return (int(h) % 12 + (12 if p == "PM" else 0)) * 60 + int(m)

            diff = abs(_abs(*times[1]) - _abs(*times[0]))
            window = diff if diff > 0 else 5
        if re.search(r"\b(xrp|ripple)\b", ql):
            sym = "XRP"
        elif re.search(r"\b(ethereum|ether)\b", ql) or re.search(r"\beth\b", ql):
            sym = "ETH"
        elif "hyperliquid" in ql or re.search(r"\bhype\b", ql) or "hype" in mid:
            sym = "HYPE"
        elif "bitcoin" in ql or re.search(r"\bbtc\b", ql):
            sym = "BTC"
        elif "solana" in ql or re.search(r"\bsol\b", ql):
            sym = "SOL"
        else:
            sym = "UNK"
        sz = "5m" if window <= 5 else "15m"
        return f"{sym}_updown_{sz}"

    if "eth-updown-15m" in mid or "eth_updown_15m" in mid:
        return "ETH_updown_15m"
    if "eth-updown-5m" in mid or "eth-updown-5" in mid:
        return "ETH_updown_5m"
    if "hype-updown-15m" in mid or "hype_updown_15m" in mid:
        return "HYPE_updown_15m"
    if "hype-updown-5m" in mid or "hype-updown-5" in mid:
        return "HYPE_updown_5m"
    if "xrp-updown-15m" in mid or "xrp_updown_15m" in mid:
        return "XRP_updown_15m"
    if "xrp-updown-5m" in mid:
        return "XRP_updown_5m"

    if strategy == "sol_macro":
        sz = (
            "5m"
            if re.search(r"(^|[^0-9])(5m|5-m|updown-5m)([^0-9]|$)", blob)
            else "15m"
        )
        return f"SOL_updown_{sz}"
    if strategy == "eth_macro":
        sz = (
            "5m"
            if re.search(r"(^|[^0-9])(5m|5-m|updown-5m)([^0-9]|$)", blob)
            else "15m"
        )
        return f"ETH_updown_{sz}"
    if strategy == "hype_macro":
        sz = (
            "5m"
            if re.search(r"(^|[^0-9])(5m|5-m|updown-5m)([^0-9]|$)", blob)
            else "15m"
        )
        return f"HYPE_updown_{sz}"
    if strategy == "xrp_macro":
        sz = (
            "5m"
            if re.search(r"(^|[^0-9])(5m|5-m|updown-5m)([^0-9]|$)", blob)
            else "15m"
        )
        return f"XRP_updown_{sz}"

    return strategy

# Mutating routes require X-API-Key on non-loopback clients. Local development can
# omit DASHBOARD_API_KEY, but public deployments must fail closed.
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "")


@asynccontextmanager
async def _dashboard_lifespan(_app: FastAPI):
    """Pre-warm lightweight caches on startup without using deprecated event hooks."""
    _maybe_trigger_refresh(max_age=0)
    yield


app = FastAPI(
    title="PolyBot AI Dashboard",
    description="Live monitoring for PolyBot AI trading bot.",
    version="0.2.0",
    lifespan=_dashboard_lifespan,
)


def _init_sentry() -> None:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        sentry_sdk.init(
            dsn=dsn,
            integrations=[FastApiIntegration()],
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0") or 0),
        )
    except Exception:
        logger.warning("Sentry SDK init failed; continuing without error reporting.", exc_info=True)


_init_sentry()


def _health_payload() -> Dict[str, Any]:
    sha = (
        os.getenv("RAILWAY_GIT_COMMIT_SHA")
        or os.getenv("RAILWAY_GIT_COMMIT")
        or os.getenv("SOURCE_VERSION")
        or ""
    ).strip()
    return {
        "status": "ok",
        "dashboard_ui_rev": "2026-04-28-command-center-trades-today",
        "git_sha": sha or None,
        "railway_deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID") or None,
    }

# ─── JOURNAL CACHE ────────────────────────────────────────────────
# Avoid rebuilding TradeJournal (reads all of entries.jsonl) on every API call.
# Cache is keyed by the entries.jsonl path; only reload when its mtime changes.

_journal_cache: Dict[str, object] = {
    "path": None,   # Path object for entries.jsonl
    "mtime": None,  # last known mtime
    "journal": None,  # cached TradeJournal instance
}


def _get_journal():
    """Return a TradeJournal, rebuilding only when entries.jsonl changes on disk.

    Priority:
      1. bot_instance.journal  (always fresh — bot owns it)
      2. Cached journal if entries.jsonl mtime is unchanged
      3. Rebuild from the most recent session directory on disk
    """
    if bot_instance and hasattr(bot_instance, "journal"):
        return bot_instance.journal

    from src.execution.trade_journal import TradeJournal, JOURNAL_DIR

    if not JOURNAL_DIR.exists():
        return None

    # Match TradeJournal(resume_latest): use the newest dir that actually has
    # journal data, not a newer empty stub folder (fixes blank Journal after restarts
    # when the dashboard process has no in-memory bot_instance).
    chosen = TradeJournal.newest_resumable_session_dir()
    if chosen is None:
        sessions = sorted(
            [d for d in JOURNAL_DIR.iterdir() if d.is_dir()], reverse=True
        )
        if not sessions:
            return None
        chosen = sessions[0]

    entries_file = chosen / "entries.jsonl"

    # Determine current mtime (None if file doesn't exist yet)
    try:
        current_mtime = entries_file.stat().st_mtime if entries_file.exists() else None
    except OSError:
        current_mtime = None

    cached_path = _journal_cache.get("path")
    cached_mtime = _journal_cache.get("mtime")
    cached_journal = _journal_cache.get("journal")

    # Re-use cache if same session directory and file has not been modified
    if (
        cached_journal is not None
        and cached_path == entries_file
        and cached_mtime == current_mtime
    ):
        return cached_journal

    # Cache miss — rebuild
    journal = TradeJournal(session_id=chosen.name)
    _journal_cache["path"] = entries_file
    _journal_cache["mtime"] = current_mtime
    _journal_cache["journal"] = journal
    return journal


def _get_journal_summary() -> Dict:
    """Return session summary aligned with TradeJournal when loadable.

    Prefer ``_get_journal().get_summary()`` (live bot or rebuilt from entries.jsonl)
    so hero stats match closed-trade lists and chart trade-points. Falls back to
    reading ``summary.json`` only when no journal can be loaded. Adds
    ``summary_source``: ``live_journal`` | ``summary_json`` | ``none``.
    """
    from src.execution.trade_journal import JOURNAL_DIR, TradeJournal

    _empty = {
        "session_id": None,
        "total_entries": 0,
        "total_exits": 0,
        "open_positions": 0,
        "total_cost": 0,
        "session_staked_notional": 0.0,
        "realized_pnl": 0,
        "unrealized_pnl": 0,
        "total_pnl": 0,
        "win_rate": 0,
        "wins": 0,
        "losses": 0,
        "strategy_stats": {},
        "summary_source": "none",
    }

    j = _get_journal()
    if j:
        out = j.get_summary()
        out["summary_source"] = "live_journal"
        return out

    if not JOURNAL_DIR.exists():
        return _empty

    chosen = TradeJournal.newest_resumable_session_dir()
    if chosen is None:
        sessions = sorted(
            [d for d in JOURNAL_DIR.iterdir() if d.is_dir()], reverse=True
        )
        if not sessions:
            return _empty
        chosen = sessions[0]

    summary_file = chosen / "summary.json"
    if summary_file.exists():
        try:
            with open(summary_file, encoding="utf-8") as f:
                out = json.load(f)
                out["summary_source"] = "summary_json"
                return out
        except Exception:
            pass

    return _empty


def _command_center_session(js: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact session stats for Command Center (journal is source of truth)."""
    if not js:
        return {}
    return {
        "session_id": js.get("session_id"),
        "fills": int(js.get("total_entries", 0) or 0),
        "closed": int(js.get("total_exits", 0) or 0),
        "open": int(js.get("open_positions", 0) or 0),
        "realized_pnl": js.get("realized_pnl", 0),
        "total_pnl": js.get("total_pnl", 0),
        "open_stake": js.get("total_cost", 0),
        "session_staked_notional": js.get("session_staked_notional", 0),
    }


_KELLY_STRATEGY_KEYS = (
    "bitcoin",
    "sol_macro",
    "eth_macro",
    "hype_macro",
    "xrp_macro",
)


def _kelly_state_payload() -> Dict[str, Any]:
    """Streak + effective Kelly fraction + per-window breakdown — live from bot when connected."""
    ks = getattr(bot_instance, "kelly_sizer", None) if bot_instance else None
    if ks is not None:
        base = {
            k: {
                "streak": ks.get_current_streak(k),
                "fraction": round(ks.get_kelly_fraction(k), 4),
            }
            for k in _KELLY_STRATEGY_KEYS
        }
        window_stats = ks.get_all_window_stats()
        base["_window_stats"] = window_stats
        return base
    cfg: Dict = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            pass
    st = cfg.get("strategies", {}) or {}
    out: Dict[str, Any] = {}
    for k in _KELLY_STRATEGY_KEYS:
        sc = st.get(k) if isinstance(st.get(k), dict) else {}
        base = float(sc.get("kelly_fraction", 0.15))
        out[k] = {"streak": 0, "fraction": round(base, 4)}
    out["_window_stats"] = {
        k: {"5m": {"streak": 0, "wins": 0, "losses": 0, "wr": 0.0, "trades": 0},
            "15m": {"streak": 0, "wins": 0, "losses": 0, "wr": 0.0, "trades": 0}}
        for k in _KELLY_STRATEGY_KEYS
    }
    return out


def _journal_for_query(session_id: Optional[str]):
    """Load ``TradeJournal`` for a specific session (active or archive), or None."""
    if not session_id:
        return None
    from src.execution.trade_journal import TradeJournal, JOURNAL_DIR

    if (JOURNAL_DIR / session_id).is_dir():
        return TradeJournal(session_id=session_id)
    if TradeJournal._find_archive_session_path(session_id):
        return TradeJournal(session_id=session_id)
    return None


# ─── HELPERS ──────────────────────────────────────────────────────


def _is_loopback_client(request: Request) -> bool:
    client_host = (request.client.host if request.client else "") or ""
    return client_host in {"127.0.0.1", "::1", "localhost"}


def _check_auth(request: Request):
    """Require X-API-Key header for mutating endpoints outside local dev."""
    if not DASHBOARD_API_KEY:
        if _is_loopback_client(request):
            return
        raise HTTPException(
            status_code=503,
            detail=(
                "DASHBOARD_API_KEY required for non-loopback access. "
                "Set this env var before deploying the dashboard publicly."
            ),
        )
    api_key = request.headers.get("X-API-Key", "")
    if api_key != DASHBOARD_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")


def _safe_env() -> Dict[str, str]:
    """Environment for dashboard-spawned subprocesses (backtests, scans, optional second main).

    Must pass through normal provider keys (``OPENROUTER_API_KEY``, ``POLYMARKET_API_KEY``,
    etc.). A broad ``API_KEY`` substring denylist was stripping those and breaking hosted
    backtests. Child processes run in the same trust boundary as the bot process.
    """
    env = dict(os.environ)
    env.update({"NO_COLOR": "1", "TERM": "dumb", "PYTHONIOENCODING": "utf-8"})
    return env


def _parse_direction(question: str) -> str:
    q = (question or "").lower()
    up_words = ("above", "over", "exceed", "reach", "hit", "surpass", "higher", "rise", "up")
    dn_words = ("below", "under", "drop", "fall", "crash", "decline", "lower", "down")
    up = sum(1 for w in up_words if w in q)
    dn = sum(1 for w in dn_words if w in q)
    return "UP" if up >= dn else "DOWN"


def _parse_threshold(question: str, asset: str = "btc") -> Optional[float]:
    patterns = [
        re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*([mk])?", re.IGNORECASE),
        re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:dollars|usd)", re.IGNORECASE),
    ]
    for pat in patterns:
        m = pat.search(question or "")
        if not m:
            continue
        try:
            price = float(m.group(1).replace(",", ""))
            suffix = (m.group(2) or "").lower()
            if suffix == "m":
                price *= 1_000_000
            elif suffix == "k":
                price *= 1000
            if asset == "sol" and 1 < price < 10000:
                return price
            if asset == "eth" and 200 < price < 100_000:
                return price
            if asset == "xrp" and 0.05 < price < 500:
                return price
            if asset == "btc" and 1000 < price < 1_000_000_000:
                return price
        except Exception:
            continue
    return None


# ─── MAIN PAGE ────────────────────────────────────────────────────


def _dashboard_html_with_injections() -> str:
    """Read index.html and inject optional head snippets (e.g. browser Sentry) from env."""
    html_path = Path(__file__).parent / "index.html"
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    inject_parts: List[str] = []
    browser_dsn = os.getenv("SENTRY_BROWSER_DSN", "").strip()
    if browser_dsn:
        rep = os.getenv("SENTRY_REPLAY_SESSION_SAMPLE_RATE", "0.05").strip() or "0.05"
        try:
            float(rep)
        except ValueError:
            rep = "0.05"
        dsn_js = json.dumps(browser_dsn)
        inject_parts.append(
            f'<script src="https://browser.sentry-cdn.com/8.47.0/bundle.tracing.replay.min.js" '
            f'crossorigin="anonymous"></script>\n<script>\n'
            f"Sentry.init({{ dsn: {dsn_js}, integrations: ["
            f"Sentry.browserTracingIntegration(), Sentry.replayIntegration()], "
            f"tracesSampleRate: 0, replaysSessionSampleRate: {rep}, "
            f"replaysOnErrorSampleRate: 1.0 }});\n</script>"
        )
    blob = "\n".join(inject_parts)
    marker = "<!-- DASHBOARD_HEAD_INJECT -->\n"
    if marker in content:
        content = content.replace(marker, blob + ("\n" if blob else ""))
    elif blob:
        content = blob + "\n" + content
    return content


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """Serves the main dashboard HTML page (always fresh reload)."""
    try:
        return HTMLResponse(content=_dashboard_html_with_injections())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Dashboard file not found.</h1>")


# ─── HEALTH (Railway / container uptime check) ────────────────────

@app.get("/health")
async def health_check():
    """Keep ``status: ok`` for probes; extra fields help confirm the image matches Git (see docs/RAILWAY.md)."""
    return _health_payload()


@app.get("/api/dashboard/health-snippet", response_class=HTMLResponse)
async def health_snippet():
    """Tiny HTML fragment for HTMX polling (deploy fingerprint); keeps operators on a live UI rev."""
    h = _health_payload()
    rev = h.get("dashboard_ui_rev") or "?"
    return HTMLResponse(
        content=f'<span class="badge badge-green" title="HTMX polled /health">{rev}</span>',
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/ops/summary")
async def get_ops_summary():
    """Same structured snapshot as OPS_JSON log lines (for curl / monitoring without log drain)."""
    from src.ops_pulse import build_ops_snapshot

    if not bot_instance:
        return {
            "error": "bot not running",
            "hint": "Start the bot or use /api/journal/summary for the latest disk session.",
        }
    return build_ops_snapshot(bot_instance, "http")


# ─── SERVER-SENT EVENTS (live status push every 2s) ───────────────

@app.get("/api/events")
async def sse_stream(request: Request):
    """Server-Sent Events stream — pushes live status snapshot every 2s."""
    async def event_generator():
        sse_interval = 2.0
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                sse_interval = float((cfg.get("dashboard") or {}).get("sse_interval_sec", 2.0))
            except Exception:
                sse_interval = 2.0
        while True:
            bot = _full_bot_instance()
            if await request.is_disconnected():
                break
            try:
                # Align SSE hero fields with /api/status (risk_manager), not journal keys.
                # journal.get_summary() has no "total_trades"; PolyBot has no .positions — both
                # defaulted to 0 every 2s and overwrote Command Center trades/positions.
                rm = (
                    getattr(bot, "risk_manager", None) if bot else None
                )
                open_n = (
                    len(rm.active_positions)
                    if rm and getattr(rm, "active_positions", None) is not None
                    else 0
                )
                daily_trades_n = (
                    int(getattr(rm, "daily_trades", 0) or 0) if rm else 0
                )
                daily_pnl_v = (
                    round(float(getattr(rm, "daily_pnl", 0) or 0), 2) if rm else 0.0
                )
                js: Dict[str, Any] = {}
                if bot and getattr(bot, "journal", None):
                    try:
                        js = bot.journal.get_summary()
                    except Exception:
                        js = {}
                open_stake = round(float(js.get("total_cost", 0) or 0), 2)
                kill_switch_active = (DATA_ROOT / "KILL_SWITCH").exists()
                session_open = int(js.get("open_positions", 0) or 0)
                session_id = js.get("session_id")
                if bot and getattr(bot, "journal", None):
                    sid = getattr(bot.journal, "session_id", None)
                    if sid is not None:
                        session_id = sid

                # Bot stopped: align SSE hero with /api/status (disk positions + journal).
                if not bot:
                    try:
                        js = _get_journal_summary()
                    except Exception:
                        js = {}
                    disk_pos = _load_disk_positions_for_status()
                    disk_n = len(disk_pos)
                    if disk_n:
                        open_n = disk_n
                    session_open = int(
                        js.get("open_positions", 0) or 0
                    )
                    if disk_n:
                        session_open = max(session_open, disk_n)
                    open_stake = round(float(js.get("total_cost", 0) or 0), 2)
                    if session_id is None:
                        session_id = js.get("session_id")

                cfg_disk: Dict[str, Any] = {}
                if not bot and CONFIG_PATH.exists():
                    try:
                        with open(CONFIG_PATH, encoding="utf-8") as f:
                            cfg_disk = yaml.safe_load(f) or {}
                    except Exception:
                        cfg_disk = {}

                if bot:
                    dry_run = bot.config.get("trading", {}).get("dry_run", True)
                    can_trade, _reason = bot.risk_manager.can_trade()
                    if kill_switch_active:
                        can_trade = False
                    _ai_keys = getattr(bot.ai_agent, "api_keys", None) or {}
                    ai_payload = compute_ai_status(bot.config, _ai_keys)
                else:
                    dry_run = cfg_disk.get("trading", {}).get("dry_run", True)
                    can_trade = False
                    ai_payload = compute_ai_status(cfg_disk, None)

                bankroll_snap = 0.0
                if bot and hasattr(bot, "bankroll"):
                    bankroll_snap = round(float(bot.bankroll), 2)
                elif not bot:
                    j_disk = _get_journal()
                    session_dir_disk = (
                        j_disk.session_dir if j_disk else _dashboard_journal_session_dir()
                    )
                    bankroll_snap = 0.0
                    if j_disk:
                        try:
                            br = j_disk.last_bankroll_from_entries_log()
                            if br is not None:
                                bankroll_snap = float(br)
                        except (TypeError, ValueError):
                            pass
                    if bankroll_snap <= 0 and session_dir_disk:
                        br_snap = _last_snapshot_bankroll(session_dir_disk)
                        if br_snap is not None:
                            bankroll_snap = round(br_snap, 2)
                    if bankroll_snap <= 0:
                        initial = float(
                            (cfg_disk.get("backtest") or {}).get(
                                "initial_bankroll", 500
                            )
                            or 500
                        )
                        tp = float(js.get("total_pnl", 0) or 0)
                        if js.get("session_id"):
                            bankroll_snap = round(initial + tp, 2)

                snapshot = {
                    "running": bot.running if bot else False,
                    "kill_switch_active": kill_switch_active,
                    "dry_run": dry_run,
                    "can_trade": can_trade,
                    "ai": ai_payload,
                    "session_id": session_id,
                    "session_open": session_open,
                    "bankroll": bankroll_snap,
                    "positions": open_n,
                    "trades_today": daily_trades_n,
                    "daily_pnl": daily_pnl_v,
                    "open_stake": open_stake,
                    "session_fills": int(js.get("total_entries", 0) or 0),
                    "session_closed": int(js.get("total_exits", 0) or 0),
                    "session_staked": round(
                        float(js.get("session_staked_notional", 0) or 0), 2
                    ),
                    "session_realized_pnl": round(
                        float(js.get("realized_pnl", 0) or 0), 2
                    ),
                    "btc_price": round(
                        float(_btc_analysis_cache.current_price), 0
                    ) if _btc_analysis_cache and hasattr(_btc_analysis_cache, "current_price") else 0,
                    "ts": int(_time_mod.time()),
                }
                yield f"data: {json.dumps(snapshot)}\n\n"
            except Exception as e:
                logger.warning("SSE snapshot failed: %s", e, exc_info=True)
                yield f"data: {json.dumps({'ts': int(_time_mod.time()), 'sse_error': str(e)})}\n\n"
            await asyncio.sleep(max(0.5, sse_interval))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


if os.getenv("SCALAR_ENABLED", "").strip().lower() in ("1", "true", "yes"):
    try:
        from scalar_fastapi import get_scalar_api_reference

        @app.get("/scalar", include_in_schema=False)
        async def scalar_reference():
            return get_scalar_api_reference(
                openapi_url=app.openapi_url,
                title=app.title,
            )
    except ImportError:
        logger.warning(
            "SCALAR_ENABLED is set but scalar-fastapi is not installed; /scalar disabled."
        )


# ─── DISK SESSION HELPERS (status + SSE alignment) ───────────────


def _dashboard_journal_session_dir() -> Optional[Path]:
    """Same session folder priority as TradeJournal disk readers (not raw lexicographic only)."""
    from src.execution.trade_journal import TradeJournal, JOURNAL_DIR

    if not JOURNAL_DIR.exists():
        return None
    chosen = TradeJournal.newest_resumable_session_dir()
    if chosen is not None:
        return chosen
    subs = sorted([d for d in JOURNAL_DIR.iterdir() if d.is_dir()], reverse=True)
    return subs[0] if subs else None


def _positions_list_from_positions_json(pos_file: Path) -> List[Dict]:
    if not pos_file.exists():
        return []
    try:
        with open(pos_file, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []
    out: List[Dict] = []
    for pid, p in raw.items():
        out.append(
            {
                "position_id": pid,
                "market_id": p.get("market_id", ""),
                "market_question": (p.get("market_question") or "N/A")[:80],
                "outcome": p.get("outcome", ""),
                "size": p.get("size", 0),
                "entry_price": p.get("entry_price", 0),
                "current_price": p.get("current_price", p.get("entry_price", 0)),
                "pnl": p.get("pnl", 0.0),
                "opened_at": p.get("opened_at", ""),
                "strategy": p.get("strategy", "unknown"),
            }
        )
    return out


def _load_disk_positions_for_status() -> List[Dict]:
    d = _dashboard_journal_session_dir()
    if not d:
        return []
    return _positions_list_from_positions_json(d / "positions.json")


def _last_snapshot_bankroll(session_dir: Optional[Path]) -> Optional[float]:
    """Last ``bankroll`` in ``snapshots.jsonl`` (tail scan)."""
    if not session_dir:
        return None
    snap = session_dir / "snapshots.jsonl"
    if not snap.exists():
        return None
    try:
        with open(snap, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - 65536))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last_br: Optional[float] = None
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        b = o.get("bankroll")
        if b is None:
            continue
        try:
            last_br = float(b)
        except (TypeError, ValueError):
            pass
    return last_br


# ─── STATUS ───────────────────────────────────────────────────────


@app.get("/api/status")
async def get_status():
    """Bot status.

    Journal summary for PnL fields uses the same source as ``/api/journal/summary``
    (TradeJournal when loadable). Positions are read from ``positions.json`` in the
    latest session folder when the bot is not running.

    bot_instance is only consulted for:
      - running flag
      - live bankroll value
      - active in-memory positions (supplements disk positions)
    """
    kill_switch_file = DATA_ROOT / "KILL_SWITCH"
    kill_switch_active = kill_switch_file.exists()
    bot = _full_bot_instance()
    strategy_names = (
        "bitcoin",
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "weather",
    )
    strategy_attrs = {
        "bitcoin": "bitcoin_strategy",
        "sol_macro": "sol_macro_strategy",
        "eth_macro": "eth_macro_strategy",
        "hype_macro": "hype_macro_strategy",
        "xrp_macro": "xrp_macro_strategy",
        "weather": "weather_strategy",
    }

    def _build_strategy_state(cfg: Dict[str, Any], running: bool) -> Dict[str, Dict[str, Any]]:
        strategies_cfg = (cfg or {}).get("strategies", {})
        state: Dict[str, Dict[str, Any]] = {}
        for name in strategy_names:
            cfg_block = strategies_cfg.get(name, {})
            configured_enabled = bool(cfg_block.get("enabled", False))
            row: Dict[str, Any] = {
                "configured_enabled": configured_enabled,
                "running": running,
                "runtime_present": False,
                "runtime_enabled": None,
                "last_cycle_time": None,
                "last_signal_count": None,
                "cumulative_signal_count": None,
            }
            if bot:
                attr = strategy_attrs.get(name)
                strat_obj = getattr(bot, attr, None) if attr else None
                row["runtime_present"] = strat_obj is not None
                if strat_obj is not None:
                    row["runtime_enabled"] = bool(
                        getattr(strat_obj, "enabled", configured_enabled)
                    )
                else:
                    row["runtime_enabled"] = configured_enabled
                row["last_cycle_time"] = (
                    getattr(bot, "last_cycle_times", {}) or {}
                ).get(name)
                row["last_signal_count"] = (
                    getattr(bot, "last_signal_counts", {}) or {}
                ).get(name)
                row["cumulative_signal_count"] = (
                    getattr(bot, "cumulative_signal_counts", {}) or {}
                ).get(name)
            state[name] = row
        return state

    # ── Read positions from disk (same session as resumable journal) ──
    disk_positions: List[Dict] = _load_disk_positions_for_status()

    # ── If full bot is live, prefer its in-memory positions ──
    if bot:
        dry_run = bot.config.get("trading", {}).get("dry_run", True)
        can_trade, can_trade_reason = bot.risk_manager.can_trade()
        if kill_switch_active:
            can_trade = False
            can_trade_reason = "Kill switch active (data/KILL_SWITCH)"

        bankroll = getattr(bot, "bankroll", 0.0)
        portfolio = (
            bot.risk_manager.get_portfolio_summary(bankroll) if bankroll else None
        )

        def serialize_position(p):
            return {
                "position_id": p.position_id,
                "market_id": p.market_id,
                "market_question": (p.market_question or "N/A")[:80],
                "outcome": p.outcome,
                "size": p.size,
                "entry_price": p.entry_price,
                "current_price": getattr(p, "current_price", p.entry_price),
                "pnl": getattr(p, "pnl", 0.0),
                "opened_at": (
                    p.opened_at.isoformat()
                    if hasattr(p.opened_at, "isoformat")
                    else str(p.opened_at)
                ),
                "strategy": getattr(p, "strategy", "unknown"),
            }

        positions = [
            serialize_position(p)
            for p in bot.risk_manager.active_positions.values()
        ]
        _ai_keys = getattr(bot.ai_agent, "api_keys", None) or {}
        try:
            _js = bot.journal.get_summary()
        except Exception:
            _js = {}
        return {
            "running": getattr(bot, "running", False),
            "mode": "paper" if dry_run else "live",
            "dry_run": dry_run,
            "kill_switch_active": kill_switch_active,
            "can_trade": can_trade,
            "can_trade_reason": can_trade_reason,
            "bankroll": bankroll,
            "portfolio": portfolio,
            "positions": positions,
            "session": _command_center_session(_js),
            "ai": compute_ai_status(bot.config, _ai_keys),
            "strategy_state": _build_strategy_state(
                bot.config, bool(getattr(bot, "running", False))
            ),
            "session_id": getattr(bot.journal, "session_id", None),
        }

    # ── No bot_instance: read everything from disk ──
    summary = _get_journal_summary()
    session_cc = _command_center_session(summary)

    # Infer dry_run and AI status from config if available
    dry_run = True
    cfg_disk: Dict = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg_disk = yaml.safe_load(f) or {}
            dry_run = cfg_disk.get("trading", {}).get("dry_run", True)
        except Exception:
            pass

    j_disk = _get_journal()
    session_dir_disk = j_disk.session_dir if j_disk else _dashboard_journal_session_dir()
    bankroll_disk = 0.0
    if j_disk:
        try:
            br = j_disk.last_bankroll_from_entries_log()
            if br is not None:
                bankroll_disk = float(br)
        except (TypeError, ValueError):
            pass
    if bankroll_disk <= 0 and session_dir_disk:
        br_snap = _last_snapshot_bankroll(session_dir_disk)
        if br_snap is not None:
            bankroll_disk = round(br_snap, 2)
    if bankroll_disk <= 0:
        initial = float(
            (cfg_disk.get("backtest") or {}).get("initial_bankroll", 500) or 500
        )
        tp = float(summary.get("total_pnl", 0) or 0)
        if summary.get("session_id"):
            bankroll_disk = round(initial + tp, 2)

    open_disk = len(disk_positions)
    open_sum = int(summary.get("open_positions", 0) or 0)
    total_pos = open_disk if open_disk else open_sum
    portfolio_disk = {
        "total_positions": total_pos,
        "total_cost": float(summary.get("total_cost", 0) or 0),
        "total_exposure": float(summary.get("total_cost", 0) or 0),
        "open_stake": float(summary.get("total_cost", 0) or 0),
        "daily_pnl": 0.0,
        "daily_trades": 0,
        "emergency_stopped": False,
    }

    return {
        "running": False,
        "mode": "paper" if dry_run else "live",
        "dry_run": dry_run,
        "kill_switch_active": kill_switch_active,
        "can_trade": False,
        "can_trade_reason": "Bot not running",
        "bankroll": bankroll_disk,
        "portfolio": portfolio_disk,
        "positions": disk_positions,
        # surface summary fields so the UI can show historical stats
        "realized_pnl": summary.get("realized_pnl", 0),
        "total_pnl": summary.get("total_pnl", 0),
        "open_positions_count": summary.get("open_positions", 0),
        "session": session_cc,
        "ai": compute_ai_status(cfg_disk, None),
        "strategy_state": _build_strategy_state(cfg_disk, False),
        "session_id": summary.get("session_id"),
    }


def _process_env_ai_keys() -> Dict[str, str]:
    """Same secret names as ``main.py`` / ``PolyBot.set_api_keys`` for dashboard-only probes."""
    names = (
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY",
        "MINIMAX_API_KEY",
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
    )
    return {n: v for n in names if (v := os.getenv(n))}


@app.get("/api/ai/health")
async def get_ai_health():
    """Live MiniMax completion probe (strict JSON + ``estimated_probability``), not just key presence."""
    cfg: Dict[str, Any] = {}
    keys: Dict[str, str] = {}
    bot = _full_bot_instance()
    if bot is not None:
        cfg = bot.config
        keys = dict(getattr(bot.ai_agent, "api_keys", None) or {})
    elif CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
    if not keys:
        keys = _process_env_ai_keys()
    st = compute_ai_status(cfg, keys if keys else None)
    from src.analysis.ai_agent import run_minimax_live_probe

    probe = await run_minimax_live_probe(cfg, keys)
    return {
        "ok": bool(probe.get("ok")),
        "status": st,
        "probe": probe,
    }


# ─── API USAGE ────────────────────────────────────────────────────


@app.get("/api/usage/summary")
async def get_usage_summary():
    return usage_tracker.get_summary()


@app.get("/api/usage/records")
async def get_usage_records():
    return usage_tracker.get_all_records()


# ─── BACKTEST REPORTS ─────────────────────────────────────────────


@app.get("/api/backtest/reports")
async def get_backtest_reports():
    report_dir = DATA_ROOT / "backtest" / "reports"
    if not report_dir.exists():
        return {"reports": [], "latest": None}
    files = sorted(
        report_dir.glob("backtest_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    reports = []
    seen_crypto_keys = set()
    for idx, f in enumerate(files):
        try:
            with open(f) as fp:
                data = json.load(fp)
            crypto_key = None
            if data.get("report_type") == "crypto_updown" or (
                data.get("symbol") and data.get("window_minutes") and not data.get("per_strategy_metrics")
            ):
                crypto_key = (str(data.get("symbol", "")).upper(), int(data.get("window_minutes", 0) or 0))
            include_report = idx < 30
            if crypto_key and crypto_key not in seen_crypto_keys:
                # Keep the latest report for every symbol/window even when older than
                # the recent-file window, so Backtest cards do not regress to "Not yet run".
                seen_crypto_keys.add(crypto_key)
                include_report = True
            if not include_report:
                continue
            data["filename"] = f.name
            # Strip heavy fields to keep payload small
            data.pop("trades", None)
            data.pop("results", None)
            data.pop("stress_scenarios", None)
            reports.append(data)
        except Exception:
            pass
    return {"reports": reports, "latest": reports[0] if reports else None}


# ─── LIVE PERFORMANCE ──────────────────────────────────────────────


@app.get("/api/live/performance")
async def get_live_performance():
    """Live trade performance metrics sourced from summary.json (cached journal for
    closed-trade detail).  No fresh PerformanceTracker() construction per call."""
    summary = _get_journal_summary()
    strategy_stats = summary.get("strategy_stats", {})
    active_perf_strategies = (
        "bitcoin",
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "weather",
    )
    strategy_stats_filtered: Dict[str, Any] = {}
    if isinstance(strategy_stats, dict):
        strategy_stats_filtered = {
            k: v for k, v in strategy_stats.items() if k in active_perf_strategies
        }

    # Build closed-trade list for equity curve etc. using the cached journal
    closed_trades: List[Dict] = []
    try:
        j = _get_journal()
        if j:
            closed_trades = j.get_closed_trades()
    except Exception:
        pass

    total_exits = summary.get("total_exits", len(closed_trades))
    wins = summary.get("wins", 0)
    losses = summary.get("losses", 0)
    realized_pnl = summary.get("realized_pnl", 0.0)
    win_rate = summary.get("win_rate", 0.0)

    # avg win / loss
    win_pnls = [t.get("pnl", 0) for t in closed_trades if t.get("pnl", 0) > 0]
    loss_pnls = [t.get("pnl", 0) for t in closed_trades if t.get("pnl", 0) <= 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    profit_factor = (
        abs(sum(win_pnls) / sum(loss_pnls)) if sum(loss_pnls) != 0 else 0.0
    )

    # Equity curve (running cumulative PnL, last 200 points)
    equity_curve: List[float] = []
    running = 0.0
    for t in closed_trades:
        running += t.get("pnl", 0)
        equity_curve.append(round(running, 2))
    equity_curve = equity_curve[-200:]

    # Max drawdown
    max_drawdown = 0.0
    peak = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_drawdown:
            max_drawdown = dd

    kelly: Dict[str, Any] = {}
    try:
        kelly = _kelly_state_payload()
    except Exception as e:
        logger.warning("kelly_state payload failed (live/performance still returned): %s", e)

    return {
        "total_trades": total_exits,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(realized_pnl, 2),
        "max_drawdown": round(max_drawdown, 2),
        "sharpe_ratio": 0.0,
        "by_strategy": strategy_stats_filtered,
        "equity_curve": equity_curve,
        "kelly_state": kelly,
    }


@app.get("/api/live/drift")
async def get_live_drift():
    """Compare live performance against backtest expectations."""
    from src.execution.live_testing import PerformanceTracker

    perf = PerformanceTracker()
    # Load backtest expectations from latest reports
    report_dir = DATA_ROOT / "backtest" / "reports"
    expectations = {}
    if report_dir.exists():
        for f in sorted(
            report_dir.glob("backtest_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                with open(f) as fp:
                    data = json.load(fp)
                strategy = data.get("strategy")
                if strategy and strategy not in expectations:
                    bt_trades = data.get("trades", [])
                    wins = sum(1 for t in bt_trades if t.get("pnl", 0) > 0)
                    edges = [
                        t.get("edge", 0) for t in bt_trades if t.get("edge") is not None
                    ]
                    expectations[strategy] = {
                        "win_rate": wins / len(bt_trades) if bt_trades else 0,
                        "avg_edge": sum(edges) / len(edges) if edges else 0,
                        "trades_per_day": len(bt_trades)
                        / max(1, data.get("data_row_count", 1) / 24),
                    }
            except Exception:
                pass

    drift = perf.check_drift(expectations)
    return {
        "reports": [
            {
                "strategy": r.strategy,
                "bt_win_rate": round(r.bt_win_rate, 4),
                "live_win_rate": round(r.live_win_rate, 4),
                "win_rate_drift": round(r.win_rate_drift, 4),
                "bt_avg_edge": round(r.bt_avg_edge, 4),
                "live_avg_edge": round(r.live_avg_edge, 4),
                "edge_drift": round(r.edge_drift, 4),
                "bt_trades_per_day": round(r.bt_trades_per_day, 4),
                "live_trades_per_day": round(r.live_trades_per_day, 4),
                "trade_freq_drift": round(r.trade_freq_drift, 4),
                "live_sample_size": r.live_sample_size,
                "is_diverging": r.is_diverging,
                "verdict": r.verdict,
            }
            for r in drift
        ]
    }


# ─── BOT PROCESS MANAGEMENT ─────────────────────────────────────────


_bot_process: Optional[subprocess.Popen] = None


@app.get("/api/live/status")
async def get_bot_status():
    """Check if the live bot subprocess is running."""
    return {
        "running": _bot_process is not None and _bot_process.poll() is None,
        "pid": _bot_process.pid if _bot_process else None,
    }


@app.post("/api/live/start")
async def start_live_bot(request: Request):
    """Start the live bot as a background subprocess."""
    global _bot_process
    _check_auth(request)

    if _bot_process is not None and _bot_process.poll() is None:
        return {"status": "already_running", "pid": _bot_process.pid}

    try:
        _bot_process = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "src" / "main.py")],
            cwd=str(PROJECT_ROOT),
            env=_safe_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        logger.info(f"Live bot started with PID {_bot_process.pid}")
        return {"status": "started", "pid": _bot_process.pid}
    except Exception as e:
        logger.error(f"Failed to start live bot: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/live/stop")
async def stop_live_bot(request: Request):
    """Stop the live bot subprocess."""
    global _bot_process
    _check_auth(request)

    if _bot_process is None:
        return {"status": "not_running"}

    try:
        _bot_process.terminate()
        _bot_process.wait(timeout=10)
        logger.info("Live bot stopped")
        _bot_process = None
        return {"status": "stopped"}
    except subprocess.TimeoutExpired:
        _bot_process.kill()
        _bot_process = None
        return {"status": "killed"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ─── BACKTEST MANAGEMENT ──────────────────────────────────────────────

MAX_CONCURRENT_BACKTESTS = 4

_backtest_jobs_lock = threading.Lock()


@dataclass
class BacktestJob:
    job_id: str
    proc: subprocess.Popen
    output: List[str] = field(default_factory=list)
    summary: str = ""


_backtest_jobs: Dict[str, BacktestJob] = {}
_auto_startup_backtests_started: set[tuple[str, str]] = set()
_auto_backtest_start_lock = threading.Lock()


def _prune_finished_backtest_jobs(max_keep: int = 48) -> None:
    """Drop oldest finished jobs so the map does not grow forever."""
    with _backtest_jobs_lock:
        if len(_backtest_jobs) <= max_keep:
            return
        finished = [
            jid
            for jid, j in _backtest_jobs.items()
            if j.proc.poll() is not None
        ]
        for jid in finished[: max(0, len(_backtest_jobs) - max_keep)]:
            _backtest_jobs.pop(jid, None)


def _backtest_reader(job: BacktestJob) -> None:
    """Background thread: reads subprocess stdout into job.output."""
    try:
        for line in job.proc.stdout:
            line = line.rstrip()
            if line:
                job.output.append(line)
    except Exception:
        pass


def _start_backtest_job(cmd_args: List[str], summary: str) -> Dict[str, Any]:
    """Spawn a backtest subprocess, register in-memory tracking, and return API payload."""
    jid = uuid.uuid4().hex[:10]
    proc = subprocess.Popen(
        cmd_args,
        cwd=str(PROJECT_ROOT),
        env=_safe_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    job = BacktestJob(job_id=jid, proc=proc, output=[], summary=summary)
    with _backtest_jobs_lock:
        _backtest_jobs[jid] = job
    threading.Thread(
        target=_backtest_reader,
        args=(job,),
        daemon=True,
    ).start()
    _prune_finished_backtest_jobs()
    logger.info("Backtest job %s started (PID %s) %s", jid, proc.pid, summary)
    return {"status": "started", "job_id": jid, "pid": proc.pid, "summary": summary}


def _auto_backtest_specs(session_id: str, phase: str) -> List[Tuple[str, List[str], str]]:
    specs: List[Tuple[str, List[str], str]] = []
    bot = _full_bot_instance()
    dashboard_cfg = (bot.config.get("dashboard", {}) or {}) if bot else {}

    if dashboard_cfg.get(f"auto_sol5_backtest_on_{phase}", True):
        crypto_script = PROJECT_ROOT / "scripts" / "run_backtest_crypto.py"
        if crypto_script.exists():
            specs.append(
                (
                    "sol5",
                    [
                        sys.executable,
                        str(crypto_script),
                        "--symbol",
                        "SOL",
                        "--window",
                        "5",
                    ],
                    f"SOL 5m crypto [auto-on-{phase}:{session_id}]",
                )
            )

    if dashboard_cfg.get(f"auto_weather_backtest_on_{phase}", True):
        weather_script = PROJECT_ROOT / "scripts" / "run_backtest_weather.py"
        if weather_script.exists():
            specs.append(
                (
                    "weather",
                    [
                        sys.executable,
                        str(weather_script),
                        "--quick",
                        "--save-report",
                    ],
                    f"weather live [auto-on-{phase}:{session_id}]",
                )
            )

    return specs


def _maybe_start_auto_backtests(phase: str) -> List[Dict[str, Any]]:
    """Optionally kick off the configured auto backtest batch for startup/reset."""
    global _auto_startup_backtests_started
    bot = _full_bot_instance()
    if not bot:
        return []
    if not bot.config.get("trading", {}).get("dry_run", True):
        return []

    current_session_id = getattr(getattr(bot, "journal", None), "session_id", None)
    if not current_session_id:
        return []

    specs = _auto_backtest_specs(current_session_id, phase)
    if not specs:
        return []

    results: List[Dict[str, Any]] = []
    with _auto_backtest_start_lock:
        with _backtest_jobs_lock:
            running_n = sum(1 for j in _backtest_jobs.values() if j.proc.poll() is None)
        for key, cmd_args, summary in specs:
            dedupe_key = (current_session_id, f"{phase}:{key}")
            if phase == "startup" and dedupe_key in _auto_startup_backtests_started:
                results.append(
                    {
                        "name": key,
                        "status": "skipped",
                        "reason": "startup_dedupe",
                    }
                )
                continue
            if running_n >= MAX_CONCURRENT_BACKTESTS:
                results.append(
                    {
                        "name": key,
                        "status": "skipped",
                        "reason": "max_concurrent_backtests",
                    }
                )
                continue
            if phase == "startup":
                _auto_startup_backtests_started.add(dedupe_key)
            try:
                result = _start_backtest_job(cmd_args, summary)
                result["name"] = key
                results.append(result)
                running_n += 1
            except Exception as e:
                if phase == "startup":
                    _auto_startup_backtests_started.discard(dedupe_key)
                logger.error("Auto %s backtest on %s failed: %s", key, phase, e)
                results.append({"name": key, "status": "error", "reason": str(e)})
    return results


@app.get("/api/backtest/status")
async def get_backtest_status(job_id: Optional[str] = None):
    """Backtest status. Optional job_id scopes to one job; else lists all jobs."""
    with _backtest_jobs_lock:
        if job_id:
            if job_id not in _backtest_jobs:
                return {
                    "running": False,
                    "job_unknown": True,
                    "jobs": [],
                    "output": [],
                }
            j = _backtest_jobs[job_id]
            alive = j.proc.poll() is None
            return {
                "running": alive,
                "jobs": [
                    {
                        "job_id": job_id,
                        "running": alive,
                        "pid": j.proc.pid,
                        "summary": j.summary,
                    }
                ],
                "output": j.output[-50:],
            }
        jobs_payload: List[Dict[str, Any]] = []
        any_running = False
        merged_out: List[str] = []
        for jid, j in _backtest_jobs.items():
            alive = j.proc.poll() is None
            any_running = any_running or alive
            jobs_payload.append(
                {
                    "job_id": jid,
                    "running": alive,
                    "pid": j.proc.pid,
                    "summary": j.summary,
                }
            )
            if alive:
                merged_out.extend(j.output[-15:])
        return {
            "running": any_running,
            "jobs": jobs_payload,
            "output": merged_out[-50:],
        }


@app.post("/api/backtest/start")
async def start_backtest(request: Request):
    """Start a backtest subprocess (multiple may run while the live bot trades)."""
    global _backtest_jobs
    _check_auth(request)

    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    strategies_raw = body.get("strategies", "fade arbitrage")
    if isinstance(strategies_raw, str):
        strategies_list = [
            x.strip()
            for x in re.split(r"[\s,]+", strategies_raw.strip())
            if x.strip()
        ]
    else:
        strategies_list = [str(x).strip() for x in strategies_raw if str(x).strip()]
    if not strategies_list:
        strategies_list = ["fade", "arbitrage"]
    periods = body.get("periods", "all")
    symbol = body.get("symbol", "")
    window = str(body.get("window", 15))
    test_start = body.get("test_start", "").strip()

    with _backtest_jobs_lock:
        running_n = sum(
            1 for j in _backtest_jobs.values() if j.proc.poll() is None
        )
        if running_n >= MAX_CONCURRENT_BACKTESTS:
            return {
                "status": "error",
                "message": (
                    f"Max {MAX_CONCURRENT_BACKTESTS} concurrent backtests "
                    "(wait for one to finish or use /api/backtest/status)."
                ),
            }

    if symbol in ("BTC", "SOL", "ETH", "HYPE", "XRP"):
        script = PROJECT_ROOT / "scripts" / "run_backtest_crypto.py"
        if not script.exists():
            return {"status": "error", "message": f"{script} not found"}
        cmd_args = [
            sys.executable,
            str(script),
            "--symbol", symbol,
            "--window", window,
        ]
        if test_start:
            cmd_args += ["--test-start", test_start]
        summary = f"{symbol} {window}m crypto" + (f" test-from={test_start}" if test_start else " [in-sample]")
    else:
        script = PROJECT_ROOT / "scripts" / "run_backtest_rigorous.py"
        if not script.exists():
            return {"status": "error", "message": f"{script} not found"}
        cmd_args = [sys.executable, str(script), "--strategies", *strategies_list]
        if periods == "all":
            cmd_args.append("--no-train-test")
        cmd_args.extend(["--no-stress", "--save-report", "--quick"])
        summary = f"rigorous {' '.join(strategies_list)}"

    try:
        return _start_backtest_job(cmd_args, summary)
    except Exception as e:
        logger.error(f"Failed to start backtest: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/backtest/output")
async def get_backtest_output(job_id: Optional[str] = None):
    """Tail of backtest stdout. Pass job_id when multiple jobs are active."""
    with _backtest_jobs_lock:
        if job_id and job_id in _backtest_jobs:
            j = _backtest_jobs[job_id]
            return {
                "lines": j.output[-100:],
                "running": j.proc.poll() is None,
                "job_id": job_id,
            }
        alive = [j for j in _backtest_jobs.values() if j.proc.poll() is None]
        if len(alive) == 1:
            j = alive[0]
            return {
                "lines": j.output[-100:],
                "running": True,
                "job_id": j.job_id,
            }
        return {
            "lines": [],
            "running": bool(alive),
            "job_id": None,
            "hint": "Pass job_id when multiple backtests are running",
        }


# ─── TEST RESULTS ─────────────────────────────────────────────────


@app.get("/api/tests/results")
async def get_test_results():
    """Run pytest and return structured results."""
    import re

    _ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    test_dir = PROJECT_ROOT / "tests"
    if not test_dir.exists():
        return {
            "status": "no_tests",
            "tests": [],
            "summary": "No tests directory found.",
        }
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(test_dir),
                "-v",
                "--tb=line",
                "--no-header",
                "-p",
                "no:sugar",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
            env=_safe_env(),
        )
        raw = _ansi_re.sub("", result.stdout).strip()
        lines = raw.split("\n")
        tests = []
        for line in lines:
            if " PASSED" in line or " FAILED" in line or " ERROR" in line:
                status = (
                    "passed"
                    if "PASSED" in line
                    else "failed"
                    if "FAILED" in line
                    else "error"
                )
                # Strip everything from " PASSED" / " FAILED" / " ERROR" onward
                for marker in [" PASSED", " FAILED", " ERROR"]:
                    if marker in line:
                        name = line[: line.index(marker)].strip()
                        break
                else:
                    name = line.strip()
                tests.append({"name": name, "status": status})

        summary_line = ""
        for line in reversed(lines):
            if "passed" in line or "failed" in line:
                summary_line = line.strip()
                break

        passed = sum(1 for t in tests if t["status"] == "passed")
        failed = sum(1 for t in tests if t["status"] != "passed")
        return {
            "status": "passed" if failed == 0 else "failed",
            "passed": passed,
            "failed": failed,
            "tests": tests,
            "summary": summary_line,
            "stderr": result.stderr[-500:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "tests": [], "summary": "Tests timed out (120s)"}
    except Exception as e:
        return {"status": "error", "tests": [], "summary": str(e)}


# ─── LIVE SCAN RESULTS ────────────────────────────────────────────


@app.get("/api/scans/latest")
async def get_latest_scan():
    """Return the most recent live scan results."""
    scan_dir = DATA_ROOT / "live_scans"
    if not scan_dir.exists():
        return {"scan": None, "available": 0}
    files = sorted(
        scan_dir.glob("scan_*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not files:
        return {"scan": None, "available": 0}
    try:
        with open(files[0]) as fp:
            data = json.load(fp)
        data["filename"] = files[0].name
        return {"scan": data, "available": len(files)}
    except Exception as e:
        return {"scan": None, "available": len(files), "error": str(e)}


@app.get("/api/scans/history")
async def get_scan_history():
    """Return all scan summaries for trend tracking."""
    scan_dir = DATA_ROOT / "live_scans"
    if not scan_dir.exists():
        return {"scans": []}
    files = sorted(
        scan_dir.glob("scan_*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    scans = []
    for f in files[:50]:
        try:
            with open(f) as fp:
                data = json.load(fp)
            scans.append(
                {
                    "filename": f.name,
                    "timestamp": data.get("timestamp", ""),
                    "markets_scanned": data.get("markets_scanned", 0),
                    "total_signals": len(data.get("signals", [])),
                    "fade": sum(
                        1
                        for s in data.get("signals", [])
                        if s.get("strategy") == "fade"
                    ),
                    "arbitrage": sum(
                        1
                        for s in data.get("signals", [])
                        if s.get("strategy") == "arbitrage"
                    ),
                    "neh": sum(
                        1 for s in data.get("signals", []) if s.get("strategy") == "neh"
                    ),
                    "distribution": data.get("distribution", {}),
                }
            )
        except Exception:
            pass
    return {"scans": scans}


@app.post("/api/scans/run")
async def run_live_scan(request: Request):
    """Trigger a live scan from the dashboard."""
    _check_auth(request)
    script = PROJECT_ROOT / "scripts" / "live_strategy_scan.py"
    if not script.exists():
        raise HTTPException(status_code=404, detail="live_strategy_scan.py not found")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--strategy",
                "all",
                "--limit",
                "200",
                "--min-volume",
                "10000",
                "--save",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(PROJECT_ROOT),
            env=_safe_env(),
        )
        return {
            "status": "completed",
            "output": result.stdout[-2000:],
            "errors": result.stderr[-500:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "output": "Scan timed out after 180s"}
    except Exception as e:
        return {"status": "error", "output": str(e)}


@app.get("/api/strategy/watchlist")
async def get_strategy_watchlist(
    limit: int = 40,
    include_general_markets: bool = True,
):
    """Approximate 'next trigger' levels for dashboard visualization.

    This is display-oriented guidance: it shows how far a market's current
    probability is from strategy entry zones (and for updown markets, how far
    spot price is from the parsed threshold).

    When include_general_markets is False, skips the Gamma scan (arb/fade/neh)
    and only returns crypto updown rows from the latest live scan — fast path
    for the BTC chart + reason-buckets panel (~ms instead of tens of seconds).
    """
    limit = max(10, min(limit, 200))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}

    strategies_cfg = cfg.get("strategies", {})
    watchlist: List[Dict[str, Any]] = []

    # ── General market watchlist (fade/arb/neh) ─────────────────────────────
    if include_general_markets:
        try:
            from src.market.scanner import MarketScanner

            scanner = MarketScanner(cfg)
            markets = await scanner.fetch_markets(limit=250)
            markets = await scanner.update_market_prices(markets)

            arb_cfg = strategies_cfg.get("arbitrage", {})
            fade_cfg = strategies_cfg.get("fade", {})
            neh_cfg = strategies_cfg.get("neh", {})

            arb_min = float(arb_cfg.get("entry_price_min", 0.20))
            arb_max = float(arb_cfg.get("entry_price_max", 0.40))
            fade_low = float(fade_cfg.get("consensus_threshold_lower", 0.80))
            fade_high = float(fade_cfg.get("consensus_threshold_upper", 0.95))
            fade_e_min = float(fade_cfg.get("entry_price_min", 0.05))
            fade_e_max = float(fade_cfg.get("entry_price_max", 0.45))
            neh_max_yes = float(neh_cfg.get("max_yes_price", 0.02))
            neh_min_days = float(neh_cfg.get("min_days_to_resolution", 60))

            for m in markets:
                yes = float(m.yes_price)
                no = float(m.no_price)
                cheap_side = min(yes, no)
                cheap_action = "BUY_YES" if yes <= no else "BUY_NO"

                # Arbitrage: cheap-side entry zone
                if cheap_side < arb_min:
                    arb_trigger = arb_min
                    arb_ready = False
                elif cheap_side > arb_max:
                    arb_trigger = arb_max
                    arb_ready = False
                else:
                    arb_trigger = cheap_side
                    arb_ready = True
                watchlist.append(
                    {
                        "strategy": "arbitrage",
                        "market_id": m.id,
                        "market_question": m.question,
                        "action_hint": cheap_action,
                        "current_price": cheap_side,
                        "trigger_price": arb_trigger,
                        "distance": abs(cheap_side - arb_trigger),
                        "ready": arb_ready,
                        "block_reason": "" if arb_ready else "outside_entry_zone",
                    }
                )

                # Fade: consensus + opposite side entry band
                consensus_side = max(yes, no)
                opposite = min(yes, no)
                if consensus_side < fade_low:
                    fade_trigger = fade_low
                    fade_reason = "consensus_not_extreme"
                elif consensus_side > fade_high:
                    fade_trigger = fade_high
                    fade_reason = "lottery_zone"
                elif opposite < fade_e_min:
                    fade_trigger = fade_e_min
                    fade_reason = "opposite_too_cheap"
                elif opposite > fade_e_max:
                    fade_trigger = fade_e_max
                    fade_reason = "opposite_too_expensive"
                else:
                    fade_trigger = opposite
                    fade_reason = ""
                fade_ready = fade_reason == ""
                watchlist.append(
                    {
                        "strategy": "fade",
                        "market_id": m.id,
                        "market_question": m.question,
                        "action_hint": "BUY_NO" if yes >= no else "BUY_YES",
                        "current_price": opposite,
                        "trigger_price": fade_trigger,
                        "distance": abs(opposite - fade_trigger),
                        "ready": fade_ready,
                        "block_reason": fade_reason,
                    }
                )

                # NEH: ultra-low YES + long duration
                days_to_res = (m.hours_to_expiration / 24.0) if m.hours_to_expiration is not None else 0.0
                neh_ready = yes <= neh_max_yes and days_to_res >= neh_min_days
                watchlist.append(
                    {
                        "strategy": "neh",
                        "market_id": m.id,
                        "market_question": m.question,
                        "action_hint": "SELL_YES",
                        "current_price": yes,
                        "trigger_price": neh_max_yes,
                        "distance": abs(yes - neh_max_yes),
                        "ready": neh_ready,
                        "block_reason": "" if neh_ready else ("too_expensive" if yes > neh_max_yes else "too_short_term"),
                    }
                )

            await scanner.close()
        except Exception as e:
            logger.warning(f"Watchlist general markets unavailable: {e}")

    # ── Crypto updown watchlist (bitcoin / sol_macro / eth_macro / xrp_macro)
    try:
        btc_cfg = strategies_cfg.get("bitcoin", {})
        sol_cfg = strategies_cfg.get("sol_macro", {})
        eth_cfg = strategies_cfg.get("eth_macro", {})
        hype_cfg = strategies_cfg.get("hype_macro", {})
        xrp_cfg = strategies_cfg.get("xrp_macro", {})
        btc_e_min = float(btc_cfg.get("entry_price_min", 0.10))
        btc_e_max = float(btc_cfg.get("entry_price_max", 0.90))
        sol_e_min = float(sol_cfg.get("entry_price_min", 0.46))
        sol_e_max = float(sol_cfg.get("entry_price_max", 0.54))
        eth_e_min = float(eth_cfg.get("entry_price_min", 0.46))
        eth_e_max = float(eth_cfg.get("entry_price_max", 0.54))
        # XRP dump-hedge: display band for token price (signals are event-driven; wide band avoids false "blocked")
        xrp_e_min = float(xrp_cfg.get("watchlist_entry_min", 0.02))
        xrp_e_max = float(xrp_cfg.get("watchlist_entry_max", 0.98))

        # Fetch all spot prices in parallel via threads — these are blocking
        # network calls (Binance) and would otherwise pin the dashboard event
        # loop, stalling every other endpoint that arrives during the wait.
        from src.analysis.sol_btc_service import SOLBTCService

        def _btc_spot_sync():
            try:
                if bot_instance and hasattr(bot_instance, "bitcoin_strategy"):
                    v = bot_instance.bitcoin_strategy.btc_service.get_current_price()
                    if v is not None:
                        return v
                return _get_btc_svc().get_current_price()
            except Exception:
                return None

        def _sol_spot_sync():
            try:
                if bot_instance and hasattr(bot_instance, "sol_macro_strategy"):
                    return bot_instance.sol_macro_strategy.sol_service.get_current_price("SOLUSDT")
            except Exception:
                pass
            return None

        def _eth_spot_sync():
            try:
                if bot_instance and getattr(bot_instance, "eth_macro_strategy", None):
                    v = bot_instance.eth_macro_strategy.sol_service.get_current_price("ETHUSDT")
                    if v is not None:
                        return v
                return SOLBTCService(alt_symbol="ETHUSDT").get_current_price("ETHUSDT")
            except Exception:
                return None

        def _xrp_spot_sync():
            try:
                return SOLBTCService(alt_symbol="XRPUSDT").get_current_price("XRPUSDT")
            except Exception:
                return None

        btc_spot, sol_spot, eth_spot, xrp_spot_cached = await asyncio.gather(
            asyncio.to_thread(_btc_spot_sync),
            asyncio.to_thread(_sol_spot_sync),
            asyncio.to_thread(_eth_spot_sync),
            asyncio.to_thread(_xrp_spot_sync),
        )

        scan_file_dir = DATA_ROOT / "live_scans"
        files = sorted(scan_file_dir.glob("scan_*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if scan_file_dir.exists() else []
        signals = []
        if files:
            try:
                with open(files[0], encoding="utf-8") as fp:
                    signals = json.load(fp).get("signals", [])
            except Exception:
                signals = []

        for s in signals:
            strat = s.get("strategy")
            q = s.get("market_question", "")
            price = float(s.get("price", 0) or 0)
            if strat == "bitcoin":
                threshold = _parse_threshold(q, asset="btc")
                if threshold and btc_spot:
                    dist_pct = abs(float(btc_spot) - threshold) / threshold * 100.0
                else:
                    dist_pct = None
                trigger = btc_e_min if price < btc_e_min else btc_e_max if price > btc_e_max else price
                watchlist.append(
                    {
                        "strategy": "bitcoin",
                        "market_id": s.get("market_id"),
                        "market_question": q,
                        "action_hint": s.get("action", _parse_direction(q)),
                        "current_price": price,
                        "trigger_price": trigger,
                        "distance": abs(price - trigger),
                        "ready": btc_e_min <= price <= btc_e_max,
                        "block_reason": "" if btc_e_min <= price <= btc_e_max else "outside_entry_zone",
                        "spot_price": btc_spot,
                        "threshold_price": threshold,
                        "spot_distance_pct": dist_pct,
                    }
                )
            elif strat == "sol_macro":
                threshold = _parse_threshold(q, asset="sol")
                if threshold and sol_spot:
                    dist_pct = abs(float(sol_spot) - threshold) / threshold * 100.0
                else:
                    dist_pct = None
                trigger = sol_e_min if price < sol_e_min else sol_e_max if price > sol_e_max else price
                watchlist.append(
                    {
                        "strategy": "sol_macro",
                        "market_id": s.get("market_id"),
                        "market_question": q,
                        "action_hint": s.get("action", _parse_direction(q)),
                        "current_price": price,
                        "trigger_price": trigger,
                        "distance": abs(price - trigger),
                        "ready": sol_e_min <= price <= sol_e_max,
                        "block_reason": "" if sol_e_min <= price <= sol_e_max else "outside_entry_zone",
                        "spot_price": sol_spot,
                        "threshold_price": threshold,
                        "spot_distance_pct": dist_pct,
                    }
                )
            elif strat == "eth_macro":
                threshold = _parse_threshold(q, asset="eth")
                if threshold and eth_spot:
                    dist_pct = abs(float(eth_spot) - threshold) / threshold * 100.0
                else:
                    dist_pct = None
                trigger = eth_e_min if price < eth_e_min else eth_e_max if price > eth_e_max else price
                watchlist.append(
                    {
                        "strategy": "eth_macro",
                        "market_id": s.get("market_id"),
                        "market_question": q,
                        "action_hint": s.get("action", _parse_direction(q)),
                        "current_price": price,
                        "trigger_price": trigger,
                        "distance": abs(price - trigger),
                        "ready": eth_e_min <= price <= eth_e_max,
                        "block_reason": "" if eth_e_min <= price <= eth_e_max else "outside_entry_zone",
                        "spot_price": eth_spot,
                        "threshold_price": threshold,
                        "spot_distance_pct": dist_pct,
                    }
                )
            elif strat == "hype_macro":
                hype_e_min = float(hype_cfg.get("entry_price_min", 0.46))
                hype_e_max = float(hype_cfg.get("entry_price_max", 0.54))
                trigger = hype_e_min if price < hype_e_min else hype_e_max if price > hype_e_max else price
                watchlist.append(
                    {
                        "strategy": "hype_macro",
                        "market_id": s.get("market_id"),
                        "market_question": q,
                        "action_hint": s.get("action", _parse_direction(q)),
                        "current_price": price,
                        "trigger_price": trigger,
                        "distance": abs(price - trigger),
                        "ready": hype_e_min <= price <= hype_e_max,
                        "block_reason": "" if hype_e_min <= price <= hype_e_max else "outside_entry_zone",
                    }
                )
            elif strat == "xrp_macro":
                threshold = _parse_threshold(q, asset="xrp")
                xrp_spot = xrp_spot_cached
                dist_pct = None
                if threshold and xrp_spot:
                    dist_pct = abs(float(xrp_spot) - threshold) / threshold * 100.0
                trigger = xrp_e_min if price < xrp_e_min else xrp_e_max if price > xrp_e_max else price
                in_band = xrp_e_min <= price <= xrp_e_max
                watchlist.append(
                    {
                        "strategy": "xrp_macro",
                        "market_id": s.get("market_id"),
                        "market_question": q,
                        "action_hint": s.get("action", _parse_direction(q)),
                        "current_price": price,
                        "trigger_price": trigger,
                        "distance": abs(price - trigger),
                        "ready": in_band,
                        "block_reason": "" if in_band else "outside_entry_zone",
                        "spot_price": xrp_spot,
                        "threshold_price": threshold,
                        "spot_distance_pct": dist_pct,
                    }
                )
    except Exception as e:
        logger.warning(f"Watchlist crypto markets unavailable: {e}")

    # Keep nearest candidates first, with READY entries pinned to top per strategy.
    by_strat: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in watchlist:
        by_strat[row.get("strategy", "unknown")].append(row)

    out: List[Dict[str, Any]] = []
    for strat, rows in by_strat.items():
        rows.sort(key=lambda r: (0 if r.get("ready") else 1, float(r.get("distance", 9999.0))))
        out.extend(rows[:8])

    out.sort(key=lambda r: (r.get("strategy", ""), 0 if r.get("ready") else 1, float(r.get("distance", 9999.0))))
    return {"watchlist": out[:limit]}


# ─── STRATEGY METRICS ─────────────────────────────────────────────


@app.get("/api/strategy/metrics")
async def get_strategy_metrics():
    """Aggregate strategy performance.

    Primary source: summary.json strategy_stats (always fresh, fast).
    bot_instance only supplements real-time signal counts / cycle timestamps
    if it happens to be running.
    """
    metrics = {
        "fade": {"signals": 0, "trades": 0, "pnl": 0, "win_rate": None, "reports": 0},
        "arbitrage": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "reports": 0,
        },
        "neh": {"signals": 0, "trades": 0, "pnl": 0, "win_rate": None, "reports": 0},
        "bitcoin": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "reports": 0,
        },
        "sol_macro": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "reports": 0,
        },
        "eth_macro": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "reports": 0,
        },
        "hype_macro": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "reports": 0,
        },
        "xrp_macro": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "reports": 0,
        },
        "weather": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "reports": 0,
            "subtypes": {
                "temp": {"trades": 0, "wins": 0, "pnl": 0, "avg_pnl": 0, "win_rate": 0, "open": 0, "unrealized_pnl": 0},
                "precip": {"trades": 0, "wins": 0, "pnl": 0, "avg_pnl": 0, "win_rate": 0, "open": 0, "unrealized_pnl": 0},
            },
        },
    }

    # ── Primary: live trade stats from summary.json (disk-first) ──
    summary = _get_journal_summary()
    for strat, s in summary.get("strategy_stats", {}).items():
        if strat in metrics:
            metrics[strat]["trades"] = s.get("trades", 0)
            metrics[strat]["pnl"] = s.get("pnl", 0)
            metrics[strat]["win_rate"] = s.get("win_rate", None)
            metrics[strat]["wins"] = s.get("wins", 0)
            metrics[strat]["avg_pnl"] = s.get("avg_pnl", 0)
    weather_subtypes = summary.get("weather_subtype_stats", {}) or {}
    weather_open = summary.get("weather_open_stats", {}) or {}
    for subtype in ("temp", "precip"):
        stats = weather_subtypes.get(subtype, {}) or {}
        open_stats = weather_open.get(subtype, {}) or {}
        metrics["weather"]["subtypes"][subtype].update(
            {
                "trades": stats.get("trades", 0),
                "wins": stats.get("wins", 0),
                "pnl": stats.get("pnl", 0),
                "avg_pnl": stats.get("avg_pnl", 0),
                "win_rate": stats.get("win_rate", 0),
                "open": open_stats.get("open", 0),
                "unrealized_pnl": open_stats.get("unrealized_pnl", 0),
            }
        )

    # ── Aggregate backtest report counts (lightweight metadata only) ──
    report_dir = DATA_ROOT / "backtest" / "reports"
    if report_dir.exists():
        for f in report_dir.glob("backtest_*.json"):
            try:
                with open(f) as fp:
                    data = json.load(fp)
                strat = data.get("strategy", "")
                if strat in metrics:
                    metrics[strat]["reports"] += 1
            except Exception:
                pass

    # ── Aggregate signals from latest scan file ──
    scan_dir = DATA_ROOT / "live_scans"
    if scan_dir.exists():
        files = sorted(
            scan_dir.glob("scan_*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if files:
            try:
                with open(files[0]) as fp:
                    scan = json.load(fp)
                for sig in scan.get("signals", []):
                    strat = sig.get("strategy", "")
                    if strat in metrics:
                        metrics[strat]["signals"] += 1
            except Exception:
                pass

    # ── Supplement with real-time bot data when available ──
    bot = _full_bot_instance()
    if bot and hasattr(bot, "last_signal_counts"):
        for strat, count in bot.last_signal_counts.items():
            if strat in metrics:
                metrics[strat]["signals"] = count
        # Cycle timestamps — when did each strategy last complete a scan?
        if hasattr(bot, "last_cycle_times"):
            for strat, t in bot.last_cycle_times.items():
                if strat in metrics:
                    metrics[strat]["last_cycle"] = t
        # Cumulative signal counts (never reset — shows lifetime activity)
        if hasattr(bot, "cumulative_signal_counts"):
            for strat, total in bot.cumulative_signal_counts.items():
                if strat in metrics:
                    metrics[strat]["total_signals"] = total

    # ── Open position count from bot in-memory state (real-time) ──
    if bot:
        for p in bot.risk_manager.active_positions.values():
            strat = getattr(p, "strategy", "unknown")
            if strat in metrics:
                metrics[strat]["open_positions"] = (
                    metrics[strat].get("open_positions", 0) + 1
                )

    # ── Weather scan diagnostics ──
    if bot and hasattr(bot, "last_ai_scan_stats"):
        wx = bot.last_ai_scan_stats.get("weather") or {}
        if wx and "weather" in metrics:
            metrics["weather"]["scan_stats"] = {
                "total_markets_seen":      wx.get("total_markets_seen", 0),
                "weather_keyword_matches": wx.get("weather_keyword_matches", 0),
                "markets_scanned":        wx.get("markets_scanned", 0),
                "city_matches":           wx.get("city_matches", 0),
                "temp_markets":           wx.get("temp_markets", 0),
                "precip_markets":         wx.get("precip_markets", 0),
                "signals_generated":      wx.get("signals_generated", 0),
                "skipped_no_location":    wx.get("skipped_no_location", 0),
                "skipped_no_temp_keyword": wx.get("skipped_no_temp_keyword", 0),
                "skipped_below_liquidity": wx.get("skipped_below_liquidity", 0),
                "skipped_below_volume":   wx.get("skipped_below_volume", 0),
                "skipped_below_min_hours": wx.get("skipped_below_min_hours", 0),
                "skipped_above_max_hours": wx.get("skipped_above_max_hours", 0),
                "skipped_too_far_out":    wx.get("skipped_too_far_out", 0),
                "skipped_extreme_consensus": wx.get("skipped_extreme_consensus", 0),
                "skipped_ev":             wx.get("skipped_ev", 0),
                "skipped_no_threshold":   wx.get("skipped_no_threshold", 0),
                "skipped_no_forecast":    wx.get("skipped_no_forecast", 0),
                "skipped_metar_mismatch": wx.get("skipped_metar_mismatch", 0),
            }

    return metrics


# ─── PAPER TRADE JOURNAL ──────────────────────────────────────────


@app.post("/api/journal/invalidate-cache")
async def invalidate_journal_cache(request: Request):
    """Clear the in-memory TradeJournal cache so the next read replays ``entries.jsonl`` from disk."""
    _check_auth(request)
    global _journal_cache
    _journal_cache = {"path": None, "mtime": None, "journal": None}
    return {"status": "ok"}


@app.get("/api/journal/summary")
async def get_journal_summary(session_id: Optional[str] = None):
    """Return journal summary from TradeJournal when possible (see _get_journal_summary).

    Optional ``session_id`` loads that run from disk (active or ``paper_trades_archive``).
    """
    if session_id:
        j = _journal_for_query(session_id)
        if not j:
            raise HTTPException(status_code=404, detail="Session not found")
        out = j.get_summary()
        out["_source"] = (
            "archived"
            if "paper_trades_archive" in str(j.session_dir.resolve())
            else "active"
        )
        return out
    return _get_journal_summary()


@app.get("/api/journal/positions")
async def get_journal_positions(session_id: Optional[str] = None):
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"positions": j.get_open_positions() if j else []}


@app.get("/api/journal/trades")
async def get_journal_trades(session_id: Optional[str] = None):
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"trades": j.get_closed_trades() if j else []}


@app.get("/api/journal/trade-points")
async def get_journal_trade_points(limit: int = 300, session_id: Optional[str] = None):
    """Normalized closed-trade points for charting.

    Returns epoch `time` + entry/exit prices so frontend can render bubble/marker
    overlays without guessing field names.
    """
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    trades = j.get_closed_trades() if j else []
    points: List[Dict[str, Any]] = []
    for t in trades[-max(10, min(limit, 2000)) :]:
        # Anchor chart marker at entry time (learning) once trade is closed; fallback to exit.
        entry_ts = t.get("opened_at") or t.get("closed_at")
        try:
            epoch = int(datetime.fromisoformat(str(entry_ts)).timestamp()) if entry_ts else None
        except Exception:
            epoch = None
        if not epoch:
            continue
        closed_ts = t.get("closed_at")
        closed_epoch = None
        if closed_ts:
            try:
                closed_epoch = int(datetime.fromisoformat(str(closed_ts)).timestamp())
            except Exception:
                closed_epoch = None
        points.append(
            {
                "time": epoch,
                "closed_at": closed_epoch,
                "strategy": t.get("strategy", "unknown"),
                "market_id": t.get("market_id"),
                "market_question": t.get("market_question", ""),
                "entry_price": float(t.get("entry_price", 0) or 0),
                "exit_price": float(t.get("exit_price", t.get("current_price", 0)) or 0),
                "pnl": float(t.get("pnl", 0) or 0),
                "outcome": "win" if float(t.get("pnl", 0) or 0) >= 0 else "loss",
            }
        )
    return {"points": points}


@app.get("/api/journal/trade_journey")
async def get_trade_journey(
    strategy: Optional[str] = None,
    limit: int = 24,
    session_id: Optional[str] = None,
):
    """Recent closed trades as a compact timeline for the dashboard (all crypto strategies)."""
    limit = max(1, min(int(limit), 200))
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    trades = list(j.get_closed_trades() if j else [])
    if strategy:
        trades = [t for t in trades if t.get("strategy") == strategy]
    trades.sort(
        key=lambda t: str(t.get("closed_at") or t.get("opened_at") or ""),
        reverse=True,
    )
    trades = trades[:limit]
    out: List[Dict[str, Any]] = []
    for t in trades:
        q = t.get("market_question") or ""
        mid = str(t.get("market_id", "") or "")
        out.append(
            {
                "trade_id": t.get("trade_id"),
                "strategy": t.get("strategy"),
                "updown_bucket": _classify_updown_trade(q, str(t.get("strategy", "")), mid),
                "opened_at": t.get("opened_at"),
                "closed_at": t.get("closed_at"),
                "market_question": q[:160],
                "action": t.get("action"),
                "side": t.get("side"),
                "pnl": t.get("pnl"),
                "exit_reason": t.get("exit_reason"),
                "edge": t.get("edge"),
            }
        )
    return {
        "trades": out,
        "limit": limit,
        "strategy_filter": strategy,
    }


@app.get("/api/journal/updown_breakdown")
async def get_updown_breakdown():
    """Break down closed trades by strategy type (BTC/SOL updown 15m/5m).
    Also splits OLD CODE (pre-restart) vs NEW CODE (post-restart) results.
    NEW CODE is detected by first appearance of 'Anti-LTF gate passed' in today's log.
    """
    import re as _re
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    from collections import defaultdict as _dd

    # Detect new-code start time — search last 7 days of logs (not just today)
    log_dir = DATA_ROOT / "logs"
    NEW_CODE_MARKERS = ["Anti-LTF gate passed", "4H histogram", "1H histogram"]
    new_code_start: str | None = None
    for _days_back in range(7):
        _check_date = (_dt.now() - timedelta(days=_days_back)).strftime("%Y%m%d")
        _log_path = log_dir / f"polybot_{_check_date}.log"
        if not _log_path.exists():
            continue
        try:
            with open(_log_path, errors="replace") as _lf:
                for _line in _lf:
                    if any(_mk in _line for _mk in NEW_CODE_MARKERS):
                        _m = _re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', _line)
                        if _m:
                            new_code_start = _m.group(1)
                            break
            if new_code_start:
                break
        except Exception:
            pass

    j = _get_journal()
    closed = j.get_closed_trades() if j else []

    old_stats: dict = _dd(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    new_stats: dict = _dd(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})

    for t in closed:
        pnl = float(t.get("pnl") or 0.0)
        q   = t.get("market_question", "")
        strat = t.get("strategy", "unknown")
        mid = str(t.get("market_id", "") or "")
        ts  = (t.get("closed_at") or t.get("timestamp") or "")[:19]
        cat = _classify_updown_trade(q, strat, mid)
        is_new = new_code_start is not None and ts >= new_code_start
        bucket = new_stats if is_new else old_stats
        if pnl > 0.01:
            bucket[cat]["wins"] += 1
        elif pnl < -0.01:
            bucket[cat]["losses"] += 1
        bucket[cat]["pnl"] += pnl

    def _fmt(d):
        out = {}
        for cat, v in d.items():
            n = v["wins"] + v["losses"]
            out[cat] = {
                "wins": v["wins"], "losses": v["losses"],
                "trades": n,
                "win_rate": round(v["wins"] / n, 4) if n > 0 else 0.0,
                "pnl": round(v["pnl"], 2),
            }
        return out

    return {
        "new_code_start": new_code_start,
        "old_code": _fmt(old_stats),
        "new_code": _fmt(new_stats),
    }


@app.get("/api/strategy/reason-buckets")
async def get_strategy_reason_buckets(limit: int = 4000, watchlist_limit: int = 160):
    """Summarize recent crypto entry reasons and current watchlist blockers."""
    limit = max(200, min(limit, 20000))
    watchlist_limit = max(40, min(watchlist_limit, 300))

    out: Dict[str, Dict[str, Any]] = {
        "bitcoin": {
            "entries": 0,
            "actions": {"BUY_YES": 0, "SELL_YES": 0},
            "path": {"updown_15m": 0, "updown_5m": 0, "threshold": 0, "other": 0},
            "bias": {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0, "other": 0},
            "exposure": {"full": 0, "moderate": 0, "minimal": 0, "paused": 0, "other": 0},
            "blockers": {},
        },
        "sol_macro": {
            "entries": 0,
            "actions": {"BUY_YES": 0, "SELL_YES": 0},
            "path": {"updown_15m": 0, "updown_5m": 0, "threshold": 0, "other": 0},
            "bias": {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0, "other": 0},
            "exposure": {"full": 0, "moderate": 0, "minimal": 0, "paused": 0, "other": 0},
            "blockers": {},
        },
        "eth_macro": {
            "entries": 0,
            "actions": {"BUY_YES": 0, "SELL_YES": 0},
            "path": {"updown_15m": 0, "updown_5m": 0, "threshold": 0, "other": 0},
            "bias": {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0, "other": 0},
            "exposure": {"full": 0, "moderate": 0, "minimal": 0, "paused": 0, "other": 0},
            "blockers": {},
        },
        "hype_macro": {
            "entries": 0,
            "actions": {"BUY_YES": 0, "SELL_YES": 0},
            "path": {"updown_15m": 0, "updown_5m": 0, "threshold": 0, "other": 0},
            "bias": {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0, "other": 0},
            "exposure": {"full": 0, "moderate": 0, "minimal": 0, "paused": 0, "other": 0},
            "blockers": {},
        },
        "xrp_macro": {
            "entries": 0,
            "actions": {"BUY_YES": 0, "BUY_NO": 0, "SELL_YES": 0},
            "path": {"updown_15m": 0, "updown_5m": 0, "threshold": 0, "other": 0},
            "bias": {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0, "other": 0},
            "exposure": {"full": 0, "moderate": 0, "minimal": 0, "paused": 0, "other": 0},
            "blockers": {},
        },
        "weather": {
            "entries": 0,
            "actions": {"BUY_YES": 0, "BUY_NO": 0, "SELL_YES": 0},
            "path": {"updown_15m": 0, "updown_5m": 0, "threshold": 0, "other": 0},
            "bias": {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0, "other": 0},
            "exposure": {"full": 0, "moderate": 0, "minimal": 0, "paused": 0, "other": 0},
            "blockers": {},
        },
    }

    # 1) Recent ENTRY reasons from journal
    j = _get_journal()
    entries = j.get_all_entries(limit) if j else []
    for e in entries:
        if e.get("event") != "ENTRY":
            continue
        strat = e.get("strategy")
        if strat not in out:
            continue

        bucket = out[strat]
        bucket["entries"] += 1

        action = (e.get("action") or "").upper()
        if action in bucket["actions"]:
            bucket["actions"][action] += 1

        reason = str(e.get("reason") or "")
        r_low = reason.lower()

        if "updown_5m" in r_low:
            bucket["path"]["updown_5m"] += 1
        elif "updown_15m" in r_low:
            bucket["path"]["updown_15m"] += 1
        elif "target=$" in r_low:
            bucket["path"]["threshold"] += 1
        else:
            bucket["path"]["other"] += 1

        m_bias = re.search(r"(HTF|MACRO)=([A-Z_]+)", reason)
        if m_bias:
            b = m_bias.group(2)
            if b in bucket["bias"]:
                bucket["bias"][b] += 1
            else:
                bucket["bias"]["other"] += 1
        else:
            bucket["bias"]["other"] += 1

        m_exp = re.search(r"exp=([a-z_]+)\(", r_low)
        if m_exp:
            tier = m_exp.group(1)
            if tier in bucket["exposure"]:
                bucket["exposure"][tier] += 1
            else:
                bucket["exposure"]["other"] += 1
        else:
            bucket["exposure"]["other"] += 1

    # 2) Current blocker buckets from watchlist (display-only "why not ready")
    try:
        wl = await get_strategy_watchlist(
            limit=watchlist_limit, include_general_markets=False
        )
        for row in wl.get("watchlist", []):
            strat = row.get("strategy")
            if strat not in out:
                continue
            if row.get("ready"):
                continue
            reason = row.get("block_reason") or "unknown"
            blockers = out[strat]["blockers"]
            blockers[reason] = int(blockers.get(reason, 0)) + 1
    except Exception as e:
        logger.warning(f"reason-buckets watchlist unavailable: {e}")

    return {"reason_buckets": out, "updated_at": datetime.utcnow().isoformat()}


@app.get("/api/journal/entries")
async def get_journal_entries(limit: int = 100, session_id: Optional[str] = None):
    limit = max(1, min(int(limit), 500))
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"entries": j.get_all_entries(limit) if j else []}


@app.get("/api/journal/snapshots")
async def get_journal_snapshots(limit: int = 500, session_id: Optional[str] = None):
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"snapshots": j.get_snapshots(limit) if j else []}


@app.get("/api/journal/sessions")
async def get_journal_sessions():
    from src.execution.trade_journal import TradeJournal
    return {"sessions": TradeJournal.list_sessions()}


@app.get("/api/journal/session/{session_id}")
async def get_session_detail(session_id: str):
    """Load full stats for a specific session by ID (active or archived)."""
    from src.execution.trade_journal import TradeJournal, JOURNAL_DIR
    from pathlib import Path
    ARCHIVE_DIR = JOURNAL_DIR.parent / "paper_trades_archive"
    session_dir = None
    for base in [JOURNAL_DIR, ARCHIVE_DIR]:
        candidate = base / session_id
        if candidate.exists():
            session_dir = candidate
            break
    if not session_dir:
        return {"error": f"Session {session_id} not found"}
    try:
        j = TradeJournal(session_id=session_id)
        summary = j.get_summary()
        summary["_source"] = "active" if session_dir.parent == JOURNAL_DIR else "archived"
        return summary
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/journal/settle-archived")
async def settle_archived_positions(request: Request):
    """Run settle script for archived sessions (e.g. ~70 pending from pre-reset batch)."""
    _check_auth(request)
    script = PROJECT_ROOT / "scripts" / "settle_archived_positions.py"
    if not script.exists():
        return {"settled": 0, "message": "settle script not found"}
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=_safe_env(),
        )
        out = (result.stdout or "") + (result.stderr or "")
        settled = 0
        if "Total settled:" in out:
            for line in out.splitlines():
                if line.strip().startswith("Total settled:"):
                    try:
                        settled = int(line.split(":")[-1].strip())
                    except ValueError:
                        pass
                    break
        return {"settled": settled, "message": "Done", "output": out[-500:]}
    except subprocess.TimeoutExpired:
        return {"settled": -1, "message": "Timed out after 120s"}
    except Exception as e:
        logger.error(f"Settle archived error: {e}", exc_info=True)
        return {"settled": -1, "message": str(e)}


# ─── AI SESSION SUMMARY ──────────────────────────────────────────


@app.get("/api/journal/ai-summary")
async def get_session_ai_summary(session_id: Optional[str] = None):
    """Generate AI natural-language summary of the most recent session. Cached per session_id."""
    import httpx

    journal = _journal_for_query(session_id) if session_id else _get_journal()
    if not journal:
        return {"summary": "No session data available yet.", "session_id": None}

    try:
        summary_data = journal.get_summary()
        session_id = str(getattr(journal, "session_id", "unknown"))
    except Exception:
        return {"summary": "Could not read session data.", "session_id": None}

    # Return cached result if session hasn't changed
    if session_id in _ai_summary_cache:
        return {"summary": _ai_summary_cache[session_id], "session_id": session_id, "cached": True}

    trades = summary_data.get("total_trades", 0) or summary_data.get("total_entries", 0) or 0
    wins = summary_data.get("wins", 0)
    losses = summary_data.get("losses", 0)
    wr = summary_data.get("win_rate", 0) or 0
    realized_pnl = summary_data.get("realized_pnl", 0) or 0

    if trades == 0:
        summary = "No trades have been completed in this session yet."
        _ai_summary_cache[session_id] = summary
        return {"summary": summary, "session_id": session_id}

    trades_word = "trade" if trades == 1 else "trades"
    prompt = (
        f"You are a trading coach. Summarize this paper trading session in 3-4 concise sentences. "
        f"Be direct and analytical. Session stats: {trades} {trades_word}, {wins} wins / {losses} losses, "
        f"{wr:.1%} win rate, ${realized_pnl:+.2f} realized PnL. "
        f"Focus on what the numbers mean for strategy performance and what to watch."
    )

    minimax_key = os.getenv("MINIMAX_API_KEY", "")
    summary = None

    if minimax_key:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.minimax.io/anthropic/v1/messages",
                    headers={
                        "x-api-key": minimax_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "MiniMax-M2.7",
                        "max_tokens": 200,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                if resp.status_code == 200:
                    summary = resp.json()["content"][0]["text"].strip()
        except Exception as e:
            logger.warning(f"AI summary minimax failed: {e}")

    if not summary:
        summary = (
            f"Session completed {trades} {trades_word} with a {wr:.1%} win rate "
            f"({wins}W/{losses}L) and ${realized_pnl:+.2f} realized PnL. "
            f"{'Above break-even — strategy is generating edge.' if wr > 0.55 else 'Below 55% target — monitor for regime issues.'}"
        )

    _ai_summary_cache[session_id] = summary
    return {"summary": summary, "session_id": session_id, "cached": False}


# ─── EXPOSURE MANAGER ────────────────────────────────────────────


def _all_exposure_managers():
    """Return all active exposure managers."""
    bot = _full_bot_instance()
    if not bot:
        return []
    managers = []
    for attr in (
        "btc_exposure_manager",
        "sol_exposure_manager",
        "eth_exposure_manager",
        "xrp_exposure_manager",
        "event_exposure_manager",
    ):
        mgr = getattr(bot, attr, None)
        if mgr:
            managers.append(mgr)
    return managers


EXPOSURE_LANE_TO_ATTR = {
    "btc": "btc_exposure_manager",
    "sol": "sol_exposure_manager",
    "eth": "eth_exposure_manager",
    "xrp": "xrp_exposure_manager",
    "event": "event_exposure_manager",
}


def _exposure_manager_for_lane(lane: str):
    """Resolve a dashboard lane key (btc, sol, …) to an ExposureManager or None."""
    bot = _full_bot_instance()
    if not bot or not lane:
        return None
    key = lane.lower().strip()
    attr = EXPOSURE_LANE_TO_ATTR.get(key)
    if not attr:
        return None
    return getattr(bot, attr, None)


@app.get("/api/exposure")
async def get_exposure_status():
    """Per-strategy exposure tiers. Uses stable keys (btc, sol, …) so the UI
 always labels ETH/XRP correctly; also emits manager_0..N for compatibility."""
    bot = _full_bot_instance()
    if not bot:
        return {"error": "No bot instance"}
    key_attrs = (
        ("btc", "btc_exposure_manager"),
        ("sol", "sol_exposure_manager"),
        ("eth", "eth_exposure_manager"),
        ("xrp", "xrp_exposure_manager"),
        ("event", "event_exposure_manager"),
    )
    out: Dict[str, Any] = {}
    idx = 0
    for key, attr in key_attrs:
        mgr = getattr(bot, attr, None)
        if mgr is None:
            continue
        st = mgr.get_status()
        st["key"] = key
        out[key] = st
        out[f"manager_{idx}"] = st
        idx += 1
    return out


@app.post("/api/exposure/pause")
async def pause_exposure(request: Request):
    _check_auth(request)
    managers = _all_exposure_managers()
    if managers:
        for m in managers:
            m.manual_pause()
        return {"status": "paused", "managers": len(managers)}
    return {"error": "No bot instance"}


@app.post("/api/exposure/resume")
async def resume_exposure(request: Request):
    _check_auth(request)
    managers = _all_exposure_managers()
    if managers:
        for m in managers:
            m.manual_resume()
        return {"status": "resumed", "managers": len(managers)}
    return {"error": "No bot instance"}


@app.post("/api/exposure/pause/{lane}")
async def pause_exposure_lane(lane: str, request: Request):
    """Pause a single exposure lane (manual) — other lanes keep trading."""
    _check_auth(request)
    mgr = _exposure_manager_for_lane(lane)
    if mgr is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown lane or bot not running. Use btc, sol, eth, xrp, or event.",
        )
    mgr.manual_pause()
    return {"status": "paused", "lane": lane.lower().strip()}


@app.post("/api/exposure/resume/{lane}")
async def resume_exposure_lane(lane: str, request: Request):
    """Resume one lane after manual or loss pause (clears manual pause for that lane)."""
    _check_auth(request)
    mgr = _exposure_manager_for_lane(lane)
    if mgr is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown lane or bot not running. Use btc, sol, eth, xrp, or event.",
        )
    mgr.manual_resume()
    return {"status": "resumed", "lane": lane.lower().strip()}


# ─── BITCOIN LIVE ANALYSIS ────────────────────────────────────


@app.get("/api/bitcoin/analysis")
async def get_bitcoin_analysis():
    """Return live BTC technical analysis for the dashboard."""
    try:
        # Always serve from the background cache — never block the event loop.
        # When the bot is running, its btc_service feeds the same singleton cache
        # so data is always fresh.  Trigger a background refresh if stale.
        _maybe_trigger_refresh()
        ta = _btc_analysis_cache

        if ta:
            # Compute HTF bias the same way the strategy does
            sabre = ta.trend_sabre
            macd_4h = ta.macd_4h
            price = ta.current_price
            bull, bear = 0, 0
            if sabre.trend == 1:
                bull += 1
            elif sabre.trend == -1:
                bear += 1
            if price > sabre.ma_value:
                bull += 1
            elif price < sabre.ma_value:
                bear += 1
            if macd_4h.above_zero:
                bull += 1
            else:
                bear += 1
            htf_bias = (
                "BULLISH" if bull >= 2 else "BEARISH" if bear >= 2 else "NEUTRAL"
            )
            mom = ta.candle_momentum
            # Helper: cast numpy scalars → native Python types so FastAPI can serialize them
            def _f(v):
                try:
                    return float(v) if v is not None else None
                except (TypeError, ValueError):
                    return None

            def _fl(lst):
                try:
                    return [float(x) for x in (lst or [])]
                except (TypeError, ValueError):
                    return []

            def _b(v):
                return bool(v) if v is not None else None

            return {
                "price": _f(price),
                "htf_bias": htf_bias,
                "rsi": _f(ta.rsi_14),
                "ema_9": _f(ta.ema_9),
                "ema_21": _f(ta.ema_21),
                "ema_50": _f(ta.ema_50),
                "ema_200": _f(ta.ema_200),
                "sabre_trend": int(sabre.trend) if sabre.trend is not None else None,
                "sabre_ma": _f(sabre.ma_value),
                "sabre_trail": _f(sabre.trail_value),
                "sabre_bull_signal": _b(sabre.bull_signal),
                "sabre_bear_signal": _b(sabre.bear_signal),
                "tension": _f(sabre.tension),
                "atr": _f(sabre.atr),
                "snap_supports": _fl(sabre.snap_supports[:3]),
                "snap_resistances": _fl(sabre.snap_resistances[:3]),
                "macd_4h_hist": _f(macd_4h.histogram),
                "macd_4h_hist_rising": _b(macd_4h.histogram_rising),
                "macd_4h_cross": _b(macd_4h.crossover),
                "macd_4h_above_zero": _b(macd_4h.above_zero),
                "macd_15m_hist": _f(ta.macd_15m.histogram),
                "macd_15m_hist_rising": _b(ta.macd_15m.histogram_rising),
                "macd_15m_cross": _b(ta.macd_15m.crossover),
                "macd_15m_above_zero": _b(ta.macd_15m.above_zero),
                "mom_15m": mom.m15_direction,
                "mom_15m_pct": _f(mom.m15_move_pct),
                "mom_15m_age": _f(mom.m15_candle_age_minutes),
                "mom_15m_in_window": _b(mom.m15_in_prediction_window),
                "mom_5m": mom.m5_direction,
                "mom_5m_age": _f(mom.m5_candle_age_minutes),
                "mom_5m_in_window": _b(mom.m5_in_prediction_window),
                "momentum_signal": mom.momentum_signal,
                "momentum_strength": _f(mom.momentum_strength),
                "vp_poc": _f(ta.volume_profile.poc_price),
                "vp_vah": _f(ta.volume_profile.vah_price),
                "vp_val": _f(ta.volume_profile.val_price),
                "nearest_support": _f(ta.nearest_support),
                "nearest_resistance": _f(ta.nearest_resistance),
                "support_levels": _fl(ta.support_levels[:5]),
                "resistance_levels": _fl(ta.resistance_levels[:5]),
                "daily_trend": ta.daily_trend,
                "h4_trend": ta.h4_trend,
                "h1_trend": ta.h1_trend,
                "chainlink_price": _f(ta.chainlink_price),
            }
        return {"error": "BTC analysis not available"}
    except Exception as e:
        logger.error(f"BTC analysis endpoint error: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/bitcoin/candles")
async def get_bitcoin_candles(interval: str = "15m", limit: int = 60):
    """Return recent BTC/USDT candles for the live chart (from Binance)."""
    try:
        # Clamp to safe values
        limit = max(10, min(200, limit))
        if interval not in ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"):
            interval = "15m"

        svc = None
        if bot_instance and hasattr(bot_instance, "bitcoin_strategy"):
            svc = bot_instance.bitcoin_strategy.btc_service
        else:
            svc = _get_btc_svc()

        df = await asyncio.to_thread(svc.fetch_klines, interval=interval, limit=limit)
        if df.empty:
            return {"candles": [], "error": "No data from Binance"}

        def _row_ok(o: float, h: float, l: float, c: float) -> bool:
            """Drop pathological bars that break chart autoscale (bad merge / corrupt tick)."""
            if not (o > 0 and h > 0 and l > 0 and c > 0 and h >= l):
                return False
            if l < 500 or h > 2_000_000:
                return False
            if h / l > 1.35:
                return False
            return True

        candles = []
        for _, row in df.iterrows():
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            if not _row_ok(o, h, l, c):
                continue
            ts = int(row["open_time"].timestamp())
            candles.append({
                "time": ts,
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c, 2),
                "volume": round(float(row["volume"]), 4),
            })

        if len(candles) < 5:
            candles = []
            for _, row in df.iterrows():
                ts = int(row["open_time"].timestamp())
                candles.append({
                    "time": ts,
                    "open": round(float(row["open"]), 2),
                    "high": round(float(row["high"]), 2),
                    "low": round(float(row["low"]), 2),
                    "close": round(float(row["close"]), 2),
                    "volume": round(float(row["volume"]), 4),
                })

        return {"candles": candles, "interval": interval, "count": len(candles)}
    except Exception as e:
        logger.error(f"BTC candles endpoint error: {e}", exc_info=True)
        return {"candles": [], "error": str(e)}


# ─── SOL / ETH / XRP LIVE ANALYSIS (alt vs BTC) ───────────────


def _solbtc_analysis_payload(ta, alt_symbol: str = "SOLUSDT") -> Dict[str, Any]:
    """Shared JSON shape for SOL / ETH / HYPE / XRP alt-vs-BTC dashboards."""
    sol = ta.sol
    corr = ta.correlation
    mtt = ta.multi_tf
    bull, bear = 0, 0
    if mtt.h1_trend == "BULLISH":
        bull += 1
    elif mtt.h1_trend == "BEARISH":
        bear += 1
    if sol.ema_9 > sol.ema_21 > sol.ema_50:
        bull += 1
    elif sol.ema_9 < sol.ema_21 < sol.ema_50:
        bear += 1
    if sol.rsi_14 > 55:
        bull += 1
    elif sol.rsi_14 < 45:
        bear += 1
    macro = "BULLISH" if bull >= 2 else "BEARISH" if bear >= 2 else "NEUTRAL"
    raw = (alt_symbol or "SOLUSDT").upper()
    alt_code = raw.replace("USDT", "").replace("USD", "").strip().lower() or "sol"
    spot = float(sol.current_price)
    out: Dict[str, Any] = {
        "spot_price": spot,
        "alt_asset_code": alt_code,
        f"{alt_code}_price": spot,
        "btc_price": corr.btc_price,
        "macro_trend": macro,
        "h1_trend": mtt.h1_trend,
        "m15_trend": mtt.m15_trend,
        "m5_trend": mtt.m5_trend,
        "aligned": mtt.aligned,
        "rsi": sol.rsi_14,
        "ema_9": sol.ema_9,
        "ema_21": sol.ema_21,
        "ema_50": sol.ema_50,
        "macd_15m_hist": sol.macd_15m.histogram,
        "macd_15m_hist_rising": sol.macd_15m.histogram_rising,
        "macd_15m_cross": sol.macd_15m.crossover,
        "macd_5m_hist": sol.macd_5m.histogram,
        "macd_5m_hist_rising": sol.macd_5m.histogram_rising,
        "macd_5m_cross": sol.macd_5m.crossover,
        "h1_macd_hist": sol.macd_1h.histogram,
        "h1_macd_hist_rising": sol.macd_1h.histogram_rising,
        "atr_14": sol.atr_14,
        "correlation_1h": corr.correlation_1h,
        "btc_move_5m": corr.btc_move_5m_pct,
        "btc_move_15m": corr.btc_move_15m_pct,
        "sol_move_5m": corr.sol_move_5m_pct,
        "sol_move_15m": corr.sol_move_15m_pct,
        "btc_spike": corr.btc_spike_detected,
        "btc_spike_dir": corr.btc_spike_direction,
        "lag_opportunity": corr.lag_opportunity,
        "lag_direction": corr.opportunity_direction,
        "lag_magnitude": corr.opportunity_magnitude,
        "chainlink_btc": corr.btc_chainlink_price,
        "chainlink_alt": sol.chainlink_price,
        "chainlink_alt_network": sol.chainlink_network,
        "oracle_basis_bps": sol.oracle_basis_bps,
    }
    # Legacy: dashboard/scripts that still read sol_price for the SOL leg only
    if alt_code == "sol":
        out["sol_price"] = spot
    return out


def _run_alt_analysis_sync(alt_symbol: str, bot_attr: Optional[str]):
    """Pure-sync helper for /api/{alt}/analysis — runs in a worker thread."""
    from src.analysis.sol_btc_service import SOLBTCService

    svc = None
    if bot_attr and bot_instance and hasattr(bot_instance, bot_attr):
        svc = getattr(getattr(bot_instance, bot_attr), "sol_service", None)
    if svc is None:
        svc = SOLBTCService(alt_symbol=alt_symbol)
    ta = svc.get_full_analysis()
    alt_sym = getattr(svc, "alt_symbol", alt_symbol) or alt_symbol
    return ta, alt_sym


@app.get("/api/sol/analysis")
async def get_sol_analysis():
    """Return live SOL-BTC correlation analysis for the dashboard."""
    try:
        ta, alt_sym = await asyncio.to_thread(_run_alt_analysis_sync, "SOLUSDT", "sol_macro_strategy")
        if ta:
            return _solbtc_analysis_payload(ta, alt_sym)
        return {"error": "SOL analysis not available"}
    except Exception as e:
        logger.error(f"SOL analysis endpoint error: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/eth/analysis")
async def get_eth_analysis():
    """Live ETH–BTC correlation (same machinery as SOL lag)."""
    try:
        ta, alt_sym = await asyncio.to_thread(_run_alt_analysis_sync, "ETHUSDT", "eth_macro_strategy")
        if ta:
            return _solbtc_analysis_payload(ta, alt_sym)
        return {"error": "ETH analysis not available"}
    except Exception as e:
        logger.error(f"ETH analysis endpoint error: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/hype/analysis")
async def get_hype_analysis():
    """Live HYPE–BTC correlation for dashboard using HyperliquidHypeService."""

    def _hype_sync():
        from src.analysis.hyperliquid_hype_service import HyperliquidHypeService

        svc = None
        if bot_instance and hasattr(bot_instance, "hype_macro_strategy"):
            svc = getattr(bot_instance.hype_macro_strategy, "sol_service", None)
        if svc is None:
            svc = HyperliquidHypeService()
        ta = svc.get_full_analysis()
        alt_sym = getattr(svc, "alt_symbol", "HYPEUSDT") or "HYPEUSDT"
        return ta, alt_sym

    try:
        ta, alt_sym = await asyncio.to_thread(_hype_sync)
        if ta:
            return _solbtc_analysis_payload(ta, alt_sym)
        return {"error": "HYPE analysis not available"}
    except Exception as e:
        logger.error(f"HYPE analysis endpoint error: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/xrp/analysis")
async def get_xrp_analysis():
    """Live XRP–BTC correlation for dashboard (independent of dump-hedge leg logic)."""
    try:
        ta, alt_sym = await asyncio.to_thread(_run_alt_analysis_sync, "XRPUSDT", "xrp_macro_strategy")
        if ta:
            return _solbtc_analysis_payload(ta, alt_sym)
        return {"error": "XRP analysis not available"}
    except Exception as e:
        logger.error(f"XRP analysis endpoint error: {e}", exc_info=True)
        return {"error": str(e)}


# ─── CONFIG PANEL ─────────────────────────────────────────────────


@app.get("/api/config")
async def get_config():
    """Return current settings.yaml as JSON."""
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="settings.yaml not found")
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ConfigUpdates(BaseModel):
    """Validated partial settings.yaml patch for dashboard operator controls."""

    model_config = ConfigDict(extra="forbid")

    ai: Optional[Dict[str, Any]] = None
    strategies: Optional[Dict[str, Any]] = None
    trading: Optional[Dict[str, Any]] = None
    exposure: Optional[Dict[str, Any]] = None
    backtest: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_config_patch(self) -> "ConfigUpdates":
        if self.ai is not None:
            _validate_section_keys(self.ai, "ai", {"enabled", "live_inferencing"})
            _validate_bool_fields(self.ai, "ai", {"enabled", "live_inferencing"})

        if self.trading is not None:
            _validate_section_keys(
                self.trading,
                "trading",
                {
                    "default_position_size",
                    "max_position_size",
                    "max_days_to_resolution",
                    "min_hours_to_resolution",
                    "kelly_fraction",
                    "daily_loss_limit",
                    "max_exposure_per_trade",
                    "dry_run",
                },
            )
            if self.trading.get("dry_run") is False:
                raise ValueError("trading.dry_run cannot be disabled via dashboard config")
            _validate_numeric_range(self.trading, "trading", "default_position_size", gt=0)
            _validate_numeric_range(self.trading, "trading", "max_position_size", gt=0)
            _validate_numeric_range(self.trading, "trading", "max_days_to_resolution", gt=0, le=365)
            _validate_numeric_range(self.trading, "trading", "min_hours_to_resolution", ge=0, le=8760)
            _validate_numeric_range(self.trading, "trading", "kelly_fraction", ge=0, le=1)
            _validate_numeric_range(self.trading, "trading", "daily_loss_limit", ge=0, le=1)
            _validate_numeric_range(self.trading, "trading", "max_exposure_per_trade", ge=0, le=1)

        if self.backtest is not None:
            _validate_section_keys(
                self.backtest,
                "backtest",
                {"initial_bankroll", "take_profit_pct", "stop_loss_pct", "max_hold_hours"},
            )
            _validate_numeric_range(self.backtest, "backtest", "initial_bankroll", gt=0)
            _validate_numeric_range(self.backtest, "backtest", "take_profit_pct", ge=0, le=1)
            _validate_numeric_range(self.backtest, "backtest", "stop_loss_pct", ge=0, le=1)
            _validate_numeric_range(self.backtest, "backtest", "max_hold_hours", gt=0)

        if self.exposure is not None:
            _validate_section_keys(
                self.exposure,
                "exposure",
                {
                    "full_size",
                    "moderate_size",
                    "minimal_size",
                    "max_consecutive_losses",
                    "pause_cycles",
                    "loss_kill_switch_enabled",
                },
            )
            _validate_numeric_range(self.exposure, "exposure", "full_size", ge=0)
            _validate_numeric_range(self.exposure, "exposure", "moderate_size", ge=0)
            _validate_numeric_range(self.exposure, "exposure", "minimal_size", ge=0)
            _validate_numeric_range(self.exposure, "exposure", "max_consecutive_losses", ge=1)
            _validate_numeric_range(self.exposure, "exposure", "pause_cycles", ge=0)
            _validate_bool_fields(self.exposure, "exposure", {"loss_kill_switch_enabled"})

        if self.strategies is not None:
            allowed_strategies = {
                "bitcoin",
                "sol_macro",
                "eth_macro",
                "hype_macro",
                "xrp_macro",
                "weather",
            }
            _validate_section_keys(self.strategies, "strategies", allowed_strategies)
            allowed_strategy_fields = {
                "enabled",
                "use_ai",
                "dead_zone_enabled",
                "resolution_window_enabled",
                "min_edge",
                "entry_price_min",
                "entry_price_max",
                "kelly_fraction",
                "ai_confidence_threshold",
            }
            bool_fields = {"enabled", "use_ai", "dead_zone_enabled", "resolution_window_enabled"}
            unit_fields = {
                "min_edge",
                "entry_price_min",
                "entry_price_max",
                "kelly_fraction",
                "ai_confidence_threshold",
            }
            for name, patch in self.strategies.items():
                if not isinstance(patch, dict):
                    raise ValueError(f"strategies.{name} must be an object")
                section = f"strategies.{name}"
                _validate_section_keys(patch, section, allowed_strategy_fields)
                _validate_bool_fields(patch, section, bool_fields)
                for field_name in unit_fields:
                    _validate_numeric_range(patch, section, field_name, ge=0, le=1)
                min_price = patch.get("entry_price_min")
                max_price = patch.get("entry_price_max")
                if min_price is not None and max_price is not None and min_price > max_price:
                    raise ValueError(f"{section}.entry_price_min must be <= entry_price_max")

        return self


def _validate_section_keys(section: Dict[str, Any], section_name: str, allowed: set[str]) -> None:
    if not isinstance(section, dict):
        raise ValueError(f"{section_name} must be an object")
    unknown = set(section) - allowed
    if unknown:
        raise ValueError(f"Unknown config key(s) in {section_name}: {sorted(unknown)}")


def _validate_bool_fields(section: Dict[str, Any], section_name: str, fields: set[str]) -> None:
    for key in fields:
        if key in section and not isinstance(section[key], bool):
            raise ValueError(f"{section_name}.{key} must be a boolean")


def _validate_numeric_range(
    section: Dict[str, Any],
    section_name: str,
    key: str,
    *,
    ge: Optional[float] = None,
    gt: Optional[float] = None,
    le: Optional[float] = None,
) -> None:
    if key not in section or section[key] is None:
        return
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{section_name}.{key} must be numeric")
    if gt is not None and not value > gt:
        raise ValueError(f"{section_name}.{key} must be > {gt}")
    if ge is not None and not value >= ge:
        raise ValueError(f"{section_name}.{key} must be >= {ge}")
    if le is not None and not value <= le:
        raise ValueError(f"{section_name}.{key} must be <= {le}")


@app.post("/api/config")
async def update_config(request: Request):
    """Merge partial updates into settings.yaml and save."""
    _check_auth(request)
    try:
        updates = ConfigUpdates.model_validate(await request.json())
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json()))
    updates_dict = updates.model_dump(exclude_none=True)
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="settings.yaml not found")
    try:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f)
        _deep_merge(config, updates_dict)
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        live_apply_ok = True
        live_apply_error = None
        bot = _full_bot_instance()
        if bot is not None:
            try:
                bot.apply_config_updates(updates_dict)
            except Exception as e:
                logger.error("Live config apply failed: %s", e, exc_info=True)
                live_apply_ok = False
                live_apply_error = str(e)
        msg = "Configuration updated successfully."
        if not live_apply_ok:
            msg += " Saved to disk; running bot could not apply changes (restart may be needed)."
        return {
            "status": "saved",
            "message": msg,
            "live_apply": live_apply_ok,
            **({"live_apply_error": live_apply_error} if live_apply_error else {}),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Config save error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ─── TRADE PANEL ──────────────────────────────────────────────────


class TradeRequest(BaseModel):
    market_id: str = Field(..., description="Market ID")
    side: str = Field(..., description="buy or sell")
    size: float = Field(..., gt=0)
    price: float = Field(..., ge=0.01, le=0.99)


@app.post("/api/trade")
async def execute_trade(trade: TradeRequest, request: Request):
    _check_auth(request)
    bot = _full_bot_instance()
    if not bot or not bot.clob_client:
        raise HTTPException(status_code=503, detail="Trading client is not available.")
    try:
        # Fetch markets and find the one matching market_id
        markets = await bot.market_scanner.fetch_markets(limit=200)
        market = next((m for m in markets if m.id == trade.market_id), None)
        if not market:
            raise HTTPException(
                status_code=404, detail=f"Market {trade.market_id} not found."
            )
        # Use token_id_yes for BUY, token_id_no for SELL
        if trade.side.upper() == "BUY":
            token_id = market.token_id_yes
        else:
            token_id = market.token_id_no
        order = await bot.clob_client.place_order(
            token_id=token_id,
            side=trade.side.upper(),
            price=trade.price,
            size=trade.size,
            market_id=trade.market_id,
            post_only=True,
            dry_run=bot.config.get("trading", {}).get("dry_run", True),
        )
        if order and hasattr(order, "order_id"):
            return {"message": "Trade submitted!", "order_id": order.order_id}
        return {"message": "Trade processed (dry run?)."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Trade error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/paper/reset")
async def reset_paper_session(request: Request):
    """Start a fresh paper trading session at initial_bankroll (dashboard one-click).

    Creates a new session folder, archives every *other* session under
    ``data/paper_trades/`` so the next process restart still resumes this run,
    resets bankroll / risk / exposure state, and clears dashboard journal cache.
    """
    global _journal_cache, _ai_summary_cache

    _check_auth(request)
    bot = _full_bot_instance()
    if not bot:
        raise HTTPException(status_code=503, detail="Bot instance not available.")
    if not bot.config.get("trading", {}).get("dry_run", True):
        raise HTTPException(
            status_code=400,
            detail="Paper reset is only allowed in dry_run (paper) mode.",
        )

    from src.execution.trade_journal import TradeJournal, JOURNAL_DIR

    new_id = datetime.now().strftime("reset_%Y%m%d_%H%M%S")
    new_bankroll = float(
        bot.config.get("backtest", {}).get("initial_bankroll", 500.0)
    )
    archive_rel: Optional[str] = None

    async with bot._execution_lock:
        # New session dir first; then archive older folders (never move the active dir).
        bot.journal = TradeJournal(session_id=new_id, resume_latest=False)
        bot.bankroll = new_bankroll
        bot.risk_manager.bankroll = new_bankroll
        bot.risk_manager.active_positions.clear()
        bot.risk_manager.daily_pnl = 0.0
        bot.risk_manager.daily_trades = 0
        for mgr in _all_exposure_managers():
            mgr.reset_for_new_paper_session()

        ARCHIVE_BASE = JOURNAL_DIR.parent / "paper_trades_archive"
        ARCHIVE_BASE.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_dir = ARCHIVE_BASE / f"ui_reset_{ts}"
        moved_any = False
        if JOURNAL_DIR.exists():
            for d in list(JOURNAL_DIR.iterdir()):
                if not d.is_dir() or d.name == new_id:
                    continue
                archive_dir.mkdir(parents=True, exist_ok=True)
                dest = archive_dir / d.name
                shutil.move(str(d), str(dest))
                moved_any = True
        if moved_any:
            try:
                archive_rel = str(archive_dir.relative_to(PROJECT_ROOT))
            except ValueError:
                archive_rel = str(archive_dir)

        # Seed chart + summary for an empty session
        try:
            bot.journal.take_snapshot(new_bankroll)
        except Exception as e:
            logger.warning("Paper reset: initial snapshot failed: %s", e)

        _journal_cache["journal"] = None
        _journal_cache["path"] = None
        _journal_cache["mtime"] = None
        _ai_summary_cache.clear()

    logging.info(
        f"[dashboard] Paper session reset → session_id={new_id}, bankroll=${new_bankroll:,.2f}"
    )
    out: Dict[str, Any] = {
        "status": "ok",
        "new_session_id": new_id,
        "bankroll": new_bankroll,
    }
    auto_backtests = _maybe_start_auto_backtests("reset")
    if auto_backtests:
        out["auto_backtests"] = auto_backtests
        out["auto_backtest"] = auto_backtests[0]
    if archive_rel:
        out["archived_to"] = archive_rel
    return out
