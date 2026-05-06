#!/usr/bin/env python3
"""
Parse a single paper session entries.jsonl: join ENTRY + EXIT, emit JSON summary.
Read-only; used for session reviews and vault updates.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("entries_jsonl", type=Path)
    ap.add_argument("-o", "--out-json", type=Path, default=None)
    args = ap.parse_args()

    open_entries: dict[str, dict] = {}
    exits: list[dict] = []

    with open(args.entries_jsonl, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = e.get("event", "")
            tid = e.get("trade_id") or ""
            if et == "ENTRY" and tid:
                open_entries[tid] = e
            elif et == "EXIT" and tid:
                exits.append(e)

    closed: list[dict[str, Any]] = []
    for ex in exits:
        tid = ex.get("trade_id", "")
        ent = open_entries.get(tid, {})
        ee = ent.get("extra") or {}
        xo = ex.get("extra") or {}
        merged = {**ee, **xo}

        pnl = float(ex.get("pnl") or 0)
        entry_price = float(ex.get("entry_price") or 0)
        cur = float(ex.get("current_price") or 0)
        # skip phantom binary resolution lines (heatmap script parity)
        if entry_price > 0 and abs(entry_price + cur - 1.0) < 0.02:
            continue

        sig = str(merged.get("signal_reason") or ee.get("signal_reason") or "")
        reason_entry = str(ent.get("reason", ""))

        lag_mag = merged.get("lag_magnitude")
        if lag_mag is None and "macro_leg=" in reason_entry:
            # parse macro_leg=0.0431 from reason
            try:
                part = reason_entry.split("macro_leg=")[1].split()[0]
                lag_mag = float(part)
            except (IndexError, ValueError):
                lag_mag = None

        closed.append(
            {
                "trade_id": tid,
                "strategy": ex.get("strategy", ""),
                "action": ex.get("action", ""),
                "pnl": pnl,
                "exit_reason": str(ex.get("reason", "")),
                "entry_edge": merged.get("entry_edge", merged.get("edge")),
                "hour_pt_entry": merged.get("hour_pt"),
                "hour_utc_entry": merged.get("hour_utc_entry", merged.get("hour_utc")),
                "window_size": merged.get("window_size"),
                "htf_bias": merged.get("htf_bias"),
                "lag_magnitude": lag_mag,
                "counter_trend_btc": "counter_trend=btc_4h_hist_declining" in sig
                or "counter_trend=btc_4h_hist_declining" in reason_entry,
            }
        )

    def agg(rows: list[dict], key_fn) -> dict:
        buckets: dict[Any, dict] = defaultdict(
            lambda: {
                "trades": 0,
                "wins": 0,
                "pnl": 0.0,
                "pnls_pos": [],
                "pnls_neg": [],
            }
        )
        for r in rows:
            k = key_fn(r)
            b = buckets[k]
            b["trades"] += 1
            b["pnl"] += r["pnl"]
            if r["pnl"] > 0:
                b["wins"] += 1
                b["pnls_pos"].append(r["pnl"])
            elif r["pnl"] < 0:
                b["pnls_neg"].append(r["pnl"])
        out = {}
        for k, b in sorted(buckets.items(), key=lambda x: (str(type(x[0])), x[0])):
            n = b["trades"]
            pos, neg = b["pnls_pos"], b["pnls_neg"]
            out[str(k)] = {
                "trades": n,
                "wins": b["wins"],
                "win_rate": round(b["wins"] / n, 4) if n else 0,
                "pnl": round(b["pnl"], 4),
                "avg_win": round(sum(pos) / len(pos), 4) if pos else None,
                "avg_loss": round(sum(neg) / len(neg), 4) if neg else None,
            }
        return out

    by_strat = agg(closed, lambda r: r["strategy"])
    by_strat_action = agg(
        closed, lambda r: f"{r['strategy']}::{r['action']}"
    )
    by_exit = agg(closed, lambda r: r["exit_reason"])

    # BTC counter-trend subset
    btc_ct = [r for r in closed if r["strategy"] == "bitcoin" and r["counter_trend_btc"]]
    btc_buy_no = [r for r in closed if r["strategy"] == "bitcoin" and r["action"] == "BUY_NO"]

    # HYPE lag null (no magnitude)
    hype = [r for r in closed if r["strategy"] == "hype_macro"]
    hype_no_lag = [r for r in hype if r["lag_magnitude"] is None]

    # Hour PT worst (session)
    by_h_pt: dict[int, list] = defaultdict(list)
    for r in closed:
        h = r.get("hour_pt_entry")
        if h is not None:
            by_h_pt[int(h)].append(r)

    hour_stats = {}
    for h, lst in sorted(by_h_pt.items()):
        pnl = sum(x["pnl"] for x in lst)
        wins = sum(1 for x in lst if x["pnl"] > 0)
        hour_stats[h] = {
            "trades": len(lst),
            "win_rate": round(wins / len(lst), 4) if lst else 0,
            "pnl": round(pnl, 4),
        }

    summary = {
        "session_file": str(args.entries_jsonl),
        "closed_trades": len(closed),
        "by_strategy": by_strat,
        "by_strategy_action": by_strat_action,
        "by_exit_reason": by_exit,
        "bitcoin": {
            "buy_no_trades": len(btc_buy_no),
            "buy_no_pnl": round(sum(x["pnl"] for x in btc_buy_no), 4),
            "buy_no_wins": sum(1 for x in btc_buy_no if x["pnl"] > 0),
            "counter_trend_trades": len(btc_ct),
            "counter_trend_pnl": round(sum(x["pnl"] for x in btc_ct), 4),
            "counter_trend_wins": sum(1 for x in btc_ct if x["pnl"] > 0),
        },
        "hype_macro": {
            "trades": len(hype),
            "lag_magnitude_null_trades": len(hype_no_lag),
            "lag_magnitude_null_share": round(
                len(hype_no_lag) / len(hype), 4
            )
            if hype
            else 0,
        },
        "hour_pt_entry": hour_stats,
    }

    text = json.dumps(summary, indent=2)
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.out_json}")
    else:
        print(text)


if __name__ == "__main__":
    main()
