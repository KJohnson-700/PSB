from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


STRATEGY_ORDER = ("bitcoin", "sol_macro", "eth_macro", "hype_macro", "xrp_macro")
STRATEGY_TO_SYMBOL = {
    "bitcoin": "BTC",
    "sol_macro": "SOL",
    "eth_macro": "ETH",
    "hype_macro": "HYPE",
    "xrp_macro": "XRP",
}


@dataclass(frozen=True)
class ClosedTrade:
    session_id: str
    trade_id: str
    strategy: str
    action: str
    pnl: float
    exit_reason: str
    edge: float
    yes_price: Optional[float]
    window_size: str
    htf_bias: str


@dataclass(frozen=True)
class BuyNoSkipEvent:
    session_id: str
    strategy: str
    skip_reason: str
    edge: float
    effective_min_edge: float
    yes_price: Optional[float]
    rsi: Optional[float]
    htf_bias: str
    alt_1h_trend: str


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def discover_recent_sessions(
    paper_root: Path,
    *,
    after_session: str,
    min_exits: int = 5,
) -> List[str]:
    sessions: List[str] = []
    for session_dir in sorted(paper_root.iterdir()):
        if not session_dir.is_dir():
            continue
        if session_dir.name <= after_session:
            continue
        entries_path = session_dir / "entries.jsonl"
        if not entries_path.exists():
            continue
        exits = 0
        with entries_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") == "EXIT":
                    exits += 1
        if exits >= min_exits:
            sessions.append(session_dir.name)
    return sessions


def load_closed_trades(paper_root: Path, sessions: Iterable[str]) -> List[ClosedTrade]:
    rows: List[ClosedTrade] = []
    for session_id in sessions:
        entries_path = paper_root / session_id / "entries.jsonl"
        if not entries_path.exists():
            continue
        entries: Dict[str, Dict[str, Any]] = {}
        with entries_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                trade_id = str(payload.get("trade_id") or "")
                if payload.get("event") == "ENTRY" and trade_id:
                    entries[trade_id] = payload
                elif payload.get("event") == "EXIT" and trade_id:
                    entry = entries.get(trade_id, {})
                    extra = entry.get("extra") or {}
                    entry_price = _safe_float(payload.get("entry_price"))
                    current_price = _safe_float(payload.get("current_price"))
                    # Skip phantom binary rows from older journals.
                    if entry_price > 0 and abs(entry_price + current_price - 1.0) < 0.02:
                        continue
                    rows.append(
                        ClosedTrade(
                            session_id=session_id,
                            trade_id=trade_id,
                            strategy=str(payload.get("strategy") or ""),
                            action=str(payload.get("action") or ""),
                            pnl=_safe_float(payload.get("pnl")),
                            exit_reason=str(
                                payload.get("reason")
                                or (payload.get("extra") or {}).get("exit_reason")
                                or ""
                            ),
                            edge=_safe_float(extra.get("edge")),
                            yes_price=(
                                float(extra["yes_price"])
                                if extra.get("yes_price") is not None
                                else None
                            ),
                            window_size=str(extra.get("window_size") or ""),
                            htf_bias=str(extra.get("htf_bias") or ""),
                        )
                    )
    return rows


def load_buy_no_skips(paper_root: Path, sessions: Iterable[str]) -> List[BuyNoSkipEvent]:
    rows: List[BuyNoSkipEvent] = []
    for session_id in sessions:
        entries_path = paper_root / session_id / "entries.jsonl"
        if not entries_path.exists():
            continue
        with entries_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("event") != "BUY_NO_SKIP":
                    continue
                extra = payload.get("extra") or {}
                rows.append(
                    BuyNoSkipEvent(
                        session_id=session_id,
                        strategy=str(payload.get("strategy") or ""),
                        skip_reason=str(
                            payload.get("reason") or extra.get("skip_reason") or "unknown"
                        ),
                        edge=_safe_float(extra.get("edge")),
                        effective_min_edge=_safe_float(extra.get("effective_min_edge")),
                        yes_price=(
                            float(extra["yes_price"])
                            if extra.get("yes_price") is not None
                            else None
                        ),
                        rsi=(
                            float(extra["rsi"])
                            if extra.get("rsi") is not None
                            else None
                        ),
                        htf_bias=str(extra.get("htf_bias") or ""),
                        alt_1h_trend=str(extra.get("alt_1h_trend") or ""),
                    )
                )
    return rows


