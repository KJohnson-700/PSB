#!/usr/bin/env python3
"""Read-only per-lane calibration report.

Reads ``data/calibration/trades.jsonl`` (the Phase 0 append-only log) and
prints per-lane statistics: realized vs stated edge, win/loss/flat breakdown,
Beta posterior summary (computed on the fly with the same Beta(2,3) prior the
plan uses for Phase 6).

Examples:
    python tools/calibration_report.py
    python tools/calibration_report.py --lane "eth_macro|5m|down"
    python tools/calibration_report.py --session test_20260514_192738 --json

The report is sorted by ``|alpha_implied - 1|`` descending so the most
miscalibrated lanes surface first.
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
DEFAULT_LOG = REPO_ROOT / "data" / "calibration" / "trades.jsonl"

# Same prior as the Phase 6 plan — slightly pessimistic, reflects competitive markets.
PRIOR_A = 2.0
PRIOR_B = 3.0


def _iter_records(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _filter(records: Iterable[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    out = []
    for r in records:
        if args.session and r.get("session_id") != args.session:
            continue
        if args.lane:
            lane = str(r.get("lane_id") or "")
            if not (lane == args.lane or lane.startswith(args.lane)):
                continue
        if args.strategy and r.get("strategy") != args.strategy:
            continue
        if args.since and str(r.get("ts") or "") < args.since:
            continue
        out.append(r)
    return out


def _beta_quantile(a: float, b: float, q: float) -> float:
    """Approximate Beta(a,b) quantile via Wilson-Hilferty (good enough for display)."""
    # Use simple normal-approx on logit scale; fine for small-n display only.
    if a <= 0 or b <= 0:
        return float("nan")
    mean = a / (a + b)
    var = (a * b) / ((a + b) ** 2 * (a + b + 1))
    if var <= 0:
        return mean
    sd = math.sqrt(var)
    # Inverse normal CDF via Beasley-Springer-Moro approximation for q in (0,1)
    z = _norm_ppf(q)
    return max(0.0, min(1.0, mean + z * sd))


def _norm_ppf(q: float) -> float:
    if q <= 0.0:
        return -10.0
    if q >= 1.0:
        return 10.0
    # Acklam's approximation (sufficient for display)
    a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2,
         1.383577518672690e2, -3.066479806614716e1, 2.506628277459239e0]
    b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2,
         6.680131188771972e1, -1.328068155288572e1]
    c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838,
         -2.549732539343734, 4.374664141464968, 2.938163982698783]
    d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996,
         3.754408661907416]
    plow = 0.02425
    phigh = 1 - plow
    if q < plow:
        r = math.sqrt(-2.0 * math.log(q))
        return (((((c[0] * r + c[1]) * r + c[2]) * r + c[3]) * r + c[4]) * r + c[5]) / (
            (((d[0] * r + d[1]) * r + d[2]) * r + d[3]) * r + 1
        )
    if q <= phigh:
        r = q - 0.5
        s = r * r
        return (((((a[0] * s + a[1]) * s + a[2]) * s + a[3]) * s + a[4]) * s + a[5]) * r / (
            ((((b[0] * s + b[1]) * s + b[2]) * s + b[3]) * s + b[4]) * s + 1
        )
    r = math.sqrt(-2.0 * math.log(1 - q))
    return -(((((c[0] * r + c[1]) * r + c[2]) * r + c[3]) * r + c[4]) * r + c[5]) / (
        (((d[0] * r + d[1]) * r + d[2]) * r + d[3]) * r + 1
    )


def _aggregate(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    lanes: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0, "flat": 0,
        "total_pnl": 0.0,
        "sum_stated_edge": 0.0, "n_stated_edge": 0,
        "sum_stated_est_prob": 0.0, "n_stated_est_prob": 0,
        "sum_realized_pct": 0.0,
        "sum_a_obs": 0.0, "n_a_obs": 0,  # for alpha mean
    })
    for r in records:
        lane = str(r.get("lane_id") or "unknown")
        slot = lanes[lane]
        slot["n"] += 1
        pnl = float(r.get("pnl") or 0.0)
        slot["total_pnl"] += pnl
        if pnl > 0.01:
            slot["wins"] += 1
        elif pnl < -0.01:
            slot["losses"] += 1
        else:
            slot["flat"] += 1
        rp = float(r.get("realized_pct") or 0.0)
        slot["sum_realized_pct"] += rp
        se = r.get("stated_edge")
        if isinstance(se, (int, float)) and se is not None:
            slot["sum_stated_edge"] += float(se)
            slot["n_stated_edge"] += 1
        sp = r.get("stated_est_prob")
        if isinstance(sp, (int, float)) and sp is not None:
            slot["sum_stated_est_prob"] += float(sp)
            slot["n_stated_est_prob"] += 1
            dev = float(sp) - 0.5
            if abs(dev) >= 0.005:
                slot["sum_a_obs"] += rp / dev
                slot["n_a_obs"] += 1

    rows: List[Dict[str, Any]] = []
    for lane, s in lanes.items():
        n = s["n"]
        if n == 0:
            continue
        wins = s["wins"]
        losses = s["losses"]
        flat = s["flat"]
        denom = wins + losses + flat
        wr = wins / denom if denom else 0.0
        avg_realized = s["sum_realized_pct"] / n
        avg_stated_edge = s["sum_stated_edge"] / s["n_stated_edge"] if s["n_stated_edge"] else None
        avg_stated_prob = (
            s["sum_stated_est_prob"] / s["n_stated_est_prob"]
            if s["n_stated_est_prob"]
            else None
        )
        alpha_implied = s["sum_a_obs"] / s["n_a_obs"] if s["n_a_obs"] else None
        # Beta posterior with prior Beta(2, 3); only win/loss count (flats omitted per binary semantics elsewhere)
        beta_a = PRIOR_A + wins
        beta_b = PRIOR_B + losses
        rows.append({
            "lane_id": lane,
            "n": n,
            "wins": wins, "losses": losses, "flat": flat,
            "win_rate": round(wr, 4),
            "total_pnl": round(s["total_pnl"], 4),
            "avg_pnl": round(s["total_pnl"] / n, 4),
            "avg_stated_edge": round(avg_stated_edge, 4) if avg_stated_edge is not None else None,
            "avg_stated_prob": round(avg_stated_prob, 4) if avg_stated_prob is not None else None,
            "avg_realized_pct": round(avg_realized, 4),
            "alpha_implied": round(alpha_implied, 3) if alpha_implied is not None else None,
            "beta_a": round(beta_a, 3), "beta_b": round(beta_b, 3),
            "beta_p25": round(_beta_quantile(beta_a, beta_b, 0.25), 4),
            "beta_p50": round(_beta_quantile(beta_a, beta_b, 0.50), 4),
            "beta_p75": round(_beta_quantile(beta_a, beta_b, 0.75), 4),
        })

    rows.sort(
        key=lambda r: abs((r.get("alpha_implied") if r.get("alpha_implied") is not None else 1.0) - 1.0),
        reverse=True,
    )
    return rows


def _fmt_table(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(no calibration records match the filter)"
    cols = [
        ("lane_id", 60),
        ("n", 4),
        ("wr", 6),
        ("pnl", 9),
        ("avg", 7),
        ("edge", 6),
        ("p_up", 6),
        ("real%", 7),
        ("α", 7),
        ("β p25", 7),
        ("β p50", 7),
        ("β p75", 7),
    ]
    header = "  ".join(name.rjust(w) if i > 0 else name.ljust(w) for i, (name, w) in enumerate(cols))
    lines = [header, "-" * len(header)]
    for r in rows:
        cells = [
            str(r["lane_id"])[: cols[0][1]].ljust(cols[0][1]),
            f"{r['n']:>4d}",
            f"{r['win_rate'] * 100:>5.1f}%",
            f"{r['total_pnl']:>+8.2f}",
            f"{r['avg_pnl']:>+6.2f}",
            (f"{r['avg_stated_edge']:>6.3f}" if r['avg_stated_edge'] is not None else "    -"),
            (f"{r['avg_stated_prob']:>6.3f}" if r['avg_stated_prob'] is not None else "    -"),
            f"{r['avg_realized_pct']:>+6.3f}",
            (f"{r['alpha_implied']:>+6.2f}" if r['alpha_implied'] is not None else "     -"),
            f"{r['beta_p25']:>6.3f}",
            f"{r['beta_p50']:>6.3f}",
            f"{r['beta_p75']:>6.3f}",
        ]
        lines.append("  ".join(cells))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", type=Path, default=DEFAULT_LOG,
                    help="path to trades.jsonl (default: data/calibration/trades.jsonl)")
    ap.add_argument("--session", help="filter by session_id")
    ap.add_argument("--lane", help="filter by lane_id (exact or prefix match)")
    ap.add_argument("--strategy", help="filter by strategy")
    ap.add_argument("--since", help="filter by ts >= this iso8601 string")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)

    records = list(_filter(_iter_records(args.path), args))
    rows = _aggregate(records)

    if args.json:
        json.dump({"path": str(args.path), "rows": rows}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not args.path.exists():
        sys.stderr.write(f"(no log at {args.path}; nothing logged yet)\n")
        return 0

    print(f"# calibration report — {args.path}  (records: {len(records)})")
    print(_fmt_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
