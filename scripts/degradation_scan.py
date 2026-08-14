#!/usr/bin/env python3
"""
degradation_scan.py — the "look first, guess never" monitor tool (operator-directed 2026-08-09).

Purpose: when the bot degrades, MEASURE what is different vs the winning baseline BEFORE forming any
hypothesis. This script computes the current session's BEHAVIOR FINGERPRINT and diffs it against a stored
baseline built from winning sessions. It emits RED FLAGS with the specific metric + a mechanical POINTER
to pursue — it never concludes "it's fine" and never guesses a cause. Claude reads the flags and chases
each one to its source.

Fingerprint dimensions (all diffed, because winners vary — high-vol/47%WR AND low-vol/69%WR both won):
  entry_rate (trades/hr) · WR · payoff b (avgWin/avgLoss) · breakeven margin · exit-reason mix ·
  stop_share · reject-reason histogram · direction-override directional ratio · worst lane.

Usage:
  # build/refresh the baseline from winning sessions (measure, don't assume):
  python scripts/degradation_scan.py --build-baseline test_20260808_062202 test_20260722_200518
  # scan the current/latest session and print red flags:
  python scripts/degradation_scan.py            # auto-detects newest session
  python scripts/degradation_scan.py --session test_20260809_201804 --json
"""
import json, os, sys, glob, argparse, collections
from datetime import datetime, timezone

CWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES = os.path.join(CWD, "data/calibration/trades.jsonl")
REJECTS = os.path.join(CWD, "data/calibration/rejected_candidates.jsonl")
OVERRIDE = os.path.join(CWD, "data/runtime/claude_direction_override.json")
BASELINE = os.path.join(CWD, "data/runtime/degradation_baseline.json")
PAPER = os.path.join(CWD, "data/paper_trades")


def _load_trades(session_ids):
    ids = set(session_ids)
    out = []
    with open(TRADES) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("session_id") in ids:
                out.append(r)
    return out


def _span_hours(trades):
    ts = sorted(t.get("opened_at", "") for t in trades if t.get("opened_at"))
    if len(ts) < 2:
        return None
    try:
        a = datetime.fromisoformat(ts[0]); b = datetime.fromisoformat(ts[-1])
        h = (b - a).total_seconds() / 3600.0
        return h if h > 0.05 else None
    except Exception:
        return None


def fingerprint(trades):
    """Behavior fingerprint of a set of closed trades. Robust to small n (fields may be None)."""
    n = len(trades)
    if n == 0:
        return None
    wins = [t for t in trades if t.get("win")]
    losses = [t for t in trades if not t.get("win")]
    avg_win = (sum(t.get("pnl", 0) for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.get("pnl", 0) for t in losses) / len(losses)) if losses else 0.0
    b = (avg_win / abs(avg_loss)) if avg_loss < 0 else None
    wr = len(wins) / n
    breakeven = (1.0 / (1.0 + b)) if b else None
    span = _span_hours(trades)
    exit_mix = collections.Counter(t.get("exit_reason", "?") for t in trades)
    exit_mix = {k: round(v / n, 3) for k, v in exit_mix.items()}
    stop_share = round(sum(1 for t in trades if "stop" in str(t.get("exit_reason", ""))) / n, 3)
    lane = collections.defaultdict(lambda: [0, 0.0])
    for t in trades:
        k = "%s|%s|%s" % (t.get("strategy"), t.get("window"), t.get("side"))
        lane[k][0] += 1; lane[k][1] += t.get("pnl", 0)
    worst = min(((k, v[1], v[0]) for k, v in lane.items() if v[0] >= 3),
               key=lambda x: x[1], default=None)
    return {
        "n": n, "wr": round(wr, 3), "payoff_b": round(b, 3) if b else None,
        "breakeven_wr": round(breakeven, 3) if breakeven else None,
        "breakeven_margin": round(wr - breakeven, 3) if breakeven else None,
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "entry_rate_per_hr": round(n / span, 2) if span else None,
        "stop_share": stop_share, "exit_mix": exit_mix,
        "worst_lane": ({"lane": worst[0], "pnl": round(worst[1], 2), "n": worst[2]} if worst else None),
        "total_pnl": round(sum(t.get("pnl", 0) for t in trades), 2),
    }


