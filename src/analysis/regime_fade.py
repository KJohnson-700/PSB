"""Per-lane regime fade filter — sit out a lane's mis-ranked mid-confidence band
when THAT lane's recent realized win rate has collapsed (its momentum edge
inverting in chop).

WHY (2026-06-21, validated per-lane on live data):
The momentum-based ``est_prob`` is mis-ranked in the MIDDLE and the inversion is
LANE-SPECIFIC — the alts are alt-native (HYPE off Hyperliquid, BNB its own), so a
single pooled signal is wrong. Per-lane band win-rate over recent settled trades
(predicted P(win) in ``[band_low, band_high)``):

  bnb 32% (n=34) | hype 38% (n=84) | xrp 35% (n=37) | btc 39% (n=23)  -> bleeding
  doge 50% (n=6) | sol 50% (n=4)                                       -> too thin

So each lane gets its OWN fade state: suppress that lane's mid-band entries only
while ITS band WR is below ``fade_below_wr``; release above ``recover_above_wr``
(hysteresis); protect the genuine >= band_high winners and leave < band_low alone.
Lanes with fewer than ``min_band_samples`` recent in-band trades stay inactive
(never suppress on thin data). Regime-adaptive: a lane that trends again wins its
band and the filter becomes a no-op for it (no frequency cut).

Source of truth = ``data/calibration/trades.jsonl`` (settled trades: ``strategy``
/ est_prob / side / win). Read-only, mtime+TTL+config cached. Default ON, opt-out
``regime_fade.enabled: false``. Suppressed entries ghost-logged by the caller.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CALIBRATION_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
DEFAULT_TRADES_PATH = _CALIBRATION_DIR / "trades.jsonl"
DEFAULT_STATUS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "runtime"
    / "regime_fade_state.json"
)

_SHORT_SIDES = {"BUY_NO", "NO", "SHORT", "SELL_YES", "DOWN"}


@dataclass(frozen=True)
class RegimeFadeConfig:
    """Parsed ``regime_fade`` config block."""

    enabled: bool = True
    band_low: float = 0.45
    band_high: float = 0.65
    window_trades: int = 60       # most-recent settled trades PER LANE
    min_band_samples: int = 8     # need >= this many in-band trades for a lane to act
    fade_below_wr: float = 0.48
    recover_above_wr: float = 0.53
    max_trade_age_hours: float = 48.0
    action: str = "sit_out"       # "sit_out" | "raise_bar"
    raise_bar_min_edge_bonus: float = 0.08
    cache_ttl_sec: float = 60.0

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "RegimeFadeConfig":
        raw = raw or {}

        def _f(key: str, default: float) -> float:
            try:
                return float(raw.get(key, default))
            except (TypeError, ValueError):
                return default

        def _i(key: str, default: int) -> int:
            try:
                return int(raw.get(key, default))
            except (TypeError, ValueError):
                return default

        action = str(raw.get("action", "sit_out")).strip().lower()
        if action not in ("sit_out", "raise_bar"):
            action = "sit_out"
        return cls(
            enabled=bool(raw.get("enabled", True)),
            band_low=_f("band_low", 0.45),
            band_high=_f("band_high", 0.65),
            window_trades=max(1, _i("window_trades", 60)),
            min_band_samples=max(1, _i("min_band_samples", 8)),
            fade_below_wr=_f("fade_below_wr", 0.48),
            recover_above_wr=_f("recover_above_wr", 0.53),
            max_trade_age_hours=_f("max_trade_age_hours", 48.0),
            action=action,
            raise_bar_min_edge_bonus=_f("raise_bar_min_edge_bonus", 0.08),
            cache_ttl_sec=_f("cache_ttl_sec", 60.0),
        )

    def in_band(self, p_win: Optional[float]) -> bool:
        return p_win is not None and self.band_low <= p_win < self.band_high


@dataclass
class RegimeFadeState:
    """Per-lane fade evaluation result."""

    lane: str = ""
    active: bool = False
    rolling_wr: Optional[float] = None
    n_band: int = 0
    n_window: int = 0
    band_low: float = 0.45
    band_high: float = 0.65
    fade_below_wr: float = 0.48
    recover_above_wr: float = 0.53
    action: str = "sit_out"
    reason: str = "init"
    computed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lane": self.lane,
            "active": self.active,
            "rolling_wr": (round(self.rolling_wr, 4) if self.rolling_wr is not None else None),
            "n_band": self.n_band,
            "n_window": self.n_window,
            "band_low": self.band_low,
            "band_high": self.band_high,
            "fade_below_wr": self.fade_below_wr,
            "recover_above_wr": self.recover_above_wr,
            "action": self.action,
            "reason": self.reason,
            "computed_at": self.computed_at,
        }


def predicted_p_win(side: Any, est_prob: Optional[float]) -> Optional[float]:
    """Predicted probability that the trade WINS (est_prob is P(YES))."""
    if est_prob is None:
        return None
    try:
        p = float(est_prob)
    except (TypeError, ValueError):
        return None
    if str(side).upper() in _SHORT_SIDES:
        return 1.0 - p
    return p


def lane_key(strategy: Any, window: Any, side: Any) -> str:
    """Composite per-(strategy, window, side) fade key.

    2026-06-21: the fade was keyed on strategy alone, which idled a whole asset's
    mid band across BOTH directions and ALL windows. The bleed is lane-specific
    (e.g. xrp 5m BUY_NO, sol 1h BUY_YES) — so judge and fade each (asset, window,
    side) on its OWN band WR, leaving the rest of that asset trading.
    """
    return "%s|%s|%s" % (
        str(strategy or "").strip(),
        str(window or "").strip(),
        str(side or "").strip().upper(),
    )


def _row_est_prob(row: Dict[str, Any]) -> Optional[float]:
    for key in ("calibrated_est_prob", "stated_est_prob", "est_prob", "raw_est_prob"):
        if row.get(key) is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return None


def _row_ts(row: Dict[str, Any]) -> Optional[datetime]:
    for key in ("closed_at", "ts", "opened_at"):
        val = row.get(key)
        if not val:
            continue
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _read_last_lines(path: Path, max_lines: int, max_bytes: int = 3_000_000) -> List[str]:
    """Read up to ``max_lines`` final lines without loading the whole file."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    read_bytes = min(size, max_bytes)
    try:
        with open(path, "rb") as fh:
            fh.seek(size - read_bytes)
            chunk = fh.read(read_bytes)
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="ignore").splitlines()
    if read_bytes < size and lines:
        lines = lines[1:]  # drop the partial first line
    return lines[-max_lines:]


