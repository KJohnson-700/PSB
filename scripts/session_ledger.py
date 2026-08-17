#!/usr/bin/env python3
"""session_ledger.py — one run view across restarts, and settle the positions a restart
abandoned so it cannot hide part of the run.

WHAT IS ACTUALLY BROKEN (measured 2026-08-17, not assumed)
──────────────────────────────────────────────────────────
The cross-session ledger already EXISTS and is not losing closed trades. Measured: EXIT
events in each `data/paper_trades/<session>/entries.jsonl` match rows in
`data/calibration/trades.jsonl` one-for-one, every session (25/25, 31/31, 51/51, ...). And
the ledger is MORE complete than the folders — 218 distinct session_ids in trades.jsonl vs
87 surviving folders, so folder-based review silently loses history.

So the defects are NOT "build a ledger". They are:

  1. ORPHANED OPEN POSITIONS. A restart abandons whatever is open. Those positions never
     close, never reach trades.jsonl, and their outcome is never recorded. Measured 36
     across 12 recent session folders; of 24 sampled, 24 were TRUE orphans (0 later closed).
     Their P&L is simply missing from every review.
  2. NO RUN CONCEPT. A reviewer must know which of 218 sessions belong to the current era.
     I got a direction answer wrong twice today from exactly this.
  3. EVERYTHING READS per-session `summary.json`, so a restart splits the review in two.

⛔ THE ORPHANS ARE RECOVERABLE, NOT JUST REPORTABLE. These are crypto up/down markets that
RESOLVE. `positions.json` keeps `market_id`, `action`, `entry_price` and `size`, so each
orphan can be settled against its REAL Polymarket resolution — the same live-realized class
`settle_stopped_trades.py` uses, and the same P&L convention (verified identical, not
re-derived: stake = size * entry_price, win pays stake*(1-entry)/entry, loss forfeits stake,
minus FEE_RATE on stake).

⛔⛔ EVERY RESTART STARTS A FRESH $500 PAPER BANKROLL. So the run total below is a
TRADE-ECONOMICS aggregate, NOT a bankroll curve, and it is NOT what the account would show.
Never present a cross-session sum as account growth.

⚠️ `positions.json` field `outcome` is the LEG WE HOLD (matches `entry_leg`), NOT the
resolution. Reading it as an outcome inverts every short. Resolution comes from the API.

READ-ONLY on bot data. Appends only to its own settled file. Never restarts anything.

USAGE
  scripts/session_ledger.py                          # run view, orphans listed not settled
  scripts/session_ledger.py --settle-orphans         # + resolve them against Polymarket
  scripts/session_ledger.py --since 2026-08-16T22:57:15
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER = os.path.join(REPO, "data", "paper_trades")
LEDGER = os.path.join(REPO, "data", "calibration", "trades.jsonl")
OUT = os.path.join(REPO, "data", "calibration", "orphaned_positions_settled.jsonl")

# identical to settle_stopped_trades.py — one fee model, not two
FEE_RATE = 0.0396
DEFAULT_ANCHOR = "2026-08-16T22:57:15"    # the cut-watchlist era anchor


def load_ledger():
    rows, ids = [], set()
    try:
        with open(LEDGER, errors="ignore") as fh:
            for line in fh:
                try:
                    t = json.loads(line)
                except ValueError:
                    continue
                rows.append(t)
                ids.add(str(t.get("trade_id")))
    except OSError:
        pass
    return rows, ids


def iter_positions(session_dir):
    """positions.json is a DICT keyed by something opaque; values are the records."""
    p = os.path.join(session_dir, "positions.json")
    if not os.path.isfile(p):
        return []
    try:
        j = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = j if isinstance(j, list) else list(j.values()) if isinstance(j, dict) else []
    return [x for x in items if isinstance(x, dict)]


def find_orphans(ledger_ids):
    out = []
    try:
        sessions = sorted(d for d in os.listdir(PAPER)
                          if os.path.isdir(os.path.join(PAPER, d)))
    except OSError:
        return out
    for s in sessions:
        for pos in iter_positions(os.path.join(PAPER, s)):
            tid = str(pos.get("trade_id") or "")
            if not tid or tid in ledger_ids:
                continue          # closed later — the file was just stale
            out.append((s, pos))
    return out


def already_settled():
    done = set()
    try:
        with open(OUT, errors="ignore") as fh:
            for line in fh:
                try:
                    done.add(str(json.loads(line).get("trade_id")))
                except ValueError:
                    continue
    except OSError:
        pass
    return done


def settle_orphans(orphans, throttle, limit):
    """Resolve each orphan against the REAL market outcome. Idempotent."""
    try:
        sys.path.insert(0, REPO)
        from src.analysis.ghost_calibration import fetch_resolution
    except Exception as e:
        print(f"  cannot import fetch_resolution: {e}")
        return [], Counter()

    done = already_settled()
    todo = [(s, p) for s, p in orphans if str(p.get("trade_id")) not in done]
    if limit:
        todo = todo[:limit]
    cache, summary, written = {}, Counter(), []
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as fh:
        for sess, pos in todo:
            mid = str(pos.get("market_id") or "")
            if not mid:
                summary["no_market_id"] += 1
                continue
            try:
                entry = float(pos.get("entry_price"))
                shares = float(pos.get("size"))
            except (TypeError, ValueError):
                summary["bad_fields"] += 1
                continue
            if not (0.0 < entry < 1.0) or shares <= 0:
                summary["bad_fields"] += 1
                continue
            oc = fetch_resolution(mid, cache)
            if throttle:
                time.sleep(throttle)
            if oc not in ("YES", "NO"):
                summary["unresolved"] += 1
                continue
            action = str(pos.get("action") or "")
            right = (action == "BUY_YES") == (oc == "YES")
            stake = shares * entry
            gross = stake * (1.0 - entry) / entry if right else -stake
            net = gross - stake * FEE_RATE
            rec = {
                "trade_id": str(pos.get("trade_id")), "market_id": mid,
                "orphaned_from_session": sess,
                "settled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "strategy": pos.get("strategy"), "window": pos.get("window_size"),
                "action": action, "opened_at": pos.get("opened_at"),
                "entry_price": entry, "shares": shares, "notional": round(stake, 4),
                "outcome": oc, "right_side": right,
                "settled_pnl_gross": round(gross, 4), "settled_pnl_net": round(net, 4),
                "mark_pnl_at_abandon": pos.get("pnl"),
                "source": "orphaned_position_settled",
            }
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            written.append(rec)
            summary["settled"] += 1
    summary["already_settled"] = len(done)
    summary["candidates"] = len(orphans)
    return written, summary


def load_settled_orphans():
    rows = []
    try:
        with open(OUT, errors="ignore") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def report_orphan_breakdown(settled, anchor):
    """Split recovered orphan P&L by ERA and by ENTRY BAND.

    ⛔ WHY THIS IS NOT ONE NUMBER. The first pass reported "-346 recovered" pooled. That
    total is dominated by pre-08-13 positions bought at 0.80-0.96, where breakeven needs
    ~89% and even 80% right-side loses badly. Quoting the pooled figure repeats the era
    -pooling error that has already faked findings twice in this codebase. The eras trade at
    different price bands, so they do not belong in the same sum.
    """
    print("  --- RECOVERED ORPHAN P&L, SPLIT (never quote the pooled total) ---")

    def block(label, rows):
        if not rows:
            return
        n = len(rows)
        right = sum(1 for r in rows if r.get("right_side"))
        net = sum(float(r.get("settled_pnl_net") or 0) for r in rows)
        mark = sum(float(r.get("mark_pnl_at_abandon") or 0) for r in rows)
        px = [float(r.get("entry_price") or 0.5) for r in rows]
        be = sum(px) / len(px) * 100
        print(f"    {label:34s} n={n:3d} right={right / n * 100:5.1f}% "
              f"(breakeven {be:4.1f}%) settled={net:+9.2f}  mark_was={mark:+8.2f}")

    eras = [("pre 08-13 (old exit regime)", lambda t: t < "2026-08-13"),
            ("08-13..08-16 (exits killed)", lambda t: "2026-08-13" <= t < anchor[:10]),
            (f"post-anchor {anchor[:10]}", lambda t: t >= anchor)]
    for label, f in eras:
        block(label, [r for r in settled if f(str(r.get("opened_at") or ""))])
    print()
    print("    by ENTRY BAND (breakeven WR at price p IS p):")
    for lo, hi in [(0.0, 0.45), (0.45, 0.55), (0.55, 0.80), (0.80, 1.01)]:
        block(f"  entry {lo:.2f}-{hi:.2f}",
              [r for r in settled if lo <= float(r.get("entry_price") or 0) < hi])
    print()
    tot = sum(float(r.get("settled_pnl_net") or 0) for r in settled)
    mk = sum(float(r.get("mark_pnl_at_abandon") or 0) for r in settled)
    print(f"    ⚠️ THE POINT: at abandon these marked {mk:+.2f}, so any review that glanced at")
    print(f"       positions.json assumed roughly that. Settled truth is {tot:+.2f} — a")
    print(f"       {tot - mk:+.2f} gap that no session summary anywhere reflected.")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=DEFAULT_ANCHOR, help="era anchor (opened_at >= this)")
    ap.add_argument("--settle-orphans", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--throttle", type=float, default=0.05)
    ap.add_argument("--json", help="write machine-readable output here")
    args = ap.parse_args()

    ledger, ledger_ids = load_ledger()
    orphans = find_orphans(ledger_ids)

    print("=== SESSION LEDGER ===")
    print(f"  ledger rows                  {len(ledger)}")
    print(f"  distinct session_ids in it   {len(set(str(t.get('session_id')) for t in ledger))}")
    try:
        print(f"  surviving session folders    "
              f"{len([d for d in os.listdir(PAPER) if os.path.isdir(os.path.join(PAPER, d))])}"
              f"   ⚠️ folders < ledger sessions: folder-based review loses history")
    except OSError:
        pass
    print(f"  ORPHANED open positions      {len(orphans)}  (entered, never closed, "
          f"absent from the ledger)")
    print()

    if args.settle_orphans and orphans:
        print("  --- settling orphans against REAL Polymarket resolutions ---")
        written, summ = settle_orphans(orphans, args.throttle, args.limit)
        print(f"  {dict(summ)}")
        print()

    settled = load_settled_orphans()
    rec_by_sess = defaultdict(float)
    for r in settled:
        rec_by_sess[str(r.get("orphaned_from_session"))] += float(r.get("settled_pnl_net") or 0)

    if settled:
        report_orphan_breakdown(settled, args.since)

    # ── run view, era-filtered, from the LEDGER (not summary.json) ────────────
    era = [t for t in ledger if str(t.get("opened_at") or "") >= args.since]
    by_sess = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
    for t in era:
        a = by_sess[str(t.get("session_id"))]
        a["n"] += 1
        a["w"] += 1 if t.get("win") else 0
        a["pnl"] += float(t.get("pnl") or 0)

    print(f"  --- RUN VIEW since {args.since} ---")
    print(f"  {'session':26s} {'n':>4} {'WR':>7} {'closed$':>10} {'orphan$':>9} "
          f"{'summary$':>10} {'delta':>8}")
    tot_n = tot_w = 0
    tot_pnl = tot_orph = 0.0
    for s in sorted(by_sess):
        a = by_sess[s]
        sp = os.path.join(PAPER, s, "summary.json")
        srep = None
        if os.path.isfile(sp):
            try:
                srep = float(json.load(open(sp)).get("realized_pnl"))
            except Exception:
                srep = None
        orph = rec_by_sess.get(s, 0.0)
        delta = (srep - a["pnl"]) if srep is not None else None
        tot_n += a["n"]; tot_w += a["w"]; tot_pnl += a["pnl"]; tot_orph += orph
        print(f"  {s:26s} {a['n']:4d} {a['w']/max(a['n'],1)*100:6.1f}% {a['pnl']:+10.2f} "
              f"{orph:+9.2f} {('%.2f' % srep) if srep is not None else '-':>10} "
              f"{('%+.2f' % delta) if delta is not None else '-':>8}")
    print(f"  {'TOTAL':26s} {tot_n:4d} {tot_w/max(tot_n,1)*100:6.1f}% {tot_pnl:+10.2f} "
          f"{tot_orph:+9.2f}")
    print()
    print(f"  closed P&L {tot_pnl:+.2f}  +  recovered orphan P&L {tot_orph:+.2f}  "
          f"=  {tot_pnl + tot_orph:+.2f} TRUE run economics")
    print("  ⛔ NOT a bankroll curve — every restart begins a FRESH $500 paper bankroll.")
    print("     This is trade economics across the era, not account growth.")
    if any(abs(v) > 0.01 for v in
           [(float(json.load(open(os.path.join(PAPER, s, 'summary.json'))).get('realized_pnl'))
             - by_sess[s]['pnl'])
            for s in by_sess
            if os.path.isfile(os.path.join(PAPER, s, 'summary.json'))]):
        print("  ⚠️ non-zero `delta` = summary.json disagrees with the ledger for that session;")
        print("     the LEDGER is the source of truth (summary.json can miss late closes).")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"orphans": [{"session": s, **{k: p.get(k) for k in
                                                     ("trade_id", "market_id", "strategy",
                                                      "window_size", "action", "entry_price",
                                                      "size", "opened_at")}}
                                   for s, p in orphans],
                       "run": {s: by_sess[s] for s in by_sess},
                       "recovered": rec_by_sess}, fh, indent=2, default=str)
        print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
