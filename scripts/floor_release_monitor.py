#!/usr/bin/env python3
# ⛔⛔ SUPERSEDED 2026-08-17 — DO NOT RE-ARM THIS FILE.
# It watches gates that NO LONGER EXIST. Measured against the current build:
#   cut_reopen_tripwire  : min_edge 0.30 cut is GONE (live floors 0.05-0.09);
#                          reasons lane_min_edge / min_edge = 0 of 1,823 rows.
#                          Also had NO time filter -> era-pooled the reject log.
#   floor_release_monitor: buy_no_*_pocket_off / eth_buy_no_rsi_floor_off = 0 of 1,823.
#                          RSI blocking now logs as `rsi_hard_blocked`.
# Both also joined resolutions by grepping "Market <id> resolved:" from data/logs/*.log —
# now 7 lines a day across a 3.6GB glob.
# REPLACEMENT: scripts/blocked_band_guard.py (live reasons, GAMMA resolutions,
# market_id dedupe, era filter, ranks on EV/$ not a flat 0.52 WR bar).

"""floor_release_monitor.py — tape-aware RELEASE monitor for the 2026-07-24 RSI floors.

WHY: doge 5m (RSI>=35) and xrp 5m (RSI>=45) BUY_NO floors are REGIME-DEPENDENT — the
sub-floor low-RSI shorts they block are LOSERS in bad tape (this session) but WINNERS in
good/+800 tape (doge below-floor +$32 in win-sessions, xrp +$61). A STATIC floor silently
cuts those winners when the good tape returns. This monitor watches the blocked band's
REAL outcomes on a rolling-recent window; when it flips +EV, that is the signal the good
tape is back and the floor should be RELAXED/LIFTED.

MECHANISM (mirrors scripts/cut_reopen_tripwire.py): every candidate the floor rejects logs
in rejected_candidates.jsonl as reason `buy_no_<tf>_pocket_off`. Join each to its REAL
market resolution (bot logs "Market <id> resolved: YES/NO/UP/DOWN", NOT the severed ghost
pipeline) and compute would-WR / would-EV of the blocked shorts over a RECENT window.

⚠️ FLAGS ONLY — NEVER writes config, never lifts a floor. It uses inferred would-win so it
is GHOST-ADJACENT: directional signal for a HUMAN decision. The floor stays until the
operator relaxes it. [[feedback_ghost_data_do_not_trust_hard_rule]]

SHADOW GUARANTEE: separate process, reads reject log + bot logs, appends one jsonl, no bot
import, no config write, cannot affect a trade.
"""
import json, os, re, glob, sys
import datetime as dt
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL = os.path.join(REPO, "data", "calibration")
REJECT = os.path.join(CAL, "rejected_candidates.jsonl")
OUT = os.path.join(CAL, "floor_release_shadow.jsonl")
LOGDIR = os.path.join(REPO, "data", "logs")

# the regime-dependent floors to watch: (strategy, window, floor_rsi, pocket_reason)
# doge 5m/35 + xrp 5m/45 are the regime-dependent ones (proven cut winners in good tape).
# sol 5m/30 included for completeness but it is regime-SAFE (loses both tapes) — low priority.
WATCH_FLOORS = [
    ("doge_macro", "5m", 35, "buy_no_5m_pocket_off"),
    ("xrp_macro",  "5m", 45, "buy_no_5m_pocket_off"),
    ("doge_macro", "15m", 35, "buy_no_15m_pocket_off"),
    ("sol_macro",  "5m", 30, "buy_no_5m_pocket_off"),
    ("eth_macro",  "15m", 55, "eth_buy_no_rsi_floor_off"),  # 2026-07-24: static eth short floor -> releasable
    ("eth_macro",  "1h",  55, "eth_buy_no_rsi_floor_off"),
]
RECENT_HOURS = 24.0     # rolling window — we want RECENT tape, not lifetime
MIN_N = 8               # need enough settled blocked shorts to trust the read
RELAX_WR = 0.52         # blocked band winning above this => good tape may be back
RELAX_EV = 0.0          # AND positive would-EV per $


def load_resolutions():
    """market_id -> 'YES'/'NO' from bot logs (UP=YES, DOWN=NO)."""
    res = {}
    rx = re.compile(r"Market (\d+) resolved: (YES|NO|UP|DOWN)")
    for lg in glob.glob(os.path.join(LOGDIR, "*.log")) + glob.glob(os.path.join(LOGDIR, "*.out")):
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


