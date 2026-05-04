"""
Structured operational logging for hosts that capture stdout (Railway, Docker, systemd).

Every pulse is one line prefixed with OPS_JSON so you can filter:

  railway logs | findstr OPS_JSON

or ingest into log platforms as JSON.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

OPS_PREFIX = "OPS_JSON"
OPS_PULSE_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "logs" / "ops_pulse.jsonl"
)

# Canonical clock for ops snapshots (ISO UTC). Logs may mix formats — see docs/PSB_TIMEZONE_POLICY.md.
CANONICAL_OPS_TIMEZONE = "UTC"


def _coerce_skip_count(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _scan_skip_digest(ai_scan_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Per-lane top_skip_reasons for dashboard / OPS_JSON (no log grep)."""
    lanes = (
        "bitcoin",
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "weather",
    )
    per_lane: Dict[str, Any] = {}
    for lane in lanes:
        block = ai_scan_stats.get(lane) or {}
        skips = block.get("top_skip_reasons") or {}
        if skips:
            normalized = [(k, _coerce_skip_count(v)) for k, v in skips.items()]
            ordered = sorted(normalized, key=lambda kv: kv[1], reverse=True)[:10]
            per_lane[lane] = {k: v for k, v in ordered}
    totals: Dict[str, int] = {}
    for skips in per_lane.values():
        for k, v in skips.items():
            totals[k] = totals.get(k, 0) + v
    top_totals = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:20]
    return {
        "per_strategy": per_lane,
        "aggregate_top": {k: v for k, v in top_totals},
    }


def _regime_hint(trading_cfg: Dict[str, Any], btc_spot: Optional[float]) -> Optional[Dict[str, Any]]:
    rcfg = (trading_cfg or {}).get("regime") or {}
    if not rcfg.get("enabled"):
        return None
    hi = rcfg.get("btc_break_above_usd")
    lo = rcfg.get("btc_break_below_usd")
    out: Dict[str, Any] = {
        "enabled": True,
        "btc_break_above_usd": hi,
        "btc_break_below_usd": lo,
        "btc_spot_usd": btc_spot,
    }
    if btc_spot is not None and hi is not None:
        try:
            out["spot_gte_break_high"] = btc_spot >= float(hi)
        except (TypeError, ValueError):
            pass
    if btc_spot is not None and lo is not None:
        try:
            out["spot_lte_break_low"] = btc_spot <= float(lo)
        except (TypeError, ValueError):
            pass
    return out


def public_dashboard_url() -> Optional[str]:
    """HTTPS base URL for the dashboard when the platform sets a public domain (e.g. Railway)."""
    d = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL")
    if not d:
        return None
    d = d.strip().rstrip("/")
    if d.startswith("http://") or d.startswith("https://"):
        return d
    return f"https://{d}"


