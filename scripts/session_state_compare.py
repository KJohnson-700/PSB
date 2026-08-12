#!/usr/bin/env python3
"""Rank paper-trading sessions by the metrics that actually matter — net, PAYOFF
GEOMETRY (avg win vs avg loss), max drawdown, win-rate, frequency — so the best
*positive* session states surface on their own instead of being guessed at.

Operator's standing ask (2026-08-11): "you act like you do not know how to go back
and look at the different configurations and see which one was the best one, the more
profitable one, the one with the less drawdown ... you should be presenting the
different session states to me." This is that tool.

Usage:
  python scripts/session_state_compare.py                 # top sessions, last 30 by mtime
  python scripts/session_state_compare.py --min-closed 8  # only sessions with >=8 closed
  python scripts/session_state_compare.py --all           # every session
  python scripts/session_state_compare.py --sort payoff   # net|payoff|dd|wr|n
"""
import json, os, glob, argparse, datetime as dt

def load_jsonl(p):
    out = []
    try:
        with open(p, errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try: out.append(json.loads(line))
                    except Exception: pass
    except FileNotFoundError:
        pass
    return out

def session_metrics(d):
    ents = load_jsonl(os.path.join(d, "entries.jsonl"))
    snaps = load_jsonl(os.path.join(d, "snapshots.jsonl"))
    # realized per-trade pnl from EXIT events
    exits = [e for e in ents if e.get("event") == "EXIT"]
    pnls = []
    for e in exits:
        v = e.get("pnl")
        try:
            v = float(v)
            pnls.append(v)
        except Exception:
            pass
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    net = sum(pnls)
    wr = (len(wins) / n) if n else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0   # negative
    payoff = (avg_win / abs(avg_loss)) if losses else float("inf") if wins else 0.0
    # max drawdown from equity series
    eq = []
    for s in snaps:
        v = s.get("equity") if s.get("equity") is not None else s.get("bankroll")
        try: eq.append(float(v))
        except Exception: pass
    peak = -1e18; maxdd = 0.0
    for v in eq:
        if v > peak: peak = v
        if peak > 0: maxdd = max(maxdd, peak - v)
    # duration
    dur_min = 0.0
    if snaps:
        try:
            t0 = snaps[0].get("timestamp"); t1 = snaps[-1].get("timestamp")
            f = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            dur_min = (f(t1) - f(t0)).total_seconds() / 60.0
        except Exception:
            pass
    n_entries = sum(1 for e in ents if e.get("event") == "ENTRY")
    return dict(n=n, net=net, wr=wr, avg_win=avg_win, avg_loss=avg_loss,
                payoff=payoff, maxdd=maxdd, dur_min=dur_min, n_entries=n_entries,
                trades_per_hr=(n_entries / (dur_min/60.0)) if dur_min > 1 else 0.0)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-closed", type=int, default=5)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--sort", default="net", choices=["net","payoff","dd","wr","n"])
    a = ap.parse_args()

    dirs = sorted(glob.glob("data/paper_trades/*/"), key=os.path.getmtime, reverse=True)
    rows = []
    for d in dirs:
        m = session_metrics(d)
        if m["n"] < a.min_closed:
            continue
        m["sess"] = os.path.basename(d.rstrip("/"))
        rows.append(m)
        if not a.all and len(rows) >= a.limit:
            break

    keyf = {"net": lambda r: -r["net"], "payoff": lambda r: -min(r["payoff"], 99),
            "dd": lambda r: r["maxdd"], "wr": lambda r: -r["wr"], "n": lambda r: -r["n"]}[a.sort]
    rows.sort(key=keyf)

    print(f"{'session':26} {'net$':>8} {'WR':>5} {'avgW':>6} {'avgL':>7} {'payoff':>6} {'maxDD':>7} {'n':>4} {'ent':>4} {'t/hr':>5} {'dur_m':>6}")
    print("-" * 104)
    for r in rows:
        po = "inf" if r["payoff"] == float("inf") else f"{r['payoff']:.2f}"
        print(f"{r['sess']:26} {r['net']:8.2f} {r['wr']*100:4.0f}% {r['avg_win']:6.2f} {r['avg_loss']:7.2f} {po:>6} {r['maxdd']:7.2f} {r['n']:4d} {r['n_entries']:4d} {r['trades_per_hr']:5.1f} {r['dur_min']:6.0f}")

    pos = [r for r in rows if r["net"] > 0 and r["n"] >= a.min_closed]
    if pos:
        best_net = max(pos, key=lambda r: r["net"])
        best_geo = max(pos, key=lambda r: (min(r["payoff"], 9) * r["net"]) / (r["maxdd"] + 1))
        print("\nBEST BY NET:      ", best_net["sess"], f"net ${best_net['net']:.2f} payoff {best_geo['payoff']:.2f} DD ${best_net['maxdd']:.2f}")
        print("BEST GEOMETRY:    ", best_geo["sess"], f"net ${best_geo['net']:.2f} payoff {min(best_geo['payoff'],9):.2f} avgW ${best_geo['avg_win']:.2f} avgL ${best_geo['avg_loss']:.2f} DD ${best_geo['maxdd']:.2f}")

if __name__ == "__main__":
    main()
