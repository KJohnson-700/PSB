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
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.analysis.ghost_calibration import (
    DEFAULT_REGIME_LOG,
    REGIME_MATCH_MAX_AGE_SEC,
    enrich_with_regime,
    load_regime_snapshots,
)


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
    if args.price_regime and str(row.get("price_regime") or "") != args.price_regime:
        return False
    if (
        args.polymarket_regime
        and str(row.get("polymarket_regime") or "") != args.polymarket_regime
    ):
        return False
    if args.combined_regime and str(row.get("combined_regime") or "") != args.combined_regime:
        return False
    if args.btc_1h_regime and str(row.get("btc_1h_regime") or "") != args.btc_1h_regime:
        return False
    return True


def _wilson_interval(wins: int, n: int, z: float = 1.96) -> Dict[str, float]:
    if n <= 0:
        return {"win_rate_ci_low": 0.0, "win_rate_ci_high": 0.0}
    phat = wins / n
    denom = 1.0 + (z * z / n)
    centre = (phat + (z * z / (2.0 * n))) / denom
    radius = z * math.sqrt((phat * (1.0 - phat) + (z * z / (4.0 * n))) / n) / denom
    return {
        "win_rate_ci_low": round(max(0.0, centre - radius), 4),
        "win_rate_ci_high": round(min(1.0, centre + radius), 4),
    }


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
        **_wilson_interval(wins, n),
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


def aggregate_regimes(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = "|".join(
            [
                str(row.get("price_regime") or "unknown"),
                str(row.get("polymarket_regime") or "unknown"),
                str(row.get("combined_regime") or "unknown"),
            ]
        )
        buckets[key].append(row)

    out: List[Dict[str, Any]] = []
    for regime_key, bucket in buckets.items():
        sample = bucket[0]
        out.append(
            {
                "regime_key": regime_key,
                "price_regime": sample.get("price_regime") or "unknown",
                "polymarket_regime": sample.get("polymarket_regime") or "unknown",
                "combined_regime": sample.get("combined_regime") or "unknown",
                **_econ_metrics(bucket),
            }
        )
    out.sort(key=lambda r: (int(r["n"]), abs(float(r["net_gate_value_pct"]))), reverse=True)
    return out


def aggregate_regime_gates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = "|".join(
            [
                str(row.get("combined_regime") or "unknown"),
                str(row.get("strategy") or ""),
                str(row.get("window") or ""),
                str(row.get("action") or ""),
                str(row.get("reason") or ""),
            ]
        )
        buckets[key].append(row)

    out: List[Dict[str, Any]] = []
    for regime_gate_key, bucket in buckets.items():
        sample = bucket[0]
        out.append(
            {
                "regime_gate_key": regime_gate_key,
                "combined_regime": sample.get("combined_regime") or "unknown",
                "strategy": sample.get("strategy"),
                "window": sample.get("window"),
                "action": sample.get("action"),
                "reason": sample.get("reason"),
                **_econ_metrics(bucket),
            }
        )
    out.sort(key=lambda r: (abs(float(r["net_gate_value_pct"])), int(r["n"])), reverse=True)
    return out


def _convergence_bucket(value: Any) -> str:
    score = _as_float(value)
    if score is None:
        return "unknown"
    if score >= 0.75:
        return "high"
    if score >= 0.55:
        return "medium"
    return "low"


def aggregate_btc_regimes(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get("btc_1h_regime") or "unknown")].append(row)
    out: List[Dict[str, Any]] = []
    for key, bucket in buckets.items():
        out.append({"btc_1h_regime": key, **_econ_metrics(bucket)})
    out.sort(key=lambda r: (int(r["n"]), abs(float(r["net_gate_value_pct"]))), reverse=True)
    return out


def aggregate_convergence(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[_convergence_bucket(row.get("convergence_score"))].append(row)
    out: List[Dict[str, Any]] = []
    for key, bucket in buckets.items():
        avg = [
            _as_float(r.get("convergence_score"))
            for r in bucket
            if _as_float(r.get("convergence_score")) is not None
        ]
        payload = {"convergence_bucket": key, **_econ_metrics(bucket)}
        payload["avg_convergence_score"] = round(sum(avg) / len(avg), 6) if avg else None
        out.append(payload)
    out.sort(key=lambda r: (int(r["n"]), abs(float(r["net_gate_value_pct"]))), reverse=True)
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
                "wins": wins,
                "losses": n - wins,
                "win_rate": round((wins / n) if n else 0.0, 4),
                "avg_realized_pct": round((total / n) if n else 0.0, 6),
                "total_realized_pct": round(total, 6),
                "missed_ev_pct": round(missed_ev, 6),
                "protected_loss_pct": round(protected_loss, 6),
                "net_gate_value_pct": round(protected_loss - missed_ev, 6),
                **_wilson_interval(wins, n),
            }
        )
        out.append(payload)
    out.sort(key=lambda r: (str(r["probe"]), str(r["kind"]), float(r["delta"])))
    return out


