"""Tape-state MAP — Phase 1, SHADOW / observe-only (2026-08-02, operator directive).

The operator's core problem: gates were being fit to ONE tape (a bull) and broke when the
tape flipped. The fix is a two-layer design — (1) an explicit, observable MAP of the current
tape per asset, and (2) behavior that shifts with it. This module is layer (1), Phase 1:
compute a per-asset mechanical tape state each scan and append it to
``data/calibration/tape_map.jsonl``. **NOTHING trades on this yet** — it is the map we validate
(does it label the bull correctly? would it have flagged the flips we broke on?) BEFORE any
behavior hangs off it (Phase 2).

State per asset (computed from the asset's OWN indicators — no BTC input; alts self-classify):
  - DIRECTION  : UP / DOWN / FLAT   (vote across MACD 5m/15m/1h signs, EMA stack, trend_direction)
  - STRENGTH   : 0..1               (trend_strength; how decisively it's trending)
  - VOLATILITY : atr% + percentile  (rolling per-asset, so "high/low vol" is relative to itself)
  - CONFIDENCE : 0..1               (agreement of the direction signals x strength)

Pure/robust: every input is optional (getattr fallbacks), never raises into a scan loop, and
dedupes to ~once per asset per cycle. Fail-silent by construction.
"""

from __future__ import annotations

import json
import math
import os
import time as _time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock
from typing import Any, Optional

_CALIB_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
_MAP_PATH = _CALIB_DIR / "tape_map.jsonl"
_VETO_SHADOW_PATH = _CALIB_DIR / "tape_side_veto_shadow.jsonl"

# Rolling per-asset ATR% history so the volatility bucket is RELATIVE to each asset's own
# recent range (a "high vol" read means high for THIS asset, not an absolute threshold).
_ATR_HIST: dict[str, deque] = defaultdict(lambda: deque(maxlen=600))
_LAST_LOG: dict[str, float] = {}
# In-memory cache of the most-recent computed tape state per asset. Populated by
# snapshot_and_log each cycle so latest_tape_state() is a dict lookup with ZERO file reads
# (reading tape_map.jsonl per scan would be the jsonl-reread churn that ballooned RSS before).
_LAST_STATE: dict[str, dict] = {}
_LOCK = Lock()