def _bucket_label(edge: float) -> str:
    if edge < 0.08:
        return "<0.08"
    if edge < 0.10:
        return "0.08-0.10"
    if edge < 0.12:
        return "0.10-0.12"
    return ">=0.12"


def _aggregate_trade_rows(rows: Iterable[ClosedTrade]) -> Dict[str, Any]:
    bucketed: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0, "edge_sum": 0.0, "yes_price_sum": 0.0, "yes_price_n": 0}
    )
    for row in rows:
        key = f"{row.strategy}::{row.action}::{row.exit_reason}"
        agg = bucketed[key]
        agg["n"] += 1
        agg["pnl"] += row.pnl
        agg["edge_sum"] += row.edge
        if row.pnl > 0:
            agg["wins"] += 1
        if row.yes_price is not None:
            agg["yes_price_sum"] += row.yes_price
            agg["yes_price_n"] += 1
    out: Dict[str, Any] = {}
    for key, agg in sorted(bucketed.items()):
        n = agg["n"]
        out[key] = {
            "n": n,
            "wins": agg["wins"],
            "win_rate": round(agg["wins"] / n, 4) if n else 0.0,
            "net_pnl": round(agg["pnl"], 4),
            "avg_edge": round(agg["edge_sum"] / n, 4) if n else 0.0,
            "avg_yes_price": round(agg["yes_price_sum"] / agg["yes_price_n"], 4)
            if agg["yes_price_n"]
            else None,
        }
    return out


def _group_action_summary(rows: Iterable[ClosedTrade]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[ClosedTrade]] = defaultdict(list)
    for row in rows:
        grouped[f"{row.strategy}::{row.action}"].append(row)
    out: Dict[str, Dict[str, Any]] = {}
    for key, group in sorted(grouped.items()):
        n = len(group)
        pnl = sum(r.pnl for r in group)
        wins = sum(1 for r in group if r.pnl > 0)
        out[key] = {
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4) if n else 0.0,
            "net_pnl": round(pnl, 4),
            "avg_edge": round(sum(r.edge for r in group) / n, 4) if n else 0.0,
        }
    return out


def _edge_buckets(rows: Iterable[ClosedTrade]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[ClosedTrade]] = defaultdict(list)
    for row in rows:
        grouped[f"{row.strategy}::{_bucket_label(row.edge)}"].append(row)
    out: Dict[str, Dict[str, Any]] = {}
    for key, group in sorted(grouped.items()):
        n = len(group)
        pnl = sum(r.pnl for r in group)
        wins = sum(1 for r in group if r.pnl > 0)
        out[key] = {
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4) if n else 0.0,
            "net_pnl": round(pnl, 4),
        }
    return out


def _side_mix(rows: Iterable[ClosedTrade]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[ClosedTrade]] = defaultdict(list)
    for row in rows:
        grouped[row.strategy].append(row)
    out: Dict[str, Dict[str, Any]] = {}
    for strategy, group in sorted(grouped.items()):
        total = len(group)
        buy_no_n = sum(1 for r in group if r.action == "BUY_NO")
        buy_yes_n = sum(1 for r in group if r.action == "BUY_YES")
        tp_n = sum(1 for r in group if r.exit_reason == "take_profit")
        time_stop_n = sum(1 for r in group if r.exit_reason == "updown_time_stop")
        out[strategy] = {
            "total": total,
            "buy_no_n": buy_no_n,
            "buy_yes_n": buy_yes_n,
            "buy_no_share": round(buy_no_n / total, 4) if total else 0.0,
            "buy_yes_share": round(buy_yes_n / total, 4) if total else 0.0,
            "take_profit_n": tp_n,
            "updown_time_stop_n": time_stop_n,
        }
    return out


def _skip_summary(rows: Iterable[BuyNoSkipEvent]) -> Dict[str, Any]:
    by_strategy_reason: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "edge_excess_sum": 0.0, "edge_sum": 0.0}
    )
    for row in rows:
        key = f"{row.strategy}::{row.skip_reason}"
        agg = by_strategy_reason[key]
        agg["n"] += 1
        agg["edge_sum"] += row.edge
        agg["edge_excess_sum"] += max(0.0, row.edge - row.effective_min_edge)
    out: Dict[str, Any] = {}
    for key, agg in sorted(by_strategy_reason.items()):
        n = agg["n"]
        out[key] = {
            "n": n,
            "avg_edge": round(agg["edge_sum"] / n, 4) if n else 0.0,
            "avg_edge_excess": round(agg["edge_excess_sum"] / n, 4) if n else 0.0,
        }
    return out