def _session_start_utc(session_id):
    """Robust UTC start = earliest file mtime in the session dir (epoch, tz-correct). The session_id
    encodes LOCAL (PT) wall-clock, so parsing it as UTC is wrong; the dir mtime avoids the offset trap."""
    sdir = os.path.join(PAPER, session_id)
    try:
        mtimes = [os.path.getmtime(os.path.join(sdir, f)) for f in os.listdir(sdir)]
        if mtimes:
            # entries.jsonl is created at session start; take the earliest file as the anchor.
            return datetime.fromtimestamp(min(mtimes), tz=timezone.utc)
    except Exception:
        pass
    return None


def reject_histogram(session_id):
    """Reject-reason counts for THIS session's live window (rejects have no session_id, so gate by ts)."""
    start = _session_start_utc(session_id)
    if not start:
        return {}, 0
    from datetime import timedelta
    gate = start - timedelta(minutes=10)  # small buffer for clock skew; NOT a multi-hour widen
    hist = collections.Counter()
    total = 0
    try:
        with open(REJECTS) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    ts = datetime.fromisoformat(r.get("ts", ""))
                except Exception:
                    continue
                if ts < gate:
                    continue
                hist[r.get("reason", "?")] += 1
                total += 1
    except FileNotFoundError:
        return {}, 0
    return dict(hist.most_common(15)), total


def direction_ratio():
    try:
        d = json.load(open(OVERRIDE))
    except Exception:
        return None
    directional = sum(1 for v in d.values() if v.get("side") in ("LONG", "SHORT"))
    total = len(d) or 1
    return {"directional": directional, "total": len(d), "ratio": round(directional / total, 3),
            "assets": {k: v.get("side") for k, v in d.items()}}


def newest_session():
    dirs = sorted(glob.glob(os.path.join(PAPER, "test_*")), key=os.path.getmtime)
    return os.path.basename(dirs[-1]) if dirs else None