def _state_for_lane(
    cfg: RegimeFadeConfig, lane: str, band: List[Tuple[bool, float]], n_window: int,
    *, prev_active: bool, computed_at: str,
) -> RegimeFadeState:
    state = RegimeFadeState(
        lane=lane, band_low=cfg.band_low, band_high=cfg.band_high,
        fade_below_wr=cfg.fade_below_wr, recover_above_wr=cfg.recover_above_wr,
        action=cfg.action, n_window=n_window, n_band=len(band), computed_at=computed_at,
    )
    if len(band) < cfg.min_band_samples:
        state.active = False
        state.reason = f"insufficient_band_samples(n={len(band)}<{cfg.min_band_samples})"
        return state
    wins = sum(1 for (w, _p) in band if w)
    rolling_wr = wins / len(band)
    state.rolling_wr = rolling_wr
    if prev_active:
        active = rolling_wr < cfg.recover_above_wr
    else:
        active = rolling_wr < cfg.fade_below_wr
    state.active = active
    if active:
        state.reason = (
            f"band[{cfg.band_low:.2f},{cfg.band_high:.2f})_wr={rolling_wr:.3f}<{cfg.fade_below_wr:.2f}"
            if not prev_active
            else f"band_wr={rolling_wr:.3f}<recover={cfg.recover_above_wr:.2f}(held)"
        )
    else:
        state.reason = f"band_wr={rolling_wr:.3f}>=recover={cfg.recover_above_wr:.2f}"
    return state


def _compute_states(
    cfg: RegimeFadeConfig,
    trades_path: Path,
    *,
    prev_active: Dict[str, bool],
    now: Optional[datetime] = None,
) -> Dict[str, RegimeFadeState]:
    now = now or datetime.now(timezone.utc)
    computed_at = now.isoformat(timespec="seconds")
    # Read enough tail to cover window_trades for the thick lanes; thin lanes get
    # all of their (few) rows regardless.
    raw_lines = _read_last_lines(trades_path, max_lines=cfg.window_trades * 15)
    cutoff = now.timestamp() - cfg.max_trade_age_hours * 3600.0

    per_lane: Dict[str, List[Tuple[float, bool, float]]] = defaultdict(list)
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict) or row.get("shadow_mode"):
            continue
        strat = row.get("strategy")
        win_sz = row.get("window") or row.get("window_size")
        side_tok = row.get("action") or row.get("side")
        if not strat or not win_sz or not side_tok:
            continue
        lane = lane_key(strat, win_sz, side_tok)
        win = row.get("win")
        if not isinstance(win, bool):
            continue
        est_prob = _row_est_prob(row)
        if est_prob is None:
            continue
        p_win = predicted_p_win(row.get("side") or row.get("action"), est_prob)
        if p_win is None:
            continue
        ts = _row_ts(row)
        if ts is None or ts.timestamp() < cutoff:
            continue
        per_lane[str(lane)].append((ts.timestamp(), bool(win), float(p_win)))

    states: Dict[str, RegimeFadeState] = {}
    for lane, rows in per_lane.items():
        rows.sort(key=lambda r: r[0])
        window = rows[-cfg.window_trades:]
        band = [(w, p) for (_ts, w, p) in window if cfg.in_band(p)]
        states[lane] = _state_for_lane(
            cfg, lane, band, len(window),
            prev_active=bool(prev_active.get(lane, False)), computed_at=computed_at,
        )
    return states


# --- process-local cache + per-lane hysteresis memory ---------------------

