"""Off-cycle AI narrators that summarize bot diagnostics into plain-language notes.

All narrators share the same shape: take some structured input, ask the AI to
narrate it in 3-6 sentences, return a string. Empty string on failure. Each
narrator self-gates on its config block so the orchestrator can blindly fan out.

Narrators are NOT in the trade hot path. They're invoked by
``scripts/run_ai_session_summary.py`` on a manual cadence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def _truncate(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…[truncated]"


async def _ai_narrate(
    ai_agent: Any,
    *,
    title: str,
    body: str,
    strategy_hint: str,
    timeout: float = 30.0,
) -> str:
    """Common narrator scaffold. Calls ai_agent.analyze_market with a synthetic
    market shape and returns the reasoning text. Returns empty string on failure
    so callers can ignore failed narrators silently."""
    if not ai_agent or not ai_agent.is_available():
        return ""
    try:
        result = await asyncio.wait_for(
            ai_agent.analyze_market(
                market_question=title,
                market_description=body,
                current_yes_price=0.5,
                market_id=f"narrator::{strategy_hint}",
                strategy_hint=strategy_hint,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.debug("AI narrator timeout: %s", strategy_hint)
        return ""
    except Exception as exc:
        logger.debug("AI narrator failed (%s): %s", strategy_hint, exc)
        return ""
    if not result:
        return ""
    return str(getattr(result, "reasoning", "") or "").strip()


# ── 1. Underperformance narrator ────────────────────────────────────────────


async def summarize_underperformance(
    audit_report: Dict[str, Any],
    ai_agent: Any,
    *,
    timeout: float = 30.0,
) -> str:
    """Turn a build_underperformance_report() dict into a plain-language note.

    audit_report comes from src/analysis/underperformance_audit.py. Pass either
    the structured dict (preferred — we json.dumps it) or any pre-rendered text.
    """
    if not audit_report:
        return ""
    if isinstance(audit_report, str):
        body_data = audit_report
    else:
        try:
            body_data = json.dumps(audit_report, indent=2, default=str)
        except (TypeError, ValueError):
            body_data = str(audit_report)
    body = (
        "You are reviewing a trading bot underperformance audit. Output should "
        "be a 4-6 sentence plain-language summary that a human operator can act "
        "on: which strategies regressed, what is the dominant loss driver, and "
        "what one or two specific tuning levers to consider. Avoid restating "
        "raw numbers verbatim — synthesize.\n\n"
        f"=== AUDIT ===\n{_truncate(body_data, 6000)}"
    )
    return await _ai_narrate(
        ai_agent,
        title="Underperformance audit summary",
        body=body,
        strategy_hint="narrator_underperformance",
        timeout=timeout,
    )


# ── 2. Skip / exit-reason summarizer ────────────────────────────────────────


async def summarize_skip_exit_reasons(
    skip_distribution: Dict[str, int],
    exit_distribution: Dict[str, int],
    ai_agent: Any,
    *,
    total_skips_threshold: int = 1,
    timeout: float = 30.0,
) -> str:
    """Narrate dominant skip-reasons and exit-reasons for the session.

    skip_distribution / exit_distribution: {reason: count}
    """
    skip_total = sum(int(v or 0) for v in skip_distribution.values())
    exit_total = sum(int(v or 0) for v in exit_distribution.values())
    if skip_total < total_skips_threshold and exit_total < total_skips_threshold:
        return ""

    def _top(d: Dict[str, int], n: int = 8) -> List[str]:
        items = sorted(
            ((str(k), int(v or 0)) for k, v in d.items()),
            key=lambda kv: kv[1],
            reverse=True,
        )[:n]
        return [f"{k}={v}" for k, v in items if v > 0]

    skip_top = _top(skip_distribution)
    exit_top = _top(exit_distribution)
    body = (
        "You are reviewing skip-reason and exit-reason distributions from a "
        "trading session. In 3-5 sentences, flag any reason that dominates "
        "(>30% of total) and what tuning lever it implies (threshold, gate, "
        "time stop, etc.). If nothing dominates, say so.\n\n"
        f"Skip reasons (total={skip_total}):\n  " + ("\n  ".join(skip_top) or "(none)") +
        f"\n\nExit reasons (total={exit_total}):\n  " + ("\n  ".join(exit_top) or "(none)")
    )
    return await _ai_narrate(
        ai_agent,
        title="Skip/exit reason distribution",
        body=body,
        strategy_hint="narrator_skip_exit",
        timeout=timeout,
    )


# ── 3. Calibration drift detector ──────────────────────────────────────────


def _index_closed_trades_by_market(closed_trades: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Map market_id -> {wins: int, losses: int, pnl: float}. Best-effort."""
    out: Dict[str, Dict[str, Any]] = {}
    for trade in closed_trades or []:
        mid = str(trade.get("market_id") or "")
        if not mid:
            continue
        pnl = float(trade.get("pnl") or 0.0)
        rec = out.setdefault(mid, {"wins": 0, "losses": 0, "pnl": 0.0})
        rec["pnl"] += pnl
        if pnl > 0:
            rec["wins"] += 1
        elif pnl < 0:
            rec["losses"] += 1
    return out


