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
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
import json
import logging
import re
import shutil
import signal
import subprocess
import sys
import threading
import yaml
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo
import uuid

from src.analysis.usage_tracker import usage_tracker
from src.analysis.btc_price_service import BTCPriceService as _BTCPriceService
from src.analysis.lane_manager import LaneManager
from src.analysis.lane_thresholds import load_lane_thresholds
from src.config_merge import deep_merge_config as _deep_merge
from src.env_bootstrap import load_project_dotenv, project_root_from_here
from src.ai_status import compute_ai_status
from src.execution.performance_feedback import public_feedback_status

# Standalone `uvicorn src.dashboard.server:app` still picks up repo-root `.env` / secrets.env.
load_project_dotenv(project_root_from_here(), quiet=True)

bot_instance: Optional["PolyBot"] = None

# Uvicorn server started from main.py before PolyBot finishes heavy init (PaaS health checks).
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


def _calibration_status_from_config(cfg: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    cal_cfg = dict((cfg or {}).get("lane_calibration") or {})
    enabled = bool(cal_cfg.get("enabled", False))
    default_shadow = bool(cal_cfg.get("shadow_mode", True))
    mode_shadow = bool(
        cal_cfg.get("paper_shadow_mode", default_shadow)
        if dry_run
        else cal_cfg.get("live_shadow_mode", default_shadow)
    )
    plt_cfg = dict(cal_cfg.get("per_lane_thresholds") or {})
    return {
        "enabled": enabled,
        "active": bool(enabled and not mode_shadow),
        "shadow_mode": mode_shadow,
        "per_lane_thresholds_enabled": bool(plt_cfg.get("enabled", False)),
        "min_samples_to_apply_live": int(cal_cfg.get("min_samples_to_apply_live", 15) or 15),
        "beta_veto_max_mean": float(cal_cfg.get("beta_veto_max_mean", 0.0) or 0.0),
        "beta_veto_min_n": int(cal_cfg.get("beta_veto_min_n", 30) or 30),
    }

# ── All-time journal aggregate cache (TTL'd, used for session-vs-alltime deltas)
# Baseline cutoff: earlier sessions ran under pre-ghost / pre-calibration code
# and aren't comparable. Start from 2026-05-15 — first day with settled-ghost
# data and the 2026-05-22 baseline ref doc's bracket.
_ALLTIME_BASELINE_START_ISO = "2026-05-15"
_alltime_agg_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_ALLTIME_AGG_TTL_S = 300.0


def _lane_meta_from_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    extra = dict(trade.get("entry_signal") or {})
    for key in ("lane_id", "lane_side", "lane_window", "lane_regime", "entry_family", "promotion_state"):
        if key not in extra and key in trade:
            extra[key] = trade.get(key)
    return extra


def _iter_lane_entries(journal: Any, limit: int = 5000) -> List[Dict[str, Any]]:
    if not journal:
        return []
    try:
        return list(journal.get_all_entries(limit) or [])
    except Exception:
        return []


# mtime-keyed cache for settings.yaml (2026-06-20: cycle-stall root cause, py-spy-confirmed).
# Many dashboard endpoints parse settings.yaml on every poll; pure-Python yaml.safe_load
# holds the GIL, and since the dashboard runs IN the bot process that starves the trading
# loop (cycles 6s -> 300s+). Re-parse only when the file actually changes on disk.
_yaml_config_cache: Dict[str, Any] = {"mtime": None, "value": None}


def _load_yaml_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        mtime = None
    cache = _yaml_config_cache
    if cache["value"] is not None and cache["mtime"] == mtime:
        return cache["value"]
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            value = yaml.safe_load(f) or {}
    except Exception:
        value = cache["value"] if cache["value"] is not None else {}
    cache["value"] = value
    cache["mtime"] = mtime
    return value


def _append_jsonl_record(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n")


def _read_jsonl_tail(path: Path, limit: int = 20) -> List[Dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return rows[-limit:]
    except Exception:
        return []


def _read_json_object(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_json_object(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, sort_keys=True, indent=2)


def _parse_utc_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _sync_lane_candidate_status(lanes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    store = _read_json_object(LANE_CANDIDATE_STATUS_PATH)
    statuses = dict(store.get("lanes") or {})
    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    changed = False

    for lane in lanes:
        lane_id = str(lane.get("lane_id") or "").strip()
        if not lane_id:
            continue
        prev = dict(statuses.get(lane_id) or {})
        next_row = dict(prev)
        active = bool(lane.get("auto_pause_candidate"))
        if active:
            next_row["active"] = True
            next_row["status"] = "ready" if bool(lane.get("auto_pause_confirmed")) else "watch"
            next_row["first_seen_at"] = str(prev.get("first_seen_at") or now)
            next_row["last_seen_at"] = now
            next_row["reason"] = str(lane.get("auto_pause_reason") or "")
            next_row["confirmation_remaining"] = int(lane.get("auto_pause_confirmation_remaining") or 0)
            if (
                next_row["status"] == "ready"
                and str(lane.get("effective_state") or "") == "live"
                and str(prev.get("status") or "") != "ready"
            ):
                next_row["last_ready_live_warning_at"] = now
                _append_jsonl_record(
                    LANE_STATE_AUDIT_LOG,
                    {
                        "timestamp": now,
                        "event_type": "ready_live_warning",
                        "lane_id": lane_id,
                        "requested_state": "live",
                        "effective_state": "live",
                        "previous_state": str(prev.get("status") or "watch"),
                        "source": "auto_pause_ready_live",
                        "note": "lane reached auto-pause ready while still live",
                    },
                )
        elif prev.get("active"):
            next_row["active"] = False
            next_row["status"] = "cleared"
            next_row["last_cleared_at"] = now
            next_row["reason"] = ""
            next_row["confirmation_remaining"] = 0
        if next_row != prev:
            statuses[lane_id] = next_row
            changed = True

    if changed:
        _write_json_object(
            LANE_CANDIDATE_STATUS_PATH,
            {"updated_at": now, "lanes": statuses},
        )
    return statuses


def _build_lane_health(journal: Any) -> Dict[str, Any]:
    cfg = dict(getattr(_full_bot_instance(), "config", None) or {})
    if not cfg:
        cfg = _load_yaml_config()
    manager = LaneManager(cfg)
    try:
        closed = list(journal.get_closed_trades() if journal else [])
    except Exception:
        closed = []
    per_lane: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "trades": 0,
            "wins": 0,
            "pnl": 0.0,
            "sum_edge": 0.0,
            "sum_confidence": 0.0,
            "sum_realized_return": 0.0,
            "returns_n": 0,
            "ai_trades": 0,
            "exit_reasons": defaultdict(int),
            "strategy": "unknown",
            "lane_state_at_entry": "unknown",
            "lane_side": "unknown",
            "lane_window": "unknown",
            "lane_regime": "unclassified",
            "entry_family": "standard",
        }
    )

    for trade in closed:
        meta = _lane_meta_from_trade(trade)
        lane_id = str(meta.get("lane_id") or "").strip()
        if not lane_id:
            continue
        row = per_lane[lane_id]
        pnl = float(trade.get("pnl") or 0.0)
        edge = float(trade.get("edge") or 0.0)
        conf = float(trade.get("confidence") or 0.0)
        size = float(trade.get("size") or 0.0)
        reason = str(trade.get("exit_reason") or trade.get("reason") or "unknown")
        row["trades"] += 1
        row["wins"] += 1 if pnl > 0 else 0
        row["pnl"] += pnl
        row["sum_edge"] += edge
        row["sum_confidence"] += conf
        if size > 0:
            row["sum_realized_return"] += pnl / size
            row["returns_n"] += 1
        row["ai_trades"] += 1 if bool(meta.get("ai_used", False)) else 0
        row["exit_reasons"][reason] += 1
        row["strategy"] = str(trade.get("strategy") or row["strategy"])
        row["lane_state_at_entry"] = str(meta.get("promotion_state") or row["lane_state_at_entry"])
        row["lane_side"] = str(meta.get("lane_side") or row["lane_side"])
        row["lane_window"] = str(meta.get("lane_window") or meta.get("window_size") or row["lane_window"])
        row["lane_regime"] = str(meta.get("lane_regime") or row["lane_regime"])
        row["entry_family"] = str(meta.get("entry_family") or row["entry_family"])

    lanes: List[Dict[str, Any]] = []
    for lane_id, row in sorted(per_lane.items()):
        trades = int(row["trades"])
        avg_edge = (row["sum_edge"] / trades) if trades else 0.0
        avg_conf = (row["sum_confidence"] / trades) if trades else 0.0
        avg_ret = (row["sum_realized_return"] / row["returns_n"]) if row["returns_n"] else 0.0
        lanes.append(
            {
                "lane_id": lane_id,
                "strategy": row["strategy"],
                "state_at_entry": row["lane_state_at_entry"],
                "lane_side": row["lane_side"],
                "lane_window": row["lane_window"],
                "lane_regime": row["lane_regime"],
                "entry_family": row["entry_family"],
                "trades": trades,
                "win_rate": round((row["wins"] / trades), 4) if trades else 0.0,
                "pnl": round(float(row["pnl"]), 4),
                "expectancy": round(float(row["pnl"]) / trades, 4) if trades else 0.0,
                "avg_edge": round(avg_edge, 4),
                "avg_confidence": round(avg_conf, 4),
                "avg_realized_return_on_notional": round(avg_ret, 4),
                "edge_realized_gap": round(avg_edge - avg_ret, 4) if row["returns_n"] else 0.0,
                "ai_trades": int(row["ai_trades"]),
                "top_exit_reason": max(row["exit_reasons"], key=row["exit_reasons"].get) if row["exit_reasons"] else None,
                "exit_reasons": dict(sorted(row["exit_reasons"].items())),
            }
        )
    for lane in lanes:
        assessment = manager.assess_lane(str(lane.get("lane_id") or ""), lane)
        lane["recommended_state"] = assessment["recommended_state"]
        lane["recommendation_reasons"] = assessment["recommendation_reasons"]
        lane["effective_state"] = assessment["effective_state"]
        lane["matched_rule"] = assessment["matched_rule"]
        lane["auto_pause_candidate"] = assessment["auto_pause_candidate"]
        lane["auto_pause_confirmed"] = assessment["auto_pause_confirmed"]
        lane["auto_pause_confirmation_remaining"] = assessment["auto_pause_confirmation_remaining"]
        lane["auto_pause_reason"] = assessment["auto_pause_reason"]
    candidate_statuses = _sync_lane_candidate_status(lanes)
    now_dt = datetime.utcnow().replace(tzinfo=None)
    for lane in lanes:
        lane_id = str(lane.get("lane_id") or "").strip()
        status_row = dict(candidate_statuses.get(lane_id) or {})
        first_seen_at = str(status_row.get("first_seen_at") or "")
        first_seen_dt = _parse_utc_timestamp(first_seen_at)
        age_minutes = None
        if bool(status_row.get("active")) and first_seen_dt is not None:
            age_minutes = max(0, int((now_dt - first_seen_dt.replace(tzinfo=None)).total_seconds() // 60))
        lane["auto_pause_status"] = str(status_row.get("status") or "")
        lane["auto_pause_first_seen_at"] = first_seen_at or None
        lane["auto_pause_last_seen_at"] = str(status_row.get("last_seen_at") or "") or None
        lane["auto_pause_last_cleared_at"] = str(status_row.get("last_cleared_at") or "") or None
        lane["auto_pause_last_ready_live_warning_at"] = str(status_row.get("last_ready_live_warning_at") or "") or None
        lane["auto_pause_age_minutes"] = age_minutes
    return {"lanes": lanes, "total": len(lanes)}


def _build_lane_states(journal: Any) -> Dict[str, Any]:
    cfg = dict(getattr(_full_bot_instance(), "config", None) or {})
    if not cfg:
        cfg = _load_yaml_config()
    manager = LaneManager(cfg)
    dry_run = bool((cfg.get("trading") or {}).get("dry_run", True))
    observed: Dict[str, Dict[str, Any]] = {}
    for entry in _iter_lane_entries(journal, 5000):
        extra = dict(entry.get("extra") or {})
        lane_id = str(extra.get("lane_id") or "").strip()
        if not lane_id:
            continue
        if lane_id not in observed:
            state, matched_key = manager.get_lane_state(lane_id)
            meta, meta_rule = manager.get_lane_meta(lane_id)
            allowed, reason, effective_state, matched_exec = manager.can_execute(lane_id, dry_run=dry_run)
            observed[lane_id] = {
                "lane_id": lane_id,
                "configured_state": state,
                "matched_rule": matched_key or matched_exec or "",
                "effective_state": effective_state,
                "executable_now": bool(allowed),
                "execution_reason": reason,
                "recommended_state": None,
                "recommendation_reasons": [],
                "strategy": str(entry.get("strategy") or "unknown"),
                "lane_side": str(extra.get("lane_side") or "unknown"),
                "lane_window": str(extra.get("lane_window") or extra.get("window_size") or "unknown"),
                "state_meta": meta,
                "meta_rule": meta_rule,
            }
    configured_only = []
    configured_lane_keys = sorted(set(manager.states.keys()) | set(manager.state_meta.keys()))
    for key in configured_lane_keys:
        if key in observed:
            continue
        meta, meta_rule = manager.get_lane_meta(key)
        allowed, reason, effective_state, matched_exec = manager.can_execute(key, dry_run=dry_run)
        configured_only.append(
            {
                "lane_id": key,
                "configured_state": manager.states.get(key, manager.default_state),
                "matched_rule": key or matched_exec or "",
                "effective_state": effective_state,
                "executable_now": bool(allowed),
                "execution_reason": reason,
                "recommended_state": None,
                "recommendation_reasons": [],
                "strategy": key.split("|", 1)[0] if "|" in key else "unknown",
                "lane_side": key.split("|")[2] if key.count("|") >= 2 else "unknown",
                "lane_window": key.split("|")[1] if key.count("|") >= 1 else "unknown",
                "state_meta": meta,
                "meta_rule": meta_rule,
            }
        )
    return {
        "enabled": bool(manager.enabled),
        "default_state": manager.default_state,
        "dry_run": dry_run,
        "lanes": sorted(observed.values(), key=lambda x: (x["strategy"], x["lane_id"])) + configured_only,
        "configured_rules": len(manager.states),
    }


# ── Lane gate status (config-driven open/closed tracker) ───────────────────────
# Mirrors the per-asset disable_buy_* flags so the dashboard can show, at a glance,
# which strategy/window/side lanes are deliberately stopped.
_LANE_GATE_STRATEGIES: List[Tuple[str, str]] = [
    ("bitcoin", "BTC"),
    ("sol_macro", "SOL"),
    ("eth_macro", "ETH"),
    ("hype_macro", "HYPE"),
    ("xrp_macro", "XRP"),
    ("doge_macro", "DOGE"),
    ("bnb_macro", "BNB"),
]
_LANE_GATE_WINDOWS: List[str] = ["5m", "15m", "1h"]
_LANE_GATE_SIDES: List[str] = ["BUY_YES", "BUY_NO"]


def _resolve_lane_gate(
    strategy_cfg: Dict[str, Any],
    window: str,
    side: str,
    strategy_id: str,
) -> Dict[str, Any]:
    """Return open/closed status for one strategy/window/side lane.

    Checks, in order of precedence:
      - blanket disable for the side (disable_buy_yes / disable_buy_no)
      - per-window disable (disable_buy_yes_<tf> / disable_buy_no_<tf>)
      - bias-conditioned per-window disable (..., _when_bullish / _when_bearish)
      - native 5m BUY_NO suppression (disable_buy_no_5m_native)
      - BTC counter-trend BUY_NO suppression (disable_buy_no_counter_trend)
    """
    side_key = side.lower()  # buy_yes / buy_no

    # 1. Blanket side disable
    flag = f"disable_{side_key}"
    if bool(strategy_cfg.get(flag, False)):
        return {"open": False, "kind": "disabled", "flag": flag}

    # 2. Per-window disable
    flag = f"disable_{side_key}_{window}"
    if bool(strategy_cfg.get(flag, False)):
        return {"open": False, "kind": "disabled", "flag": flag}

    # 3. Bias-conditioned per-window disable
    for bias in ("bullish", "bearish"):
        flag = f"disable_{side_key}_{window}_when_{bias}"
        if bool(strategy_cfg.get(flag, False)):
            return {"open": False, "kind": "conditional", "flag": flag, "condition": f"htf={bias.upper()}"}

    # 4. Native 5m BUY_NO suppression (used by alt strategies)
    if side == "BUY_NO" and window == "5m" and bool(strategy_cfg.get("disable_buy_no_5m_native", False)):
        return {"open": False, "kind": "disabled", "flag": "disable_buy_no_5m_native"}

    # 5. BTC counter-trend BUY_NO suppression
    if side == "BUY_NO" and bool(strategy_cfg.get("disable_buy_no_counter_trend", False)):
        return {"open": False, "kind": "disabled", "flag": "disable_buy_no_counter_trend"}

    return {"open": True, "kind": "open", "flag": None}


def _build_lane_gates(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if cfg is None:
        cfg = _load_yaml_config()
    strategies_cfg = dict(cfg.get("strategies") or {})
    out: Dict[str, Any] = {"updated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z", "strategies": []}
    for strategy_id, label in _LANE_GATE_STRATEGIES:
        strategy_cfg = dict(strategies_cfg.get(strategy_id) or {})
        windows: Dict[str, Any] = {}
        for window in _LANE_GATE_WINDOWS:
            windows[window] = {
                side: _resolve_lane_gate(strategy_cfg, window, side, strategy_id)
                for side in _LANE_GATE_SIDES
            }
        closed_count = sum(1 for w in windows.values() for s in w.values() if not s["open"])
        out["strategies"].append(
            {
                "id": strategy_id,
                "label": label,
                "windows": windows,
                "closed_count": closed_count,
                "total_count": len(_LANE_GATE_WINDOWS) * len(_LANE_GATE_SIDES),
            }
        )
    return out


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
    global bot_instance, _journal_cache
    # Don't let a startup shim overwrite an already-registered full bot.
    # Race: the dashboard thread may call set_bot_instance(_dash_holder) after
    # main() has already registered the real PolyBot — without this guard the
    # shim silently wins and every _full_bot_instance() call returns None.
    if bot is not None and not _is_full_bot(bot) and _is_full_bot(bot_instance):
        return
    bot_instance = bot
    if not _is_full_bot(bot):
        return
    # Live bot owns journal in memory — drop any disk-rebuilt TradeJournal cached while
    # the shim was listening (prevents mismatched summaries vs /api/status after reconnect).
    _journal_cache = {"path": None, "mtime": None, "journal": None}
    # Pre-warm the cache when bot starts so first dashboard load is instant
    _maybe_trigger_refresh(max_age=0)
    # Auto-backtest hook removed 2026-05-24 with the broken backtester.


logger = logging.getLogger(__name__)
DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
BOT_RUNTIME_STATUS_FILE = DATA_ROOT / "runtime" / "bot_runtime_status.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"


def _roundtrip_yaml():
    """ruamel.yaml round-trip handler that preserves comments + formatting.

    Returns None if ruamel is unavailable so callers can fall back to PyYAML
    (comment-stripping, legacy behaviour) instead of crashing.
    """
    try:
        from ruamel.yaml import YAML
    except ImportError:
        return None
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # don't reflow long scalars/comment lines
    return y


def _load_settings_config():
    """Load settings.yaml preserving comments where possible.

    Returns (handler, config): if handler is not None, save via that handler to
    keep the human-authored comments/rationale that the dashboard would otherwise
    strip on every write. Falls back to PyYAML plain load when ruamel is missing.
    """
    y = _roundtrip_yaml()
    if y is not None:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return y, (y.load(f) or {})
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return None, (yaml.safe_load(f) or {})


def _save_settings_config(handler, config) -> None:
    """Persist settings.yaml, preserving comments when handler (ruamel) is set."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        if handler is not None:
            handler.dump(config, f)
        else:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)


LANE_STATE_AUDIT_LOG = DATA_ROOT / "lane_state_audit.jsonl"
LANE_CANDIDATE_STATUS_PATH = DATA_ROOT / "lane_candidate_status.json"
DEFAULT_MARKET_REGIME_LOG = DATA_ROOT / "calibration" / "market_regime.jsonl"

ACTIVE_STRATEGY_NAMES = (
    "bitcoin",
    "sol_macro",
    "eth_macro",
    "hype_macro",
    "xrp_macro",
    "doge_macro",
    "bnb_macro",
)
_DASHBOARD_STRATEGY_NAMES = ACTIVE_STRATEGY_NAMES
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
        # Hourly product questions are "Bitcoin Up or Down - May 17, 1AM ET" —
        # no colon-time range, just a single hour token. Detect those first.
        hourly_match = re.search(r"\b\d{1,2}(am|pm)\s*et\b", ql)
        if hourly_match and len(times) < 2:
            window = 60
        else:
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
        elif "dogecoin" in ql or re.search(r"\bdoge\b", ql):
            sym = "DOGE"
        elif re.search(r"\bbnb\b", ql) or "binance coin" in ql:
            sym = "BNB"
        elif "bitcoin" in ql or re.search(r"\bbtc\b", ql):
            sym = "BTC"
        elif "solana" in ql or re.search(r"\bsol\b", ql):
            sym = "SOL"
        else:
            sym = "UNK"
        if window >= 45:
            sz = "1h"
        elif window <= 6:
            sz = "5m"
        elif window >= 23:
            sz = "30m"  # legacy band retained for historic journal rows
        else:
            sz = "15m"
        return f"{sym}_updown_{sz}"

    # Hourly slug shape (e.g. bitcoin-up-or-down-may-17-2026-1am-et). Emit _1h
    # for new trades; legacy _30m slug branches below stay for old journal rows.
    # HYPE uses the short ``hype-up-or-down-...`` slug prefix on Polymarket hourly.
    if re.search(r"-up-or-down-.*(?:am|pm)-et\b", mid):
        if "ethereum-up-or-down" in mid or "eth_updown_1h" in mid:
            return "ETH_updown_1h"
        if "xrp-up-or-down" in mid or "xrp_updown_1h" in mid:
            return "XRP_updown_1h"
        if "bitcoin-up-or-down" in mid:
            return "BTC_updown_1h"
        if "solana-up-or-down" in mid:
            return "SOL_updown_1h"
        if "hype-up-or-down" in mid or "hyperliquid-up-or-down" in mid or "hype_updown_1h" in mid:
            return "HYPE_updown_1h"
        if "doge-up-or-down" in mid or "dogecoin-up-or-down" in mid or "doge_updown_1h" in mid:
            return "DOGE_updown_1h"
        if "bnb-up-or-down" in mid or "binance-coin-up-or-down" in mid or "bnb_updown_1h" in mid:
            return "BNB_updown_1h"

    if "eth-updown-30m" in mid or "eth_updown_30m" in mid:
        return "ETH_updown_30m"
    if "hype-updown-30m" in mid or "hype_updown_30m" in mid:
        return "HYPE_updown_30m"
    if "xrp-updown-30m" in mid or "xrp_updown_30m" in mid:
        return "XRP_updown_30m"
    if "btc-updown-30m" in mid:
        return "BTC_updown_30m"
    if "sol-updown-30m" in mid:
        return "SOL_updown_30m"

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
    if "doge-updown-15m" in mid or "doge_updown_15m" in mid:
        return "DOGE_updown_15m"
    if "doge-updown-5m" in mid:
        return "DOGE_updown_5m"
    if "bnb-updown-15m" in mid or "bnb_updown_15m" in mid:
        return "BNB_updown_15m"
    if "bnb-updown-5m" in mid:
        return "BNB_updown_5m"

    def _updown_sz_from_blob(s: str) -> str:
        if re.search(r"(^|[^0-9])(5m|5-m|updown-5m)([^0-9]|$)", s):
            return "5m"
        if re.search(r"(30m|30-m|updown-30m)", s):
            return "30m"
        if re.search(r"-up-or-down-.*(?:am|pm)-et\b", s) or re.search(r"\b(1h|1-h|updown-1h)\b", s):
            return "1h"
        return "15m"

    if strategy == "sol_macro":
        return f"SOL_updown_{_updown_sz_from_blob(blob)}"
    if strategy == "eth_macro":
        return f"ETH_updown_{_updown_sz_from_blob(blob)}"
    if strategy == "hype_macro":
        return f"HYPE_updown_{_updown_sz_from_blob(blob)}"
    if strategy == "xrp_macro":
        return f"XRP_updown_{_updown_sz_from_blob(blob)}"
    if strategy == "doge_macro":
        return f"DOGE_updown_{_updown_sz_from_blob(blob)}"
    if strategy == "bnb_macro":
        return f"BNB_updown_{_updown_sz_from_blob(blob)}"
    if strategy == "bitcoin":
        return f"BTC_updown_{_updown_sz_from_blob(blob)}"

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
    title="Oracle AI Dashboard",
    description="Live monitoring for Oracle AI (Polymarket trading bot).",
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
        "dashboard_ui_rev": "2026-06-18-journal-cleanup",
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

_exit_reason_summary_cache: Dict[Tuple[str, Optional[float]], Dict[str, Any]] = {}
_action_breakdown_cache: Dict[Tuple[str, Optional[float]], Dict[str, Any]] = {}

# ── Perf-feedback status cache (2026-06-20: cycle-stall root cause, py-spy-confirmed) ──
# public_feedback_status() -> check_overtight() parses the multi-hundred-MB
# rejected_candidates_settled.jsonl line-by-line on EVERY call. The dashboard polls
# status every ~2s (SSE heartbeat + /api/status + external watchers), so uncached this
# 260MB+ JSON re-parse runs continuously across threadpool workers, pinning the single
# CPU core and starving the bot's scan loop (cycles ballooned 6s -> 300s+). The settled
# file only changes on the ~600s settle cadence, so a short TTL is correct and lossless.
_FEEDBACK_STATUS_TTL_SEC = 120.0
_feedback_status_cache: Dict[str, Any] = {"value": None, "ts": 0.0}


def _cached_public_feedback_status(config: Dict[str, Any]) -> Dict[str, Any]:
    now = _time_mod.time()
    cache = _feedback_status_cache
    if cache["value"] is not None and (now - cache["ts"]) < _FEEDBACK_STATUS_TTL_SEC:
        return cache["value"]
    try:
        value = public_feedback_status(config)
    except Exception:
        value = cache["value"] if cache["value"] is not None else {}
    cache["value"] = value
    cache["ts"] = now
    return value


def _feedback_status_nonblocking() -> Optional[Dict[str, Any]]:
    """Return the last cached feedback status WITHOUT ever parsing on miss.

    public_feedback_status() parses the multi-hundred-MB settled ghost log. In
    split/--dashboard-only mode the dashboard process never has a warm cache, so
    calling the blocking variant inside /api/status made the whole endpoint
    exceed its 12s deadline -> 504 -> the status cache never populated -> bankroll
    and the orb stayed dead permanently. The headline status must never block on
    that parse; the feedback panel has its own (slow-tolerant) endpoint to warm it.
    """
    cache = _feedback_status_cache
    return cache["value"] if cache["value"] is not None else None


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

    # Prefer the newest directory by session id even if it is still empty. After
    # a clean restart/reset, that empty folder is the active paper session and
    # the dashboard must not keep showing the previous resumable session.
    chosen = _latest_session_dir_by_name()
    if chosen is None:
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


# ── Short-TTL cache for the journal summary ───────────────────────────────────
# get_summary() re-parses the journal and can take ~3s on an active session. The
# SSE stream polls it every 2s and several endpoints call it per request, so the
# raw call pins the single dashboard event loop ~100% (CPU-bound under the GIL).
# Cache the result for a few seconds so the expensive parse runs at most once per
# window; PnL/position hero stats do not need sub-TTL freshness.
_journal_summary_cache: Dict[str, Any] = {"ts": 0.0, "summary": None}
_JOURNAL_SUMMARY_TTL = 6.0  # seconds


async def _run_dashboard_blocking(fn, *args, timeout: float = 5.0, label: str = "dashboard_io", **kwargs):
    """Run blocking dashboard I/O off the uvicorn event loop with a hard deadline."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        logger.warning("%s timed out after %.1fs", label, timeout)
        raise HTTPException(
            status_code=504,
            detail=f"{label} timed out after {timeout:.1f}s",
        ) from exc


def _get_cached_journal_summary() -> Dict:
    """``_get_journal_summary`` with a short TTL cache (see note above)."""
    now = _time_mod.time()
    cached = _journal_summary_cache.get("summary")
    if cached is not None and (now - float(_journal_summary_cache.get("ts") or 0.0)) < _JOURNAL_SUMMARY_TTL:
        return cached
    summary = _get_journal_summary()
    _journal_summary_cache["summary"] = summary
    _journal_summary_cache["ts"] = now
    return summary


def _get_current_session_summary() -> Dict:
    """Journal summary for the dashboard's active session.

    A fresh restart creates a newer empty session directory before any fills are
    written. In dashboard-only mode, ``TradeJournal.newest_resumable_session_dir``
    still points at the previous non-empty session; use the empty newer session
    for UI rollups so Performance does not show stale prior-session data.
    """
    summary = _get_cached_journal_summary()
    empty_startup_dir = None if bot_instance is not None else _newer_empty_startup_session(summary)
    if empty_startup_dir is not None:
        return _empty_session_summary(empty_startup_dir.name)
    return summary


def _parse_cycle_epoch_ms(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z") or re.search(r"[+-]\d\d:?\d\d$", text):
        raw = text
    else:
        raw = text + "Z"
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _scanner_health_payload_sync() -> Dict[str, Any]:
    """Cheap scanner freshness payload for the hero orb; no journal parsing."""
    bot = _full_bot_instance()
    source = "bot_instance" if bot is not None else "runtime_status"
    running = bool(getattr(bot, "running", False)) if bot is not None else False
    last_cycles: Dict[str, Any] = {}
    runtime_status: Dict[str, Any] = {}

    if bot is not None:
        last_cycles = dict(getattr(bot, "last_cycle_times", {}) or {})
    else:
        try:
            runtime_status = json.loads(BOT_RUNTIME_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            runtime_status = {}
        last_cycles = dict(runtime_status.get("last_cycle_times") or {})
        runtime_pid = int(runtime_status.get("pid") or 0)
        if runtime_pid and runtime_pid != os.getpid():
            try:
                os.kill(runtime_pid, 0)
                running = True
            except OSError:
                running = False

    newest_ms: Optional[int] = None
    for iso in last_cycles.values():
        ms = _parse_cycle_epoch_ms(iso)
        if ms is not None and (newest_ms is None or ms > newest_ms):
            newest_ms = ms

    # Split / --dashboard-only mode: the lightweight bot_runtime_status.json
    # heartbeat does NOT carry last_cycle_times, so the loop above yields None
    # and the orb shows "not connected". Fall back to the runtime file's own
    # top-level `ts` (rewritten every cycle) as the freshness signal — that IS
    # the scanner heartbeat. Keeps the orb honest without a bot-side change.
    if newest_ms is None and runtime_status:
        rt_ms = _parse_cycle_epoch_ms(runtime_status.get("ts"))
        if rt_ms is not None:
            newest_ms = rt_ms

    now_ms = int(_time_mod.time() * 1000)
    age_seconds = (now_ms - newest_ms) / 1000 if newest_ms is not None else None
    return {
        "ok": True,
        "source": source,
        "running": running,
        "last_cycle_times": last_cycles,
        "newest_cycle_ms": newest_ms,
        "age_seconds": age_seconds,
        "cycle_count": runtime_status.get("cycle_count") if runtime_status else None,
        "phase": runtime_status.get("phase") if runtime_status else None,
        "ts": now_ms,
    }


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


def _empty_session_summary(session_id: Optional[str]) -> Dict[str, Any]:
    return {
        "session_id": session_id,
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
        "summary_source": "empty_startup_session",
    }


_KELLY_STRATEGY_KEYS = ACTIVE_STRATEGY_NAMES


def _empty_kelly_window_stats() -> Dict[str, Dict[str, Any]]:
    return {
        "5m": {"streak": 0, "wins": 0, "losses": 0, "wr": 0.0, "trades": 0},
        "15m": {"streak": 0, "wins": 0, "losses": 0, "wr": 0.0, "trades": 0},
        "30m": {"streak": 0, "wins": 0, "losses": 0, "wr": 0.0, "trades": 0},
        "1h": {"streak": 0, "wins": 0, "losses": 0, "wr": 0.0, "trades": 0},
    }


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
        raw_window_stats = ks.get_all_window_stats()
        window_stats = {
            k: (raw_window_stats.get(k) if isinstance(raw_window_stats.get(k), dict) else _empty_kelly_window_stats())
            for k in _KELLY_STRATEGY_KEYS
        }
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
    out["_window_stats"] = {k: _empty_kelly_window_stats() for k in _KELLY_STRATEGY_KEYS}
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
            if asset == "doge" and 0.001 < price < 100:
                return price
            if asset == "bnb" and 10 < price < 100_000:
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
        return HTMLResponse(
            content=_dashboard_html_with_injections(),
            headers={"Cache-Control": "no-store, must-revalidate"},
        )
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Dashboard file not found.</h1>",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )


# ─── HEALTH (container / PaaS uptime check) ──────────────────────

@app.get("/health")
async def health_check():
    """Keep ``status: ok`` for probes; extra fields help confirm the image matches Git."""
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

    bot = _full_bot_instance()
    if bot is not None:
        return build_ops_snapshot(bot, "http")
    # Split / --dashboard-only mode: the trader runs in a SEPARATE process, so this
    # process holds only a config shim (no .journal). Serve the latest session's
    # journal summary from disk instead of 500-ing on the shim.
    try:
        js = _get_cached_journal_summary()
        return {"source": "disk_journal_summary", "split_dashboard": True, **(js or {})}
    except Exception as exc:
        return {
            "error": "no in-process trading bot (dashboard-only); disk summary unavailable",
            "detail": str(exc)[:200],
        }


# ─── SERVER-SENT EVENTS (live status push every 2s) ───────────────

@app.get("/api/events")
async def sse_stream(request: Request):
    """Server-Sent Events stream — pushes live status snapshot every 2s."""
    async def event_generator():
        sse_interval = 2.0
        try:
            cfg = await _run_dashboard_blocking(
                _load_yaml_config,
                timeout=1.0,
                label="sse_config_read",
            )
            sse_interval = float((cfg.get("dashboard") or {}).get("sse_interval_sec", 2.0))
        except HTTPException:
            sse_interval = 2.0
        while True:
            if await request.is_disconnected():
                break
            try:
                status = await _run_dashboard_blocking(
                    _get_status_payload_sync,
                    timeout=4.0,
                    label="sse_status_snapshot",
                )
                portfolio = status.get("portfolio") or {}
                session = status.get("session") or {}
                positions = status.get("positions") or []
                open_n = int(portfolio.get("total_positions") or len(positions) or 0)
                # /api/status builds portfolio from risk_manager, including:
                # int(getattr(rm, "daily_trades", 0) or 0)
                # round(float(getattr(rm, "daily_pnl", 0) or 0), 2)
                snapshot = {
                    "running": status.get("running", False),
                    "kill_switch_active": status.get("kill_switch_active", False),
                    "dry_run": status.get("dry_run", True),
                    "exposure": status.get("exposure") or {},
                    "loss_pause_active": bool(status.get("loss_pause_active", False)),
                    "loss_pause_lanes": list(status.get("loss_pause_lanes", []) or []),
                    "loss_pause_latest_trigger": status.get("loss_pause_latest_trigger"),
                    "can_trade": status.get("can_trade", False),
                    "ai": status.get("ai") or {},
                    "calibration": status.get("calibration") or {},
                    "session_id": status.get("session_id"),
                    "session_open": int(session.get("open") or status.get("open_positions_count") or open_n),
                    "bankroll": status.get("bankroll"),
                    "bankroll_source": status.get("bankroll_source", "unavailable"),
                    "bankroll_warning": status.get("bankroll_warning"),
                    "positions": open_n,
                    "trades_today": int(portfolio.get("daily_trades") or 0),
                    "daily_pnl": round(float(portfolio.get("daily_pnl") or 0), 2),
                    "open_stake": round(float(session.get("open_stake") or portfolio.get("open_stake") or 0), 2),
                    "session_fills": int(session.get("fills") or 0),
                    "session_closed": int(session.get("closed") or 0),
                    "session_staked": round(float(session.get("session_staked_notional") or 0), 2),
                    "session_realized_pnl": round(float(session.get("realized_pnl") or status.get("realized_pnl") or 0), 2),
                    "session_total_pnl": round(float(session.get("total_pnl") or status.get("total_pnl") or 0), 2),
                    "btc_price": round(
                        float(_btc_analysis_cache.current_price), 0
                    ) if _btc_analysis_cache and hasattr(_btc_analysis_cache, "current_price") else 0,
                    "ai_pipeline": status.get("ai_pipeline") or {},
                    "decision_gates": status.get("decision_gates") or {},
                    "side_selection": status.get("side_selection") or {},
                    "ts": int(_time_mod.time()),
                }
                yield f"data: {json.dumps(snapshot)}\n\n"
            except HTTPException as e:
                detail = getattr(e, "detail", str(e))
                logger.warning("SSE snapshot timed out/failed: %s", detail)
                yield f"data: {json.dumps({'ts': int(_time_mod.time()), 'sse_error': detail})}\n\n"
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


def _latest_session_dir_by_name() -> Optional[Path]:
    from src.execution.trade_journal import JOURNAL_DIR

    if not JOURNAL_DIR.exists():
        return None
    subs = sorted([d for d in JOURNAL_DIR.iterdir() if d.is_dir()], reverse=True)
    return subs[0] if subs else None


def _newer_empty_startup_session(summary: Optional[Dict[str, Any]]) -> Optional[Path]:
    """Newest fresh-start directory that should own Command Center before first write."""
    from src.execution.trade_journal import TradeJournal

    latest = _latest_session_dir_by_name()
    if latest is None or TradeJournal.session_dir_has_activity(latest):
        return None
    current_id = str((summary or {}).get("session_id") or "")
    if current_id and latest.name <= current_id:
        return None
    return latest


def _empty_startup_session_dir_for_summary(summary: Optional[Dict[str, Any]]) -> Optional[Path]:
    """Return the empty startup dir already selected for a status/SSE summary."""
    if not summary or summary.get("summary_source") != "empty_startup_session":
        return None
    sid = str(summary.get("session_id") or "")
    latest = _latest_session_dir_by_name()
    if latest is not None and latest.name == sid:
        return latest
    return None


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
        row = {
            "position_id": pid,
            "market_id": p.get("market_id", ""),
            "market_question": (p.get("market_question") or "N/A")[:80],
            "outcome": p.get("outcome", ""),
            "entry_leg": p.get("entry_leg", ""),
            "size": p.get("size", 0),
            "entry_price": p.get("entry_price", 0),
            "current_price": p.get("current_price", p.get("entry_price", 0)),
            "pnl": p.get("pnl", 0.0),
            "opened_at": p.get("opened_at", ""),
            "strategy": p.get("strategy", "unknown"),
        }
        ty = str(p.get("token_id_yes") or "").strip()
        tn = str(p.get("token_id_no") or "").strip()
        if ty:
            row["token_id_yes"] = ty
        if tn:
            row["token_id_no"] = tn
        leg = str(row.get("entry_leg") or "YES").upper()
        held = tn if leg == "NO" else ty
        if held:
            row["clob_token_id"] = held
        if p.get("end_date"):
            row["end_date"] = p.get("end_date")
        out.append(row)
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


def _resolve_bankroll_snapshot(
    journal_bankroll: Optional[float],
    session_dir: Optional[Path],
    *,
    summary_total_pnl: float,
    summary_has_session: bool,
    initial_bankroll: float,
    prefer_summary_equity: bool = False,
    summary_realized_pnl: Optional[float] = None,
) -> Dict[str, Any]:
    """Preserve a real zero bankroll and surface source/availability to the UI.

    Bankroll is closed (realized) equity only. Unrealized P&L on open positions
    is at-risk and surfaced separately by the UI; folding it into bankroll would
    double-count and inflate/deflate the headline number whenever a session has
    open positions. The display layer (index.html) shows realized in the
    "Bankroll" card and the user can inspect unrealized per-position in the
    open-positions list.
    """
    realized = float(summary_realized_pnl) if summary_realized_pnl is not None else None

    if prefer_summary_equity and summary_has_session and realized is not None:
        return {
            "bankroll": round(float(initial_bankroll) + realized, 2),
            "source": "summary_realized_equity",
        }

    if journal_bankroll is not None:
        return {"bankroll": round(float(journal_bankroll), 2), "source": "journal"}

    br_snap = _last_snapshot_bankroll(session_dir)
    if br_snap is not None:
        return {"bankroll": round(float(br_snap), 2), "source": "snapshots"}

    if summary_has_session:
        # Prefer realized-only; fall back to total_pnl only if realized is missing
        # (legacy summaries that pre-date the realized field).
        addend = realized if realized is not None else float(summary_total_pnl)
        return {
            "bankroll": round(float(initial_bankroll) + addend, 2),
            "source": "summary_realized" if realized is not None else "summary",
        }

    return {
        "bankroll": None,
        "source": "unavailable",
        "warning": "Could not resolve bankroll from journal, summary, or snapshots.",
    }


# ─── STATUS ───────────────────────────────────────────────────────


@app.get("/api/status")
async def get_status():
    # Keep the blocking journal/disk/status assembly off the uvicorn event loop.
    # Uses _get_cached_journal_summary() inside the worker thread.
    return await _run_dashboard_blocking(
        _get_status_payload_sync,
        timeout=12.0,
        label="api_status",
    )


@app.get("/api/scanner/health")
async def get_scanner_health():
    return await _run_dashboard_blocking(
        _scanner_health_payload_sync,
        timeout=1.0,
        label="scanner_health",
    )


# Short-TTL snapshot of the disk-assembled status payload for split/dashboard-only
# mode (see note in _get_status_payload_sync). Keyed by nothing — there is exactly
# one trading process — so a plain {ts,value} cell suffices.
_split_status_cache: Dict[str, Any] = {"ts": 0.0, "value": None}
_SPLIT_STATUS_TTL = 5.0


def _get_status_payload_sync():
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
    # Split / --dashboard-only mode has no in-process bot, so this payload is
    # assembled entirely from disk (journal rebuild + a 260MB feedback parse on a
    # cache miss). Repeated frontend polls otherwise saturate the small dashboard
    # thread pool — which is exactly what made /api/status hang >12s and starved
    # the orb worker into 504s. Serve a short-TTL snapshot so the heavy disk
    # assembly runs at most once per TTL; all other polls return instantly.
    if bot is None:
        _sc = _split_status_cache
        if _sc["value"] is not None and (_time_mod.time() - _sc["ts"]) < _SPLIT_STATUS_TTL:
            return _sc["value"]
    strategy_names = (
        "bitcoin",
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "doge_macro",
        "bnb_macro",
    )
    strategy_attrs = {
        "bitcoin": "bitcoin_strategy",
        "sol_macro": "sol_macro_strategy",
        "eth_macro": "eth_macro_strategy",
        "hype_macro": "hype_macro_strategy",
        "xrp_macro": "xrp_macro_strategy",
        "doge_macro": "doge_macro_strategy",
        "bnb_macro": "bnb_macro_strategy",
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
            can_trade_reason = "Manual global stop active (data/KILL_SWITCH)"

        try:
            _js = _get_cached_journal_summary()
        except Exception:
            _js = {}
        bankroll_cash = getattr(bot, "bankroll", 0.0)
        realized_pnl = float(_js.get("realized_pnl", 0) or 0)
        total_pnl = float(_js.get("total_pnl", 0) or 0)
        unrealized_pnl = total_pnl - realized_pnl
        bankroll = round(float(bankroll_cash) + unrealized_pnl, 2)
        portfolio = (
            bot.risk_manager.get_portfolio_summary(bankroll_cash) if bankroll_cash else None
        )

        def serialize_position(p):
            row = {
                "position_id": p.position_id,
                "market_id": p.market_id,
                "market_question": (p.market_question or "N/A")[:80],
                "outcome": p.outcome,
                "entry_leg": getattr(p, "entry_leg", "YES"),
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
            ty = str(getattr(p, "token_id_yes", "") or "").strip()
            tn = str(getattr(p, "token_id_no", "") or "").strip()
            if ty:
                row["token_id_yes"] = ty
            if tn:
                row["token_id_no"] = tn
            leg = str(row.get("entry_leg") or "YES").upper()
            held = tn if leg == "NO" else ty
            if held:
                row["clob_token_id"] = held
            ed = getattr(p, "end_date", None)
            if ed is not None:
                row["end_date"] = (
                    ed.isoformat() if hasattr(ed, "isoformat") else str(ed)
                )
            return row

        positions = [
            serialize_position(p)
            for p in bot.risk_manager.active_positions.values()
        ]
        _ai_keys = getattr(bot.ai_agent, "api_keys", None) or {}
        try:
            from src.ops_pulse import build_ops_snapshot

            _ops = build_ops_snapshot(bot, "status")
        except Exception:
            _ops = {}
        _loss_pause = _loss_streak_pause_summary()
        return {
            "running": getattr(bot, "running", False),
            "mode": "paper" if dry_run else "live",
            "dry_run": dry_run,
            "exposure": _effective_exposure_section(bot.config),
            "calibration": _calibration_status_from_config(bot.config, dry_run=dry_run),
            "kill_switch_active": kill_switch_active,
            "loss_pause_active": _loss_pause.get("active", False),
            "loss_pause_count": _loss_pause.get("count", 0),
            "loss_pause_lanes": _loss_pause.get("lanes", []),
            "loss_pause_latest_trigger": _loss_pause.get("latest_trigger"),
            "can_trade": can_trade,
            "can_trade_reason": can_trade_reason,
            "bankroll": bankroll,
            "bankroll_source": getattr(bot, "bankroll_source", "bot_mark_to_market"),
            "bankroll_warning": None,
            "portfolio": portfolio,
            "positions": positions,
            "session": _command_center_session(_js),
            "ai": compute_ai_status(bot.config, _ai_keys),
            "strategy_state": _build_strategy_state(
                bot.config, bool(getattr(bot, "running", False))
            ),
            "session_id": getattr(bot.journal, "session_id", None),
            "scan_skip_digest": _ops.get("scan_skip_digest"),
            "decision_gates": _ops.get("decision_gates"),
            "buy_no_skip_diagnostics": _ops.get("buy_no_skip_diagnostics"),
            "side_selection": _ops.get("side_selection"),
            "ops_ai_status": _ops.get("ai_status"),
            "ai_pipeline": _ops.get("ai_pipeline"),
            "ai_activity_note": _ops.get("ai_activity_note"),
            "timestamps_policy": _ops.get("timestamps_policy"),
            "regime": _ops.get("regime"),
            "performance_feedback": _cached_public_feedback_status(bot.config),
            "ts": int(_time_mod.time()),
        }

    # ── No bot_instance: read everything from disk. In split-process mode the
    # dashboard has no in-memory bot, so prefer the runtime file written by the
    # trading child over the dashboard-only process' newest empty startup folder.
    runtime_status: Dict[str, Any] = {}
    try:
        runtime_status = json.loads(BOT_RUNTIME_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        runtime_status = {}
    runtime_pid = int(runtime_status.get("pid") or 0)
    split_bot_running = False
    if runtime_pid and runtime_pid != os.getpid():
        try:
            os.kill(runtime_pid, 0)
            split_bot_running = True
        except OSError:
            split_bot_running = False
    runtime_session_id = str(runtime_status.get("session_id") or "").strip()

    summary = _get_cached_journal_summary()
    j_runtime = _journal_for_query(runtime_session_id) if runtime_session_id else None
    if j_runtime is not None:
        try:
            summary = j_runtime.get_summary()
            summary["summary_source"] = "runtime_journal"
        except Exception:
            pass

    empty_startup_dir = None if split_bot_running else _newer_empty_startup_session(summary)
    if empty_startup_dir is not None:
        summary = _empty_session_summary(empty_startup_dir.name)
        disk_positions = []
    elif j_runtime is not None:
        try:
            disk_positions = j_runtime.get_open_positions()
        except Exception:
            disk_positions = []
    if split_bot_running and int(runtime_status.get("open_positions") or 0) == 0:
        # The runtime_status JSON can lag or have been written before an open
        # position was persisted to disk; do NOT clobber the disk positions
        # just because runtime_status reports 0. Only override disk when the
        # runtime value is a positive, trustworthy count.
        pass
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
    runtime_mode = str(runtime_status.get("mode") or "").lower()
    if split_bot_running and runtime_mode in {"paper", "live"}:
        dry_run = runtime_mode != "live"
    elif split_bot_running:
        argv = [str(x) for x in (runtime_status.get("argv") or [])]
        if "--live" in argv:
            dry_run = False

    j_disk = (
        j_runtime
        if j_runtime is not None
        else (None if empty_startup_dir is not None else _get_journal())
    )
    session_dir_disk = (
        empty_startup_dir
        if empty_startup_dir is not None
        else (j_disk.session_dir if j_disk else _dashboard_journal_session_dir())
    )
    bankroll_journal: Optional[float] = None
    if j_disk:
        try:
            br = j_disk.last_bankroll_from_entries_log()
            if br is not None:
                bankroll_journal = float(br)
        except (TypeError, ValueError):
            pass
    bankroll_payload = _resolve_bankroll_snapshot(
        bankroll_journal,
        session_dir_disk,
        summary_total_pnl=float(summary.get("total_pnl", 0) or 0),
        summary_has_session=bool(summary.get("session_id")),
        initial_bankroll=float(
            (cfg_disk.get("backtest") or {}).get("initial_bankroll", 500) or 500
        ),
        prefer_summary_equity=split_bot_running,
        summary_realized_pnl=(
            float(summary["realized_pnl"])
            if summary.get("realized_pnl") is not None
            else None
        ),
    )

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
    try:
        from src.ops_pulse import _decision_gate_digest

        decision_gates_disk = _decision_gate_digest(cfg_disk, {})
    except Exception:
        decision_gates_disk = None

    _split_payload = {
        "running": split_bot_running,
        "mode": "paper" if dry_run else "live",
        "dry_run": dry_run,
        "exposure": _effective_exposure_section(cfg_disk),
        "calibration": _calibration_status_from_config(cfg_disk, dry_run=dry_run),
        "kill_switch_active": kill_switch_active,
        "loss_pause_active": False,
        "loss_pause_count": 0,
        "loss_pause_lanes": [],
        "loss_pause_latest_trigger": None,
        "can_trade": bool(split_bot_running and not kill_switch_active),
        "can_trade_reason": (
            "Split bot process running"
            if split_bot_running and not kill_switch_active
            else "Manual global stop active (data/KILL_SWITCH)"
            if split_bot_running and kill_switch_active
            else "Bot not running"
        ),
        "bankroll": bankroll_payload.get("bankroll"),
        "bankroll_source": bankroll_payload.get("source"),
        "bankroll_warning": bankroll_payload.get("warning"),
        "portfolio": portfolio_disk,
        "positions": disk_positions,
        # surface summary fields so the UI can show historical stats
        "realized_pnl": summary.get("realized_pnl", 0),
        "total_pnl": summary.get("total_pnl", 0),
        "open_positions_count": summary.get("open_positions", 0),
        "session": session_cc,
        "ai": compute_ai_status(cfg_disk, None),
        "strategy_state": _build_strategy_state(cfg_disk, split_bot_running),
        "session_id": summary.get("session_id") or runtime_session_id or None,
        "scan_skip_digest": None,
        "decision_gates": decision_gates_disk,
        "buy_no_skip_diagnostics": None,
        "side_selection": None,
        "ai_pipeline": {"per_strategy": {}, "aggregate": {}},
        # Non-blocking: never parse the 460MB settled log inside /api/status
        # (that 12s+ parse is what 504'd the endpoint and killed bankroll/orb).
        "performance_feedback": _feedback_status_nonblocking(),
        "ts": int(_time_mod.time()),
    }
    _split_status_cache["value"] = _split_payload
    _split_status_cache["ts"] = _time_mod.time()
    return _split_payload


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
    out = {n: v for n in names if (v := os.getenv(n))}
    if "MINIMAX_API_KEY" not in out:
        m = (
            os.getenv("MINIMAX_API_KEY")
            or os.getenv("MINIMAX_KEY")
            or os.getenv("MINMAX_API_KEY")
        )
        if m:
            out["MINIMAX_API_KEY"] = m
    return out


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


@app.get("/api/orderbook")
async def get_orderbook(token_id: str = Query(..., min_length=4)):
    """L2 order book for a CLOB outcome token.

    Prefers the bot's **WebSocket** cache (``ws_client.snapshot_order_book_json``)
    when subscribed; falls back to **public REST** ``get_order_book`` via
    ``clob_client.fetch_order_book_snapshot`` (no trading keys required).
    """
    bot = _full_bot_instance()
    if not bot:
        raise HTTPException(
            status_code=503,
            detail="Bot instance unavailable — start the bot for live WS books.",
        )
    tid = (token_id or "").strip()
    ws_snap = bot.ws_client.snapshot_order_book_json(tid)
    has_ws_levels = bool(
        ws_snap and ((ws_snap.get("bids") or []) or (ws_snap.get("asks") or []))
    )
    if has_ws_levels:
        return {"source": "websocket", **ws_snap}
    cc = getattr(bot, "clob_client", None)
    if cc:
        try:
            rest = await asyncio.wait_for(cc.fetch_order_book_snapshot(tid), timeout=4.0)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Order book REST fallback timed out") from exc
        if rest and ((rest.get("bids") or []) or (rest.get("asks") or [])):
            return {"source": "rest", **rest}
    if ws_snap is not None:
        return {"source": "websocket", **ws_snap}
    if cc:
        try:
            rest = await asyncio.wait_for(cc.fetch_order_book_snapshot(tid), timeout=4.0)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Order book REST fallback timed out") from exc
        if rest:
            return {"source": "rest", **rest}
    raise HTTPException(
        status_code=503,
        detail="Could not load order book for this token.",
    )


# ─── API USAGE ────────────────────────────────────────────────────


@app.get("/api/usage/summary")
async def get_usage_summary():
    return usage_tracker.get_summary()


@app.get("/api/usage/records")
async def get_usage_records():
    return usage_tracker.get_all_records()


# ---------------------------------------------------------------------------
# Ghost Lab — time-of-day counterfactual explorer
# ---------------------------------------------------------------------------
#
# Merges two outcome sources into one response:
#   1. data/calibration/rejected_candidates_settled.jsonl — settled "ghost"
#      rejections (live scanner rejected, settled vs Polymarket outcome).
#   2. data/paper_trades/test_*/entries.jsonl — EXIT events for actual
#      live (paper) trades.
#   3. data/calibration/lane_posteriors.json — Bayesian per-lane state
#      joined onto each lane.
#
# All sources share the canonical lane_id format from
# src/analysis/lane_identity.py:121 (strategy|window|side|regime|entry_family).


def _gl_parse_ts(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        if isinstance(s, (int, float)):
            return datetime.utcfromtimestamp(float(s)).replace(tzinfo=None)
        txt = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


def _gl_wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[float, float, float]:
    """Wilson score interval for binomial proportion. Returns (p, lo, hi)."""
    if n <= 0:
        return (0.0, 0.0, 0.0)
    p = wins / n
    denom = 1 + (z * z) / n
    centre = (p + (z * z) / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + (z * z) / (4 * n * n)) ** 0.5)) / denom
    return (p, max(0.0, centre - margin), min(1.0, centre + margin))


_GL_GHOST_CACHE: Dict[str, Any] = {"key": None, "ts": 0.0, "rows": None}
_GL_GHOST_CACHE_TTL = 180.0  # seconds
# Cheap raw-line ts extractors for the pre-filter below (no full json.loads).
_GL_TS_RE = re.compile(r'"ts"\s*:\s*"([^"]+)"')
_GL_SETTLED_AT_RE = re.compile(r'"settled_at"\s*:\s*"([^"]+)"')


def _gl_load_ghosts(since: datetime) -> List[Dict[str, Any]]:
    # DISABLED 2026-06-18: Ghost Lab + /api/ghosts/* removed. This full-parse of the
    # 877MB settled-ghost log was a confirmed in-process OOM source — even with the
    # cheap ts pre-filter it ballooned the (embedded) bot's heap to multi-GB when an
    # open Ghost Lab tab polled it (30-day default window). Return [] so this single
    # chokepoint no-ops EVERY caller (the stubbed /api/ghosts/* endpoints + the AI
    # Review priority_actions path). Ghost analysis is done OUT-OF-PROCESS via
    # data/calibration/rejected_candidates_settled.jsonl + .venv scripts (stream +
    # date pre-filter), never in the trading process.
    return []
    # ── legacy implementation below is unreachable (kept for history) ─────────────
    import time as _t

    key = since.isoformat()
    c = _GL_GHOST_CACHE
    if c["key"] == key and c["rows"] is not None and (_t.monotonic() - c["ts"]) < _GL_GHOST_CACHE_TTL:
        return c["rows"]

    path = DATA_ROOT / "calibration" / "rejected_candidates_settled.jsonl"
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # Cheap ts pre-filter: skip rows older than `since` WITHOUT a full
                # json.loads of the fat nested row. This endpoint otherwise parses
                # ~450k rows of the 818MB ghost log on every dashboard/Hermes call,
                # ballooning the IN-PROCESS bot heap toward OOM (2026-06-18). Only
                # rows in the time window get fully parsed. A regex miss (e.g. epoch
                # ts) falls through to the authoritative full parse below.
                _mts = _GL_TS_RE.search(line) or _GL_SETTLED_AT_RE.search(line)
                if _mts is not None:
                    _qts = _gl_parse_ts(_mts.group(1))
                    if _qts is not None and _qts < since:
                        continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = _gl_parse_ts(rec.get("ts") or rec.get("settled_at"))
                if ts is None or ts < since:
                    continue
                context = rec.get("context")
                context_lane_id = (
                    context.get("calibration_lane_id") if isinstance(context, dict) else ""
                )
                live_lane_id = (
                    rec.get("live_lane_id")
                    or context_lane_id
                    or rec.get("lane_id")
                    or ""
                )
                out.append({
                    "ts": ts.isoformat(),
                    "hour_utc": ts.hour,
                    "dow": ts.weekday(),  # Monday=0
                    "lane_id": live_lane_id,
                    "ghost_lane_id": rec.get("ghost_lane_id") or rec.get("lane_id") or "",
                    "live_lane_id": live_lane_id,
                    "strategy": rec.get("strategy") or "",
                    "window": rec.get("window") or "",
                    "side": rec.get("side") or "",
                    "action": rec.get("action") or "",
                    "source": "ghost",
                    "reason": rec.get("reason") or "",
                    "win": bool(rec.get("win")) if rec.get("win") is not None else None,
                    "yes_price": rec.get("yes_price"),
                    "est_prob_up": rec.get("est_prob_up"),
                    "htf_bias": rec.get("htf_bias"),
                    "primary_htf_bias": rec.get("primary_htf_bias"),
                    "alt_htf_bias": rec.get("alt_htf_bias"),
                    "btc_htf_bias": rec.get("btc_htf_bias"),
                    "side_source": rec.get("side_source"),
                    "resolver_path": rec.get("resolver_path"),
                    "lane_family": rec.get("lane_family"),
                    "effective_min_edge": rec.get("effective_min_edge"),
                    "raw_est_prob": rec.get("raw_est_prob"),
                    "calibrated_est_prob": rec.get("calibrated_est_prob"),
                    "gate_reason": rec.get("gate_reason") or rec.get("reason"),
                    "gate_stage": rec.get("gate_stage"),
                    "hypothetical_payout": rec.get("hypothetical_payout"),
                    "market_id": rec.get("market_id"),
                    "price_regime": rec.get("price_regime"),
                    "polymarket_regime": rec.get("polymarket_regime"),
                    "combined_regime": rec.get("combined_regime"),
                    "regime_source": rec.get("regime_source"),
                })
    except Exception:
        pass
    _GL_GHOST_CACHE.update({"key": since.isoformat(), "ts": __import__("time").monotonic(), "rows": out})
    return out


def _gl_load_paper(since: datetime) -> List[Dict[str, Any]]:
    """Scan all paper_trades/<session>/entries.jsonl for live EXIT events."""
    base = DATA_ROOT / "paper_trades"
    out: List[Dict[str, Any]] = []
    if not base.exists():
        return out

    sessions = sorted(base.glob("test_*/entries.jsonl"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    for path in sessions:
        try:
            mtime = datetime.utcfromtimestamp(path.stat().st_mtime)
            if mtime < since - timedelta(days=2):
                # Skip very old sessions whose latest event is well before `since`.
                continue
        except Exception:
            pass
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    event = str(rec.get("event") or "")
                    if event != "EXIT":
                        continue
                    ts = _gl_parse_ts(rec.get("timestamp"))
                    if ts is None or ts < since:
                        continue
                    extra = rec.get("extra") or {}
                    out.append({
                        "ts": ts.isoformat(),
                        "hour_utc": extra.get("hour_utc_entry") if extra.get("hour_utc_entry") is not None else ts.hour,
                        "dow": ts.weekday(),
                        "lane_id": str(extra.get("lane_id") or ""),
                        "strategy": rec.get("strategy") or "",
                        "window": str(extra.get("lane_window") or extra.get("window_size") or ""),
                        "side": str(extra.get("lane_side") or extra.get("direction") or ""),
                        "action": rec.get("action") or "",
                        "source": "live",
                        "reason": rec.get("reason") or "",
                        "win": bool(extra.get("outcome_won")) if extra.get("outcome_won") is not None else (rec.get("pnl", 0) > 0),
                        "yes_price": extra.get("yes_price") or rec.get("entry_price"),
                        "est_prob_up": extra.get("est_prob") or extra.get("raw_est_prob"),
                        "htf_bias": extra.get("htf_bias"),
                        "hypothetical_payout": rec.get("pnl"),
                        "market_id": rec.get("market_id"),
                        "price_regime": extra.get("price_regime"),
                        "polymarket_regime": extra.get("polymarket_regime"),
                        "combined_regime": extra.get("combined_regime"),
                        "regime_source": extra.get("regime_source"),
                    })
        except Exception:
            continue
    return out


def _gl_load_posteriors() -> Dict[str, Dict[str, Any]]:
    path = DATA_ROOT / "calibration" / "lane_posteriors.json"
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        lanes = doc.get("lanes") or doc
        if not isinstance(lanes, dict):
            return {}
        return lanes
    except Exception:
        return {}


def _gl_current_regime_status() -> Dict[str, Any]:
    """Current market-regime feed health (written by the standalone price tracker)."""
    cfg = _load_yaml_config()
    feed_cfg = ((cfg.get("trading") or {}).get("regime_feed") or {})
    path = Path(feed_cfg.get("regime_log") or DEFAULT_MARKET_REGIME_LOG)
    latest: Optional[Dict[str, Any]] = None
    if path.exists():
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
                        latest = obj
        except OSError:
            latest = None

    now = datetime.now(timezone.utc)
    max_age_sec = float(feed_cfg.get("max_snapshot_age_sec", 1800) or 1800)
    age_sec = None
    fresh = False
    if latest:
        try:
            ts = datetime.fromisoformat(str(latest.get("ts")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_sec = max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds())
            fresh = age_sec <= max_age_sec
        except (TypeError, ValueError):
            fresh = False

    combined = str((latest or {}).get("combined_regime") or "")
    poly = str((latest or {}).get("polymarket_regime") or "")
    return {
        "fresh": bool(fresh),
        "age_sec": round(age_sec, 3) if age_sec is not None else None,
        "max_age_sec": max_age_sec,
        "hour_utc": now.hour,
        "combined_regime": combined or None,
        "polymarket_regime": poly or None,
        "latest": latest,
    }


def _gl_aggregate(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate events by lane and by (lane, hour, dow) with Wilson CIs."""
    by_lane: Dict[str, Dict[str, Any]] = {}
    by_lane_hour: Dict[Tuple[str, int], Dict[str, int]] = defaultdict(lambda: {"n": 0, "wins": 0})
    by_lane_hour_dow: Dict[Tuple[str, int, int], Dict[str, int]] = defaultdict(lambda: {"n": 0, "wins": 0})

    for ev in events:
        lid = ev.get("lane_id") or ""
        if not lid:
            continue
        lane = by_lane.setdefault(lid, {
            "lane_id": lid,
            "strategy": ev.get("strategy"),
            "window": ev.get("window"),
            "side": ev.get("side"),
            "n_ghosts": 0, "ghost_wins": 0,
            "n_live": 0, "live_wins": 0,
        })
        src = ev.get("source")
        win = ev.get("win")
        h = ev.get("hour_utc")
        dow = ev.get("dow")
        if src == "ghost":
            lane["n_ghosts"] += 1
            if win is True:
                lane["ghost_wins"] += 1
        elif src == "live":
            lane["n_live"] += 1
            if win is True:
                lane["live_wins"] += 1
        if isinstance(h, int) and win is not None:
            cell = by_lane_hour[(lid, h)]
            cell["n"] += 1
            if win is True:
                cell["wins"] += 1
            if isinstance(dow, int):
                cell2 = by_lane_hour_dow[(lid, h, dow)]
                cell2["n"] += 1
                if win is True:
                    cell2["wins"] += 1
    # Compute WRs + Wilson CIs per lane.
    for lane in by_lane.values():
        for prefix, n_key in (("ghost", "n_ghosts"), ("live", "n_live")):
            n = lane[n_key]
            w = lane[f"{prefix}_wins"]
            p, lo, hi = _gl_wilson_ci(w, n)
            lane[f"{prefix}_wr"] = round(p, 4)
            lane[f"{prefix}_wr_lo"] = round(lo, 4)
            lane[f"{prefix}_wr_hi"] = round(hi, 4)
    buckets_hour = []
    for (lid, h), cell in by_lane_hour.items():
        p, lo, hi = _gl_wilson_ci(cell["wins"], cell["n"])
        buckets_hour.append({"lane_id": lid, "hour_utc": h, "n": cell["n"], "wins": cell["wins"],
                             "wr": round(p, 4), "wr_lo": round(lo, 4), "wr_hi": round(hi, 4)})
    buckets_hour_dow = []
    for (lid, h, dow), cell in by_lane_hour_dow.items():
        p, lo, hi = _gl_wilson_ci(cell["wins"], cell["n"])
        buckets_hour_dow.append({"lane_id": lid, "hour_utc": h, "dow": dow, "n": cell["n"], "wins": cell["wins"],
                                 "wr": round(p, 4), "wr_lo": round(lo, 4), "wr_hi": round(hi, 4)})
    return {
        "lanes": list(by_lane.values()),
        "buckets_hour": buckets_hour,
        "buckets_hour_dow": buckets_hour_dow,
    }


def _gl_regime_breakdown(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate settled ghosts by gate bucket and embedded market regime labels."""
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "regime_source_counts": defaultdict(int)}
    )
    missing_regime = 0

    for ev in events:
        if ev.get("source") != "ghost":
            continue
        lane_id = str(ev.get("lane_id") or "")
        gate = lane_id.split("|")[-1] if lane_id else str(ev.get("reason") or "unknown")
        regime = str(ev.get("combined_regime") or "unknown")
        if regime == "unknown":
            missing_regime += 1
        key = (gate or "unknown", regime)
        bucket = buckets[key]
        bucket["n"] += 1
        if ev.get("win") is True:
            bucket["wins"] += 1
        bucket["regime_source_counts"][str(ev.get("regime_source") or "missing")] += 1

    rows: List[Dict[str, Any]] = []
    for (gate, regime), stats in sorted(buckets.items()):
        p, lo, hi = _gl_wilson_ci(int(stats["wins"]), int(stats["n"]))
        rows.append(
            {
                "gate": gate,
                "regime": regime,
                "n": stats["n"],
                "wins": stats["wins"],
                "win_rate": round(p, 4),
                "ci_lower": round(lo, 4),
                "ci_upper": round(hi, 4),
                "regime_source_counts": dict(stats["regime_source_counts"]),
            }
        )

    return {
        "rows": rows,
        "metadata": {
            "n_ghosts": sum(row["n"] for row in rows),
            "missing_regime": missing_regime,
            "source": "rejected_candidates_settled.jsonl.embedded_regime_fields",
            "wilson_confidence": 0.95,
        },
    }


def _gl_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        if out != out:
            return None
        return out
    except (TypeError, ValueError):
        return None


def _gl_build_decision_digest(since_dt: datetime, until_dt: Optional[datetime] = None) -> Dict[str, Any]:
    until_dt = until_dt or datetime.utcnow()
    digest: Dict[str, Any] = {
        "since": since_dt.isoformat(),
        "now": until_dt.isoformat(),
    }

    try:
        from tools.ghost_gate_report import build_report as build_ghost_gate_report
        from tools.ghost_gate_report import enrich_rows_from_regime_log
        from tools.ghost_gate_report import _iter_jsonl as iter_ghost_jsonl
        from tools.ghost_gate_report import DEFAULT_SETTLED

        raw_rows = [
            row
            for row in iter_ghost_jsonl(DEFAULT_SETTLED)
            if since_dt <= (_gl_parse_ts(row.get("ts") or row.get("settled_at")) or datetime.min) <= until_dt
        ]
        ghost_report = build_ghost_gate_report(enrich_rows_from_regime_log(raw_rows))
        digest["ghost_gate"] = {
            "rows": ghost_report.get("rows", 0),
            "overall": ghost_report.get("overall", {}),
            "actionable_overtight_gates": ghost_report.get("actionable_overtight_gates", [])[:12],
            "top_missed_ev": ghost_report.get("top_missed_ev", [])[:12],
            "top_protected_loss": ghost_report.get("top_protected_loss", [])[:12],
            "regimes": ghost_report.get("regimes", [])[:12],
            "btc_regimes": ghost_report.get("btc_regimes", [])[:12],
            "convergence": ghost_report.get("convergence", [])[:12],
        }
    except Exception as exc:
        digest["ghost_gate"] = {"error": str(exc)}

    try:
        from tools.calibration_report import DEFAULT_LOG as CALIBRATION_LOG
        from tools.calibration_report import _aggregate as aggregate_calibration
        from tools.calibration_report import _iter_records as iter_calibration_records

        calibration_records = [
            row
            for row in iter_calibration_records(CALIBRATION_LOG)
            if since_dt <= (_gl_parse_ts(row.get("ts")) or datetime.min) <= until_dt
        ]
        digest["calibration"] = {
            "rows": aggregate_calibration(calibration_records)[:20],
            "n_records": len(calibration_records),
        }
    except Exception as exc:
        digest["calibration"] = {"error": str(exc)}

    return digest


def _gl_metric_pct(value: Any) -> Optional[float]:
    out = _gl_float(value)
    return round(out * 100.0, 1) if out is not None else None


def _gl_lane_parts(lane_id: Any) -> Dict[str, str]:
    parts = str(lane_id or "").split("|")
    return {
        "strategy": parts[0] if len(parts) > 0 else "",
        "window": parts[1] if len(parts) > 1 else "",
        "side": parts[2] if len(parts) > 2 else "",
        "regime": parts[3] if len(parts) > 3 else "",
        "family": parts[4] if len(parts) > 4 else "",
    }


def _gl_sample_grade(n: Any) -> str:
    count = int(n or 0)
    if count >= 40:
        return "strong"
    if count >= 15:
        return "medium"
    return "thin"


def _gl_file_status(path: Path) -> Dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        return {
            "path": str(path.relative_to(PROJECT_ROOT) if path.is_absolute() and PROJECT_ROOT in path.parents else path),
            "exists": False,
            "updated_at": None,
            "bytes": 0,
        }
    return {
        "path": str(path.relative_to(PROJECT_ROOT) if path.is_absolute() and PROJECT_ROOT in path.parents else path),
        "exists": True,
        "updated_at": datetime.utcfromtimestamp(st.st_mtime).isoformat(),
        "bytes": st.st_size,
    }


def _gl_default_overnight_window(tz_name: str = "America/Los_Angeles") -> Tuple[datetime, datetime, str]:
    """Return the operator's overnight window as naive UTC datetimes."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
        tz_name = "America/Los_Angeles"
    now_local = datetime.now(tz)
    if now_local.hour >= 9:
        end_local = now_local.replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        end_local = now_local
    start_local = end_local.replace(hour=18, minute=0, second=0, microsecond=0)
    if start_local >= end_local:
        start_local -= timedelta(days=1)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    label = f"{start_local.strftime('%Y-%m-%d %H:%M')} -> {end_local.strftime('%Y-%m-%d %H:%M')} {tz_name}"
    return start_utc, end_utc, label


def _gl_build_morning_summary(since_dt: datetime, until_dt: Optional[datetime] = None, *, window_label: str = "") -> Dict[str, Any]:
    """Build an operator-facing overnight summary from Ghost Lab's local evidence."""
    until_dt = until_dt or datetime.utcnow()
    digest = _gl_build_decision_digest(since_dt, until_dt)
    ghost_gate = digest.get("ghost_gate") if isinstance(digest.get("ghost_gate"), dict) else {}
    calibration = digest.get("calibration") if isinstance(digest.get("calibration"), dict) else {}

    standouts: List[Dict[str, Any]] = []
    adjustments: List[Dict[str, Any]] = []
    lane_calibrations: List[Dict[str, Any]] = []
    posteriors = _gl_load_posteriors()
    cfg_disk: Dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg_disk = yaml.safe_load(f) or {}
        except Exception:
            cfg_disk = {}
    cal_cfg = dict(cfg_disk.get("lane_calibration") or {})
    posterior_version = str(cal_cfg.get("posterior_version") or "").strip()
    beta_veto_max_mean = float(cal_cfg.get("beta_veto_max_mean", 0.0) or 0.0)
    beta_veto_min_n = int(cal_cfg.get("beta_veto_min_n", 30) or 30)
    plt_cfg = dict(cal_cfg.get("per_lane_thresholds") or {})
    per_lane_thresholds_enabled = bool(plt_cfg.get("enabled", False))
    per_lane_thresholds: Dict[str, Dict[str, Any]] = {}
    if per_lane_thresholds_enabled:
        try:
            path_str = str(
                plt_cfg.get("path")
                or (DATA_ROOT / "calibration" / "lane_thresholds.json")
            )
            per_lane_thresholds = load_lane_thresholds(Path(path_str))
        except Exception:
            per_lane_thresholds = {}

    def add_standout(kind: str, title: str, evidence: str, action: str, *, n: int = 0, severity: str = "info", lane_id: str = "") -> None:
        standouts.append(
            {
                "kind": kind,
                "severity": severity,
                "title": title,
                "evidence": evidence,
                "action": action,
                "n": n,
                "sample_grade": _gl_sample_grade(n),
                **({"lane_id": lane_id, "lane": _gl_lane_parts(lane_id)} if lane_id else {}),
            }
        )

    for row in (ghost_gate.get("actionable_overtight_gates") or [])[:8]:
        n = int(row.get("n") or 0)
        if n < 10:
            continue
        key = str(row.get("gate_key") or row.get("reason") or "gate")
        wr = _gl_metric_pct(row.get("win_rate"))
        ci_low = _gl_metric_pct(row.get("win_rate_ci_low"))
        net = float(row.get("net_gate_value_pct") or 0.0)
        evidence = f"n={n}, ghost WR {wr}%, Wilson low {ci_low}%, net gate value {net:+.3f}"
        add_standout(
            "gate",
            f"Overtight gate: {key}",
            evidence,
            "Test a small relaxation for this gate/lane family; do not apply globally.",
            n=n,
            severity="warning",
        )
        adjustments.append(
            {
                "setting": key,
                "recommendation": "candidate for lower min_edge or relaxed gate threshold on this lane only",
                "evidence": evidence,
                "confidence": _gl_sample_grade(n),
            }
        )

    for row in (ghost_gate.get("top_protected_loss") or [])[:6]:
        n = int(row.get("n") or 0)
        if n < 10:
            continue
        key = str(row.get("gate_key") or row.get("reason") or "gate")
        wr = _gl_metric_pct(row.get("win_rate"))
        protected = float(row.get("protected_loss_pct") or 0.0)
        evidence = f"n={n}, rejected-candidate WR {wr}%, protected loss {protected:.3f}"
        add_standout(
            "gate",
            f"Protective gate: {key}",
            evidence,
            "Keep this protection; it is currently saving bad entries.",
            n=n,
            severity="positive",
        )

    for row in (calibration.get("rows") or [])[:20]:
        n = int(row.get("n") or 0)
        if n < 3:
            continue
        lane_id = str(row.get("lane_id") or "unknown")
        pnl = float(row.get("total_pnl") or 0.0)
        avg = float(row.get("avg_pnl") or 0.0)
        wr = _gl_metric_pct(row.get("win_rate"))
        alpha = row.get("alpha_implied")
        beta_p50 = row.get("beta_p50")
        posterior = posteriors.get(lane_id) if isinstance(posteriors, dict) else None
        p_n = int((posterior or {}).get("n", 0) or 0) if isinstance(posterior, dict) else 0
        p_a = float((posterior or {}).get("beta_a", 0.0) or 0.0) if isinstance(posterior, dict) else 0.0
        p_b = float((posterior or {}).get("beta_b", 0.0) or 0.0) if isinstance(posterior, dict) else 0.0
        p_total = p_a + p_b
        beta_mean = (p_a / p_total) if p_total > 0 else None
        prefix = f"{posterior_version}::" if posterior_version else ""
        lane_key = lane_id.split("::", 1)[1] if (prefix and lane_id.startswith(prefix)) else lane_id
        threshold_override = per_lane_thresholds.get(lane_id) or per_lane_thresholds.get(lane_key) or {}
        veto_floor = float(threshold_override.get("recommended_max_mean", beta_veto_max_mean) or 0.0)
        veto_recommended = bool(threshold_override.get("veto_recommended"))
        veto_active = False
        if veto_recommended:
            veto_active = True
        elif beta_mean is not None and p_n >= beta_veto_min_n and veto_floor > 0.0:
            veto_active = beta_mean < veto_floor
        action = "collect more samples"
        severity = "info"
        if pnl < 0 and (alpha is None or float(alpha) < 0.8):
            action = "shrink this lane's effective confidence / calibration alpha before increasing size"
            severity = "warning"
        elif pnl > 0 and beta_p50 is not None and float(beta_p50) >= 0.55:
            action = "candidate for slightly more throughput after risk review"
            severity = "positive"
        lane_calibrations.append(
            {
                "lane_id": lane_id,
                "lane": _gl_lane_parts(lane_id),
                "n": n,
                "sample_grade": _gl_sample_grade(n),
                "win_rate_pct": wr,
                "total_pnl": round(pnl, 2),
                "avg_pnl": round(avg, 2),
                "alpha_implied": alpha,
                "beta_p50": beta_p50,
                "beta_mean": round(beta_mean, 4) if beta_mean is not None else None,
                "veto_active": bool(veto_active),
                "veto_source": (
                    "per_lane_override"
                    if veto_recommended and per_lane_thresholds_enabled
                    else ("beta_veto" if veto_active else "none")
                ),
                "severity": severity,
                "recommendation": action,
            }
        )

    standouts.sort(key=lambda r: ({"warning": 3, "positive": 2, "info": 1}.get(str(r.get("severity")), 0), int(r.get("n") or 0)), reverse=True)
    lane_calibrations.sort(key=lambda r: ({"warning": 3, "positive": 2, "info": 1}.get(str(r.get("severity")), 0), abs(float(r.get("total_pnl") or 0))), reverse=True)
    counts = {
        "ghost_rows": int((ghost_gate.get("overall") or {}).get("n") or ghost_gate.get("rows") or 0),
        "calibration_records": int(calibration.get("n_records") or 0),
    }
    actionable_count = len(adjustments) + len([r for r in lane_calibrations if r.get("severity") in {"warning", "positive"}])
    data_loops = [
        {
            "loop": "ghost rejects -> settled outcomes",
            "status": "closed" if counts["ghost_rows"] > 0 else "waiting_for_settled_ghosts",
            "detail": f"{counts['ghost_rows']} settled rejected candidates in window",
        },
        {
            "loop": "closed trades -> lane calibration",
            "status": "closed" if counts["calibration_records"] > 0 else "waiting_for_closed_trades",
            "detail": f"{counts['calibration_records']} calibration trade records in window",
        },
        {
            "loop": "evidence -> settings candidates",
            "status": "closed" if actionable_count > 0 else "waiting_for_stronger_signal",
            "detail": f"{actionable_count} settings or calibration candidates surfaced",
        },
        {
            "loop": "settings candidates -> live config change",
            "status": "manual_review",
            "detail": "dashboard surfaces evidence only; it does not auto-change trading settings",
        },
    ]
    priority_actions = []
    for idx, row in enumerate(adjustments[:5], start=1):
        priority_actions.append(
            {
                "id": f"settings-{idx}",
                "title": row.get("setting") or "settings candidate",
                "change_type": "settings_adjustment",
                "recommendation": row.get("recommendation") or "",
                "evidence": row.get("evidence") or "",
                "confidence": row.get("confidence") or "thin",
                "apply_mode": "manual_review",
                "next_measurement": "Compare the next overnight ghost WR, net gate value, and closed-trade PnL for the same lane/gate.",
            }
        )
    lane_idx = 1
    for row in lane_calibrations:
        if row.get("severity") not in {"warning", "positive"}:
            continue
        priority_actions.append(
            {
                "id": f"lane-calibration-{lane_idx}",
                "title": row.get("lane_id") or "lane calibration",
                "change_type": "lane_calibration",
                "recommendation": row.get("recommendation") or "",
                "evidence": (
                    f"n={row.get('n')}, WR={row.get('win_rate_pct')}%, "
                    f"pnl={row.get('total_pnl')}, alpha={row.get('alpha_implied')}"
                ),
                "confidence": row.get("sample_grade") or "thin",
                "apply_mode": "manual_review",
                "next_measurement": "Track this lane's next closed-trade sample and posterior movement before scaling.",
            }
        )
        lane_idx += 1
        if len(priority_actions) >= 8:
            break
    learning_loop = {
        "mode": "advisory_self_learning",
        "auto_apply": False,
        "closed_loops": sum(1 for row in data_loops if row["status"] == "closed"),
        "total_loops": len(data_loops),
        "next_step": (
            "review_priority_actions"
            if priority_actions
            else "collect_more_overnight_samples"
        ),
        "cycle": [
            "collect live trades and rejected ghosts",
            "settle outcomes against real Polymarket results",
            "summarize standouts and lane calibration",
            "queue manual settings candidates",
            "measure next round against the same lane/gate evidence",
        ],
    }

    return {
        "window": {
            "label": window_label,
            "since": since_dt.isoformat(),
            "until": until_dt.isoformat(),
        },
        "source": "dashboard.ghost_lab",
        "hermes_crons_needed": False,
        "hermes_note": (
            "Hermes ghost crons only format PSB ghost/calibration reports. "
            "Keep them only if you want external notifications."
        ),
        "counts": counts,
        "source_files": {
            "settled_ghosts": _gl_file_status(DATA_ROOT / "calibration" / "rejected_candidates_settled.jsonl"),
            "calibration_trades": _gl_file_status(DATA_ROOT / "calibration" / "trades.jsonl"),
            "lane_posteriors": _gl_file_status(DATA_ROOT / "calibration" / "lane_posteriors.json"),
        },
        "data_loops": data_loops,
        "learning_loop": learning_loop,
        "priority_actions": priority_actions[:8],
        "standouts": standouts[:12],
        "settings_adjustments": adjustments[:10],
        "lane_calibrations": lane_calibrations[:12],
    }


# Ghost Lab + /api/ghosts/* REMOVED 2026-06-18 — these endpoints full-parsed the
# 877MB settled-ghost log inside the trading process and were a confirmed OOM source
# (an open Ghost Lab tab polling them ballooned the bot heap to multi-GB). Ghost
# analysis is now done OUT-OF-PROCESS via data/calibration/*.jsonl + .venv scripts.
# The handlers return a stub so a cached/bookmarked/open tab can't trigger the load.
_GHOST_LAB_REMOVED = {
    "disabled": True,
    "removed": "2026-06-18",
    "reason": "Ghost Lab was an in-process OOM source (877MB ghost-log full-parse).",
    "use_instead": "Query data/calibration/rejected_candidates_settled.jsonl directly (stream + date pre-filter).",
}


@app.get("/api/ghosts/morning-summary")
async def get_ghost_morning_summary(
    since: Optional[str] = None,
    until: Optional[str] = None,
    timezone_name: str = Query("America/Los_Angeles", alias="tz"),
):
    """REMOVED — see _GHOST_LAB_REMOVED."""
    return JSONResponse(content=_GHOST_LAB_REMOVED, headers={"Cache-Control": "no-store"})


def _gl_build_lab_payload(
    since: Optional[str] = None,
    strategy: Optional[str] = None,
    window: Optional[str] = None,
    side: Optional[str] = None,
    limit: int = 5000,
) -> Dict[str, Any]:
    """Ghost Lab — settled-ghost + live-trade explorer with time-of-day buckets."""
    # Default = last 30 days (time-of-day buckets need sample volume).
    if since:
        since_dt = _gl_parse_ts(since) or (datetime.utcnow() - timedelta(days=30))
    else:
        since_dt = datetime.utcnow() - timedelta(days=30)

    ghosts = _gl_load_ghosts(since_dt)
    paper = _gl_load_paper(since_dt)
    events = ghosts + paper

    # Filter.
    if strategy:
        events = [e for e in events if (e.get("strategy") or "") == strategy]
    if window:
        events = [e for e in events if (e.get("window") or "") == window]
    if side:
        events = [e for e in events if (e.get("side") or "") == side]

    agg = _gl_aggregate(events)
    posteriors = _gl_load_posteriors()
    # Join posterior state onto lanes.
    for lane in agg["lanes"]:
        post = posteriors.get(lane["lane_id"]) or {}
        if post:
            a = float(post.get("beta_a") or 0)
            b = float(post.get("beta_b") or 0)
            mean = a / (a + b) if (a + b) > 0 else None
            lane["posterior"] = {
                "alpha_ewma": post.get("alpha_ewma"),
                "beta_a": a,
                "beta_b": b,
                "mean": round(mean, 4) if mean is not None else None,
                "n": post.get("n"),
                "last_updated": post.get("last_updated"),
            }
        else:
            lane["posterior"] = None

    # Distinct reasons sorted by count (for the gate-toggle UI).
    reason_counts: Dict[str, int] = defaultdict(int)
    for e in events:
        r = e.get("reason") or ""
        if r:
            reason_counts[r] += 1
    reasons = [{"reason": r, "n": n} for r, n in sorted(reason_counts.items(), key=lambda kv: -kv[1])]

    # Sort events newest-first; cap the raw array (aggregated buckets always shipped).
    events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    capped = events[: max(1, min(int(limit), 20000))]

    counts = {
        "ghost": sum(1 for e in events if e.get("source") == "ghost"),
        "live": sum(1 for e in events if e.get("source") == "live"),
    }
    current_regime = _gl_current_regime_status()
    return {
        "events": capped,
        "lanes": agg["lanes"],
        "buckets_hour": agg["buckets_hour"],
        "buckets_hour_dow": agg["buckets_hour_dow"],
        "reasons": reasons,
        "current_regime": current_regime,
        "session": {
            "since": since_dt.isoformat(),
            "now": datetime.utcnow().isoformat(),
            "n_events_total": len(events),
            "n_events_capped": len(capped),
            "n_events_per_source": counts,
        },
    }


@app.get("/api/ghosts/lab")
async def get_ghost_lab(
    since: Optional[str] = None,
    strategy: Optional[str] = None,
    window: Optional[str] = None,
    side: Optional[str] = None,
    limit: int = 5000,
):
    """REMOVED — see _GHOST_LAB_REMOVED."""
    return JSONResponse(content=_GHOST_LAB_REMOVED, headers={"Cache-Control": "no-store"})


# /api/backtest/reports route removed 2026-05-24; backtester deleted (see CLAUDE.md ghost-log rule).


@app.get("/api/ghosts/regime-breakdown")
async def get_ghost_regime_breakdown(
    since: Optional[str] = Query(default=None, description="REMOVED"),
):
    """REMOVED — see _GHOST_LAB_REMOVED."""
    return JSONResponse(content=_GHOST_LAB_REMOVED, headers={"Cache-Control": "no-store"})


@app.get("/api/ghosts/decision-digest")
async def get_ghost_decision_digest(
    since: Optional[str] = Query(default=None, description="REMOVED"),
):
    """REMOVED — see _GHOST_LAB_REMOVED."""
    return JSONResponse(content=_GHOST_LAB_REMOVED, headers={"Cache-Control": "no-store"})


# ─── LIVE PERFORMANCE ──────────────────────────────────────────────


@app.get("/api/live/performance")
async def get_live_performance():
    """Live trade performance metrics sourced from summary.json (cached journal for
    closed-trade detail).  No fresh PerformanceTracker() construction per call."""
    summary = _get_current_session_summary()
    strategy_stats = summary.get("strategy_stats", {})
    strategy_stats_filtered: Dict[str, Any] = {}
    if isinstance(strategy_stats, dict):
        strategy_stats_filtered = {
            k: v for k, v in strategy_stats.items() if k in _DASHBOARD_STRATEGY_NAMES
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
    total_pnl = summary.get("total_pnl", realized_pnl)
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
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(summary.get("unrealized_pnl", 0.0), 2),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_drawdown, 2),
        "sharpe_ratio": 0.0,
        "by_strategy": strategy_stats_filtered,
        "equity_curve": equity_curve,
        "kelly_state": kelly,
    }


@app.get("/api/live/drift")
async def get_live_drift():
    """Compare live performance against backtest expectations.

    DEPRECATED: backtest infrastructure has been removed.
    Use ghost calibration and paper trade sessions for performance validation.
    """
    return {
        "reports": [],
        "notice": "backtest infrastructure removed — use ghost calibration and paper trade sessions",
    }


# ─── BOT PROCESS MANAGEMENT ─────────────────────────────────────────


_bot_process: Optional[subprocess.Popen] = None


def _running_bot_pid() -> Optional[int]:
    """Return the PID of a live bot if one is running, else None.

    Reconciles the two ways a bot can exist so the dashboard never goes blind to
    a bot it did not personally spawn (supervisor / split-process mode) and never
    double-spawns onto the same account:
      1) the in-memory Popen handle from /api/live/start, and
      2) the cross-process runtime-status PID file the bot writes itself.
    """
    proc = _bot_process
    if proc is not None and proc.poll() is None:
        return proc.pid
    try:
        runtime_status = json.loads(BOT_RUNTIME_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    pid = int(runtime_status.get("pid") or 0)
    if not pid:
        return None
    # A cleanly shut-down bot leaves its PID in the file; don't report it as live
    # (also guards against the PID being reused by an unrelated process).
    if runtime_status.get("clean_shutdown") and runtime_status.get("phase") == "shutdown_complete":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # exists, owned by another user — still alive
    except Exception:
        return None
    return pid


@app.get("/api/live/status")
async def get_bot_status():
    """Check if a live bot is running (own subprocess or split-mode bot)."""
    pid = _running_bot_pid()
    return {
        "running": pid is not None,
        "pid": pid,
    }


@app.post("/api/live/start")
async def start_live_bot(request: Request, mode: str = "paper"):
    """Start the bot as a background subprocess in explicit paper/live mode."""
    global _bot_process
    _check_auth(request)

    # Reconcile against the runtime-status PID too, so we don't spawn a second bot
    # alongside a supervisor-owned (split-mode) bot already trading this account.
    existing_pid = _running_bot_pid()
    if existing_pid is not None:
        return {"status": "already_running", "pid": existing_pid}

    mode_norm = str(mode or "paper").strip().lower()
    if mode_norm not in {"paper", "live"}:
        return {"status": "error", "message": f"Unsupported mode: {mode}"}

    # --no-dashboard: this dashboard process already serves the UI on its own port,
    # so the spawned bot must NOT auto-start a second dashboard (which would collide
    # on the dashboard port). The bot publishes state via the runtime-status file.
    args = [sys.executable, str(PROJECT_ROOT / "src" / "main.py"), "--no-dashboard"]
    if mode_norm == "live":
        args.extend(["--live", "--confirm-live"])
    else:
        args.append("--paper")

    try:
        _bot_process = subprocess.Popen(
            args,
            cwd=str(PROJECT_ROOT),
            env=_safe_env(),
            stdin=subprocess.DEVNULL,
        )
        logger.info("Bot started with PID %s in %s mode", _bot_process.pid, mode_norm)
        return {"status": "started", "pid": _bot_process.pid, "mode": mode_norm}
    except Exception as e:
        logger.error(f"Failed to start live bot: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/live/stop")
async def stop_live_bot(request: Request):
    """Arm the manual global stop without tearing down the bot subprocess."""
    _check_auth(request)

    kill_switch_file = DATA_ROOT / "KILL_SWITCH"
    kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
    kill_switch_file.touch()
    running = _bot_process is not None and _bot_process.poll() is None
    if running:
        logger.info("Manual global stop enabled; subprocess kept alive for scans/metrics")
        return {
            "status": "trading_halted",
            "kill_switch_active": True,
            "subprocess_running": True,
        }
    logger.info("Manual global stop enabled (no live subprocess was running)")
    return {
        "status": "manual_stop_enabled",
        "kill_switch_active": True,
        "subprocess_running": False,
    }


@app.post("/api/live/shutdown")
async def shutdown_live_bot(request: Request):
    """Cooperatively shut down the dashboard-owned or in-process local bot."""
    _check_auth(request)

    kill_switch_file = DATA_ROOT / "KILL_SWITCH"
    kill_switch_active = kill_switch_file.exists()

    if _bot_process is not None and _bot_process.poll() is None:
        _bot_process.send_signal(signal.SIGINT)
        return {
            "status": "shutdown_signal_sent",
            "target": "dashboard_subprocess",
            "pid": _bot_process.pid,
            "kill_switch_active": kill_switch_active,
        }

    try:
        runtime_status = json.loads(BOT_RUNTIME_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        runtime_status = {}
    runtime_pid = int(runtime_status.get("pid") or 0)
    if runtime_pid and runtime_pid != os.getpid():
        try:
            os.kill(runtime_pid, signal.SIGINT)
            return {
                "status": "shutdown_signal_sent",
                "target": "split_bot_process",
                "pid": runtime_pid,
                "phase": runtime_status.get("phase"),
                "kill_switch_active": kill_switch_active,
            }
        except ProcessLookupError:
            pass
        except PermissionError:
            raise HTTPException(status_code=403, detail="No permission to signal bot process")

    if bot_instance is not None:
        pid = os.getpid()

        def _delayed_sigint() -> None:
            _time_mod.sleep(0.25)
            os.kill(pid, signal.SIGINT)

        threading.Thread(target=_delayed_sigint, daemon=True).start()
        return {
            "status": "shutdown_signal_scheduled",
            "target": "current_process",
            "pid": pid,
            "kill_switch_active": kill_switch_active,
        }

    return {
        "status": "no_running_bot_handle",
        "kill_switch_active": kill_switch_active,
    }


@app.post("/api/live/resume")
async def resume_live_bot(request: Request):
    """Clear the manual global stop so a running bot can trade again."""
    _check_auth(request)

    kill_switch_file = DATA_ROOT / "KILL_SWITCH"
    if kill_switch_file.exists():
        kill_switch_file.unlink()
        logger.info("Manual global stop cleared from dashboard")
    running = _bot_process is not None and _bot_process.poll() is None
    return {
        "status": "trading_resumed",
        "kill_switch_active": False,
        "subprocess_running": running,
    }


# Backtest management (BacktestJob class, /api/backtest/{status,start,output}) removed
# 2026-05-24 with the broken backtester. See CLAUDE.md: validation uses the ghost log.


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


@app.get("/api/strategy/watchlist")
async def get_strategy_watchlist(
    limit: int = 40,
    include_general_markets: bool = True,
):
    """Approximate 'next trigger' levels for dashboard visualization.

    Crypto-only display guidance showing how far active BTC/SOL/ETH/HYPE/XRP
    strategy candidates are from their entry zones and thresholds.
    """
    limit = max(10, min(limit, 200))
    _ = include_general_markets
    cfg = _load_yaml_config()  # mtime-cached: avoid re-parsing settings.yaml per poll (GIL/loop starvation)

    strategies_cfg = cfg.get("strategies", {})
    watchlist: List[Dict[str, Any]] = []

    try:
        from src.analysis.sol_btc_service import SOLBTCService

        asset_specs = {
            "bitcoin": {
                "asset": "btc",
                "spot_symbol": None,
                "cfg": strategies_cfg.get("bitcoin", {}),
                "entry_min": float((strategies_cfg.get("bitcoin", {}) or {}).get("entry_price_min", 0.10)),
                "entry_max": float((strategies_cfg.get("bitcoin", {}) or {}).get("entry_price_max", 0.90)),
            },
            "sol_macro": {
                "asset": "sol",
                "spot_symbol": "SOLUSDT",
                "cfg": strategies_cfg.get("sol_macro", {}),
                "entry_min": float((strategies_cfg.get("sol_macro", {}) or {}).get("entry_price_min", 0.46)),
                "entry_max": float((strategies_cfg.get("sol_macro", {}) or {}).get("entry_price_max", 0.54)),
            },
            "eth_macro": {
                "asset": "eth",
                "spot_symbol": "ETHUSDT",
                "cfg": strategies_cfg.get("eth_macro", {}),
                "entry_min": float((strategies_cfg.get("eth_macro", {}) or {}).get("entry_price_min", 0.46)),
                "entry_max": float((strategies_cfg.get("eth_macro", {}) or {}).get("entry_price_max", 0.54)),
            },
            "hype_macro": {
                "asset": "hype",
                "spot_symbol": None,
                "cfg": strategies_cfg.get("hype_macro", {}),
                "entry_min": float((strategies_cfg.get("hype_macro", {}) or {}).get("entry_price_min", 0.46)),
                "entry_max": float((strategies_cfg.get("hype_macro", {}) or {}).get("entry_price_max", 0.54)),
            },
            "xrp_macro": {
                "asset": "xrp",
                "spot_symbol": "XRPUSDT",
                "cfg": strategies_cfg.get("xrp_macro", {}),
                "entry_min": float((strategies_cfg.get("xrp_macro", {}) or {}).get("watchlist_entry_min", 0.02)),
                "entry_max": float((strategies_cfg.get("xrp_macro", {}) or {}).get("watchlist_entry_max", 0.98)),
            },
            "doge_macro": {
                "asset": "doge",
                "spot_symbol": "DOGEUSDT",
                "cfg": strategies_cfg.get("doge_macro", {}),
                "entry_min": float((strategies_cfg.get("doge_macro", {}) or {}).get("entry_price_min", 0.46)),
                "entry_max": float((strategies_cfg.get("doge_macro", {}) or {}).get("entry_price_max", 0.54)),
            },
            "bnb_macro": {
                "asset": "bnb",
                "spot_symbol": "BNBUSDT",
                "cfg": strategies_cfg.get("bnb_macro", {}),
                "entry_min": float((strategies_cfg.get("bnb_macro", {}) or {}).get("entry_price_min", 0.46)),
                "entry_max": float((strategies_cfg.get("bnb_macro", {}) or {}).get("entry_price_max", 0.54)),
            },
        }

        def _btc_spot_sync():
            try:
                if bot_instance and hasattr(bot_instance, "bitcoin_strategy"):
                    v = bot_instance.bitcoin_strategy.btc_service.get_current_price()
                    if v is not None:
                        return v
                return _get_btc_svc().get_current_price()
            except Exception:
                return None

        def _alt_spot_sync(symbol: str):
            try:
                strategy_attr = {
                    "SOLUSDT": "sol_macro_strategy",
                    "ETHUSDT": "eth_macro_strategy",
                    "DOGEUSDT": "doge_macro_strategy",
                    "BNBUSDT": "bnb_macro_strategy",
                }.get(symbol)
                if strategy_attr and bot_instance and getattr(bot_instance, strategy_attr, None):
                    svc = getattr(getattr(bot_instance, strategy_attr), "sol_service", None)
                    if svc:
                        v = svc.get_current_price(symbol)
                        if v is not None:
                            return v
                return SOLBTCService(alt_symbol=symbol).get_current_price(symbol)
            except Exception:
                return None

        spot_results = await asyncio.gather(
            asyncio.to_thread(_btc_spot_sync),
            asyncio.to_thread(_alt_spot_sync, "SOLUSDT"),
            asyncio.to_thread(_alt_spot_sync, "ETHUSDT"),
            asyncio.to_thread(_alt_spot_sync, "XRPUSDT"),
            asyncio.to_thread(_alt_spot_sync, "DOGEUSDT"),
            asyncio.to_thread(_alt_spot_sync, "BNBUSDT"),
        )
        spot_by_strategy = {
            "bitcoin": spot_results[0],
            "sol_macro": spot_results[1],
            "eth_macro": spot_results[2],
            "xrp_macro": spot_results[3],
            "doge_macro": spot_results[4],
            "bnb_macro": spot_results[5],
        }

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
            spec = asset_specs.get(strat)
            if not spec:
                continue
            q = s.get("market_question", "")
            price = float(s.get("price", 0) or 0)
            entry_min = float(spec["entry_min"])
            entry_max = float(spec["entry_max"])
            spot = spot_by_strategy.get(strat)
            if strat == "doge_macro":
                threshold = _parse_threshold(q, asset="doge")
            elif strat == "bnb_macro":
                threshold = _parse_threshold(q, asset="bnb")
            else:
                threshold = _parse_threshold(q, asset=str(spec["asset"]))
            dist_pct = None
            if threshold and spot:
                dist_pct = abs(float(spot) - threshold) / threshold * 100.0
            trigger = entry_min if price < entry_min else entry_max if price > entry_max else price
            in_band = entry_min <= price <= entry_max
            watchlist.append(
                {
                    "strategy": strat,
                    "market_id": s.get("market_id"),
                    "market_question": q,
                    "action_hint": s.get("action", _parse_direction(q)),
                    "current_price": price,
                    "trigger_price": trigger,
                    "distance": abs(price - trigger),
                    "ready": in_band,
                    "block_reason": "" if in_band else "outside_entry_zone",
                    "spot_price": spot,
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
    # Keep summary/report/scan JSON reads off the uvicorn event loop.
    # Uses _get_current_session_summary() inside the worker thread.
    return await _run_dashboard_blocking(
        _get_strategy_metrics_payload_sync,
        timeout=5.0,
        label="strategy_metrics",
    )


def _get_strategy_metrics_payload_sync():
    """Aggregate strategy performance.

    Primary source: summary.json strategy_stats (always fresh, fast).
    bot_instance only supplements real-time signal counts / cycle timestamps
    if it happens to be running.
    """
    metrics = {
        "bitcoin": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "open_positions": 0,
            "reports": 0,
        },
        "sol_macro": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "open_positions": 0,
            "reports": 0,
        },
        "eth_macro": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "open_positions": 0,
            "reports": 0,
        },
        "hype_macro": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "open_positions": 0,
            "reports": 0,
        },
        "xrp_macro": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "open_positions": 0,
            "reports": 0,
        },
        "doge_macro": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "open_positions": 0,
            "reports": 0,
        },
        "bnb_macro": {
            "signals": 0,
            "trades": 0,
            "pnl": 0,
            "win_rate": None,
            "open_positions": 0,
            "reports": 0,
        },
    }

    # ── Primary: live trade stats from summary.json (disk-first) ──
    summary = _get_current_session_summary()
    for strat, s in summary.get("strategy_stats", {}).items():
        if strat in metrics:
            metrics[strat]["trades"] = s.get("trades", 0)
            metrics[strat]["pnl"] = s.get("pnl", 0)
            metrics[strat]["win_rate"] = s.get("win_rate", None)
            metrics[strat]["wins"] = s.get("wins", 0)
            metrics[strat]["avg_pnl"] = s.get("avg_pnl", 0)
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
    else:
        for p in _load_disk_positions_for_status():
            strat = str(p.get("strategy") or "unknown")
            if strat in metrics:
                metrics[strat]["open_positions"] = (
                    metrics[strat].get("open_positions", 0) + 1
                )

    return metrics


# ─── PAPER TRADE JOURNAL ──────────────────────────────────────────


@app.post("/api/journal/invalidate-cache")
async def invalidate_journal_cache(request: Request):
    """Clear the in-memory TradeJournal cache so the next read replays ``entries.jsonl`` from disk."""
    _check_auth(request)
    global _journal_cache
    _journal_cache = {"path": None, "mtime": None, "journal": None}
    _journal_summary_cache.update({"ts": 0.0, "summary": None})
    _exit_reason_summary_cache.clear()
    _action_breakdown_cache.clear()
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
    return _get_current_session_summary()


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


def _journal_entries_mtime(journal: Any) -> Optional[float]:
    entries_file = getattr(journal, "_entries_file", None)
    if entries_file is None:
        session_dir = getattr(journal, "session_dir", None)
        entries_file = Path(session_dir) / "entries.jsonl" if session_dir else None
    try:
        p = Path(entries_file) if entries_file else None
        return p.stat().st_mtime if p and p.exists() else None
    except OSError:
        return None


def _build_exit_reason_summary(journal: Any) -> Dict[str, Any]:
    session_id = str(getattr(journal, "session_id", "") or "")
    mtime = _journal_entries_mtime(journal)
    cache_key = (session_id, mtime)
    cached = _exit_reason_summary_cache.get(cache_key)
    if cached is not None:
        return cached

    total_by_reason: Dict[str, int] = defaultdict(int)
    by_strategy: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    win_loss_by_reason: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "pnl": 0.0}
    )
    total = 0

    for trade in journal.get_closed_trades():
        reason = str(trade.get("exit_reason") or trade.get("reason") or "").strip()
        if not reason:
            logger.debug("Skipping closed trade with empty exit_reason: %s", trade.get("trade_id"))
            continue
        strategy = str(trade.get("strategy") or "unknown")
        try:
            pnl = float(trade.get("pnl", 0) or 0)
        except (TypeError, ValueError):
            pnl = 0.0

        total += 1
        total_by_reason[reason] += 1
        by_strategy[strategy][reason] += 1
        if pnl > 0:
            win_loss_by_reason[reason]["wins"] += 1
        else:
            win_loss_by_reason[reason]["losses"] += 1
        win_loss_by_reason[reason]["pnl"] += pnl

    payload = {
        "session_id": session_id or None,
        "total": total,
        "total_by_reason": dict(sorted(total_by_reason.items())),
        "by_strategy": {
            strategy: dict(sorted(reasons.items()))
            for strategy, reasons in sorted(by_strategy.items())
        },
        "win_loss_by_reason": {
            reason: {
                "wins": int(stats["wins"]),
                "losses": int(stats["losses"]),
                "pnl": round(float(stats["pnl"]), 2),
            }
            for reason, stats in sorted(win_loss_by_reason.items())
        },
    }
    _exit_reason_summary_cache.clear()
    _exit_reason_summary_cache[cache_key] = payload
    return payload


@app.get("/api/journal/exit-reason-summary")
async def get_exit_reason_summary(session_id: Optional[str] = None):
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    if not j:
        return {
            "session_id": session_id,
            "total": 0,
            "total_by_reason": {},
            "by_strategy": {},
            "win_loss_by_reason": {},
        }
    return _build_exit_reason_summary(j)


def _action_bucket_template() -> Dict[str, Any]:
    return {"wins": 0, "losses": 0, "flat": 0, "pnl": 0.0}


def _fmt_action_bucket(raw: Dict[str, Any]) -> Dict[str, Any]:
    wins = int(raw.get("wins", 0))
    losses = int(raw.get("losses", 0))
    flat = int(raw.get("flat", 0))
    n = wins + losses + flat
    net_pnl = float(raw.get("pnl", 0.0))
    return {
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "trades": n,
        "win_rate": round(wins / n, 4) if n > 0 else 0.0,
        "net_pnl": round(net_pnl, 2),
        "avg_pnl": round(net_pnl / n, 2) if n > 0 else 0.0,
    }


def _compute_action_slipping(
    yes: Dict[str, Any], no: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    min_trades = 3
    if yes.get("trades", 0) < min_trades or no.get("trades", 0) < min_trades:
        return None
    yes_pnl = float(yes.get("net_pnl", 0))
    no_pnl = float(no.get("net_pnl", 0))
    yes_wr = float(yes.get("win_rate", 0))
    no_wr = float(no.get("win_rate", 0))
    if yes_pnl < no_pnl:
        slipping = "BUY_YES"
        reason = "lower_net_pnl"
    elif no_pnl < yes_pnl:
        slipping = "BUY_NO"
        reason = "lower_net_pnl"
    elif yes_wr < no_wr:
        slipping = "BUY_YES"
        reason = "lower_win_rate"
    else:
        slipping = "BUY_NO"
        reason = "lower_win_rate"
    other = no if slipping == "BUY_YES" else yes
    slip = yes if slipping == "BUY_YES" else no
    return {
        "action": slipping,
        "reason": reason,
        "delta_win_rate": round(float(slip.get("win_rate", 0)) - float(other.get("win_rate", 0)), 4),
        "delta_net_pnl": round(float(slip.get("net_pnl", 0)) - float(other.get("net_pnl", 0)), 2),
    }


_ACTION_BREAKDOWN_STRATEGIES = _DASHBOARD_STRATEGY_NAMES


def _empty_reason_bucket() -> Dict[str, Any]:
    return {
        "entries": 0,
        "actions": {"BUY_YES": 0, "BUY_NO": 0, "SELL_YES": 0},
        "path": {
            "updown_15m": 0,
            "updown_5m": 0,
            "updown_1h": 0,
            "updown_30m": 0,
            "threshold": 0,
            "other": 0,
        },
        "bias": {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0, "other": 0},
        "exposure": {"full": 0, "moderate": 0, "minimal": 0, "paused": 0, "other": 0},
        "blockers": {},
    }


def _empty_action_breakdown_payload(session_id: Optional[str] = None) -> Dict[str, Any]:
    empty_yes = _fmt_action_bucket(_action_bucket_template())
    empty_no = _fmt_action_bucket(_action_bucket_template())
    by_strategy: Dict[str, Any] = {}
    for strat in _ACTION_BREAKDOWN_STRATEGIES:
        by_strategy[strat] = {
            "actions": {"BUY_YES": dict(empty_yes), "BUY_NO": dict(empty_no)},
            "slipping": None,
            "total_trades": 0,
        }
    return {
        "session_id": session_id,
        "total_closed": 0,
        "other_action_trades": 0,
        "actions": {"BUY_YES": empty_yes, "BUY_NO": empty_no},
        "slipping": None,
        "by_strategy": by_strategy,
    }


def _new_strategy_action_raw() -> Dict[str, Dict[str, Any]]:
    return {
        "BUY_YES": _action_bucket_template(),
        "BUY_NO": _action_bucket_template(),
        "SELL_YES": _action_bucket_template(),
    }


def _accumulate_action_bucket(bucket: Dict[str, Any], pnl: float) -> None:
    if pnl > 0.01:
        bucket["wins"] += 1
    elif pnl < -0.01:
        bucket["losses"] += 1
    else:
        bucket["flat"] += 1
    bucket["pnl"] += pnl


def _build_action_breakdown(journal: Any) -> Dict[str, Any]:
    """Session closed-trade stats split by BUY_YES vs BUY_NO, per strategy and total."""
    session_id = str(getattr(journal, "session_id", "") or "")
    mtime = _journal_entries_mtime(journal)
    cache_key = (session_id, mtime)
    cached = _action_breakdown_cache.get(cache_key)
    if cached is not None:
        return cached

    raw: Dict[str, Dict[str, Any]] = _new_strategy_action_raw()
    by_strategy_raw: Dict[str, Dict[str, Dict[str, Any]]] = {
        s: _new_strategy_action_raw() for s in _ACTION_BREAKDOWN_STRATEGIES
    }
    other_count = 0
    total_closed = 0

    for trade in journal.get_closed_trades():
        total_closed += 1
        action = str(trade.get("action") or "").strip().upper()
        strategy = str(trade.get("strategy") or "unknown")
        try:
            pnl = float(trade.get("pnl", 0) or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        if action not in raw:
            other_count += 1
            continue
        _accumulate_action_bucket(raw[action], pnl)
        if strategy in by_strategy_raw and action in by_strategy_raw[strategy]:
            _accumulate_action_bucket(by_strategy_raw[strategy][action], pnl)

    actions: Dict[str, Any] = {
        "BUY_YES": _fmt_action_bucket(raw["BUY_YES"]),
        "BUY_NO": _fmt_action_bucket(raw["BUY_NO"]),
    }
    if raw["SELL_YES"]["wins"] + raw["SELL_YES"]["losses"] + raw["SELL_YES"]["flat"] > 0:
        actions["SELL_YES"] = _fmt_action_bucket(raw["SELL_YES"])

    by_strategy: Dict[str, Any] = {}
    for strat in _ACTION_BREAKDOWN_STRATEGIES:
        sraw = by_strategy_raw[strat]
        s_actions = {
            "BUY_YES": _fmt_action_bucket(sraw["BUY_YES"]),
            "BUY_NO": _fmt_action_bucket(sraw["BUY_NO"]),
        }
        s_total = s_actions["BUY_YES"]["trades"] + s_actions["BUY_NO"]["trades"]
        if sraw["SELL_YES"]["wins"] + sraw["SELL_YES"]["losses"] + sraw["SELL_YES"]["flat"] > 0:
            s_actions["SELL_YES"] = _fmt_action_bucket(sraw["SELL_YES"])
        by_strategy[strat] = {
            "actions": s_actions,
            "slipping": _compute_action_slipping(s_actions["BUY_YES"], s_actions["BUY_NO"]),
            "total_trades": s_total,
        }

    payload = {
        "session_id": session_id or None,
        "total_closed": total_closed,
        "other_action_trades": other_count,
        "actions": actions,
        "slipping": _compute_action_slipping(actions["BUY_YES"], actions["BUY_NO"]),
        "by_strategy": by_strategy,
    }
    _action_breakdown_cache.clear()
    _action_breakdown_cache[cache_key] = payload
    return payload


@app.get("/api/journal/action_breakdown")
async def get_action_breakdown(session_id: Optional[str] = None):
    """BUY_YES vs BUY_NO closed-trade performance for the current paper session."""
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    if not j:
        return _empty_action_breakdown_payload(session_id)
    return _build_action_breakdown(j)


@app.get("/api/lane_gates")
async def get_lane_gates():
    """Config-driven open/closed status per strategy/window/side lane."""
    return _build_lane_gates()


def _closed_trades_to_chart_points(trades: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
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
                "exit_reason": t.get("exit_reason"),
            }
        )
    return points


@app.get("/api/journal/trade-points")
async def get_journal_trade_points(
    limit: int = 300,
    session_id: Optional[str] = None,
    include_recent: bool = False,
):
    """Normalized closed-trade points for charting.

    Returns epoch `time` + entry/exit prices so frontend can render bubble/marker
    overlays without guessing field names. ``include_recent`` is retained only
    for backward-compatible query parsing; fresh sessions must not inherit old
    closed-trade points.
    """
    empty_startup_session_id = None
    if not session_id:
        empty_startup_dir = _newer_empty_startup_session(_get_cached_journal_summary())
        if empty_startup_dir is not None:
            empty_startup_session_id = empty_startup_dir.name
            return {"points": [], "session_id": empty_startup_session_id}
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    points = _closed_trades_to_chart_points(j.get_closed_trades() if j else [], limit)
    return {"points": points, "session_id": getattr(j, "session_id", None) if j else session_id}


@app.get("/api/session/equity_history")
def get_session_equity_history(limit: int = 1000, session_id: Optional[str] = None):
    """Equity time-series for the current (or named) session, sourced from
    ``snapshots.jsonl``. Each row contains ``t`` (epoch ms) and ``v`` (equity =
    bankroll + realized_pnl + unrealized_pnl). Used to restore the Live P&L
    trace shape after a dashboard refresh.
    """
    if not session_id:
        empty_startup_dir = _newer_empty_startup_session(_get_cached_journal_summary())
        if empty_startup_dir is not None:
            return {"points": [], "session_id": empty_startup_dir.name}
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    session_dir = getattr(j, "session_dir", None) if j else None
    if session_dir is None:
        session_dir = _dashboard_journal_session_dir()
    if not session_dir:
        return {"points": [], "session_id": None}
    snap = Path(session_dir) / "snapshots.jsonl"
    if not snap.exists():
        return {"points": [], "session_id": getattr(j, "session_id", None) if j else None}
    limit = max(10, min(int(limit), 5000))
    points: List[Dict[str, Any]] = []
    try:
        with open(snap, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = o.get("timestamp")
                if not ts:
                    continue
                try:
                    epoch_ms = int(datetime.fromisoformat(str(ts)).timestamp() * 1000)
                except Exception:
                    continue
                try:
                    base = float(o.get("bankroll") or 0)
                    rpnl = float(o.get("realized_pnl") or 0)
                    upnl = float(o.get("unrealized_pnl") or 0)
                except (TypeError, ValueError):
                    continue
                points.append({"t": epoch_ms, "v": round(base + rpnl + upnl, 4)})
    except OSError:
        return {"points": [], "session_id": getattr(j, "session_id", None) if j else None}
    if len(points) > limit:
        # Decimate evenly so the shape survives without flooding the chart.
        step = len(points) / float(limit)
        points = [points[int(i * step)] for i in range(limit)]
    return {
        "points": points,
        "session_id": getattr(j, "session_id", None) if j else None,
    }


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
def get_updown_breakdown(session_id: Optional[str] = None):
    """Break down closed trades by up/down bucket (1h / 30m / 15m / 5m per asset).
    Also splits trades before/after recent marker lines in local logs.
    """
    import re as _re
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    from collections import defaultdict as _dd

    # Detect new-code start time. This marker scan walks up to 7 daily logs and was
    # the main cost of this endpoint (~14-17s). The running code version is stable,
    # so compute it ONCE per process and cache (the DISPLAYED trades are session-
    # scoped, not 7 days — the scan only sets the old/new split point).
    _nc_cache = getattr(get_updown_breakdown, "_nc_cache", None)
    if _nc_cache is not None:
        new_code_start = _nc_cache["v"]
    else:
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
        get_updown_breakdown._nc_cache = {"v": new_code_start}

    j = _journal_for_query(session_id) if session_id else _get_journal()
    closed = j.get_closed_trades() if j else []

    # Flat trades (|pnl| <= 0.01, including exact break-even resolutions) get
    # their own bucket but still count toward `trades` so the denominator
    # matches `len(closed)` for the rendered category. Win-rate stays honest:
    # a binary that resolved flat is not a win.
    old_stats: dict = _dd(lambda: {"wins": 0, "losses": 0, "flat": 0, "pnl": 0.0})
    new_stats: dict = _dd(lambda: {"wins": 0, "losses": 0, "flat": 0, "pnl": 0.0})

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
        else:
            bucket[cat]["flat"] += 1
        bucket[cat]["pnl"] += pnl

    def _fmt(d):
        out = {}
        for cat, v in d.items():
            wins = v.get("wins", 0)
            losses = v.get("losses", 0)
            flat = v.get("flat", 0)
            n = wins + losses + flat
            out[cat] = {
                "wins": wins, "losses": losses, "flat": flat,
                "trades": n,
                "win_rate": round(wins / n, 4) if n > 0 else 0.0,
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
        strategy: _empty_reason_bucket() for strategy in _DASHBOARD_STRATEGY_NAMES
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
        elif "updown_1h" in r_low:
            bucket["path"]["updown_1h"] += 1
        elif "updown_30m" in r_low:
            bucket["path"]["updown_30m"] += 1
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
def get_journal_entries(limit: int = 100, session_id: Optional[str] = None):
    limit = max(1, min(int(limit), 500))
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"entries": j.get_all_entries(limit) if j else []}


@app.get("/api/journal/lane-health")
async def get_journal_lane_health(session_id: Optional[str] = None):
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    if not j:
        return {"lanes": [], "total": 0}
    return _build_lane_health(j)


@app.get("/api/journal/lane-states")
async def get_journal_lane_states(session_id: Optional[str] = None):
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    states = _build_lane_states(j)
    health = _build_lane_health(j) if j else {"lanes": []}
    rec_map = {
        str(item.get("lane_id") or ""): item
        for item in (health.get("lanes") or [])
    }
    for row in states.get("lanes", []):
        rec = rec_map.get(str(row.get("lane_id") or ""))
        row["recommended_state"] = rec.get("recommended_state") if rec else None
        row["recommendation_reasons"] = rec.get("recommendation_reasons", []) if rec else []
        row["auto_pause_candidate"] = bool(rec.get("auto_pause_candidate")) if rec else False
        row["auto_pause_confirmed"] = bool(rec.get("auto_pause_confirmed")) if rec else False
        row["auto_pause_confirmation_remaining"] = int(rec.get("auto_pause_confirmation_remaining", 0)) if rec else 0
        row["auto_pause_reason"] = rec.get("auto_pause_reason", "") if rec else ""
        row["auto_pause_status"] = rec.get("auto_pause_status", "") if rec else ""
        row["auto_pause_first_seen_at"] = rec.get("auto_pause_first_seen_at") if rec else None
        row["auto_pause_last_seen_at"] = rec.get("auto_pause_last_seen_at") if rec else None
        row["auto_pause_last_cleared_at"] = rec.get("auto_pause_last_cleared_at") if rec else None
        row["auto_pause_last_ready_live_warning_at"] = rec.get("auto_pause_last_ready_live_warning_at") if rec else None
        row["auto_pause_age_minutes"] = rec.get("auto_pause_age_minutes") if rec else None
    return states


@app.get("/api/lane-state-history")
async def get_lane_state_history(limit: int = 20):
    rows = _read_jsonl_tail(LANE_STATE_AUDIT_LOG, max(1, min(int(limit), 100)))
    rows.reverse()
    return {"items": rows, "total": len(rows)}


@app.get("/api/journal/snapshots")
def get_journal_snapshots(limit: int = 500, session_id: Optional[str] = None):
    j = _journal_for_query(session_id) if session_id else _get_journal()
    if session_id and not j:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"snapshots": j.get_snapshots(limit) if j else []}


@app.get("/api/journal/sessions")
async def get_journal_sessions():
    from src.execution.trade_journal import TradeJournal
    return {"sessions": TradeJournal.list_sessions()}


@app.post("/api/journal/prune-short-sessions")
async def prune_short_journal_sessions(request: Request, execute: bool = False):
    """Dry-run or delete completed paper sessions below the 50-fill listing floor."""
    _check_auth(request)
    from src.execution.trade_journal import TradeJournal

    return TradeJournal.prune_short_completed_sessions(execute=execute)


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


# ─── EXPOSURE MANAGER ────────────────────────────────────────────


def _all_exposure_managers():
    """Return all active exposure managers."""
    bot = _full_bot_instance()
    if not bot:
        return []
    managers = []
    seen = set()
    for attr in (
        "btc_exposure_manager",
        "sol_exposure_manager",
        "eth_exposure_manager",
        "hype_exposure_manager",
        "xrp_exposure_manager",
        "doge_exposure_manager",
        "bnb_exposure_manager",
    ):
        mgr = getattr(bot, attr, None)
        if mgr and id(mgr) not in seen:
            seen.add(id(mgr))
            managers.append(mgr)
    return managers


def _loss_streak_pause_summary() -> Dict[str, Any]:
    """Return whether any exposure manager is paused by consecutive losses."""
    paused_lanes: List[str] = []
    latest_trigger: Optional[Dict[str, Any]] = None
    for mgr in _all_exposure_managers():
        try:
            st = mgr.get_status()
        except Exception:
            continue
        trigger = st.get("last_loss_kill_trigger") if isinstance(st, dict) else None
        if isinstance(trigger, dict) and trigger:
            ts = str(trigger.get("timestamp") or "")
            if latest_trigger is None or ts > str(latest_trigger.get("timestamp") or ""):
                latest_trigger = {
                    "lane": str(trigger.get("lane") or getattr(mgr, "lane_name", "") or "unknown"),
                    "window_size": str(trigger.get("window_size") or ""),
                    "reason": str(trigger.get("reason") or ""),
                    "timestamp": ts,
                }
        if not bool(st.get("paused")):
            continue
        reason = str(st.get("pause_reason") or "")
        if "consecutive losses" not in reason.lower():
            continue
        lane_name = str(getattr(mgr, "lane_name", "") or "").lower()
        paused_lanes.append(lane_name or "unknown")
    return {
        "active": bool(paused_lanes),
        "count": len(paused_lanes),
        "lanes": paused_lanes,
        "latest_trigger": latest_trigger,
    }


def _effective_exposure_section(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return exposure config overlaid with the running bot's effective state.

    The bot may apply a process-start env override for ``loss_kill_switch_enabled``.
    When that happens, the dashboard must reflect the runtime truth instead of the
    YAML snapshot, otherwise the button can show OFF while the bot is actually ON.
    """
    base = dict((config or {}).get("exposure") or {})
    bot = _full_bot_instance()
    mgr = getattr(bot, "btc_exposure_manager", None) if bot else None
    if mgr is not None and hasattr(mgr, "loss_kill_switch_enabled"):
        # Report the EFFECTIVE state: the loss-streak pause is live-only, so a
        # paper session shows OFF even when the flag is set. Falls back to the raw
        # flag if loss_kill_active isn't present (older manager).
        base["loss_kill_switch_enabled"] = bool(
            getattr(mgr, "loss_kill_active", mgr.loss_kill_switch_enabled)
        )
        base["_runtime_source"] = "bot"
    else:
        base["_runtime_source"] = "config"
    return base


EXPOSURE_LANE_TO_ATTR = {
    "btc": "btc_exposure_manager",
    "sol": "sol_exposure_manager",
    "eth": "eth_exposure_manager",
    "hype": "hype_exposure_manager",
    "xrp": "xrp_exposure_manager",
    "doge": "doge_exposure_manager",
    "bnb": "bnb_exposure_manager",
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
        try:
            runtime_status = json.loads(BOT_RUNTIME_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            runtime_status = {}
        runtime_exposure = runtime_status.get("exposure_managers")
        if isinstance(runtime_exposure, dict) and runtime_exposure:
            out = dict(runtime_exposure)
            out["_source"] = out.get("_source") or "runtime_status"
            out["_runtime_phase"] = runtime_status.get("phase")
            out["_runtime_pid"] = runtime_status.get("pid")
            out["_runtime_ts"] = runtime_status.get("ts")
            return out
        return {"error": "No bot instance or runtime exposure snapshot"}
    key_attrs = (
        ("btc", "btc_exposure_manager"),
        ("sol", "sol_exposure_manager"),
        ("eth", "eth_exposure_manager"),
        ("hype", "hype_exposure_manager"),
        ("xrp", "xrp_exposure_manager"),
        ("doge", "doge_exposure_manager"),
        ("bnb", "bnb_exposure_manager"),
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
            detail="Unknown lane or bot not running. Use btc, sol, eth, hype, or xrp.",
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
            detail="Unknown lane or bot not running. Use btc, sol, eth, hype, or xrp.",
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
                "macd_1h_hist": _f(ta.macd_1h.histogram),
                "macd_1h_hist_rising": _b(ta.macd_1h.histogram_rising),
                "macd_1h_cross": ta.macd_1h.crossover or "",
                "macd_1h_above_zero": _b(ta.macd_1h.above_zero),
                "macd_30m_hist": _f(ta.macd_30m.histogram),
                "macd_30m_hist_rising": _b(ta.macd_30m.histogram_rising),
                "macd_30m_cross": ta.macd_30m.crossover or "",
                "macd_30m_above_zero": _b(ta.macd_30m.above_zero),
                "macd_15m_hist": _f(ta.macd_15m.histogram),
                "macd_15m_hist_rising": _b(ta.macd_15m.histogram_rising),
                "macd_15m_cross": ta.macd_15m.crossover or "",
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
        "macd_30m_hist": sol.macd_30m.histogram,
        "macd_30m_hist_rising": sol.macd_30m.histogram_rising,
        "macd_30m_cross": sol.macd_30m.crossover,
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
        "btc_move_30m": corr.btc_move_30m_pct,
        # Alt-leg % moves — sol_move_* kept for backwards compatibility (same numeric series for any alt)
        "sol_move_5m": corr.sol_move_5m_pct,
        "sol_move_15m": corr.sol_move_15m_pct,
        "sol_move_30m": corr.sol_move_30m_pct,
        f"{alt_code}_move_5m": corr.sol_move_5m_pct,
        f"{alt_code}_move_15m": corr.sol_move_15m_pct,
        f"{alt_code}_move_30m": corr.sol_move_30m_pct,
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
        from src.analysis.hyperliquid_hype_service import (
            HyperliquidHypeService,
            hyperliquid_kwargs_from_config,
        )

        svc = None
        if bot_instance and hasattr(bot_instance, "hype_macro_strategy"):
            svc = getattr(bot_instance.hype_macro_strategy, "sol_service", None)
        if svc is None:
            hl = {}
            try:
                if CONFIG_PATH.exists():
                    with open(CONFIG_PATH) as f:
                        root = yaml.safe_load(f) or {}
                    hl = hyperliquid_kwargs_from_config(root.get("hyperliquid"))
            except Exception:
                hl = {}
            svc = HyperliquidHypeService(**hl)
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


@app.get("/api/doge/analysis")
async def get_doge_analysis():
    """Live DOGE–BTC correlation for dashboard."""
    try:
        ta, alt_sym = await asyncio.to_thread(_run_alt_analysis_sync, "DOGEUSDT", "doge_macro_strategy")
        if ta:
            return _solbtc_analysis_payload(ta, alt_sym)
        return {"error": "DOGE analysis not available"}
    except Exception as e:
        logger.error(f"DOGE analysis endpoint error: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/bnb/analysis")
async def get_bnb_analysis():
    """Live BNB–BTC correlation for dashboard."""
    try:
        ta, alt_sym = await asyncio.to_thread(_run_alt_analysis_sync, "BNBUSDT", "bnb_macro_strategy")
        if ta:
            return _solbtc_analysis_payload(ta, alt_sym)
        return {"error": "BNB analysis not available"}
    except Exception as e:
        logger.error(f"BNB analysis endpoint error: {e}", exc_info=True)
        return {"error": str(e)}


# ─── CROSS-ASSET MACRO ALIGNMENT ──────────────────────────────────

_MACRO_ALIGN_ASSETS = [
    {"key": "bitcoin",    "symbol": "BTCUSDT",  "label": "BTC",  "color": "#22d3ee", "source": "binance"},
    {"key": "sol_macro",  "symbol": "SOLUSDT",  "label": "SOL",  "color": "#a855f7", "source": "binance"},
    {"key": "eth_macro",  "symbol": "ETHUSDT",  "label": "ETH",  "color": "#fb923c", "source": "binance"},
    {"key": "hype_macro", "symbol": "HYPEUSDT", "label": "HYPE", "color": "#a78bfa", "source": "hyperliquid"},
    {"key": "xrp_macro",  "symbol": "XRPUSDT",  "label": "XRP",  "color": "#ff5a36", "source": "binance"},
    {"key": "doge_macro", "symbol": "DOGEUSDT", "label": "DOGE", "color": "#ff6ec7", "source": "binance"},
    {"key": "bnb_macro",  "symbol": "BNBUSDT",  "label": "BNB",  "color": "#f3ba2f", "source": "binance"},
]


def _macro_pearson(a: List[float], b: List[float]) -> Optional[float]:
    """Pearson correlation of two equal-length series; returns None if invalid."""
    n = min(len(a), len(b))
    if n < 5:
        return None
    aa = a[-n:]
    bb = b[-n:]
    ma = sum(aa) / n
    mb = sum(bb) / n
    sxy = sum((aa[i] - ma) * (bb[i] - mb) for i in range(n))
    sxx = sum((aa[i] - ma) ** 2 for i in range(n))
    syy = sum((bb[i] - mb) ** 2 for i in range(n))
    denom = (sxx * syy) ** 0.5
    if denom <= 1e-9:
        return None
    return max(-1.0, min(1.0, sxy / denom))


_MACRO_ALIGN_STRAT_ATTRS = {
    "BTCUSDT": ("bitcoin_strategy", "btc_service"),
    "SOLUSDT": ("sol_macro_strategy", "sol_service"),
    "ETHUSDT": ("eth_macro_strategy", "sol_service"),
    "XRPUSDT": ("xrp_macro_strategy", "sol_service"),
    "HYPEUSDT": ("hype_macro_strategy", "sol_service"),
    "DOGEUSDT": ("doge_macro_strategy", "sol_service"),
    "BNBUSDT": ("bnb_macro_strategy", "sol_service"),
}

# Lightweight singleton services for symbols the bot doesn't expose (e.g. before
# DOGE/BNB strategies attach to bot_instance). One instance per symbol; reused
# across requests so we don't re-init transport on every dashboard tick.
_MACRO_ALIGN_FALLBACK_SVC: Dict[str, Any] = {}

def _macro_align_get_svc(symbol: str, source: str):
    """Return an existing service to fetch klines for `symbol` without spinning up new transports."""
    if source == "hyperliquid":
        # HYPE: try bot's hype service first (carries auth + warmed config)
        if bot_instance and hasattr(bot_instance, "hype_macro_strategy"):
            svc = getattr(bot_instance.hype_macro_strategy, "sol_service", None)
            if svc is not None:
                return svc
        cached = _MACRO_ALIGN_FALLBACK_SVC.get(symbol)
        if cached is not None:
            return cached
        from src.analysis.hyperliquid_hype_service import (
            HyperliquidHypeService,
            hyperliquid_kwargs_from_config,
        )
        hl = {}
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH) as f:
                    root = yaml.safe_load(f) or {}
                hl = hyperliquid_kwargs_from_config(root.get("hyperliquid"))
        except Exception:
            hl = {}
        svc = HyperliquidHypeService(**hl)
        _MACRO_ALIGN_FALLBACK_SVC[symbol] = svc
        return svc

    # Binance route: prefer the bot's pre-warmed service if attached.
    attrs = _MACRO_ALIGN_STRAT_ATTRS.get(symbol)
    if bot_instance and attrs:
        strat_attr, svc_attr = attrs
        strat = getattr(bot_instance, strat_attr, None)
        if strat is not None:
            svc = getattr(strat, svc_attr, None)
            if svc is not None and hasattr(svc, "fetch_klines"):
                return svc
    cached = _MACRO_ALIGN_FALLBACK_SVC.get(symbol)
    if cached is not None:
        return cached
    if symbol == "BTCUSDT":
        svc = _get_btc_svc()
    else:
        from src.analysis.sol_btc_service import SOLBTCService
        svc = SOLBTCService(alt_symbol=symbol)
    _MACRO_ALIGN_FALLBACK_SVC[symbol] = svc
    return svc


def _macro_fetch_series_sync(symbol: str, source: str, interval: str, limit: int) -> List[Dict[str, Any]]:
    """Fetch raw kline rows for one symbol; returns [{time, close}, ...]. Empty on failure.

    Reuses bot_instance services when available so we ride the bot's existing
    transport + caches instead of opening a new client per request.
    """
    try:
        svc = _macro_align_get_svc(symbol, source)
        if svc is None:
            return []
        if symbol == "BTCUSDT" and source != "hyperliquid":
            # BTC service exposes fetch_klines(interval, limit) — no symbol arg.
            df = svc.fetch_klines(interval=interval, limit=limit)
        else:
            df = svc.fetch_klines(symbol, interval=interval, limit=limit)
        if df is None or df.empty:
            return []
        rows: List[Dict[str, Any]] = []
        for _, r in df.iterrows():
            try:
                ts = int(r["open_time"].timestamp())
                cl = float(r["close"])
                if ts > 0 and cl > 0:
                    rows.append({"time": ts, "close": cl})
            except Exception:
                continue
        return rows
    except Exception as e:
        logger.warning(f"macro_align fetch failed for {symbol}/{source}: {e}")
        return []


_MACRO_ALIGN_CACHE: Dict[str, Any] = {"key": None, "data": None, "expires": 0.0}
_MACRO_ALIGN_CACHE_TTL = 18.0  # seconds — just under the 20s dashboard refresh


@app.get("/api/macro_align/series")
async def get_macro_align_series(interval: str = "15m", limit: int = 120):
    """Per-asset normalized % return series for the cross-asset macro alignment chart.

    Returns a series of `(c - c0)/c0 * 100` for each of the 7 strategy assets, plus
    derived 1H trend, Pearson correlation vs BTC, and last %.  Used by the live
    macro-align panel to render 7 toggleable lines + trade bubbles.

    Hot path: serves cached results within `_MACRO_ALIGN_CACHE_TTL` (~18s) so the
    dashboard's 20s refresh never triggers a new external API call. Bot data is
    untouched.
    """
    interval = interval if interval in ("5m", "15m", "30m", "1h", "4h") else "15m"
    limit = max(20, min(int(limit), 240))

    cache_key = f"{interval}:{limit}"
    now = _time_mod.time()
    if (
        _MACRO_ALIGN_CACHE["key"] == cache_key
        and _MACRO_ALIGN_CACHE["data"] is not None
        and now < _MACRO_ALIGN_CACHE["expires"]
    ):
        return _MACRO_ALIGN_CACHE["data"]

    tasks = [
        asyncio.to_thread(_macro_fetch_series_sync, a["symbol"], a["source"], interval, limit)
        for a in _MACRO_ALIGN_ASSETS
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Align all series to BTC's timestamp grid so % values share an x-axis.
    btc_rows: List[Dict[str, Any]] = raw_results[0] if isinstance(raw_results[0], list) else []
    btc_times = [r["time"] for r in btc_rows]
    btc_index = {t: i for i, t in enumerate(btc_times)}
    btc_norm: List[float] = []
    if btc_rows:
        c0 = float(btc_rows[0]["close"])
        if c0 > 0:
            btc_norm = [(float(r["close"]) - c0) / c0 * 100.0 for r in btc_rows]

    assets_out: Dict[str, Any] = {}
    for asset, rows in zip(_MACRO_ALIGN_ASSETS, raw_results):
        meta = {
            "label": asset["label"],
            "color": asset["color"],
            "source": asset["source"],
            "available": False,
            "series": [],
            "last_pct": None,
            "m1h": "NEUT",
            "corr": None,
            "align": None,
        }
        if not isinstance(rows, list) or not rows:
            assets_out[asset["key"]] = meta
            continue
        c0 = float(rows[0]["close"])
        if c0 <= 0:
            assets_out[asset["key"]] = meta
            continue
        # Align to BTC's grid: produce one value per btc_time, or null if no bar at that ts.
        if btc_times:
            row_by_time = {r["time"]: r for r in rows}
            series: List[Optional[float]] = []
            last_pct: Optional[float] = None
            for t in btc_times:
                r = row_by_time.get(t)
                if r is None:
                    series.append(None)
                else:
                    pct = (float(r["close"]) - c0) / c0 * 100.0
                    series.append(round(pct, 4))
                    last_pct = pct
        else:
            series = [round((float(r["close"]) - c0) / c0 * 100.0, 4) for r in rows]
            last_pct = series[-1] if series else None

        # m1h: slope over the last ~4 hours of bars (interval-aware).
        bars_per_hour = {"5m": 12, "15m": 4, "30m": 2, "1h": 1, "4h": 0.25}.get(interval, 4)
        lookback = max(4, int(bars_per_hour * 4))
        clean = [v for v in series if v is not None]
        if len(clean) >= lookback:
            delta = clean[-1] - clean[-lookback]
            meta["m1h"] = "BULL" if delta > 0.15 else "BEAR" if delta < -0.15 else "NEUT"
        # Pearson vs BTC over aligned overlap.
        if btc_norm and any(v is not None for v in series):
            paired_a, paired_b = [], []
            for i, v in enumerate(series):
                if v is None or i >= len(btc_norm):
                    continue
                paired_a.append(btc_norm[i])
                paired_b.append(v)
            meta["corr"] = round(_macro_pearson(paired_a, paired_b) or 0.0, 3) if len(paired_a) >= 5 else None
            # align ≈ corr * sign-agreement of last-window trend
            if meta["corr"] is not None and len(paired_a) >= lookback:
                btc_dir = paired_a[-1] - paired_a[-lookback]
                alt_dir = paired_b[-1] - paired_b[-lookback]
                sign_match = 1.0 if (btc_dir >= 0) == (alt_dir >= 0) else -1.0
                meta["align"] = round(abs(meta["corr"]) * sign_match, 3)

        meta["available"] = True
        meta["series"] = series
        meta["last_pct"] = round(last_pct, 3) if last_pct is not None else None
        assets_out[asset["key"]] = meta

    payload = {
        "interval": interval,
        "limit": limit,
        "times": btc_times,
        "assets": assets_out,
        "order": [a["key"] for a in _MACRO_ALIGN_ASSETS],
        "cached_at": int(now),
        "cache_ttl": int(_MACRO_ALIGN_CACHE_TTL),
    }
    _MACRO_ALIGN_CACHE["key"] = cache_key
    _MACRO_ALIGN_CACHE["data"] = payload
    _MACRO_ALIGN_CACHE["expires"] = now + _MACRO_ALIGN_CACHE_TTL
    return payload


# ─── CONFIG PANEL ─────────────────────────────────────────────────


@app.get("/api/config")
async def get_config():
    """Return current settings.yaml as JSON, overlaid with effective runtime flags."""
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="settings.yaml not found")
    try:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}
        config["exposure"] = _effective_exposure_section(config)
        return config
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
    lane_management: Optional[Dict[str, Any]] = None

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
                    "exit_rules",
                },
            )
            exit_rules = self.trading.get("exit_rules")
            if exit_rules is not None:
                _validate_section_keys(
                    exit_rules,
                    "trading.exit_rules",
                    {
                        "updown_stop_loss_pct",
                        "updown_hold_winners_to_resolution",
                        "updown_lane_overrides",
                        "updown_overrides",
                    },
                )
                _validate_numeric_range(
                    exit_rules,
                    "trading.exit_rules",
                    "updown_stop_loss_pct",
                    ge=0,
                    le=1,
                )
                _validate_updown_override_patch(exit_rules)
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

        if self.lane_management is not None:
            _validate_section_keys(
                self.lane_management,
                "lane_management",
                {
                    "enabled",
                    "execution_enforcement_enabled",
                    "default_state",
                    "states",
                },
            )
            _validate_bool_fields(
                self.lane_management,
                "lane_management",
                {"enabled", "execution_enforcement_enabled"},
            )
            default_state = self.lane_management.get("default_state")
            if default_state is not None and str(default_state).strip().lower() not in {"paper", "live", "paused"}:
                raise ValueError("lane_management.default_state must be paper, live, or paused")
            states = self.lane_management.get("states")
            if states is not None:
                if not isinstance(states, dict):
                    raise ValueError("lane_management.states must be an object")
                for lane_key, state in states.items():
                    if not str(lane_key).strip():
                        raise ValueError("lane_management.states keys must be non-empty")
                    if str(state).strip().lower() not in {"paper", "live", "paused"}:
                        raise ValueError(
                            f"lane_management.states.{lane_key} must be paper, live, or paused"
                        )

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
                "doge_macro",
                "bnb_macro",
            }
            _validate_section_keys(self.strategies, "strategies", allowed_strategies)
            allowed_strategy_fields = {
                "enabled",
                "use_ai",
                "resolution_window_enabled",
                "min_edge",
                "entry_price_min",
                "entry_price_max",
                "kelly_fraction",
                "ai_confidence_threshold",
            }
            bool_fields = {"enabled", "use_ai", "resolution_window_enabled"}
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


def _validate_updown_override_patch(exit_rules: Dict[str, Any]) -> None:
    def _validate_override_map(section: Dict[str, Any], section_name: str) -> None:
        _validate_section_keys(
            section,
            section_name,
            {
                "updown_stop_loss_pct",
                "updown_stop_cents",
                "updown_exit_window_mins",
                "updown_max_hold_mins",
                "updown_exit_window_max_fraction",
                "updown_stop_cents_high_entry",
                "updown_high_entry_threshold",
                "updown_in_profit_stop_trigger_pct",
                "updown_in_profit_stop_tighten_to_pct",
                "updown_hold_winners_to_resolution",
                "lane_overrides",
                "window_lane_overrides",
            },
        )
        for key in (
            "updown_stop_loss_pct",
            "updown_stop_cents",
            "updown_exit_window_mins",
            "updown_max_hold_mins",
            "updown_exit_window_max_fraction",
            "updown_stop_cents_high_entry",
            "updown_high_entry_threshold",
            "updown_in_profit_stop_trigger_pct",
            "updown_in_profit_stop_tighten_to_pct",
        ):
            _validate_numeric_range(section, section_name, key, ge=0, le=1 if key.endswith("_pct") or key.endswith("_fraction") else None)
        lane_overrides = section.get("lane_overrides") or {}
        if lane_overrides:
            _validate_section_keys(lane_overrides, f"{section_name}.lane_overrides", {"up", "down"})
            for lane, lane_cfg in lane_overrides.items():
                _validate_override_map(lane_cfg, f"{section_name}.lane_overrides.{lane}")
        window_lane_overrides = section.get("window_lane_overrides") or {}
        if window_lane_overrides:
            if not isinstance(window_lane_overrides, dict):
                raise ValueError(f"{section_name}.window_lane_overrides must be an object")
            for window, window_cfg in window_lane_overrides.items():
                if str(window) not in {"5m", "15m", "30m", "1h"}:
                    raise ValueError(f"{section_name}.window_lane_overrides.{window} must be one of 5m, 15m, 30m, 1h")
                _validate_section_keys(window_cfg, f"{section_name}.window_lane_overrides.{window}", {"up", "down"})
                for lane, lane_cfg in window_cfg.items():
                    _validate_override_map(
                        lane_cfg,
                        f"{section_name}.window_lane_overrides.{window}.{lane}",
                    )

    lane_overrides = exit_rules.get("updown_lane_overrides") or {}
    if lane_overrides:
        _validate_section_keys(lane_overrides, "trading.exit_rules.updown_lane_overrides", {"up", "down"})
        for lane, lane_cfg in lane_overrides.items():
            _validate_override_map(lane_cfg, f"trading.exit_rules.updown_lane_overrides.{lane}")
    strategy_overrides = exit_rules.get("updown_overrides") or {}
    if strategy_overrides:
        if not isinstance(strategy_overrides, dict):
            raise ValueError("trading.exit_rules.updown_overrides must be an object")
        for strategy, strategy_cfg in strategy_overrides.items():
            _validate_override_map(strategy_cfg, f"trading.exit_rules.updown_overrides.{strategy}")


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
        handler, config = _load_settings_config()
        _deep_merge(config, updates_dict)
        _save_settings_config(handler, config)
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


class LaneStateUpdateRequest(BaseModel):
    lane_id: str = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    source: str = Field("dashboard_manual", min_length=1)
    note: Optional[str] = None


def _normalize_lane_state_value(value: str) -> str:
    state = str(value or "").strip().lower()
    if state not in {"paper", "live", "paused", "default"}:
        raise ValueError("state must be one of: paper, live, paused, default")
    return state


@app.post("/api/lane-state")
async def update_lane_state(request: Request):
    _check_auth(request)
    try:
        payload = LaneStateUpdateRequest.model_validate(await request.json())
        lane_id = str(payload.lane_id).strip()
        state = _normalize_lane_state_value(payload.state)
        source = str(payload.source or "dashboard_manual").strip() or "dashboard_manual"
        note = str(payload.note or "").strip()
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json()))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="settings.yaml not found")

    try:
        handler, config = _load_settings_config()
        lane_cfg = config.setdefault("lane_management", {})
        lane_cfg.setdefault("default_state", "paper")
        states = lane_cfg.setdefault("states", {})
        state_meta = lane_cfg.setdefault("state_meta", {})
        if not isinstance(states, dict):
            raise ValueError("lane_management.states must be an object")
        if not isinstance(state_meta, dict):
            raise ValueError("lane_management.state_meta must be an object")
        updated_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        if state == "default":
            states.pop(lane_id, None)
        else:
            lane_cfg["enabled"] = True
            states[lane_id] = state
        previous_meta = dict(state_meta.get(lane_id) or {})
        previous_state = previous_meta.get("last_effective_state", states.get(lane_id, lane_cfg.get("default_state", "paper")))
        next_meta = {
            **previous_meta,
            "updated_at": updated_at,
            "updated_via": source,
            "reviewed_at": updated_at,
            "review_note": note,
            "last_requested_state": state,
            "last_effective_state": states.get(lane_id, lane_cfg.get("default_state", "paper")),
        }
        state_meta[lane_id] = next_meta
        _save_settings_config(handler, config)
        audit_row = {
            "timestamp": updated_at,
            "lane_id": lane_id,
            "requested_state": state,
            "effective_state": states.get(lane_id, lane_cfg.get("default_state", "paper")),
            "previous_state": previous_state,
            "source": source,
            "note": note,
        }
        _append_jsonl_record(LANE_STATE_AUDIT_LOG, audit_row)

        updates = {
            "lane_management": {
                "enabled": bool(lane_cfg.get("enabled", False)),
                "default_state": str(lane_cfg.get("default_state") or "paper"),
                "states": dict(states),
                "state_meta": dict(state_meta),
            }
        }
        live_apply_ok = True
        live_apply_error = None
        bot = _full_bot_instance()
        if bot is not None:
            try:
                bot.apply_config_updates(updates)
            except Exception as e:
                logger.error("Live lane state apply failed: %s", e, exc_info=True)
                live_apply_ok = False
                live_apply_error = str(e)
        return {
            "status": "saved",
            "lane_id": lane_id,
            "state": state,
            "effective_state": states.get(lane_id, lane_cfg.get("default_state", "paper")),
            "state_meta": next_meta,
            "audit_row": audit_row,
            "live_apply": live_apply_ok,
            **({"live_apply_error": live_apply_error} if live_apply_error else {}),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Lane state save error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
    global _journal_cache

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
        _exit_reason_summary_cache.clear()
        _action_breakdown_cache.clear()

    logging.info(
        f"[dashboard] Paper session reset → session_id={new_id}, bankroll=${new_bankroll:,.2f}"
    )
    out: Dict[str, Any] = {
        "status": "ok",
        "new_session_id": new_id,
        "bankroll": new_bankroll,
    }
    # Auto-backtest on reset removed 2026-05-24 with the broken backtester.
    if archive_rel:
        out["archived_to"] = archive_rel
    return out
