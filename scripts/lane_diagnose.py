#!/usr/bin/env python3
"""Per-lane trade diagnosis for the Hermes watch.

The infra watch (wss/price_age/RSS/cycle) is blind to WHICH lane is bleeding and
WHY. This drills into the LIVE session's trades per lane (strategy|window|side)
and produces the same first-pass read a human does: win-rate, PnL, blind-0.5
entries, dominant HTF bias, dominant exit reason — then names the worst lane.

Usage: lane_diagnose.py <trades.jsonl> <session_id> [diag_out.jsonl]
  stdout line 1 (for the bash watch, space-delimited, no internal spaces):
    BLIND05 LOSS_STREAK SESS_WR CLOSED_N BLIND_LANES WORST_LANE WORST_WR WORST_N WORST_REASON
  also APPENDS a rich per-lane diagnosis record to diag_out.jsonl when a lane is
  flagged (WR<35% & n>=3, or any blind-0.5) — the artifact Claude reads on wake to
  do the deep diagnosis / fix.
"""
import json
import sys
import time
from collections import Counter, defaultdict

FLAG_WR = 35          # lane win-rate below this (with enough n) = flagged
FLAG_MIN_N = 3        # min closed trades in a lane before WR is trusted
DIAGNOSE_WR = 30      # below this (with n) = escalate to DIAGNOSE_LANE (page + Claude)
# Blind-0.5 is only a problem as a CLUSTER. Post scanner degenerate-0.5 guard
# (drops both-legs-exactly-0.5 = no real book), a lone 0.5 entry is a LEGIT 50/50
# market the bot has edge on — not blind. A flood of 0.5s = the guard regressed.
BLIND_CLUSTER = 3     # >= this many 0.5 entries (session or single lane) = flag

# ── CLEAN-BASELINE PARAMETERS (2026-06-20 real-price re-baseline) ───────────────
# The watch had no reference for what a lane SHOULD do, so it could only react to a
# session-local collapse — never catch a structural regression. These give it that.
#
# DISABLED: lanes we deliberately sat out (bleeders). They must take ~0 trades; ANY
# trade here = the sit-out config/code REGRESSED — highest-severity, page immediately.
# Match is by exact lane key OR by strategy prefix (value "*" = whole asset off).
DISABLED_LANES = {
    "sol_macro|5m|BUY_NO": True,     # sol disable_buy_no_5m_native (-34 @ 29%)
    # NOTE: eth is NOT disabled — it runs pocket-gated (eth_pocket_only). bnb 1h
    # BUY_YES is NOT disabled — it's the documented TOP lane (n=18 was too thin to
    # kill; both were over-disabled 2026-06-20 then reverted). btc 5m BUY_NO is sat
    # out ONLY when 4H is bearish (conditional) — not a hard zero. None belong here.
}
# WINNERS: proven +EV lanes. Flag if WR falls below the floor with enough n — early
# warning that a money-maker is degrading (regime shift / data break) before it bleeds.
WINNER_FLOORS = {
    "xrp_macro|5m|BUY_YES": 45, "xrp_macro|5m|BUY_NO": 42,
    "bitcoin|1h|BUY_YES": 45,   "bitcoin|5m|BUY_YES": 45,
    "hype_macro|5m|BUY_YES": 42, "doge_macro|1h|BUY_YES": 45,
    "sol_macro|15m|BUY_YES": 48,
}
WINNER_MIN_N = 5      # need this many closed before trusting a winner's WR drop


