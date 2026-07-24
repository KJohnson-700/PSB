#!/usr/bin/env python3
"""directional_breaker_shadow.py — OBSERVE-ONLY directional circuit breaker.

WHY: the edge ROTATES DIRECTION intraday. xrp 5m 2026-07-23: longs won 03-06Z
(+14.9) and 18-21Z (+7.2), shorts won 06-09Z (+5.1) then BLED 09-12Z (5 straight
down-losses, -16.4). A static min_edge floor can't see that — it treats 09:00 like
06:00. A directional circuit breaker cuts a lane-direction after a loss cluster and
re-opens on recovery.

This was parked earlier as "overfit." SHADOW-FIRST answers that: it REPLAYS the
breaker over real good-config EXIT streams and measures, per candidate trigger,
would_save ($ of losers it would have blocked) vs false_cut ($ of winners it would
have blocked). Only if would_save - false_cut is robustly positive across a param
grid does it earn a live in-process gate (separate step, operator GO).

SHADOW GUARANTEE: separate process. Reads entries.jsonl, appends one jsonl. Never
imports the bot, never blocks a trade, never writes config/_runtime_feedback/caps.

Realized = EXIT-sum from entries.jsonl (never snapshot fields). Good-config only.
"""
import json, os, sys
import datetime as dt
from collections import defaultdict
from itertools import product

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESS_ROOT = os.path.join(REPO, "data", "paper_trades")
OUT = os.path.join(REPO, "data", "calibration", "directional_breaker_shadow.jsonl")

GOOD_CONFIG_SINCE = "2026-07-21"
MIN_SESSION_TRADES = 20

# candidate trigger grid (this is what the shadow forward-tunes)
K_CONSEC_STOPS = [2, 3]            # cut after K consecutive stop-loss exits
COOLDOWN_MINS = [20, 45]           # block the direction for this long after a trip
# (rolling-WR trigger could be added; start with the cleaner consecutive-stop rule)


def parse_ts(s):
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def asset(s):
    return s.replace("_macro", "")


def load_streams(now):
    """Return {lane: [(exit_ts, pnl, is_stop), ...] sorted} across good-config sessions."""
    since = dt.datetime.strptime(GOOD_CONFIG_SINCE, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    lanes = defaultdict(list)
    included = []
    for name in sorted(os.listdir(SESS_ROOT)):
        if not name.startswith("test_"):
            continue
        try:
            sdate = dt.datetime.strptime(name.split("_")[1], "%Y%m%d").replace(tzinfo=dt.timezone.utc)
        except (IndexError, ValueError):
            continue
        if sdate < since:
            continue
        path = os.path.join(SESS_ROOT, name, "entries.jsonl")
        if not os.path.isfile(path):
            continue
        rows = []
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    if r.get("event") != "EXIT" or not r.get("timestamp"):
                        continue
                    win = r.get("extra", {}).get("window_size", "?")
                    side = "up" if r.get("action") == "BUY_YES" else "down"
                    lane = f"{asset(r.get('strategy',''))}|{win}|{side}"
                    is_stop = "stop" in (r.get("reason", "") or "")
                    rows.append((lane, parse_ts(r["timestamp"]), float(r.get("pnl", 0) or 0), is_stop))
        except (OSError, ValueError):
            continue
        if len(rows) < MIN_SESSION_TRADES:
            continue
        included.append(name)
        for lane, ts, pnl, is_stop in rows:
            lanes[lane].append((ts, pnl, is_stop))
    for lane in lanes:
        lanes[lane].sort()
    return lanes, included


def simulate(stream, k_stops, cooldown_mins):
    """Replay the breaker over one lane's chronological (ts,pnl,is_stop) stream.

    Returns (episodes, would_blocked_pnl, false_cut_pnl, blocked_n).
    - trip after k_stops consecutive stop-loss exits.
    - while tripped (within cooldown_mins of the tripping exit), subsequent entries
      in this lane are 'would-blocked'. Their pnl accrues: negatives = would_save,
      positives = false_cut. Cooldown resets on each blocked event's time check.
    """
    consec = 0
    tripped_until = None
    episodes = 0
    blocked_pnl = 0.0
    false_cut = 0.0
    blocked_n = 0
    for ts, pnl, is_stop in stream:
        if tripped_until is not None and ts < tripped_until:
            # this trade would have been blocked
            blocked_pnl += pnl
            if pnl > 0:
                false_cut += pnl
            blocked_n += 1
            continue
        else:
            tripped_until = None  # cooldown expired
        # not blocked -> update streak
        if is_stop and pnl < 0:
            consec += 1
        else:
            consec = 0
        if consec >= k_stops:
            episodes += 1
            tripped_until = ts + dt.timedelta(minutes=cooldown_mins)
            consec = 0
    would_save = -(blocked_pnl - false_cut)  # losers avoided (positive = good)
    return episodes, blocked_pnl, would_save, false_cut, blocked_n


def main():
    now = dt.datetime.now(dt.timezone.utc)
    lanes, included = load_streams(now)
    if not lanes:
        sys.stderr.write("directional_breaker_shadow: no good-config exits\n")
        return 0
    out_rows = []
    for lane, stream in sorted(lanes.items()):
        if len(stream) < 6:
            continue
        for k, cd in product(K_CONSEC_STOPS, COOLDOWN_MINS):
            ep, bpnl, save, fcut, bn = simulate(stream, k, cd)
            if ep == 0:
                continue
            out_rows.append({
                "ts_utc": now.isoformat(),
                "lane": lane,
                "k_consec_stops": k,
                "cooldown_mins": cd,
                "episodes": ep,
                "blocked_n": bn,
                "blocked_pnl": round(bpnl, 2),       # net pnl of blocked trades (negative = breaker helped)
                "would_save": round(save, 2),        # losers avoided
                "false_cut": round(fcut, 2),         # winners missed
                "net_benefit": round(-bpnl, 2),      # -blocked_pnl: positive = breaker net-helped
                "n_stream": len(stream),
                "mode": "shadow",
            })
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as f:
        for r in out_rows:
            f.write(json.dumps(r) + "\n")
    # summary: which (lane, params) would have net-helped most
    print(f"directional_breaker_shadow {now.isoformat()} | sessions={len(included)} lanes={len(lanes)}")
    winners = sorted([r for r in out_rows if r["net_benefit"] > 0], key=lambda r: -r["net_benefit"])[:10]
    print("  TOP would-net-help (lane k/cooldown: +$net = save - falsecut):")
    for r in winners:
        print(f"   {r['lane']:16} k{r['k_consec_stops']}/{r['cooldown_mins']}m  "
              f"net{r['net_benefit']:+7.2f} (save {r['would_save']:+.2f} / falsecut -{r['false_cut']:.2f}, blocked {r['blocked_n']})")
    hurt = [r for r in out_rows if r["net_benefit"] < 0]
    print(f"  (episodes where breaker would have NET-HURT: {len(hurt)} of {len(out_rows)} lane/param rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
