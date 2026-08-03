#!/usr/bin/env python3
"""Lane expectancy + small-sample-safe ranking (Script B).

The recurring failure mode is chasing noise in thin lanes: a 5/6 lane (83% WR)
outranking a 55/100 lane (55% WR) when the first is almost certainly luck. This
script ranks lanes the disciplined way:

  * raw WR + dollar expectancy per lane
  * WILSON lower confidence bound on WR  -> rank on THIS, not the point estimate
  * EMPIRICAL-BAYES shrunk WR (beta-binomial prior fit across all lanes) so thin
    lanes are pulled toward the global rate until they earn their extremity

Expectancy is in dollars (avg_win, avg_loss from realized PnL), because a
high-WR lane with payoff asymmetry (loss >> win) is still a loser.

LIVE REALIZED only (data/calibration/trades.jsonl). Pure stdlib + math, read-only.

Usage:
  python3 scripts/lane_expectancy.py                    # last 8 sessions
  python3 scripts/lane_expectancy.py --sessions 12 --min-n 5
  python3 scripts/lane_expectancy.py --by rsi_bucket    # segment within lanes
  python3 scripts/lane_expectancy.py --all --json
"""
from __future__ import annotations
import argparse, json, math, sys
from collections import defaultdict
from pathlib import Path

TRADES = Path(__file__).resolve().parent.parent / "data" / "calibration" / "trades.jsonl"
Z = 1.96  # 95% Wilson


def _f(r, k):
    v = r.get(k)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def load_trades(args):
    rows = []
    try:
        with open(TRADES) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        print(f"[expectancy] no trades log at {TRADES}", file=sys.stderr)
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


def wilson(w, n, z=Z):
    """Wilson score interval for a binomial proportion. Returns (lower, upper)."""
    if n == 0:
        return (0.0, 1.0)
    phat = w / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def fit_beta_prior(lane_stats):
    """Empirical-Bayes: method-of-moments Beta(a,b) prior from per-lane rates,
    weighted by n. Falls back to a weak uniform prior if variance is degenerate."""
    pts = [(s["wins"] / s["n"], s["n"]) for s in lane_stats if s["n"] > 0]
    if len(pts) < 3:
        return (1.0, 1.0)
    wsum = sum(n for _, n in pts)
    m = sum(p * n for p, n in pts) / wsum
    v = sum(n * (p - m) ** 2 for p, n in pts) / wsum
    if v <= 1e-9 or m <= 0 or m >= 1:
        return (1.0, 1.0)
    kappa = m * (1 - m) / v - 1.0
    if kappa <= 0:
        return (1.0, 1.0)
    a = m * kappa
    b = (1 - m) * kappa
    return (max(a, 1e-3), max(b, 1e-3))


def build(rows, by=None):
    lanes = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0,
                                 "win_sum": 0.0, "loss_sum": 0.0, "losses": 0})
    for r in rows:
        key = f"{r.get('strategy','?')}|{r.get('window','?')}|{r.get('action','?')}"
        if by:
            key += f"|{by}={r.get(by,'?')}"
        pnl = _f(r, "pnl") or 0.0
        L = lanes[key]
        L["n"] += 1; L["pnl"] += pnl
        if pnl > 0:
            L["wins"] += 1; L["win_sum"] += pnl
        else:
            L["losses"] += 1; L["loss_sum"] += -pnl
    return lanes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=8)
    ap.add_argument("--session", type=str, default=None)
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--min-n", type=int, default=1, help="hide lanes below this n")
    ap.add_argument("--by", type=str, default=None,
                    help="also segment within lanes by this field (e.g. rsi_bucket, edge_bucket)")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_trades(args)
    if not rows:
        print("[expectancy] no trades after filter"); return
    lanes = build(rows, by=args.by)
    stats = list(lanes.values())
    a0, b0 = fit_beta_prior(stats)

    out = []
    for key, L in lanes.items():
        if L["n"] < args.min_n:
            continue
        n, w = L["n"], L["wins"]
        wr = w / n
        lo, hi = wilson(w, n)
        eb = (w + a0) / (n + a0 + b0)                       # shrunk WR
        avg_win = L["win_sum"] / w if w else 0.0
        avg_loss = L["loss_sum"] / L["losses"] if L["losses"] else 0.0
        exp = wr * avg_win - (1 - wr) * avg_loss            # $ expectancy / trade
        payoff = (avg_win / avg_loss) if avg_loss else float("inf")
        out.append({
            "lane": key, "n": n, "wr": round(wr, 3), "wr_lo": round(lo, 3),
            "wr_hi": round(hi, 3), "wr_eb": round(eb, 3), "pnl": round(L["pnl"], 2),
            "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
            "payoff": round(payoff, 2) if payoff != float("inf") else None,
            "exp_per_trade": round(exp, 3),
        })
    # rank by Wilson lower bound (small-n-safe), then expectancy
    out.sort(key=lambda d: (-d["wr_lo"], -d["exp_per_trade"]))

    if args.json:
        print(json.dumps({"beta_prior": {"a": round(a0, 3), "b": round(b0, 3),
                          "prior_wr": round(a0 / (a0 + b0), 3)}, "lanes": out}, indent=2))
        return

    scope = (f"session={args.session}" if args.session
             else "all-history" if args.all else f"last {args.sessions} sessions")
    seg = f"  segmented by {args.by}" if args.by else ""
    print(f"\nLANE EXPECTANCY  ·  {scope}{seg}  ·  {len(rows)} trades")
    print(f"  EB beta-prior: a={a0:.2f} b={b0:.2f}  global WR={a0/(a0+b0):.3f}"
          f"   (thin lanes shrink toward this)")
    print(f"\n  {'lane':34s} {'n':>4} {'WR':>5} {'WRlo':>5} {'WR_eb':>6} "
          f"{'exp$':>7} {'payoff':>6} {'pnl':>8}")
    print("  " + "-" * 84)
    for d in out[:args.top]:
        po = f"{d['payoff']:.2f}" if d["payoff"] is not None else "inf"
        print(f"  {d['lane']:34s} {d['n']:>4} {d['wr']:>5.2f} {d['wr_lo']:>5.2f} "
              f"{d['wr_eb']:>6.2f} {d['exp_per_trade']:>+7.2f} {po:>6} {d['pnl']:>+8.2f}")
    print("\n  ranked by Wilson lower bound (WRlo): a thin high-WR lane can't top a")
    print("  proven one until its lower bound clears. WR_eb = shrunk toward global.\n")
    if len(out) > args.top:
        print(f"  ({len(out)-args.top} more lanes; --top to show, --min-n to prune)\n")


if __name__ == "__main__":
    main()
