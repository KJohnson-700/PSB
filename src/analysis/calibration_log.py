"""Phase 0 calibration log.

Appends one JSON object per closed trade to ``data/calibration/trades.jsonl``.
Purpose: build up the dataset that Phase 6 (per-lane probability calibration)
will read from. This module has no behavior side-effects — write-only — and
never throws into the caller; logging failures degrade silently with a warning.

The schema is intentionally additive: Phase 6 will populate
``calibrated_est_prob`` / ``alpha_used`` / ``posterior_*`` with real values.
Until then they default to ``stated_est_prob`` / ``1.0`` / ``None``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.analysis.calibration_buckets import build_bucket_tags

logger = logging.getLogger(__name__)

# Centralised across-session log so calibration_report.py can aggregate easily.
DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_TRADES_LOG = DEFAULT_CALIBRATION_DIR / "trades.jsonl"
CALIBRATION_SCHEMA_VERSION = 3  # 2026-07-30: +fill economics, raw/exec PnL split, entry executability


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        f = float(value)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _resolve_side(closed: Dict[str, Any]) -> str:
    """Return BUY_YES / BUY_NO from closed-trade record."""
    action = str(closed.get("action") or "").strip().upper()
    if action in ("BUY_YES", "BUY_NO"):
        return action
    leg = str(closed.get("entry_leg") or "").strip().upper()
    if leg == "NO":
        return "BUY_NO"
    return "BUY_YES"


def _resolve_lane_id(closed: Dict[str, Any]) -> str:
    """Pull lane_id from the closed trade's entry_signal (set at entry time)."""
    signal = closed.get("entry_signal") or {}
    lane_id = signal.get("lane_id") if isinstance(signal, dict) else None
    if isinstance(lane_id, str) and lane_id.strip():
        return lane_id.strip()
    # Fallback when an older position record lacks lane_id (e.g. restart-sync of
    # a position opened before lane_identity was wired). Use the coarse triple.
    strategy = str(closed.get("strategy") or "unknown")
    window = str(closed.get("window_size") or signal.get("window_size") or "unknown")
    side = "down" if _resolve_side(closed) == "BUY_NO" else "up"
    return f"{strategy}|{window}|{side}|unknown|fallback"


def _resolve_lane_family(signal: Dict[str, Any], lane_id: str) -> str:
    family = signal.get("lane_family")
    if isinstance(family, str) and family.strip():
        return family.strip()
    family = signal.get("entry_family")
    if isinstance(family, str) and family.strip():
        return family.strip()
    parts = [part.strip() for part in str(lane_id or "").split("|")]
    if len(parts) >= 5 and parts[4]:
        return parts[4]
    return ""


