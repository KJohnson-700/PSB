#!/usr/bin/env python3
"""Execution-adjusted lane diagnostic (operator 2026-07-31).

Answers, PER LANE, the questions that separate a real edge from execution drag —
NOT by optimizing lanes together, but by joining every taken trade to its
resolution counterfactual:

  trades.jsonl          entry/exit/fill-quality/MFE/MAE/exec-adjusted pnl
  trades_settled.jsonl  what HOLDING to resolution would have done (held_win,
                        held_pnl, hold_minus_exit_pnl) -> the stop counterfactual

Per lane (asset/window/side) it reports:
  1. direction_right_at_resolution  = held_win rate
  2. stop_killed_green              = exits via stop where holding would have WON
                                      (held_win AND hold_minus_exit_pnl > 0)
  3. tp_left_mfe                     = trades with real MFE that still closed <= 0
  4. entry_edge_pos_after_costs      = exec_adjusted_pnl vs raw drag (fee+spread)
  5. correlated_dup_of_btc           = same window+side as a BTC trade within +/-Wmin

Read-only. Decisions = LIVE realized; this is the execution-calibration read-out.

Usage:
  python scripts/exec_lane_diagnostic.py                 # current session, all assets
  python scripts/exec_lane_diagnostic.py --assets BTC XRP --table
  python scripts/exec_lane_diagnostic.py --since 2026-07-30   # live-like window
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
CAL = ROOT / "data" / "calibration"
PAPER = ROOT / "data" / "paper_trades"
TRADES = CAL / "trades.jsonl"
SETTLED = CAL / "trades_settled.jsonl"

_ASSET = {"bitcoin": "BTC", "sol_macro": "SOL", "eth_macro": "ETH", "xrp_macro": "XRP",
          "bnb_macro": "BNB", "doge_macro": "DOGE", "hype_macro": "HYPE"}


def _iter(path: Path):
    if not path.exists():
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _side(a: Optional[str]) -> str:
    return {"BUY_YES": "UP", "BUY_NO": "DOWN"}.get(str(a or "").upper(), "?")


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) else None


def _default_session() -> str:
    s = sorted(PAPER.glob("test_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return s[0].name if s else ""


def _session_start(sess: str) -> Optional[str]:
    try:
        return json.loads((PAPER / sess / "summary.json").read_text()).get("started_at")
    except Exception:
        return None


def build(session: Optional[str], since: Optional[str], assets: Optional[set]) -> Dict[str, Any]:
    # index settled by trade_id
    settled = {r.get("trade_id"): r for r in _iter(SETTLED) if r.get("trade_id")}

    def keep(r):
        if since is not None:
            return str(r.get("ts", "")) >= since
        return r.get("session_id") == session

    trades = [r for r in _iter(TRADES) if keep(r)]
    if assets:
        trades = [r for r in trades if _ASSET.get(str(r.get("strategy")), "?") in assets]

    # per-trade enriched rows
    rows: List[dict] = []
    for t in trades:
        st = settled.get(t.get("trade_id"), {})
        fq = t.get("entry_paper_fill_quality") or {}
        asset = _ASSET.get(str(t.get("strategy")), str(t.get("strategy")))
        pnl = _num(t.get("pnl"))
        raw = _num(t.get("raw_signal_pnl"))
        exec_adj = _num(t.get("execution_adjusted_pnl"))
        rows.append({
            "asset": asset, "window": str(t.get("window") or "?"),
            "side": _side(t.get("action")),
            "lane_family": t.get("lane_family"),
            "entry_type": fq.get("paper_fill_model") or ("maker" if t.get("entry_is_maker") else "?"),
            "entry_bucket": t.get("entry_price_bucket"),
            "spread": _num(t.get("entry_spread")),
            "fee": _num(t.get("fill_fee_usdc")),
            "mfe": _num(t.get("mfe_pct")), "mae": _num(t.get("mae_pct")),
            "exit_reason": str(t.get("exit_reason") or "?"),
            "pnl": pnl,
            "exec_drag": (round(raw - exec_adj, 3) if (raw is not None and exec_adj is not None) else None),
            "stated_edge": _num(t.get("stated_edge")),
            # resolution counterfactual (from settled join)
            "held_win": st.get("held_win"),
            "held_pnl": _num(st.get("held_pnl")),
            "hold_minus_exit": _num(st.get("hold_minus_exit_pnl")),
            "settled": bool(st),
            "ts": t.get("ts"),
        })

    # BTC trades for correlation check (window+side+time)
    btc = [(r["window"], r["side"], r["ts"]) for r in rows if r["asset"] == "BTC"]

    def is_dup_of_btc(r, wmin=8.0):
        if r["asset"] == "BTC":
            return False
        for bw, bs, bts in btc:
            if bw == r["window"] and bs == r["side"] and r["ts"] and bts:
                # crude time proximity by ISO string minute prefix (same session, coarse)
                if r["ts"][:15] == bts[:15]:  # same yyyy-mm-ddThh:mm rounded to 10-min
                    return True
        return False

    for r in rows:
        r["dup_of_btc"] = is_dup_of_btc(r)

    # per-lane rollup
    lanes: Dict[tuple, List[dict]] = defaultdict(list)
    for r in rows:
        lanes[(r["asset"], r["window"], r["side"])].append(r)

    lane_report = []
    for key, rs in sorted(lanes.items()):
        n = len(rs)
        sett = [r for r in rs if r["settled"]]
        stops = [r for r in rs if "stop" in r["exit_reason"]]
        stop_killed_green = [r for r in stops if r.get("held_win") and (r.get("hold_minus_exit") or 0) > 0]
        tp_left_mfe = [r for r in rs if (r.get("mfe") or 0) >= 0.08 and (r.get("pnl") or 0) <= 0]
        pnls = [r["pnl"] for r in rs if r["pnl"] is not None]
        drags = [r["exec_drag"] for r in rs if r["exec_drag"] is not None]
        dirn = [r for r in sett if r.get("held_win") is not None]
        lane_report.append({
            "lane": f"{key[0]} {key[1]} {key[2]}",
            "n": n, "settled_n": len(sett),
            "pnl": round(sum(pnls), 2) if pnls else 0.0,
            "wr": round(sum(1 for p in pnls if p > 0) / n, 2) if n else None,
            "direction_right_at_resolution": (
                f"{sum(1 for r in dirn if r['held_win'])}/{len(dirn)}" if dirn else "n/a"),
            "stop_killed_green": len(stop_killed_green),
            "tp_left_mfe": len(tp_left_mfe),
            "exec_drag_total": round(sum(drags), 2) if drags else None,
            "dup_of_btc": sum(1 for r in rs if r.get("dup_of_btc")),
        })
    return {"rows": rows, "lanes": lane_report,
            "session": session, "since": since}


def _fmt(rep: Dict[str, Any], table: bool) -> str:
    L = [f"=== EXECUTION-ADJUSTED LANE DIAGNOSTIC — {rep['since'] and ('since '+rep['since']) or rep['session']} ==="]
    L.append("")
    L.append(f"{'lane':<20}{'n':>3}{'pnl':>8}{'wr':>5}  {'dir@resol':>9}  {'stopKgreen':>10}  {'tpLeftMFE':>9}  {'execDrag':>8}  {'dupBTC':>6}")
    for r in sorted(rep["lanes"], key=lambda z: z["pnl"]):
        L.append(f"{r['lane']:<20}{r['n']:>3}{r['pnl']:>8.2f}{(r['wr'] if r['wr'] is not None else 0):>5.2f}  "
                 f"{r['direction_right_at_resolution']:>9}  {r['stop_killed_green']:>10}  {r['tp_left_mfe']:>9}  "
                 f"{(r['exec_drag_total'] if r['exec_drag_total'] is not None else 0):>8.2f}  {r['dup_of_btc']:>6}")
    L.append("")
    L.append("cols: dir@resol = held_win/settled (direction right if held) · stopKgreen = stops that would've WON if held ·")
    L.append("      tpLeftMFE = trades that went >=+8% MFE but closed <=0 · execDrag = raw_signal_pnl - exec_adjusted_pnl ($) ·")
    L.append("      dupBTC = same window+side+~time as a BTC trade (correlated basket).")
    if table:
        L.append("")
        L.append("--- per-trade ---")
        L.append(f"{'asset':<5}{'win':<4}{'sd':<5}{'entry':<11}{'bkt':<10}{'spr':>5}{'mfe':>6}{'mae':>6}{'exit':<22}{'pnl':>7}{'held?':>6}{'h-e':>7}")
        for r in sorted(rep["rows"], key=lambda z: (z["asset"], z["ts"] or "")):
            hw = ("W" if r["held_win"] else "L") if r["held_win"] is not None else "-"
            L.append(f"{r['asset']:<5}{r['window']:<4}{r['side']:<5}{str(r['entry_type'])[:10]:<11}"
                     f"{str(r['entry_bucket'])[:9]:<10}{(r['spread'] or 0):>5.2f}"
                     f"{(r['mfe'] or 0)*100:>5.0f}%{(r['mae'] or 0)*100:>5.0f}%{r['exit_reason'][:21]:<22}"
                     f"{(r['pnl'] or 0):>7.2f}{hw:>6}{(r['hold_minus_exit'] if r['hold_minus_exit'] is not None else 0):>7.2f}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Execution-adjusted lane diagnostic")
    ap.add_argument("--session", default=None)
    ap.add_argument("--since", default=None, help="ISO ts lower bound (overrides session)")
    ap.add_argument("--assets", nargs="*", default=None, help="e.g. BTC XRP")
    ap.add_argument("--table", action="store_true", help="also print per-trade rows")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    session = args.session or _default_session()
    since = args.since
    if since is None and args.session is None and not args.__dict__.get("_no_default"):
        pass
    assets = set(a.upper() for a in args.assets) if args.assets else None
    rep = build(session if since is None else None, since, assets)
    print(json.dumps(rep["lanes"], indent=2) if args.json else _fmt(rep, args.table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