def _disabled_hit(strategy, lane_key):
    if DISABLED_LANES.get(strategy) == "*":
        return True
    return bool(DISABLED_LANES.get(lane_key))


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    if len(sys.argv) < 3:
        print("0 0 - 0 - - - 0 - -")
        return
    path, sess = sys.argv[1], sys.argv[2]
    diag_out = sys.argv[3] if len(sys.argv) > 3 else None

    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("session_id") == sess:
                    rows.append(d)
    except Exception:
        print("0 0 - 0 - - - 0 - -")
        return

    def is05(r):
        v = _f(r.get("entry_price"))
        return v is not None and abs(v - 0.5) < 1e-3

    closed = [r for r in rows if r.get("pnl") is not None]
    blind = [r for r in rows if is05(r)]

    # session-level loss streak (most recent closed backwards)
    streak = 0
    for r in reversed(closed):
        if r.get("win"):
            break
        streak += 1
    sess_wr = int(round(100.0 * sum(1 for r in closed if r.get("win")) / len(closed))) if closed else None

    blind_lanes = Counter((r.get("strategy") or "?") for r in blind)
    blind_lanes_s = ",".join("%s:%d" % (k, v) for k, v in blind_lanes.most_common(4)) or "-"

    # DISABLED-lane regression: scan ALL entries (incl. still-open) — a sat-out lane
    # taking ANY trade means the sit-out config/code broke. Highest severity.
    disabled_hits = Counter()
    for r in rows:
        if _f(r.get("entry_price")) is None:
            continue
        strat = r.get("strategy") or "?"
        key = "%s|%s|%s" % (strat, r.get("window") or "?", r.get("side") or "?")
        if _disabled_hit(strat, key):
            disabled_hits[strat if DISABLED_LANES.get(strat) == "*" else key] += 1

    # per-lane grouping: strategy|window|side (closed trades)
    lanes = defaultdict(list)
    for r in closed:
        key = "%s|%s|%s" % (r.get("strategy") or "?", r.get("window") or "?", r.get("side") or "?")
        lanes[key].append(r)

    flagged = []
    # candidates: (severity, tiebreak, lane, n, wr, reason, alert) — sev 3=SITOUT 2=WINNER 1=BLEED
    cands = []
    for key, lr in lanes.items():
        n = len(lr)
        wins = sum(1 for r in lr if r.get("win"))
        wr = int(round(100.0 * wins / n)) if n else 0
        pnl = round(sum(_f(r.get("pnl")) or 0 for r in lr), 2)
        nblind = sum(1 for r in lr if is05(r))
        top_bias = Counter(str(r.get("primary_htf_bias") or r.get("htf_bias") or "?") for r in lr).most_common(1)[0][0]
        top_exit = Counter(str(r.get("exit_reason") or "?") for r in lr).most_common(1)[0][0]
        ents = [_f(r.get("entry_price")) for r in lr if _f(r.get("entry_price")) is not None]
        avg_entry = round(sum(ents) / len(ents), 3) if ents else None
        # space-free reason (last field of the bash line tolerates it, but keep clean)
        reason = "bias:%s,exit:%s,entry:%s,pnl:%s,blind:%d" % (top_bias, top_exit, avg_entry, pnl, nblind)
        rec = {
            "lane": key, "n": n, "wr": wr, "pnl": pnl, "blind05": nblind,
            "top_bias": top_bias, "top_exit": top_exit, "avg_entry": avg_entry,
            "trades": [
                {"ts": r.get("ts"), "entry": _f(r.get("entry_price")), "side": r.get("side"),
                 "bias": r.get("primary_htf_bias") or r.get("htf_bias"),
                 "exit": r.get("exit_reason"), "pnl": _f(r.get("pnl")),
                 "side_source": r.get("side_source"), "mae": r.get("mae_pct"), "mfe": r.get("mfe_pct")}
                for r in lr[-8:]
            ],
        }
        alert = None
        if key in WINNER_FLOORS and n >= WINNER_MIN_N and wr < WINNER_FLOORS[key]:
            alert = "WINNER"
            cands.append((2, wr, key, n, wr, "WINNER_DEGRADED:wr%d<floor%d,%s" % (wr, WINNER_FLOORS[key], reason), "WINNER"))
        if nblind >= BLIND_CLUSTER or (n >= FLAG_MIN_N and wr < FLAG_WR):
            cands.append((1, wr, key, n, wr, reason, "BLEED"))
            alert = alert or "BLEED"
        if alert:
            rec["alert"] = alert
            flagged.append(rec)

    # disabled hits = top severity (sit-out regressed)
    for lane_or_strat, cnt in disabled_hits.items():
        cands.append((3, -cnt, lane_or_strat, cnt, "-",
                      "SITOUT_REGRESSION:%s_took_%d_trade(s)_while_DISABLED" % (lane_or_strat, cnt), "SITOUT"))
        flagged.append({"lane": lane_or_strat, "alert": "SITOUT", "trades_while_disabled": cnt})

    if cands:
        cands.sort(key=lambda c: (-c[0], c[1]))  # highest severity, then lowest wr / most hits
        _sev, _tb, worst_lane, worst_n, worst_wr, worst_reason, worst_alert = cands[0]
    else:
        worst_lane, worst_n, worst_wr, worst_reason, worst_alert = "-", 0, "-", "-", "-"

    # write the rich diagnosis artifact for Claude / the user to read
    if diag_out and flagged:
        try:
            with open(diag_out, "a") as f:
                f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "session": sess, "session_wr": sess_wr, "closed": len(closed),
                    "worst_lane": worst_lane, "worst_alert": worst_alert, "flagged_lanes": flagged,
                }) + "\n")
        except Exception:
            pass

    print("%s %s %s %s %s %s %s %s %s %s" % (
        len(blind), streak, sess_wr if sess_wr is not None else "-", len(closed),
        blind_lanes_s, worst_lane, worst_wr, worst_n, worst_alert, worst_reason))


if __name__ == "__main__":
    main()