def build_record_from_closed_trade(
    closed: Dict[str, Any],
    *,
    session_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build one calibration-log row from a journal closed-trade dict.

    `closed` is the element appended to ``TradeJournal.closed_trades`` inside
    ``log_exit`` — it carries entry context (``entry_signal``, ``edge``,
    ``confidence``, ``entry_leg``, ``window_size``) plus exit fields
    (``closed_at``, ``exit_price``, ``pnl``, ``exit_reason``).
    """
    signal = closed.get("entry_signal") or {}
    if not isinstance(signal, dict):
        signal = {}

    entry_price = _coerce_float(closed.get("entry_price")) or 0.0
    exit_price = _coerce_float(closed.get("exit_price")) or 0.0
    size = _coerce_float(closed.get("size")) or 0.0
    pnl = _coerce_float(closed.get("pnl")) or 0.0
    notional = size * entry_price
    realized_pct = (pnl / notional) if notional else 0.0

    stated_edge = _coerce_float(closed.get("edge"))
    # signal.est_prob carries the *calibrated* (post-blend) value used to
    # compute the actual entry edge. signal.raw_est_prob carries the raw
    # model output before calibration. Mapping was previously identical
    # (both pulled from signal.est_prob) which made the audit columns
    # useless. Split corrected 2026-05-29 so trades.jsonl rows let us
    # compare raw vs calibrated directly.
    raw_est_prob = _coerce_float(signal.get("raw_est_prob"))
    calibrated_est_prob = _coerce_float(signal.get("est_prob"))
    if raw_est_prob is None:
        raw_est_prob = calibrated_est_prob
    if calibrated_est_prob is None:
        calibrated_est_prob = raw_est_prob
    stated_est_prob = raw_est_prob
    lane_id = _resolve_lane_id(closed)
    side_source = str(signal.get("side_source") or "").strip()
    resolver_path = str(signal.get("resolver_path") or side_source).strip()
    primary_htf_bias = str(
        signal.get("primary_htf_bias")
        or signal.get("htf_bias")
        or closed.get("htf_bias")
        or ""
    ).strip()
    alt_htf_bias = str(signal.get("alt_htf_bias") or "").strip()
    btc_htf_bias = str(signal.get("btc_htf_bias") or "").strip()
    entry_policy_snapshot = signal.get("entry_policy")
    if not isinstance(entry_policy_snapshot, dict):
        entry_policy_snapshot = {}
    effective_min_edge = _coerce_float(
        signal.get("effective_min_edge") or closed.get("effective_min_edge")
    )
    gate_reason = str(closed.get("gate_reason") or "").strip()
    gate_stage = str(closed.get("gate_stage") or "").strip()
    indicator_snapshot = signal.get("indicator_snapshot")
    if not isinstance(indicator_snapshot, dict):
        indicator_snapshot = {}
    corr_value = _coerce_float(
        signal.get("corr_1h")
        or indicator_snapshot.get("corr_1h")
        or indicator_snapshot.get("correlation_1h")
    )
    # Raw atr/spot/rsi for vol- and rsi-bucketing. Threaded onto indicator_snapshot
    # by the strategy signal builders under ghost-matching keys (atr_14, asset_spot,
    # rsi_14; alts also carry alt_rsi_14). Bucketed identically to the ghost log so
    # trades.jsonl atr_bucket/rsi_bucket line up with rejected_candidates_settled.
    atr_value = _coerce_float(
        signal.get("atr_14") or indicator_snapshot.get("atr_14")
    )
    asset_spot_value = _coerce_float(
        signal.get("asset_spot") or indicator_snapshot.get("asset_spot")
    )
    rsi_value = _coerce_float(
        signal.get("rsi_14")
        or indicator_snapshot.get("rsi_14")
        or indicator_snapshot.get("alt_rsi_14")
    )

    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    bucket_tags = build_bucket_tags(
        edge=stated_edge,
        yes_price=entry_price,
        correlation=corr_value,
        side_source=side_source,
        regime_tag=primary_htf_bias,
        gate_reason=gate_reason,
        gate_stage=gate_stage,
        rsi=rsi_value,
        atr=atr_value,
        asset_spot=asset_spot_value,
    )

    return {
        "ts": timestamp,
        "session_id": str(session_id or ""),
        "trade_id": str(closed.get("trade_id") or ""),
        "lane_id": lane_id,
        "strategy": str(closed.get("strategy") or "unknown"),
        "window": str(closed.get("window_size") or signal.get("window_size") or ""),
        "side": _resolve_side(closed),
        "action": str(closed.get("action") or ""),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "size": size,
        "notional": round(notional, 6),
        "pnl": round(pnl, 6),
        "realized_pct": round(realized_pct, 6),
        "win": pnl > 0,
        "stated_edge": stated_edge,
        "stated_est_prob": stated_est_prob,
        # Phase 6 will overwrite the next two; Phase 0 logs them as identity.
        "calibrated_est_prob": calibrated_est_prob,
        "alpha_used": 1.0,
        "exit_reason": str(closed.get("exit_reason") or ""),
        "opened_at": str(closed.get("opened_at") or ""),
        "closed_at": str(closed.get("closed_at") or ""),
        "side_source": side_source,
        "resolver_path": resolver_path,
        "conflict_type": str(signal.get("conflict_type") or "").strip(),
        "htf_side": str(signal.get("htf_side") or "").strip(),
        "quant_side": str(signal.get("quant_side") or "").strip(),
        "momentum_side": str(signal.get("momentum_side") or "").strip(),
        "primary_htf_bias": primary_htf_bias,
        "alt_htf_bias": alt_htf_bias,
        "btc_htf_bias": btc_htf_bias,
        "lane_family": _resolve_lane_family(signal, lane_id),
        "entry_policy_snapshot": entry_policy_snapshot,
        "effective_min_edge": effective_min_edge,
        "raw_est_prob": raw_est_prob,
        "gate_reason": gate_reason,
        "gate_stage": gate_stage,
        # Exit-calibration telemetry (None on legacy/reload exits). mae/mfe are the
        # worst/best excursion vs entry; effective_stop_loss_pct is the threshold in
        # force at exit, so (realized_pct vs -effective_stop_loss_pct) = stop overshoot.
        "mae_pct": _coerce_float(closed.get("mae_pct")),
        "mfe_pct": _coerce_float(closed.get("mfe_pct")),
        "pnl_pct_at_exit": _coerce_float(closed.get("pnl_pct_at_exit")),
        "effective_stop_loss_pct": _coerce_float(closed.get("effective_stop_loss_pct")),
        # 2026-08-10 MFE-conditional-stop smoke-test: which hold policy fired at exit
        # (never_green_stop | catastrophic_stop | hold_to_resolution | loser_floor |
        # favorite_hard_stop | None). Threaded via exit_telemetry -> closed row. Makes
        # trades.jsonl self-sufficient to isolate never-green cuts and measure the fix live.
        "hold_policy_applied": str(closed.get("hold_policy_applied") or ""),
        # 2026-07-13 (operator GO, Codex GO): exit feed-provenance passthrough — journal
        # carries these via exit_telemetry; whitelist them so trades.jsonl is
        # self-sufficient for per-exit feed-health / trail re-measure analysis.
        "btc_1h_regime": signal.get("btc_1h_regime"),  # 2026-07-13 P3 (restart passenger): honest regime on fills once classifier enabled; None until then
        "exit_mark_src": closed.get("exit_mark_src"),
        "exit_mark_age_ms": _coerce_float(closed.get("exit_mark_age_ms")),
        "ws_price_age_ms": _coerce_float(closed.get("ws_price_age_ms")),
        # 2026-07-30 PAPER-TO-LIVE CALIBRATION capture fix (operator): the paper CLOB
        # execution-simulator fields were being written to entries.jsonl + stamped onto
        # the closed-trade dict, but NEVER whitelisted here — so trades.jsonl (the row the
        # analyzers read) carried none of them. Whitelist ALL of them so trades.jsonl is
        # self-sufficient for fill-economics / execution-drag / fillability analysis.
        # EXIT fill economics (realistic_paper_fills + execution_fees):
        "fill_fee_usdc": _coerce_float(closed.get("fill_fee_usdc")),
        "fill_fee_rate": _coerce_float(closed.get("fill_fee_rate")),
        "fill_slippage_pct": _coerce_float(closed.get("fill_slippage_pct")),
        "fill_mark_price": _coerce_float(closed.get("fill_mark_price")),
        # Signal-vs-execution PnL split (gap = execution drag = fees + slippage):
        "raw_signal_pnl": _coerce_float(closed.get("raw_signal_pnl")),
        "execution_adjusted_pnl": _coerce_float(closed.get("execution_adjusted_pnl")),
        # Microstructure at exit:
        "secs_to_expiry_at_exit": _coerce_float(closed.get("secs_to_expiry_at_exit")),
        "exit_book_spread": _coerce_float(closed.get("exit_book_spread")),
        "exit_best_bid": _coerce_float(closed.get("exit_best_bid")),
        "exit_best_ask": _coerce_float(closed.get("exit_best_ask")),
        "exit_depth_at_limit": _coerce_float(closed.get("exit_depth_at_limit")),
        "exit_fill_ratio": _coerce_float(closed.get("exit_fill_ratio")),
        # ENTRY executability proof (paper_entry_fresh_fill book-walk); nested dict +
        # flattened numerics so per-lane aggregation needs no join. None on live/non-fresh.
        "entry_paper_fill_quality": (
            closed.get("entry_paper_fill_quality")
            if isinstance(closed.get("entry_paper_fill_quality"), dict)
            else None
        ),
        "entry_spread": _coerce_float(
            (closed.get("entry_paper_fill_quality") or {}).get("entry_spread")
            if isinstance(closed.get("entry_paper_fill_quality"), dict) else None
        ),
        "entry_sim_fill_ratio": _coerce_float(
            (closed.get("entry_paper_fill_quality") or {}).get("sim_fill_ratio")
            if isinstance(closed.get("entry_paper_fill_quality"), dict) else None
        ),
        "entry_sim_fill_price": _coerce_float(
            (closed.get("entry_paper_fill_quality") or {}).get("sim_fill_price")
            if isinstance(closed.get("entry_paper_fill_quality"), dict) else None
        ),
        "entry_depth_at_limit": _coerce_float(
            (closed.get("entry_paper_fill_quality") or {}).get("entry_depth_at_limit")
            if isinstance(closed.get("entry_paper_fill_quality"), dict) else None
        ),
        "entry_fee_usdc": _coerce_float(
            (closed.get("entry_paper_fill_quality") or {}).get("fee_usdc")
            if isinstance(closed.get("entry_paper_fill_quality"), dict) else None
        ),
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        **bucket_tags,
    }


def append_calibration_record(
    record: Dict[str, Any],
    *,
    log_path: Optional[Path] = None,
) -> bool:
    """Append one calibration record as a single JSON line. Returns True on success.

    Failure never raises into the caller — calibration logging is best-effort
    telemetry. The trade execution and journaling paths must be unaffected.
    """
    path = Path(log_path) if log_path is not None else DEFAULT_TRADES_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":")) + "\n"
        # POSIX O_APPEND makes line-sized writes atomic across processes when the
        # payload is well under PIPE_BUF (typical 4 KiB). Our records are ~600 B.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except OSError as exc:
        logger.warning("calibration_log append failed (%s): %s", path, exc)
        return False
    except (TypeError, ValueError) as exc:
        logger.warning("calibration_log serialize failed: %s; record=%r", exc, record)
        return False
