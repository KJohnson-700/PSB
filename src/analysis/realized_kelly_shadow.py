"""realized_kelly_shadow — rebuild the old Kelly *size range* on REALIZED data.

Why this exists (2026-08-08, operator-directed):
    The bot wins ~60% of trades but bleeds, because the payoff geometry is upside
    down: dollar-wins are ~0.49x dollar-losses. The old Kelly sizer kept dollar-wins
    ~2.5x losses by sizing across a RANGE keyed on (back-tested) confidence. When the
    back-tests were removed (~May) that confidence signal died and nothing replaced it
    for *sizing* — the live adaptive_lane_sizer scales by an ROI multiplier, not a
    proper Kelly f* = (p*b - q)/b that uses BOTH realized win-prob p AND realized
    payoff b. The empirical result: sizing on WR alone would over-fund the favorite
    band (78% WR but b=0.21, -$34 tails); sizing on est_prob/stated_edge is noise.

    The one signal that actually discriminates is the lane's realized posterior
    (monotonic 23%->86% WR across buckets). This module recomputes a Kelly-fraction
    size RANGE from realized per-lane (p, b) and logs what it WOULD stake — a pure
    SHADOW. It never moves real size (mode defaults to "shadow", resolve() returns
    1.0 unless explicitly flipped to live). Prove the walk-forward reweight goes green
    vs the live baseline BEFORE anyone flips mode -> live.

Sources / conventions mirror adaptive_lane_sizer.py so a future live flip is a drop-in:
    lane key       = "strategy|window|action"
    realized ledger = data/calibration/trades.jsonl (`pnl`, INCLUDES stop-cuts)
                      NOT trades_settled.jsonl — see REALIZED_PATH note below
    shadow log     = data/calibration/realized_kelly_shadow.jsonl  (append-only)
    state file     = data/calibration/realized_kelly_state.json    (atomic replace)

CLI:
    python -m src.analysis.realized_kelly_shadow --replay      # walk-forward proof, no writes
    python -m src.analysis.realized_kelly_shadow               # one shadow dump (state + log line)
    python -m src.analysis.realized_kelly_shadow --loop 600    # daemon: dump every 600s (nohup)
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
# CANONICAL SOURCE = the REALIZED exit ledger (trades.jsonl `pnl`), which includes
# stop-cuts and is what the bankroll actually saw. Do NOT use trades_settled.jsonl:
# the settler never re-settles stopped trades, so that file OMITS the stop losses and
# shows every lane ~100% WR — sizing off it would fund the exact lanes that bleed.
REALIZED_PATH = ROOT / "data" / "calibration" / "trades.jsonl"
SETTLED_PATH = REALIZED_PATH  # back-compat alias (older callers / --source default)
STATE_PATH = ROOT / "data" / "calibration" / "realized_kelly_state.json"
SHADOW_LOG = ROOT / "data" / "calibration" / "realized_kelly_shadow.jsonl"

# Beta prior for the realized win-prob (Laplace-style shrink so a 3-0 lane isn't p=1.0).
PRIOR_A = 1.0
PRIOR_B = 1.0

_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    # "shadow" => resolve() ALWAYS returns 1.0 (cannot move real size). Only "live"
    # lets the multiplier bite, and only after a proven walk-forward reweight.
    "mode": "shadow",
    "lookback_sessions": 12,     # fallback ONLY when no era_months/era_from_date is set
    # ERA SELECTION (see _era). Both are unioned. Empty => fall back to lookback_sessions.
    "era_months": [],            # e.g. ["2026-06","2026-07"] — whole clean months
    "era_from_date": "",         # e.g. "2026-08-13" — everything on/after this date
    # ASYMMETRIC n-gate (mirrors adaptive_lane_sizer min_n_down=6 / min_n_up=12, and the
    # operator's durable rule: quick to cut a loser, slow to grow a winner). min_n stays
    # as the back-compat floor when these are unset.
    "min_n_up": 12,              # a lane may be sized ABOVE 1.0x only at n>=this
    "min_n_down": 6,             # a lane may be starved below 1.0x at n>=this
    "min_n": 8,                  # below this a lane is UNPROVEN -> neutral 1.0 (no starve, no boost)
    "kelly_cap": 0.5,            # half-Kelly safety clamp on the raw fraction
    "ref_fraction": 0.12,        # the Kelly fraction that maps to 1.0x base size
    "mult_floor": 0.15,          # a Kelly<=0 lane is starved to this (not 0, so it keeps logging data)
    "mult_ceil": 2.0,            # a strong +Kelly lane can reach 2.0x base
    "b_cap": 5.0,                # clamp payoff ratio (a lane with ~0 losses shouldn't blow up)
    "min_losses_for_b": 2,       # below this, fall back to the global payoff ratio for b
    # 2026-08-15 STALENESS CEILING. If realized_kelly_state.json is older than this,
    # resolve() returns neutral 1.0 for EVERY lane rather than sizing off a dead era.
    # 0 disables the guard (not recommended in live).
    "max_state_age_hours": 24.0,
}


def _num(v: Any, default: float) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    # 2026-08-15 (Codex P0): read ``trading.realized_kelly_sizer`` FIRST, top-level only as a
    # fallback. The module originally read top-level ONLY, so the natural config placement
    # (`trading.realized_kelly_sizer.mode: live`, beside adaptive_sizer) would have been read as
    # an empty block -> mode defaulted to "shadow" -> resolve() returned 1.0 for every lane. That
    # is a SILENT no-op: the bot logs nothing, sizing is unchanged, and the flip looks applied.
    # This was one of FOUR independent reasons the 2026-08-08 sizer never moved a single dollar.
    c = dict(_DEFAULTS)
    block: Dict[str, Any] = {}
    if isinstance(config, dict):
        _trading = config.get("trading")
        if isinstance(_trading, dict):
            block = _trading.get("realized_kelly_sizer") or {}
        if not block:
            block = config.get("realized_kelly_sizer") or {}
        if not isinstance(block, dict):
            block = {}
    for k, v in block.items():
        if k in c and v is not None:
            c[k] = v
    return c


def _load_rows(path: Path = SETTLED_PATH) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):  # skip arrays/strings/etc (Codex MED: _pnl assumes .get)
            out.append(obj)
    return out


def _era(rows: List[Dict[str, Any]], c: Dict[str, Any]) -> List[Dict[str, Any]]:
    """ERA SELECTOR (2026-08-15, operator directive) — supersedes the plain N-session
    lookback when ``era_months`` or ``era_from_date`` is configured.

    WHY: ``lookback_sessions: 12`` resolved to 08-11..08-14 — 100% of the August bug
    era — while 4,281 rows of clean Jun+Jul sat unused. The two eras give INVERTED
    verdicts (eth|5m|BUY_YES = 2.00x clean vs 0.15x August), so the lookback silently
    decided the entire sizing outcome. August-only also starved 8 of 9 lanes to the
    floor, i.e. a pure downsizer, which is the opposite of the operator's intent.

    ⛔ SELECTION IS BY ERA BOUNDARY, NEVER BY SESSION PROFITABILITY. Filtering to
    winning sessions is survivorship bias with teeth: b = avgWin/avgLoss, so dropping
    losing sessions inflates b -> inflates f* -> inflates stake. The bar for excluding
    an era is "the machine was demonstrably different/broken", not "it lost money".
    Losing sessions INSIDE an included era are kept — they are what defines b.
    """
    months = set(c.get("era_months") or [])
    from_date = str(c.get("era_from_date") or "")
    if not months and not from_date:
        return _recent(rows, int(c["lookback_sessions"]))
    out: List[Dict[str, Any]] = []
    for r in rows:
        t = str(r.get("ts") or r.get("timestamp") or "")
        if not t:
            continue
        if months and t[:7] in months:
            out.append(r)
        elif from_date and t[:10] >= from_date:
            out.append(r)
    return out


def _recent(rows: List[Dict[str, Any]], lookback_sessions: int) -> List[Dict[str, Any]]:
    """Keep only the most recent `lookback_sessions` sessions (by each session's newest ts)."""
    if lookback_sessions <= 0:
        return list(rows)
    newest_ts: Dict[str, str] = {}
    for r in rows:
        sid = r.get("session_id")
        if not sid:
            continue
        t = str(r.get("ts") or "")
        if sid not in newest_ts or t > newest_ts[sid]:
            newest_ts[sid] = t
    ordered = sorted(newest_ts, key=lambda s: newest_ts[s])
    keep = set(ordered[-lookback_sessions:])
    # Codex LOW: keep rows that lack a session_id (legacy/partial) rather than silently
    # dropping them — a falsy session_id is never in `keep`, so guard it explicitly.
    return [r for r in rows if (not r.get("session_id")) or r.get("session_id") in keep]


def lane_key(r: Dict[str, Any]) -> str:
    return "%s|%s|%s" % (r.get("strategy", "?"), r.get("window", "?"), r.get("action", "?"))


def _pnl(r: Dict[str, Any]) -> Optional[float]:
    v = r.get("actual_pnl")
    if v is None:
        v = r.get("pnl")  # trades.jsonl fallback
    if v is None:
        return None
    # Codex LOW: a non-numeric pnl must be SKIPPED (return None), not coerced to 0.0 —
    # a fake 0.0 would inflate n, dilute p, and prematurely satisfy min_n.
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def kelly_fraction(p: float, b: float, cap: float) -> float:
    """Kelly f* = (p*b - (1-p)) / b, clamped to [0, cap]. 0 when the lane is -EV."""
    if b <= 0:
        return 0.0
    f = (p * b - (1.0 - p)) / b
    if not math.isfinite(f) or f <= 0:
        return 0.0
    return min(f, cap)


def _global_payoff(rows: List[Dict[str, Any]]) -> float:
    wins = [pl for pl in (_pnl(r) for r in rows) if pl is not None and pl > 0]
    loss = [-pl for pl in (_pnl(r) for r in rows) if pl is not None and pl < 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(loss) / len(loss) if loss else 1e-9
    return (aw / al) if al > 0 else 0.0


def lane_stats(rows: List[Dict[str, Any]], c: Dict[str, Any],
               global_b: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """Per-lane realized (p, b, n, avg_cost) from settled rows, with shrink + fallbacks."""
    if global_b is None:
        global_b = _global_payoff(rows)
    by_lane: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        pl = _pnl(r)
        if pl is None:
            continue
        by_lane.setdefault(lane_key(r), []).append(r)

    out: Dict[str, Dict[str, Any]] = {}
    for lane, rs in by_lane.items():
        pnls = [_pnl(r) for r in rs]
        pnls = [x for x in pnls if x is not None]
        n = len(pnls)
        wins = [x for x in pnls if x > 0]
        loss = [-x for x in pnls if x < 0]
        # realized win-prob with Beta(1,1) shrink so a tiny sample isn't overconfident
        p = (len(wins) + PRIOR_A) / (n + PRIOR_A + PRIOR_B) if n else PRIOR_A / (PRIOR_A + PRIOR_B)
        aw = sum(wins) / len(wins) if wins else 0.0
        al = sum(loss) / len(loss) if loss else 0.0
        if len(loss) >= int(c["min_losses_for_b"]) and al > 0:
            b = aw / al
        else:
            # too few losses to trust the ratio -> use the global payoff (conservative)
            b = global_b if global_b > 0 else 1.0
        b = max(0.0, min(b, float(c["b_cap"])))
        costs = [(_num(r.get("cost_basis"), 0.0) or _num(r.get("notional"), 0.0)) for r in rs]
        costs = [x for x in costs if x > 0]
        avg_cost = sum(costs) / len(costs) if costs else 0.0
        out[lane] = {
            "n": n, "wins": len(wins), "losses": len(loss),
            "p": round(p, 4), "avg_win": round(aw, 3), "avg_loss": round(al, 3),
            "b": round(b, 4), "avg_cost": round(avg_cost, 3),
        }
    return out


def size_mult(stat: Dict[str, Any], c: Dict[str, Any]) -> Tuple[float, float, str]:
    """Map a lane's realized (p, b) to a size multiplier in [floor, ceil].

    Returns (mult, kelly_fraction, reason). An UNPROVEN lane (n<min_n) is neutral
    1.0 — we neither starve nor boost until it has earned an opinion.
    """
    n = int(stat.get("n", 0))
    _n_down = int(c.get("min_n_down", c["min_n"]) or c["min_n"])
    _n_up = int(c.get("min_n_up", c["min_n"]) or c["min_n"])
    if n < min(_n_down, _n_up):
        return 1.0, 0.0, "unproven(n=%d<%d)" % (n, min(_n_down, _n_up))
    f = kelly_fraction(float(stat["p"]), float(stat["b"]), float(c["kelly_cap"]))
    ref = float(c["ref_fraction"]) or 0.12
    mult = f / ref
    mult = max(float(c["mult_floor"]), min(mult, float(c["mult_ceil"])))
    # ASYMMETRIC n-GATE (operator's durable sizing rule, mirrored from adaptive_lane_sizer):
    # be QUICK to cut a loser and SLOW to grow a winner. A lane with enough evidence to be
    # starved (n>=min_n_down) may not have enough to be BOOSTED (n>=min_n_up) — in that
    # window it is pinned to 1.0x rather than sized up on thin proof. Sizing is the LAST
    # knob and only after edge is proven; an over-sized thin winner is how a book blows up.
    if mult > 1.0 and n < _n_up:
        return 1.0, round(f, 4), "win_below_min_n_up(n=%d<%d)" % (n, _n_up)
    if mult < 1.0 and n < _n_down:
        return 1.0, round(f, 4), "loss_below_min_n_down(n=%d<%d)" % (n, _n_down)
    if f <= 0:
        return round(mult, 4), 0.0, "kelly<=0_starve(p=%.2f,b=%.2f)" % (stat["p"], stat["b"])
    return round(mult, 4), round(f, 4), "kelly(p=%.2f,b=%.2f,f=%.3f)" % (stat["p"], stat["b"], f)


def build(config: Optional[Dict[str, Any]] = None,
          rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    c = _cfg(config)
    all_rows = rows if rows is not None else _load_rows()
    recent = _era(all_rows, c)
    stats = lane_stats(recent, c)
    lanes: List[Dict[str, Any]] = []
    for lane, st in stats.items():
        mult, f, reason = size_mult(st, c)
        strat, window, action = (lane.split("|") + ["?", "?", "?"])[:3]
        lanes.append({
            "lane": lane, "strategy": strat, "window": window, "action": action,
            "n": st["n"], "wr": round(100.0 * st["wins"] / st["n"], 1) if st["n"] else 0.0,
            "p": st["p"], "b": st["b"], "avg_win": st["avg_win"], "avg_loss": st["avg_loss"],
            "kelly_fraction": f, "mult": mult, "reason": reason,
        })
    lanes.sort(key=lambda d: -d["mult"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": c["mode"],
        "shadow_only": c["mode"] != "live",
        "lookback_sessions": int(c["lookback_sessions"]),
        "settled_n_recent": len(recent),
        "params": {k: c[k] for k in ("min_n", "kelly_cap", "ref_fraction",
                                     "mult_floor", "mult_ceil", "b_cap")},
        "lanes": lanes,
    }


def write(state: Dict[str, Any]) -> None:
    """Atomic state write + append one compact line to the shadow log (mirrors adaptive sizer)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)
    row = {"ts": state["generated_at"], "mode": state["mode"],
           "lanes": {l["lane"]: l["mult"] for l in state["lanes"]}}
    with SHADOW_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


_STATE_CACHE: Dict[str, Any] = {"mtime": None, "mults": {}, "age_ok": False}


def _state_mults(max_age_h: float) -> Dict[str, float]:
    """mtime-keyed cache of lane->mult (Codex P1: resolve() ran per-entry file IO + a
    linear scan of every lane). Re-reads ONLY when realized_kelly_state.json changes.

    STALENESS GUARD (Codex P0): a state file older than ``max_state_age_hours`` returns
    EMPTY, so every lane resolves neutral 1.0 instead of being sized off a dead era.
    This is not hypothetical — on 2026-08-15 the on-disk state was from 08-09 and
    contained ZERO eth lanes while eth|15m|BUY_NO was carrying the entire book. Sizing
    off it would have starved lanes that no longer existed and boosted a lane that was
    PAUSED. Mirrors the hydrate ceiling: stale adaptive state must go NEUTRAL, not act.
    """
    try:
        mtime = STATE_PATH.stat().st_mtime
    except Exception:
        return {}
    if _STATE_CACHE["mtime"] != mtime:
        try:
            st = json.loads(STATE_PATH.read_text())
            _STATE_CACHE["mults"] = {
                str(l.get("lane")): float(l.get("mult", 1.0))
                for l in st.get("lanes", [])
                if l.get("lane")
            }
        except Exception:
            _STATE_CACHE["mults"] = {}
        _STATE_CACHE["mtime"] = mtime
    if max_age_h > 0:
        import time as _t
        if (_t.time() - mtime) > (max_age_h * 3600.0):
            return {}
    return _STATE_CACHE["mults"]


def resolve(config: Dict[str, Any], *, strategy: str, window: str, action: str) -> float:
    """Live hook (NO-OP in shadow). Returns 1.0 unless mode==live, so this module can
    never move real size until someone deliberately flips realized_kelly_sizer.mode.

    Any failure — missing state, unparseable state, STALE state, or an unknown lane —
    degrades to neutral 1.0. It can never return 0.0 and never raises into the execute
    path.
    """
    c = _cfg(config)
    if not bool(c["enabled"]) or c["mode"] != "live":
        return 1.0
    mults = _state_mults(float(c.get("max_state_age_hours", 24.0) or 0.0))
    if not mults:
        return 1.0
    try:
        return float(mults.get("%s|%s|%s" % (strategy, window, action), 1.0))
    except Exception:
        return 1.0


def replay(config: Optional[Dict[str, Any]] = None,
           source: Path = SETTLED_PATH) -> Dict[str, Any]:
    """WALK-FORWARD proof (no lookahead): for each settled trade in time order, size it
    by the Kelly mult computed from that lane's PRIOR trades only, then reweight its
    realized pnl by the mult. Assumes pnl scales ~linearly with stake (true for a paper
    fill at the same price) — stated as an assumption, not a guarantee.
    """
    c = _cfg(config)
    rows = _load_rows(source)
    rows = [r for r in rows if _pnl(r) is not None]
    rows.sort(key=lambda r: str(r.get("ts") or ""))

    hist: Dict[str, List[Dict[str, Any]]] = {}
    # Codex HIGH: global payoff fallback (for thin lanes) must use PRIOR trades only.
    # Maintain running win/loss aggregates and derive global_b from them each step.
    g_win_sum = g_win_n = g_loss_sum = g_loss_n = 0.0
    base_net = shadow_net = 0.0
    base_w = base_l = 0.0
    sh_w = sh_l = 0.0
    per_lane: Dict[str, List[float]] = {}
    applied = 0
    for r in rows:
        lane = lane_key(r)
        prior = hist.get(lane, [])
        aw = g_win_sum / g_win_n if g_win_n else 0.0
        al = g_loss_sum / g_loss_n if g_loss_n else 0.0
        global_b = (aw / al) if al > 0 else 1.0  # prior-only; neutral 1.0 until history exists
        st = lane_stats(prior, c, global_b=global_b).get(lane) if prior else None
        mult = 1.0
        if st:
            mult, _f, _reason = size_mult(st, c)
        pl = _pnl(r) or 0.0
        spl = pl * mult
        base_net += pl
        shadow_net += spl
        if pl > 0:
            base_w += pl
        elif pl < 0:
            base_l += -pl
        if spl > 0:
            sh_w += spl
        elif spl < 0:
            sh_l += -spl
        per_lane.setdefault(lane, [0.0, 0.0])
        per_lane[lane][0] += pl
        per_lane[lane][1] += spl
        if st:
            applied += 1
        hist.setdefault(lane, []).append(r)
        # update the running global aggregates AFTER sizing this trade (so t never sees itself)
        if pl > 0:
            g_win_sum += pl
            g_win_n += 1
        elif pl < 0:
            g_loss_sum += -pl
            g_loss_n += 1

    def payoff(w: float, l_: float) -> float:
        return (w / l_) if l_ > 0 else float("inf")

    return {
        "n_trades": len(rows),
        "n_sized_by_kelly": applied,
        "baseline_net": round(base_net, 2),
        "shadow_net": round(shadow_net, 2),
        "baseline_payoff_dollar": round(payoff(base_w, base_l), 2),
        "shadow_payoff_dollar": round(payoff(sh_w, sh_l), 2),
        "per_lane": {k: [round(v[0], 2), round(v[1], 2)] for k, v in
                     sorted(per_lane.items(), key=lambda x: x[1][0])},
    }


def _main() -> None:
    ap = argparse.ArgumentParser(description="realized-Kelly shadow sizer")
    ap.add_argument("--replay", action="store_true", help="walk-forward proof (no writes)")
    ap.add_argument("--source", default=str(SETTLED_PATH), help="settled trades path")
    ap.add_argument("--loop", type=int, default=0, help="daemon: dump every N seconds")
    args = ap.parse_args()

    if args.replay:
        res = replay(source=Path(args.source))
        print(json.dumps(res, indent=2))
        return

    if args.loop > 0:
        import time
        while True:
            try:
                write(build())
            except Exception as e:  # noqa: BLE001
                print("kelly-shadow dump error:", e)
            time.sleep(args.loop)
    else:
        state = build()
        write(state)
        print("wrote %s (%d lanes, mode=%s)" % (STATE_PATH.name, len(state["lanes"]), state["mode"]))


if __name__ == "__main__":
    _main()
