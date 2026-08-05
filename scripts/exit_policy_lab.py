#!/usr/bin/env python3
"""Exit Policy Lab (Script E / Tier-0 AI shadow exit-manager) — champion vs challenger.

The champion is the bot's STATIC exit (what actually happened). Challengers are
alternative exit POLICIES, each of which decides — per closed trade — whether to
HOLD to resolution or CUT at the static exit point. Because every settled trade
carries BOTH outcomes (`actual_pnl` = cut, `held_pnl` = hold), this comparison is
FAITHFUL — no price-path reconstruction, no backtester. We score each policy's total
realized $ vs the static champion, per lane and per tape bucket.

This is the observe-only, zero-live-touch first rung of the AI shadow exit-manager
(docs/AI_SHADOW_EXIT_MANAGER_SCOPE.md). The **AI-policy slot** is the POLICIES
registry below: each entry is a function ctx -> "HOLD" | "CUT". The tape-conditioned
heuristic is the first AI-derived policy (guberm-style "does the tape still support the
position? hold : cut"); an LLM-backed policy can be registered the same way.

HONEST LIMITS:
  * Only HOLD-vs-CUT is faithfully scorable from logged data. Path-dependent policies
    (trailing stop, time-exit, TP-level sweep) need the ordered per-tick path we do NOT
    persist yet — those are Tier 1 (see scope doc). This script deliberately does not fake them.
  * n is small (settled trades only) — every cell carries n; do not act on thin cells.
  * The real mechanical tape_map has ~0 coverage on already-settled trades (it is young);
    the heuristic falls back to the entry-context PROXY tape (primary_htf_bias), which our
    own memory flags as an unreliable read. Treat tape-policy results as provisional until
    tape_map covers settled trades. Static-vs-hold results are solid now.

LIVE REALIZED only. Read-only, fail-safe.

Usage:
  python3 scripts/exit_policy_lab.py                       # all settled trades
  python3 scripts/exit_policy_lab.py --by-lane --min-n 4
  python3 scripts/exit_policy_lab.py --by-tape
  python3 scripts/exit_policy_lab.py --json
"""
from __future__ import annotations
import argparse, json, math, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

CAL = Path(__file__).resolve().parent.parent / "data" / "calibration"
SETTLED = CAL / "trades_settled.jsonl"
TRADES = CAL / "trades.jsonl"
TAPE = CAL / "tape_map.jsonl"
TAPE_TOL_S = 300.0  # match a trade to a tape snapshot within this many seconds


def _f(r, k):
    v = r.get(k)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _epoch(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def wilson(w, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, c - h), min(1.0, c + h))


# ---------------------------------------------------------------------------
# AI-POLICY SLOT. Each policy: ctx -> "HOLD" | "CUT".
#   ctx = {side_up, tape_dir ('UP'|'DOWN'|'FLAT'|None), tape_strength, tape_source,
#          lane, mfe_pct, mae_pct}
# Add an LLM-backed policy by registering a callable that batches ctx -> decision.
# ---------------------------------------------------------------------------
def pol_static(ctx):
    return "CUT"          # champion: take the static exit that actually happened


def pol_always_hold(ctx):
    return "HOLD"


def _tape_aligned(ctx):
    d = ctx.get("tape_dir")
    if d is None:
        return None
    if ctx["side_up"]:
        return d == "UP"
    return d == "DOWN"


def pol_tape_hold_aligned(ctx):
    """HOLD only when the tape agrees with the position's side; else CUT.
    Grounded in the finding that longs-into-a-DOWN-tape bled — a position fighting
    the tape should be cut, one riding it can be held to resolution."""
    a = _tape_aligned(ctx)
    if a is None:
        return "CUT"      # unknown tape -> conservative: take the static exit
    return "HOLD" if a else "CUT"


def pol_tape_hold_aligned_trending(ctx):
    """As above but also require conviction (strength) to hold — hold only a strong,
    aligned trend; cut aligned-but-weak and everything unaligned."""
    a = _tape_aligned(ctx)
    if a:
        s = ctx.get("tape_strength")
        if s is None or s >= 0.5:
            return "HOLD"
    return "CUT"


# --- LLM AI-policy slot -----------------------------------------------------
# The AI brain runs OUT-OF-PROCESS via a terminal tool (Codex CLI) or Claude Code
# — NOT the API. It reads an emitted decision batch (no outcome leaked) and writes
# ai_exit_decisions.jsonl {trade_id, decision:'HOLD'|'CUT'}. This policy just reads
# that file back and applies it, so the AI is scored on the exact champion/challenger
# footing as every coded policy. Missing/unknown trade -> CUT (conservative = static).
AI_DECISIONS = CAL / "ai_exit_decisions.jsonl"
_AI_CACHE = {}


