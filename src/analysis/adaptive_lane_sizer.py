"""Adaptive per-lane SIZER — single realized-P&L-driven size multiplier (SHADOW).

Replaces the sprawl of static/dead sizing knobs (the dead performance_feedback
kelly_mult path, scattered lane_max_notional_*, lane_direction_recovery_size_mult)
with ONE adaptive multiplier per (strategy|window|side), learned from RECENT
realized trade truth.

Design (operator-directed, Codex-scoped):
  - size DOWN losing lanes freely; size UP a lane ONLY with proven positive
    realized at adequate n (asymmetric min-n gate) — never size up on noise.
  - RECENT realized only (lookback by session), NEVER pooled all-history totals
    (stale sessions are poison).
  - per-lane, side-isolated. bounded multiplier [floor, ceil]. EMA-smoothed so it
    does not flip-flop tick-to-tick. EMA state persisted across restarts.
  - SHADOW by default: computes + logs the multiplier it WOULD apply, applies
    NOTHING to live size. Phase 2 (separate, operator-gated) wires an
    execution-time resolver to read the state file and scale the final notional,
    always clamped under the absolute max_position_size safety rail.

Exit/sizing changes cannot be ghost-validated — this is forward-test only, exactly
like lane_exit_policy.

Usage:
    python -m src.analysis.adaptive_lane_sizer            # recompute state (shadow)
    python -m src.analysis.adaptive_lane_sizer --print    # also print the table
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
SETTLED_PATH = ROOT / "data" / "calibration" / "trades_settled.jsonl"
STATE_PATH = ROOT / "data" / "calibration" / "adaptive_sizer_state.json"
SHADOW_LOG = ROOT / "data" / "calibration" / "adaptive_sizer_shadow.jsonl"

# ── defaults (mirrored into config block trading.adaptive_sizer) ──────────────
DEFAULTS = {
    "enabled": True,
    "mode": "shadow",          # shadow | live  (live is Phase 2, operator-gated)
    "lookback_sessions": 8,    # only the most recent N sessions feed the stats
    "min_n_down": 6,           # size DOWN a losing lane once n>=this
    "min_n_up": 12,            # size UP a winning lane only once n>=this (stricter)
    "mult_floor": 0.40,        # hardest a losing lane gets shrunk
    "mult_ceil": 1.60,         # most a winning lane gets grown
    "roi_scale": 0.35,         # ROI (avg_pnl/avg_cost) that maps to ~76% of the way to a bound
    "sensitivity": 0.60,       # how aggressively ROI moves the multiplier
    "ema_alpha": 0.30,         # new = alpha*target + (1-alpha)*prev
    "unproven_lane_mult": 1.0, # 2026-08-04 lever-2: brand-new/very-low-n lane (n<min_n_down) rides
                               # this reduced mult until it has data (default 1.0 = OFF, back-compat)
    # 2026-08-05 PROVEN-LANE CAP keys (Codex re-review FIX: MUST live in DEFAULTS or _cfg drops
    # them — the `if k in c` filter at line ~82 strips any config key not mirrored here, which
    # would make resolve_lane_cap a silent no-op). Default 0.0 = OFF (proven-cap disabled until
    # config sets proven_lane_max_usd>0); thresholds are the proven-winner bar.
    "proven_lane_max_usd": 0.0,  # $ ceiling a PROVEN lane may grow to (0 = feature off)
    "proven_wr_min": 0.55,       # min recent WR (fraction) to count a lane proven
    "proven_roi_min": 0.05,      # min recent ROI (avg_pnl/avg_cost) to count a lane proven
}


def _num(v: Any, default: float) -> float:
    """Coerce a config value to float, falling back to default on None/bad-string (Codex P2)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _sizer_block(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract trading.adaptive_sizer (or top-level adaptive_sizer) as a dict, tolerating a
    non-dict config or a non-dict parent block (Codex P1: never crash _cfg on malformed yaml)."""
    if not isinstance(config, dict):
        return {}
    trading = config.get("trading")
    blk = trading.get("adaptive_sizer") if isinstance(trading, dict) else None
    if not isinstance(blk, dict):
        blk = config.get("adaptive_sizer")
    return blk if isinstance(blk, dict) else {}


def _cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    c = dict(DEFAULTS)
    blk = _sizer_block(config)
    for k, v in blk.items():
        if k in c and v is not None:
            c[k] = v
    # validate numeric guardrails (Codex nit 1): keep 1.0 inside the band and alpha in [0,1]
    c["mult_floor"] = min(_num(c["mult_floor"], 0.40), 1.0)
    c["mult_ceil"] = max(_num(c["mult_ceil"], 1.0), 1.0)
    c["ema_alpha"] = max(0.0, min(_num(c["ema_alpha"], 0.30), 1.0))
    # 2026-08-04 WR-GATE (per-lane realized SELF-FLIP): nested block, ALLOWLISTED lanes only.
    # A structurally wrong-side lane (recent WR below a floor at n>=min_n) is driven to a
    # near-sitout mult that rides the ~$1 venue floor — it keeps trading (calibration data)
    # at ~0 risk and self-reverses as WR recovers. Kept OUT of DEFAULTS/params (it's a dict).
    _w = blk.get("wr_gate")
    c["wr_gate"] = _w if isinstance(_w, dict) else {}
    return c


def _load_rows(path: Path = SETTLED_PATH) -> List[Dict[str, Any]]:
    # line-by-line + skip bad lines (Codex nit 2): the settler rewrites this file,
    # so a mid-write read can hit a partial/corrupt final line.
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for l in path.read_text().splitlines():
        l = l.strip()
        if not l:
            continue
        try:
            out.append(json.loads(l))
        except (json.JSONDecodeError, ValueError):
            continue
    return out


def _recent(rows: List[Dict[str, Any]], lookback_sessions: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Keep only trades from the most recent `lookback_sessions` sessions.

    Order sessions by their newest trade `ts` (Codex nit 3) so 'recent' is robust
    to any manual append/backfill that breaks pure file order.
    """
    newest_ts: Dict[str, str] = {}
    for r in rows:
        sid = r.get("session_id")
        if not sid:
            continue
        t = str(r.get("ts") or "")
        if sid not in newest_ts or t > newest_ts[sid]:
            newest_ts[sid] = t
    ordered = sorted(newest_ts, key=lambda s: newest_ts[s])  # oldest -> newest
    recent = ordered[-lookback_sessions:] if lookback_sessions > 0 else ordered
    keep = set(recent)
    return [r for r in rows if r.get("session_id") in keep], list(recent)


def lane_key(r: Dict[str, Any]) -> str:
    return "%s|%s|%s" % (r.get("strategy", "?"), r.get("window", "?"), r.get("action", "?"))


def _target_mult(avg_pnl: float, avg_cost: float, n: int, c: Dict[str, Any]) -> Tuple[float, str]:
    """Map a lane's recent realized to a bounded target multiplier.

    Uses ROI = avg_pnl / avg_cost so lanes of different absolute size compare fairly.
    tanh keeps it smooth and bounded. Asymmetric n-gate: shrink losers at min_n_down,
    grow winners only at the stricter min_n_up.
    """
    if avg_cost <= 0:
        return 1.0, "no_cost_basis"
    # 2026-08-04 UNPROVEN-LANE CAP (operator GO — sizing-inversion fix). A brand-new / very-low-n
    # lane (n < min_n_down) trades at a REDUCED mult until it has enough data to size properly —
    # a fresh lane must never blow up at full base ($15) on n=3. AUDIT: the sizing-inversion big
    # losers (xrp 5m BUY_NO, sol 15m BUY_YES) were exactly n=3 lanes stuck at mult 1.00 because they
    # sat below min_n_down=4. Applies to WIN or LOSS (unknown => bet small). Default 1.0 = OFF.
    _unproven = _num(c.get("unproven_lane_mult", 1.0), 1.0)
    if _unproven < 1.0 and n < int(c["min_n_down"]):
        return _unproven, "unproven_lowN(n=%d<%d,mult=%.2f)" % (n, int(c["min_n_down"]), _unproven)
    roi = avg_pnl / avg_cost
    # smooth signed signal in (-1, 1)
    signal = math.tanh(roi / max(1e-6, float(c["roi_scale"])))
    if signal >= 0:  # winning lane -> size up
        if n < int(c["min_n_up"]):
            return 1.0, "win_below_min_n_up(n=%d<%d)" % (n, int(c["min_n_up"]))
        target = 1.0 + float(c["sensitivity"]) * signal * (float(c["mult_ceil"]) - 1.0)
        return max(1.0, min(target, float(c["mult_ceil"]))), "size_up"
    else:            # losing lane -> size down
        if n < int(c["min_n_down"]):
            return 1.0, "loss_below_min_n_down(n=%d<%d)" % (n, int(c["min_n_down"]))
        target = 1.0 + float(c["sensitivity"]) * signal * (1.0 - float(c["mult_floor"]))
        return max(float(c["mult_floor"]), min(target, 1.0)), "size_down"


def build(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    c = _cfg(config)
    rows_all = _load_rows()
    rows, recent_sessions = _recent(rows_all, int(c["lookback_sessions"]))

    prev_state = {}
    if STATE_PATH.exists():
        try:
            prev_state = {l["lane"]: l for l in json.loads(STATE_PATH.read_text()).get("lanes", [])}
        except Exception:
            prev_state = {}

    by_lane: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_lane.setdefault(lane_key(r), []).append(r)

    lanes: List[Dict[str, Any]] = []
    for lane, rs in by_lane.items():
        n = len(rs)
        pnls = [(r.get("actual_pnl") or 0.0) for r in rs]
        costs = [(r.get("cost_basis") or 0.0) for r in rs if (r.get("cost_basis") or 0.0) > 0]
        avg_pnl = sum(pnls) / n if n else 0.0
        avg_cost = (sum(costs) / len(costs)) if costs else 0.0
        wr = 100.0 * sum(1 for p in pnls if p > 0) / n if n else 0.0
        target, reason = _target_mult(avg_pnl, avg_cost, n, c)
        prev_mult = float((prev_state.get(lane) or {}).get("ema_mult", 1.0))
        alpha = float(c["ema_alpha"])
        ema = alpha * target + (1.0 - alpha) * prev_mult
        # clamp the SMOOTHED value too (Codex nit 1): a corrupt prev_state or a
        # narrowed band must never let ema_mult escape [floor, ceil].
        ema = round(max(float(c["mult_floor"]), min(ema, float(c["mult_ceil"]))), 4)
        # 2026-08-05 (Codex MED): a lane whose CURRENT target is not a fresh win (<=1.0) must never
        # stay oversized from a stale winner EMA — the wider mult_ceil=2.5 makes this bite. Clamp the
        # smoothed value to <=1.0 whenever the fresh target isn't >1.0 (loser / neutral / unproven).
        if target <= 1.0 and ema > 1.0:
            ema = 1.0
        # 2026-08-04 WR-GATE self-flip override. Applied AFTER the [mult_floor,ceil] clamp so
        # a sit-out can go BELOW mult_floor. Allowlisted lanes only (protects winners /
        # collection lanes from a global WR sweep). Only ever SHRINKS (min), never grows, and
        # self-reverses: when wr climbs back above the floor the override lifts and ema returns
        # to the ROI value on the next recompute. wr is a percent (0..100) here.
        wg = c.get("wr_gate") or {}
        if wg.get("enabled") and lane in set(wg.get("lanes") or []) and n >= int(_num(wg.get("min_n", 8), 8)):
            _sit_wr = 100.0 * _num(wg.get("sitout_wr", 0.35), 0.35)
            _dn_wr = 100.0 * _num(wg.get("downsize_wr", 0.45), 0.45)
            if wr < _sit_wr:
                _sm = _num(wg.get("sitout_mult", 0.10), 0.10)
                if ema > _sm:
                    ema = round(_sm, 4)
                    reason = "wr_sitout(wr=%.0f,n=%d)" % (wr, n)
            elif wr < _dn_wr:
                _dm = _num(wg.get("downsize_mult", 0.40), 0.40)
                if ema > _dm:
                    ema = round(_dm, 4)
                    reason = "wr_downsize(wr=%.0f,n=%d)" % (wr, n)
        strat, window, action = lane.split("|")
        lanes.append({
            "lane": lane, "strategy": strat, "window": window, "action": action,
            "n": n, "wr": round(wr, 1), "avg_pnl": round(avg_pnl, 3),
            "avg_cost": round(avg_cost, 3), "target_mult": round(target, 4),
            "ema_mult": ema, "reason": reason,
        })
    lanes.sort(key=lambda d: -d["ema_mult"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": c["mode"],
        "shadow_only": c["mode"] != "live",
        "lookback_sessions": int(c["lookback_sessions"]),
        "recent_sessions": recent_sessions,
        "settled_n_recent": len(rows),
        "params": {k: c[k] for k in ("min_n_down", "min_n_up", "mult_floor", "mult_ceil",
                                      "roi_scale", "sensitivity", "ema_alpha")},
        "lanes": lanes,
    }


def write(state: Dict[str, Any]) -> None:
    # Atomic write (Codex P3): in-process recompute + the 10-min out-of-process daemon both
    # write this file; a temp-file + os.replace() makes each write atomic so a concurrent
    # reader never sees a half-written state (avoids the transient partial-line fallback).
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)
    row = {"ts": state["generated_at"], "mode": state["mode"],
           "lanes": {l["lane"]: l["ema_mult"] for l in state["lanes"]}}
    with SHADOW_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


# mtime-keyed cache so the hot sizing path (Phase 2) does no disk I/O or O(n)
# scan per order (Codex nit 4): rebuild the {lane: ema_mult} dict only when the
# state file actually changes.
_MULT_CACHE: Dict[str, Any] = {"mtime": None, "map": {}}


def _load_mult_map() -> Dict[str, float]:
    try:
        mtime = STATE_PATH.stat().st_mtime
    except OSError:
        return {}
    if _MULT_CACHE["mtime"] != mtime:
        try:
            st = json.loads(STATE_PATH.read_text())
            _MULT_CACHE["map"] = {l["lane"]: float(l.get("ema_mult", 1.0) or 1.0)
                                  for l in st.get("lanes", [])}
            _MULT_CACHE["mtime"] = mtime
        except Exception:
            return _MULT_CACHE.get("map", {})
    return _MULT_CACHE["map"]


def resolve_size_mult(config: Dict[str, Any], *, strategy: str, window: str, action: str) -> float:
    """Phase-2 hook (returns 1.0 in shadow mode). Reads the persisted state file.

    In shadow mode this ALWAYS returns 1.0 so it can never move real size; the
    state file still records what it WOULD have applied for forward-testing.
    """
    c = _cfg(config)
    if not bool(c["enabled"]) or c["mode"] != "live":
        return 1.0
    key = "%s|%s|%s" % (strategy, window, action)
    return float(_load_mult_map().get(key, 1.0))


# Proven-lane record cache (mtime-keyed, same pattern as _load_mult_map).
_REC_CACHE: Dict[str, Any] = {"mtime": None, "map": {}}


def _load_lane_records() -> Dict[str, Dict[str, Any]]:
    try:
        mtime = STATE_PATH.stat().st_mtime
    except OSError:
        return {}
    if _REC_CACHE["mtime"] != mtime:
        try:
            st = json.loads(STATE_PATH.read_text())
            _REC_CACHE["map"] = {l["lane"]: l for l in st.get("lanes", [])}
            _REC_CACHE["mtime"] = mtime
        except Exception:
            return _REC_CACHE.get("map", {})
    return _REC_CACHE["map"]


def resolve_lane_cap(config: Dict[str, Any], *, strategy: str, window: str, action: str) -> float:
    """2026-08-05 PROVEN-LANE CAP. Return proven_lane_max_usd if this lane is PROVEN
    (n>=min_n_up AND wr>=proven_wr_min AND ROI>=proven_roi_min), else 0.0 (=> caller uses
    the normal max_position_size). Lets a proven winner grow PAST the uniform $ cap while
    losers/unproven stay capped (the winners-too-small / sizing-inversion fix). Returns 0.0
    unless enabled + mode==live (no-op in shadow), so it can never lift the cap by accident.
    """
    c = _cfg(config)
    if not bool(c["enabled"]) or c["mode"] != "live":
        return 0.0
    pmax = _num(c.get("proven_lane_max_usd", 0.0), 0.0)
    if pmax <= 0:
        return 0.0
    rec = _load_lane_records().get("%s|%s|%s" % (strategy, window, action))
    if not rec:
        return 0.0
    n = int(rec.get("n", 0) or 0)
    wr = float(rec.get("wr", 0.0) or 0.0)  # percent 0..100
    avg_pnl = float(rec.get("avg_pnl", 0.0) or 0.0)
    avg_cost = float(rec.get("avg_cost", 0.0) or 0.0)
    roi = (avg_pnl / avg_cost) if avg_cost > 0 else 0.0
    wr_min = 100.0 * _num(c.get("proven_wr_min", 0.55), 0.55)
    roi_min = _num(c.get("proven_roi_min", 0.05), 0.05)
    if n >= int(c["min_n_up"]) and wr >= wr_min and roi >= roi_min:
        return float(pmax)
    return 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="do_print")
    args = ap.parse_args()
    import yaml
    cfg_path = ROOT / "config" / "settings.yaml"
    config = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    state = build(config)
    write(state)
    print("Wrote %s — %d lanes (mode=%s, recent %d sessions, n=%d)" % (
        STATE_PATH, len(state["lanes"]), state["mode"],
        state["lookback_sessions"], state["settled_n_recent"]))
    if args.do_print:
        print("\n%-26s %3s %5s %8s %8s %8s %-24s" % (
            "lane", "n", "wr%", "avgPnl", "target", "EMAmult", "reason"))
        for l in state["lanes"]:
            print("%-26s %3d %5.0f %8.3f %8.3f %8.3f %-24s" % (
                l["lane"], l["n"], l["wr"], l["avg_pnl"],
                l["target_mult"], l["ema_mult"], l["reason"]))


if __name__ == "__main__":
    main()
