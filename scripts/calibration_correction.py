#!/usr/bin/env python3
"""Calibration CORRECTION — shadow prototype (does NOT touch live sizing).

The quant's est_prob is miscalibrated (overconfident on high-vol lanes, underrates
some winners). true-Kelly sizes on win_prob = est_prob-derived, so it oversizes the
overconfident losers. This builds a per-lane correction map (shrinks/boosts est_prob
toward realized resolution WR, damped by sample size) and SHADOWS its effect:
retrospectively re-sizes every settled trade with corrected win_prob and reports
whether it concentrates capital on lanes that actually win.

Artifacts:
  - writes  data/calibration/est_prob_correction_map.json  (the map the bot WOULD read)
  - prints  per-lane correction + shadow size-weighted WR (raw vs corrected)

Read-only vs the bot. Correction is applied ONLY in this script, never live.
Usage: python scripts/calibration_correction.py [--min-n 8] [--k 25] [--session SUB]
"""
import json, glob, os, argparse
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_jsonl(p):
    out = []
    try:
        for ln in open(p):
            ln = ln.strip()
            if ln:
                try: out.append(json.loads(ln))
                except Exception: pass
    except FileNotFoundError: pass
    return out

def clamp(x, lo, hi): return max(lo, min(hi, x))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=8, help="min settled trades to correct a lane")
    ap.add_argument("--k", type=float, default=25.0, help="shrinkage pseudocount (higher=more damping)")
    ap.add_argument("--session", default=None)
    ap.add_argument("--frac", type=float, default=0.27, help="kelly fraction for shadow sizing")
    args = ap.parse_args()

    # est_prob per trade_id from all entries
    est = {}
    for f in glob.glob(os.path.join(REPO, "data/paper_trades/*/entries.jsonl")):
        for r in load_jsonl(f):
            if str(r.get("event")) != "ENTRY": continue
            tid = r.get("trade_id"); ep = r.get("est_prob", (r.get("extra") or {}).get("est_prob"))
            if tid is None or ep is None: continue
            est[tid] = {"p_up": float(ep), "action": r.get("action"),
                        "strategy": (r.get("strategy") or "").replace("_macro", ""),
                        "window": (r.get("extra") or {}).get("window_size")}

    # join to settled outcomes
    settled = load_jsonl(os.path.join(REPO, "data/calibration/trades_settled.jsonl"))
    lane = defaultdict(lambda: {"n": 0, "pred": 0.0, "wins": 0, "trades": []})
    for s in settled:
        if args.session and args.session not in str(s.get("session_id", "")): continue
        e = est.get(s.get("trade_id"))
        if not e: continue
        act = e["action"] or s.get("action") or ""
        p_side = e["p_up"] if act == "BUY_YES" else (1.0 - e["p_up"])
        hw = s.get("held_win")
        won = (1 if hw else 0) if hw is not None else (1 if float(s.get("actual_pnl") or 0) > 0 else 0)
        strat = e["strategy"] or (s.get("strategy") or "?").replace("_macro", "")
        key = f"{strat}|{e['window'] or s.get('window') or '?'}|{'up' if act=='BUY_YES' else 'down'}"
        d = lane[key]
        d["n"] += 1; d["pred"] += p_side; d["wins"] += won
        our_price = float(s.get("entry_price") or p_side)
        notional = abs(float(s.get("size") or 0.0)) * (our_price if our_price > 0 else 1.0)
        d["trades"].append({"p_side": p_side, "won": won, "our_price": our_price,
                            "pnl": float(s.get("actual_pnl") or 0), "notional": notional})

    # build correction map: delta = shrink * (predWR - realWR); shrink = n/(n+k)
    cmap = {}
    for k, d in lane.items():
        if d["n"] < args.min_n: continue
        pbar = d["pred"] / d["n"]; real = d["wins"] / d["n"]
        gap = pbar - real
        shrink = d["n"] / (d["n"] + args.k)
        delta = shrink * gap
        cmap[k] = {"n": d["n"], "pred_wr": round(pbar, 4), "real_wr": round(real, 4),
                   "gap": round(gap, 4), "shrink": round(shrink, 3),
                   "delta_p_side": round(delta, 4)}

    out_path = os.path.join(REPO, "data/calibration/est_prob_correction_map.json")
    json.dump({"note": "SHADOW ONLY — not read by live bot yet. corrected_p_side = clamp(raw_p_side - delta_p_side, .02,.98)",
               "min_n": args.min_n, "k": args.k, "lanes": cmap},
              open(out_path, "w"), indent=2)

    # ---- SHADOW: re-size every settled trade raw vs corrected; size-weighted WR ----
    def kelly(p, c, frac):
        c = clamp(c, 0.02, 0.98)
        if p <= c: return 0.0
        b = (1 - c) / c
        fk = ((b * p) - (1 - p)) / b
        return max(0.0, min(25.0, 500.0 * fk * frac))  # $25 cap proxy (full tier)

    raw_sz = raw_win = cor_sz = cor_win = 0.0
    raw_pnl = cor_pnl = 0.0
    per = defaultdict(lambda: {"raw": 0.0, "cor": 0.0})
    for k, d in lane.items():
        delta = cmap.get(k, {}).get("delta_p_side", 0.0)
        for t in d["trades"]:
            p = t["p_side"]; c = t["our_price"]; won = t["won"]
            rs = kelly(p, c, args.frac)
            cs = kelly(clamp(p - delta, 0.02, 0.98), c, args.frac)
            raw_sz += rs; raw_win += rs * won
            cor_sz += cs; cor_win += cs * won
            per[k]["raw"] += rs; per[k]["cor"] += cs
            # PnL-weighted: scale each trade's REALIZED pnl by size scheme.
            # pnl_per_$ = actual_pnl / actual_notional (contracts*price); apply to each scheme's $.
            notional = abs(t.get("notional") or 0.0)
            ppd = (t["pnl"] / notional) if notional > 1e-9 else 0.0
            raw_pnl += rs * ppd; cor_pnl += cs * ppd

    print("=== PER-LANE CORRECTION (shadow map written to data/calibration/est_prob_correction_map.json) ===")
    hdr = f"{'lane':<20}{'n':>4}{'predWR':>8}{'realWR':>8}{'gap':>7}{'delta':>7}{'$raw':>8}{'$corr':>8}{'shift':>8}"
    print(hdr); print('-'*len(hdr))
    for k in sorted(per, key=lambda x: -abs(cmap.get(x, {}).get('gap', 0))):
        cm = cmap.get(k)
        if not cm: continue
        rw = per[k]["raw"]; co = per[k]["cor"]; sh = co - rw
        print(f"{k:<20}{cm['n']:>4}{cm['pred_wr']:>8.1%}{cm['real_wr']:>8.1%}{cm['gap']:>+7.1%}{-cm['delta_p_side']:>+7.1%}{rw:>8.0f}{co:>8.0f}{sh:>+8.0f}")

    print("\n=== SHADOW AGGREGATE (does correction concentrate $ on real winners?) ===")
    print(f"total $ sized   raw={raw_sz:8.0f}   corrected={cor_sz:8.0f}   ({cor_sz-raw_sz:+.0f})")
    print(f"size-weighted resolution WR   raw={raw_win/raw_sz:.1%}   corrected={cor_win/cor_sz:.1%}"
          f"   ({(cor_win/cor_sz)-(raw_win/raw_sz):+.1%})")
    print(f"PnL-weighted (realized, incl EXITS)  raw=${raw_pnl:+.0f}  corrected=${cor_pnl:+.0f}"
          f"   ({cor_pnl-raw_pnl:+.0f})")
    print(f"return on $ deployed          raw={raw_pnl/raw_sz:+.3f}/$   corrected={cor_pnl/cor_sz:+.3f}/$")
    print("\nInterpret: WR uses resolution; PnL uses ACTUAL realized (captures the exit edge).")
    print("If PnL-weighted DROPS, the correction over-penalizes exit-edge lanes — do NOT apply as-is.")
    print("delta<0 = lane sized DOWN (was overconfident).")

if __name__ == "__main__":
    main()