def scan(session_id):
    base = json.load(open(BASELINE)) if os.path.exists(BASELINE) else None
    cur = fingerprint(_load_trades([session_id]))
    rej, rej_total = reject_histogram(session_id)
    dirr = direction_ratio()
    flags = []

    def flag(key, msg, pursue):
        flags.append({"flag": key, "detail": msg, "pursue": pursue})

    if cur is None:
        flag("NO_TRADES", "session has 0 closed trades", "confirm bot is scanning + entries firing; check reject histogram below")
    else:
        bfp = (base or {}).get("fingerprint", {})
        # entry rate vs baseline
        er, ber = cur.get("entry_rate_per_hr"), bfp.get("entry_rate_per_hr")
        if er is not None and ber and er < 0.5 * ber:
            flag("FREQ_DROP", "entry_rate %.2f/hr vs baseline %.2f/hr (<50%%)" % (er, ber),
                 "reject histogram: which gate spiked vs baseline? direction override starved?")
        # payoff geometry underwater
        m = cur.get("breakeven_margin")
        if m is not None and m < -0.03:
            flag("UNDERWATER", "WR %.0f%% is %.1fpts under breakeven %.0f%% (payoff b=%s)" % (
                 cur["wr"] * 100, m * 100, (cur["breakeven_wr"] or 0) * 100, cur["payoff_b"]),
                 "loss SIZE problem: which lanes have avg_loss >> avg_win? are stops cutting winners or entries wrong-side?")
        # payoff collapse vs baseline
        pb, bpb = cur.get("payoff_b"), bfp.get("payoff_b")
        if pb and bpb and pb < 0.7 * bpb:
            flag("PAYOFF_COLLAPSE", "payoff b=%.2f vs baseline %.2f" % (pb, bpb),
                 "losers got bigger or winners smaller — check exit_mix shift + worst_lane")
        # stop share jump
        ss, bss = cur.get("stop_share"), bfp.get("stop_share")
        if bss is not None and ss - bss > 0.20:
            flag("STOP_SHARE_UP", "stop_share %.0f%% vs baseline %.0f%%" % (ss * 100, bss * 100),
                 "are stops firing on winners (settled-green) or genuine losers? join to ghost outcome")
        # worst lane bleed
        wl = cur.get("worst_lane")
        if wl and wl["pnl"] <= -40:
            flag("LANE_BLEED", "worst lane %s %s over n=%d" % (wl["lane"], wl["pnl"], wl["n"]),
                 "is this lane side+window-specific? settled would-be WR? config change vs winning window?")
    # direction starvation
    if dirr and dirr["ratio"] < 0.4:
        flag("DIRECTION_STARVED", "only %d/%d assets directional (%s)" % (
             dirr["directional"], dirr["total"], dirr["assets"]),
             "is the direction engine sitting out (minimax FLAT)? is it low-vol time-of-day? drive the engine, don't wait")
    # reject-gate spike vs baseline shares
    bhist = (base or {}).get("reject_shares", {})
    if rej_total > 30:
        for reason, cnt in rej.items():
            share = cnt / rej_total
            bshare = bhist.get(reason, 0.0)
            if share > 0.15 and share > 2.5 * (bshare or 0.001) and reason not in ("eth_fade_shadow",):
                flag("GATE_SPIKE", "%s = %d rejects (%.0f%% of window vs baseline %.0f%%)" % (
                     reason, cnt, share * 100, bshare * 100),
                     "trace this gate to its source; is it blocking a proven lane? is the input feeding it stale/wrong?")

    return {
        "session": session_id, "fingerprint": cur, "direction": dirr,
        "reject_top": rej, "reject_total": rej_total,
        "baseline_used": bool(base), "flags": flags,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--build-baseline", nargs="+", default=None,
                    help="session ids of WINNING sessions to profile into the baseline")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.build_baseline:
        trades = _load_trades(args.build_baseline)
        fp = fingerprint(trades)
        # reject shares aggregated across the baseline sessions' windows
        agg = collections.Counter(); tot = 0
        for s in args.build_baseline:
            h, t = reject_histogram(s)
            for k, v in h.items():
                agg[k] += v
            tot += t
        shares = {k: round(v / tot, 3) for k, v in agg.items()} if tot else {}
        base = {"built_from": args.build_baseline, "fingerprint": fp, "reject_shares": shares}
        json.dump(base, open(BASELINE, "w"), indent=2)
        print("baseline written from %s -> %s" % (args.build_baseline, BASELINE))
        print(json.dumps(fp, indent=2))
        return

    sess = args.session or newest_session()
    res = scan(sess)
    if args.json:
        print(json.dumps(res, indent=2))
        return
    fp = res["fingerprint"]
    print("=== DEGRADATION SCAN: %s ===" % sess)
    if fp:
        print("pnl=%s n=%d WR=%.0f%% b=%s breakeven_margin=%s entry=%s/hr stop_share=%.0f%%" % (
            fp["total_pnl"], fp["n"], fp["wr"] * 100, fp["payoff_b"],
            fp["breakeven_margin"], fp["entry_rate_per_hr"], fp["stop_share"] * 100))
        if fp["worst_lane"]:
            print("worst_lane: %s %s (n=%d)" % (fp["worst_lane"]["lane"], fp["worst_lane"]["pnl"], fp["worst_lane"]["n"]))
    d = res["direction"]
    if d:
        print("direction: %d/%d directional -> %s" % (d["directional"], d["total"], d["assets"]))
    print("rejects (window total %d): %s" % (res["reject_total"], list(res["reject_top"].items())[:6]))
    print()
    if not res["flags"]:
        print(">>> NO RED FLAGS vs baseline. (still verify direction + entry rate above look sane.)")
    else:
        print(">>> %d RED FLAG(S) — PURSUE EACH, do not guess:" % len(res["flags"]))
        for fl in res["flags"]:
            print("  [%s] %s" % (fl["flag"], fl["detail"]))
            print("      -> pursue: %s" % fl["pursue"])
    if not res["baseline_used"]:
        print("\n(!) no baseline stored — run --build-baseline first for diff-based flags.")


if __name__ == "__main__":
    main()
