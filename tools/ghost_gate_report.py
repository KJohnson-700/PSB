#!/usr/bin/env python3
"""Summarize settled rejected-candidate outcomes by lane, gate, and probe variant.

Reads ``data/calibration/rejected_candidates_settled.jsonl`` and produces:

1. Lane-level ghost calibration summaries.
2. Gate rankings by missed EV and protected loss.
3. Optional probe-variant sensitivity summaries for threshold relax/tighten data.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTLED = REPO_ROOT / "data" / "calibration" / "rejected_candidates_settled.jsonl"


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
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
                yield obj


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        if out != out:
            return None
        return out
    except (TypeError, ValueError):
        return None


def _passes_filters(row: Dict[str, Any], args: argparse.Namespace) -> bool:
    if args.strategy and str(row.get("strategy") or "") != args.strategy:
        return False
    if args.reason and str(row.get("reason") or "") != args.reason:
        return False
    if args.action and str(row.get("action") or "") != args.action:
        return False
    if args.lane:
        lane = str(row.get("lane_id") or "")
        if not (lane == args.lane or lane.startswith(args.lane)):
            return False
    if args.since and str(row.get("ts") or "") < args.since:
        return False
    return True


def _econ_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    realized = [_as_float(r.get("realized_pct")) or 0.0 for r in rows]
    wins = sum(1 for r in rows if bool(r.get("win")) is True)
    losses = sum(1 for r in rows if bool(r.get("win")) is False)
    n = len(rows)
    missed_ev = sum(max(v, 0.0) for v in realized)
    protected_loss = sum(max(-v, 0.0) for v in realized)
    total_realized = sum(realized)
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / n) if n else 0.0, 4),
        "avg_realized_pct": round((total_realized / n) if n else 0.0, 6),
        "total_realized_pct": round(total_realized, 6),
        "missed_ev_pct": round(missed_ev, 6),
        "protected_loss_pct": round(protected_loss, 6),
        "net_gate_value_pct": round(protected_loss - missed_ev, 6),
    }


def aggregate_lanes(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get("lane_id") or "unknown")].append(row)
    out: List[Dict[str, Any]] = []
    for lane_id, bucket in buckets.items():
        sample = bucket[0]
        metrics = _econ_metrics(bucket)
        out.append(
            {
                "lane_id": lane_id,
                "strategy": sample.get("strategy"),
                "window": sample.get("window"),
                "action": sample.get("action"),
                "reason_samples": sorted({str(r.get("reason") or "") for r in bucket}),
                **metrics,
            }
        )
    out.sort(key=lambda r: (abs(float(r["net_gate_value_pct"])), int(r["n"])), reverse=True)
    return out


def aggregate_gates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = "|".join(
            [
                str(row.get("strategy") or ""),
                str(row.get("window") or ""),
                str(row.get("action") or ""),
                str(row.get("reason") or ""),
            ]
        )
        buckets[key].append(row)
    out: List[Dict[str, Any]] = []
    for gate_key, bucket in buckets.items():
        sample = bucket[0]
        metrics = _econ_metrics(bucket)
        out.append(
            {
                "gate_key": gate_key,
                "strategy": sample.get("strategy"),
                "window": sample.get("window"),
                "action": sample.get("action"),
                "reason": sample.get("reason"),
                **metrics,
            }
        )
    out.sort(key=lambda r: (abs(float(r["net_gate_value_pct"])), int(r["n"])), reverse=True)
    return out


def aggregate_probes(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[float]] = defaultdict(list)
    counts: Dict[str, int] = defaultdict(int)
    meta: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        realized = _as_float(row.get("realized_pct"))
        if realized is None:
            continue
        for probe in row.get("probe_variants") or []:
            if not isinstance(probe, dict):
                continue
            if not bool(probe.get("would_pass")):
                continue
            key = "|".join(
                [
                    str(row.get("strategy") or ""),
                    str(row.get("window") or ""),
                    str(row.get("action") or ""),
                    str(row.get("reason") or ""),
                    str(probe.get("probe") or ""),
                    str(probe.get("kind") or ""),
                    f"{_as_float(probe.get('delta')) or 0.0:.6f}",
                ]
            )
            buckets[key].append(realized)
            counts[key] += 1
            meta[key] = {
                "strategy": row.get("strategy"),
                "window": row.get("window"),
                "action": row.get("action"),
                "reason": row.get("reason"),
                "probe": probe.get("probe"),
                "kind": probe.get("kind"),
                "delta": round(_as_float(probe.get("delta")) or 0.0, 6),
            }
    out: List[Dict[str, Any]] = []
    for key, realized_values in buckets.items():
        n = counts[key]
        missed_ev = sum(max(v, 0.0) for v in realized_values)
        protected_loss = sum(max(-v, 0.0) for v in realized_values)
        total = sum(realized_values)
        wins = sum(1 for v in realized_values if v > 0)
        payload = dict(meta[key])
        payload.update(
            {
                "variant_key": key,
                "n": n,
                "win_rate": round((wins / n) if n else 0.0, 4),
                "avg_realized_pct": round((total / n) if n else 0.0, 6),
                "total_realized_pct": round(total, 6),
                "missed_ev_pct": round(missed_ev, 6),
                "protected_loss_pct": round(protected_loss, 6),
                "net_gate_value_pct": round(protected_loss - missed_ev, 6),
            }
        )
        out.append(payload)
    out.sort(key=lambda r: (str(r["probe"]), str(r["kind"]), float(r["delta"])))
    return out


def build_report(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    lane_rows = aggregate_lanes(rows)
    gate_rows = aggregate_gates(rows)
    probe_rows = aggregate_probes(rows)
    return {
        "rows": len(rows),
        "overall": _econ_metrics(rows),
        "lanes": lane_rows,
        "gates": gate_rows,
        "top_missed_ev": sorted(gate_rows, key=lambda r: (float(r["missed_ev_pct"]), int(r["n"])), reverse=True)[:20],
        "top_protected_loss": sorted(gate_rows, key=lambda r: (float(r["protected_loss_pct"]), int(r["n"])), reverse=True)[:20],
        "probe_variants": probe_rows,
    }


def _fmt_simple_table(rows: List[Dict[str, Any]], kind: str, limit: int) -> str:
    if not rows:
        return "(no rows)"
    items = rows[:limit]
    if kind == "lanes":
        header = (
            "lane_id".ljust(50)
            + "  n   WR    avg%    total%   missedEV  protLoss  netGate"
        )
        lines = [header, "-" * len(header)]
        for row in items:
            lines.append(
                f"{str(row['lane_id'])[:50].ljust(50)}  "
                f"{int(row['n']):>3d}  "
                f"{float(row['win_rate'])*100:>5.1f}%  "
                f"{float(row['avg_realized_pct']):>+7.3f}  "
                f"{float(row['total_realized_pct']):>+8.3f}  "
                f"{float(row['missed_ev_pct']):>8.3f}  "
                f"{float(row['protected_loss_pct']):>8.3f}  "
                f"{float(row['net_gate_value_pct']):>+7.3f}"
            )
        return "\n".join(lines)
    header = (
        "gate_key".ljust(58)
        + "  n   WR    avg%    missedEV  protLoss  netGate"
    )
    lines = [header, "-" * len(header)]
    for row in items:
        lines.append(
            f"{str(row['gate_key'])[:58].ljust(58)}  "
            f"{int(row['n']):>3d}  "
            f"{float(row['win_rate'])*100:>5.1f}%  "
            f"{float(row['avg_realized_pct']):>+7.3f}  "
            f"{float(row['missed_ev_pct']):>8.3f}  "
            f"{float(row['protected_loss_pct']):>8.3f}  "
            f"{float(row['net_gate_value_pct']):>+7.3f}"
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Settled ghost gate report.")
    parser.add_argument("--path", type=Path, default=DEFAULT_SETTLED)
    parser.add_argument("--strategy")
    parser.add_argument("--reason")
    parser.add_argument("--action")
    parser.add_argument("--lane")
    parser.add_argument("--since")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.path.exists():
        sys.stderr.write(f"(no settled ghost log at {args.path})\n")
        return 0

    rows = [row for row in _iter_jsonl(args.path) if _passes_filters(row, args)]
    report = build_report(rows)
    if args.json:
        json.dump({"path": str(args.path), **report}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"# ghost gate report — {args.path}  (rows: {len(rows)})")
    print(
        "overall:"
        f" n={report['overall']['n']}"
        f" wr={report['overall']['win_rate']*100:.1f}%"
        f" avg={report['overall']['avg_realized_pct']:+.3f}"
        f" missed_ev={report['overall']['missed_ev_pct']:.3f}"
        f" protected_loss={report['overall']['protected_loss_pct']:.3f}"
        f" net_gate={report['overall']['net_gate_value_pct']:+.3f}"
    )
    print("\n## lanes")
    print(_fmt_simple_table(report["lanes"], "lanes", args.limit))
    print("\n## top missed ev gates")
    print(_fmt_simple_table(report["top_missed_ev"], "gates", args.limit))
    print("\n## top protected loss gates")
    print(_fmt_simple_table(report["top_protected_loss"], "gates", args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
