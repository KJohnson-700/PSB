"""Regime fade filter — sit out the mis-ranked mid-confidence band when its
recent *realized* win rate has collapsed (the momentum edge inverting in chop).

WHY (2026-06-21, validated on live joined data):
The momentum-based ``est_prob`` is REGIME-CONDITIONAL **and** mis-ranked in the
middle. Validated on 315 settled trades (data/logs/trade_analysis_joined.jsonl):

  predicted P(win)  trades  win rate  realized P&L
  ~0.4               60      42%       +$20
  ~0.5               72      40%       -$55
  ~0.6 (the bulk)   135      36%       -$38
  ~0.7               25      64%       +$66   <- genuine winner, DO NOT touch
  ~0.8               18      28%       -$1

So the bleed is concentrated in the **0.5-0.6 band** (-$93 combined, the bulk of
volume), while the 0.7 band is a real winner. A blanket "fade everything >=0.6"
would kill the +$66 winner; the correct, validated move is to gate the mid band
only: predicted P(win) in ``[band_low, band_high)``. Sitting that band out turns
the session from +$11 to ~+$104 without touching the 0.7 lane.

It stays REGIME-ADAPTIVE: it only suppresses while the band's *rolling realized*
win rate is below ``fade_below_wr`` (the edge is inverting), and re-enables the
band once it recovers above ``recover_above_wr`` (trend resumed) — hysteresis. In
a trending tape the band wins and the filter is a no-op, so it does not
permanently cut frequency.

Source of truth = ``data/calibration/trades.jsonl`` (settled trades: est_prob /
side / win). Read-only, mtime-TTL cached. Default ON, opt-out
``regime_fade.enabled: false``. Suppressed entries are ghost-logged by the caller.
"""
from __future__ import annotations

import json
import logging
import os
import time
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
    band_low: float = 0.45        # gate predicted P(win) in [band_low, band_high)
    band_high: float = 0.65       # protects the genuine 0.7+ winner above this
    window_trades: int = 40
    min_band_samples: int = 10
    fade_below_wr: float = 0.48   # activate when the band's rolling WR < this
    recover_above_wr: float = 0.53  # deactivate when band WR >= this (hysteresis)
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
            window_trades=max(1, _i("window_trades", 40)),
            min_band_samples=max(1, _i("min_band_samples", 10)),
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
    """Result of one fade evaluation."""

    active: bool = False
    rolling_wr: Optional[float] = None   # band's rolling realized WR
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
    """Predicted probability that the trade WINS.

    ``est_prob`` is P(YES). For a BUY_YES that is the win prob directly; for a
    BUY_NO / SHORT the win prob is ``1 - est_prob``.
    """
    if est_prob is None:
        return None
    try:
        p = float(est_prob)
    except (TypeError, ValueError):
        return None
    if str(side).upper() in _SHORT_SIDES:
        return 1.0 - p
    return p


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


def _read_last_lines(path: Path, max_lines: int, max_bytes: int = 1_500_000) -> List[str]:
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


def _compute_state(
    cfg: RegimeFadeConfig,
    trades_path: Path,
    *,
    prev_active: bool,
    now: Optional[datetime] = None,
) -> RegimeFadeState:
    now = now or datetime.now(timezone.utc)
    computed_at = now.isoformat(timespec="seconds")

    raw_lines = _read_last_lines(trades_path, max_lines=cfg.window_trades * 8)
    cutoff = now.timestamp() - cfg.max_trade_age_hours * 3600.0

    eligible: List[Tuple[float, bool, float]] = []  # (ts, win, pred_p_win)
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
        eligible.append((ts.timestamp(), bool(win), float(p_win)))

    eligible.sort(key=lambda r: r[0])
    window = eligible[-cfg.window_trades:]
    band = [(w, p) for (_ts, w, p) in window if cfg.in_band(p)]
    n_window = len(window)
    n_band = len(band)

    state = RegimeFadeState(
        band_low=cfg.band_low,
        band_high=cfg.band_high,
        fade_below_wr=cfg.fade_below_wr,
        recover_above_wr=cfg.recover_above_wr,
        action=cfg.action,
        n_window=n_window,
        n_band=n_band,
        computed_at=computed_at,
    )

    if n_band < cfg.min_band_samples:
        # Not enough realized band trades to judge — do NOT suppress on thin data.
        state.active = False
        state.reason = f"insufficient_band_samples(n={n_band}<{cfg.min_band_samples})"
        return state

    wins = sum(1 for (w, _p) in band if w)
    rolling_wr = wins / n_band
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


