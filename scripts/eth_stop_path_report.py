#!/usr/bin/env python3
"""
eth_stop_path_report.py — should eth 5m/15m KEEP stops, or ride to resolution like sol/bnb? (operator Q 2026-08-10)

early_stop_windows:[1h] lets 5m/15m RIDE because sol/bnb winners dip-then-recover there (a mark stop would
cut them). But eth is a separate strategy — its 5m/15m losers ride straight to $0. If eth's winners DON'T
dip (they hold/rise) while its losers sink monotonically, then eth specifically SHOULD keep a 5m/15m stop
— it would cap losers without cutting winners. This joins each eth 5m/15m position's intra-window mark path
(from the generalized position_mark_path.jsonl poller) to its settled win/loss and asks:
  - do eth WINNERS dip below candidate stop floors (0.40/0.35/0.30)? (if yes, a stop cuts them = keep riding)
  - do eth LOSERS sink below them? (if yes, a stop caps them = keep stops on eth 5m/15m)
The clean case for eth stops: ~0 winners dip below the floor, most losers do.

Usage: python scripts/eth_stop_path_report.py
"""
import json, os, collections

CWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(CWD, "data/calibration/position_mark_path.jsonl")
TRADES = os.path.join(CWD, "data/calibration/trades.jsonl")


def main():
    min_mark, entry_of, win_of, pnl_of, meta = {}, {}, {}, {}, {}
    for line in open(PATH):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("strategy") != "eth_macro" or str(r.get("window")) not in ("5m", "15m"):
            continue
        tid = str(r.get("trade_id"))
        mk = r.get("mark")
        if tid == "None" or mk is None:
            continue
        if tid not in min_mark or mk < min_mark[tid]:
            min_mark[tid] = mk
        entry_of[tid] = r.get("entry")
        meta[tid] = (r.get("window"), r.get("action"))
    for line in open(TRADES):
        try:
            r = json.loads(line)
        except Exception:
            continue
        tid = str(r.get("trade_id"))
        if tid in min_mark:
            win_of[tid] = bool(r.get("win"))
            pnl_of[tid] = r.get("pnl", 0.0)

    joined = [t for t in min_mark if t in win_of]
    print("=== ETH 5m/15m STOP-PATH SHADOW ===")
    print("eth 5m/15m positions observed: %d | joined to settled outcome: %d" % (len(min_mark), len(joined)))
    if not joined:
        print("(none settled yet — let it accumulate; eth must open 5m/15m positions)")
        return
    W = [t for t in joined if win_of[t]]
    L = [t for t in joined if not win_of[t]]
    print("  winners=%d losers=%d" % (len(W), len(L)))
    print()
    print("floor   winners_dipping_below (WOULD be cut)   losers_sinking_below (WOULD be capped)")
    for floor in (0.40, 0.35, 0.30, 0.25):
        wc = [t for t in W if min_mark[t] <= floor]
        lc = [t for t in L if min_mark[t] <= floor]
        print("  %.2f      %2d / %2d winners                        %2d / %2d losers" % (
            floor, len(wc), len(W), len(lc), len(L)))
    print()
    print("WINNERS and their deepest dip (do eth winners dip low, like sol — or hold?):")
    for t in sorted(W, key=lambda x: min_mark[x])[:10]:
        print("  %s %s entry=%.2f deepest_dip=%.2f pnl=+%.2f" % (
            meta[t][0], meta[t][1], entry_of.get(t) or 0, min_mark[t], pnl_of[t]))
    print("LOSERS and their deepest dip (how early could a stop have capped them?):")
    for t in sorted(L, key=lambda x: min_mark[x])[:10]:
        print("  %s %s entry=%.2f deepest_dip=%.2f pnl=%.2f" % (
            meta[t][0], meta[t][1], entry_of.get(t) or 0, min_mark[t], pnl_of[t]))
    print()
    print("READ: if winners rarely dip below a floor but losers do → eth 5m/15m SHOULD keep a stop there")
    print("      (unlike sol/bnb). If winners dip as deep as losers → eth also can't stop, ride is correct.")


if __name__ == "__main__":
    main()
