#!/usr/bin/env python3
"""entry_exit_split.py — lane stats in TWO buckets, so entry quality and the exit layer
stop being read as one number.

WHY THIS IS THE WHOLE BALLGAME
──────────────────────────────
Grading a lane on realized P&L measures the ENTRY and the EXIT together, and that confound
has faked findings repeatedly in this codebase:
  · "six lanes are systematically wrong-sided at -4 to -6.6 sigma" — on resolutions those
    same lanes were POSITIVE; sol_macro|1h|BUY_YES read -11.24 on P&L and +35.35 on
    resolutions. It nearly got the best lane in the book paused.
  · "flipping them wins 56-64%" — 88.2% of those "losses" were STOPS, not resolutions.
    Graded on resolutions the flip wins 39.9%, worse than a coin.

So every lane gets two numbers and their difference:

  BUCKET A — ENTRY / DIRECTION.  What the position would have returned HELD TO RESOLUTION.
             This is the only honest measure of whether a lane picks the right side.
  BUCKET B — EXECUTED.           What we actually banked, exit layer included.
  DELTA B-A — THE EXIT LAYER'S CONTRIBUTION. Positive = exits ADD money. Negative = exits
             are a leak. This is the number to argue about, and it was previously buried.

⛔ THE REASON-LIST ROT THIS FIXES
`settle_stopped_trades.py` hardcodes six STOP_REASONS. Measured 2026-08-17, that list MISSES
`favorite_hard_stop` (48), `favorite_presettle_derisk` (31), `take_profit_late` (76) and every
`take_profit` (1441) / `hold_fixed_take_profit` (15) — **1,611 closed trades that nothing has
ever settled.** A hardcoded list of exits silently stops covering new exits.
So this file inverts the test: RESOLUTION_REASONS is the allowlist, and ANYTHING ELSE is
treated as exit-layer intervention. A new exit reason lands in the right bucket automatically.

HOW BUCKET A IS OBTAINED
  · resolution exits         -> already the truth, use realized pnl directly
  · every other exit         -> join trade_id -> market_id via the session entries.jsonl,
                                fetch the REAL Polymarket resolution, compute held P&L with
                                the SAME convention as settle_stopped_trades.py:
                                stake = notional, win pays stake*(1-e)/e, loss forfeits
                                stake, minus FEE_RATE on stake.
  Already-settled rows in stopped_trades_settled.jsonl and orphaned_positions_settled.jsonl
  are reused as a cache so this does not re-hit the API for work already done.

⛔ ERA-ANCHORED BY DEFAULT. Pooled multi-era tables are poisoned here — pre-08-13 trades were
bought at 0.71-0.86 where breakeven needs 71-86%, and mixing them with current-band trades
manufactured a "+3.88 BEAT at +3.2 sigma" that evaporated on inspection.

READ-ONLY on bot data. Appends only to its own ledger. Cannot affect a trade.

USAGE
  scripts/entry_exit_split.py                      # era-anchored, uses cached settlements
  scripts/entry_exit_split.py --settle --limit 400 # settle more unsettled early exits
  scripts/entry_exit_split.py --since 2026-08-13 --min-n 5
"""
import argparse
import glob
import json
import math
import os
import sys
import time
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
CAL = os.path.join(REPO, "data", "calibration")
TRADES = os.path.join(CAL, "trades.jsonl")
OUT = os.path.join(CAL, "exit_layer_settled.jsonl")
CACHES = (os.path.join(CAL, "stopped_trades_settled.jsonl"),
          os.path.join(CAL, "orphaned_positions_settled.jsonl"))

# ⛔ ALLOWLIST, not a blocklist. These are the exits that ARE a resolution. Everything else
# is the exit layer intervening — including exits invented after this file was written.
RESOLUTION_REASONS = {"updown_expired", "RESOLVED:YES (real)", "RESOLVED:NO (real)"}
FEE_RATE = 0.0396                      # identical to settle_stopped_trades.py
DEFAULT_ANCHOR = "2026-08-16T22:57:15"


def _market_index():
    """trade_id -> market_id, from the paper-trade entry journals."""
    idx = {}
    for p in glob.glob(os.path.join(REPO, "data", "paper_trades", "*", "entries.jsonl")):
        try:
            with open(p, errors="ignore") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    tid, mid = e.get("trade_id"), e.get("market_id")
                    if tid and mid:
                        idx[str(tid)] = str(mid)
        except OSError:
            continue
    return idx


