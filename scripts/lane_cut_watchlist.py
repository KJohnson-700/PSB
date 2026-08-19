#!/usr/bin/env python3
"""LANE CUT WATCHLIST — operator rule 2026-08-16: "if that continues and you dont see a fix
then we cut."

Encodes that rule so it cannot be forgotten or argued with later. Each watched lane carries a
DECISION-n and a CUT CONDITION agreed up front; the lane is cut only when it reaches its own n
in the POST-FIX era and is STILL failing. No cut before n. No reprieve after n.

⛔ THE ERA ANCHOR IS THE WHOLE POINT. Two eth fixes shipped 2026-08-16 (BTC true-Kelly wrong-side
prob; eth nonpositive_edge ordering). Grading these lanes on trades taken BEFORE that would
condemn them for a defect that has since been fixed — the exact "pooled table poisoned by a dead
era" trap. So the CUT verdict reads ONLY post-anchor trades; the pre-anchor record is printed
beside it as context and is NEVER the decision.

⛔ RANK ON BEAT, NOT net$ AND NOT `b`. Breakeven WR for a contract bought at price p and held to
resolution is exactly p, so BEAT = WR% - mean_entry% is the only scoreboard that means anything.
A lane can be net-negative with positive BEAT (that is a SIZING/cost problem, not a direction
problem — do NOT cut it, fix the sizing) and net-positive with negative BEAT (a payoff trap).

Usage:
  .venv/bin/python scripts/lane_cut_watchlist.py            # report
  .venv/bin/python scripts/lane_cut_watchlist.py --json     # machine output
  .venv/bin/python scripts/lane_cut_watchlist.py --set-anchor   # stamp the era (restart time)
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES = os.path.join(_REPO, "data/calibration/trades.jsonl")
ANCHOR = os.path.join(_REPO, "data/calibration/lane_cut_watchlist_anchor.json")

# Baseline measured POST-EXIT-KILL (08-14+), pre-fix, session test_20260815_223521 and earlier.
# Kept literal so drift is visible: if a lane's post-fix numbers look nothing like these, say so.
WATCH = {
    "eth_macro|1h|BUY_YES": {
        "verdict": "CUT CANDIDATE",
        "baseline": "n=12 WR 33.3% entry 53.2% BEAT -19.92 net -28.67 (-2.39/t)",
        "cut_n": 25,
        "rule": "BEAT < -3.0",
        "test": lambda s: s["beat"] < -3.0,
        "why": "worst eth lane by a mile; loses on DIRECTION, not on costs",
    },
    "eth_macro|5m|BUY_NO": {
        "verdict": "WATCH",
        "baseline": "n=25 WR 52.0% entry 51.7% BEAT +0.32 net -21.24 (-0.85/t)",
        "cut_n": 45,
        "rule": "$/trade < 0 AND BEAT < +1.0",
        "test": lambda s: s["per_trade"] < 0 and s["beat"] < 1.0,
        "why": "BEAT ~0 but bleeds money => costs/sizing, not side. Cut only if BEAT never lifts.",
    },
    "eth_macro|5m|BUY_YES": {
        "verdict": "WATCH",
        "baseline": "n=13 WR 53.8% entry 52.3% BEAT +1.54 net -17.00 (-1.31/t)",
        "cut_n": 30,
        "rule": "$/trade < 0 AND BEAT < +1.0",
        "test": lambda s: s["per_trade"] < 0 and s["beat"] < 1.0,
        "why": "same shape as 5m|BUY_NO — positive BEAT, negative money.",
    },
    "eth_macro|15m|BUY_NO": {
        "verdict": "PROTECTED WINNER — DO NOT CUT",
        "baseline": "n=41 WR 61.0% entry 52.0% BEAT +8.93 net +90.76 (+2.21/t)",
        "cut_n": 60,
        "rule": "ALERT ONLY if BEAT < +2.0 (never auto-cut)",
        "test": lambda s: False,
        "alert": lambda s: s["beat"] < 2.0,
        "why": "carries eth. Never tweak a winner — this row exists to catch DEGRADATION.",
    },
    # ── added 2026-08-16 after the full 7-strategy sweep of the post-exit-kill era ──
    "bnb_macro|1h|BUY_YES": {
        "verdict": "CUT CANDIDATE — CHRONIC, SECOND ERA",
        "baseline": "n=5 WR 0.0% entry 52.0% BEAT -52.00 net -43.20 (-8.64/t)",
        "cut_n": 20,
        "rule": "BEAT < -3.0",
        "test": lambda s: s["beat"] < -3.0,
        "why": "0-for-5 and the worst $/trade in the book. NOT new: named a chronic bleeder in "
               "the psb-monitor skill in a PRIOR era (wr 0) and it has come back the same way. "
               "A second independent era of the same failure is the strongest cut case here.",
    },
    "xrp_macro|5m|BUY_NO": {
        "verdict": "WATCH",
        "baseline": "n=13 WR 46.2% entry 53.1% BEAT -6.92 +/-14.37 net -34.77 (-2.67/t)",
        "cut_n": 35,
        "rule": "$/trade < 0 AND BEAT < +1.0",
        "test": lambda s: s["per_trade"] < 0 and s["beat"] < 1.0,
        "why": "worst non-eth alt lane by money. BEAT -6.92 but 1SE is 14.37 — the negative is "
               "NOT yet distinguishable from noise, so it accrues rather than cuts.",
    },
    "doge_macro|5m|BUY_YES": {
        "verdict": "WATCH",
        "baseline": "n=2 WR 0.0% entry 51.0% BEAT -51.00 net -24.94 (-12.47/t)",
        "cut_n": 20,
        "rule": "$/trade < 0 AND BEAT < +1.0",
        "test": lambda s: s["per_trade"] < 0 and s["beat"] < 1.0,
        "why": "worst $/trade in the book at n=2. Two trades prove NOTHING — this row exists so "
               "it cannot quietly keep bleeding unmeasured.",
    },
    "bitcoin|1h|BUY_YES": {
        "verdict": "PRE-ARMED — chronic bleeder, ZERO data this era",
        "baseline": "prior era: 41 LANE_BLEED CRITs, WR 25-33%, -$18 (psb-monitor skill)",
        "cut_n": 25,
        "rule": "BEAT < -3.0",
        "test": lambda s: s["beat"] < -3.0,
        "why": "BTC took ZERO entries all era (the true-Kelly wrong-side bug), so this lane is "
               "UNMEASURED, not healthy. Armed BEFORE the fix refills it so it is watched from "
               "trade 1. ⚠ ITS OLD DIAGNOSIS IS DEAD: the skill says the entries were fine and "
               "the EXITS gapped through — but exits were KILLED 08-13/14, so hold-to-resolution "
               "makes this a genuinely NEW test, not a rerun.",
    },
    "xrp_macro|15m|BUY_YES": {
        "verdict": "PROTECTED WINNER — DO NOT CUT",
        "baseline": "n=51 WR 56.9% entry 51.3% BEAT +5.53 net +43.90 (+0.86/t)",
        "cut_n": 70,
        "rule": "ALERT ONLY if BEAT < +1.0 (never auto-cut)",
        "test": lambda s: False,
        "alert": lambda s: s["beat"] < 1.0,
        "why": "second-biggest earner and the deepest sample in the book. Degradation watch only.",
    },
    "sol_macro|1h|BUY_YES": {
        "verdict": "PROTECTED EARNER — watch the mechanism, do NOT cut",
        "baseline": "n=33 WR 51.5% entry 52.5% BEAT -0.94 net +58.15 (+1.76/t)",
        "cut_n": 50,
        "rule": "ALERT ONLY if $/trade < 0 (never auto-cut)",
        "test": lambda s: False,
        "alert": lambda s: s["per_trade"] < 0,
        "why": "ODD ONE OUT: BEAT ~0 yet +$58.15. It makes money from size/price distribution, "
               "NOT from picking sides — the mirror image of the eth 5m lanes. Fragile by "
               "construction, so alert the moment the money turns, but never cut a +$58 lane.",
    },
}


def _anchor():
    try:
        with open(ANCHOR) as f:
            return json.load(f).get("anchor_ts") or ""
    except Exception:
        return ""


def set_anchor(at: str = "", why: str = ""):
    """Stamp the era boundary. Decisions read POST-anchor trades ONLY.

    ⚠️ Prefer --at <session started_at> over the default "now". Stamping at "now" silently
    DISCARDS trades already taken on the build you are trying to measure — on 2026-08-16 the
    live session had 3 entries in flight, and a now-stamp would have thrown them away.
    """
    ts = (at or datetime.now(timezone.utc).isoformat()).strip()
    prev = _anchor()
    with open(ANCHOR, "w") as f:
        json.dump({
            "anchor_ts": ts,
            "why": why or "era boundary re-stamped; decisions read post-anchor trades only",
            "previous_anchor_ts": prev or None,
        }, f, indent=2)
    print("anchor set: %s%s" % (ts, ("   (was %s)" % prev) if prev else ""))


def _stats(rows):
    n = len(rows)
    if not n:
        return None
    wins = sum(1 for r in rows if float(r["pnl"]) > 0)
    wr = 100.0 * wins / n
    ep = 100.0 * sum(float(r["entry_price"]) for r in rows) / n
    pnl = sum(float(r["pnl"]) for r in rows)
    b = [(1.0 if float(r["pnl"]) > 0 else 0.0) - float(r["entry_price"]) for r in rows]
    m = sum(b) / n
    var = sum((x - m) ** 2 for x in b) / max(n - 1, 1)
    return {"n": n, "wr": wr, "entry": ep, "beat": wr - ep, "net": pnl,
            "per_trade": pnl / n, "se": 100.0 * math.sqrt(var / n)}


def load(anchor_ts):
    pre, post = {}, {}
    for line in open(TRADES, errors="ignore"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("pnl") is None or r.get("shadow_mode") or not r.get("entry_price"):
            continue
        key = "%s|%s|%s" % (r.get("strategy"), r.get("window"), r.get("action"))
        if key not in WATCH:
            continue
        ts = str(r.get("opened_at") or r.get("ts") or "")
        # pre-anchor context window: post-exit-kill era only, never the dead pre-08-14 eras
        if anchor_ts and ts >= anchor_ts:
            post.setdefault(key, []).append(r)
        elif ts[:10] >= "2026-08-14":
            pre.setdefault(key, []).append(r)
    return pre, post


def evaluate():
    anchor_ts = _anchor()
    pre, post = load(anchor_ts)
    # 2026-08-18: a lane the operator already PAUSED must not keep paging "CUT NOW" —
    # the eth|5m|BUY_YES row stayed red for an hour after the cut was made. Map the
    # watchlist's strategy|window|SIDE keys onto lane_management.states (up/down form)
    # and mark satisfied conditions CUT-DONE.
    _paused = set()
    try:
        import yaml as _yaml
        _cfg = _yaml.safe_load(open(os.path.join(_REPO, "config/settings.yaml")))
        for _k, _v in (((_cfg.get("lane_management") or {}).get("states")) or {}).items():
            if str(_v).lower() == "paused":
                _paused.add(str(_k))
    except Exception:
        _paused = set()

    def _is_paused(lane_key):
        try:
            strat, window, side = lane_key.split("|")
            ud = "up" if side.upper() == "BUY_YES" else "down"
            return f"{strat}|{window}|{ud}" in _paused
        except ValueError:
            return False

    out = []
    for lane, spec in WATCH.items():
        s = _stats(post.get(lane, []))
        p = _stats(pre.get(lane, []))
        n = s["n"] if s else 0
        if _is_paused(lane):
            state = "CUT-DONE"
            note = "lane is PAUSED in lane_management.states — condition acted on"
            out.append({"lane": lane, "state": state, "note": note, "verdict": spec["verdict"],
                        "rule": spec["rule"], "cut_n": spec["cut_n"], "why": spec["why"],
                        "baseline": spec["baseline"], "post": s, "pre": p})
            continue
        if not s:
            state, note = "NO DATA YET", "0 post-fix trades"
        elif n < spec["cut_n"]:
            state = "ACCRUING"
            note = "n=%d/%d — no cut before n" % (n, spec["cut_n"])
        elif spec["test"](s):
            state = "CUT NOW"
            note = "n=%d >= %d and STILL failing (%s)" % (n, spec["cut_n"], spec["rule"])
        else:
            state = "CLEARED"
            note = "n=%d >= %d and rule NOT met — keep" % (n, spec["cut_n"])
        # ⛔ DEGRADATION ALERTS NEED THEIR OWN MINIMUM n. Without this a protected winner throws
        # a 🔴 on its FIRST losing trade (n=1 => BEAT -52), which is noise, and a pager that cries
        # wolf is worse than no pager. The CUT path is already gated by cut_n; this gates the
        # ALERT path the same way.
        alert_n = spec.get("alert_n", max(12, spec["cut_n"] // 4))
        if (s and state != "CUT NOW" and spec.get("alert")
                and n >= alert_n and spec["alert"](s)):
            state = "DEGRADED"
            note = "protected lane slipped at n=%d (>=%d): BEAT %+.2f +/-%.2f" % (
                n, alert_n, s["beat"], s["se"])
        elif s and state != "CUT NOW" and spec.get("alert") and n < alert_n:
            note += " | alert needs n>=%d" % alert_n
        out.append({"lane": lane, "state": state, "note": note, "verdict": spec["verdict"],
                    "rule": spec["rule"], "cut_n": spec["cut_n"], "why": spec["why"],
                    "baseline": spec["baseline"], "post": s, "pre": p})
    return anchor_ts, out


def main():
    if "--set-anchor" in sys.argv:
        at = why = ""
        if "--at" in sys.argv:
            at = sys.argv[sys.argv.index("--at") + 1]
        if "--why" in sys.argv:
            why = sys.argv[sys.argv.index("--why") + 1]
        set_anchor(at, why)
        return
    anchor_ts, rows = evaluate()
    if "--json" in sys.argv:
        print(json.dumps({"anchor_ts": anchor_ts, "lanes": rows}, default=str))
        return
    print("=== LANE CUT WATCHLIST — 'if that continues and you dont see a fix then we cut' ===")
    print("post-fix era anchor: %s" % (anchor_ts or "NOT SET (run --set-anchor at restart)"))
    print("decision reads POST-ANCHOR ONLY; pre-anchor shown as context, never as the verdict.\n")
    mark = {"CUT NOW": "🔴", "DEGRADED": "🔴", "ACCRUING": "🟡",
            "NO DATA YET": "⬜", "CLEARED": "✅"}
    for r in sorted(rows, key=lambda x: 0 if x["state"] in ("CUT NOW", "DEGRADED") else 1):
        print("%s %-11s %-24s %s" % (mark.get(r["state"], "?"), r["state"], r["lane"], r["verdict"]))
        print("     rule: %-34s %s" % (r["rule"], r["note"]))
        s, p = r["post"], r["pre"]
        if s:
            print("     POST-FIX  n=%3d WR %5.1f%% entry %5.1f%% BEAT %+6.2f +/-%.2f  net %+8.2f (%+.2f/t)"
                  % (s["n"], s["wr"], s["entry"], s["beat"], s["se"], s["net"], s["per_trade"]))
        if p:
            print("     pre-fix   n=%3d WR %5.1f%% entry %5.1f%% BEAT %+6.2f            net %+8.2f (%+.2f/t)"
                  % (p["n"], p["wr"], p["entry"], p["beat"], p["net"], p["per_trade"]))
        print("     baseline: %s" % r["baseline"])
        print("     why:      %s\n" % r["why"])
    cuts = [r for r in rows if r["state"] in ("CUT NOW", "DEGRADED")]
    print("  %d lane(s) at CUT/DEGRADED  <-- PAGE" % len(cuts) if cuts
          else "  no lane has met its cut condition")


if __name__ == "__main__":
    main()
