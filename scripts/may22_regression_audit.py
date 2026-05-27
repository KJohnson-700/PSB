#!/usr/bin/env python3
"""Compare May 22 paper-session trade economics against current sessions."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.execution.trade_journal import is_phantom_exit_row


DEFAULT_BASELINE_SESSIONS = ("test_20260522_052210",)
DEFAULT_CURRENT_SESSIONS = ("test_20260525_231430", "test_20260526_042005")
DEFAULT_PAPER_ROOT = Path("data/paper_trades")
DEFAULT_OUT_DIR = Path("docs/session_reports")
DEFAULT_BASELINE = "5/22"
DEFAULT_BASELINE_TOP_N = 2
DEFAULT_CURRENT_TOP_N = 2
DEFAULT_MIN_SESSION_TRADES = 50


@dataclass(frozen=True)
class ClosedTrade:
    session_id: str
    trade_id: str
    timestamp: str
    strategy: str
    action: str
    size: float
    pnl: float
    entry_price: float | None
    exit_price: float | None
    exit_reason: str
    window_size: str
    lane_id: str
    lane_family: str
    lane_side: str
    lane_window: str
    side_source: str
    entry_policy_name: str
    size_multiplier: float | None


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    closed_trades: int
    wins: int
    win_rate: float
    pnl: float
    avg_pnl: float
    avg_size: float
    buy_yes: int
    buy_no: int
    first_ts: str
    last_ts: str
    hours: float | None


def _parse_mmdd(value: str) -> tuple[int, int]:
    try:
        month_s, day_s = value.strip().split("/", 1)
        return int(month_s), int(day_s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected M/D or MM/DD date token, got {value!r}"
        ) from exc


def _session_date_token(session_id: str) -> str | None:
    # test_20260522_052210 -> 20260522
    parts = session_id.split("_")
    for part in parts:
        if len(part) == 8 and part.isdigit():
            return part
    return None


def _session_matches_mmdd(session_id: str, *, month: int, day: int) -> bool:
    token = _session_date_token(session_id)
    if not token:
        return False
    return int(token[4:6]) == month and int(token[6:8]) == day


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def _extra(row: dict[str, Any]) -> dict[str, Any]:
    extra = row.get("extra")
    return extra if isinstance(extra, dict) else {}


def _entry_policy(merged_extra: dict[str, Any]) -> dict[str, Any]:
    policy = merged_extra.get("entry_policy")
    return policy if isinstance(policy, dict) else {}


def load_closed_trades(
    paper_root: Path,
    session_ids: Iterable[str],
) -> list[ClosedTrade]:
    trades: list[ClosedTrade] = []
    for session_id in session_ids:
        entries_file = paper_root / session_id / "entries.jsonl"
        if not entries_file.exists():
            raise FileNotFoundError(f"missing journal: {entries_file}")

        entries_by_id: dict[str, dict[str, Any]] = {}
        exits: list[dict[str, Any]] = []
        for row in _jsonl(entries_file):
            event = row.get("event")
            trade_id = str(row.get("trade_id") or "")
            if event == "ENTRY" and trade_id:
                entries_by_id[trade_id] = row
            elif event == "EXIT" and trade_id:
                exits.append(row)

        for exit_row in exits:
            if is_phantom_exit_row(exit_row):
                continue
            trade_id = str(exit_row.get("trade_id") or "")
            entry_row = entries_by_id.get(trade_id, {})
            merged_extra = {**_extra(entry_row), **_extra(exit_row)}
            policy = _entry_policy(merged_extra)
            lane_id = str(
                merged_extra.get("lane_id")
                or merged_extra.get("entry_lane")
                or merged_extra.get("signal_reason")
                or "unknown"
            )
            lane_family = str(
                merged_extra.get("lane_family")
                or merged_extra.get("entry_family")
                or merged_extra.get("family")
                or "unknown"
            )
            trades.append(
                ClosedTrade(
                    session_id=session_id,
                    trade_id=trade_id,
                    timestamp=str(exit_row.get("timestamp") or ""),
                    strategy=str(exit_row.get("strategy") or entry_row.get("strategy") or ""),
                    action=str(exit_row.get("action") or entry_row.get("action") or ""),
                    size=float(
                        _as_float(entry_row.get("size"))
                        or _as_float(exit_row.get("size"))
                        or 0.0
                    ),
                    pnl=float(_as_float(exit_row.get("pnl")) or 0.0),
                    entry_price=_as_float(
                        entry_row.get("entry_price") or exit_row.get("entry_price")
                    ),
                    exit_price=_as_float(exit_row.get("current_price")),
                    exit_reason=str(exit_row.get("reason") or "unknown"),
                    window_size=str(merged_extra.get("window_size") or "unknown"),
                    lane_id=lane_id,
                    lane_family=lane_family,
                    lane_side=str(merged_extra.get("lane_side") or "unknown"),
                    lane_window=str(merged_extra.get("lane_window") or "unknown"),
                    side_source=str(merged_extra.get("side_source") or "unknown"),
                    entry_policy_name=str(policy.get("name") or "unknown"),
                    size_multiplier=_as_float(policy.get("size_multiplier")),
                )
            )
    return trades


def summarize_session(session_id: str, trades: list[ClosedTrade]) -> SessionSummary:
    pnls = [row.pnl for row in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    actions = Counter(row.action for row in trades)
    timestamps = sorted(row.timestamp for row in trades if row.timestamp)
    hours: float | None = None
    if len(timestamps) >= 2:
        try:
            start = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
            end = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            hours = max((end - start).total_seconds() / 3600.0, 0.0)
        except ValueError:
            hours = None
    return SessionSummary(
        session_id=session_id,
        closed_trades=len(trades),
        wins=len(wins),
        win_rate=len(wins) / len(trades) if trades else 0.0,
        pnl=sum(pnls),
        avg_pnl=mean(pnls) if pnls else 0.0,
        avg_size=mean(row.size for row in trades) if trades else 0.0,
        buy_yes=actions.get("BUY_YES", 0),
        buy_no=actions.get("BUY_NO", 0),
        first_ts=timestamps[0] if timestamps else "",
        last_ts=timestamps[-1] if timestamps else "",
        hours=hours,
    )


def summarize_sessions(
    paper_root: Path,
    session_ids: Iterable[str],
) -> list[SessionSummary]:
    summaries: list[SessionSummary] = []
    for session_id in session_ids:
        summaries.append(summarize_session(session_id, load_closed_trades(paper_root, [session_id])))
    return summaries


def discover_session_ids(paper_root: Path) -> list[str]:
    if not paper_root.exists():
        return []
    return sorted(
        path.name
        for path in paper_root.iterdir()
        if path.is_dir() and (path / "entries.jsonl").exists()
    )


def select_gold_sessions(
    paper_root: Path,
    *,
    baseline: str,
    top_n: int,
    min_trades: int,
) -> tuple[list[str], str, list[SessionSummary]]:
    month, day = _parse_mmdd(baseline)
    candidates = [
        session_id
        for session_id in discover_session_ids(paper_root)
        if _session_matches_mmdd(session_id, month=month, day=day)
    ]
    summaries = [
        summary
        for summary in summarize_sessions(paper_root, candidates)
        if summary.closed_trades >= min_trades
    ]
    summaries.sort(key=lambda row: (row.pnl, row.closed_trades, row.session_id), reverse=True)
    selected = [row.session_id for row in summaries[:top_n]]
    rule = (
        f"GOLD baseline = top {top_n} sessions by realized PnL on {baseline} "
        f"with closed trades >= {min_trades}; ties by trade count then session id."
    )
    return selected, rule, summaries


def select_recent_sessions(
    paper_root: Path,
    *,
    top_n: int,
    min_trades: int,
    exclude_sessions: Iterable[str],
) -> tuple[list[str], str, list[SessionSummary]]:
    excluded = set(exclude_sessions)
    candidates = [
        session_id
        for session_id in discover_session_ids(paper_root)
        if session_id not in excluded
    ]
    summaries = [
        summary
        for summary in summarize_sessions(paper_root, candidates)
        if summary.closed_trades >= min_trades
    ]
    summaries.sort(key=lambda row: row.session_id, reverse=True)
    selected = [row.session_id for row in summaries[:top_n]]
    selected_summaries = summaries[:top_n]
    rule = (
        f"CURRENT = newest {top_n} sessions by session id with closed trades >= "
        f"{min_trades}, excluding selected baseline sessions."
    )
    return selected, rule, selected_summaries


def _ratio(avg_win: float | None, avg_loss: float | None) -> float | None:
    if avg_win is None or avg_loss is None or avg_loss == 0:
        return None
    return avg_win / abs(avg_loss)


def _pct_delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before == 0:
        return None
    return (after - before) / abs(before)


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def aggregate(
    trades: Iterable[ClosedTrade],
    key_fn: Callable[[ClosedTrade], str],
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[ClosedTrade]] = defaultdict(list)
    for trade in trades:
        buckets[key_fn(trade)].append(trade)

    out: dict[str, dict[str, Any]] = {}
    for key, rows in buckets.items():
        pnls = [row.pnl for row in rows]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        avg_win = mean(wins) if wins else None
        avg_loss = mean(losses) if losses else None
        ratio = _ratio(avg_win, avg_loss)
        actions = Counter(row.action for row in rows)
        exit_reasons = Counter(row.exit_reason for row in rows)
        sessions = Counter(row.session_id for row in rows)
        size_multipliers = [
            row.size_multiplier for row in rows if row.size_multiplier is not None
        ]
        out[str(key)] = {
            "trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(rows) if rows else 0.0,
            "pnl": sum(pnls),
            "avg_pnl": mean(pnls) if pnls else 0.0,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "win_loss_ratio": ratio,
            "avg_size": mean(row.size for row in rows) if rows else 0.0,
            "action_counts": dict(actions),
            "exit_reason_counts": dict(exit_reasons),
            "session_counts": dict(sessions),
            "avg_size_multiplier": mean(size_multipliers)
            if size_multipliers
            else None,
        }
    return out


def compare_groups(
    baseline: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in sorted(set(baseline) | set(current)):
        before = baseline.get(key, {})
        after = current.get(key, {})
        rows.append(
            {
                "group": key,
                "baseline_trades": int(before.get("trades", 0) or 0),
                "current_trades": int(after.get("trades", 0) or 0),
                "baseline_win_rate": before.get("win_rate"),
                "current_win_rate": after.get("win_rate"),
                "delta_win_rate": (after.get("win_rate") or 0.0)
                - (before.get("win_rate") or 0.0),
                "delta_win_rate_pct_points": (
                    ((after.get("win_rate") or 0.0) - (before.get("win_rate") or 0.0))
                    * 100.0
                ),
                "baseline_pnl": before.get("pnl", 0.0),
                "current_pnl": after.get("pnl", 0.0),
                "pnl_impact_vs_baseline_avg": (
                    ((before.get("avg_pnl") or 0.0) * int(after.get("trades", 0) or 0))
                    - (after.get("pnl", 0.0) or 0.0)
                ),
                "baseline_avg_pnl": before.get("avg_pnl"),
                "current_avg_pnl": after.get("avg_pnl"),
                "baseline_avg_win": before.get("avg_win"),
                "current_avg_win": after.get("avg_win"),
                "delta_avg_win": (after.get("avg_win") or 0.0)
                - (before.get("avg_win") or 0.0),
                "baseline_avg_loss": before.get("avg_loss"),
                "current_avg_loss": after.get("avg_loss"),
                "delta_avg_loss": (after.get("avg_loss") or 0.0)
                - (before.get("avg_loss") or 0.0),
                "baseline_win_loss_ratio": before.get("win_loss_ratio"),
                "current_win_loss_ratio": after.get("win_loss_ratio"),
                "delta_ratio_pct": _pct_delta(
                    before.get("win_loss_ratio"),
                    after.get("win_loss_ratio"),
                ),
                "baseline_avg_size": before.get("avg_size"),
                "current_avg_size": after.get("avg_size"),
                "delta_size_pct": _pct_delta(
                    before.get("avg_size"),
                    after.get("avg_size"),
                ),
            }
        )
    return rows


def build_hypothesis_ledger(report: dict[str, Any]) -> list[dict[str, Any]]:
    strategy_rows = {
        row["group"]: row for row in report["comparisons"].get("strategy", [])
    }
    lane_rows_by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lane_row in report["comparisons"].get("lane_id", []):
        strategy = str(lane_row["group"]).split("|", 1)[0]
        lane_rows_by_strategy[strategy].append(lane_row)

    ledger: list[dict[str, Any]] = []
    for strategy, row in strategy_rows.items():
        if row["current_trades"] <= 0:
            continue
        size_moved = (
            row["current_trades"] >= 15
            and row["delta_size_pct"] is not None
            and abs(row["delta_size_pct"]) >= 0.10
        )
        exit_moved = (
            row["current_trades"] >= 15
            and row["delta_ratio_pct"] is not None
            and abs(row["delta_ratio_pct"]) >= 0.20
        )
        selection_lanes: list[str] = []
        for lane_row in lane_rows_by_strategy.get(strategy, []):
            current_n = int(lane_row["current_trades"] or 0)
            baseline_n = int(lane_row["baseline_trades"] or 0)
            current_wr = lane_row["current_win_rate"]
            delta_wr = lane_row["delta_win_rate"]
            if current_n >= 5 and baseline_n >= 5 and delta_wr is not None and delta_wr <= -0.15:
                selection_lanes.append(str(lane_row["group"]))
            elif current_n >= 5 and baseline_n == 0 and current_wr is not None and current_wr < 0.35:
                selection_lanes.append(str(lane_row["group"]))
        selection_moved = bool(selection_lanes)

        causes: list[str] = []
        if size_moved:
            causes.append("sizing")
        if exit_moved:
            causes.append("exit")
        if selection_moved:
            causes.append("selection")
        if not causes:
            causes.append("monitor")

        explained_by_known_revert = size_moved and not exit_moved and not selection_moved
        if explained_by_known_revert:
            standing = "explained_by_known_sizing_revert"
        elif size_moved:
            standing = "partly_explained_by_known_sizing_revert"
        else:
            standing = "claim_still_standing"

        ledger.append(
            {
                "strategy": strategy,
                "rank_metric_pnl_impact": row["pnl_impact_vs_baseline_avg"],
                "classification": "+".join(causes),
                "standing": standing,
                "baseline_trades": row["baseline_trades"],
                "current_trades": row["current_trades"],
                "delta_size_pct": row["delta_size_pct"],
                "delta_ratio_pct": row["delta_ratio_pct"],
                "delta_win_rate_pct_points": row["delta_win_rate_pct_points"],
                "selection_lanes": selection_lanes[:5],
            }
        )
    ledger.sort(key=lambda row: row["rank_metric_pnl_impact"], reverse=True)
    return ledger


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def build_report(
    paper_root: Path,
    baseline_sessions: Iterable[str],
    current_sessions: Iterable[str],
    *,
    baseline_rule: str = "explicit --baseline-session list",
    current_rule: str = "explicit --current-session list",
    session_candidates: Iterable[SessionSummary] = (),
) -> dict[str, Any]:
    baseline_session_list = list(baseline_sessions)
    current_session_list = list(current_sessions)
    baseline_trades = load_closed_trades(paper_root, baseline_session_list)
    current_trades = load_closed_trades(paper_root, current_session_list)

    groupers: dict[str, Callable[[ClosedTrade], str]] = {
        "strategy": lambda row: row.strategy or "unknown",
        "strategy_action": lambda row: f"{row.strategy}::{row.action}",
        "lane_id": lambda row: row.lane_id or "unknown",
        "lane_family": lambda row: row.lane_family or "unknown",
        "exit_reason": lambda row: row.exit_reason or "unknown",
        "side_source": lambda row: row.side_source or "unknown",
        "window_size": lambda row: row.window_size or "unknown",
    }

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paper_root": str(paper_root),
        "baseline_sessions": baseline_session_list,
        "current_sessions": current_session_list,
        "baseline_rule": baseline_rule,
        "current_rule": current_rule,
        "session_table": [asdict(row) for row in session_candidates],
        "baseline_closed_trades": len(baseline_trades),
        "current_closed_trades": len(current_trades),
        "groups": {},
        "comparisons": {},
        "trades": {
            "baseline": [asdict(row) for row in baseline_trades],
            "current": [asdict(row) for row in current_trades],
        },
    }
    for name, key_fn in groupers.items():
        baseline_agg = aggregate(baseline_trades, key_fn)
        current_agg = aggregate(current_trades, key_fn)
        report["groups"][name] = {
            "baseline": baseline_agg,
            "current": current_agg,
        }
        report["comparisons"][name] = compare_groups(baseline_agg, current_agg)
    report["hypothesis_ledger"] = build_hypothesis_ledger(report)
    return _json_ready(report)


def _fmt_money(value: Any) -> str:
    if value is None:
        return "na"
    return f"${float(value):.2f}"


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "na"
    return f"{float(value) * 100:.1f}%"


def _fmt_num(value: Any) -> str:
    if value is None:
        return "na"
    return f"{float(value):.2f}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _comparison_by_group(report: dict[str, Any], name: str) -> list[dict[str, Any]]:
    rows = list(report["comparisons"][name])
    return sorted(
        rows,
        key=lambda row: (row["current_trades"], row["baseline_trades"]),
        reverse=True,
    )


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = [
        "## May 22 Regression Audit",
        "",
        f"**Generated:** `{report['generated_at']}`",
        f"**Baseline rule:** {report['baseline_rule']}",
        f"**Current rule:** {report['current_rule']}",
        f"**Baseline sessions:** `{', '.join(report['baseline_sessions'])}`",
        f"**Current sessions:** `{', '.join(report['current_sessions'])}`",
        f"**Closed trades:** baseline `{report['baseline_closed_trades']}`, current `{report['current_closed_trades']}`",
        "",
        "### Section A — Session Table",
    ]

    session_rows = []
    session_role: dict[str, str] = {}
    for session_id in report["baseline_sessions"]:
        session_role[session_id] = "baseline"
    for session_id in report["current_sessions"]:
        session_role[session_id] = "current"
    for row in report.get("session_table", []):
        role = session_role.get(row["session_id"], "candidate")
        session_rows.append(
            [
                role,
                row["session_id"],
                str(row["closed_trades"]),
                _fmt_num(row["hours"]),
                _fmt_pct(row["win_rate"]),
                _fmt_money(row["pnl"]),
                _fmt_money(row["avg_pnl"]),
                _fmt_money(row["avg_size"]),
                f"{row['buy_yes']} / {row['buy_no']}",
            ]
        )
    lines.append(
        _table(
            ["role", "session", "n", "hrs", "WR", "PnL", "$/trade", "avg size", "YES / NO"],
            session_rows,
        )
    )

    lines.extend(
        [
            "",
            "### Section B — Baseline Selection",
            "",
            f"- **GOLD rule:** {report['baseline_rule']}",
            f"- **Selected GOLD sessions:** `{', '.join(report['baseline_sessions'])}`",
            f"- **Current comparison rule:** {report['current_rule']}",
            f"- **Selected current sessions:** `{', '.join(report['current_sessions'])}`",
            "",
            "### Section C — Per-Strategy Economics",
        ]
    )

    strategy_rows = []
    for row in _comparison_by_group(report, "strategy"):
        if row["baseline_trades"] == 0 and row["current_trades"] == 0:
            continue
        strategy_rows.append(
            [
                row["group"],
                str(row["baseline_trades"]),
                _fmt_pct(row["baseline_win_rate"]),
                _fmt_money(row["baseline_avg_win"]),
                _fmt_money(row["baseline_avg_loss"]),
                _fmt_num(row["baseline_win_loss_ratio"]),
                _fmt_money(row["baseline_avg_size"]),
                str(row["current_trades"]),
                _fmt_pct(row["current_win_rate"]),
                _fmt_money(row["current_avg_win"]),
                _fmt_money(row["current_avg_loss"]),
                _fmt_num(row["current_win_loss_ratio"]),
                _fmt_money(row["current_avg_size"]),
                _fmt_pct(row["delta_ratio_pct"]),
                _fmt_pct(row["delta_size_pct"]),
                "yes" if row["delta_ratio_pct"] is not None and abs(row["delta_ratio_pct"]) > 0.20 else "",
            ]
        )
    lines.append(
        _table(
            [
                "strategy",
                "base n",
                "base WR",
                "base avg win",
                "base avg loss",
                "base W/L",
                "base size",
                "now n",
                "now WR",
                "now avg win",
                "now avg loss",
                "now W/L",
                "now size",
                "W/L delta",
                "size delta",
                "|W/L| >20%",
            ],
            strategy_rows,
        )
    )

    lines.extend(["", "### Direction Mix"])
    action_rows = []
    for row in _comparison_by_group(report, "strategy_action"):
        action_rows.append(
            [
                row["group"],
                str(row["baseline_trades"]),
                _fmt_pct(row["baseline_win_rate"]),
                _fmt_money(row["baseline_pnl"]),
                str(row["current_trades"]),
                _fmt_pct(row["current_win_rate"]),
                _fmt_money(row["current_pnl"]),
            ]
        )
    lines.append(
        _table(
            ["strategy::action", "base n", "base WR", "base PnL", "now n", "now WR", "now PnL"],
            action_rows,
        )
    )

    lines.extend(["", "### Section D — Per-Side-Source"])
    side_rows = []
    for row in _comparison_by_group(report, "side_source"):
        side_rows.append(
            [
                row["group"],
                str(row["baseline_trades"]),
                _fmt_pct(row["baseline_win_rate"]),
                _fmt_money(row["baseline_avg_pnl"]),
                str(row["current_trades"]),
                _fmt_pct(row["current_win_rate"]),
                _fmt_money(row["current_avg_pnl"]),
                _fmt_money(row["pnl_impact_vs_baseline_avg"]),
            ]
        )
    lines.append(
        _table(
            [
                "side_source",
                "base n",
                "base WR",
                "base $/trade",
                "now n",
                "now WR",
                "now $/trade",
                "$ impact",
            ],
            side_rows,
        )
    )

    lines.extend(["", "### Section E — Per-Exit-Reason"])
    exit_rows = []
    for row in _comparison_by_group(report, "exit_reason"):
        exit_rows.append(
            [
                row["group"],
                str(row["baseline_trades"]),
                _fmt_money(row["baseline_pnl"]),
                _fmt_money(row["baseline_avg_pnl"]),
                _fmt_money(row["baseline_avg_win"]),
                _fmt_money(row["baseline_avg_loss"]),
                str(row["current_trades"]),
                _fmt_money(row["current_pnl"]),
                _fmt_money(row["current_avg_pnl"]),
                _fmt_money(row["current_avg_win"]),
                _fmt_money(row["current_avg_loss"]),
            ]
        )
    lines.append(
        _table(
            [
                "exit reason",
                "base n",
                "base total",
                "base avg",
                "base avg win",
                "base avg loss",
                "now n",
                "now total",
                "now avg",
                "now avg win",
                "now avg loss",
            ],
            exit_rows,
        )
    )

    lines.extend(["", "### Per-Lane WR / Selection"])
    lane_rows = []
    candidates = [
        row
        for row in report["comparisons"]["lane_id"]
        if row["baseline_trades"] >= 3 or row["current_trades"] >= 3
    ]
    candidates.sort(
        key=lambda row: (
            (row["current_avg_pnl"] or 0.0) - (row["baseline_avg_pnl"] or 0.0),
            row["current_trades"],
        )
    )
    for row in candidates[:20]:
        lane_rows.append(
            [
                row["group"],
                str(row["baseline_trades"]),
                _fmt_pct(row["baseline_win_rate"]),
                _fmt_money(row["baseline_avg_pnl"]),
                _fmt_num(row["baseline_win_loss_ratio"]),
                str(row["current_trades"]),
                _fmt_pct(row["current_win_rate"]),
                _fmt_money(row["current_avg_pnl"]),
                _fmt_num(row["current_win_loss_ratio"]),
            ]
        )
    lines.append(
        _table(
            [
                "lane",
                "base n",
                "base WR",
                "base avg",
                "base W/L",
                "now n",
                "now WR",
                "now avg",
                "now W/L",
            ],
            lane_rows,
        )
    )

    lines.extend(["", "### Section F — Hypothesis Ledger"])
    ledger_rows = []
    for row in report.get("hypothesis_ledger", []):
        ledger_rows.append(
            [
                row["strategy"],
                _fmt_money(row["rank_metric_pnl_impact"]),
                row["classification"],
                row["standing"],
                str(row["baseline_trades"]),
                str(row["current_trades"]),
                _fmt_pct(row["delta_size_pct"]),
                _fmt_pct(row["delta_ratio_pct"]),
                _fmt_num(row["delta_win_rate_pct_points"]),
                "; ".join(row.get("selection_lanes", [])[:2]),
            ]
        )
    lines.append(
        _table(
            [
                "strategy",
                "$ impact",
                "classification",
                "standing",
                "base n",
                "now n",
                "size Δ",
                "W/L Δ",
                "WR pp Δ",
                "selection evidence",
            ],
            ledger_rows,
        )
    )

    lines.extend(
        [
            "",
            "### Interpretation Guardrails",
            "",
            "- This report uses closed ENTRY/EXIT pairs from `entries.jsonl` and applies the repo phantom-exit filter.",
            "- Ghost logs cannot validate exit/stop/sizing regressions; this report uses actual journal exits for those economics.",
            "- Groups with fewer than 15 trades are directional evidence, not proof.",
            "- Hypothesis classifications are independent flags: sizing = avg size moved >=10%; exit = W/L ratio moved >=20%; selection = lane WR deterioration or new low-WR current lane.",
            "",
            "### Metadata/Summary",
            "",
            "Tags: #PSB #RegressionAudit #May22Baseline #TradeJournal",
            "Related Concepts: [[May 22 Baseline]], [[Exit Economics]], [[Kelly Sizing]], [[Lane Attribution]]",
            "Summary: This audit compares the May 22 baseline sessions against current paper sessions using closed journal trades. It isolates whether degradation is coming from strategy economics, direction mix, exit reasons, or lane-level admission.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_outputs(report: dict[str, Any], out_dir: Path, stem: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-root", type=Path, default=DEFAULT_PAPER_ROOT)
    parser.add_argument(
        "--ref",
        default="HEAD",
        help="Label the report with the git ref under review. Data still comes from local journals.",
    )
    parser.add_argument(
        "--baseline",
        default=DEFAULT_BASELINE,
        help="Baseline date token for deterministic GOLD selection, e.g. 5/22.",
    )
    parser.add_argument("--baseline-top-n", type=int, default=DEFAULT_BASELINE_TOP_N)
    parser.add_argument("--current-top-n", type=int, default=DEFAULT_CURRENT_TOP_N)
    parser.add_argument("--min-session-trades", type=int, default=DEFAULT_MIN_SESSION_TRADES)
    parser.add_argument(
        "--baseline-session",
        action="append",
        dest="baseline_sessions",
        default=None,
        help="Baseline session id. Repeat for multiple sessions.",
    )
    parser.add_argument(
        "--current-session",
        action="append",
        dest="current_sessions",
        default=None,
        help="Current session id. Repeat for multiple sessions.",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--stem", default=None)
    args = parser.parse_args()

    baseline_rule = "explicit --baseline-session list"
    current_rule = "explicit --current-session list"
    session_candidates: list[SessionSummary] = []

    if args.baseline_sessions:
        baseline_sessions = args.baseline_sessions
        baseline_summaries = summarize_sessions(args.paper_root, baseline_sessions)
    else:
        baseline_sessions, baseline_rule, baseline_summaries = select_gold_sessions(
            args.paper_root,
            baseline=args.baseline,
            top_n=args.baseline_top_n,
            min_trades=args.min_session_trades,
        )

    if args.current_sessions:
        current_sessions = args.current_sessions
        current_summaries = summarize_sessions(args.paper_root, current_sessions)
    else:
        current_sessions, current_rule, current_summaries = select_recent_sessions(
            args.paper_root,
            top_n=args.current_top_n,
            min_trades=args.min_session_trades,
            exclude_sessions=baseline_sessions,
        )

    if not baseline_sessions:
        raise SystemExit("no baseline sessions selected")
    if not current_sessions:
        raise SystemExit("no current sessions selected")

    candidate_by_id = {
        row.session_id: row
        for row in [*baseline_summaries, *current_summaries]
    }
    session_candidates = [
        candidate_by_id[session_id]
        for session_id in sorted(candidate_by_id)
    ]

    report = build_report(
        args.paper_root,
        baseline_sessions,
        current_sessions,
        baseline_rule=f"{baseline_rule} (ref={args.ref})",
        current_rule=f"{current_rule} (ref={args.ref})",
        session_candidates=session_candidates,
    )
    stem = args.stem or f"may22_regression_audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    json_path, md_path = _write_outputs(report, args.out_dir, stem)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
