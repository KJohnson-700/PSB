#!/usr/bin/env python3
"""Tape-admission DEFER shadow — what would the realized adapter's admission delta
have done to the bleeding lanes, WITHOUT changing the live bot.

Operator directive (2026-08-03): the SOL 5m / sol-family / eth wrong-side shorts bled
(-35 sol, -21 eth, WR ~15%) shorting into an UP/flat tape. The refuted fix was a static
tape-DIRECTION gate. The correct fix is to DEFER to the realized adapter
(src/analysis/lane_tape_adapter.LaneTapeAdapter): a net-losing NEVER-GREEN lane gets a
POSITIVE tighten on its min_edge (fewer entries), and the SAME signal LOOSENS the lane
back when its fills start going green again — self-flipping, no static direction.

The consumption is already wired (sol_macro.py:6053 reads get_tape_admission_delta and
adds it to effective_min_edge). Only lane_tape_adapter.admission_mode is OFF, so the
persisted delta is always 0.0. This script measures — offline, observe-only — what would
change if admission_mode were 'live':

  1. Replay recent settled closes (data/calibration/trades.jsonl, the adapter's own
     hydrate source) through the REAL LaneTapeAdapter with admission ON.
  2. Interleave the live ENTRY events (session entries.jsonl) by timestamp, so each entry
     sees the exact walk-forward admission_delta that would have been active at that
     moment (only closes settled BEFORE it).
  3. For each entry: new_bar = base_min_edge + max(0, tighten_delta). SKIP if edge < new_bar.
  4. Join skip -> realized outcome (trade_id -> settled pnl) and tally $ saved / winners lost.

Nothing is written to live state. No bot coupling. Pure read + replay.

Usage:
  .venv/bin/python scripts/tape_admission_shadow.py [--session <dir>] [--hours 24] \
      [--tighten-max 0.05] [--loosen-max 0.03] [--lanes sol,eth]
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.analysis.lane_tape_adapter import LaneTapeAdapter, lane_key  # noqa: E402

CAL = os.path.join(ROOT, "data", "calibration")
TRADES = os.path.join(CAL, "trades.jsonl")
PAPER = os.path.join(ROOT, "data", "paper_trades")


def _f(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _ts(s):
    """ISO -> epoch, tolerant of Z / +00:00 / naive."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def latest_session():
    dirs = [os.path.join(PAPER, d) for d in os.listdir(PAPER)
            if d.startswith("test_") and os.path.isdir(os.path.join(PAPER, d))]
    return max(dirs, key=os.path.getmtime) if dirs else None


def load_closes(hours):
    """Settled closes from trades.jsonl (adapter hydrate source), chronological."""
    if not os.path.exists(TRADES):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    out = []
    for line in open(TRADES):
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = str(d.get("ts") or "")
        if ts and ts < cutoff:
            continue
        strat = d.get("strategy"); window = d.get("window") or d.get("window_size")
        side = d.get("action") or d.get("side"); pnl = d.get("pnl")
        if not strat or not window or not side or pnl is None:
            continue
        p = _f(pnl)
        if p is None:
            continue
        out.append({
            "ts": _ts(ts) or 0.0, "strategy": strat, "window": window, "side": side,
            "mfe_pct": _f(d.get("mfe_pct"), 0.0) or 0.0, "pnl": p,
            "trade_id": d.get("trade_id"), "win": d.get("win"),
        })
    out.sort(key=lambda r: r["ts"])
    return out


def load_entries(session):
    ef = os.path.join(session, "entries.jsonl")
    if not os.path.exists(ef):
        return []
    out = []
    for line in open(ef):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("event") != "ENTRY":
            continue
        ex = d.get("extra") or {}
        pol = ex.get("entry_policy") or {}
        base_min = _f(pol.get("min_edge"), None)
        edge = _f(d.get("edge"), _f(ex.get("edge")))
        window = d.get("window_size") or ex.get("window_size") or pol.get("window_size")
        side = d.get("action") or ex.get("entry_leg")
        out.append({
            "ts": _ts(d.get("timestamp") or ex.get("ts_utc")) or 0.0,
            "strategy": d.get("strategy"), "window": window, "side": side,
            "edge": edge, "base_min_edge": base_min,
            "trade_id": d.get("trade_id"),
        })
    out.sort(key=lambda r: r["ts"])
    return out


