#!/usr/bin/env python3
"""Give-back + fee-drag analyzer (operator 2026-07-31).

Two leaks that turn a real edge into a $0.04 "win":

  A. GIVE-BACK — the winner peaked (mfe) then the trail/stop handed most of it
     back before exit (realized_pct << mfe_pct). Per lane: how much of the peak
     is banked vs given back, and how a tighter trail would have captured more.

  B. FEE-DRAG — the notional is so small the round-trip taker fee eats a big %
     of it, so even a captured winner nets ~0. Per lane: notional distribution,
     fee as % of notional, and the min-notional floor where fees stay < target%.

Read-only. Decisions = LIVE realized; this is a calibration read-out. Does NOT
edit config — proposal only.

Usage:
  python scripts/giveback_fee_analyzer.py --since 2026-07-30
  python scripts/giveback_fee_analyzer.py --since 2026-07-30 --window 1h
"""
from __future__ import annotations

import argparse
import json
import statistics as stats
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAL = ROOT / "data" / "calibration"
TRADES = CAL / "trades.jsonl"
_ASSET = {"bitcoin": "BTC", "sol_macro": "SOL", "eth_macro": "ETH", "xrp_macro": "XRP",
          "bnb_macro": "BNB", "doge_macro": "DOGE", "hype_macro": "HYPE"}
_SIDE = {"BUY_YES": "UP", "BUY_NO": "DOWN"}
FEE_TARGET = 0.03  # fees should be < 3% of notional


def _iter(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-30")
    ap.add_argument("--window", default=None, help="filter e.g. 1h")
    args = ap.parse_args()

    lanes = defaultdict(list)
    allfees = []
    for t in _iter(TRADES):
        if str(t.get("ts", "")) < args.since:
            continue
        if args.window and str(t.get("window")) != args.window:
            continue
        asset = _ASSET.get(str(t.get("strategy")), str(t.get("strategy")))
        key = f"{asset} {t.get('window')} {_SIDE.get(str(t.get('action')).upper(),'?')}"
        fee = (_num(t.get("entry_fee_usdc")) or 0) + (_num(t.get("fill_fee_usdc")) or 0)
        rec = {
            "mfe": _num(t.get("mfe_pct")), "realized": _num(t.get("realized_pct")),
            "pnl": _num(t.get("pnl")), "raw": _num(t.get("raw_signal_pnl")),
            "notional": _num(t.get("notional")), "fee": fee,
            "exit": str(t.get("exit_reason") or "?"),
        }
        lanes[key].append(rec)
        if rec["notional"]:
            allfees.append((rec["notional"], fee))

    print(f"=== GIVE-BACK + FEE-DRAG — since {args.since}"
          + (f" window={args.window}" if args.window else "") + " ===\n")

    # ---- A. give-back per lane (winners only: pnl>0 or mfe>0) --------------
    print("--- A. GIVE-BACK (winners: how much of the peak got banked) ---")
    print(f"{'lane':<16}{'nWin':>5}{'mfe%':>6}{'realzd%':>8}{'gaveBack%':>10}{'gaveBack$':>10}{'amputated':>10}")
    gb_rows = []
    for key in sorted(lanes):
        rs = lanes[key]
        wins = [r for r in rs if (r["pnl"] or 0) > 0 or (r["mfe"] or 0) >= 0.10]
        if not wins:
            continue
        mfes = [r["mfe"] for r in wins if r["mfe"] is not None]
        reals = [r["realized"] for r in wins if r["realized"] is not None]
        if not mfes or not reals:
            continue
        gave_pct = stats.mean(mfes) - stats.mean(reals)
        # $ given back = (mfe - realized) * notional, per trade
        gave_d = stats.mean([((r["mfe"] or 0) - (r["realized"] or 0)) * (r["notional"] or 0) for r in wins])
        amputated = sum(1 for r in wins if (r["mfe"] or 0) >= 0.20 and (r["realized"] or 0) <= 0.05)
        print(f"{key:<16}{len(wins):>5}{stats.mean(mfes)*100:>6.0f}{stats.mean(reals)*100:>8.0f}"
              f"{gave_pct*100:>10.0f}{gave_d:>10.2f}{amputated:>10}")
        gb_rows.append((key, len(wins), gave_pct, gave_d, amputated))

    # ---- B. fee-drag per lane ---------------------------------------------
    print("\n--- B. FEE-DRAG (fee as % of notional; target < 3%) ---")
    print(f"{'lane':<16}{'n':>4}{'medNotional':>12}{'medFee':>8}{'fee/notl%':>10}{'over3%':>8}")
    for key in sorted(lanes):
        rs = [r for r in lanes[key] if r["notional"]]
        if not rs:
            continue
        notls = [r["notional"] for r in rs]
        fees = [r["fee"] for r in rs]
        ratios = [r["fee"] / r["notional"] for r in rs if r["notional"] > 0]
        over = sum(1 for x in ratios if x > FEE_TARGET)
        print(f"{key:<16}{len(rs):>4}{stats.median(notls):>12.2f}{stats.median(fees):>8.3f}"
              f"{stats.median(ratios)*100:>10.1f}{over:>8}")

    # ---- fee-floor recommendation -----------------------------------------
    print("\n--- FEE-FLOOR: min notional so round-trip fee < target% ---")
    if allfees:
        # typical round-trip fee (median) -> min notional = fee / target
        med_fee = stats.median([f for _, f in allfees])
        rec_floor = med_fee / FEE_TARGET
        under = sum(1 for n, _ in allfees if n < rec_floor)
        print(f"  median round-trip fee = ${med_fee:.3f}")
        print(f"  -> min notional for fee<{FEE_TARGET*100:.0f}%: ${rec_floor:.2f}")
        print(f"  trades below that floor (fee-dominated): {under}/{len(allfees)}")
    print("\n(Proposal only — no config changed. Trail-capture per lane needs the")
    print(" give-back $ ranked above + a noise check before setting gaps.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