_cache_states: Optional[Dict[str, RegimeFadeState]] = None
_cache_at: float = 0.0
_cache_mtime: float = -1.0
_cache_fp: Optional[tuple] = None


def _inactive(lane: str, cfg: RegimeFadeConfig, reason: str) -> RegimeFadeState:
    return RegimeFadeState(
        lane=lane, active=False, reason=reason, action=cfg.action,
        band_low=cfg.band_low, band_high=cfg.band_high,
        fade_below_wr=cfg.fade_below_wr, recover_above_wr=cfg.recover_above_wr,
    )


def evaluate(
    config: Optional[Dict[str, Any]],
    *,
    lane: Optional[str] = None,
    trades_path: Optional[Path] = None,
    status_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    force: bool = False,
) -> RegimeFadeState:
    """Return the fade state for ``lane`` (TTL+mtime+config cached).

    Computes every lane's state from one file read and caches the dict. ``lane``
    is the strategy name (bitcoin, hype_macro, ...). An unknown/thin lane returns
    an inactive state. ``lane=None`` returns a no-op inactive placeholder.
    """
    global _cache_states, _cache_at, _cache_mtime, _cache_fp

    cfg = RegimeFadeConfig.from_dict((config or {}).get("regime_fade"))
    if not cfg.enabled:
        return _inactive(lane or "", cfg, "disabled")

    path = Path(trades_path) if trades_path else DEFAULT_TRADES_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0

    fp = (
        str(path), cfg.band_low, cfg.band_high, cfg.window_trades,
        cfg.min_band_samples, cfg.fade_below_wr, cfg.recover_above_wr,
        cfg.max_trade_age_hours, cfg.action,
    )

    nowt = time.time()
    fresh = (
        not force
        and _cache_states is not None
        and _cache_fp == fp
        and (nowt - _cache_at) < cfg.cache_ttl_sec
        and mtime == _cache_mtime
    )
    if not fresh:
        prev_active = (
            {lk: s.active for lk, s in _cache_states.items()}
            if (_cache_states is not None and _cache_fp == fp)
            else {}
        )
        try:
            states = _compute_states(cfg, path, prev_active=prev_active, now=now)
        except Exception as exc:  # fail-open
            logger.warning("regime_fade evaluate failed (fail-open): %s", exc)
            states = {}
        _cache_states = states
        _cache_at = nowt
        _cache_mtime = mtime
        _cache_fp = fp
        _write_status(states, status_path)

    if lane is None:
        return _inactive("", cfg, "no_lane")
    st = (_cache_states or {}).get(lane)
    if st is None:
        return _inactive(lane, cfg, "no_lane_data")
    return st


def should_suppress(
    state: RegimeFadeState,
    pred_p_win: Optional[float],
    config: Optional[Dict[str, Any]] = None,
    *,
    edge: Optional[float] = None,
) -> Tuple[bool, str]:
    """Suppress only when this lane's fade is active AND the candidate's predicted
    P(win) is in the mis-ranked band ``[band_low, band_high)``."""
    if not state.active:
        return False, "fade_inactive"
    if pred_p_win is None or not (state.band_low <= pred_p_win < state.band_high):
        return False, "outside_fade_band"

    cfg = RegimeFadeConfig.from_dict((config or {}).get("regime_fade")) if config else None
    action = (cfg.action if cfg else state.action) or "sit_out"

    if action == "raise_bar":
        bonus = cfg.raise_bar_min_edge_bonus if cfg else 0.08
        if edge is None:
            return True, (
                f"regime_fade_band_chop(sit_out;lane={state.lane};"
                f"band_wr={_fmt(state.rolling_wr)};p_win={pred_p_win:.3f};no_edge)"
            )
        if float(edge) < bonus:
            return True, (
                f"regime_fade_band_chop(raise_bar;lane={state.lane};"
                f"band_wr={_fmt(state.rolling_wr)};p_win={pred_p_win:.3f};edge={float(edge):.3f}<{bonus:.3f})"
            )
        return False, f"regime_fade_raise_bar_cleared(edge={float(edge):.3f}>={bonus:.3f})"

    return True, (
        f"regime_fade_band_chop(sit_out;lane={state.lane};band_wr={_fmt(state.rolling_wr)};"
        f"n_band={state.n_band};p_win={pred_p_win:.3f};band=[{state.band_low:.2f},{state.band_high:.2f}))"
    )


def _fmt(x: Optional[float]) -> str:
    return f"{x:.3f}" if isinstance(x, (int, float)) else "na"


def _write_status(states: Dict[str, RegimeFadeState], status_path: Optional[Path]) -> None:
    path = Path(status_path) if status_path else DEFAULT_STATUS_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "per_lane": {lane: s.to_dict() for lane, s in states.items()},
            "active_lanes": sorted(lane for lane, s in states.items() if s.active),
            "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("regime_fade status write failed: %s", exc)


def reset_cache() -> None:
    """Test hook: clear the process-local cache + per-lane hysteresis memory."""
    global _cache_states, _cache_at, _cache_mtime, _cache_fp
    _cache_states = None
    _cache_at = 0.0
    _cache_mtime = -1.0
    _cache_fp = None
