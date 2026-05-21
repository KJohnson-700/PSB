#!/usr/bin/env python3
"""Probability diagnostics over resolved paper trades."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analysis.time_aware_split import build_purged_time_folds, describe_folds

PAPER_TRADES_ROOT = REPO_ROOT / "data" / "paper_trades"
CALIBRATION_TRADES = REPO_ROOT / "data" / "calibration" / "trades.jsonl"
SETTLED_REJECTS = REPO_ROOT / "data" / "calibration" / "rejected_candidates_settled.jsonl"


def _parse_ts(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _default_output_dir() -> Path:
    return REPO_ROOT / "data" / "reports" / "probability_diagnostics"


def _bucket_label(prob: float, width: float) -> str:
    floor = max(0.0, min(1.0 - width, int(prob / width) * width))
    ceil = min(1.0, floor + width)
    return f"{floor:.2f}-{ceil:.2f}"


def _yes_outcome(exit_row: dict[str, Any]) -> Optional[int]:
    extra = exit_row.get("extra") or {}
    outcome = str(extra.get("outcome_won") or "").upper()
    if outcome == "YES":
        return 1
    if outcome == "NO":
        return 0
    reason = str(exit_row.get("reason") or "")
    if "RESOLVED:YES" in reason:
        return 1
    if "RESOLVED:NO" in reason:
        return 0
    return None


def _norm_side(action: str) -> str:
    return "DOWN" if str(action or "").upper() == "BUY_NO" else "UP"


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class ResolvedTrade:
    trade_id: str
    strategy: str
    window: str
    side: str
    est_prob: float
    yes_price: float
    actual_yes: int
    timestamp: datetime


def _iter_entries_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    files = []
    for session in sorted(root.iterdir()):
        entries = session / "entries.jsonl"
        if session.is_dir() and entries.is_file() and entries.stat().st_size > 0:
            files.append(entries)
    return files


def load_resolved_trades(
    *,
    root: Path = PAPER_TRADES_ROOT,
    cutoff: Optional[datetime] = None,
) -> tuple[list[ResolvedTrade], int]:
    rows: list[ResolvedTrade] = []
    all_exits = 0
    for entries in _iter_entries_files(root):
        with entries.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") != "EXIT":
                    continue
                ts = _parse_ts(row.get("timestamp"))
                if ts is None:
                    continue
                if cutoff is not None and ts < cutoff:
                    continue
                all_exits += 1
                actual_yes = _yes_outcome(row)
                if actual_yes is None:
                    continue
                extra = row.get("extra") or {}
                est_prob = _safe_float(extra.get("raw_est_prob", extra.get("est_prob")))
                yes_price = _safe_float(extra.get("yes_price", row.get("entry_price")))
                if est_prob is None or yes_price is None:
                    continue
                rows.append(
                    ResolvedTrade(
                        trade_id=str(row.get("trade_id") or ""),
                        strategy=str(row.get("strategy") or "?"),
                        window=str(extra.get("window_size") or "?"),
                        side=_norm_side(str(row.get("action") or "")),
                        est_prob=est_prob,
                        yes_price=yes_price,
                        actual_yes=actual_yes,
                        timestamp=ts,
                    )
                )
    rows.sort(key=lambda row: row.timestamp)
    return rows, all_exits


def _count_taken_lanes(cutoff: Optional[datetime]) -> Counter[tuple[str, str, str]]:
    counts: Counter[tuple[str, str, str]] = Counter()
    if not CALIBRATION_TRADES.is_file():
        return counts
    with CALIBRATION_TRADES.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(row.get("ts"))
            if cutoff is not None and (ts is None or ts < cutoff):
                continue
            lane = (
                str(row.get("strategy") or "?"),
                str(row.get("window") or "?"),
                _norm_side(str(row.get("action") or row.get("side") or "")),
            )
            counts[lane] += 1
    return counts


def _load_settled_rejects(cutoff: Optional[datetime]) -> tuple[list[dict[str, Any]], Counter[tuple[str, str, str]]]:
    rows: list[dict[str, Any]] = []
    counts: Counter[tuple[str, str, str]] = Counter()
    if not SETTLED_REJECTS.is_file():
        return rows, counts
    with SETTLED_REJECTS.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(row.get("ts"))
            if cutoff is not None and (ts is None or ts < cutoff):
                continue
            outcome = str(row.get("outcome") or "").upper()
            if outcome not in {"YES", "NO"}:
                continue
            lane = (
                str(row.get("strategy") or "?"),
                str(row.get("window") or "?"),
                "DOWN" if str(row.get("action") or "").upper() == "BUY_NO" or str(row.get("side") or "").upper() == "SHORT" else "UP",
            )
            counts[lane] += 1
            rows.append(
                {
                    "strategy": lane[0],
                    "window": lane[1],
                    "side": lane[2],
                    "yes_price": _safe_float(row.get("yes_price")),
                    "actual_yes": 1 if outcome == "YES" else 0,
                }
            )
    return rows, counts


def brier_score(preds: list[float], ys: list[int]) -> float:
    return sum((pred - actual) ** 2 for pred, actual in zip(preds, ys)) / len(preds)


def murphy_decomposition(
    preds: list[float],
    ys: list[int],
    *,
    bucket_width: float = 0.05,
) -> dict[str, float]:
    base_rate = statistics.mean(ys)
    buckets: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for pred, actual in zip(preds, ys):
        buckets[_bucket_label(pred, bucket_width)].append((pred, actual))
    reliability = 0.0
    resolution = 0.0
    n = len(preds)
    for values in buckets.values():
        nk = len(values)
        avg_pred = statistics.mean(pred for pred, _ in values)
        avg_outcome = statistics.mean(actual for _, actual in values)
        reliability += (nk / n) * ((avg_pred - avg_outcome) ** 2)
        resolution += (nk / n) * ((avg_outcome - base_rate) ** 2)
    uncertainty = base_rate * (1 - base_rate)
    return {
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": uncertainty,
        "brier_from_decomp": reliability - resolution + uncertainty,
    }


def empirical_baseline(
    trades: list[ResolvedTrade],
    settled_rejects: list[dict[str, Any]],
) -> list[float]:
    empirical_rows: list[dict[str, Any]] = [
        {
            "trade_id": trade.trade_id,
            "strategy": trade.strategy,
            "window": trade.window,
            "yes_price_bucket": _bucket_label(trade.yes_price, 0.05),
            "actual_yes": trade.actual_yes,
        }
        for trade in trades
    ]
    empirical_rows.extend(
        {
            "trade_id": "",
            "strategy": row["strategy"],
            "window": row["window"],
            "yes_price_bucket": _bucket_label(row["yes_price"], 0.05) if row["yes_price"] is not None else "",
            "actual_yes": row["actual_yes"],
        }
        for row in settled_rejects
        if row.get("yes_price") is not None
    )

    by_bucket_n: Counter[tuple[str, str, str]] = Counter()
    by_bucket_sum: Counter[tuple[str, str, str]] = Counter()
    by_sw_n: Counter[tuple[str, str]] = Counter()
    by_sw_sum: Counter[tuple[str, str]] = Counter()
    by_window_n: Counter[str] = Counter()
    by_window_sum: Counter[str] = Counter()
    total_yes = 0
    total_n = 0

    for row in empirical_rows:
        strategy = row["strategy"]
        window = row["window"]
        bucket = row["yes_price_bucket"]
        actual_yes = int(row["actual_yes"])
        total_yes += actual_yes
        total_n += 1
        by_sw_n[(strategy, window)] += 1
        by_sw_sum[(strategy, window)] += actual_yes
        by_window_n[window] += 1
        by_window_sum[window] += actual_yes
        if bucket:
            by_bucket_n[(strategy, window, bucket)] += 1
            by_bucket_sum[(strategy, window, bucket)] += actual_yes

    global_rate = (total_yes / total_n) if total_n else 0.5
    baseline: list[float] = []
    for trade in trades:
        bucket = _bucket_label(trade.yes_price, 0.05)
        key = (trade.strategy, trade.window, bucket)
        n = by_bucket_n[key] - 1
        s = by_bucket_sum[key] - trade.actual_yes
        if n >= 20:
            baseline.append(s / n)
            continue
        sw = (trade.strategy, trade.window)
        n = by_sw_n[sw] - 1
        s = by_sw_sum[sw] - trade.actual_yes
        if n >= 30:
            baseline.append(s / n)
            continue
        n = by_window_n[trade.window] - 1
        s = by_window_sum[trade.window] - trade.actual_yes
        if n >= 50:
            baseline.append(s / n)
            continue
        baseline.append(global_rate)
    return baseline


def build_report(
    *,
    root: Path = PAPER_TRADES_ROOT,
    cutoff: Optional[datetime] = None,
    bucket_width: float = 0.05,
    n_splits: int = 5,
    purge_minutes: int = 15,
) -> dict[str, Any]:
    trades, all_exits = load_resolved_trades(root=root, cutoff=cutoff)
    taken_counts = _count_taken_lanes(cutoff)
    settled_rejects, reject_counts = _load_settled_rejects(cutoff)
    ys = [trade.actual_yes for trade in trades]
    preds = [trade.est_prob for trade in trades]
    market = [trade.yes_price for trade in trades]
    empirical = empirical_baseline(trades, settled_rejects)
    pooled_base_rate = statistics.mean(ys) if ys else 0.0
    constant = [pooled_base_rate] * len(ys)
    overall_decomp = (
        murphy_decomposition(preds, ys, bucket_width=bucket_width)
        if ys else
        {
            "reliability": 0.0,
            "resolution": 0.0,
            "uncertainty": 0.0,
            "brier_from_decomp": 0.0,
        }
    )
    folds = build_purged_time_folds(
        [trade.timestamp for trade in trades],
        n_splits=n_splits,
        purge=timedelta(minutes=purge_minutes),
        min_train_size=max(1, len(trades) // max(n_splits, 2)),
    )

    by_lane: dict[str, list[ResolvedTrade]] = defaultdict(list)
    for trade in trades:
        lane_key = f"{trade.strategy}|{trade.window}|{trade.side}"
        by_lane[lane_key].append(trade)

    lane_rows: list[dict[str, Any]] = []
    for lane_key, lane_trades in sorted(by_lane.items(), key=lambda item: (-len(item[1]), item[0])):
        lane_ys = [trade.actual_yes for trade in lane_trades]
        lane_preds = [trade.est_prob for trade in lane_trades]
        lane_market = [trade.yes_price for trade in lane_trades]
        lane_emp = empirical_baseline(lane_trades, settled_rejects)
        strategy, window, side = lane_key.split("|")
        taken = taken_counts[(strategy, window, side)]
        rejected = reject_counts[(strategy, window, side)]
        take_rate = (taken / (taken + rejected)) if (taken + rejected) else None
        lane_base_rate = statistics.mean(lane_ys)
        lane_const = [lane_base_rate] * len(lane_ys)
        lane_rows.append(
            {
                "lane": lane_key,
                "strategy": strategy,
                "window": window,
                "side": side,
                "n_trades": len(lane_trades),
                "take_rate": take_rate,
                "base_rate": lane_base_rate,
                "avg_est_prob": statistics.mean(lane_preds),
                "avg_yes_price": statistics.mean(lane_market),
                "brier_model": brier_score(lane_preds, lane_ys),
                "brier_constant": brier_score(lane_const, lane_ys),
                "brier_market": brier_score(lane_market, lane_ys),
                "brier_table": brier_score(lane_emp, lane_ys),
                **murphy_decomposition(lane_preds, lane_ys, bucket_width=bucket_width),
            }
        )

    return {
        "cutoff": cutoff.isoformat() if cutoff is not None else None,
        "entries_root": str(root.resolve()),
        "eligible_resolved_trades": len(trades),
        "all_exits_after_cutoff": all_exits,
        "settled_rejected_candidates": len(settled_rejects),
        "overall": {
            "n_trades": len(trades),
            "take_rate": (sum(taken_counts.values()) / (sum(taken_counts.values()) + sum(reject_counts.values()))) if (sum(taken_counts.values()) + sum(reject_counts.values())) else None,
            "base_rate": pooled_base_rate,
            "avg_est_prob": statistics.mean(preds) if preds else 0.0,
            "avg_yes_price": statistics.mean(market) if market else 0.0,
            "brier_model": brier_score(preds, ys) if preds else 0.0,
            "brier_constant": brier_score(constant, ys) if ys else 0.0,
            "brier_market": brier_score(market, ys) if ys else 0.0,
            "brier_table": brier_score(empirical, ys) if ys else 0.0,
            **overall_decomp,
        },
        "time_aware_folds": describe_folds([trade.timestamp for trade in trades], folds),
        "lanes": lane_rows,
    }


def _build_svg(points_by_series: dict[str, list[dict[str, Any]]], title: str) -> str:
    width = 640
    height = 640
    margin = 70
    plot_w = width - 2 * margin
    plot_h = height - 2 * margin

    def sx(x: float) -> float:
        return margin + x * plot_w

    def sy(y: float) -> float:
        return height - margin - y * plot_h

    series_colors = {"model": "#1f77b4", "market": "#ff7f0e"}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2}" y="30" text-anchor="middle" font-family="Arial" font-size="20">{title}</text>',
    ]
    for tick in [0, 0.25, 0.5, 0.75, 1.0]:
        parts.append(f'<line x1="{sx(tick):.1f}" y1="{sy(0):.1f}" x2="{sx(tick):.1f}" y2="{sy(1):.1f}" stroke="#eeeeee"/>')
        parts.append(f'<line x1="{sx(0):.1f}" y1="{sy(tick):.1f}" x2="{sx(1):.1f}" y2="{sy(tick):.1f}" stroke="#eeeeee"/>')
        parts.append(f'<text x="{sx(tick):.1f}" y="{height - margin + 22}" text-anchor="middle" font-family="Arial" font-size="12">{tick:.2f}</text>')
        parts.append(f'<text x="{margin - 12}" y="{sy(tick) + 4:.1f}" text-anchor="end" font-family="Arial" font-size="12">{tick:.2f}</text>')
    parts.append(f'<line x1="{sx(0):.1f}" y1="{sy(0):.1f}" x2="{sx(1):.1f}" y2="{sy(1):.1f}" stroke="#999999" stroke-dasharray="6,4"/>')
    parts.append(f'<rect x="{margin}" y="{margin}" width="{plot_w}" height="{plot_h}" fill="none" stroke="#444"/>')
    for label, points in points_by_series.items():
        color = series_colors[label]
        if not points:
            continue
        path = " ".join(
            ("M" if idx == 0 else "L") + f' {sx(point["x"]):.1f} {sy(point["y"]):.1f}'
            for idx, point in enumerate(points)
        )
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        for point in points:
            parts.append(f'<circle cx="{sx(point["x"]):.1f}" cy="{sy(point["y"]):.1f}" r="4" fill="{color}"/>')
            if point["n"] >= 10:
                parts.append(f'<text x="{sx(point["x"]) + 6:.1f}" y="{sy(point["y"]) - 6:.1f}" font-family="Arial" font-size="10" fill="{color}">{point["n"]}</text>')
    parts.append(f'<text x="{width / 2}" y="{height - 18}" text-anchor="middle" font-family="Arial" font-size="14">Predicted YES probability</text>')
    parts.append(f'<text x="22" y="{height / 2}" text-anchor="middle" transform="rotate(-90 22,{height / 2})" font-family="Arial" font-size="14">Observed YES frequency</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def _series_points(preds: list[float], ys: list[int], width: float) -> list[dict[str, Any]]:
    buckets: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for pred, actual in zip(preds, ys):
        buckets[_bucket_label(pred, width)].append((pred, actual))
    out = []
    for values in buckets.values():
        out.append(
            {
                "x": statistics.mean(pred for pred, _ in values),
                "y": statistics.mean(actual for _, actual in values),
                "n": len(values),
            }
        )
    return sorted(out, key=lambda point: point["x"])


def write_artifacts(report: dict[str, Any], trades: list[ResolvedTrade], *, out_dir: Path, bucket_width: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        "# Probability Diagnostics",
        "",
        f"- Eligible resolved trades: **{report['eligible_resolved_trades']}**",
        f"- All exits after cutoff: **{report['all_exits_after_cutoff']}**",
        f"- Settled rejected candidates: **{report['settled_rejected_candidates']}**",
        f"- Cutoff: **{report['cutoff'] or 'none'}**",
        "",
        "## Overall",
    ]
    for key, value in report["overall"].items():
        if isinstance(value, float):
            lines.append(f"- {key}: **{value:.4f}**")
        else:
            lines.append(f"- {key}: **{value}**")
    lines.extend(
        [
            "",
            "## Lanes",
            "| lane | n | take_rate | brier_model | brier_market | brier_table | reliability | resolution |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report["lanes"]:
        take_rate = "n/a" if row["take_rate"] is None else f"{row['take_rate']:.3f}"
        lines.append(
            f"| {row['lane']} | {row['n_trades']} | {take_rate} | {row['brier_model']:.4f} | "
            f"{row['brier_market']:.4f} | {row['brier_table']:.4f} | {row['reliability']:.4f} | {row['resolution']:.4f} |"
        )
    if report["time_aware_folds"]:
        lines.extend(
            [
                "",
                "## Time-Aware Folds",
                "| fold | train_n | test_n | train_start | train_end | test_start | test_end |",
                "|---|---:|---:|---|---|---|---|",
            ]
        )
        for fold in report["time_aware_folds"]:
            lines.append(
                f"| {fold['fold']} | {fold['train_n']} | {fold['test_n']} | {fold['train_start']} | "
                f"{fold['train_end']} | {fold['test_start']} | {fold['test_end']} |"
            )
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ys = [trade.actual_yes for trade in trades]
    preds = [trade.est_prob for trade in trades]
    market = [trade.yes_price for trade in trades]
    svg = _build_svg(
        {
            "model": _series_points(preds, ys, bucket_width),
            "market": _series_points(market, ys, bucket_width),
        },
        "Probability Reliability",
    )
    (out_dir / "reliability_pooled.svg").write_text(svg, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probability diagnostics over resolved paper trades.")
    parser.add_argument("--root", type=Path, default=PAPER_TRADES_ROOT)
    parser.add_argument("--cutoff", type=str, default=None)
    parser.add_argument("--bucket-width", type=float, default=0.05)
    parser.add_argument("--out-dir", type=Path, default=_default_output_dir())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--n-splits", type=int, default=5)
    parser.add_argument("--purge-minutes", type=int, default=15)
    args = parser.parse_args()

    cutoff = _parse_ts(args.cutoff) if args.cutoff else None
    trades, _ = load_resolved_trades(root=args.root, cutoff=cutoff)
    report = build_report(
        root=args.root,
        cutoff=cutoff,
        bucket_width=float(args.bucket_width),
        n_splits=int(args.n_splits),
        purge_minutes=int(args.purge_minutes),
    )
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    write_artifacts(report, trades, out_dir=args.out_dir, bucket_width=float(args.bucket_width))
    print(f"Wrote diagnostics to {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
