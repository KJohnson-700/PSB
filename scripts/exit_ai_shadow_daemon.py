#!/usr/bin/env python3
"""Live exit-decision SHADOW daemon — observe-only forward paper test of the exit brain.

Operator directive (2026-08-03): stop retro-only scoring and MOVE FORWARD — test the exit
policy on paper, live. This daemon does that WITHOUT touching the bot: it polls the active
paper session's positions.json (the same file the dashboard reads), and for every OPEN
position logs what the guardrailed exit policy WOULD decide (HOLD-to-resolution vs CUT/keep
the static exit) each tick — joined later to the realized outcome by trade_id. It NEVER
places or cancels anything; it only writes its own log. Same discipline as the tape-side-veto
shadow and the ghost settler.

WHY the CODED guardrail, not per-tick LLM: a live per-tick LLM call per open position would
add latency/cost and a network dependency in a loop. The LLM (Codex, offline) scores settled
BATCHES nightly (scripts/ai_exit_decide.py); this live shadow runs the deterministic guardrail
distilled from that analysis. Both are the same policy family, measured the same way vs static.

Guardrails (downside-bounded — defaults to CUT/static, only HOLDs when ALL agree):
  lane_hold_prior >= 0.55 (walk-forward, past trades only)  AND  not fighting the tape
  AND position actually went green (mfe)  AND  time remains to recover.

Read-only, fail-silent, no bot coupling. Reads tape from the TAIL of tape_map.jsonl (not a
full reread — avoids the jsonl-reread churn that ballooned RSS before).

Run (nohup daemon; launchd can't touch ~/Documents under Sequoia TCC):
  nohup .venv/bin/python scripts/exit_ai_shadow_daemon.py --interval 20 \
      >> data/calibration/exit_ai_shadow_daemon.log 2>&1 &
"""
from __future__ import annotations
import argparse, json, os, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAL = ROOT / "data" / "calibration"
PAPER = ROOT / "data" / "paper_trades"
TAPE = CAL / "tape_map.jsonl"
SETTLED = CAL / "trades_settled.jsonl"
OUT = CAL / "exit_ai_shadow.jsonl"

# asset key in tape_map matches the strategy name for alts; bitcoin strategy -> 'bitcoin'.
_ALPHA = _BETA = 2.0


def _now() -> float:
    return time.time()


