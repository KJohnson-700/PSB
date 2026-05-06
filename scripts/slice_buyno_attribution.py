#!/usr/bin/env python3
"""Slice paper-trading outcomes by strategy × action × regime.

Focus: figure out whether BUY_NO is firing in conditions that match its
semantic intent (price won't be higher than start-of-window price), and
attribute the WR collapse between two sessions.

Usage:
    python scripts/slice_buyno_attribution.py <session_dir>
    python scripts/slice_buyno_attribution.py <new_session> <old_session>
    python scripts/slice_buyno_attribution.py        # auto: latest 2 sessions
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAPER_DIR = ROOT / "data" / "paper_trades"


def latest_sessions(n: int = 2) -> list[Path]:
    sessions = sorted(
        (p for p in PAPER_DIR.iterdir() if p.is_dir() and (p / "entries.jsonl").exists()),
        key=lambda p: p.name,
        reverse=True,
    )
    return sessions[:n]


def load_trades(session: Path) -> list[dict]:
    """Return one row per closed trade by joining ENTRY → EXIT on trade_id."""
    entries: dict[str, dict] = {}
    exits: dict[str, dict] = {}
    with (session / "entries.jsonl").open() as fh:
        for line in fh:
            row = json.loads(line)
            ev = row.get("event")
            tid = row.get("trade_id")
            if not tid:
                continue
            if ev == "ENTRY":
                entries[tid] = row
            elif ev == "EXIT":
                exits[tid] = row

    trades = []
    for tid, ex in exits.items():
        en = entries.get(tid, {})
        ex_extra = ex.get("extra", {})
        en_extra = en.get("extra", {})
        trades.append(
            {
                "trade_id": tid,
                "strategy": ex.get("strategy"),
                "action": ex.get("action"),
                "result": ex_extra.get("result"),
                "pnl": ex.get("pnl", 0.0),
                "exit_reason": ex_extra.get("exit_reason"),
                "hold_seconds": ex_extra.get("hold_seconds"),
                "edge": en_extra.get("edge"),
                "htf_bias": en_extra.get("htf_bias"),
                "btc_1h_regime": en_extra.get("btc_1h_regime"),
                "direction": en_extra.get("direction"),
                "window_size": en_extra.get("window_size"),
                "ai_used": en_extra.get("ai_used"),
                "signal_reason": en_extra.get("signal_reason", ""),
            }
        )
    return trades


def fmt_row(label: str, trades: list[dict], width: int = 32) -> str:
    if not trades:
        return f"  {label:<{width}}     0   —     —          —"
    n = len(trades)
    wins = sum(1 for t in trades if t["result"] == "WIN")
    pnl = sum(t["pnl"] for t in trades)
    avg_edge = sum((t["edge"] or 0) for t in trades) / n
    wr = wins / n * 100
    return f"  {label:<{width}} {n:5d}  {wr:5.1f}%  {pnl:+8.2f}  edge_avg={avg_edge:.3f}"


def slice_summary(trades: list[dict], title: str) -> None:
    print(f"\n=== {title} ===")
    print(f"  total trades: {len(trades)}")
    if not trades:
        return
    wins = sum(1 for t in trades if t["result"] == "WIN")
    pnl = sum(t["pnl"] for t in trades)
    print(f"  WR: {wins}/{len(trades)} = {wins/len(trades)*100:.1f}%   PnL: {pnl:+.2f}")

    print("\n  -- by strategy × action --")
    by_sa = defaultdict(list)
    for t in trades:
        by_sa[(t["strategy"], t["action"])].append(t)
    for key in sorted(by_sa):
        print(fmt_row(f"{key[0]:<12} {key[1]}", by_sa[key]))

    print("\n  -- BUY_NO regime context (semantic check) --")
    print("     BUY_NO should fire when indicators say price WON'T close above start-of-window.")
    print("     If BUY_NO is firing under htf_bias=BULLISH, that's the counter-trend path.")
    buy_nos = [t for t in trades if t["action"] == "BUY_NO"]
    if not buy_nos:
        print("     (no BUY_NO trades in this session)")
    else:
        by_regime = defaultdict(list)
        for t in buy_nos:
            key = (t["strategy"], t["htf_bias"], t["btc_1h_regime"])
            by_regime[key].append(t)
        for (strat, htf, btc1h), ts in sorted(by_regime.items()):
            print(fmt_row(f"{strat:<12} htf={htf} btc1h={btc1h}", ts, width=44))

    print("\n  -- BUY_YES by HTF bias (sanity) --")
    buy_yeses = [t for t in trades if t["action"] == "BUY_YES"]
    by_htf = defaultdict(list)
    for t in buy_yeses:
        by_htf[(t["strategy"], t["htf_bias"])].append(t)
    for key in sorted(by_htf):
        print(fmt_row(f"{key[0]:<12} htf={key[1]}", by_htf[key], width=32))

    print("\n  -- exit reasons --")
    by_exit = defaultdict(list)
    for t in trades:
        by_exit[(t["action"], t["exit_reason"])].append(t)
    for key in sorted(by_exit):
        print(fmt_row(f"{key[0]} / {key[1]}", by_exit[key], width=40))


def main() -> int:
    args = sys.argv[1:]
    if not args:
        sessions = latest_sessions(2)
    else:
        sessions = [Path(a) if Path(a).is_absolute() else PAPER_DIR / a for a in args]

    if not sessions:
        print("no sessions found", file=sys.stderr)
        return 1

    for s in sessions:
        if not s.exists():
            print(f"missing: {s}", file=sys.stderr)
            return 1

    for s in sessions:
        trades = load_trades(s)
        slice_summary(trades, s.name)

    if len(sessions) >= 2:
        print("\n=== DELTA (first vs second arg) ===")
        new_t = load_trades(sessions[0])
        old_t = load_trades(sessions[1])

        def stats(ts: list[dict], pred):
            f = [t for t in ts if pred(t)]
            n = len(f)
            wr = (sum(1 for t in f if t["result"] == "WIN") / n * 100) if n else 0.0
            pnl = sum(t["pnl"] for t in f)
            return n, wr, pnl

        rows = [
            ("ALL", lambda t: True),
            ("BUY_YES", lambda t: t["action"] == "BUY_YES"),
            ("BUY_NO", lambda t: t["action"] == "BUY_NO"),
            ("BUY_NO htf=BULLISH (counter-trend)", lambda t: t["action"] == "BUY_NO" and t["htf_bias"] == "BULLISH"),
            ("BTC", lambda t: t["strategy"] == "bitcoin"),
            ("ETH", lambda t: t["strategy"] == "eth_macro"),
            ("HYPE", lambda t: t["strategy"] == "hype_macro"),
            ("XRP", lambda t: t["strategy"] == "xrp_macro"),
            ("SOL", lambda t: t["strategy"] == "sol_macro"),
        ]
        print(f"  {'slice':<40} {'new(n/wr/pnl)':<28} {'old(n/wr/pnl)':<28}")
        for label, pred in rows:
            n_new, wr_new, p_new = stats(new_t, pred)
            n_old, wr_old, p_old = stats(old_t, pred)
            print(f"  {label:<40} {n_new:3d}/{wr_new:5.1f}%/{p_new:+7.2f}     {n_old:3d}/{wr_old:5.1f}%/{p_old:+7.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