def _f(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        return v if math.isfinite(v) else None  # reject NaN/Inf (would break strict JSON)
    except (TypeError, ValueError):
        return None


def _sign(x: Optional[float]) -> int:
    if x is None:
        return 0
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _macd_sign(m: Any) -> int:
    """MACD objects expose .histogram; a bare number is used directly."""
    if m is None:
        return 0
    h = getattr(m, "histogram", None)
    if h is None:
        h = m  # maybe a plain number
    return _sign(_f(h))


def compute_tape_state(
    asset: str,
    *,
    current_price: Any = None,
    atr_14: Any = None,
    trend_direction: Any = None,
    trend_strength: Any = None,
    macd_5m: Any = None,
    macd_15m: Any = None,
    macd_1h: Any = None,
    rsi_14: Any = None,
    ema_9: Any = None,
    ema_21: Any = None,
    ema_50: Any = None,
    aux_dir: Any = None,
    now: Optional[float] = None,
) -> dict:
    """Compute the tape state from whatever indicators are provided (all optional)."""
    m5, m15, m1h = _macd_sign(macd_5m), _macd_sign(macd_15m), _macd_sign(macd_1h)
    macd_net = m5 + m15 + m1h  # -3..3

    # EMA stack direction (only when the full stack + price are present, e.g. alts; eth omits).
    ema_dir = 0
    cp, e9, e21, e50 = _f(current_price), _f(ema_9), _f(ema_21), _f(ema_50)
    if None not in (cp, e9, e21, e50):
        if cp > e21 > e50 and e9 >= e21:
            ema_dir = 1
        elif cp < e21 < e50 and e9 <= e21:
            ema_dir = -1

    # Assets without an EMA stack (e.g. BTC, which uses a sabre/MA trend instead) can supply a
    # native MA-trend vote via aux_dir in {-1,0,1}. It fills the EMA-vote slot so they still get
    # a 3rd direction vote (macd + MA-trend + htf) rather than just macd + htf — keeping the
    # 3-term dscore and +/-2 threshold identical to the alts.
    if ema_dir == 0 and aux_dir is not None:
        ema_dir = _sign(_f(aux_dir))

    # Explicit trend label when the service provides one.
    td = str(trend_direction or "").upper()
    td_dir = 1 if td in ("UP", "BULLISH") else (-1 if td in ("DOWN", "BEARISH") else 0)

    # Direction vote: MACD consensus + EMA stack + trend label, each in {-1,0,1}.
    dscore = _sign(macd_net) + ema_dir + td_dir  # -3..3
    if dscore >= 2:
        direction = "UP"
    elif dscore <= -2:
        direction = "DOWN"
    else:
        direction = "FLAT"

    strength = _f(trend_strength)
    strength = max(0.0, min(1.0, strength)) if strength is not None else None

    # Volatility: atr% + rolling percentile vs this asset's own recent history.
    atr_pct = None
    vol_pctile = None
    vol_bucket = None
    apct = _f(atr_14)
    if apct is not None and cp not in (None, 0):
        atr_pct = apct / cp
        hist = _ATR_HIST[asset]
        hist.append(atr_pct)
        if len(hist) >= 5:
            vol_pctile = round(sum(1 for h in hist if h <= atr_pct) / len(hist), 3)
            vol_bucket = "high" if vol_pctile >= 0.70 else ("low" if vol_pctile <= 0.30 else "mid")

    # Confidence: how much the (nonzero) direction signals agree, blended with strength.
    sigs = [s for s in (_sign(macd_net), ema_dir, td_dir) if s]
    agree = (abs(sum(sigs)) / len(sigs)) if sigs else 0.0  # 1.0 = unanimous
    conf = 0.6 * agree + 0.4 * (strength if strength is not None else 0.0)

    return {
        "ts": now if now is not None else 0.0,
        "asset": asset,
        "direction": direction,
        "dscore": dscore,
        "strength": strength,
        "vol_pct": (round(atr_pct, 6) if atr_pct is not None else None),
        "vol_pctile": vol_pctile,
        "vol_bucket": vol_bucket,
        "confidence": round(conf, 3),
        "macd_signs": [m5, m15, m1h],
        "ema_dir": ema_dir,
        "trend_dir_label": td or None,
        "rsi_14": _f(rsi_14),
        "price": cp,
    }


def snapshot_and_log(asset: str, *, min_interval_s: float = 30.0, **indicators: Any) -> Optional[dict]:
    """Compute + append one tape-state row for ``asset``. Deduped to ~once per asset per
    cycle. NEVER raises (fail-silent) — safe to call from inside a scan loop. Returns the
    state dict on write, else None (deduped or error)."""
    try:
        now = _time.time()
        with _LOCK:
            if now - _LAST_LOG.get(asset, 0.0) < min_interval_s:
                return None
            _LAST_LOG[asset] = now
        st = compute_tape_state(asset, now=now, **indicators)
        _LAST_STATE[asset] = st  # in-memory cache for latest_tape_state() (no file re-read)
        try:
            _line = json.dumps(st) + "\n"
            with _LOCK:  # serialize appends so concurrent asset lines never interleave
                _CALIB_DIR.mkdir(parents=True, exist_ok=True)
                with open(_MAP_PATH, "a") as fh:
                    fh.write(_line)
        except Exception:
            return None
        return st
    except Exception:
        return None


def latest_tape_state(asset: str) -> Optional[dict]:
    """Most-recent computed tape state for ``asset`` from the in-memory cache — a dict lookup,
    NO file read (avoids the per-scan jsonl-reread that ballooned RSS). Returns None until the
    first snapshot_and_log() for the asset this process. Fail-open."""
    try:
        return _LAST_STATE.get(asset)
    except Exception:
        return None


def log_side_veto_shadow(**fields: Any) -> None:
    """Observe-only: record what a tape-adaptive side-veto WOULD do (would_veto) for an entry,
    without blocking it — so we can measure (offline, joined to the trade outcome) whether the
    veto correctly catches wrong-direction entries before it is ever made active. Fail-silent."""
    try:
        fields.setdefault("ts", _time.time())
        _line = json.dumps(fields) + "\n"
        with _LOCK:
            _CALIB_DIR.mkdir(parents=True, exist_ok=True)
            with open(_VETO_SHADOW_PATH, "a") as fh:
                fh.write(_line)
    except Exception:
        return None
