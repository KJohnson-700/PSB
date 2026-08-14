#!/usr/bin/env python3
"""
favorite_derisk_shadow.py — SHADOW for the continuous-0.55 favorite loss-cap (operator GO, 2026-08-10).

Question: if we cut a favorite the moment its OUR-SIDE mark walks down to <=0.55 (continuous, ALL windows,
not just the final-180s presettle), do we cut only true losers — or do we also cut winners that dip to
0.55 and recover? We can't answer from trades.jsonl (per-position intra-window marks aren't logged) and we
will NOT touch live_testing.py (restart-class). So this daemon reconstructs the path OUT-OF-BAND: it polls
the live session's positions.json (which carries current_price per open position, rewritten each cycle) and
appends one observation per favorite-priced open position per tick. The report joins each market's DEEPEST
dip to its settled win/loss, so we can see exactly how many WINNERS a 0.55 (or 0.50 / 0.60) floor would cut.

Zero behavior change — pure observation. Run:
  nohup .venv/bin/python scripts/favorite_derisk_shadow.py --loop --interval 20 >> data/logs/fav_derisk_shadow.log 2>&1 </dev/null & disown
"""
import json, os, time, glob, argparse

CWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER = os.path.join(CWD, "data/paper_trades")
# 2026-08-10 GENERALIZED: capture EVERY open position's mark path (was favorites-only). Feeds both the
# favorite-derisk 0.55 shadow AND the eth 5m/15m dip-recover analysis. Reports filter by entry/strategy.
OUT = os.path.join(CWD, "data/calibration/position_mark_path.jsonl")
FAV_FLOOR = 0.0  # capture all positions now (0.0 = no floor); reports do the filtering


def newest_session_dir():
    dirs = sorted(glob.glob(os.path.join(PAPER, "test_*")), key=os.path.getmtime)
    return dirs[-1] if dirs else None


def tick():
    sdir = newest_session_dir()
    if not sdir:
        return 0
    pf = os.path.join(sdir, "positions.json")
    try:
        d = json.load(open(pf))
    except Exception:
        return 0
    sess = os.path.basename(sdir)
    n = 0
    for _, v in d.items():
        if not isinstance(v, dict):
            continue
        entry = v.get("entry_price")
        mark = v.get("current_price")
        if entry is None or mark is None:
            continue
        if float(entry) < FAV_FLOOR:
            continue
        row = {
            "ts": None,  # stamped by writer below
            "session": sess,
            "trade_id": v.get("trade_id"),   # 2026-08-10 the reliable join key (favorite trades log market_id=None)
            "market_id": v.get("market_id"),
            "strategy": v.get("strategy"),
            "window": v.get("window_size"),
            "action": v.get("action"),
            "entry": round(float(entry), 4),
            "mark": round(float(mark), 4),
        }
        row["ts"] = time.time()
        try:
            with open(OUT, "a") as fh:
                fh.write(json.dumps(row) + "\n")
            n += 1
        except Exception:
            pass
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=20.0)
    args = ap.parse_args()
    if not args.loop:
        print("logged %d favorite observations" % tick())
        return
    print("favorite_derisk_shadow: polling every %.0fs -> %s" % (args.interval, OUT), flush=True)
    while True:
        try:
            tick()
        except Exception as e:
            print("tick error:", str(e)[:80], flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