def _join_shadow_with_outcomes(
    shadow_records: Iterable[Dict[str, Any]],
    closed_index: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    joined: List[Dict[str, Any]] = []
    for rec in shadow_records or []:
        mid = str(rec.get("market_id") or "")
        if not mid or mid not in closed_index:
            continue
        portfolio = rec.get("portfolio_decision") or {}
        trader = rec.get("trader_proposal") or {}
        research = rec.get("research_plan") or {}
        ai_confidence = (
            rec.get("shadow_confidence")
            or rec.get("confidence_score")
            or rec.get("confidence")
            or portfolio.get("confidence")
            or trader.get("confidence")
            or research.get("confidence")
            or 0.0
        )
        joined.append({
            "market_id": mid,
            "strategy": rec.get("strategy_hint") or rec.get("strategy") or "",
            "ai_confidence": float(ai_confidence),
            "marginal_recommendation": rec.get("marginal_recommendation") or rec.get("recommendation"),
            "shadow_action": rec.get("shadow_action") or portfolio.get("action") or "",
            "outcome": closed_index[mid],
        })
    return joined


def _bucket_calibration(joined: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Group joined records into low/mid/high confidence buckets, compute win-rate."""
    buckets = {
        "low (0.0–0.4)": (0.0, 0.4),
        "mid (0.4–0.7)": (0.4, 0.7),
        "high (0.7–1.0)": (0.7, 1.001),
    }
    out: Dict[str, Dict[str, Any]] = {}
    for label, (lo, hi) in buckets.items():
        sample = [r for r in joined if lo <= r["ai_confidence"] < hi]
        wins = sum(r["outcome"]["wins"] for r in sample)
        losses = sum(r["outcome"]["losses"] for r in sample)
        total = wins + losses
        win_rate = (wins / total) if total else None
        out[label] = {
            "n_trades": total,
            "win_rate": win_rate,
            "pnl": sum(r["outcome"]["pnl"] for r in sample),
        }
    return out


async def detect_calibration_drift(
    shadow_records: List[Dict[str, Any]],
    closed_trades: List[Dict[str, Any]],
    ai_agent: Any,
    *,
    min_paired_records: int = 5,
    timeout: float = 30.0,
) -> str:
    """Narrate AI calibration drift: confidence vs realized win-rate.

    shadow_records: parsed lines from data/logs/ai_pipeline/shadow_pipeline.jsonl
    closed_trades: list of dicts with market_id + pnl (from trade journal).
    """
    if not shadow_records or not closed_trades:
        return ""
    closed_idx = _index_closed_trades_by_market(closed_trades)
    joined = _join_shadow_with_outcomes(shadow_records, closed_idx)
    if len(joined) < min_paired_records:
        return ""
    buckets = _bucket_calibration(joined)
    body_summary = json.dumps(
        {
            "paired_records": len(joined),
            "buckets": buckets,
        },
        indent=2,
        default=str,
    )
    body = (
        "You are evaluating AI confidence calibration. Each bucket below shows "
        "how trades grouped by AI confidence performed. In 3-5 sentences: is "
        "the AI well-calibrated (higher confidence → higher win-rate)? If not, "
        "describe the drift direction (over- or under-confident) and which "
        "bucket is most miscalibrated.\n\n"
        f"=== CALIBRATION ===\n{body_summary}"
    )
    return await _ai_narrate(
        ai_agent,
        title="AI calibration drift",
        body=body,
        strategy_hint="narrator_calibration",
        timeout=timeout,
    )


# ── 4. Strategy conflict explainer ─────────────────────────────────────────


def _detect_conflicts(scan_summaries: Dict[str, Dict[str, Any]]) -> List[str]:
    """Find pairs of strategies whose htf_bias / macro_trend disagree."""
    conflicts: List[str] = []
    biases = {
        s: (
            (sum_.get("htf_bias") or sum_.get("macro_trend") or "").upper()
        )
        for s, sum_ in (scan_summaries or {}).items()
    }
    items = [(s, b) for s, b in biases.items() if b]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a_s, a_b = items[i]
            b_s, b_b = items[j]
            if {"BULL", "BULLISH"} & {a_b} and {"BEAR", "BEARISH", "RISK_OFF"} & {b_b}:
                conflicts.append(f"{a_s}={a_b} vs {b_s}={b_b}")
            elif {"BEAR", "BEARISH", "RISK_OFF"} & {a_b} and {"BULL", "BULLISH"} & {b_b}:
                conflicts.append(f"{a_s}={a_b} vs {b_s}={b_b}")
    return conflicts


async def explain_strategy_conflict(
    scan_summaries: Dict[str, Dict[str, Any]],
    ai_agent: Any,
    *,
    timeout: float = 30.0,
) -> str:
    """Narrate when crypto strategies disagree on directional regime.

    scan_summaries: {strategy_name: {htf_bias|macro_trend, ...}}
    """
    conflicts = _detect_conflicts(scan_summaries or {})
    if not conflicts:
        return ""
    body_summary = json.dumps(scan_summaries, indent=2, default=str)
    body = (
        "Two or more crypto strategies disagree on the macro regime. In 3-5 "
        "sentences: name the conflict, explain what could be driving the split "
        "(timeframe difference, asset-specific signal, lag), and what the "
        "operator should treat as the dominant signal.\n\n"
        f"=== CONFLICTS ===\n  {chr(10).join('  ' + c for c in conflicts)}\n\n"
        f"=== SCAN SUMMARIES ===\n{_truncate(body_summary, 4000)}"
    )
    return await _ai_narrate(
        ai_agent,
        title="Strategy regime conflict",
        body=body,
        strategy_hint="narrator_conflict",
        timeout=timeout,
    )


# ── Helpers used by orchestrator ───────────────────────────────────────────


def load_shadow_records(shadow_jsonl: Path, max_records: int = 500) -> List[Dict[str, Any]]:
    """Parse the most recent N lines of the shadow pipeline JSONL."""
    if not shadow_jsonl.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(shadow_jsonl, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    for line in lines[-max_records:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_closed_trades_from_summary(summary_path: Path) -> List[Dict[str, Any]]:
    """Extract the closed_trades list from a TradeJournal summary.json."""
    if not summary_path.exists():
        return []
    try:
        with open(summary_path, encoding="utf-8", errors="replace") as f:
            data = json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return []
    closed = data.get("closed_trades") or []
    return [c for c in closed if isinstance(c, dict)]


def aggregate_skip_exit_distributions(
    journal_entries_path: Path,
) -> Dict[str, Dict[str, int]]:
    """Walk a journal entries.jsonl and return {skip: {reason: n}, exit: {reason: n}}."""
    skip: Counter = Counter()
    exit_: Counter = Counter()
    if not journal_entries_path.exists():
        return {"skip": {}, "exit": {}}
    try:
        with open(journal_entries_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = rec.get("event")
                reason = (rec.get("reason") or "").strip()
                if not reason:
                    extra = rec.get("extra") or {}
                    reason = str(extra.get("skip_reason") or extra.get("exit_reason") or "")
                if event == "SKIP" and reason:
                    skip[reason] += 1
                elif event == "EXIT" and reason:
                    exit_[reason] += 1
    except OSError:
        pass
    return {"skip": dict(skip), "exit": dict(exit_)}