def load_backtest_action_summary(reports_dir: Path) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for strategy, symbol in STRATEGY_TO_SYMBOL.items():
        matches = sorted(
            reports_dir.glob(f"backtest_crypto_{symbol}_15m_*.json"),
            reverse=True,
        )
        if not matches:
            continue
        report = json.loads(matches[0].read_text(encoding="utf-8"))
        by_action: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        by_bucket: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for trade in report.get("trades") or []:
            action = str(trade.get("action") or "")
            by_action[action].append(trade)
            by_bucket[_bucket_label(_safe_float(trade.get("edge")))].append(trade)
        action_summary: Dict[str, Any] = {}
        for action, trades in sorted(by_action.items()):
            n = len(trades)
            pnl = sum(_safe_float(t.get("pnl")) for t in trades)
            wins = sum(1 for t in trades if _safe_float(t.get("pnl")) > 0)
            action_summary[action] = {
                "n": n,
                "wins": wins,
                "win_rate": round(wins / n, 4) if n else 0.0,
                "net_pnl": round(pnl, 4),
                "avg_edge": round(
                    sum(_safe_float(t.get("edge")) for t in trades) / n, 4
                )
                if n
                else 0.0,
            }
        bucket_summary: Dict[str, Any] = {}
        for bucket, trades in sorted(by_bucket.items()):
            n = len(trades)
            pnl = sum(_safe_float(t.get("pnl")) for t in trades)
            wins = sum(1 for t in trades if _safe_float(t.get("pnl")) > 0)
            bucket_summary[bucket] = {
                "n": n,
                "wins": wins,
                "win_rate": round(wins / n, 4) if n else 0.0,
                "net_pnl": round(pnl, 4),
            }
        out[strategy] = {
            "symbol": symbol,
            "file": matches[0].name,
            "net_pnl": round(_safe_float(report.get("net_pnl")), 4),
            "win_rate": round(_safe_float(report.get("win_rate")), 4),
            "trades_count": int(report.get("trades_count") or 0),
            "is_reliable_control": int(report.get("trades_count") or 0) >= 20,
            "by_action": action_summary,
            "by_edge_bucket": bucket_summary,
        }
    return out


