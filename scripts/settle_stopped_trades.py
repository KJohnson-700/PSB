#!/usr/bin/env python3
"""STOPPED-TRADE SETTLER — does the catastrophic stop SAVE money or COST it? (2026-08-16)

THE QUESTION THIS EXISTS TO ANSWER, WITH DATA INSTEAD OF ARGUMENT.
In the current era 14% of trades exit on `hold_catastrophic_stop` and they take -$336, while the
231 trades held to resolution make +$291 at 59.7% WR. So one exit turns a profitable book negative.
But that does NOT mean removing it is right: a stopped trade might have gone on to lose MORE. Prior
evidence points the other way — 620 stopped trades from an earlier regime, settled against real
resolutions, would have lost $2,604 MORE if held (only 18% resolved our way).

⛔ THE TRAP THIS SCRIPT IS BUILT TO AVOID (I hit it twice on 2026-08-16 before writing this).
NEVER grade a counterfactual against realized P&L. A stop is NOT a resolution — the market often
resolves OUR way after we are stopped out. Comparing "held" (graded on resolution) against "actual"
(graded on P&L across an exit layer) manufactured a fake +$6,003 flip result and a fake
"six lanes are wrong-sided at -6 sigma". Both evaporated when graded apples-to-apples.
So: this settles the stop against the REAL Polymarket resolution of the SAME market, and reports
BOTH the gross and the fee-adjusted counterfactual. No P&L-vs-resolution comparisons anywhere.

Read-only on the bot's data; appends to its own ledger. Idempotent — a trade_id already settled is
never re-settled. Safe to run beside the live bot.

Usage:
  .venv/bin/python scripts/settle_stopped_trades.py --once
  .venv/bin/python scripts/settle_stopped_trades.py --loop --interval 1800
  .venv/bin/python scripts/settle_stopped_trades.py --report        # verdict from the ledger only
"""
import argparse
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TRADES = ROOT / "data" / "calibration" / "trades.jsonl"
LEDGER = ROOT / "data" / "calibration" / "stopped_trades_settled.jsonl"

# exits that are NOT a resolution — the population in question
STOP_REASONS = {
    "hold_catastrophic_stop", "updown_stop_loss", "never_green_cut",
    "updown_time_stop", "take_profit_giveback", "updown_flatten_pre_resolution",
}
# measured fee drag as a share of notional (see the 08-16 payoff audit)
FEE_RATE = 0.0396


def _market_index():
    """trade_id -> market_id, from the paper-trade entry journals."""
    idx = {}
    for p in glob.glob(str(ROOT / "data" / "paper_trades" / "*" / "entries.jsonl")):
        try:
            for line in open(p, errors="ignore"):
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                tid, mid = e.get("trade_id"), e.get("market_id")
                if tid and mid:
                    idx[str(tid)] = str(mid)
        except OSError:
            continue
    return idx


def _already():
    done = set()
    if LEDGER.exists():
        for line in open(LEDGER, errors="ignore"):
            try:
                done.add(str(json.loads(line).get("trade_id")))
            except Exception:
                continue
    return done


