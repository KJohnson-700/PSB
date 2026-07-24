#!/usr/bin/env python3
"""cut_reopen_tripwire.py — forward REOPEN tripwire for the 2026-07-23 cut lanes.

WHY: we cut 4 persistent-loser lanes (prohibitive min_edge 0.30). That decision was
made on REALIZED good-config data. But once cut, the lanes stop trading and we go
blind — if a lane's edge comes BACK we'd never know. This accumulates the forward
counterfactual so we can catch a reopen.

MECHANISM (mirrors scripts/btc_blocked_short_settle.py): every candidate the 0.30
floor now rejects on a cut lane shows in rejected_candidates.jsonl as reason
`lane_min_edge`. We join each to its REAL market resolution (from the bot logs —
"Market <id> resolved: YES/NO/UP/DOWN", NOT the severed ghost pipeline) and compute
what the blocked side WOULD have returned. This is the honest counterfactual: real
outcome, inferred "would we have won."

⚠️ TRIPWIRE ONLY — NOT a decision driver. It uses inferred would-win, so it is
GHOST-ADJACENT: directional signal only. It NEVER auto-reopens. It flags a lane for
HUMAN review when the cut looks like it's blocking winners; the reopen decision stays
operator + realized-data. [[feedback_ghost_data_do_not_trust_hard_rule]]

SHADOW GUARANTEE: separate process, reads reject log + bot logs, appends one jsonl,
no bot import, no config write, cannot affect a trade.
"""
import json, os, re, glob, sys
import datetime as dt
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL = os.path.join(REPO, "data", "calibration")
REJECT = os.path.join(CAL, "rejected_candidates.jsonl")
OUT = os.path.join(CAL, "cut_reopen_shadow.jsonl")
LOGDIR = os.path.join(REPO, "data", "logs")

# the 2026-07-23 cut lanes: (strategy, window, action, old_floor, cut_floor)
CUT_LANES = [
    ("hype_macro", "15m", "BUY_YES", 0.03, 0.30),
    ("eth_macro",  "1h",  "BUY_NO",  0.06, 0.30),
    ("xrp_macro",  "1h",  "BUY_YES", 0.00, 0.30),
    ("sol_macro",  "1h",  "BUY_YES", 0.07, 0.30),
]
FLOOR_REASONS = ("lane_min_edge", "min_edge")  # the floor's reject reason
# tripwire thresholds to FLAG a lane for reopen review
MIN_N = 8
WR_FLAG = 0.52
CUT_CONFIRM_WR = 0.45


def load_resolutions():
    """market_id -> 'YES'/'NO' from bot logs (UP=YES, DOWN=NO)."""
    res = {}
    rx = re.compile(r"Market (\d+) resolved: (YES|NO|UP|DOWN)")
    for lg in glob.glob(os.path.join(LOGDIR, "*.log")):
        try:
            with open(lg, errors="ignore") as f:
                for line in f:
                    m = rx.search(line)
                    if m:
                        outc = m.group(2)
                        res[m.group(1)] = "YES" if outc in ("YES", "UP") else "NO"
        except OSError:
            continue
    return res


def blocked_return(action, yes_price, outcome):
    """Return per $1 staked for the blocked side, given the real outcome."""
    try:
        yp = float(yes_price)
    except (TypeError, ValueError):
        return None, None
    if not (0.0 < yp < 1.0):
        return None, None
    if action == "BUY_YES":
        win = 1 if outcome == "YES" else 0
        r = (1.0 / yp - 1.0) if win else -1.0
    else:  # BUY_NO
        win = 1 if outcome == "NO" else 0
        noprice = 1.0 - yp
        r = (1.0 / noprice - 1.0) if win else -1.0
    return win, r


def main():
    now = dt.datetime.now(dt.timezone.utc)
    res = load_resolutions()
    # gather blocked candidates per cut lane
    agg = {}
    for strat, win, act, oldf, cutf in CUT_LANES:
        agg[(strat, win, act)] = {"n": 0, "wins": 0, "ret": 0.0, "old_floor": oldf, "cut_floor": cutf}
    if not os.path.isfile(REJECT):
        sys.stderr.write("cut_reopen_tripwire: no reject log\n")
        return 0
    with open(REJECT, errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            strat = d.get("strategy")
            win = d.get("window_size") or d.get("window") or (d.get("extra", {}) or {}).get("window_size")
            act = d.get("action")
            key = (strat, win, act)
            if key not in agg:
                continue
            reason = str(d.get("reason") or d.get("skip_reason") or "")
            if not any(fr in reason for fr in FLOOR_REASONS):
                continue  # only candidates the floor blocked
            mid = str(d.get("market_id") or "")
            if mid not in res:
                continue  # not resolved yet
            win_flag, r = blocked_return(act, d.get("yes_price"), res[mid])
            if win_flag is None:
                continue
            a = agg[key]
            a["n"] += 1
            a["wins"] += win_flag
            a["ret"] += r

    rows = []
    for (strat, win, act), a in agg.items():
        lane = f"{strat.replace('_macro','')}|{win}|{'up' if act=='BUY_YES' else 'down'}"
        n = a["n"]
        wr = (a["wins"] / n) if n else None
        ev = (a["ret"] / n) if n else None
        if n < MIN_N:
            verdict = "insufficient"
        elif wr > WR_FLAG and ev > 0:
            verdict = "FLAG_REOPEN_REVIEW"   # cut may be blocking winners — human look
        elif wr < CUT_CONFIRM_WR or ev < 0:
            verdict = "cut_confirmed"          # cut correctly blocking losers
        else:
            verdict = "borderline"
        rows.append({
            "ts_utc": now.isoformat(), "lane": lane,
            "blocked_settled_n": n, "would_wr": round(wr, 4) if wr is not None else None,
            "would_ev_per_dollar": round(ev, 4) if ev is not None else None,
            "cut_floor": a["cut_floor"], "old_floor": a["old_floor"],
            "verdict": verdict, "resolutions_known": len(res), "mode": "shadow_tripwire",
        })

    os.makedirs(CAL, exist_ok=True)
    with open(OUT, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"cut_reopen_tripwire {now.isoformat()} | resolutions known={len(res)}")
    for r in sorted(rows, key=lambda x: x["lane"]):
        wr = f"{r['would_wr']:.0%}" if r['would_wr'] is not None else " n/a"
        ev = f"{r['would_ev_per_dollar']:+.2f}" if r['would_ev_per_dollar'] is not None else " n/a"
        print(f"  {r['lane']:14} n={r['blocked_settled_n']:>3} would-WR {wr:>5} EV/$ {ev:>6}  -> {r['verdict']}")
    flags = [r for r in rows if r["verdict"] == "FLAG_REOPEN_REVIEW"]
    if flags:
        print(f"  ⚠️ {len(flags)} lane(s) FLAGGED for reopen review (cut may block winners) — HUMAN decision, realized-data only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
