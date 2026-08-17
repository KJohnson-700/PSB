#!/usr/bin/env python3
"""S7 — do the admission gates NEGATIVELY SELECT? (answered 2026-08-15: NO)

BOTH pools are graded on the SAME ground truth: REAL Polymarket resolution.
  REJECTED : rejected_candidates_settled.jsonl (self-contained: action + outcome), deduped by
             market_id, restricted to that file's own time coverage.
  ACCEPTED : real entries opened in the SAME window, graded RIGHT-SIDE (action vs resolution).

⛔ THE TRAP THIS SCRIPT EXISTS TO AVOID — read before changing it.
A first pass graded rejects on RESOLUTION but entries on P&L. In the covered window the exit layer
was still live, so right-side entries were being stopped into losses. That produced a FAKE +13.48pt
"negative selection" gap. Grading entries BOTH ways on the identical n=324 showed the truth:
    right-side 49.38%  vs  P&L 40.74%   = the EXIT layer cost 8.64 points of win rate
    38 of the 44 right-side-but-losing trades exited via hold_catastrophic_stop
Apples-to-apples the gap is +1.47pt with overlapping CIs => NOT CONFIRMED.
Never compare a counterfactual (never traded, no exits, no fees) against realized P&L.

Usage: .venv/bin/python scripts/score_gate_selection.py
"""
import collections
import glob
import json
import math

LO, HI = "2026-08-12T09:44", "2026-08-14T00:45"   # the settled file's own coverage
SETTLED = "data/calibration/rejected_candidates_settled.jsonl"
TRADES = "data/calibration/trades.jsonl"


def ci(k, n):
    if not n:
        return 0.0, 0.0
    p = k / n
    return p * 100, 1.96 * math.sqrt(max(p * (1 - p), 1e-12) / n) * 100


def load_outcomes_and_rejects():
    """market_id -> 'YES'/'NO', plus the deduped rejected pool graded on resolution."""
    out, rej, dup = {}, {}, 0
    for line in open(SETTLED, errors="ignore"):
        if '"outcome"' not in line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        mid, oc = str(r.get("market_id") or ""), r.get("outcome")
        if not mid or oc not in ("YES", "NO"):
            continue
        out.setdefault(mid, oc)
        act, ts = str(r.get("action") or ""), str(r.get("ts") or "")
        if act not in ("BUY_YES", "BUY_NO") or not (LO <= ts <= HI):
            continue
        if mid in rej:
            dup += 1
            continue
        rej[mid] = (
            1 if ((act == "BUY_YES") == (oc == "YES")) else 0,
            "%s|%s|%s" % (r.get("strategy"), r.get("window"), act),
            r.get("reason"),
        )
    return out, rej, dup


def load_entries(out):
    """Entries in the window, graded BOTH ways so the exit confound stays visible."""
    tr = {}
    for line in open(TRADES):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("trade_id") and r.get("pnl") is not None:
            tr[r["trade_id"]] = r
    acc = {}
    for path in glob.glob("data/paper_trades/*/entries.jsonl"):
        for line in open(path):
            try:
                e = json.loads(line)
            except Exception:
                continue
            t = tr.get(e.get("trade_id"))
            mid = str(e.get("market_id") or "")
            if not t or mid not in out:
                continue
            ts = str(t.get("opened_at") or t.get("ts") or "")
            if not (LO <= ts <= HI):
                continue
            act = str(t.get("action") or "")
            acc[mid] = (
                1 if ((act == "BUY_YES") == (out[mid] == "YES")) else 0,   # RIGHT SIDE
                1 if float(t.get("pnl") or 0) > 0 else 0,                   # P&L (confounded)
                "%s|%s|%s" % (t.get("strategy"), t.get("window"), act),
                t.get("exit_reason"),
            )
    return acc


def main():
    out, rej, dup = load_outcomes_and_rejects()
    acc = load_entries(out)
    for mid in set(acc) & set(rej):      # keep the pools disjoint
        rej.pop(mid, None)

    print("REJECTED: %d unique markets (%d duplicate rows collapsed)" % (len(rej), dup))
    print("ACCEPTED: %d unique markets\n" % len(acc))

    rn, rk = len(rej), sum(v[0] for v in rej.values())
    an, ak = len(acc), sum(v[0] for v in acc.values())
    ra, rh = ci(rk, rn)
    aa, ah = ci(ak, an)
    print("=== S7 — both pools on RIGHT SIDE (real resolution) ===")
    print("  REJECTED (thrown away) n=%5d  right %5.2f%% +/-%4.2f" % (rn, ra, rh))
    print("  ACCEPTED (taken)       n=%5d  right %5.2f%% +/-%4.2f" % (an, aa, ah))
    gap = ra - aa
    disjoint = (ra - rh) > (aa + ah)
    print("  GAP %+.2f pt | bar: >=3.0pt, n>=300, disjoint CIs" % gap)
    print("  n>=300? %s   CIs disjoint? %s" % ("YES" if an >= 300 else "NO", "YES" if disjoint else "NO"))
    print("  => %s\n" % ("CONFIRMED" if (gap >= 3.0 and an >= 300 and disjoint) else "NOT CONFIRMED"))

    pk = sum(v[1] for v in acc.values())
    pa, ph = ci(pk, an)
    print("=== THE CONFOUND, made explicit (same n) ===")
    print("  entries on RIGHT SIDE : %5.2f%% +/-%4.2f" % (aa, ah))
    print("  entries on P&L        : %5.2f%% +/-%4.2f" % (pa, ph))
    print("  EXIT-LAYER COST       : %+.2f pt" % (aa - pa))
    c = collections.Counter(v[3] for v in acc.values() if v[0] == 1 and v[1] == 0)
    print("  right-side but LOST, by exit reason:")
    for k, v in c.most_common(6):
        print("     %-28s %d" % (str(k)[:28], v))


if __name__ == "__main__":
    main()
