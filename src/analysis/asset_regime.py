"""Per-asset fast regime classifier (P1, 2026-07-03). OBSERVE-ONLY.

Computes a per-asset trend/chop/dead label every scan cycle (~60s) from the
same 5m klines the TA services already fetch — vs the 15-min global cron
tracker consumed by no_signal_gate (P0). P1 adds granularity (per asset) and
speed (per cycle); it does NOT gate anything yet. Labels are exposed in
ops_pulse scan stats and state TRANSITIONS are appended to
data/calibration/asset_regime.jsonl so the labels can be validated against
realized trade outcomes before any consumption is wired (same live-data bar
the P0 gate had to clear).

Classifier (dual-threshold hysteresis, per asset):
- efficiency ratio ER = |close[-1] - close[-N]| / sum(|bar-to-bar moves|), N=24
  (2h of 5m bars). High ER = directional/efficient tape, low = overlap/chop.
- vol = stdev of the last N 5m log-returns. Below the dead floor = nothing
  moving regardless of ER.
- states: dead (vol floor) > trend (ER high) > chop (default).
- hysteresis: enter trend at ER >= 0.30, leave below 0.22; enter dead at
  vol <= 0.00040, leave above 0.00060. Thresholds are STARTING GUESSES —
  tune from the validation join, do not trust until validated.

Thread-safety: registry guarded by a lock (strategies scan concurrently).
Failures never propagate — this layer must not affect scanning.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRANSITIONS_PATH = _REPO_ROOT / "data" / "calibration" / "asset_regime.jsonl"

ER_N = 24
ER_TREND_ENTER = 0.30
ER_TREND_EXIT = 0.22
DEAD_VOL_ENTER = 0.00040
DEAD_VOL_EXIT = 0.00060

_lock = threading.Lock()
_registry: dict = {}  # symbol -> {"state","er","vol_5m","since","updated"}


def _metrics(closes: list) -> tuple:
    c = [float(x) for x in closes[-(ER_N + 1):]]
    if len(c) < ER_N + 1 or any((x <= 0 or not math.isfinite(x)) for x in c):
        return None, None
    net = abs(c[-1] - c[0])
    path = sum(abs(b - a) for a, b in zip(c, c[1:]))
    er = (net / path) if path > 0 else 0.0
    rets = [math.log(b / a) for a, b in zip(c, c[1:])]
    # RMS (not stdev): a smooth steady drift has ~zero stdev but IS moving —
    # "dead" must mean no movement at all, so include the mean in the metric.
    vol = math.sqrt(sum(r * r for r in rets) / len(rets))
    return er, vol


def _next_state(prev: str, er: float, vol: float) -> str:
    # dead has priority; both bands use dual thresholds so labels can't flap
    if prev == "dead":
        if vol <= DEAD_VOL_EXIT:
            return "dead"
    elif vol <= DEAD_VOL_ENTER:
        return "dead"
    if prev == "trend":
        return "trend" if er >= ER_TREND_EXIT else "chop"
    return "trend" if er >= ER_TREND_ENTER else "chop"


def update(symbol: str, closes: list) -> None:
    """Feed the latest 5m closes for one asset. Never raises."""
    try:
        if not symbol or closes is None:
            return
        er, vol = _metrics(list(closes))
        if er is None:
            return
        try:  # P2 observe-only HMM piggyback (never affects P1 labels)
            from src.analysis import asset_regime_hmm as _hmm
            _hmm.update(symbol, closes)
        except Exception:
            pass
        now = time.time()
        with _lock:
            prev = _registry.get(symbol)
            prev_state = prev["state"] if prev else "chop"
            state = _next_state(prev_state, er, vol)
            since = prev["since"] if (prev and prev["state"] == state) else now
            _registry[symbol] = {
                "state": state,
                "er": round(er, 4),
                "vol_5m": round(vol, 6),
                "since": since,
                "updated": now,
            }
            changed = (prev is None) or (prev["state"] != state)
        if changed:
            logger.info(
                "ASSET_REGIME %s -> %s (er=%.3f vol5m=%.5f, was %s)",
                symbol, state, er, vol, prev_state if prev else "none",
            )
            try:
                _TRANSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
                with _lock, open(_TRANSITIONS_PATH, "a") as f:
                    f.write(json.dumps({
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                        "symbol": symbol,
                        "state": state,
                        "prev": prev_state if prev else None,
                        "er": round(er, 4),
                        "vol_5m": round(vol, 6),
                    }) + "\n")
            except Exception:
                pass
    except Exception as e:
        logger.debug("asset_regime.update(%s) failed: %s", symbol, e)


def get_state(symbol: str):
    """Latest snapshot for one asset, or None. Stale (>300s) returns None."""
    try:
        with _lock:
            d = _registry.get(symbol)
            if not d:
                return None
            if time.time() - d["updated"] > 300:
                return None
            out = dict(d)
        out["age_sec"] = round(time.time() - out.pop("updated"), 1)
        out["since_min"] = round((time.time() - out.pop("since")) / 60.0, 1)
        return out
    except Exception:
        return None