def _load_ai_decisions():
    if _AI_CACHE:
        return _AI_CACHE
    if AI_DECISIONS.exists():
        for l in open(AI_DECISIONS):
            try:
                d = json.loads(l)
                dec = str(d.get("decision", "")).upper()
                if dec in ("HOLD", "CUT"):
                    _AI_CACHE[d.get("trade_id")] = dec
            except Exception:
                continue
    return _AI_CACHE


def pol_llm(ctx):
    return _load_ai_decisions().get(ctx["trade_id"], "CUT")


# --- GUARDRAILS -------------------------------------------------------------
# The guardrail DEFAULTS TO CUT (= static champion) and only permits a HOLD when
# multiple independent conditions all agree. Because every deviation from static is
# gated, the policy is downside-bounded: it can only differ from the champion on the
# subset of trades that pass every check. This is the "guardrails" the operator asked
# to try — discipline encoded as necessary conditions, not free judgment on thin context.
def _guardrail_allows_hold(ctx):
    p = ctx.get("lane_hold_prior")
    if p is None:
        return False
    lane_favors = p >= 0.55                                   # history says this lane rewards holding
    not_against_tape = _tape_aligned(ctx) is not False        # not fighting the tape (aligned or unknown)
    was_green = (ctx.get("mfe_pct") or 0.0) >= 0.05           # it actually went green (cut-a-winner signature)
    has_room = (ctx.get("secs_to_expiry_at_exit") or 0.0) >= 60.0  # time left to recover
    return lane_favors and not_against_tape and was_green and has_room


def pol_guardrail_hold(ctx):
    """Pure guardrail policy: HOLD only when every condition agrees, else take the static exit."""
    return "HOLD" if _guardrail_allows_hold(ctx) else "CUT"


def pol_llm_guarded(ctx):
    """AI INSIDE guardrails: honor the AI's HOLD only if the guardrail also permits it.
    Caps the AI's downside — it can no longer hold a tape-fighting, never-green, prior-negative
    position no matter what it 'thinks'. The AI can still choose to CUT freely."""
    ai = _load_ai_decisions().get(ctx["trade_id"], "CUT")
    if ai == "HOLD":
        return "HOLD" if _guardrail_allows_hold(ctx) else "CUT"
    return "CUT"


POLICIES = {
    "static": pol_static,
    "always_hold": pol_always_hold,
    "tape_hold_aligned": pol_tape_hold_aligned,
    "tape_hold_aligned_trending": pol_tape_hold_aligned_trending,
    "guardrail_hold": pol_guardrail_hold,   # rule-based guardrail (no AI)
    "llm": pol_llm,                         # AI free judgment (terminal AI, no API)
    "llm_guarded": pol_llm_guarded,         # AI inside the guardrail — downside-capped
}

_PROXY_DIR = {"BULLISH": "UP", "BEARISH": "DOWN", "NEUTRAL": "FLAT",
              "UP": "UP", "DOWN": "DOWN", "FLAT": "FLAT"}


