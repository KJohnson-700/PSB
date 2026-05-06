#!/usr/bin/env python3
"""Drill into BUY_YES updown_time_stop losers to find common patterns.

Focus: HYPE/ETH BUY_YES entries that never reached TP and timed out.
Compare against winners in the same session to see what differentiates them.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
PAPER_DIR = ROOT / "data" / "paper_trades"


def load_trades(session: Path) -> list[dict]:
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

    out = []
    for tid, ex in exits.items():
        en = entries.get(tid, {})
        en_ex = en.get("extra", {})
        ex_ex = ex.get("extra", {})
        sr = en_ex.get("signal_reason", "")
        out.append(
            {
                "trade_id": tid,
                "strategy": ex.get("strategy"),
                "action": ex.get("action"),
                "result": ex_ex.get("result"),
                "exit_reason": ex_ex.get("exit_reason"),
                "pnl": ex.get("pnl", 0.0),
                "hold_seconds": ex_ex.get("hold_seconds"),
                "edge": en_ex.get("edge"),
                "est_prob": en_ex.get("est_prob"),
                "confidence": en.get("confidence"),
                "rsi": en_ex.get("rsi"),
                "corr_1h": en_ex.get("corr_1h"),
                "htf_bias": en_ex.get("htf_bias"),
                "btc_1h_regime": en_ex.get("btc_1h_regime"),
                "yes_price": en_ex.get("yes_price"),
                "lag_magnitude": en_ex.get("lag_magnitude"),
                "ai_used": en_ex.get("ai_used"),
                "signal_reason": sr,
                "minutes_to_market_end": en_ex.get("minutes_to_market_end"),
                "window_size": en_ex.get("window_size"),
                "hour_utc": en_ex.get("hour_utc"),
                "market_question": ex.get("market_question", ""),
            }
        )
    return out


def parse_macd_from_reason(sr: str) -> dict:
    """Pull MACD/exp tags out of signal_reason string."""
    out: dict = {}
    m = re.search(r"5m_MACD=([+-][\d.]+)", sr)
    if m:
        out["macd_5m"] = float(m.group(1))
    m = re.search(r"15m_MACD=([+-][\d.]+)", sr)
    if m:
        out["macd_15m"] = float(m.group(1))
    m = re.search(r"exp=([A-Z]+)", sr)
    if m:
        out["expansion"] = m.group(1)
    if "MACD>signal" in sr:
        out["macd_signal"] = "above"
    elif "MACD<signal" in sr:
        out["macd_signal"] = "below"
    return out


def stats(values: list[float]) -> str:
    if not values:
        return "—"
    return f"n={len(values)} avg={mean(values):.3f} med={median(values):.3f} min={min(values):.3f} max={max(values):.3f}"


def show_group(label: str, trades: list[dict]) -> None:
    print(f"\n{'=' * 70}")
    print(f"{label}  (n={len(trades)})")
    print("=" * 70)
    if not trades:
        return

    # Aggregate fields
    edges = [t["edge"] for t in trades if t["edge"] is not None]
    confs = [t["confidence"] for t in trades if t["confidence"] is not None]
    rsis = [t["rsi"] for t in trades if t["rsi"] is not None]
    corrs = [t["corr_1h"] for t in trades if t["corr_1h"] is not None]
    yeps = [t["yes_price"] for t in trades if t["yes_price"] is not None]
    holds = [t["hold_seconds"] for t in trades if t["hold_seconds"] is not None]
    mte = [t["minutes_to_market_end"] for t in trades if t["minutes_to_market_end"] is not None]

    print(f"  edge:        {stats(edges)}")
    print(f"  confidence:  {stats(confs)}")
    print(f"  RSI:         {stats(rsis)}")
    print(f"  corr_1h:     {stats(corrs)}")
    print(f"  yes_price:   {stats(yeps)}")
    print(f"  hold_secs:   {stats(holds)}")
    print(f"  mins_to_end: {stats(mte)}")

    # MACD parse
    macds_5m = []
    macds_15m = []
    exps: dict = defaultdict(int)
    macd_sig: dict = defaultdict(int)
    for t in trades:
        p = parse_macd_from_reason(t["signal_reason"])
        if "macd_5m" in p:
            macds_5m.append(p["macd_5m"])
        if "macd_15m" in p:
            macds_15m.append(p["macd_15m"])
        if "expansion" in p:
            exps[p["expansion"]] += 1
        if "macd_signal" in p:
            macd_sig[p["macd_signal"]] += 1

    if macds_5m:
        print(f"  5m MACD:     {stats(macds_5m)}")
    if macds_15m:
        print(f"  15m MACD:    {stats(macds_15m)}")
    if exps:
        print(f"  expansion:   {dict(exps)}")
    if macd_sig:
        print(f"  macd>signal: {dict(macd_sig)}")

    # AI usage
    ai_count = sum(1 for t in trades if t["ai_used"])
    print(f"  ai_used:     {ai_count}/{len(trades)}")

    # Window size
    ws = defaultdict(int)
    for t in trades:
        ws[t["window_size"]] += 1
    print(f"  window:      {dict(ws)}")

    # btc_1h_regime
    btc1h = defaultdict(int)
    for t in trades:
        btc1h[t["btc_1h_regime"]] += 1
    print(f"  btc_1h:      {dict(btc1h)}")

    # Hour distribution
    hrs = defaultdict(int)
    for t in trades:
        hrs[t["hour_utc"]] += 1
    print(f"  hour_utc:    {dict(sorted(hrs.items()))}")


def show_per_trade(label: str, trades: list[dict]) -> None:
    print(f"\n--- {label} per-trade detail ---")
    if not trades:
        print("  (none)")
        return
    print(f"  {'strat':<10} {'edge':>5} {'conf':>5} {'rsi':>5} {'5m_macd':>8} {'mte':>4} {'pnl':>7}  market")
    for t in sorted(trades, key=lambda x: (x["strategy"], x["pnl"])):
        p = parse_macd_from_reason(t["signal_reason"])
        macd5 = p.get("macd_5m")
        macd5_s = f"{macd5:+.3f}" if macd5 is not None else "    —"
        mq = t["market_question"][:50]
        print(
            f"  {t['strategy']:<10} {t['edge'] or 0:>5.3f} "
            f"{t['confidence'] or 0:>5.3f} {t['rsi'] or 0:>5.1f} "
            f"{macd5_s:>8} {t['minutes_to_market_end'] or 0:>4} "
            f"{t['pnl']:>+7.2f}  {mq}"
        )


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: slice_timestop_losers.py <session_dir>", file=sys.stderr)
        return 1
    session = Path(args[0]) if Path(args[0]).is_absolute() else PAPER_DIR / args[0]
    if not session.exists():
        print(f"missing: {session}", file=sys.stderr)
        return 1

    trades = load_trades(session)
    print(f"session: {session.name}   total trades: {len(trades)}")

    # Slice 1: ETH/HYPE BUY_YES losers (time_stop) vs winners (TP)
    eth_hype = [t for t in trades if t["strategy"] in ("eth_macro", "hype_macro") and t["action"] == "BUY_YES"]
    losers = [t for t in eth_hype if t["exit_reason"] == "updown_time_stop"]
    winners = [t for t in eth_hype if t["exit_reason"] == "take_profit"]
    show_group("ETH+HYPE BUY_YES — TIME_STOP losers", losers)
    show_group("ETH+HYPE BUY_YES — take_profit winners", winners)
    show_per_trade("ETH+HYPE losers", losers)
    show_per_trade("ETH+HYPE winners", winners)

    # Slice 2: All BUY_YES time_stop losers across strategies (any pattern?)
    all_losers = [t for t in trades if t["action"] == "BUY_YES" and t["exit_reason"] == "updown_time_stop"]
    all_winners = [t for t in trades if t["action"] == "BUY_YES" and t["exit_reason"] == "take_profit"]
    show_group("ALL BUY_YES — TIME_STOP losers", all_losers)
    show_group("ALL BUY_YES — take_profit winners", all_winners)

    return 0


if __name__ == "__main__":
    sys.exit(main())
