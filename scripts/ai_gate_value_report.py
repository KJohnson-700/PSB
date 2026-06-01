"""Exact AI gate value report from enriched decision_layer.jsonl rows.

This intentionally refuses to estimate PnL for old rows that do not include
entry economics. It scores only rows with an `entry_price` logged at decision
time, then settles the quant side against the final Polymarket outcome.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analysis.ai_decision_settler import _resolve_outcomes

DECISION_LOG = REPO_ROOT / "data" / "logs" / "ai_pipeline" / "decision_layer.jsonl"


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_since(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


def _iter_rows(path: Path, since: datetime | None) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(row.get("ts_utc"))
            if since is not None and ts is not None and ts < since:
                continue
            yield row


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _side_won(action: str, outcome: str) -> bool:
    return (action == "BUY_YES" and outcome == "YES") or (
        action == "BUY_NO" and outcome == "NO"
    )


def _pnl_for_stake(entry_price: float, side_won: bool, stake: float) -> float:
    if side_won:
        return stake * ((1.0 - entry_price) / entry_price)
    return -stake


def _bucket(row: dict[str, Any]) -> str:
    if row.get("approved") is True and not row.get("fail_open"):
        return "approved"
    if row.get("approved") is False and not row.get("fail_open"):
        return "rejected_if_taken"
    if row.get("fail_open"):
        return "fail_open"
    return "other"


def _summarize(rows: list[dict[str, Any]], stake: float) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for row in rows:
        b = _bucket(row)
        out[b]["n"] += 1
        if row["quant_would_win"]:
            out[b]["wins"] += 1
        out[b]["pnl"] += _pnl_for_stake(
            float(row["entry_price"]), bool(row["quant_would_win"]), stake
        )
    return dict(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="only rows on/after UTC date YYYY-MM-DD")
    parser.add_argument("--stake", type=float, default=10.0, help="normalized stake per trade")
    args = parser.parse_args()

    since = _parse_since(args.since)
    all_rows = list(_iter_rows(DECISION_LOG, since))
    enriched = []
    missing_entry = 0
    for row in all_rows:
        action = str(row.get("quant_action") or "").upper()
        entry_price = _as_float(row.get("entry_price"))
        if action not in {"BUY_YES", "BUY_NO"} or entry_price is None:
            missing_entry += 1
            continue
        if not 0.0 < entry_price < 1.0:
            missing_entry += 1
            continue
        enriched.append({**row, "entry_price": entry_price, "quant_action": action})

    outcomes = _resolve_outcomes(str(row.get("market_id")) for row in enriched)
    settled = []
    unresolved = 0
    for row in enriched:
        outcome = outcomes.get(str(row.get("market_id")))
        if outcome not in {"YES", "NO"}:
            unresolved += 1
            continue
        settled.append(
            {
                **row,
                "outcome_won": outcome,
                "quant_would_win": _side_won(row["quant_action"], outcome),
            }
        )

    summary = _summarize(settled, args.stake)
    print("=== Exact AI gate value ===")
    print(f"source: {DECISION_LOG}")
    print(f"rows read: {len(all_rows)}")
    print(f"skipped missing entry economics: {missing_entry}")
    print(f"settled enriched rows: {len(settled)}")
    print(f"unresolved enriched rows: {unresolved}")
    print(f"normalized stake: ${args.stake:.2f}\n")
    print(f"{'bucket':18s} {'n':>6s} {'wr':>8s} {'pnl':>12s} {'avg':>10s}")
    for bucket in ("approved", "rejected_if_taken", "fail_open", "other"):
        row = summary.get(bucket, {"n": 0, "wins": 0, "pnl": 0.0})
        n = int(row["n"])
        wr = (float(row["wins"]) / n * 100.0) if n else 0.0
        pnl = float(row["pnl"])
        avg = pnl / n if n else 0.0
        print(f"{bucket:18s} {n:6d} {wr:7.2f}% {pnl:+12.2f} {avg:+10.3f}")
    rejected_pnl = float(summary.get("rejected_if_taken", {}).get("pnl", 0.0))
    print(f"\nAI gate delta vs taking rejected quant candidates: {-rejected_pnl:+.2f}")


if __name__ == "__main__":
    main()