def _stopped_trades(since: str):
    out = []
    for line in open(TRADES, errors="ignore"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("pnl") is None or r.get("shadow_mode"):
            continue
        if str(r.get("exit_reason") or "") not in STOP_REASONS:
            continue
        if since and str(r.get("opened_at") or "")[:10] < since:
            continue
        if not r.get("entry_price") or not r.get("notional"):
            continue
        out.append(r)
    return out


def settle(since: str, throttle: float, limit: int) -> dict:
    from src.analysis.ghost_calibration import fetch_resolution

    idx = _market_index()
    done = _already()
    rows = [r for r in _stopped_trades(since) if str(r.get("trade_id")) not in done]
    cache, summary = {}, Counter()
    with open(LEDGER, "a", encoding="utf-8") as fh:
        for r in rows:
            if limit and summary["settled"] >= limit:
                break
            tid = str(r.get("trade_id"))
            mid = idx.get(tid)
            if not mid:
                summary["no_market_id"] += 1
                continue
            oc = fetch_resolution(mid, cache)
            if throttle:
                time.sleep(throttle)
            if oc not in ("YES", "NO"):
                summary["unresolved"] += 1
                continue
            action = str(r.get("action") or "")
            right = (action == "BUY_YES") == (oc == "YES")
            stake = float(r["notional"])
            entry = float(r["entry_price"])
            # HELD to resolution: win pays (1-entry)/entry per $1; loss forfeits the stake.
            held_gross = stake * (1.0 - entry) / entry if right else -stake
            held_net = held_gross - stake * FEE_RATE
            fh.write(json.dumps({
                "trade_id": tid, "market_id": mid, "settled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "strategy": r.get("strategy"), "window": r.get("window"), "action": action,
                "exit_reason": r.get("exit_reason"), "opened_at": r.get("opened_at"),
                "entry_price": entry, "notional": stake,
                "actual_pnl": float(r["pnl"]), "outcome": oc, "held_right_side": right,
                "held_pnl_gross": round(held_gross, 4), "held_pnl_net": round(held_net, 4),
                "stop_saved": round(float(r["pnl"]) - held_net, 4),
            }, separators=(",", ":")) + "\n")
            summary["settled"] += 1
    summary["candidates"] = len(rows)
    return dict(summary)


def report():
    if not LEDGER.exists():
        print("no ledger yet — run --once first")
        return
    rows = []
    for line in open(LEDGER, errors="ignore"):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if not rows:
        print("ledger empty")
        return
    n = len(rows)
    right = sum(1 for r in rows if r.get("held_right_side"))
    act = sum(float(r["actual_pnl"]) for r in rows)
    held = sum(float(r["held_pnl_net"]) for r in rows)
    print("=== DOES THE STOP SAVE MONEY OR COST IT?  (n=%d settled) ===" % n)
    print("  stopped trades that WOULD have resolved our way : %d = %.1f%%" % (right, 100 * right / n))
    print("  ACTUAL pnl (we stopped out)                     : %+.2f" % act)
    print("  HAD WE HELD to resolution (net of fees)         : %+.2f" % held)
    verdict = "THE STOP IS SAVING MONEY — KEEP IT" if act > held else "THE STOP IS COSTING MONEY"
    print("  STOP SAVED                                      : %+.2f   => %s" % (act - held, verdict))
    print("\n  by exit reason:")
    g = defaultdict(list)
    for r in rows:
        g[str(r.get("exit_reason"))].append(r)
    for k, v in sorted(g.items(), key=lambda x: -len(x[1])):
        a = sum(float(x["actual_pnl"]) for x in v)
        h = sum(float(x["held_pnl_net"]) for x in v)
        rr = sum(1 for x in v if x.get("held_right_side"))
        print("     %-28s n=%3d  right %5.1f%%  actual %+9.2f  held %+9.2f  saved %+9.2f"
              % (k, len(v), 100 * rr / len(v), a, h, a - h))
    print("\n  by lane (n>=5):")
    g2 = defaultdict(list)
    for r in rows:
        g2["%s|%s|%s" % (r.get("strategy"), r.get("window"), r.get("action"))].append(r)
    for k, v in sorted(g2.items(), key=lambda x: sum(float(i["actual_pnl"]) - float(i["held_pnl_net"]) for i in x[1])):
        if len(v) < 5:
            continue
        a = sum(float(x["actual_pnl"]) for x in v)
        h = sum(float(x["held_pnl_net"]) for x in v)
        print("     %-26s n=%3d  actual %+9.2f  held %+9.2f  saved %+9.2f" % (k, len(v), a, h, a - h))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--interval", type=float, default=1800)
    ap.add_argument("--since", default="2026-08-14", help="only trades opened on/after (default: post exit-kill)")
    ap.add_argument("--throttle", type=float, default=0.05)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if a.report:
        report()
        return 0
    while True:
        s = settle(a.since, a.throttle, a.limit)
        print("[stopped-settler] %s" % s, flush=True)
        report()
        if not a.loop:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
