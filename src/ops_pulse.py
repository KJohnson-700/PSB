"""
Structured operational logging for hosts that capture stdout (Docker, systemd, PaaS).

Every pulse is one line prefixed with OPS_JSON so you can filter:

  <host log command> | findstr OPS_JSON

or ingest into log platforms as JSON.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.analysis import window_watch as _window_watch


def _window_watch_ops_stats(bot: Any) -> Dict[str, Any]:
    """PHASE 5: near-window registry stats for OPS_JSON (fail-safe — never breaks pulse)."""
    try:
        return _window_watch.registry_stats(getattr(bot, "config", {}) or {})
    except Exception:  # noqa: BLE001
        return {}

from src.ai_status import compute_ai_status

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
        "doge_macro",
        "bnb_macro",
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


def _decision_gate_digest(config: Dict[str, Any], ai_scan_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Decision-control config + current block telemetry for dashboard operators."""
    cfg = config or {}
    strategies = cfg.get("strategies") or {}
    ai_cfg = (cfg.get("ai") or {}).get("decision_layer") or {}
    composite = cfg.get("updown_composite") or {}
    # Alts first so dashboard/API consumers list macro lanes before BTC-only controls.
    lanes = (
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "doge_macro",
        "bnb_macro",
        "bitcoin",
    )
    oracle_block_keys = {"oracle_missing", "oracle_stale", "oracle_basis_block"}
    control_prefixes = ("ai_decision_",)
    control_keys = {
        "composite_score_below_floor",
        "ai_unavailable_neutral_15m",
        "ai_call_limit_neutral_15m",
        "ai_nonpositive_edge_neutral_15m",
        "neutral_15m_low_conf_no_ai",
    }

    enforced_cfg = ai_cfg.get("enforced_lanes") if isinstance(ai_cfg, dict) else {}
    if not isinstance(enforced_cfg, dict):
        enforced_cfg = {}
    shadow_required_cfg = ai_cfg.get("shadow_required_lanes") if isinstance(ai_cfg, dict) else {}
    if not isinstance(shadow_required_cfg, dict):
        shadow_required_cfg = {}

    active_blocks: Dict[str, Dict[str, int]] = {}
    gate_scores: Dict[str, Any] = {}
    lane_payload: Dict[str, Any] = {}
    for lane in lanes:
        scfg = strategies.get(lane) or {}
        enforced = enforced_cfg.get(lane) or []
        if not isinstance(enforced, list):
            enforced = list(enforced) if isinstance(enforced, (tuple, set)) else []
        shadow_required = shadow_required_cfg.get(lane) or []
        if not isinstance(shadow_required, list):
            shadow_required = (
                list(shadow_required) if isinstance(shadow_required, (tuple, set)) else []
            )
        stats = ai_scan_stats.get(lane) or {}
        skips = stats.get("top_skip_reasons") or {}
        filtered: Dict[str, int] = {}
        for key, value in skips.items():
            key_s = str(key)
            if (
                key_s in oracle_block_keys
                or key_s in control_keys
                or key_s.startswith(control_prefixes)
            ):
                filtered[key_s] = _coerce_skip_count(value)
        if filtered:
            active_blocks[lane] = dict(sorted(filtered.items(), key=lambda kv: kv[1], reverse=True))

        dist = (stats.get("gate_distributions") or {}).get("composite_score")
        if dist:
            gate_scores[lane] = dist

        oracle_required = bool(scfg.get("require_oracle_for_updown", False))
        oracle_payload = {
            "required": oracle_required,
            "max_age_sec": scfg.get("oracle_max_age_sec"),
            "max_basis_bps": scfg.get("oracle_max_basis_bps"),
        }
        if lane == "bitcoin":
            oracle_payload = {"required": False, "note": "BTC neutral gate only"}

        lane_payload[lane] = {
            "enabled": bool(scfg.get("enabled", False)),
            "enforced_lanes": [str(item) for item in enforced],
            "shadow_required_lanes": [str(item) for item in shadow_required],
            "oracle": oracle_payload,
            "composite_floor": (
                scfg.get("neutral_15m_min_composite_score") if lane == "bitcoin" else None
            ),
            "shadow_required": bool(
                shadow_required
                or (
                    lane == "bitcoin"
                    and scfg.get("neutral_15m_requires_shadow_portfolio", False)
                )
            ),
        }

    return {
        "enabled": bool(ai_cfg.get("enabled", False)),
        "min_confidence": ai_cfg.get("min_confidence", 0.60),
        "hard_skip_if_unavailable": bool(ai_cfg.get("hard_skip_if_unavailable_on_enforced", False)),
        "shadow_global": bool(ai_cfg.get("use_shadow_portfolio", False)),
        "floors": {
            "default_min_score": composite.get("default_min_score"),
            "btc_neutral_15m_min_score": composite.get("btc_neutral_15m_min_score"),
            "low_confidence_min_score": composite.get("low_confidence_min_score"),
        },
        "lanes": lane_payload,
        "active_blocks": active_blocks,
        "gate_scores": gate_scores,
    }


