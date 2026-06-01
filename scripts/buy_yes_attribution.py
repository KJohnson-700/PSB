#!/usr/bin/env python3
"""Read-only BUY_YES attribution report for closed calibration trades."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEFAULT_TARGETS = {
    "xrp_macro|5m|xrp_5m_native",
    "xrp_macro|15m|xrp_15m_native",
    "xrp_macro|5m|xrp_5m_neutral_fallback_1h",
    "eth_macro|5m|eth_5m_native",
    "eth_macro|15m|eth_15m_native",
    "eth_macro|1h|eth_1h_native",
    "eth_macro|1h|drift",
    "eth_macro|15m|drift",
    "bitcoin|15m|htf_bullish_side_long",
    "hype_macro|15m|spike",
}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _extra(row: dict[str, Any]) -> dict[str, Any]:
    extra = row.get("extra") or row.get("entry_signal") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            extra = {}
    return extra if isinstance(extra, dict) else {}


def _win(row: dict[str, Any]) -> bool:
    for key in ("win", "won", "is_win"):
        if key in row:
            return bool(row[key])
    value = _num(row.get("pnl") or row.get("realized_pnl") or row.get("profit_loss"))
    return bool(value is not None and value > 0)


def _pnl(row: dict[str, Any]) -> float:
    for key in ("pnl", "realized_pnl", "profit_loss"):
        value = _num(row.get(key))
        if value is not None:
            return value
    return 0.0


def _bucket(value: float | None, width: float, *, missing: str = "missing") -> str:
    if value is None:
        return missing
    floor = math.floor(value / width) * width
    ceil = floor + width
    return f"{floor:.2f}-{ceil:.2f}"


def _basis_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    abs_v = abs(value)
    if abs_v < 10:
        return "abs<10bps"
    if abs_v < 20:
        return "abs10-20bps"
    if abs_v < 40:
        return "abs20-40bps"
    return "abs>=40bps"


def _minutes_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 1:
        return "<1m"
    if value < 5:
        return "1-5m"
    if value < 15:
        return "5-15m"
    if value < 60:
        return "15-60m"
    return "60m+"


def _row_features(row: dict[str, Any]) -> dict[str, Any]:
    extra = _extra(row)
    lane_id = str(extra.get("lane_id") or row.get("lane_id") or "")
    parts = lane_id.split("|")
    strategy = str(row.get("strategy") or extra.get("strategy") or (parts[0] if parts else ""))
    window = str(row.get("window") or extra.get("window_size") or (parts[1] if len(parts) > 1 else ""))
    family = str(extra.get("entry_family") or extra.get("lane_family") or (parts[4] if len(parts) > 4 else ""))
    price = _num(row.get("entry_price") or row.get("yes_price") or extra.get("yes_price"))
    raw_prob = _num(extra.get("raw_est_prob") or row.get("raw_est_prob"))
    est_prob = _num(extra.get("estimated_prob") or extra.get("est_prob") or row.get("estimated_prob"))
    edge = (raw_prob - price) if raw_prob is not None and price is not None else _num(row.get("edge") or extra.get("edge"))
    basis = _num(extra.get("oracle_basis_bps") or row.get("oracle_basis_bps"))
    mins = _num(extra.get("minutes_to_market_end") or extra.get("mins_left") or row.get("minutes_to_market_end"))
    return {
        "strategy": strategy,
        "window": window,
        "family": family,
        "lane_key": f"{strategy}|{window}|{family}",
        "price": price,
        "raw_prob": raw_prob,
        "est_prob": est_prob,
        "edge": edge,
        "basis": basis,
        "minutes_left": mins,
        "side_source": str(extra.get("side_source") or extra.get("resolver_path") or ""),
        "primary_htf_bias": str(extra.get("primary_htf_bias") or extra.get("htf_bias") or ""),
        "alt_htf_bias": str(extra.get("alt_htf_bias") or ""),
        "btc_1h_regime": str(extra.get("btc_1h_regime") or ""),
        "win": _win(row),
        "pnl": _pnl(row),
    }


def load_rows(paths: Iterable[Path], *, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                action = str(row.get("action") or row.get("entry_action") or "").upper()
                if action != "BUY_YES":
                    continue
                ts = _parse_dt(
                    row.get("closed_at")
                    or row.get("exit_ts")
                    or row.get("timestamp")
                    or row.get("entry_ts")
                    or row.get("opened_at")
                )
                if ts is None or not (start <= ts < end):
                    continue
                key = str(row.get("trade_id") or row.get("id") or f"{path}:{len(rows)}")
                if key in seen:
                    continue
                seen.add(key)
                rows.append(_row_features(row))
    return rows


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for r in rows if r["win"])
    values = {
        "n": n,
        "wins": wins,
        "wr": wins / n if n else 0.0,
        "pnl": sum(float(r["pnl"]) for r in rows),
    }
    for key in ("price", "raw_prob", "est_prob", "edge", "basis", "minutes_left"):
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        values[f"avg_{key}"] = statistics.mean(vals) if vals else None
    return values


def _group(rows: list[dict[str, Any]], key: str, *, min_n: int) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "missing")].append(row)
    return {
        name: _summary(group)
        for name, group in sorted(groups.items())
        if len(group) >= min_n
    }


def build_report(rows: list[dict[str, Any]], *, min_n: int) -> dict[str, Any]:
    target_rows = [r for r in rows if str(r["lane_key"]) in DEFAULT_TARGETS]
    bucketed = []
    for lane, lane_rows in sorted(_collect(target_rows, "lane_key").items()):
        bucketed.append(
            {
                "lane": lane,
                "overall": _summary(lane_rows),
                "by_price_bucket": _group(
                    [{**r, "bucket": _bucket(r.get("price"), 0.02)} for r in lane_rows],
                    "bucket",
                    min_n=min_n,
                ),
                "by_raw_prob_bucket": _group(
                    [{**r, "bucket": _bucket(r.get("raw_prob"), 0.05)} for r in lane_rows],
                    "bucket",
                    min_n=min_n,
                ),
                "by_edge_bucket": _group(
                    [{**r, "bucket": _bucket(r.get("edge"), 0.02)} for r in lane_rows],
                    "bucket",
                    min_n=min_n,
                ),
                "by_basis_bucket": _group(
                    [{**r, "bucket": _basis_bucket(r.get("basis"))} for r in lane_rows],
                    "bucket",
                    min_n=min_n,
                ),
                "by_minutes_bucket": _group(
                    [{**r, "bucket": _minutes_bucket(r.get("minutes_left"))} for r in lane_rows],
                    "bucket",
                    min_n=min_n,
                ),
                "by_regime": _group(
                    [
                        {
                            **r,
                            "regime": "|".join(
                                [
                                    str(r.get("primary_htf_bias") or "na"),
                                    str(r.get("alt_htf_bias") or "na"),
                                    str(r.get("btc_1h_regime") or "na"),
                                ]
                            ),
                        }
                        for r in lane_rows
                    ],
                    "regime",
                    min_n=min_n,
                ),
            }
        )
    return {
        "overall": _summary(rows),
        "targets": bucketed,
        "by_lane": _group(rows, "lane_key", min_n=min_n),
    }


def _collect(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "missing")].append(row)
    return groups


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def to_markdown(report: dict[str, Any], *, start: datetime, end: datetime) -> str:
    lines = [
        "# BUY_YES Attribution Report",
        "",
        f"- **Window UTC:** `{start.isoformat()}` to `{end.isoformat()}`",
        f"- **Overall:** n={report['overall']['n']} WR={report['overall']['wr']:.1%} PnL={report['overall']['pnl']:+.2f}",
        "",
        "## Target Lanes",
        "",
        "| Lane | n | WR | PnL | avg price | avg raw prob | avg edge | avg basis |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["targets"]:
        s = item["overall"]
        lines.append(
            f"| `{item['lane']}` | {s['n']} | {s['wr']:.1%} | {s['pnl']:+.2f} | "
            f"{_fmt(s['avg_price'])} | {_fmt(s['avg_raw_prob'])} | {_fmt(s['avg_edge'])} | {_fmt(s['avg_basis'], 1)} |"
        )
    for item in report["targets"]:
        lines.extend(["", f"### `{item['lane']}`", ""])
        for title, key in (
            ("Price Bucket", "by_price_bucket"),
            ("Raw Probability Bucket", "by_raw_prob_bucket"),
            ("Raw Edge Bucket", "by_edge_bucket"),
            ("Oracle Basis Bucket", "by_basis_bucket"),
            ("Minutes Left Bucket", "by_minutes_bucket"),
            ("Regime Context", "by_regime"),
        ):
            lines.extend([f"#### {title}", "", "| Bucket | n | WR | PnL | avg raw | avg edge |", "|---|---:|---:|---:|---:|---:|"])
            groups = item.get(key) or {}
            if not groups:
                lines.append("| n/a | 0 | n/a | n/a | n/a | n/a |")
            else:
                for bucket, stats in groups.items():
                    lines.append(
                        f"| `{bucket}` | {stats['n']} | {stats['wr']:.1%} | {stats['pnl']:+.2f} | "
                        f"{_fmt(stats['avg_raw_prob'])} | {_fmt(stats['avg_edge'])} |"
                    )
            lines.append("")
    lines.append("")
    lines.append("## Repair Notes")
    lines.append("")
    lines.append("- Active repairs should use probability haircuts or min-edge adders only; no lane disable or family allowlist.")
    lines.append("- BTC htf-bullish BUY_YES is report-only for this pass because the lane was low-WR but positive-PnL and not probability-inflated.")
    lines.append("- ETH/XRP/HYPE spike cohorts show raw probability materially above realized hit rate, so the implemented repairs target overconfidence.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Start date/time in the selected timezone, e.g. 2026-05-29")
    parser.add_argument("--end", required=True, help="End date/time in the selected timezone, e.g. 2026-06-01")
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument("--min-n", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    tz = ZoneInfo(args.timezone)
    start_local = datetime.fromisoformat(args.start).replace(tzinfo=tz)
    end_local = datetime.fromisoformat(args.end).replace(tzinfo=tz)
    start = start_local.astimezone(timezone.utc)
    end = end_local.astimezone(timezone.utc)
    paths = [
        Path("data/calibration/trades.jsonl"),
        Path("data/calibration/trades_settled.jsonl"),
    ]
    rows = load_rows(paths, start=start, end=end)
    report = build_report(rows, min_n=max(1, int(args.min_n)))
    payload = json.dumps(report, indent=2) if args.json else to_markdown(report, start=start, end=end)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
