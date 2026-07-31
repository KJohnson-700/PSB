#!/usr/bin/env python3
"""Per-lane stop-placement optimizer (operator 2026-07-31).

The bot's core mechanic: direction is ~57% right at resolution, so the lane is
profitable ONLY if avg_win > avg_loss. The stop is the knob that sets loss size
WITHOUT clipping winners. This finds, per lane, where the stop should sit so:
  - losers get cut SMALL (stop below their eventual resolution loss), and
  - winners still RUN (stop above their typical drawdown, so it doesn't guillotine
    a trade that would have resolved green).

Model, per settled trade, at candidate stop width S (fraction of entry):
  mae_pct <= -S      -> stop fires. pnl ~= -S*notional - round_trip_fees  (a LOSS)
  else               -> rides to resolution = held_pnl (settler truth)
                        held_pnl>0 -> WIN ; held_pnl<=0 -> LOSS
Then per (lane, S): n_win, n_loss, avg_win, avg_loss, PAYOFF=avg_win/avg_loss, net.

THE DISCRIMINABILITY TEST (why a lane can/can't be stopped):
  winner_MAE  = median drawdown of trades that resolve GREEN
  loser_MAE   = median drawdown of trades that resolve RED
  If winner_MAE ~ loser_MAE  -> NO stop separates them -> HOLD (only -50% cat).
  If winner_MAE much shallower -> a stop sits between them -> STOP at that gap.

Recommends per lane the S that maximizes net AND, where possible, gets payoff>1.
Read-only. Decisions = LIVE realized; this is the calibration read-out.

Usage:
  python scripts/lane_stop_optimizer.py                 # current session, all lanes
  python scripts/lane_stop_optimizer.py --since 2026-07-30 --min-n 4
  python scripts/lane_stop_optimizer.py --lane "BTC 15m DOWN"
"""
from __future__ import annotations

import argparse
import json
import statistics as stats
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAL = ROOT / "data" / "calibration"
PAPER = ROOT / "data" / "paper_trades"
TRADES = CAL / "trades.jsonl"
SETTLED = CAL / "trades_settled.jsonl"

_ASSET = {"bitcoin": "BTC", "sol_macro": "SOL", "eth_macro": "ETH", "xrp_macro": "XRP",
          "bnb_macro": "BNB", "doge_macro": "DOGE", "hype_macro": "HYPE"}
_SIDE = {"BUY_YES": "UP", "BUY_NO": "DOWN"}
GRID = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]


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