# --- process-local cache + hysteresis memory ------------------------------

_cache_state: Optional[RegimeFadeState] = None
_cache_at: float = 0.0
_cache_mtime: float = -1.0
_cache_fp: Optional[tuple] = None  # config/path fingerprint the cached state was computed under


def evaluate(
    config: Optional[Dict[str, Any]],
    *,
    trades_path: Optional[Path] = None,
    status_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    force: bool = False,
) -> RegimeFadeState:
    """Return the current fade state (TTL-cached on file mtime)."""
    global _cache_state, _cache_at, _cache_mtime, _cache_fp

    cfg = RegimeFadeConfig.from_dict((config or {}).get("regime_fade"))
    if not cfg.enabled:
        return RegimeFadeState(active=False, reason="disabled", action=cfg.action)

    path = Path(trades_path) if trades_path else DEFAULT_TRADES_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0

    # Fingerprint the inputs the state depends on, so a config/path change
    # invalidates the cache immediately instead of reusing a stale state (and a
    # stale prev_active) for up to the TTL. (Codex review 2026-06-21)
    fp = (
        str(path), cfg.band_low, cfg.band_high, cfg.window_trades,
        cfg.min_band_samples, cfg.fade_below_wr, cfg.recover_above_wr,
        cfg.max_trade_age_hours, cfg.action,
    )

    nowt = time.time()
    if (
        not force
        and _cache_state is not None
        and _cache_fp == fp
        and (nowt - _cache_at) < cfg.cache_ttl_sec
        and mtime == _cache_mtime
    ):
        return _cache_state

    prev_active = bool(_cache_state.active) if _cache_state is not None else False
    try:
        state = _compute_state(cfg, path, prev_active=prev_active, now=now)
    except Exception as exc:  # fail-open: never starve entries
        logger.warning("regime_fade evaluate failed (fail-open): %s", exc)
        state = RegimeFadeState(active=False, reason=f"error:{type(exc).__name__}", action=cfg.action)

    _cache_state = state
    _cache_at = nowt
    _cache_mtime = mtime
    _cache_fp = fp
    _write_status(state, status_path)
    return state


def should_suppress(
    state: RegimeFadeState,
    pred_p_win: Optional[float],
    config: Optional[Dict[str, Any]] = None,
    *,
    edge: Optional[float] = None,
) -> Tuple[bool, str]:
    """Decide whether a candidate should be suppressed.

    Only candidates whose predicted P(win) falls in the mis-ranked band
    ``[band_low, band_high)`` are ever suppressed — low-conviction entries and the
    genuine high-conviction (>= band_high) winners are left alone.
    """
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
                f"regime_fade_band_chop(sit_out;band_wr={_fmt(state.rolling_wr)};"
                f"p_win={pred_p_win:.3f};no_edge)"
            )
        if float(edge) < bonus:
            return True, (
                f"regime_fade_band_chop(raise_bar;band_wr={_fmt(state.rolling_wr)};"
                f"p_win={pred_p_win:.3f};edge={float(edge):.3f}<{bonus:.3f})"
            )
        return False, f"regime_fade_raise_bar_cleared(edge={float(edge):.3f}>={bonus:.3f})"

    return True, (
        f"regime_fade_band_chop(sit_out;band_wr={_fmt(state.rolling_wr)};"
        f"n_band={state.n_band};p_win={pred_p_win:.3f};"
        f"band=[{state.band_low:.2f},{state.band_high:.2f}))"
    )


def _fmt(x: Optional[float]) -> str:
    return f"{x:.3f}" if isinstance(x, (int, float)) else "na"


def _write_status(state: RegimeFadeState, status_path: Optional[Path]) -> None:
    path = Path(status_path) if status_path else DEFAULT_STATUS_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh)
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("regime_fade status write failed: %s", exc)


def reset_cache() -> None:
    """Test hook: clear the process-local cache + hysteresis memory."""
    global _cache_state, _cache_at, _cache_mtime, _cache_fp
    _cache_state = None
    _cache_at = 0.0
    _cache_mtime = -1.0
    _cache_fp = None
