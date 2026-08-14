#!/usr/bin/env python3
"""
favorite_derisk_shadow_report.py — read the favorite mark-path shadow and answer the ONE question:
at each candidate floor (0.50 / 0.55 / 0.60), how many WINNERS would a continuous cut wrongly kill,
vs how many LOSERS it would correctly cap — and net $ effect. Joins each market's DEEPEST observed dip
to its settled win/loss + realized pnl from trades.jsonl.

Decision rule: flip the continuous floor LIVE only if it cuts ~0 winners at that threshold.
Usage: python scripts/favorite_derisk_shadow_report.py
"""
import json, os, collections

CWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(CWD, "data/calibration/position_mark_path.jsonl")  # generalized poller output
TRADES = os.path.join(CWD, "data/calibration/trades.jsonl")
FAV_FLOOR = 0.85  # this report only looks at favorite-priced positions


def main():
    # deepest dip per TRADE (join on trade_id — favorite trades log market_id=None, so market_id is useless)
    min_mark = {}
    entry_of = {}
    win_of = {}
    for line in open(PATH):
        try:
            r = json.loads(line)
        except Exception:
            continue
        tid = str(r.get("trade_id"))
        mk = r.get("mark")
        ent = r.get("entry")
        if tid == "None" or mk is None or ent is None or float(ent) < FAV_FLOOR:
            continue  # favorites only
        if tid not in min_mark or mk < min_mark[tid]:
            min_mark[tid] = mk
        entry_of[tid] = ent
    # join to outcome (win/loss + pnl) from trades.jsonl by trade_id
    pnl_of = {}
    for line in open(TRADES):
        try:
            r = json.loads(line)
        except Exception:
            continue
        tid = str(r.get("trade_id"))
        if tid in min_mark:
            win_of[tid] = bool(r.get("win"))
            pnl_of[tid] = r.get("pnl", 0.0)

    joined = [m for m in min_mark if m in win_of]
    print("=== FAVORITE continuous-derisk SHADOW ===")
    print("favorite markets observed: %d | joined to a settled outcome: %d" % (len(min_mark), len(joined)))
    if not joined:
        print("(no settled favorites yet — let the shadow accumulate)")
        return
    print()
    print("threshold  would_cut  winners_cut  losers_capped   $saved(losers)  $lost(winners)  net")
    for thr in (0.50, 0.55, 0.60):
        cut = [m for m in joined if min_mark[m] <= thr]
        wc = [m for m in cut if win_of[m]]
        lc = [m for m in cut if not win_of[m]]
        # losers capped: cutting at thr caps loss at ~(entry-thr)/entry; $saved = actual_loss - capped_loss
        saved = 0.0
        for m in lc:
            e = entry_of.get(m) or 0.88
            actual = pnl_of[m]                       # negative
            capped = -abs((e - thr) / e) * abs(actual) / max(1e-6, abs((e - (0.0)) / e))  # rough scale
            saved += (capped - actual) if actual < capped else 0.0
        lost = 0.0
        for m in wc:
            e = entry_of.get(m) or 0.88
            # a winner cut at thr: instead of +win, realize ~ -(e-thr)/e * notional. Approx loss vs its actual win.
            lost += pnl_of[m] + abs((e - thr) / e) * 70.0  # crude: forfeited win + realized loss
        print("  %.2f       %3d        %3d          %3d            %7.2f         %7.2f      %7.2f" % (
            thr, len(cut), len(wc), len(lc), saved, -lost, saved - lost))
    print()
    print("WINNERS that dipped to <=0.55 (these would be WRONGLY cut — the risk):")
    any_bad = False
    for m in sorted(joined, key=lambda x: min_mark[x]):
        if win_of[m] and min_mark[m] <= 0.55:
            any_bad = True
            print("  market %s entry=%.2f deepest_dip=%.2f pnl=+%.2f (WON after dipping to %.2f)" % (
                m, entry_of.get(m) or 0, min_mark[m], pnl_of[m], min_mark[m]))
    if not any_bad:
        print("  NONE — no winner dipped to 0.55. A continuous 0.55 floor looks safe to flip live.")


if __name__ == "__main__":
    main()
