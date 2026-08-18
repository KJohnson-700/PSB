#!/usr/bin/env python3
"""est_prob calibration — Step 2 Phase A (2026-08-17, operator GO).

Measures what the entry model's stated probability actually DELIVERS on live
resolutions, fits a per-family shrink

    est_cal = price + k * (claimed - price),  k in [0, 1]

and scores three predictors by Brier: the MARKET PRICE alone, the RAW model
claim, and the k-CALIBRATED claim. Placement decision (operator-approved):
calibration is for SIZING ONLY — admission keeps the raw claim, so this tool
changes zero bot behavior. It is the measurement + state layer that Step 3's
Kelly flip (`p = est_cal`) will consume.

Ground rules carried in from the session's hard lessons:
  * LIVE RESOLUTIONS ONLY. exit_reason in RESOLUTION_REASONS grades directly;
    every other exit (fixed TP, MFE cut, catastrophic) grades via the
    held-to-resolution counterfactual in exit_layer_settled.jsonl, which is
    OWNED by scripts/entry_exit_split.py --settle. This tool never settles —
    one writer per file, always.
  * ERA-ANCHORED. Default anchor is the current-build boundary
    2026-08-16T22:57:15Z (session test_20260816_155632). Pooling the pre-fix
    era is how "book beats the quote +3.2 sigma" got faked.
  * SIDE-AWARE. stated_est_prob is prob-of-UP; the claim being graded is the
    LEG's claim: est for BUY_YES, 1-est for BUY_NO. entry_price is the leg
    price, so breakeven prob == entry_price on both sides.
  * WALK-FORWARD, not backfit. If a previous state file exists, its FROZEN k
    is scored on trades opened AFTER its fit_through before refitting. That
    out-of-sample record (state["walkforward"]) is what graduation gate C
    reads — in-sample Brier cannot graduate anything.
  * k floored at 0: an inverted model (doge|5m claims 0.70, delivers 42%)
    collapses to the market price; we never ANTI-follow a signal.

Graduation gate (pre-registered, Phase C): calibrated walk-forward Brier must
beat BOTH raw and market-price-only on >= 150 new resolution-graded trades.
If calibrated cannot beat the price, the model contributes nothing to sizing
and Kelly's p should be the price itself — a valid outcome, not a failure.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL = os.path.join(ROOT, "data", "calibration")
TRADES = os.path.join(CAL, "trades.jsonl")
HELD = os.path.join(CAL, "exit_layer_settled.jsonl")
STATE = os.path.join(CAL, "est_prob_calibration.json")

RESOLUTION_REASONS = {"updown_expired", "RESOLVED:YES (real)", "RESOLVED:NO (real)"}
DEFAULT_ANCHOR = "2026-08-16T22:57:15"
PRIOR_N = 12          # credibility mass pulling a group's k toward its parent
MIN_LEAF_N = 4        # below this a leaf just inherits its parent verbatim
BUCKETS = (0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80)


def _load_held():
    """trade_id -> held_right_side from the entry_exit_split settle cache."""
    out = {}
    if not os.path.isfile(HELD):
        return out
    with open(HELD, errors="ignore") as fh:
        for ln in fh:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("held_right_side") is not None:
                out[str(r.get("trade_id"))] = bool(r["held_right_side"])
    return out


def load_graded(since):
    """One row per closed trade in the era: (group keys, price, claimed, won).

    Coverage accounting: a trade with no grade (non-resolution exit and no
    settled counterfactual yet) is COUNTED and reported — low coverage reads
    as "we cannot tell", never as a smaller clean sample.
    """
    held = _load_held()
    rows, ungraded, skipped_fields = [], 0, 0
    with open(TRADES, errors="ignore") as fh:
        for ln in fh:
            try:
                t = json.loads(ln)
            except Exception:
                continue
            if str(t.get("opened_at") or "") < since or t.get("pnl") is None:
                continue
            reason = str(t.get("exit_reason"))
            if reason in RESOLUTION_REASONS:
                won = float(t["pnl"]) > 0
            elif str(t.get("trade_id")) in held:
                won = held[str(t.get("trade_id"))]
            else:
                ungraded += 1
                continue
            est = t.get("stated_est_prob")
            price = t.get("entry_price")
            action = str(t.get("action") or "")
            strat = str(t.get("strategy") or "")
            window = str(t.get("window") or "")
            if est is None or price is None or action not in ("BUY_YES", "BUY_NO") \
                    or not strat or not window:
                skipped_fields += 1
                continue
            est, price = float(est), float(price)
            claimed = est if action == "BUY_YES" else 1.0 - est
            rows.append({
                "opened_at": str(t.get("opened_at")),
                "strategy": strat, "window": window, "action": action,
                "price": price, "claimed": claimed, "won": bool(won),
            })
    return rows, ungraded, skipped_fields


def _fit_k_raw(rows):
    """Regression through the origin of (won - price) on (claimed - price)."""
    num = den = 0.0
    for r in rows:
        x = r["claimed"] - r["price"]
        num += ((1.0 if r["won"] else 0.0) - r["price"]) * x
        den += x * x
    if den <= 0.0:
        return None
    return max(0.0, min(1.0, num / den))


def fit_hierarchy(rows):
    """global -> strategy -> strategy|window -> strategy|window|action,
    each level credibility-shrunk toward its parent (PRIOR_N pseudo-trades)."""
    def level_key(r, lvl):
        if lvl == 0:
            return "GLOBAL"
        if lvl == 1:
            return r["strategy"]
        if lvl == 2:
            return f"{r['strategy']}|{r['window']}"
        return f"{r['strategy']}|{r['window']}|{r['action']}"

    groups = {}
    kg = _fit_k_raw(rows)
    kg = 0.0 if kg is None else kg
    groups["GLOBAL"] = {"k": kg, "n": len(rows), "level": 0, "parent": None}
    for lvl in (1, 2, 3):
        by = defaultdict(list)
        for r in rows:
            by[level_key(r, lvl)].append(r)
        for key, rs in by.items():
            parent_key = "GLOBAL" if lvl == 1 else key.rsplit("|", 1)[0]
            pk = groups.get(parent_key, groups["GLOBAL"])["k"]
            raw = _fit_k_raw(rs)
            n = len(rs)
            if raw is None or n < MIN_LEAF_N:
                k = pk
            else:
                k = (n * raw + PRIOR_N * pk) / (n + PRIOR_N)
            groups[key] = {"k": round(max(0.0, min(1.0, k)), 4), "n": n,
                           "level": lvl, "parent": parent_key,
                           "k_raw": None if raw is None else round(raw, 4)}
    return groups


def leaf_k(groups, r):
    for key in (f"{r['strategy']}|{r['window']}|{r['action']}",
                f"{r['strategy']}|{r['window']}", r["strategy"], "GLOBAL"):
        if key in groups:
            return groups[key]["k"]
    return 0.0


def brier(rows, predictor):
    if not rows:
        return None
    s = 0.0
    for r in rows:
        p = predictor(r)
        s += (p - (1.0 if r["won"] else 0.0)) ** 2
    return s / len(rows)


def score_triple(rows, groups):
    return {
        "n": len(rows),
        "market": round(brier(rows, lambda r: r["price"]), 5),
        "raw": round(brier(rows, lambda r: r["claimed"]), 5),
        "calibrated": round(brier(
            rows, lambda r: r["price"] + leaf_k(groups, r) * (r["claimed"] - r["price"])
        ), 5),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=DEFAULT_ANCHOR,
                    help="era anchor (opened_at >=); default = current-build boundary")
    ap.add_argument("--write-state", action="store_true",
                    help="refit and write data/calibration/est_prob_calibration.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rows, ungraded, bad = load_graded(args.since)
    total = len(rows) + ungraded
    cov = (len(rows) / total * 100.0) if total else 100.0
    say = (lambda *a: None) if args.quiet else print

    say(f"=== EST_PROB CALIBRATION (era >= {args.since}) ===")
    say(f"  graded {len(rows)}/{total} ({cov:.0f}% coverage; {ungraded} awaiting "
        f"entry_exit_split --settle, {bad} missing fields)")
    if cov < 80.0 and total:
        say("  ⚠️ coverage <80% — fits below are provisional; run the settle first")

    # ── claimed-vs-delivered buckets (the honesty table) ────────────────────
    bk = defaultdict(lambda: [0, 0])
    for r in rows:
        b = min(BUCKETS, key=lambda x: abs(x - r["claimed"]))
        bk[b][0] += 1
        bk[b][1] += 1 if r["won"] else 0
    say("\n  claimed -> delivered (resolution basis):")
    for b in sorted(bk):
        n, w = bk[b]
        say(f"    ~{b:.2f}  n={n:>4}  delivered {w / n * 100:5.1f}%   "
            f"({'+' if w / n >= b else ''}{(w / n - b) * 100:.1f} pts)")

    # ── walk-forward: score the PREVIOUS frozen k on trades it never saw ────
    walk = None
    prev = None
    if os.path.isfile(STATE):
        try:
            with open(STATE) as fh:
                prev = json.load(fh)
        except Exception:
            prev = None
    if prev and prev.get("fit_through"):
        oos = [r for r in rows if r["opened_at"] > prev["fit_through"]]
        if oos:
            walk = score_triple(oos, prev.get("groups", {}))
            walk["fit_through"] = prev["fit_through"]
            say(f"\n  WALK-FORWARD (frozen k of {prev.get('generated_at', '?')[:16]} "
                f"on {len(oos)} unseen trades):")
            say(f"    Brier  market {walk['market']}  raw {walk['raw']}  "
                f"calibrated {walk['calibrated']}   (lower = better)")

    # ── refit on the full era ───────────────────────────────────────────────
    groups = fit_hierarchy(rows)
    ins = score_triple(rows, groups)
    say(f"\n  IN-SAMPLE Brier (n={ins['n']}): market {ins['market']}  "
        f"raw {ins['raw']}  calibrated {ins['calibrated']}")
    say("  (in-sample cannot graduate anything — gate C reads walkforward only)")

    say("\n  fitted k by family (leaf level, n>=%d):" % MIN_LEAF_N)
    leaves = [(k, g) for k, g in groups.items() if g["level"] == 3]
    for key, g in sorted(leaves, key=lambda kg: kg[1]["k"]):
        say(f"    {key:<28} k={g['k']:.3f}  (raw {g.get('k_raw')}, n={g['n']})")
    say(f"    GLOBAL k={groups['GLOBAL']['k']:.3f} (n={groups['GLOBAL']['n']})")

    if args.write_state:
        fit_through = max((r["opened_at"] for r in rows), default=args.since)
        hist = (prev or {}).get("walkforward_history", [])
        if walk:
            hist = (hist + [walk])[-40:]
        state = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "since": args.since,
            "fit_through": fit_through,
            "coverage_pct": round(cov, 1),
            "graded_n": len(rows),
            "ungraded_n": ungraded,
            "groups": groups,
            "brier_insample": ins,
            "walkforward": walk,
            "walkforward_history": hist,
            "prior_n": PRIOR_N,
            "consumer": "SIZING ONLY — admission keeps raw est_prob (operator-approved scope)",
        }
        tmp = STATE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, STATE)
        say(f"\n  state written: {STATE}")

    # exit code for the daemon: 0 ok, 3 = coverage too low to trust
    return 0 if cov >= 50.0 or not total else 3


if __name__ == "__main__":
    sys.exit(main())
