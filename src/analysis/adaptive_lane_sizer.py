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
}


def _cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    c = dict(DEFAULTS)
    if config:
        blk = ((config.get("trading") or {}).get("adaptive_sizer")) or config.get("adaptive_sizer") or {}
        for k, v in (blk or {}).items():
            if k in c and v is not None:
                c[k] = v
    # validate numeric guardrails (Codex nit 1): keep 1.0 inside the band and alpha in [0,1]
    c["mult_floor"] = min(float(c["mult_floor"]), 1.0)
    c["mult_ceil"] = max(float(c["mult_ceil"]), 1.0)
    c["ema_alpha"] = max(0.0, min(float(c["ema_alpha"]), 1.0))
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
    STATE_PATH.write_text(json.dumps(state, indent=2))
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