def load():
    if not SETTLED.exists():
        print(f"[exitlab] no settled log at {SETTLED}", file=sys.stderr); return []
    settled = [json.loads(l) for l in open(SETTLED) if l.strip()]
    # join trades.jsonl by trade_id for proxy tape + excursion
    tj = {}
    if TRADES.exists():
        for l in open(TRADES):
            try:
                d = json.loads(l); tj[d.get("trade_id")] = d
            except Exception:
                continue
    # tape_map by asset -> sorted (epoch, row)
    tape = defaultdict(list)
    if TAPE.exists():
        for l in open(TAPE):
            try:
                d = json.loads(l)
                tape[d.get("asset")].append((float(d.get("ts", 0.0)), d))
            except Exception:
                continue
    for a in tape:
        tape[a].sort(key=lambda x: x[0])

    rows = []
    real_tape_hits = 0
    for s in settled:
        tid = s.get("trade_id")
        base = _f(s, "cost_basis") or (abs(_f(s, "actual_pnl") or 0) or 1.0)
        static_pnl = _f(s, "actual_pnl")
        hold_pnl = _f(s, "held_pnl")
        if static_pnl is None or hold_pnl is None:
            continue
        strat = s.get("strategy", "?")
        action = s.get("action", "?")
        side_up = (action == "BUY_YES")
        t = tj.get(tid, {})
        # tape: real tape_map join first, else entry-context proxy
        tape_dir = tape_strength = None
        tape_source = "none"
        te = _epoch(s.get("ts"))
        if te is not None and tape.get(strat):
            best = None
            for (ep, row) in tape[strat]:
                if abs(ep - te) <= TAPE_TOL_S:
                    if best is None or abs(ep - te) < abs(best[0] - te):
                        best = (ep, row)
            if best:
                tape_dir = best[1].get("direction")
                tape_strength = _f(best[1], "strength")
                tape_source = "map"
                real_tape_hits += 1
        if tape_dir is None:
            htf = str(t.get("primary_htf_bias", "")).upper()
            tape_dir = _PROXY_DIR.get(htf)
            if tape_dir is not None:
                tape_source = "proxy_htf"
        rows.append({
            "trade_id": tid, "lane": f"{strat}|{s.get('window')}|{action}",
            "strategy": strat, "side_up": side_up,
            "static_pnl": static_pnl, "hold_pnl": hold_pnl, "base": base,
            "tape_dir": tape_dir, "tape_strength": tape_strength, "tape_source": tape_source,
            "mfe_pct": _f(s, "mfe_pct") if _f(s, "mfe_pct") is not None else _f(t, "mfe_pct"),
            "mae_pct": _f(s, "mae_pct") if _f(s, "mae_pct") is not None else _f(t, "mae_pct"),
            "exit_reason": s.get("exit_reason") or t.get("exit_reason"),
            "secs_to_expiry_at_exit": _f(s, "secs_to_expiry_at_exit")
            if _f(s, "secs_to_expiry_at_exit") is not None else _f(t, "secs_to_expiry_at_exit"),
            "entry_price": _f(s, "entry_price"),
            "rsi_bucket": s.get("rsi_bucket") or t.get("rsi_bucket"),
            "settle_ts": te if te is not None else 0.0,
            "held_better": hold_pnl > static_pnl,
        })

    # WALK-FORWARD per-lane hold prior (leakage-safe): for each trade, the fraction of
    # SAME-LANE trades that settled BEFORE it where hold beat cut, shrunk toward 0.5
    # (alpha=beta=2). This is exactly what history would have told you AT the decision —
    # no look-ahead, no reuse of the trade's own outcome. It is the empirical grounding
    # the first AI pass lacked (operator: "the ai is guessing with minimal context").
    rows.sort(key=lambda r: r["settle_ts"])
    run = defaultdict(lambda: [0, 0])  # lane -> [hold_wins, n], running over past trades only
    ALPHA = BETA = 2.0
    for r in rows:
        w, n = run[r["lane"]]
        r["lane_hold_prior"] = round((w + ALPHA) / (n + ALPHA + BETA), 3)
        r["lane_prior_n"] = n
        run[r["lane"]][1] += 1
        if r["held_better"]:
            run[r["lane"]][0] += 1
    return rows, real_tape_hits


# The decision context the AI sees — ONLY what is knowable AT THE EXIT MOMENT.
# Deliberately EXCLUDES static_pnl / hold_pnl (that is the answer we grade on) and
# any whole-life excursion (that would leak the future).
def _ai_ctx(c):
    return {
        "trade_id": c["trade_id"],
        "lane": c["lane"],
        "side": "LONG" if c["side_up"] else "SHORT",
        "entry_price": c.get("entry_price"),
        "exit_reason": c.get("exit_reason"),
        "mfe_pct": c.get("mfe_pct"),   # best favorable excursion reached before the static exit
        "mae_pct": c.get("mae_pct"),   # worst adverse excursion before the static exit
        "secs_to_expiry_at_exit": c.get("secs_to_expiry_at_exit"),
        "tape_dir": c.get("tape_dir"),
        "tape_strength": c.get("tape_strength"),
        "tape_source": c.get("tape_source"),
        "rsi_bucket": c.get("rsi_bucket"),
        # walk-forward empirical grounding: how often holding beat cutting on THIS lane in the
        # PAST (leakage-safe), and on how many prior trades. prior≈0.5 with small n = weak.
        "lane_hold_prior": c.get("lane_hold_prior"),
        "lane_prior_n": c.get("lane_prior_n"),
    }


def emit_batch(rows, path):
    with open(path, "w") as fh:
        for c in rows:
            fh.write(json.dumps(_ai_ctx(c)) + "\n")
    return len(rows)


