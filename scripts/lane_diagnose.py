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


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    if len(sys.argv) < 3:
        print("0 0 - 0 - - - 0 -")
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
        print("0 0 - 0 - - - 0 -")
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

    # per-lane grouping: strategy|window|side
    lanes = defaultdict(list)
    for r in closed:
        key = "%s|%s|%s" % (r.get("strategy") or "?", r.get("window") or "?", r.get("side") or "?")
        lanes[key].append(r)

    flagged = []
    worst = None  # (wr, key, n, reason, record)
    for key, lr in lanes.items():
        n = len(lr)
        wins = sum(1 for r in lr if r.get("win"))
        wr = int(round(100.0 * wins / n)) if n else 0
        pnl = round(sum(_f(r.get("pnl")) or 0 for r in lr), 2)
        nblind = sum(1 for r in lr if is05(r))
        bias = Counter(str(r.get("primary_htf_bias") or r.get("htf_bias") or "?") for r in lr).most_common(1)
        exitc = Counter(str(r.get("exit_reason") or "?") for r in lr).most_common(1)
        top_bias = bias[0][0] if bias else "?"
        top_exit = exitc[0][0] if exitc else "?"
        avg_entry = [_f(r.get("entry_price")) for r in lr if _f(r.get("entry_price")) is not None]
        avg_entry = round(sum(avg_entry) / len(avg_entry), 3) if avg_entry else None
        reason = "bias:%s,exit:%s,entry:%s,blind:%d" % (top_bias, top_exit, avg_entry, nblind)

        is_flagged = nblind >= BLIND_CLUSTER or (n >= FLAG_MIN_N and wr < FLAG_WR)
        rec = {
            "lane": key, "n": n, "wr": wr, "pnl": pnl, "blind05": nblind,
            "top_bias": top_bias, "top_exit": top_exit, "avg_entry": avg_entry,
            # the live trades in this lane — what we'd eyeball
            "trades": [
                {"ts": r.get("ts"), "entry": _f(r.get("entry_price")), "side": r.get("side"),
                 "bias": r.get("primary_htf_bias") or r.get("htf_bias"),
                 "exit": r.get("exit_reason"), "pnl": _f(r.get("pnl")),
                 "side_source": r.get("side_source"), "mae": r.get("mae_pct"), "mfe": r.get("mfe_pct")}
                for r in lr[-8:]
            ],
        }
        if is_flagged:
            flagged.append(rec)
        # worst = lowest WR among lanes with enough n (blind lanes always candidate)
        cand_rank = (wr if n >= FLAG_MIN_N else 999, -nblind)
        if worst is None or cand_rank < worst[0]:
            worst = (cand_rank, key, n, reason, wr, rec)

    worst_lane = worst[1] if worst else "-"
    worst_n = worst[2] if worst else 0
    worst_reason = worst[3] if worst else "-"
    worst_wr = worst[4] if worst else "-"
    # only surface a worst lane if it actually qualifies as flagged
    worst_is_real = bool(flagged) and worst is not None and (
        worst[5]["blind05"] >= BLIND_CLUSTER or (worst_n >= FLAG_MIN_N and isinstance(worst_wr, int) and worst_wr < FLAG_WR)
    )
    if not worst_is_real:
        worst_lane, worst_wr, worst_n, worst_reason = "-", "-", 0, "-"

    # write the rich diagnosis artifact for Claude / the user to read
    if diag_out and flagged:
        try:
            with open(diag_out, "a") as f:
                f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "session": sess, "session_wr": sess_wr, "closed": len(closed),
                    "worst_lane": worst_lane, "flagged_lanes": flagged,
                }) + "\n")
        except Exception:
            pass

    print("%s %s %s %s %s %s %s %s %s" % (
        len(blind), streak, sess_wr if sess_wr is not None else "-", len(closed),
        blind_lanes_s, worst_lane, worst_wr, worst_n, worst_reason))


if __name__ == "__main__":
    main()
