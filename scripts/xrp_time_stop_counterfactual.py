#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"


@dataclass
class TradePair:
    entry: dict[str, Any]
    exit: dict[str, Any]


def _load_pairs(entries_path: Path, strategy: str = "xrp_macro") -> list[TradePair]:
    entries: dict[str, dict[str, Any]] = {}
    out: list[TradePair] = []
    for line in entries_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        tid = str(row.get("trade_id") or "")
        if not tid:
            continue
        if row.get("event") == "ENTRY":
            entries[tid] = row
        elif row.get("event") == "EXIT" and row.get("strategy") == strategy:
            ent = entries.get(tid)
            if ent:
                out.append(TradePair(entry=ent, exit=row))
    return out


def _extract_resolution(market_payload: Any) -> Optional[str]:
    def _norm(v: Any) -> Optional[str]:
        if isinstance(v, str):
            vv = v.strip().upper()
            if vv in {"YES", "NO"}:
                return vv
        return None

    if isinstance(market_payload, dict):
        for k in (
            "resolvedOutcome",
            "resolved_outcome",
            "outcome",
            "winner",
            "winningOutcome",
            "winning_outcome",
            "result",
        ):
            v = _norm(market_payload.get(k))
            if v:
                return v

        tokens = market_payload.get("tokens")
        if isinstance(tokens, list):
            for t in tokens:
                if not isinstance(t, dict):
                    continue
                outcome = _norm(t.get("outcome"))
                winner = bool(t.get("winner")) or bool(t.get("winning"))
                if winner and outcome in {"YES", "NO"}:
                    return outcome

    if isinstance(market_payload, list):
        for item in market_payload:
            v = _extract_resolution(item)
            if v:
                return v
    return None


def _fetch_market(market_id: str, timeout: int = 8) -> Any:
    urls = [
        f"{GAMMA_BASE}/markets/{market_id}",
        f"{GAMMA_BASE}/markets",
    ]

    # First try direct resource path.
    try:
        r = requests.get(urls[0], timeout=timeout)
        if r.ok:
            return r.json()
    except Exception:
        pass

    # Then try query-form lookup.
    for params in ({"id": market_id}, {"market_id": market_id}, {"limit": 1, "offset": 0, "id": market_id}):
        try:
            r = requests.get(urls[1], params=params, timeout=timeout)
            if r.ok:
                payload = r.json()
                if isinstance(payload, list) and payload:
                    return payload[0]
                if isinstance(payload, dict):
                    return payload
        except Exception:
            continue
    return None


def _cf_pnl(action: str, entry_price: float, size: float, resolved: str) -> float:
    # Binary payoff assuming YES/NO settles to 1/0.
    if action == "BUY_YES":
        return size * (1.0 - entry_price) if resolved == "YES" else -size * entry_price
    if action == "BUY_NO":
        return -size * (1.0 - entry_price) if resolved == "YES" else size * entry_price
    return 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description="XRP time-stop counterfactual to resolution using Gamma outcomes.")
    ap.add_argument("entries_jsonl", type=Path)
    ap.add_argument("--strategy", default="xrp_macro")
    ap.add_argument("--out-json", type=Path, default=None)
    args = ap.parse_args()

    pairs = _load_pairs(args.entries_jsonl, strategy=args.strategy)
    ts_pairs = [p for p in pairs if p.exit.get("reason") == "updown_time_stop"]

    rows: list[dict[str, Any]] = []
    for p in ts_pairs:
        market_id = str(p.exit.get("market_id") or p.entry.get("market_id") or "")
        market_payload = _fetch_market(market_id) if market_id else None
        resolved = _extract_resolution(market_payload)
        entry_price = float(p.entry.get("entry_price") or 0.0)
        size = float(p.entry.get("size") or p.exit.get("size") or 0.0)
        actual = float(p.exit.get("pnl") or 0.0)
        row = {
            "timestamp": p.exit.get("timestamp"),
            "market_id": market_id,
            "market_question": p.exit.get("market_question") or p.entry.get("market_question"),
            "action": p.exit.get("action"),
            "entry_price": entry_price,
            "size": size,
            "actual_pnl": actual,
            "resolved_outcome": resolved,
            "cf_resolution_pnl": None,
            "delta": None,
        }
        if resolved in {"YES", "NO"}:
            cf = _cf_pnl(str(p.exit.get("action") or ""), entry_price, size, resolved)
            row["cf_resolution_pnl"] = round(cf, 6)
            row["delta"] = round(cf - actual, 6)
        rows.append(row)

    comparable = [r for r in rows if isinstance(r.get("delta"), (int, float))]
    report = {
        "session_file": str(args.entries_jsonl),
        "strategy": args.strategy,
        "time_stop_trades": len(ts_pairs),
        "resolved_comparable_trades": len(comparable),
        "actual_pnl_comparable": round(sum(float(r["actual_pnl"]) for r in comparable), 6),
        "cf_resolution_pnl_comparable": round(sum(float(r["cf_resolution_pnl"]) for r in comparable), 6),
        "net_uplift_comparable": round(sum(float(r["delta"]) for r in comparable), 6),
        "trades": rows,
    }

    txt = json.dumps(report, indent=2)
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(txt + "\n", encoding="utf-8")
        print(f"Wrote {args.out_json}")
    else:
        print(txt)


if __name__ == "__main__":
    main()