def build_underperformance_report(
    *,
    baseline_rows: List[ClosedTrade],
    recent_rows: List[ClosedTrade],
    skip_rows: List[BuyNoSkipEvent],
    backtests: Dict[str, Dict[str, Any]],
    baseline_sessions: List[str],
    recent_sessions: List[str],
) -> Dict[str, Any]:
    baseline_side_mix = _side_mix(baseline_rows)
    recent_side_mix = _side_mix(recent_rows)
    recent_total_negative = abs(sum(r.pnl for r in recent_rows if r.pnl < 0))
    overall_recent_buy_yes_timestop_loss = abs(
        sum(
            r.pnl
            for r in recent_rows
            if r.action == "BUY_YES" and r.exit_reason == "updown_time_stop" and r.pnl < 0
        )
    )

    per_strategy: Dict[str, Any] = {}
    for strategy in STRATEGY_ORDER:
        base_group = [r for r in baseline_rows if r.strategy == strategy]
        recent_group = [r for r in recent_rows if r.strategy == strategy]
        skip_group = [r for r in skip_rows if r.strategy == strategy]
        backtest = backtests.get(strategy)

        base_mix = baseline_side_mix.get(strategy, {})
        recent_mix = recent_side_mix.get(strategy, {})
        strategy_negative = abs(sum(r.pnl for r in recent_group if r.pnl < 0))
        time_stop_buy_yes_loss = abs(
            sum(
                r.pnl
                for r in recent_group
                if r.action == "BUY_YES" and r.exit_reason == "updown_time_stop" and r.pnl < 0
            )
        )
        exit_ratio = round(time_stop_buy_yes_loss / strategy_negative, 4) if strategy_negative else 0.0
        suppression_gap = max(
            0.0,
            float(base_mix.get("buy_no_share") or 0.0)
            - float(recent_mix.get("buy_no_share") or 0.0),
        )
        recent_edge_buckets = {
            k.split("::", 1)[1]: v
            for k, v in _edge_buckets(recent_group).items()
            if k.startswith(f"{strategy}::")
        }
        losing_bucket_count = sum(
            1 for bucket in recent_edge_buckets.values() if float(bucket["net_pnl"]) < 0
        )
        backtest_reliable = bool((backtest or {}).get("is_reliable_control"))
        backtest_for_hypothesis = backtest if backtest_reliable else None
        backtest_buy_no = ((backtest_for_hypothesis or {}).get("by_action") or {}).get("BUY_NO", {})
        backtest_buy_yes = ((backtest_for_hypothesis or {}).get("by_action") or {}).get("BUY_YES", {})
        backtest_net = _safe_float((backtest_for_hypothesis or {}).get("net_pnl"))

        hypotheses: List[Dict[str, Any]] = []
        if exit_ratio >= 0.35 and time_stop_buy_yes_loss >= 3.0:
            hypotheses.append(
                {
                    "cause": "exit_path_damage",
                    "priority": 1,
                    "confidence": "high",
                    "evidence": (
                        f"{strategy} recent BUY_YES updown_time_stop losses were "
                        f"{time_stop_buy_yes_loss:.2f}, {exit_ratio:.1%} of negative PnL."
                    ),
                }
            )
        if suppression_gap >= 0.03 and _safe_float(backtest_buy_no.get("net_pnl")) > max(
            0.0, _safe_float(backtest_buy_yes.get("net_pnl"))
        ):
            hypotheses.append(
                {
                    "cause": "signal_suppression",
                    "priority": 2,
                    "confidence": "high" if skip_group else "medium",
                    "evidence": (
                        f"{strategy} BUY_NO share fell from {float(base_mix.get('buy_no_share') or 0.0):.1%} "
                        f"to {float(recent_mix.get('buy_no_share') or 0.0):.1%}, while the latest 15m backtest "
                            f"shows BUY_NO net PnL {_safe_float(backtest_buy_no.get('net_pnl')):+.2f} "
                            f"vs BUY_YES {_safe_float(backtest_buy_yes.get('net_pnl')):+.2f}."
                    ),
                }
            )
        if backtest_net < 0 and (
            _safe_float(backtest_buy_no.get("net_pnl")) < 0
            and _safe_float(backtest_buy_yes.get("net_pnl")) < 0
        ):
            hypotheses.append(
                {
                    "cause": "entry_quality_or_edge_calibration",
                    "priority": 3,
                    "confidence": "high",
                    "evidence": (
                        f"{strategy} latest 15m backtest net PnL was {backtest_net:+.2f}, "
                        f"with both BUY_NO and BUY_YES negative."
                    ),
                }
            )
        elif backtest_net < 0 and losing_bucket_count >= 2:
            hypotheses.append(
                {
                    "cause": "entry_quality_or_edge_calibration",
                    "priority": 3,
                    "confidence": "medium",
                    "evidence": (
                        f"{strategy} latest 15m backtest net PnL was {backtest_net:+.2f}, and "
                        f"{losing_bucket_count} recent live edge buckets were net negative."
                    ),
                }
            )
        if not hypotheses:
            hypotheses.append(
                {
                    "cause": "mixed_or_inconclusive",
                    "priority": 4,
                    "confidence": "low",
                    "evidence": "No single dominant failure mode cleared the audit thresholds.",
                }
            )

        fix_candidates: List[str] = []
        if any(h["cause"] == "exit_path_damage" for h in hypotheses):
            fix_candidates.append(
                "Replay recent BUY_YES time-stop trades against expiry/relaxed-stop counterfactuals before touching signal gates."
            )
        if any(h["cause"] == "signal_suppression" for h in hypotheses):
            fix_candidates.append(
                "Trace BUY_NO admission gaps against the profitable backtest side mix before broadening BUY_YES entries."
            )
        if any(h["cause"] == "entry_quality_or_edge_calibration" for h in hypotheses):
            fix_candidates.append(
                "Recalibrate edge thresholds and edge buckets for this lane before assuming missed shorts are the core issue."
            )
        if strategy == "xrp_macro" and backtest_net > 0:
            fix_candidates.append(
                "Use XRP as the control lane; avoid global architecture rewrites that would discard a still-profitable backtest profile."
            )
        if strategy == "hype_macro" and not backtest_reliable:
            fix_candidates.append(
                "Restore a reproducible HYPE 15m backtest dataset first; current live-only evidence is not enough for entry-side changes."
            )

        per_strategy[strategy] = {
            "baseline": {
                "trades": len(base_group),
                "net_pnl": round(sum(r.pnl for r in base_group), 4),
                "side_mix": base_mix,
            },
            "recent": {
                "trades": len(recent_group),
                "net_pnl": round(sum(r.pnl for r in recent_group), 4),
                "side_mix": recent_mix,
                "edge_buckets": recent_edge_buckets,
                "buy_yes_time_stop_loss": round(time_stop_buy_yes_loss, 4),
                "buy_yes_time_stop_loss_share_of_negative_pnl": exit_ratio,
            },
            "buy_no_skip_events": {
                "count": len(skip_group),
                "top_reasons": _skip_summary(skip_group),
            },
            "backtest_15m": backtest,
            "backtest_15m_reliable": backtest_reliable,
            "ranked_hypotheses": sorted(hypotheses, key=lambda row: row["priority"]),
            "fix_candidates": fix_candidates,
        }

    return {
        "meta": {
            "baseline_sessions": baseline_sessions,
            "recent_sessions": recent_sessions,
            "baseline_trade_count": len(baseline_rows),
            "recent_trade_count": len(recent_rows),
            "recent_buy_no_skip_count": len(skip_rows),
        },
        "overall": {
            "baseline_net_pnl": round(sum(r.pnl for r in baseline_rows), 4),
            "recent_net_pnl": round(sum(r.pnl for r in recent_rows), 4),
            "baseline_side_mix": baseline_side_mix,
            "recent_side_mix": recent_side_mix,
            "recent_buy_yes_time_stop_loss": round(overall_recent_buy_yes_timestop_loss, 4),
            "recent_buy_yes_time_stop_loss_share_of_negative_pnl": round(
                overall_recent_buy_yes_timestop_loss / recent_total_negative, 4
            )
            if recent_total_negative
            else 0.0,
        },
        "tables": {
            "baseline_strategy_action_exit": _aggregate_trade_rows(baseline_rows),
            "recent_strategy_action_exit": _aggregate_trade_rows(recent_rows),
            "baseline_strategy_action": _group_action_summary(baseline_rows),
            "recent_strategy_action": _group_action_summary(recent_rows),
            "baseline_edge_buckets": _edge_buckets(baseline_rows),
            "recent_edge_buckets": _edge_buckets(recent_rows),
            "buy_no_skip_summary": _skip_summary(skip_rows),
        },
        "backtests_15m": backtests,
        "per_strategy": per_strategy,
    }