def blocked_return(yes_price, outcome, no_price=None):
    """Return per $1 staked for the blocked BUY_NO short, given the real outcome.

    Use the LOGGED no_price when present (book/spread can make it != 1 - yes_price);
    fall back to 1 - yes_price only if no_price is missing/invalid (Codex 2026-07-24).
    """
    noprice = None
    try:
        npv = float(no_price)
        if 0.0 < npv < 1.0:
            noprice = npv
    except (TypeError, ValueError):
        noprice = None
    if noprice is None:
        try:
            yp = float(yes_price)
        except (TypeError, ValueError):
            return None, None
        if not (0.0 < yp < 1.0):
            return None, None
        noprice = 1.0 - yp
    if noprice <= 0:
        return None, None
    win = 1 if outcome == "NO" else 0
    r = (1.0 / noprice - 1.0) if win else -1.0
    return win, r


def parse_ts(d):
    ts = str(d.get("ts") or "")
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def main():
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=RECENT_HOURS)
    res = load_resolutions()
    agg = {}
    for strat, win, floor, reason in WATCH_FLOORS:
        agg[(strat, win)] = {"n": 0, "wins": 0, "ret": 0.0, "floor": floor,
                             "rsis": [], "reason": reason}
    if not os.path.isfile(REJECT):
        sys.stderr.write("floor_release_monitor: no reject log\n")
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
            win = d.get("window") or d.get("window_size")
            key = (strat, win)
            if key not in agg:
                continue
            reason = str(d.get("reason") or d.get("skip_reason") or "")
            if reason != agg[key]["reason"]:  # 2026-07-24: per-floor reason (was hardcoded pocket_off; enables eth)
                continue
            if d.get("action") != "BUY_NO":
                continue
            ts = parse_ts(d)
            if ts is not None and ts < cutoff:
                continue  # rolling-recent only
            mid = str(d.get("market_id") or "")
            if mid not in res:
                continue  # not resolved yet
            win_flag, r = blocked_return(d.get("yes_price"), res[mid], no_price=d.get("no_price"))
            if win_flag is None:
                continue
            a = agg[key]
            a["n"] += 1
            a["wins"] += win_flag
            a["ret"] += r
            ctx = d.get("context") or {}
            rsi = ctx.get("rsi_14") if isinstance(ctx, dict) else None
            if isinstance(rsi, (int, float)):
                a["rsis"].append(float(rsi))

    rows = []
    for (strat, win), a in agg.items():
        lane = f"{strat.replace('_macro','')}|{win}|down"
        n = a["n"]
        wr = (a["wins"] / n) if n else None
        ev = (a["ret"] / n) if n else None
        med_rsi = (sorted(a["rsis"])[len(a["rsis"]) // 2] if a["rsis"] else None)
        if n < MIN_N:
            verdict = "insufficient"
        elif wr > RELAX_WR and ev > RELAX_EV:
            verdict = "FLAG_RELAX_FLOOR"   # blocked band winning => good tape may be back
        elif wr < 0.45 or ev < 0:
            verdict = "floor_holding"        # blocked band still -EV => keep the floor
        else:
            verdict = "borderline"
        rows.append({
            "ts_utc": now.isoformat(), "lane": lane, "floor_rsi": a["floor"],
            "window_hours": RECENT_HOURS, "blocked_settled_n": n,
            "would_wr": round(wr, 4) if wr is not None else None,
            "would_ev_per_dollar": round(ev, 4) if ev is not None else None,
            "median_blocked_rsi": round(med_rsi, 1) if med_rsi is not None else None,
            "verdict": verdict, "resolutions_known": len(res), "mode": "shadow_release_monitor",
        })

    os.makedirs(CAL, exist_ok=True)
    with open(OUT, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"floor_release_monitor {now.isoformat()} | recent={RECENT_HOURS}h resolutions={len(res)}")
    for r in sorted(rows, key=lambda x: x["lane"]):
        wr = f"{r['would_wr']:.0%}" if r["would_wr"] is not None else " n/a"
        ev = f"{r['would_ev_per_dollar']:+.2f}" if r["would_ev_per_dollar"] is not None else " n/a"
        rsi = f"medRSI {r['median_blocked_rsi']}" if r["median_blocked_rsi"] is not None else ""
        print(f"  {r['lane']:14} floor>={r['floor_rsi']:>2} n={r['blocked_settled_n']:>3} "
              f"would-WR {wr:>5} EV/$ {ev:>6} {rsi:>12}  -> {r['verdict']}")
    flags = [r for r in rows if r["verdict"] == "FLAG_RELAX_FLOOR"]
    if flags:
        print(f"  ⚠️ {len(flags)} floor(s) FLAGGED for RELAX — blocked band winning on recent tape; "
              f"HUMAN decision, realized-data only (this is would-win inference).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
