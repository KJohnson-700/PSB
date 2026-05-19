"""Post-merge BUY_YES / BUY_NO audit report."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRADES = REPO_ROOT / "data" / "calibration" / "trades.jsonl"
DEFAULT_REJECTED = REPO_ROOT / "data" / "calibration" / "rejected_candidates_settled.jsonl"
DEFAULT_CUTOVER = "2026-05-17T11:11:00Z"
BUY_YES_STRATEGIES = ("sol_macro", "eth_macro", "xrp_macro", "hype_macro")


def _parse_ts(raw: str) -> datetime:
    text = str(raw or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _in_window(row: Dict[str, Any], since: datetime) -> bool:
    for key in ("closed_at", "ts"):
        raw = row.get(key)
        if not raw:
            continue
        try:
            return _parse_ts(str(raw)) >= since
        except Exception:
            continue
    return False


def _buy_yes_lane_state(trade_n: int, trade_wr: float, ghost_n: int, ghost_wr: float) -> str:
    if trade_n < 5 and ghost_n < 5:
        return "under-sampled"
    if trade_n >= 5 and trade_wr >= 0.55:
        return "healthy"
    if ghost_n >= 5 and ghost_wr >= 0.58:
        return "over-blocked"
    return "over-admitted"


def build_report(
    *,
    trades_path: Path = DEFAULT_TRADES,
    rejected_path: Path = DEFAULT_REJECTED,
    since: str = DEFAULT_CUTOVER,
) -> Dict[str, Any]:
    cutover = _parse_ts(since)
    taken_counts: Dict[Tuple[str, str, str, str], Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0}
    )
    side_selection = Counter()
    buy_yes_lanes: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = defaultdict(
        lambda: {"trade_n": 0, "trade_wins": 0, "ghost_n": 0, "ghost_wins": 0}
    )

    for row in _iter_jsonl(trades_path) or []:
        if not _in_window(row, cutover):
            continue
        strategy = str(row.get("strategy") or "")
        window = str(row.get("window") or "")
        side = str(row.get("side") or row.get("action") or "")
        lane_family = str(row.get("lane_family") or "")
        key = (strategy, window, side, lane_family)
        taken_counts[key]["n"] += 1
        taken_counts[key]["wins"] += 1 if row.get("win") is True else 0
        taken_counts[key]["pnl"] += _coerce_float(row.get("pnl")) or 0.0
        side_selection[(strategy, str(row.get("side_source") or ""), side)] += 1
        if side == "BUY_YES" and strategy in BUY_YES_STRATEGIES:
            lane_key = (
                strategy,
                window,
                lane_family,
                str(row.get("entry_price_bucket") or ""),
                str(row.get("regime_tag_bucket") or ""),
            )
            buy_yes_lanes[lane_key]["trade_n"] += 1
            buy_yes_lanes[lane_key]["trade_wins"] += 1 if row.get("win") is True else 0

    rejected_counts: Dict[Tuple[str, str, str, str], Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "net_gate_value_pct": 0.0}
    )
    for row in _iter_jsonl(rejected_path) or []:
        if not _in_window(row, cutover):
            continue
        strategy = str(row.get("strategy") or "")
        window = str(row.get("window") or "")
        side = str(row.get("action") or "")
        reason = str(row.get("reason") or "")
        key = (strategy, window, side, reason)
        rejected_counts[key]["n"] += 1
        rejected_counts[key]["wins"] += 1 if row.get("win") is True else 0
        realized_pct = _coerce_float(row.get("realized_pct")) or 0.0
        rejected_counts[key]["net_gate_value_pct"] += max(-realized_pct, 0.0) - max(realized_pct, 0.0)
        side_selection[(strategy, str(row.get("side_source") or ""), side)] += 1
        if side == "BUY_YES" and strategy in BUY_YES_STRATEGIES:
            lane_key = (
                strategy,
                window,
                str(row.get("lane_family") or ""),
                str(row.get("entry_price_bucket") or ""),
                str(row.get("regime_tag_bucket") or ""),
            )
            buy_yes_lanes[lane_key]["ghost_n"] += 1
            buy_yes_lanes[lane_key]["ghost_wins"] += 1 if row.get("win") is True else 0

    return {
        "since": cutover.isoformat(),
        "taken_trades": [
            {
                "strategy": k[0],
                "window": k[1],
                "side": k[2],
                "lane_family": k[3],
                "n": v["n"],
                "win_rate": round(v["wins"] / v["n"], 4) if v["n"] else 0.0,
                "pnl": round(v["pnl"], 6),
            }
            for k, v in sorted(taken_counts.items())
        ],
        "rejected_ghosts": [
            {
                "strategy": k[0],
                "window": k[1],
                "side": k[2],
                "reason": k[3],
                "n": v["n"],
                "win_rate": round(v["wins"] / v["n"], 4) if v["n"] else 0.0,
            }
            for k, v in sorted(rejected_counts.items())
        ],
        "side_selection_counts": [
            {
                "strategy": k[0],
                "side_source": k[1],
                "chosen_side": k[2],
                "n": v,
            }
            for k, v in sorted(side_selection.items())
        ],
        "net_gate_value": [
            {
                "strategy": k[0],
                "window": k[1],
                "side": k[2],
                "reason": k[3],
                "n": v["n"],
                "net_gate_value_pct": round(v["net_gate_value_pct"], 6),
            }
            for k, v in sorted(rejected_counts.items())
        ],
        "buy_yes_lane_checks": [
            {
                "strategy": k[0],
                "window": k[1],
                "lane_family": k[2],
                "entry_price_bucket": k[3],
                "regime_tag_bucket": k[4],
                "trade_n": v["trade_n"],
                "trade_win_rate": round(v["trade_wins"] / v["trade_n"], 4) if v["trade_n"] else 0.0,
                "ghost_n": v["ghost_n"],
                "ghost_win_rate": round(v["ghost_wins"] / v["ghost_n"], 4) if v["ghost_n"] else 0.0,
                "state": _buy_yes_lane_state(
                    v["trade_n"],
                    (v["trade_wins"] / v["trade_n"]) if v["trade_n"] else 0.0,
                    v["ghost_n"],
                    (v["ghost_wins"] / v["ghost_n"]) if v["ghost_n"] else 0.0,
                ),
            }
            for k, v in sorted(buy_yes_lanes.items())
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Post-merge BUY_YES / BUY_NO side report")
    parser.add_argument("--since", default=DEFAULT_CUTOVER)
    parser.add_argument("--trades-path", default=str(DEFAULT_TRADES))
    parser.add_argument("--rejected-path", default=str(DEFAULT_REJECTED))
    args = parser.parse_args()
    report = build_report(
        trades_path=Path(args.trades_path),
        rejected_path=Path(args.rejected_path),
        since=args.since,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