def realized(policy_fn, ctx):
    return ctx["hold_pnl"] if policy_fn(ctx) == "HOLD" else ctx["static_pnl"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--by-lane", action="store_true")
    ap.add_argument("--by-tape", action="store_true")
    ap.add_argument("--min-n", type=int, default=3)
    ap.add_argument("--policies", type=str, default=None,
                    help="comma list; default = all registered")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--emit-batch", type=str, default=None,
                    help="write the AI decision batch (no-outcome contexts) to this path and exit")
    args = ap.parse_args()

    loaded = load()
    if not loaded:
        print("[exitlab] no data"); return
    rows, real_hits = loaded
    if not rows:
        print("[exitlab] no settled trades with both outcomes"); return

    if args.emit_batch:
        n = emit_batch(rows, args.emit_batch)
        print(f"[exitlab] wrote {n} decision contexts -> {args.emit_batch}")
        return

    names = [p.strip() for p in args.policies.split(",")] if args.policies else list(POLICIES)
    names = [n for n in names if n in POLICIES]

    # overall totals per policy
    totals = {n: sum(realized(POLICIES[n], c) for c in rows) for n in names}
    static_total = totals.get("static", sum(c["static_pnl"] for c in rows))

    if args.json:
        out = {"n": len(rows), "real_tape_hits": real_hits,
               "totals": totals, "static_total": static_total,
               "delta_vs_static": {n: round(totals[n] - static_total, 2) for n in names}}
        print(json.dumps(out, indent=2)); return

    tape_cov = sum(1 for c in rows if c["tape_source"] == "map")
    proxy_cov = sum(1 for c in rows if c["tape_source"] == "proxy_htf")
    print(f"\nEXIT POLICY LAB  ·  {len(rows)} settled trades  (champion = static exit)")
    print(f"  tape coverage: real_map={tape_cov}  proxy_htf={proxy_cov}  none={len(rows)-tape_cov-proxy_cov}")
    if tape_cov == 0:
        print("  ⚠ real tape_map covers 0 settled trades yet (map is young) — tape policies")
        print("    run on the PROXY htf-bias tape (known-unreliable). Provisional; static-vs-hold is solid.")
    print(f"\n  {'policy':30s} {'total$':>9} {'Δ vs static':>12}   {'holds':>6}")
    print("  " + "-" * 66)
    for n in sorted(names, key=lambda k: -totals[k]):
        holds = sum(1 for c in rows if POLICIES[n](c) == "HOLD")
        d = totals[n] - static_total
        flag = "  <-- beats static" if d > 0.01 and n != "static" else ""
        print(f"  {n:30s} {totals[n]:>+9.2f} {d:>+12.2f}   {holds:>6}{flag}")

    # per-lane static vs hold (the faithful core)
    if args.by_lane:
        lanes = defaultdict(lambda: {"n": 0, "static": 0.0, "hold": 0.0, "hold_better": 0})
        for c in rows:
            L = lanes[c["lane"]]
            L["n"] += 1; L["static"] += c["static_pnl"]; L["hold"] += c["hold_pnl"]
            if c["hold_pnl"] > c["static_pnl"]:
                L["hold_better"] += 1
        print(f"\n  PER-LANE  static vs hold-to-resolution  (min-n {args.min_n})")
        print(f"  {'lane':30s} {'n':>3} {'static$':>8} {'hold$':>8} {'Δhold':>7} {'hold>cut':>9}")
        print("  " + "-" * 74)
        for lane, L in sorted(lanes.items(), key=lambda kv: (kv[1]["hold"] - kv[1]["static"]), reverse=True):
            if L["n"] < args.min_n:
                continue
            lo, hi = wilson(L["hold_better"], L["n"])
            print(f"  {lane:30s} {L['n']:>3} {L['static']:>+8.2f} {L['hold']:>+8.2f} "
                  f"{L['hold']-L['static']:>+7.2f} {L['hold_better']}/{L['n']} [{lo:.2f}-{hi:.2f}]")
        print("  Δhold>0 = holding that lane to resolution would beat the static cut. hold>cut with")
        print("  a lower-bound above 0.5 = holding reliably (not luck) wins there.")

    # per-tape-direction: does 'hold when aligned' actually pay by tape bucket?
    if args.by_tape:
        buck = defaultdict(lambda: {"n": 0, "static": 0.0, "hold": 0.0})
        for c in rows:
            key = f"{c['tape_dir'] or '?'}|{'aligned' if _tape_aligned(c) else 'against/flat'}"
            b = buck[key]; b["n"] += 1; b["static"] += c["static_pnl"]; b["hold"] += c["hold_pnl"]
        print(f"\n  BY TAPE (dir | side-alignment)   [{('proxy' if tape_cov==0 else 'mixed')} tape]")
        print(f"  {'bucket':26s} {'n':>3} {'static$':>8} {'hold$':>8} {'Δhold':>7}")
        print("  " + "-" * 58)
        for k, b in sorted(buck.items(), key=lambda kv: (kv[1]["hold"] - kv[1]["static"]), reverse=True):
            print(f"  {k:26s} {b['n']:>3} {b['static']:>+8.2f} {b['hold']:>+8.2f} {b['hold']-b['static']:>+7.2f}")

    print("\n  read: a tape policy that beats static in $ AND concentrates its holds in the")
    print("        aligned/trending buckets is the exit brain to graduate to paper-shadow.\n")


if __name__ == "__main__":
    main()