def build_probe_relax_recommendations(
    probe_rows: List[Dict[str, Any]],
    *,
    min_n: int = 100,
    min_ci_low: float = 0.50,
) -> List[Dict[str, Any]]:
    """Recommend conservative relax deltas for probe-aware gate families.

    Picks the smallest relax delta per gate/probe bucket that has enough sample,
    a Wilson lower bound above ``min_ci_low``, and negative net gate value
    (meaning the gate likely blocked more value than it saved).
    """
    by_gate_probe: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in probe_rows:
        if str(row.get("kind") or "") != "relax":
            continue
        key = "|".join(
            [
                str(row.get("strategy") or ""),
                str(row.get("window") or ""),
                str(row.get("action") or ""),
                str(row.get("reason") or ""),
                str(row.get("probe") or ""),
            ]
        )
        by_gate_probe[key].append(row)

    out: List[Dict[str, Any]] = []
    for gate_probe_key, candidates in by_gate_probe.items():
        candidates.sort(key=lambda row: (float(row["delta"]), -int(row["n"])))
        chosen: Optional[Dict[str, Any]] = None
        for row in candidates:
            if int(row["n"]) < min_n:
                continue
            if float(row["win_rate_ci_low"]) <= min_ci_low:
                continue
            if float(row["net_gate_value_pct"]) >= 0.0:
                continue
            chosen = row
            break
        if chosen is None:
            continue

        payload = dict(chosen)
        payload["gate_probe_key"] = gate_probe_key
        payload["recommended_delta"] = float(chosen["delta"])
        payload["recommended_action"] = (
            f"relax {chosen['probe']} by {float(chosen['delta']):.6f}"
        )
        out.append(payload)

    out.sort(
        key=lambda row: (
            float(row["missed_ev_pct"]),
            float(row["win_rate_ci_low"]),
            int(row["n"]),
        ),
        reverse=True,
    )
    return out