def realized_by_trade(closes):
    m = {}
    for c in closes:
        if c.get("trade_id") is not None:
            m[c["trade_id"]] = c["pnl"]
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--tighten-max", type=float, default=0.05)
    ap.add_argument("--loosen-max", type=float, default=0.03)
    ap.add_argument("--lanes", default="sol,eth,xrp,bnb,doge",
                    help="asset prefixes to report (comma-sep)")
    args = ap.parse_args()

    session = args.session or latest_session()
    if not session:
        print("no session found"); return
    closes = load_closes(args.hours)
    entries = load_entries(session)
    realized = realized_by_trade(closes)
    print(f"session={os.path.basename(session)}  closes={len(closes)}  entries={len(entries)}  "
          f"tighten_max={args.tighten_max} loosen_max={args.loosen_max}")

    # REAL adapter, admission ON (this is exactly what admission_mode:'live' would do).
    adapter = LaneTapeAdapter({
        "mode": "shadow",  # sizing untouched
        "admission_mode": "live",
        "admission_tighten_max": args.tighten_max,
        "admission_loosen_max": args.loosen_max,
    })

    # Interleave entries + closes by ts. On a close -> record. On an entry -> read the
    # walk-forward delta (reflects only closes settled so far) and decide skip.
    events = ([{"k": "close", "ts": c["ts"], "c": c} for c in closes] +
              [{"k": "entry", "ts": e["ts"], "e": e} for e in entries])
    events.sort(key=lambda x: (x["ts"], 0 if x["k"] == "close" else 1))

    prefixes = [p.strip() for p in args.lanes.split(",") if p.strip()]
    rows = []
    for ev in events:
        if ev["k"] == "close":
            c = ev["c"]
            adapter.record_close(c["strategy"], c["window"], c["side"],
                                 mfe_pct=c["mfe_pct"], pnl=c["pnl"])
            continue
        e = ev["e"]
        if not e["strategy"] or not e["window"] or not e["side"]:
            continue
        asset = str(e["strategy"]).replace("_macro", "")
        if prefixes and asset not in prefixes:
            continue
        delta = adapter.admission_delta(e["strategy"], e["window"], e["side"])
        base = e["base_min_edge"]
        edge = e["edge"]
        skip = None
        if base is not None and edge is not None:
            new_bar = base + max(0.0, delta)   # tighten raises bar; loosen never skips
            skip = edge < new_bar
        pnl = realized.get(e["trade_id"])
        rows.append({
            "lane": lane_key(e["strategy"], e["window"], e["side"]),
            "edge": edge, "base_min": base, "delta": delta,
            "skip": skip, "pnl": pnl,
        })

    # ---- report -----------------------------------------------------------
    from collections import defaultdict
    by_lane = defaultdict(lambda: {"n": 0, "skip": 0, "skip_pnl": 0.0,
                                    "skip_losers": 0, "skip_winners": 0,
                                    "kept_pnl": 0.0, "tot_pnl": 0.0, "maxdelta": 0.0})
    for r in rows:
        s = by_lane[r["lane"]]
        s["n"] += 1
        s["maxdelta"] = max(s["maxdelta"], r["delta"] or 0.0)
        if r["pnl"] is not None:
            s["tot_pnl"] += r["pnl"]
        if r["skip"]:
            s["skip"] += 1
            if r["pnl"] is not None:
                s["skip_pnl"] += r["pnl"]
                if r["pnl"] < 0:
                    s["skip_losers"] += 1
                elif r["pnl"] > 0:
                    s["skip_winners"] += 1
        elif r["pnl"] is not None:
            s["kept_pnl"] += r["pnl"]

    print("\n=== per-lane DEFER shadow (admission ON) ===")
    print(f"{'lane':22} {'n':>3} {'skip':>4} {'skipΔpnl':>9} {'-L':>3} {'+W':>3} "
          f"{'keptPnl':>8} {'totPnl':>8} {'maxTighten':>10}")
    tot_saved = tot_skipped_winner_pnl = 0.0
    for lane, s in sorted(by_lane.items(), key=lambda kv: kv[1]["skip_pnl"]):
        print(f"{lane:22} {s['n']:>3} {s['skip']:>4} {s['skip_pnl']:>+9.2f} "
              f"{s['skip_losers']:>3} {s['skip_winners']:>3} {s['kept_pnl']:>+8.2f} "
              f"{s['tot_pnl']:>+8.2f} {s['maxdelta']:>+10.3f}")
        tot_saved += -s["skip_pnl"] if s["skip_pnl"] < 0 else 0.0
        tot_skipped_winner_pnl += s["skip_pnl"] if s["skip_pnl"] > 0 else 0.0
    print(f"\nNET: skipping these entries removes {sum(s['skip_pnl'] for s in by_lane.values()):+.2f} "
          f"of realized pnl "
          f"(loser $ avoided vs winner $ forgone). Positive lane skipΔpnl = a WINNER would "
          f"have been skipped (bad); negative = losers avoided (good).")
    # honest caveat
    graded = [r for r in rows if r["skip"] is not None]
    print(f"\ncoverage: {len(graded)}/{len(rows)} entries had edge+min_edge to grade; "
          f"delta is REACTIVE (needs >=2 prior closes on the lane before it tightens).")


if __name__ == "__main__":
    main()
