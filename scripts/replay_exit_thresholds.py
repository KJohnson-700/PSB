#!/usr/bin/env python3
"""Replay journal mark-to-market rows under alternate TP/SL thresholds."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ReplayResult:
    trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    take_profit: int = 0
    stop_loss: int = 0
    final_exit: int = 0

    def add(self, pnl: float, reason: str) -> None:
        self.trades += 1
        self.pnl += pnl
        if pnl > 0:
            self.wins += 1
        if reason == "take_profit":
            self.take_profit += 1
        elif reason == "updown_stop_loss":
            self.stop_loss += 1
        else:
            self.final_exit += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "win_rate": round(self.wins / self.trades, 4) if self.trades else 0.0,
            "pnl": round(self.pnl, 4),
            "take_profit": self.take_profit,
            "updown_stop_loss": self.stop_loss,
            "final_exit": self.final_exit,
        }


def _load_trades(path: Path) -> dict[str, list[dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") not in {"ENTRY", "PRICE_UPDATE", "EXIT"}:
                continue
            tid = str(row.get("trade_id") or "")
            if tid:
                events[tid].append(row)
    return events


def _is_crypto_updown(entry: dict[str, Any]) -> bool:
    return (
        entry.get("strategy")
        in {"bitcoin", "sol_macro", "eth_macro", "hype_macro", "xrp_macro"}
        and "up or down" in str(entry.get("market_question") or "").lower()
    )


def _mark_pnl(entry: dict[str, Any], mark_price: float) -> float:
    return (float(mark_price) - float(entry.get("entry_price") or 0.0)) * float(
        entry.get("size") or 0.0
    )


def _replay_one(
    rows: list[dict[str, Any]],
    *,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> tuple[float, str] | None:
    rows = sorted(rows, key=lambda r: r.get("timestamp") or "")
    entry = next((r for r in rows if r.get("event") == "ENTRY"), None)
    exit_row = next((r for r in rows if r.get("event") == "EXIT"), None)
    if not entry or not exit_row or not _is_crypto_updown(entry):
        return None

    entry_price = float(entry.get("entry_price") or 0.0)
    size = float(entry.get("size") or 0.0)
    cost_basis = entry_price * size
    if entry_price <= 0 or size <= 0 or cost_basis <= 0:
        return None

    for row in rows:
        if row.get("event") not in {"PRICE_UPDATE", "EXIT"}:
            continue
        mark = float(row.get("current_price") or 0.0)
        pnl = _mark_pnl(entry, mark)
        pnl_pct = pnl / cost_basis
        if pnl_pct >= take_profit_pct:
            return pnl, "take_profit"
        if stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct:
            return pnl, "updown_stop_loss"

    return float(exit_row.get("pnl") or 0.0), str(exit_row.get("reason") or "final_exit")


def build_report(
    paths: list[Path],
    *,
    take_profits: list[float],
    stop_losses: list[float],
) -> dict[str, Any]:
    grid: dict[str, ReplayResult] = {}
    by_strategy: dict[str, dict[str, ReplayResult]] = defaultdict(dict)
    for tp in take_profits:
        for sl in stop_losses:
            key = f"tp={tp:.2f}|sl={sl:.2f}"
            grid[key] = ReplayResult()
            for path in paths:
                for rows in _load_trades(path).values():
                    entry = next((r for r in rows if r.get("event") == "ENTRY"), None)
                    out = _replay_one(rows, take_profit_pct=tp, stop_loss_pct=sl)
                    if out is None or not entry:
                        continue
                    pnl, reason = out
                    grid[key].add(pnl, reason)
                    strat = str(entry.get("strategy") or "?")
                    by_strategy[strat].setdefault(key, ReplayResult()).add(pnl, reason)
    return {
        "entries_files": [str(p.resolve()) for p in paths],
        "grid": {k: v.as_dict() for k, v in grid.items()},
        "by_strategy": {
            strategy: {k: v.as_dict() for k, v in values.items()}
            for strategy, values in sorted(by_strategy.items())
        },
    }


def _csv_floats(raw: str) -> list[float]:
    return [float(part) for part in raw.split(",") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=Path, action="append", required=True)
    parser.add_argument("--take-profits", default="0.15,0.20,0.25")
    parser.add_argument("--stop-losses", default="0.15,0.20,0.25,0.30,0")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    paths = [p for p in args.entries if p.is_file()]
    report = build_report(
        paths,
        take_profits=_csv_floats(args.take_profits),
        stop_losses=_csv_floats(args.stop_losses),
    )
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"Files: {len(paths)}")
    print("| config | trades | WR | pnl | TP | SL | final |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for key, stats in sorted(report["grid"].items()):
        print(
            f"| {key} | {stats['trades']} | {stats['win_rate']:.1%} | "
            f"{stats['pnl']:+.2f} | {stats['take_profit']} | "
            f"{stats['updown_stop_loss']} | {stats['final_exit']} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