def _buy_no_skip_digest(ai_scan_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Dedicated BUY_NO suppression counters and last sample per strategy."""
    # Alts first so dashboard/API consumers list macro lanes before BTC-only controls.
    lanes = (
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "doge_macro",
        "bnb_macro",
        "bitcoin",
    )
    per_lane: Dict[str, Dict[str, int]] = {}
    last_samples: Dict[str, Dict[str, Any]] = {}
    totals: Dict[str, int] = {}
    for lane in lanes:
        block = ai_scan_stats.get(lane) or {}
        counts = block.get("buy_no_skip_counts") or {}
        if counts:
            normalized = [(k, _coerce_skip_count(v)) for k, v in counts.items()]
            ordered = sorted(normalized, key=lambda kv: kv[1], reverse=True)[:10]
            per_lane[lane] = {k: v for k, v in ordered}
            for k, v in ordered:
                totals[k] = totals.get(k, 0) + v
        sample = block.get("last_buy_no_skip_sample") or {}
        if sample:
            last_samples[lane] = sample
    top_totals = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:20]
    return {
        "per_strategy": per_lane,
        "aggregate_top": {k: v for k, v in top_totals},
        "last_samples": last_samples,
    }


def _coerce_nonnegative_int(v: Any) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _tail_lines(path: Path, max_lines: int, max_bytes_cap: int = 33_000_000) -> list[str]:
    """Return the last ``max_lines`` non-empty lines of ``path``, reading only from
    the END of the file and growing the read until enough complete lines are found
    (or the whole file / ``max_bytes_cap`` is read). The read size tracks the byte
    span of the last ``max_lines`` lines — independent of total file size — so a
    growing append-only log never forces a full-file scan."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size == 0:
                return []
            read_size = min(size, 262_144)
            while True:
                fh.seek(size - read_size)
                chunk = fh.read(read_size)
                parts = chunk.decode("utf-8", errors="replace").split("\n")
                truncated = read_size < size
                # drop the leading partial line when we didn't read from the start.
                # when truncated with no newline in the chunk (a single line larger
                # than the read) this yields [] -> the loop grows and re-reads, so an
                # oversized final line is never returned truncated.
                usable = parts[1:] if truncated else parts
                lines = [p for p in (s.strip() for s in usable) if p]
                if len(lines) >= max_lines or read_size >= size or read_size >= max_bytes_cap:
                    return lines[-max_lines:] if len(lines) > max_lines else lines
                read_size = min(size, read_size * 4, max_bytes_cap)
    except Exception:
        return []


def _iter_recent_ops_pulses(limit: int = 120) -> list[Dict[str, Any]]:
    """Read the last N structured ops pulses from disk, best-effort.

    2026-07-27 MEM-CHURN FIX: previously read the ENTIRE ops_pulse.jsonl (~92MB
    and growing all session) on EVERY cycle just to keep the last ``limit`` lines.
    memray attributed ~500GB of allocation churn/session to this scan, which
    fragmented native RSS (the balloon). Now reads only the bounded file tail —
    identical result (same last ``limit`` structured pulses)."""
    if limit <= 0 or not OPS_PULSE_FILE.exists():
        return []
    try:
        rows: list[Dict[str, Any]] = []
        for line in _tail_lines(OPS_PULSE_FILE, limit):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows
    except Exception:
        return []


def _ai_pipeline_digest(ai_scan_stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    Per-lane and aggregate counters for optional AI pipeline instrumentation.

    Supports both historical names (e.g. ai_calls) and newer names
    (ai_assists/ai_overrides/ai_pipeline_calls) so ops stays backward-compatible.
    """
    lanes = (
        "bitcoin",
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "doge_macro",
        "bnb_macro",
    )
    aliases = {
        "ai_pipeline_calls": ("ai_pipeline_calls", "ai_calls"),
        "ai_assists": ("ai_assists", "research_plans_logged"),
        "ai_overrides": ("ai_overrides",),
        "research_calls": ("research_calls",),
        "shadow_pipeline_calls": ("shadow_pipeline_calls",),
        "shadow_pipeline_ok": ("shadow_pipeline_ok",),
        "shadow_observer_calls": ("shadow_observer_calls",),
        "shadow_observer_ok": ("shadow_observer_ok",),
        "shadow_marginal_mismatch": ("shadow_marginal_mismatch",),
    }
    per_lane: Dict[str, Dict[str, int]] = {}
    aggregate: Dict[str, int] = {k: 0 for k in aliases}

    for lane in lanes:
        block = ai_scan_stats.get(lane) or {}
        lane_counts: Dict[str, int] = {}
        for canonical, keys in aliases.items():
            val = 0
            for key in keys:
                if key in block:
                    val = _coerce_nonnegative_int(block.get(key))
                    break
            if val > 0:
                lane_counts[canonical] = val
                aggregate[canonical] += val
        if lane_counts:
            per_lane[lane] = lane_counts

    aggregate_nonzero = {k: v for k, v in aggregate.items() if v > 0}
    return {
        "per_strategy": per_lane,
        "aggregate": aggregate_nonzero,
    }


def _side_selection_digest(ai_scan_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Show whether strategies are selecting a short/NO side before BUY_NO filters run."""
    lanes = (
        "bitcoin",
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
        "doge_macro",
        "bnb_macro",
    )
    per_lane: Dict[str, Dict[str, Any]] = {}
    aggregate = {"LONG": 0, "SHORT": 0, "unknown": 0}
    for lane in lanes:
        block = ai_scan_stats.get(lane) or {}
        side = str(block.get("allowed_side") or "").upper()
        side_counts = block.get("side_source_counts") or {}
        signals = _coerce_nonnegative_int(block.get("signals"))
        if side in ("LONG", "SHORT"):
            aggregate[side] += 1
        else:
            aggregate["unknown"] += 1
        per_lane[lane] = {
            "allowed_side": side or None,
            "signals": signals,
            "side_source_counts": side_counts,
            "buy_yes_possible_this_pulse": side == "LONG",
            "buy_no_possible_this_pulse": side == "SHORT",
        }
    recent = _iter_recent_ops_pulses(limit=120)
    recent_counts: Dict[str, Dict[str, int]] = {}
    for lane in lanes:
        counts: Counter[str] = Counter()
        for row in recent:
            hist = (((row.get("side_selection") or {}).get("per_strategy") or {}).get(lane) or {})
            hist_side = str(hist.get("allowed_side") or "").upper()
            if hist_side in ("LONG", "SHORT"):
                counts[hist_side] += 1
        current_side = str((per_lane.get(lane) or {}).get("allowed_side") or "").upper()
        if current_side in ("LONG", "SHORT"):
            counts[current_side] += 1
        recent_counts[lane] = {"LONG": int(counts.get("LONG", 0)), "SHORT": int(counts.get("SHORT", 0))}
    long_lanes = [lane for lane, data in per_lane.items() if data["allowed_side"] == "LONG"]
    short_lanes = [lane for lane, data in per_lane.items() if data["allowed_side"] == "SHORT"]
    return {
        "per_strategy": per_lane,
        "aggregate": aggregate,
        "long_lanes": long_lanes,
        "short_lanes": short_lanes,
        "recent_side_rollup": {
            "lookback_pulses": len(recent) + 1,
            "per_strategy": recent_counts,
        },
        "buy_no_absence_reason": (
            "No strategy selected SHORT/NO side in this pulse; BUY_NO filters and "
            "execution were not exercised."
            if not short_lanes
            else ""
        ),
    }


def _ai_activity_note(ai_status: Dict[str, Any], ai_pipeline: Dict[str, Any]) -> str:
    """Human-readable guardrail so zero call counters are not mistaken for disabled AI."""
    if not ai_status.get("ready"):
        return f"AI unavailable: {ai_status.get('reason', 'unknown')}"
    if not ai_status.get("live_inferencing", True):
        return "AI configured but live_inferencing is paused."
    aggregate = ai_pipeline.get("aggregate") or {}
    calls = int(aggregate.get("ai_pipeline_calls") or 0)
    research = int(aggregate.get("research_calls") or 0)
    shadow = int(aggregate.get("shadow_pipeline_calls") or 0)
    if calls or research or shadow:
        return "AI enabled and called in this pulse."
    return (
        "AI enabled and keys loaded; zero calls means no candidate reached an AI "
        "decision path in this pulse."
    )


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
    """HTTPS base URL for the dashboard when the host platform exposes a public domain env var."""
    d = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL")
    if not d:
        return None
    d = d.strip().rstrip("/")
    if d.startswith("http://") or d.startswith("https://"):
        return d
    return f"https://{d}"


def _calibration_scope_digest(config: Dict[str, Any]) -> Dict[str, Any]:
    """Surface trading.calibration_scope so non-BTC silence reads as intentional.

    When enabled, only `execution_strategies` create new entries; every other
    lane's scan-task is skipped in the cycle. Fail-safe: never breaks the pulse.
    """
    try:
        scope = ((config or {}).get("trading") or {}).get("calibration_scope") or {}
        enabled = bool(scope.get("enabled"))
        return {
            "enabled": enabled,
            "mode": scope.get("mode") if enabled else None,
            "execution_strategies": list(scope.get("execution_strategies") or [])
            if enabled
            else [],
            "execution_windows": list(scope.get("execution_windows") or [])
            if enabled
            else [],
        }
    except Exception:
        return {"enabled": False}


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
    ai_keys = {}
    try:
        ai_keys = dict(getattr(getattr(bot, "ai_agent", None), "api_keys", None) or {})
    except Exception:
        ai_keys = {}
    ai_status = compute_ai_status(getattr(bot, "config", {}) or {}, ai_keys)
    ai_pipeline = _ai_pipeline_digest(ai_scan_stats)
    side_selection = _side_selection_digest(ai_scan_stats)
    btc_block = ai_scan_stats.get("bitcoin") or {}
    btc_spot = btc_block.get("btc_spot_usd")
    try:
        btc_spot_f = float(btc_spot) if btc_spot is not None else None
    except (TypeError, ValueError):
        btc_spot_f = None

    # 2026-07-20 BANKROLL ACCURACY FIX (operator-reported "negative P&L but bankroll up";
    # Codex-reviewed — do NOT rebind the `bankroll` field's meaning).
    # `bot.bankroll` is the SIZING CASH — initial + REALIZED pnl only (main.py:2787 adds
    # realized per closed trade; Kelly reads it at main.py:4029-4101 for base_size =
    # edge*frac*bankroll, and it must stay realized-only so open mark-to-market never
    # inflates position size). This SAME builder feeds both /api/ops/summary AND the JSONL
    # ops log, and downstream parsers/alerts read `bankroll` expecting sizing cash — so
    # `bankroll` KEEPS its meaning. We ADD `equity` (= cash + unrealized, the true account
    # value that ties to total_pnl and to /api/status) and `cash_bankroll` (alias for
    # clarity). The dashboard hero reads `equity`. Bad accounting fails LOUD (equity=None +
    # accounting_error) rather than silently showing cash as equity.
    _cash_bankroll = round(float(getattr(bot, "bankroll", 0) or 0), 4)
    _equity = None
    _accounting_error = None
    try:
        _unreal = float(summary["unrealized_pnl"] or 0)
        # In LIVE, bot.bankroll is refreshed to venue EQUITY (cash + open positions, set by
        # refresh_live_wallet_bankroll → bankroll_source="live_wallet"), so unrealized is
        # ALREADY inside it — adding it again double-counts (2026-07-29 fix). In paper,
        # bankroll is sizing CASH, so equity = cash + unrealized.
        _bankroll_is_equity = getattr(bot, "bankroll_source", None) == "live_wallet"
        _equity = round(_cash_bankroll if _bankroll_is_equity else _cash_bankroll + _unreal, 4)
    except (KeyError, TypeError, ValueError):
        _accounting_error = "invalid_unrealized_pnl"
    return {
        "event": "ops_pulse",
        "ts": datetime.now(timezone.utc).isoformat(),
        "loop": loop,
        "session_id": getattr(bot.journal, "session_id", None),
        "rolled_from": getattr(bot, "_rolled_from", None),   # 2026-08-18 hot session rollover marker
        "rollover_draining": bool(getattr(bot, "_rollover_draining", False)),
        "journal_dir": session_dir,
        "dry_run": bool(trading.get("dry_run", True)),
        "kill_switch": bool(bot._kill_switch_active()) if hasattr(bot, "_kill_switch_active") else False,
        "running": bool(getattr(bot, "running", False)),
        "bankroll": _cash_bankroll,       # UNCHANGED: sizing cash (initial + realized) — what Kelly reads
        "cash_bankroll": _cash_bankroll,  # explicit alias for the same sizing cash
        "equity": _equity,                # NEW: true account value = cash + unrealized (matches /api/status; hero reads this)
        "accounting_error": _accounting_error,  # non-null when equity could not be computed
        "open_positions": summary.get("open_positions", 0),
        "closed_trades": summary.get("total_exits", 0),
        "total_entries": summary.get("total_entries", 0),
        "realized_pnl": summary.get("realized_pnl", 0),
        "unrealized_pnl": summary.get("unrealized_pnl", 0),
        "total_pnl": summary.get("total_pnl", 0),
        "daily_trades": getattr(rm, "daily_trades", 0) if rm else 0,
        "daily_pnl": round(float(getattr(rm, "daily_pnl", 0) or 0), 4) if rm else 0.0,
        "exposure_loss_kill_enabled": bool(getattr(em0, "loss_kill_active", False))
        if em0 is not None
        else None,
        "exposure_loss_kill_configured": bool(
            getattr(em0, "loss_kill_switch_enabled", True)
        )
        if em0 is not None
        else None,
        "exposure_loss_kill_apply_in_paper": bool(
            getattr(em0, "loss_kill_apply_in_paper", False)
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
        "decision_gates": _decision_gate_digest(getattr(bot, "config", {}) or {}, ai_scan_stats),
        "buy_no_skip_diagnostics": _buy_no_skip_digest(ai_scan_stats),
        "side_selection": side_selection,
        "rejected_candidate_tracker": dict(
            getattr(bot, "ghost_calibration_status", {}) or {}
        ),
        "ghost_calibration": dict(
            getattr(bot, "ghost_calibration_status", {}) or {}
        ),
        "ai_status": ai_status,
        "ai_pipeline": ai_pipeline,
        "ai_activity_note": _ai_activity_note(ai_status, ai_pipeline),
        "timestamps_policy": {
            "canonical": CANONICAL_OPS_TIMEZONE,
            "ops_ts": "ISO 8601 with Z/offset; this field is UTC",
            "note": "Journal/log lines may use mixed TZ — compare using ops_ts or convert explicitly",
        },
        "regime": _regime_hint(trading, btc_spot_f),
        "calibration_scope": _calibration_scope_digest(getattr(bot, "config", {}) or {}),
        "window_watch": _window_watch_ops_stats(bot),
        "scan_interval_sec": getattr(bot, "scan_interval", None),
        "dashboard_url": public_dashboard_url(),
    }


def log_ops_pulse(bot: Any, loop: str) -> None:
    """Emit one OPS_JSON line to the root logger (stdout)."""
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
            "hint": "Filter logs by grepping OPS_JSON  —  API: {url}/api/ops/summary".format(
                url=public_dashboard_url() or "http://localhost:$PORT"
            ),
        }
        line = json.dumps(payload, separators=(",", ":"), default=str)
        logging.info("%s %s", OPS_PREFIX, line)
        _append_ops_file(bot, line)
    except Exception as e:
        logging.warning("ops_start failed: %s", e)


def _maybe_rotate_ops_file(cap_mb: float) -> None:
    """Gzip-rotate the ops-pulse file when it exceeds cap_mb, then start fresh.

    The bot reopens this file on every append (no long-lived handle), so rotating
    here is safe: we gzip the full file into data/logs/archive/ and unlink the
    original; the next append recreates it. Reads are tail-bounded
    (_iter_recent_ops_pulses), so only recent rows matter live — older rows live
    in the compressed archive for offline inspection / the data lifecycle job.
    """
    try:
        if not OPS_PULSE_FILE.exists():
            return
        if OPS_PULSE_FILE.stat().st_size < cap_mb * 1024 * 1024:
            return
        import gzip
        import shutil

        archive_dir = OPS_PULSE_FILE.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest = archive_dir / f"ops_pulse.{stamp}.jsonl.gz"
        with OPS_PULSE_FILE.open("rb") as src, gzip.open(dest, "wb") as gz:
            shutil.copyfileobj(src, gz)
        OPS_PULSE_FILE.unlink()
    except Exception as e:
        logging.warning("ops_pulse rotation failed: %s", e)


def _append_ops_file(bot: Any, line: str) -> None:
    """Persist OPS_JSON lines to a dedicated JSONL file for offline inspection."""
    if not getattr(bot, "config", None):
        return
    log_cfg = bot.config.get("logging", {})
    if not log_cfg.get("ops_pulse_file", True):
        return
    try:
        OPS_PULSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate_ops_file(float(log_cfg.get("ops_pulse_max_mb", 100) or 100))
        with OPS_PULSE_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        logging.warning("ops_pulse file write failed: %s", e)