def _f(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _iso_epoch(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def active_session_dir():
    """Most-recently-modified test_* session dir (the live one)."""
    if not PAPER.exists():
        return None
    dirs = [d for d in PAPER.iterdir() if d.is_dir() and d.name.startswith("test_")]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


def load_lane_priors():
    """Walk-forward per-lane hold-beats-cut prior from settled trades (past-only, shrunk)."""
    priors = {}
    if not SETTLED.exists():
        return priors
    rows = []
    for l in open(SETTLED):
        try:
            s = json.loads(l)
        except Exception:
            continue
        a = _f(s.get("actual_pnl")); h = _f(s.get("held_pnl"))
        te = _iso_epoch(s.get("ts"))
        if a is None or h is None or te is None:
            continue
        lane = f"{s.get('strategy')}|{s.get('window')}|{s.get('action')}"
        rows.append((te, lane, h > a))
    rows.sort(key=lambda r: r[0])
    run = {}
    for _, lane, better in rows:
        w, n = run.get(lane, (0, 0))
        priors[lane] = (w + _ALPHA) / (n + _ALPHA + _BETA)  # prior as of the LATEST past trade
        run[lane] = (w + (1 if better else 0), n + 1)
    return priors


def latest_tape(max_bytes=200_000):
    """Latest tape row per asset from the TAIL of tape_map.jsonl (bounded read)."""
    out = {}
    if not TAPE.exists():
        return out
    try:
        sz = TAPE.stat().st_size
        with open(TAPE, "rb") as fh:
            if sz > max_bytes:
                fh.seek(sz - max_bytes)
                fh.readline()  # skip partial line
            for raw in fh:
                try:
                    r = json.loads(raw)
                    out[r.get("asset")] = r
                except Exception:
                    continue
    except Exception:
        return out
    return out


def _tape_for(strategy, tape):
    # bitcoin strategy -> 'bitcoin'; alts already match ('sol_macro', etc.)
    return tape.get(strategy) or tape.get(strategy.split("_")[0])


def _guardrail_decision(ctx):
    p = ctx.get("lane_hold_prior")
    if p is None:
        return "CUT", "no_lane_prior"
    lane_favors = p >= 0.55
    td = ctx.get("tape_dir")
    aligned = None if td is None else (td == ("UP" if ctx["side_up"] else "DOWN"))
    not_against = aligned is not False
    was_green = (ctx.get("mfe_pct") or 0.0) >= 0.05
    room = (ctx.get("secs_to_expiry") or 0.0) >= 60.0
    if lane_favors and not_against and was_green and room:
        return "HOLD", f"prior={p:.2f} tape_ok green room"
    why = []
    if not lane_favors: why.append(f"prior={p:.2f}<0.55")
    if aligned is False: why.append(f"against_tape({td})")
    if not was_green: why.append("never_green")
    if not room: why.append("no_time")
    return "CUT", ",".join(why) or "cut"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=20.0)
    ap.add_argument("--prior-refresh-s", type=float, default=900.0)
    ap.add_argument("--once", action="store_true", help="single pass then exit (for testing)")
    args = ap.parse_args()

    excursion = {}     # trade_id -> {mfe_pct, mae_pct, ticks}
    priors = load_lane_priors()
    last_prior = _now()
    print(f"[exit_shadow] start interval={args.interval}s  lanes_with_prior={len(priors)}  out={OUT}")

    while True:
        try:
            if _now() - last_prior > args.prior_refresh_s:
                priors = load_lane_priors(); last_prior = _now()
            sess = active_session_dir()
            pj = sess / "positions.json" if sess else None
            tape = latest_tape()
            open_ids = set()
            if pj and pj.exists():
                try:
                    raw = json.load(open(pj))
                except Exception:
                    raw = {}
                for pid, p in (raw.items() if isinstance(raw, dict) else []):
                    ep = _f(p.get("entry_price")); cp = _f(p.get("current_price"))
                    if ep in (None, 0) or cp is None:
                        continue
                    open_ids.add(pid)
                    leg = str(p.get("entry_leg") or "YES").upper()
                    side_up = (leg == "YES")
                    # positions.json current_price is ALREADY the position's OWN token mark
                    # (YES-token for a YES leg, NO-token = 1-yes for a NO leg — see
                    # trade_journal.log_price_update:434-447), so the return is (mark-entry)/entry
                    # for BOTH legs, and pnl is sign-correct (positive = position winning). The old
                    # side-aware fallback (ep-cp)/ep DOUBLE-INVERTED the NO leg because cp was
                    # already the NO mark. Use pnl when present, else the token-price return.
                    pnl = _f(p.get("pnl"))
                    sz = _f(p.get("size")) or 1.0
                    ret = (pnl / (ep * sz)) if pnl is not None else ((cp - ep) / ep)
                    ex = excursion.setdefault(pid, {"mfe_pct": ret, "mae_pct": ret, "ticks": 0})
                    ex["mfe_pct"] = max(ex["mfe_pct"], ret)
                    ex["mae_pct"] = min(ex["mae_pct"], ret)
                    ex["ticks"] += 1
                    strat = p.get("strategy", "unknown")
                    tr = _tape_for(strat, tape)
                    secs = _secs_to_expiry(p)
                    lane = f"{strat}|{p.get('window') or _infer_window(p)}|{'BUY_YES' if side_up else 'BUY_NO'}"
                    ctx = {
                        "side_up": side_up,
                        "mfe_pct": ex["mfe_pct"], "mae_pct": ex["mae_pct"],
                        "secs_to_expiry": secs,
                        "tape_dir": (tr or {}).get("direction"),
                        "lane_hold_prior": priors.get(lane),
                    }
                    decision, reason = _guardrail_decision(ctx)
                    row = {
                        "ts": _now(), "trade_id": pid, "tick": ex["ticks"],
                        "strategy": strat, "lane": lane, "side": "LONG" if side_up else "SHORT",
                        "entry_price": ep, "mark": cp, "unreal_ret": round(ret, 4),
                        "mfe_pct": round(ex["mfe_pct"], 4), "mae_pct": round(ex["mae_pct"], 4),
                        "secs_to_expiry": round(secs, 1) if secs is not None else None,
                        "tape_dir": ctx["tape_dir"],
                        "lane_hold_prior": (round(ctx["lane_hold_prior"], 3)
                                            if ctx["lane_hold_prior"] is not None else None),
                        "decision": decision, "reason": reason,
                    }
                    try:
                        with open(OUT, "a") as fh:
                            fh.write(json.dumps(row) + "\n")
                    except Exception:
                        pass
            # drop excursion trackers for positions that have closed (bounded memory)
            for tid in list(excursion):
                if tid not in open_ids:
                    excursion.pop(tid, None)
        except Exception as e:  # never die on a bad tick
            try:
                with open(OUT, "a") as fh:
                    fh.write(json.dumps({"ts": _now(), "error": str(e)[:200]}) + "\n")
            except Exception:
                pass
        if args.once:
            break
        time.sleep(args.interval)


import re as _re
_TIME_RE = _re.compile(r"(\d{1,2}):(\d{2})\s*([AP]M)", _re.IGNORECASE)
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None


def _secs_to_expiry(p):
    """Seconds until the market resolves. positions.json usually lacks end_date, so derive
    it from the market_question's END clock-time ('...5:15AM-5:30AM ET') anchored to the
    opened_at date in US/Eastern. Falls back to end_date, then None (fail-safe)."""
    ed = _iso_epoch(p.get("end_date"))
    if ed is not None:
        return ed - datetime.now(timezone.utc).timestamp()
    if _ET is None:
        return None
    ts = _TIME_RE.findall(str(p.get("market_question") or ""))
    oa = p.get("opened_at")
    if len(ts) < 2 or not oa:
        return None
    try:
        opened = datetime.fromisoformat(str(oa).replace("Z", "+00:00"))
        et_day = opened.astimezone(_ET)
        h, m, ap = ts[1]  # the window END time
        h = int(h) % 12
        if ap.upper() == "PM":
            h += 12
        expiry = et_day.replace(hour=h, minute=int(m), second=0, microsecond=0)
        if expiry < et_day:            # wrapped past midnight
            expiry = expiry.replace(day=et_day.day + 1)
        return expiry.timestamp() - datetime.now(timezone.utc).timestamp()
    except Exception:
        return None


def _infer_window(p):
    """positions.json has no 'window' field; the market_question carries the resolution
    window as a time range (e.g. '5:15AM-5:30AM ET' -> 15m). Parse the two times and map
    the span to 5m / 15m / 1h so the lane key joins the walk-forward prior. Fail -> 'NA'."""
    q = str(p.get("market_question") or "")
    ts = _TIME_RE.findall(q)
    if len(ts) >= 2:
        def _mins(h, m, ap):
            h = int(h) % 12
            if ap.upper() == "PM":
                h += 12
            return h * 60 + int(m)
        span = (_mins(*ts[1]) - _mins(*ts[0])) % (24 * 60)
        if span <= 7:
            return "5m"
        if span <= 30:
            return "15m"
        return "1h"
    return "NA"


if __name__ == "__main__":
    main()