def _load_cache():
    """trade_id -> held_pnl_net from every ledger that already settled something."""
    held = {}
    for path in list(CACHES) + [OUT]:
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    tid = str(r.get("trade_id") or "")
                    v = r.get("held_pnl_net", r.get("settled_pnl_net"))
                    if tid and v is not None:
                        held[tid] = float(v)
        except OSError:
            continue
    return held


def _held_pnl(notional, entry, action, outcome):
    right = (str(action) == "BUY_YES") == (outcome == "YES")
    gross = notional * (1.0 - entry) / entry if right else -notional
    return gross - notional * FEE_RATE, right


def load_trades(since):
    rows = []
    try:
        with open(TRADES, errors="ignore") as fh:
            for line in fh:
                try:
                    t = json.loads(line)
                except ValueError:
                    continue
                if t.get("pnl") is None or t.get("shadow_mode"):
                    continue
                if since and str(t.get("opened_at") or "") < since:
                    continue
                rows.append(t)
    except OSError:
        pass
    return rows


def settle_missing(rows, held, throttle, limit):
    """Settle exit-layer trades that no ledger has covered yet."""
    from src.analysis.ghost_calibration import fetch_resolution
    idx = _market_index()
    todo = [t for t in rows
            if str(t.get("exit_reason")) not in RESOLUTION_REASONS
            and str(t.get("trade_id")) not in held
            and t.get("entry_price") and t.get("notional")]
    no_mid = sum(1 for t in todo if str(t.get("trade_id")) not in idx)
    todo = [t for t in todo if str(t.get("trade_id")) in idx]
    todo.sort(key=lambda t: str(t.get("opened_at") or ""), reverse=True)
    capped = max(0, len(todo) - limit) if limit else 0
    if limit:
        todo = todo[:limit]

    cache, wrote, unresolved = {}, 0, 0
    os.makedirs(CAL, exist_ok=True)
    with open(OUT, "a") as fh:
        for t in todo:
            tid = str(t.get("trade_id"))
            oc = fetch_resolution(idx[tid], cache)
            if throttle:
                time.sleep(throttle)
            if oc not in ("YES", "NO"):
                unresolved += 1
                continue
            try:
                notional = float(t["notional"]); entry = float(t["entry_price"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0.0 < entry < 1.0) or notional <= 0:
                continue
            net, right = _held_pnl(notional, entry, t.get("action"), oc)
            held[tid] = net
            fh.write(json.dumps({
                "trade_id": tid, "market_id": idx[tid],
                "settled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "strategy": t.get("strategy"), "window": t.get("window"),
                "action": t.get("action"), "exit_reason": t.get("exit_reason"),
                "opened_at": t.get("opened_at"), "entry_price": entry,
                "notional": notional, "actual_pnl": float(t["pnl"]),
                "outcome": oc, "held_right_side": right,
                "held_pnl_net": round(net, 4),
                "exit_layer_delta": round(float(t["pnl"]) - net, 4),
                "source": "entry_exit_split",
            }, separators=(",", ":")) + "\n")
            wrote += 1
    return {"settled": wrote, "unresolved": unresolved,
            "no_market_id": no_mid, "capped_out": capped}


def lane_of(t):
    return (f"{str(t.get('strategy')).replace('_macro','')}|{t.get('window')}|"
            f"{'up' if t.get('action') == 'BUY_YES' else 'down'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=DEFAULT_ANCHOR)
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--throttle", type=float, default=0.03)
    ap.add_argument("--min-n", type=int, default=4)
    args = ap.parse_args()

    rows = load_trades(args.since)
    held = _load_cache()

    print("=== ENTRY / EXIT SPLIT ===")
    print(f"  era anchor            {args.since}")
    print(f"  closed trades in era  {len(rows)}")
    res = [t for t in rows if str(t.get("exit_reason")) in RESOLUTION_REASONS]
    early = [t for t in rows if str(t.get("exit_reason")) not in RESOLUTION_REASONS]
    print(f"    resolution exits    {len(res)}   (bucket A is directly true)")
    print(f"    exit-layer exits    {len(early)}   (bucket A needs settling)")

    if args.settle and early:
        print("\n  --- settling unsettled exit-layer trades vs REAL resolutions ---")
        print(f"  {settle_missing(rows, held, args.throttle, args.limit)}")

    covered = sum(1 for t in early if str(t.get("trade_id")) in held)
    print(f"\n  exit-layer trades with a held counterfactual: {covered}/{len(early)}")
    if covered < len(early):
        print(f"  ⚠️ {len(early) - covered} NOT settled — those trades are EXCLUDED from bucket A")
        print("     below, so the delta is a partial view. Run with --settle to close it.")

    # ── per-lane two-bucket table ────────────────────────────────────────────
    # ⛔ PAIRED ONLY. B-A is meaningless unless both buckets cover the SAME trades. The first
    # version of this summed B over ALL closed trades while A skipped the unsettled ones,
    # which inflated the delta by exactly those trades' realized P&L and printed a confident
    # "exits ADD money +73.09" off 15 trades that had no counterfactual at all. A trade now
    # enters BOTH buckets or NEITHER, and the excluded count is reported.
    lanes = defaultdict(lambda: {"a": [], "b": 0.0, "n": 0, "aw": 0, "an": 0, "px": [],
                                 "skipped": 0})
    for t in rows:
        L = lanes[lane_of(t)]
        pnl = float(t.get("pnl") or 0)
        tid = str(t.get("trade_id"))
        if str(t.get("exit_reason")) in RESOLUTION_REASONS:
            a_val = pnl                       # the exit IS the resolution
        elif tid in held:
            a_val = held[tid]                 # settled counterfactual
        else:
            L["skipped"] += 1                 # no counterfactual -> in NEITHER bucket
            continue
        L["a"].append(a_val)
        L["b"] += pnl
        L["n"] += 1
        L["an"] += 1
        L["aw"] += 1 if a_val > 0 else 0
        try:
            L["px"].append(float(t.get("entry_price")))
        except (TypeError, ValueError):
            pass

    print(f"\n  {'lane':18s} {'n':>3} | {'A entry$':>9} {'A WR':>6} {'brkevn':>7} {'BEAT':>7}"
          f" | {'B exec$':>9} | {'EXIT B-A':>9}")
    print(f"  {'':18s} {'':>3} | {'held to resolution':>32s} | {'realized':>9} |"
          f" {'exit layer':>9}")
    out = []
    for lane, L in lanes.items():
        if L["n"] < args.min_n or L["an"] == 0:
            continue
        a = sum(L["a"])
        wr = L["aw"] / L["an"]
        px = sum(L["px"]) / len(L["px"]) if L["px"] else 0.5
        out.append((a - L["b"], lane, L["n"], a, wr, px, (wr - px) * 100, L["b"]))
    # most exit-layer DAMAGE first (b much worse than a)
    for delta_ab, lane, n, a, wr, px, beat, b in sorted(out, key=lambda x: (x[7] - x[3])):
        print(f"  {lane:18s} {n:3d} | {a:+9.2f} {wr*100:5.1f}% {px*100:6.1f}% {beat:+6.1f}"
              f" | {b:+9.2f} | {b - a:+9.2f}")
    _skipped = sum(L["skipped"] for L in lanes.values())
    ta = sum(x[3] for x in out); tb = sum(x[7] for x in out)
    print(f"  {'TOTAL':18s} {sum(x[2] for x in out):3d} | {ta:+9.2f} {'':13s} {'':6s}"
          f" | {tb:+9.2f} | {tb - ta:+9.2f}")
    print()
    if _skipped:
        print(f"  ⚠️ {_skipped} trade(s) in NEITHER bucket (no held counterfactual yet) — the")
        print(f"     comparison below is PAIRED on {sum(x[2] for x in out)} trades only.")
    print(f"  ENTRY (held to resolution)  {ta:+.2f}")
    print(f"  EXECUTED (realized)         {tb:+.2f}")
    print(f"  EXIT LAYER contribution     {tb - ta:+.2f}"
          f"   {'<- exits ADD money' if tb > ta else '<- exits are a LEAK'}")
    print("  ⛔ Never grade a lane's DIRECTION on column B. Column A is the direction test;")
    print("     B-A is the exit layer's, and they must be argued separately.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