def build_report(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    lane_rows = aggregate_lanes(rows)
    gate_rows = aggregate_gates(rows)
    regime_rows = aggregate_regimes(rows)
    regime_gate_rows = aggregate_regime_gates(rows)
    btc_regime_rows = aggregate_btc_regimes(rows)
    convergence_rows = aggregate_convergence(rows)
    probe_rows = aggregate_probes(rows)
    probe_relax_rows = build_probe_relax_recommendations(probe_rows)
    actionable_overtight = [
        row
        for row in gate_rows
        if int(row["n"]) >= 100
        and float(row["win_rate_ci_low"]) > 0.5
        and float(row["net_gate_value_pct"]) < 0.0
    ]
    actionable_overtight.sort(
        key=lambda row: (
            float(row["missed_ev_pct"]),
            float(row["win_rate_ci_low"]),
            int(row["n"]),
        ),
        reverse=True,
    )
    return {
        "rows": len(rows),
        "overall": _econ_metrics(rows),
        "regimes": regime_rows,
        "regime_gates": regime_gate_rows,
        "btc_regimes": btc_regime_rows,
        "convergence": convergence_rows,
        "deadzone_gates": [
            row
            for row in regime_gate_rows
            if str(row.get("combined_regime") or "").startswith("deadzone")
        ][:20],
        "lanes": lane_rows,
        "gates": gate_rows,
        "top_missed_ev": sorted(gate_rows, key=lambda r: (float(r["missed_ev_pct"]), int(r["n"])), reverse=True)[:20],
        "top_protected_loss": sorted(gate_rows, key=lambda r: (float(r["protected_loss_pct"]), int(r["n"])), reverse=True)[:20],
        "actionable_overtight_gates": actionable_overtight[:20],
        "probe_variants": probe_rows,
        "actionable_probe_relaxations": probe_relax_rows[:20],
    }


def enrich_rows_from_regime_log(
    rows: List[Dict[str, Any]],
    *,
    regime_path: Path = DEFAULT_REGIME_LOG,
    max_age_sec: float = REGIME_MATCH_MAX_AGE_SEC,
) -> List[Dict[str, Any]]:
    if not regime_path.exists() or not rows:
        return rows
    snapshots = load_regime_snapshots(regime_path)
    if not snapshots:
        return rows
    return [
        enrich_with_regime(row, snapshots, max_age_sec=max_age_sec)
        for row in rows
    ]


def _fmt_simple_table(rows: List[Dict[str, Any]], kind: str, limit: int) -> str:
    if not rows:
        return "(no rows)"
    items = rows[:limit]
    if kind == "lanes":
        header = (
            "lane_id".ljust(50)
            + "  n   WR    CI_low  CI_hi   avg%    total%   missedEV  protLoss  netGate"
        )
        lines = [header, "-" * len(header)]
        for row in items:
            lines.append(
                f"{str(row['lane_id'])[:50].ljust(50)}  "
                f"{int(row['n']):>3d}  "
                f"{float(row['win_rate'])*100:>5.1f}%  "
                f"{float(row['win_rate_ci_low'])*100:>6.1f}%  "
                f"{float(row['win_rate_ci_high'])*100:>6.1f}%  "
                f"{float(row['avg_realized_pct']):>+7.3f}  "
                f"{float(row['total_realized_pct']):>+8.3f}  "
                f"{float(row['missed_ev_pct']):>8.3f}  "
                f"{float(row['protected_loss_pct']):>8.3f}  "
                f"{float(row['net_gate_value_pct']):>+7.3f}"
            )
        return "\n".join(lines)
    if kind == "probes":
        header = (
            "gate/probe".ljust(76)
            + "  n   WR    CI_low  CI_hi   delta    missedEV  protLoss  netGate"
        )
        lines = [header, "-" * len(header)]
        for row in items:
            gate_probe = "|".join(
                [
                    str(row.get("strategy") or ""),
                    str(row.get("window") or ""),
                    str(row.get("action") or ""),
                    str(row.get("reason") or ""),
                    str(row.get("probe") or ""),
                ]
            )
            lines.append(
                f"{gate_probe[:76].ljust(76)}  "
                f"{int(row['n']):>3d}  "
                f"{float(row['win_rate'])*100:>5.1f}%  "
                f"{float(row['win_rate_ci_low'])*100:>6.1f}%  "
                f"{float(row['win_rate_ci_high'])*100:>6.1f}%  "
                f"{float(row['delta']):>+7.3f}  "
                f"{float(row['missed_ev_pct']):>8.3f}  "
                f"{float(row['protected_loss_pct']):>8.3f}  "
                f"{float(row['net_gate_value_pct']):>+7.3f}"
            )
        return "\n".join(lines)
    if kind == "regimes":
        header = (
            "regime".ljust(46)
            + "  n   WR    CI_low  CI_hi   avg%    missedEV  protLoss  netGate"
        )
        lines = [header, "-" * len(header)]
        for row in items:
            lines.append(
                f"{str(row['regime_key'])[:46].ljust(46)}  "
                f"{int(row['n']):>3d}  "
                f"{float(row['win_rate'])*100:>5.1f}%  "
                f"{float(row['win_rate_ci_low'])*100:>6.1f}%  "
                f"{float(row['win_rate_ci_high'])*100:>6.1f}%  "
                f"{float(row['avg_realized_pct']):>+7.3f}  "
                f"{float(row['missed_ev_pct']):>8.3f}  "
                f"{float(row['protected_loss_pct']):>8.3f}  "
                f"{float(row['net_gate_value_pct']):>+7.3f}"
            )
        return "\n".join(lines)
    if kind == "regime_gates":
        header = (
            "regime_gate_key".ljust(76)
            + "  n   WR    CI_low  CI_hi   avg%    missedEV  protLoss  netGate"
        )
        lines = [header, "-" * len(header)]
        for row in items:
            lines.append(
                f"{str(row['regime_gate_key'])[:76].ljust(76)}  "
                f"{int(row['n']):>3d}  "
                f"{float(row['win_rate'])*100:>5.1f}%  "
                f"{float(row['win_rate_ci_low'])*100:>6.1f}%  "
                f"{float(row['win_rate_ci_high'])*100:>6.1f}%  "
                f"{float(row['avg_realized_pct']):>+7.3f}  "
                f"{float(row['missed_ev_pct']):>8.3f}  "
                f"{float(row['protected_loss_pct']):>8.3f}  "
                f"{float(row['net_gate_value_pct']):>+7.3f}"
            )
        return "\n".join(lines)
    if kind == "btc_regimes":
        header = (
            "btc_1h_regime".ljust(18)
            + "  n   WR    CI_low  CI_hi   avg%    missedEV  protLoss  netGate"
        )
        lines = [header, "-" * len(header)]
        for row in items:
            lines.append(
                f"{str(row['btc_1h_regime'])[:18].ljust(18)}  "
                f"{int(row['n']):>3d}  "
                f"{float(row['win_rate'])*100:>5.1f}%  "
                f"{float(row['win_rate_ci_low'])*100:>6.1f}%  "
                f"{float(row['win_rate_ci_high'])*100:>6.1f}%  "
                f"{float(row['avg_realized_pct']):>+7.3f}  "
                f"{float(row['missed_ev_pct']):>8.3f}  "
                f"{float(row['protected_loss_pct']):>8.3f}  "
                f"{float(row['net_gate_value_pct']):>+7.3f}"
            )
        return "\n".join(lines)
    if kind == "convergence":
        header = (
            "convergence".ljust(14)
            + "  n   WR    CI_low  CI_hi   avgConv  avg%    missedEV  protLoss  netGate"
        )
        lines = [header, "-" * len(header)]
        for row in items:
            avg_conv = row.get("avg_convergence_score")
            avg_conv_txt = "  n/a " if avg_conv is None else f"{float(avg_conv):>7.3f}"
            lines.append(
                f"{str(row['convergence_bucket'])[:14].ljust(14)}  "
                f"{int(row['n']):>3d}  "
                f"{float(row['win_rate'])*100:>5.1f}%  "
                f"{float(row['win_rate_ci_low'])*100:>6.1f}%  "
                f"{float(row['win_rate_ci_high'])*100:>6.1f}%  "
                f"{avg_conv_txt}  "
                f"{float(row['avg_realized_pct']):>+7.3f}  "
                f"{float(row['missed_ev_pct']):>8.3f}  "
                f"{float(row['protected_loss_pct']):>8.3f}  "
                f"{float(row['net_gate_value_pct']):>+7.3f}"
            )
        return "\n".join(lines)
    header = (
        "gate_key".ljust(58)
        + "  n   WR    CI_low  CI_hi   avg%    missedEV  protLoss  netGate"
    )
    lines = [header, "-" * len(header)]
    for row in items:
        lines.append(
            f"{str(row['gate_key'])[:58].ljust(58)}  "
            f"{int(row['n']):>3d}  "
            f"{float(row['win_rate'])*100:>5.1f}%  "
            f"{float(row['win_rate_ci_low'])*100:>6.1f}%  "
            f"{float(row['win_rate_ci_high'])*100:>6.1f}%  "
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
    parser.add_argument("--price-regime")
    parser.add_argument("--polymarket-regime")
    parser.add_argument("--combined-regime")
    parser.add_argument("--btc-1h-regime")
    parser.add_argument("--regime-log", type=Path, default=DEFAULT_REGIME_LOG)
    parser.add_argument("--regime-max-age-sec", type=float, default=REGIME_MATCH_MAX_AGE_SEC)
    parser.add_argument(
        "--no-regime-enrich",
        action="store_true",
        help="Use only regime labels already present in the settled log.",
    )
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.path.exists():
        sys.stderr.write(f"(no settled ghost log at {args.path})\n")
        return 0

    raw_rows = list(_iter_jsonl(args.path))
    if not args.no_regime_enrich:
        raw_rows = enrich_rows_from_regime_log(
            raw_rows,
            regime_path=args.regime_log,
            max_age_sec=args.regime_max_age_sec,
        )
    rows = [row for row in raw_rows if _passes_filters(row, args)]
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
    print("\n## regimes")
    print(_fmt_simple_table(report["regimes"], "regimes", args.limit))
    print("\n## top regime gates")
    print(_fmt_simple_table(report["regime_gates"], "regime_gates", args.limit))
    print("\n## btc 1h regimes")
    print(_fmt_simple_table(report["btc_regimes"], "btc_regimes", args.limit))
    print("\n## convergence buckets")
    print(_fmt_simple_table(report["convergence"], "convergence", args.limit))
    print("\n## deadzone gates")
    print(_fmt_simple_table(report["deadzone_gates"], "regime_gates", args.limit))
    print("\n## top missed ev gates")
    print(_fmt_simple_table(report["top_missed_ev"], "gates", args.limit))
    print("\n## actionable overtight gates (n>=100, CI_low>50%, netGate<0)")
    print(_fmt_simple_table(report["actionable_overtight_gates"], "gates", args.limit))
    print("\n## actionable probe relaxations (relax rows only; n>=100, CI_low>50%, netGate<0)")
    print(_fmt_simple_table(report["actionable_probe_relaxations"], "probes", args.limit))
    print("\n## top protected loss gates")
    print(_fmt_simple_table(report["top_protected_loss"], "gates", args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
