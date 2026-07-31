#!/usr/bin/env python3
"""Stop / hold counterfactual sweep for one lane (operator 2026-07-31).

Answers, for a single asset/window/side lane, the question the exec diagnostic
raised: is the current stop cutting trades that would have resolved GREEN, and
would a WIDER stop or HOLD-to-resolution net better?

Two counterfactuals, kept separate so the modeled one can't contaminate the
pure-data one:

  A. BINARY stop-vs-hold  (NO modeling — pure settler data)
     For every SETTLED trade in the lane:
        actual_pnl   = what the live exit netted (trades.jsonl pnl)
        held_pnl     = what holding to resolution nets (trades_settled.jsonl)
     Sum both. This is the ground-truth "should this lane hold?" answer.

  B. STOP-WIDTH sweep  (modeled off each trade's max-adverse-excursion)
     For a grid of stop widths S:
        if mae_pct <= -S  -> stop fires, pnl ~= -S * notional - round_trip_fee
        else              -> trade never hit S, rides to resolution (held_pnl)
     Assumption stated in output: mae_pct is the realized worst drawdown, so a
     stop at S is hit iff the trade drew down at least S. Fill at exactly -S is
     optimistic by any gap-through beyond S; flagged per trade where mae << -S.

Read-only. Decisions = LIVE realized; this is a calibration read-out.

Usage:
  python scripts/stop_hold_sweep.py --asset BTC --window 15m --side DOWN
  python scripts/stop_hold_sweep.py --asset XRP --window 5m --side DOWN --since 2026-07-30
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
CAL = ROOT / "data" / "calibration"
PAPER = ROOT / "data" / "paper_trades"
TRADES = CAL / "trades.jsonl"
SETTLED = CAL / "trades_settled.jsonl"

_ASSET = {"bitcoin": "BTC", "sol_macro": "SOL", "eth_macro": "ETH", "xrp_macro": "XRP",
          "bnb_macro": "BNB", "doge_macro": "DOGE", "hype_macro": "HYPE"}
_SIDE = {"BUY_YES": "UP", "BUY_NO": "DOWN"}


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


def _default_session() -> str:
    s = sorted(PAPER.glob("test_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return s[0].name if s else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", required=True)
    ap.add_argument("--window", required=True)
    ap.add_argument("--side", required=True, choices=["UP", "DOWN"])
    ap.add_argument("--session", default=None)
    ap.add_argument("--since", default=None, help="ISO ts lower bound (overrides session)")
    ap.add_argument("--grid", default="0.10,0.15,0.20,0.25,0.30,0.33,0.40,0.50")
    args = ap.parse_args()

    asset = args.asset.upper()
    window = args.window
    side = args.side.upper()
    session = args.session or _default_session()

    settled = {r.get("trade_id"): r for r in _iter(SETTLED) if r.get("trade_id")}

    def keep(r):
        if _ASSET.get(str(r.get("strategy")), "?") != asset:
            return False
        if str(r.get("window")) != window:
            return False
        if _SIDE.get(str(r.get("action")).upper()) != side:
            return False
        if args.since is not None:
            return str(r.get("ts", "")) >= args.since
        return r.get("session_id") == session

    trades = [t for t in _iter(TRADES) if keep(t)]

    rows = []
    for t in trades:
        st = settled.get(t.get("trade_id"), {})
        rows.append({
            "tid": t.get("trade_id"),
            "notional": _num(t.get("notional")),
            "mae": _num(t.get("mae_pct")),
            "mfe": _num(t.get("mfe_pct")),
            "eff_stop": _num(t.get("effective_stop_loss_pct")),
            "exit_reason": str(t.get("exit_reason") or "?"),
            "pnl": _num(t.get("pnl")),
            "entry_fee": _num(t.get("entry_fee_usdc")) or 0.0,
            "exit_fee": _num(t.get("fill_fee_usdc")) or 0.0,
            "held_win": st.get("held_win"),
            "held_pnl": _num(st.get("held_pnl")),
            "hold_minus_exit": _num(st.get("hold_minus_exit_pnl")),
            "settled": bool(st),
        })

    scope = f"since {args.since}" if args.since else session
    L = [f"=== STOP/HOLD SWEEP — {asset} {window} {side} — {scope} ===", ""]
    n = len(rows)
    sett = [r for r in rows if r["settled"] and r["held_pnl"] is not None]
    L.append(f"trades: {n}   settled(with held counterfactual): {len(sett)}")
    if not sett:
        L.append("No settled trades with a hold counterfactual — cannot sweep. "
                 "(taken_exit_settler hasn't resolved these yet.)")
        print("\n".join(L))
        return 0

    # ---- A. binary stop-vs-hold (pure data) --------------------------------
    act = sum(r["pnl"] for r in sett if r["pnl"] is not None)
    held = sum(r["held_pnl"] for r in sett)
    stops = [r for r in sett if "stop" in r["exit_reason"]]
    stop_killed_green = [r for r in stops if r.get("held_win") and (r.get("hold_minus_exit") or 0) > 0]
    stop_saved = [r for r in stops if not r.get("held_win") and (r.get("hold_minus_exit") or 0) < 0]
    L += ["",
          "--- A. BINARY stop-vs-hold (pure settler data, no modeling) ---",
          f"  actual (as traded)      : {act:+.2f}",
          f"  hold-to-resolution      : {held:+.2f}   (delta {held-act:+.2f})",
          f"  stops total             : {len(stops)}",
          f"  stops that killed GREEN  : {len(stop_killed_green)}  "
          f"(sum left on table {sum(r['hold_minus_exit'] for r in stop_killed_green):+.2f})",
          f"  stops that SAVED loss    : {len(stop_saved)}  "
          f"(sum saved {sum(-r['hold_minus_exit'] for r in stop_saved):+.2f})"]

    # ---- B. stop-width sweep (modeled off mae) -----------------------------
    grid = sorted(float(x) for x in args.grid.split(",") if x.strip())
    L += ["", "--- B. stop-width sweep (modeled: stop hits iff mae<=-S, fill ~ -S*notional - fees) ---",
          f"{'stopS':>7}{'lane_pnl':>10}{'#stopped':>9}{'#ride':>7}{'#green_cut':>11}"]
    modelable = [r for r in sett if r["mae"] is not None and r["notional"] is not None]
    for S in grid:
        total = 0.0
        n_stop = n_ride = n_green_cut = 0
        for r in modelable:
            fees = r["entry_fee"] + r["exit_fee"]
            if r["mae"] <= -S:
                total += -S * r["notional"] - fees
                n_stop += 1
                if r.get("held_win"):
                    n_green_cut += 1
            else:
                total += r["held_pnl"]
                n_ride += 1
        L.append(f"{S:>7.2f}{total:>10.2f}{n_stop:>9}{n_ride:>7}{n_green_cut:>11}")
    # hold row (no stop)
    hold_total = sum(r["held_pnl"] for r in modelable)
    L.append(f"{'HOLD':>7}{hold_total:>10.2f}{0:>9}{len(modelable):>7}{0:>11}")

    # ---- per-trade -----------------------------------------------------------
    L += ["", "--- per-trade (settled) ---",
          f"{'mfe':>5}{'mae':>6}{'effStop':>8}{'exit':>20}{'actual':>8}{'held':>8}{'h-e':>8}{'gap?':>6}"]
    for r in sorted(sett, key=lambda z: (z["hold_minus_exit"] or 0)):
        # gap flag: did drawdown blow well past the eff stop (gap-through risk)?
        gap = ""
        if r["mae"] is not None and r["eff_stop"]:
            if r["mae"] <= -(r["eff_stop"] + 0.10):
                gap = "GAP"
        L.append(f"{(r['mfe'] or 0)*100:>4.0f}%{(r['mae'] or 0)*100:>5.0f}%"
                 f"{(r['eff_stop'] or 0):>8.2f}{r['exit_reason'][:19]:>20}"
                 f"{(r['pnl'] or 0):>8.2f}{(r['held_pnl'] or 0):>8.2f}"
                 f"{(r['hold_minus_exit'] or 0):>8.2f}{gap:>6}")

    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
