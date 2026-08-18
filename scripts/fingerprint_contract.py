#!/usr/bin/env python3
"""Fingerprint contract — the winning-geometry invariants as a standing check.

2026-08-17 (operator GO). The 102-session forensic (vault:
2026-08-17-FORENSIC-era-study-where-the-profitable-bot-went.md) showed every big
winner (+460, +362, +562 on n=795, +170) satisfied SIX invariants, while every
losing era violated at least one — and each Aug config step that broke one was
locally justified and invisible until the bleed. This check makes the winning
SHAPE a paged contract: any config change that silently breaks it turns a
probation row BROKEN instead of being discovered by a losing week.

The six invariants, with contract bounds (winning-era observed ranges in
parentheses — bounds are deliberately looser than the observed range so ordinary
variance does not page; only a SHAPE change does):

  stop_share   0.20..0.70   (winners ran 0.40..0.67)
  tp_share     0.20..0.60   (winners ran 0.31..0.49)
  res_share      < 0.25     (winners ran <= 0.10 — NOTHING held to resolution)
  loss_depth     > -0.65    (winners cut losses at -0.27..-0.42 of stake)
  payoff b       > 0.90     (winners ran 1.5..1.7; 0.9 is the alarm line, not the goal)
  avg_entry      < 0.55     (winners bought 0.42..0.49; the favorite detour ran
                             0.75..0.92 and lost -- this catches that class)

Window: trades AFTER the geometry-restore anchor (2026-08-17T22:28 UTC, commit
4e7e925), most recent WINDOW_N closes, spanning restarts by design — the whole
point is that the shape must survive restarts. Below MIN_N the verdict is
ACCRUING, never a violation (small-n noise must not page).

b is only scored with >= MIN_SIDE wins AND losses; res/tp/stop shares and entry
price are meaningful at MIN_N regardless.

Exit codes: 0 = OK or ACCRUING, 4 = CONTRACT VIOLATION (probation turns BROKEN).
"""

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_GLOB = os.path.join(ROOT, "data", "paper_trades", "test_*", "entries.jsonl")

ANCHOR = "2026-08-17T22:28:00"   # geometry restore (4e7e925); override with --since
WINDOW_N = 40
MIN_N = 25
MIN_SIDE = 8

BOUNDS = {
    "stop_share": (0.20, 0.70),
    "tp_share": (0.20, 0.60),
    "res_share": (None, 0.25),
    "loss_depth": (-0.65, None),   # avg pnl/stake of losers; more negative = worse
    "b": (0.90, None),
    "avg_entry": (None, 0.55),
}


def classify(reason):
    r = reason or ""
    if "stop" in r or "never_green" in r or "catastrophic" in r:
        return "stop"
    if "take_profit" in r or "giveback" in r:
        return "tp"
    if "expired" in r or "RESOLVED" in r:
        return "resolve"
    return "other"


def load_window(since, window_n):
    rows = []
    for f in sorted(glob.glob(TRADES_GLOB)):
        with open(f, errors="ignore") as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                if d.get("event") != "EXIT":
                    continue
                ts = str(d.get("timestamp") or "")
                if ts < since:
                    continue
                try:
                    e = float(d["entry_price"])
                    pnl = float(d["pnl"])
                    nt = float(d["size"]) * e
                except Exception:
                    continue
                rows.append((ts, e, pnl, nt, classify(d.get("reason"))))
    rows.sort()
    return rows[-window_n:]


def measure(rows):
    n = len(rows)
    wins = [r for r in rows if r[2] > 0]
    losses = [r for r in rows if r[2] <= 0]
    fam = {"stop": 0, "tp": 0, "resolve": 0, "other": 0}
    for r in rows:
        fam[r[4]] += 1
    m = {
        "n": n,
        "net": round(sum(r[2] for r in rows), 2),
        "wr": round(len(wins) / n, 3) if n else None,
        "stop_share": round(fam["stop"] / n, 3) if n else None,
        "tp_share": round(fam["tp"] / n, 3) if n else None,
        "res_share": round(fam["resolve"] / n, 3) if n else None,
        "avg_entry": round(sum(r[1] for r in rows) / n, 3) if n else None,
        "loss_depth": None,
        "b": None,
    }
    if losses:
        depths = [r[2] / r[3] for r in losses if r[3] > 0]
        if depths:
            m["loss_depth"] = round(sum(depths) / len(depths), 3)
    if len(wins) >= MIN_SIDE and len(losses) >= MIN_SIDE:
        aw = sum(r[2] for r in wins) / len(wins)
        al = sum(r[2] for r in losses) / len(losses)
        if al:
            m["b"] = round(abs(aw / al), 3)
    return m


def evaluate(m):
    """Return (verdict, violations). verdict in {ACCRUING, OK, VIOLATION}."""
    if m["n"] < MIN_N:
        return "ACCRUING", []
    v = []
    for key, (lo, hi) in BOUNDS.items():
        val = m.get(key)
        if val is None:
            continue  # b below MIN_SIDE, or no losses yet — not scoreable
        if lo is not None and val < lo:
            v.append(f"{key}={val} < {lo}")
        if hi is not None and val > hi:
            v.append(f"{key}={val} > {hi}")
    return ("VIOLATION" if v else "OK"), v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=ANCHOR)
    ap.add_argument("--window", type=int, default=WINDOW_N)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load_window(args.since, args.window)
    m = measure(rows)
    verdict, violations = evaluate(m)

    if args.json:
        print(json.dumps({"verdict": verdict, "violations": violations, **m}))
    else:
        print(f"=== FINGERPRINT CONTRACT (last {m['n']} closes since {args.since}) ===")
        print(f"  net {m['net']}  WR {m['wr']}  b {m['b']}")
        print(f"  stop {m['stop_share']}  tp {m['tp_share']}  res {m['res_share']}  "
              f"lossDepth {m['loss_depth']}  avgE {m['avg_entry']}")
        print(f"  verdict: {verdict}")
        for x in violations:
            print(f"    ⛔ {x}")
        if verdict == "ACCRUING":
            print(f"    ({m['n']}/{MIN_N} closes — contract arms at {MIN_N})")
    return 4 if verdict == "VIOLATION" else 0


if __name__ == "__main__":
    sys.exit(main())