def _eval_stop(rs, S):
    """Return (n_win, n_loss, avg_win, avg_loss, payoff, net) at stop width S."""
    wins, losses = [], []
    clipped_winners = 0
    for r in rs:
        fees = r["entry_fee"] + r["exit_fee"]
        if r["mae"] is not None and r["mae"] <= -S:
            losses.append(S * r["notional"] + fees)  # magnitude
            if r["held_win"]:
                clipped_winners += 1
        else:
            hp = r["held_pnl"]
            if hp is None:
                continue
            (wins if hp > 0 else losses).append(hp if hp > 0 else -hp)
    aw = stats.mean(wins) if wins else 0.0
    al = stats.mean(losses) if losses else 0.0
    payoff = (aw / al) if al > 0 else float("inf")
    net = sum(wins) - sum(losses)
    return len(wins), len(losses), aw, al, payoff, net, clipped_winners


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--since", default=None)
    ap.add_argument("--min-n", type=int, default=4, help="min settled trades to recommend")
    ap.add_argument("--lane", default=None, help="filter one lane e.g. 'BTC 15m DOWN'")
    args = ap.parse_args()
    session = args.session or _default_session()

    settled = {r.get("trade_id"): r for r in _iter(SETTLED) if r.get("trade_id")}

    def keep(t):
        if args.since is not None:
            return str(t.get("ts", "")) >= args.since
        return t.get("session_id") == session

    lanes = defaultdict(list)
    for t in _iter(TRADES):
        if not keep(t):
            continue
        st = settled.get(t.get("trade_id"))
        if not st or _num(st.get("held_pnl")) is None:
            continue  # need the resolution counterfactual
        asset = _ASSET.get(str(t.get("strategy")), str(t.get("strategy")))
        side = _SIDE.get(str(t.get("action")).upper(), "?")
        key = f"{asset} {t.get('window')} {side}"
        if args.lane and key != args.lane:
            continue
        lanes[key].append({
            "notional": _num(t.get("notional")) or 0.0,
            "mae": _num(t.get("mae_pct")),
            "mfe": _num(t.get("mfe_pct")),
            "pnl": _num(t.get("pnl")),
            "entry_fee": _num(t.get("entry_fee_usdc")) or 0.0,
            "exit_fee": _num(t.get("fill_fee_usdc")) or 0.0,
            "held_win": bool(st.get("held_win")),
            "held_pnl": _num(st.get("held_pnl")),
        })

    scope = f"since {args.since}" if args.since else session
    print(f"=== PER-LANE STOP OPTIMIZER — {scope} — payoff lens (avg_win/avg_loss) ===\n")
    print("Direction is ~right at resolution; a lane wins iff avg_win > avg_loss.\n")

    # rank lanes by n
    recs = []
    for key in sorted(lanes, key=lambda k: -len(lanes[k])):
        rs = lanes[key]
        n = len(rs)
        w_mae = [abs(r["mae"]) for r in rs if r["held_win"] and r["mae"] is not None]
        l_mae = [abs(r["mae"]) for r in rs if not r["held_win"] and r["mae"] is not None]
        wmae = stats.median(w_mae) if w_mae else None
        lmae = stats.median(l_mae) if l_mae else None
        dirn = sum(1 for r in rs if r["held_win"])
        # sweep
        best = None
        table = []
        for S in GRID:
            nw, nl, aw, al, po, net, clip = _eval_stop(rs, S)
            table.append((S, nw, nl, aw, al, po, net, clip))
            if best is None or net > best[6]:
                best = (S, nw, nl, aw, al, po, net, clip)
        # hold row (no stop => everyone rides to resolution)
        nw, nl, aw, al, po, net, clip = _eval_stop(rs, 999)
        hold = ("HOLD", nw, nl, aw, al, po, net, clip)
        if net > best[6]:
            best = hold

        # discriminability verdict
        if wmae is not None and lmae is not None:
            sep = lmae - wmae  # positive => losers draw down deeper => a stop can sit between
            if sep >= 0.08:
                verdict = f"STOP separates (loserMAE {lmae*100:.0f}% > winnerMAE {wmae*100:.0f}%)"
            elif sep <= -0.03:
                verdict = f"winners draw down DEEPER — stop clips winners; HOLD"
            else:
                verdict = f"winner/loser MAE overlap ({wmae*100:.0f}% vs {lmae*100:.0f}%) — no clean stop; HOLD"
        else:
            verdict = "n/a (need both winners+losers)"

        flag = "" if n >= args.min_n else "  [THIN n<%d]" % args.min_n
        print(f"### {key}   n={n}  dir-right {dirn}/{n}{flag}")
        print(f"    winnerMAE(med)={wmae*100:.0f}%  loserMAE(med)={lmae*100:.0f}%  -> {verdict}"
              if (wmae is not None and lmae is not None) else f"    {verdict}")
        print(f"    {'stopS':>6}{'nW':>3}{'nL':>3}{'avgW':>7}{'avgL':>7}{'payoff':>7}{'net':>8}{'clipW':>6}")
        for S, nw, nl, aw, al, po, net, clip in table + [hold]:
            sS = f"{S:.2f}" if isinstance(S, float) else S
            pos = f"{po:.2f}" if po != float("inf") else "inf"
            star = "  <<" if (S, net) == (best[0], best[6]) else ""
            print(f"    {sS:>6}{nw:>3}{nl:>3}{aw:>7.2f}{al:>7.2f}{pos:>7}{net:>8.2f}{clip:>6}{star}")
        bS = f"{best[0]:.2f}" if isinstance(best[0], float) else best[0]
        bpo = f"{best[5]:.2f}" if best[5] != float("inf") else "inf"
        print(f"    -> BEST: stop {bS}  net {best[6]:+.2f}  payoff {bpo}  (clips {best[7]} winners)\n")
        if n >= args.min_n:
            recs.append((key, n, bS, best[6], bpo, verdict))

    print("=== RECOMMENDATIONS (n>=%d) — ranked by net gain available ===" % args.min_n)
    for key, n, bS, net, bpo, verdict in sorted(recs, key=lambda x: -x[3]):
        print(f"  {key:<18} n={n:<3} -> stop {bS:<5} net {net:+8.2f} payoff {bpo:<5} | {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
