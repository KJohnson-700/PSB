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