def render_underperformance_markdown(report: Dict[str, Any]) -> str:
    meta = report["meta"]
    overall = report["overall"]
    lines = [
        "# Strategy Underperformance Diagnosis",
        "",
        "## Summary",
        f"- **Baseline sessions:** `{', '.join(meta['baseline_sessions'])}`",
        f"- **Recent sessions:** `{', '.join(meta['recent_sessions'])}`",
        f"- **Baseline net PnL:** {overall['baseline_net_pnl']:+.2f} across {meta['baseline_trade_count']} closes",
        f"- **Recent net PnL:** {overall['recent_net_pnl']:+.2f} across {meta['recent_trade_count']} closes",
        f"- **Recent BUY_YES `updown_time_stop` loss share:** {overall['recent_buy_yes_time_stop_loss_share_of_negative_pnl']:.1%}",
        f"- **Recent `BUY_NO_SKIP` events recorded:** {meta['recent_buy_no_skip_count']}",
        "",
        "## Live Side Mix",
        "",
        "| strategy | baseline BUY_NO share | recent BUY_NO share | baseline TP | recent TP | baseline time_stop | recent time_stop |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy in STRATEGY_ORDER:
        base = overall["baseline_side_mix"].get(strategy, {})
        recent = overall["recent_side_mix"].get(strategy, {})
        lines.append(
            f"| {strategy} | {float(base.get('buy_no_share') or 0.0):.1%} | "
            f"{float(recent.get('buy_no_share') or 0.0):.1%} | "
            f"{int(base.get('take_profit_n') or 0)} | {int(recent.get('take_profit_n') or 0)} | "
            f"{int(base.get('updown_time_stop_n') or 0)} | {int(recent.get('updown_time_stop_n') or 0)} |"
        )

    lines.extend(
        [
            "",
            "## Lane Diagnosis",
            "",
        ]
    )
    for strategy in STRATEGY_ORDER:
        lane = report["per_strategy"][strategy]
        lines.append(f"### {strategy}")
        recent = lane["recent"]
        lines.append(
            f"- **Recent live:** {recent['trades']} trades, {recent['net_pnl']:+.2f} PnL, "
            f"BUY_NO share {float(recent['side_mix'].get('buy_no_share') or 0.0):.1%}"
        )
        backtest = lane.get("backtest_15m")
        if backtest:
            lines.append(
                f"- **Latest 15m backtest:** `{backtest['file']}` | "
                f"{backtest['trades_count']} trades | net {backtest['net_pnl']:+.2f} | WR {float(backtest['win_rate']):.1%}"
            )
            if not lane.get("backtest_15m_reliable"):
                lines.append("- **Backtest control quality:** insufficient sample size for strong causal claims")
        else:
            lines.append("- **Latest 15m backtest:** unavailable")
        lines.append("- **Ranked root causes:**")
        for item in lane["ranked_hypotheses"]:
            lines.append(
                f"  - `{item['cause']}` ({item['confidence']}) — {item['evidence']}"
            )
        if lane["fix_candidates"]:
            lines.append("- **Next fixes to test:**")
            for item in lane["fix_candidates"]:
                lines.append(f"  - {item}")
        lines.append("")

    lines.extend(
        [
            "## Key Tables",
            "",
            "### Recent strategy × action × exit reason",
            "",
            "| strategy | action | exit_reason | n | WR | net_pnl | avg_edge | avg_yes_price |",
            "|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    recent_rows = report["tables"]["recent_strategy_action_exit"]
    for key, row in sorted(recent_rows.items(), key=lambda item: item[1]["net_pnl"]):
        strategy, action, exit_reason = key.split("::", 2)
        avg_yes_price = row["avg_yes_price"]
        yes_price_str = f"{avg_yes_price:.3f}" if avg_yes_price is not None else "—"
        lines.append(
            f"| {strategy} | {action} | {exit_reason} | {row['n']} | {float(row['win_rate']):.1%} | "
            f"{row['net_pnl']:+.2f} | {row['avg_edge']:.3f} | {yes_price_str} |"
        )

    lines.extend(
        [
            "",
            "### Recent BUY_NO suppression telemetry",
            "",
        ]
    )
    skip_summary = report["tables"]["buy_no_skip_summary"]
    if not skip_summary:
        lines.append("- No `BUY_NO_SKIP` events were present in the selected paper sessions.")
    else:
        lines.append("| strategy | skip_reason | n | avg_edge | avg_edge_excess |")
        lines.append("|---|---|---:|---:|---:|")
        for key, row in sorted(skip_summary.items(), key=lambda item: (-item[1]["n"], item[0])):
            strategy, reason = key.split("::", 1)
            lines.append(
                f"| {strategy} | {reason} | {row['n']} | {row['avg_edge']:.3f} | {row['avg_edge_excess']:.3f} |"
            )

    lines.extend(
        [
            "",
            "## Backtest Controls",
            "",
            "| strategy | report | net_pnl | WR | BUY_NO pnl | BUY_YES pnl |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for strategy in STRATEGY_ORDER:
        backtest = report["backtests_15m"].get(strategy)
        if not backtest:
            lines.append(f"| {strategy} | — | — | — | — | — |")
            continue
        by_action = backtest.get("by_action") or {}
        lines.append(
            f"| {strategy} | {backtest['file']} | {backtest['net_pnl']:+.2f} | {float(backtest['win_rate']):.1%} | "
            f"{_safe_float((by_action.get('BUY_NO') or {}).get('net_pnl')):+.2f} | "
            f"{_safe_float((by_action.get('BUY_YES') or {}).get('net_pnl')):+.2f} |"
        )

    return "\n".join(lines) + "\n"
