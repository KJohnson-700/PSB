#!/usr/bin/env python3
"""Give-back analyzer (Script A) — dollarize the "stops cut green winners" leak.

For every lane (strategy|window|side) this measures how much money was on the
table at the trade's Maximum Favorable Excursion (MFE) but was NOT realized at
exit — i.e. the give-back:

    give_back_$ = max(0, mfe_pct - realized_pct) * notional

summed per lane, and highlights the tell that motivated this whole line of work:
what fraction of *stopped* trades had already gone GREEN (MFE > +5%) before the
stop cut them. A lane with large give-back and many green-then-stopped trades is
leaking money to its exit, not its entry.

Decisions come from LIVE REALIZED trades only (data/calibration/trades.jsonl);
ghosts are never read here. Pure stdlib, read-only, fail-safe.

Usage:
  python3 scripts/giveback_analyzer.py                 # last 8 sessions
  python3 scripts/giveback_analyzer.py --sessions 12
  python3 scripts/giveback_analyzer.py --n 300         # last 300 closed trades
  python3 scripts/giveback_analyzer.py --session test_20260801_185754
  python3 scripts/giveback_analyzer.py --all --json
"""
from __future__ import annotations
import argparse, json, sys
from collections import defaultdict
from pathlib import Path

TRADES = Path(__file__).resolve().parent.parent / "data" / "calibration" / "trades.jsonl"

MFE_GREEN = 0.05   # "had gone green" threshold before a stop
MIN_LANE_N = 3     # don't headline a lane with fewer than this many trades


def _f(r, k):
    v = r.get(k)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def load_trades(args) -> list[dict]:
    rows = []
    try:
        with open(TRADES) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        print(f"[giveback] no trades log at {TRADES}", file=sys.stderr)
        return []
    if args.session:
        rows = [r for r in rows if r.get("session_id") == args.session]
    elif not args.all:
        sess = sorted({r.get("session_id", "") for r in rows if r.get("session_id")})
        keep = set(sess[-args.sessions:])
        rows = [r for r in rows if r.get("session_id") in keep]
    if args.n:
        rows = rows[-args.n:]
    return rows


def notional(r):
    return _f(r, "notional") or _f(r, "size") or _f(r, "cost_basis") or 0.0


def analyze(rows):
    lanes = defaultdict(lambda: {
        "n": 0, "pnl": 0.0, "give_back": 0.0,
        "stops": 0, "green_stops": 0, "mfe_sum": 0.0, "realized_on_stops": 0.0,
    })
    tot = {"give_back": 0.0, "pnl": 0.0, "n": 0, "stops": 0, "green_stops": 0}
    for r in rows:
        strat = r.get("strategy", "?"); win = r.get("window", "?"); act = r.get("action", "?")
        key = f"{strat}|{win}|{act}"
        mfe = _f(r, "mfe_pct"); rp = _f(r, "realized_pct")
        if rp is None:
            rp = _f(r, "pnl_pct_at_exit")
        notl = notional(r)
        pnl = _f(r, "pnl") or 0.0
        L = lanes[key]
        L["n"] += 1; L["pnl"] += pnl; tot["n"] += 1; tot["pnl"] += pnl
        if mfe is not None and rp is not None and notl:
            gb = max(0.0, (mfe - rp)) * notl
            L["give_back"] += gb; tot["give_back"] += gb
        is_stop = str(r.get("exit_reason", "")).startswith("updown_stop")
        if is_stop:
            L["stops"] += 1; tot["stops"] += 1
            if rp is not None and notl:
                L["realized_on_stops"] += rp * notl
            if mfe is not None:
                L["mfe_sum"] += mfe
                if mfe > MFE_GREEN:
                    L["green_stops"] += 1; tot["green_stops"] += 1
    return lanes, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=8, help="last N sessions (default 8)")
    ap.add_argument("--session", type=str, default=None, help="one exact session_id")
    ap.add_argument("--n", type=int, default=None, help="last N closed trades")
    ap.add_argument("--all", action="store_true", help="use full history (ignore --sessions)")
    ap.add_argument("--top", type=int, default=15, help="show top-N leaking lanes")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_trades(args)
    if not rows:
        print("[giveback] no trades after filter"); return
    lanes, tot = analyze(rows)

    ranked = sorted(lanes.items(), key=lambda kv: -kv[1]["give_back"])
    if args.json:
        out = {"total": tot, "lanes": {k: v for k, v in ranked}}
        print(json.dumps(out, indent=2)); return

    scope = (f"session={args.session}" if args.session
             else "all-history" if args.all
             else f"last {args.sessions} sessions")
    gstop_pct = (tot["green_stops"] / tot["stops"]) if tot["stops"] else 0.0
    print(f"\nGIVE-BACK ANALYZER  ·  {scope}  ·  {tot['n']} trades")
    print(f"  realized PnL       {tot['pnl']:+.2f}")
    print(f"  total give-back    ${tot['give_back']:,.0f}   (money at MFE, not realized)")
    print(f"  green-then-stopped {tot['green_stops']}/{tot['stops']} = {gstop_pct:.0%}"
          f"   (stops that were >+{MFE_GREEN*100:.0f}% before dying)")
    print(f"\n  {'lane':28s} {'n':>4} {'pnl':>8} {'giveback$':>10} {'grn/stop':>9} {'avgMFE':>7}")
    print("  " + "-" * 72)
    for key, L in ranked[:args.top]:
        if L["n"] < MIN_LANE_N and L["give_back"] == 0:
            continue
        gs = f"{L['green_stops']}/{L['stops']}" if L["stops"] else "-"
        amfe = (L["mfe_sum"] / L["stops"]) if L["stops"] else 0.0
        print(f"  {key:28s} {L['n']:>4} {L['pnl']:>+8.2f} {L['give_back']:>10,.0f} "
              f"{gs:>9} {amfe:>+7.2f}")
    print("\n  read: a big giveback$ with a high grn/stop ratio = the exit is cutting")
    print("        trades that had already gone green. Entry isn't the problem there.\n")


if __name__ == "__main__":
    main()