def build_ops_snapshot(bot: Any, loop: str) -> Dict[str, Any]:
    """Machine-readable snapshot for logs and /api/ops/summary."""
    trading = bot.config.get("trading", {}) if getattr(bot, "config", None) else {}
    summary = {}
    try:
        summary = bot.journal.get_summary()
    except Exception as e:
        summary = {"error": str(e)}

    session_dir = ""
    try:
        session_dir = str(bot.journal.session_dir)
    except Exception:
        pass

    rm = getattr(bot, "risk_manager", None)
    em0 = getattr(bot, "btc_exposure_manager", None)
    last_counts = dict(getattr(bot, "last_signal_counts", {}) or {})
    cum = dict(getattr(bot, "cumulative_signal_counts", {}) or {})
    last_cycles = dict(getattr(bot, "last_cycle_times", {}) or {})
    ai_scan_stats = dict(getattr(bot, "last_ai_scan_stats", {}) or {})
    btc_block = ai_scan_stats.get("bitcoin") or {}
    btc_spot = btc_block.get("btc_spot_usd")
    try:
        btc_spot_f = float(btc_spot) if btc_spot is not None else None
    except (TypeError, ValueError):
        btc_spot_f = None

    return {
        "event": "ops_pulse",
        "ts": datetime.now(timezone.utc).isoformat(),
        "loop": loop,
        "session_id": getattr(bot.journal, "session_id", None),
        "journal_dir": session_dir,
        "dry_run": bool(trading.get("dry_run", True)),
        "kill_switch": bool(bot._kill_switch_active()) if hasattr(bot, "_kill_switch_active") else False,
        "running": bool(getattr(bot, "running", False)),
        "bankroll": round(float(getattr(bot, "bankroll", 0) or 0), 4),
        "open_positions": summary.get("open_positions", 0),
        "closed_trades": summary.get("total_exits", 0),
        "total_entries": summary.get("total_entries", 0),
        "realized_pnl": summary.get("realized_pnl", 0),
        "unrealized_pnl": summary.get("unrealized_pnl", 0),
        "total_pnl": summary.get("total_pnl", 0),
        "daily_trades": getattr(rm, "daily_trades", 0) if rm else 0,
        "daily_pnl": round(float(getattr(rm, "daily_pnl", 0) or 0), 4) if rm else 0.0,
        "exposure_loss_kill_enabled": bool(
            getattr(em0, "loss_kill_switch_enabled", True)
        )
        if em0 is not None
        else None,
        "exposure_max_consecutive_losses": getattr(
            em0, "max_consecutive_losses", 3
        )
        if em0 is not None
        else None,
        "last_signal_counts": last_counts,
        "cumulative_signal_counts": cum,
        "last_cycle_times": last_cycles,
        "ai_scan_stats": ai_scan_stats,
        "scan_skip_digest": _scan_skip_digest(ai_scan_stats),
        "timestamps_policy": {
            "canonical": CANONICAL_OPS_TIMEZONE,
            "ops_ts": "ISO 8601 with Z/offset; this field is UTC",
            "note": "Journal/log lines may use mixed TZ — compare using ops_ts or convert explicitly",
        },
        "regime": _regime_hint(trading, btc_spot_f),
        "scan_interval_sec": getattr(bot, "scan_interval", None),
        "dashboard_url": public_dashboard_url(),
    }


def log_ops_pulse(bot: Any, loop: str) -> None:
    """Emit one OPS_JSON line to the root logger (stdout on Railway)."""
    if not getattr(bot, "config", None):
        return
    if not bot.config.get("logging", {}).get("ops_pulse", True):
        return
    try:
        payload = build_ops_snapshot(bot, loop)
        line = json.dumps(payload, separators=(",", ":"), default=str)
        logging.info("%s %s", OPS_PREFIX, line)
        _append_ops_file(bot, line)
    except Exception as e:
        logging.warning("ops_pulse failed: %s", e)


def log_ops_startup(bot: Any) -> None:
    """Startup line: session, paths, URLs (same OPS_JSON filter)."""
    if not getattr(bot, "config", None):
        return
    if not bot.config.get("logging", {}).get("ops_pulse", True):
        return
    try:
        session_dir = str(getattr(bot.journal, "session_dir", ""))
        payload = {
            "event": "ops_start",
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": getattr(bot.journal, "session_id", None),
            "journal_dir": session_dir,
            "entries_file": str(getattr(bot.journal, "_entries_file", "")),
            "dry_run": bool(bot.config.get("trading", {}).get("dry_run", True)),
            "dashboard_url": public_dashboard_url(),
            "hint": "Filter logs: railway logs | findstr OPS_JSON  —  API: {url}/api/ops/summary".format(
                url=public_dashboard_url() or "(set PORT or RAILWAY_PUBLIC_DOMAIN)"
            ),
        }
        line = json.dumps(payload, separators=(",", ":"), default=str)
        logging.info("%s %s", OPS_PREFIX, line)
        _append_ops_file(bot, line)
    except Exception as e:
        logging.warning("ops_start failed: %s", e)


def _append_ops_file(bot: Any, line: str) -> None:
    """Persist OPS_JSON lines to a dedicated JSONL file for offline inspection."""
    if not getattr(bot, "config", None):
        return
    if not bot.config.get("logging", {}).get("ops_pulse_file", True):
        return
    try:
        OPS_PULSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with OPS_PULSE_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        logging.warning("ops_pulse file write failed: %s", e)
