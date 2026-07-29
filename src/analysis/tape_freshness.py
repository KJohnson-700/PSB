"""Candidate-time tape freshness penalty — the fast, generic 'don't buy a stale/topped
entry' layer (operator-directed 2026-07-26; Codex root-cause: winning lanes turn into
losing lanes because the only adaptive layer reacts AFTER closes and is too slow).

Unlike the lane_tape_adapter (close-based, per-lane, needs samples, lags the turn) this
runs at CANDIDATE time on the indicators already computed this scan, so it reacts on the
very entry it is judging. Unlike the tape_arbitration gate (hard block, 5m/15m + chop
only) this is a GRADED penalty that covers every window and NEVER hard-blocks — so it
cannot re-choke frequency, which is the operator's hard constraint. It expresses one
idea: the further the immediate tape has ROLLED OVER against the side (own-TF momentum
decelerating/reversing) and the more EXHAUSTED the move (RSI stretched), the staler the
entry — so demand a bit more edge AND bet smaller, rather than refuse the trade.

Output is a single ``staleness`` in [0,1] mapped to:
  - ``edge_add``  = staleness * max_edge_add   (added to effective_min_edge)
  - ``size_mult`` = 1 - staleness * max_size_cut (multiplies notional, floored)

Pure and dependency-free so it is unit-testable and identical across bitcoin/eth/alt
call sites. All inputs optional/guarded — missing indicators => 0 penalty (no-op).
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _is_long(action: Optional[str]) -> Optional[bool]:
    a = str(action or "").upper()
    if a in ("BUY_YES", "YES", "LONG", "UP"):
        return True
    if a in ("BUY_NO", "NO", "SHORT", "DOWN"):
        return False
    return None


def _momentum_staleness(is_long: bool, macd: Any) -> float:
    """0 = momentum still in the side's favor; 1 = fully rolled over against it.

    Uses histogram sign + slope + crossover on the lane's OWN timeframe MACD.
    LONG wants a rising, positive histogram; SHORT wants a falling, negative one.
    """
    if macd is None:
        return 0.0
    hist = _f(getattr(macd, "histogram", 0.0))
    rising = bool(getattr(macd, "histogram_rising", False))
    xover = getattr(macd, "crossover", None)
    if is_long:
        if xover == "BEARISH_CROSS":
            return 1.0
        if hist < 0 and not rising:
            return 1.0            # below zero AND decelerating = clearly turned
        if hist < 0 or not rising:
            return 0.6            # one of the two = rolling over
        return 0.0               # positive AND rising = fresh
    else:
        if xover == "BULLISH_CROSS":
            return 1.0
        if hist > 0 and rising:
            return 1.0
        if hist > 0 or rising:
            return 0.6
        return 0.0


def _rsi_exhaustion(is_long: bool, rsi: Any, start: float, full: float) -> float:
    """0 below the exhaustion band, ramps to 1 at/after `full`.

    LONG exhausts as RSI climbs (start<full, e.g. 70->90). SHORT exhausts as RSI
    falls (start>full, e.g. 30->12); we ramp on the mirrored distance.
    """
    if rsi is None:
        return 0.0
    try:
        r = float(rsi)
    except (TypeError, ValueError):
        return 0.0
    if is_long:
        if full <= start:
            return 0.0
        if r <= start:
            return 0.0
        return min(1.0, (r - start) / (full - start))
    else:
        if full >= start:
            return 0.0
        if r >= start:
            return 0.0
        return min(1.0, (start - r) / (start - full))


def compute_freshness_penalty(
    *,
    action: Optional[str],
    own_macd: Any,
    rsi: Any,
    cfg: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Return the graded freshness penalty for one candidate.

    cfg keys (all optional, defaults chosen conservative):
      enabled (False), mode ('live'|'shadow'|'off'),
      momentum_weight (0.6), rsi_weight (0.4),
      max_edge_add (0.04), max_size_cut (0.5), size_floor (0.4),
      rsi_long_start (70), rsi_long_full (90),
      rsi_short_start (30), rsi_short_full (12).

    Returns {staleness, edge_add, size_mult, reasons[]}. When disabled/off or the side
    is unknown, returns a no-op (edge_add 0.0, size_mult 1.0). SHADOW returns the
    computed staleness/reasons but a no-op edge_add/size_mult (for logging only).
    """
    c = dict(cfg or {})
    out = {"staleness": 0.0, "edge_add": 0.0, "size_mult": 1.0, "reasons": []}
    if not bool(c.get("enabled", False)):
        return out
    mode = str(c.get("mode", "live") or "live").lower()
    if mode == "off":
        return out
    is_long = _is_long(action)
    if is_long is None:
        return out

    mom_w = _f(c.get("momentum_weight", 0.6), 0.6)
    rsi_w = _f(c.get("rsi_weight", 0.4), 0.4)
    mom = _momentum_staleness(is_long, own_macd)
    if is_long:
        rex = _rsi_exhaustion(is_long, rsi, _f(c.get("rsi_long_start", 70.0), 70.0),
                              _f(c.get("rsi_long_full", 90.0), 90.0))
    else:
        rex = _rsi_exhaustion(is_long, rsi, _f(c.get("rsi_short_start", 30.0), 30.0),
                              _f(c.get("rsi_short_full", 12.0), 12.0))

    staleness = mom_w * mom + rsi_w * rex
    staleness = max(0.0, min(1.0, staleness))
    out["staleness"] = round(staleness, 4)
    reasons = []
    if mom > 0:
        reasons.append(f"mom_roll={mom:.1f}")
    if rex > 0:
        reasons.append(f"rsi_exh={rex:.2f}")
    out["reasons"] = reasons

    if mode == "shadow" or staleness <= 0.0:
        return out

    max_edge_add = _f(c.get("max_edge_add", 0.04), 0.04)
    max_size_cut = _f(c.get("max_size_cut", 0.5), 0.5)
    size_floor = _f(c.get("size_floor", 0.4), 0.4)
    out["edge_add"] = round(staleness * max_edge_add, 5)
    out["size_mult"] = round(max(size_floor, 1.0 - staleness * max_size_cut), 4)
    return out
