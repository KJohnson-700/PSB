"""Regime fade filter — sit out high-confidence momentum entries when the
recent *realized* high-confidence win rate has collapsed (mean-reverting tape).

WHY (2026-06-21, WR root-cause build):
The momentum-based ``est_prob`` edge is REGIME-CONDITIONAL. From the joined
dataset vs the recovered +$362 baseline (est_prob calibration: predicted P(win)
bucket vs actual win rate):

  * BASELINE (trending tape, 57% WR): high-confidence calls HELD —
    pred 0.7 -> actual 66%, 0.9 -> 100%.
  * ADVERSE  (choppy/mean-reverting tape, 33% WR): the SAME model INVERTS —
    pred 0.6 -> actual 33% (the bulk), pred 0.8 -> 31%. The bot loses MOST
    when it is MOST confident.

``btc_1h_regime`` cannot catch this: it classifies BULL/RANGE/BEAR purely by
*distance* of price from SMA(20) (see ``btc_1h_regime.py``), so it is path-blind
— a choppy tape that happens to sit above the SMA band is labeled BULL,
identical to a clean trend. This filter instead measures the realized edge
*directly*: a rolling win rate over the most recent settled high-confidence
trades. When that drops below ``fade_below_wr`` the momentum edge is inverting,
so high-confidence momentum entries are suppressed (or held to a higher bar)
until the rate recovers above ``recover_above_wr`` (hysteresis).

Source of truth = ``data/calibration/trades.jsonl`` (one line per *settled*
trade; carries ``est_prob``/``calibrated_est_prob``, ``side``, and ``win``) —
the same record the WR diagnostic used. This module is read-only over that file
and caches with a short TTL so per-candidate calls during a scan are cheap.

Default ON, opt-out via ``regime_fade.enabled: false``. Suppressed entries are
ghost-logged by the caller so the filter is fully validatable on the joined
dataset + ghost log (NOT the broken backtester).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
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

# Short BUY_NO / SHORT vocab — predicted P(win) is 1-est_prob on these.
_SHORT_SIDES = {"BUY_NO", "NO", "SHORT", "SELL_YES", "DOWN"}


@dataclass(frozen=True)
class RegimeFadeConfig:
    """Parsed ``regime_fade`` config block."""

    enabled: bool = True
    high_conf_threshold: float = 0.60
    window_trades: int = 25
    min_high_conf_samples: int = 8
    fade_below_wr: float = 0.45
    recover_above_wr: float = 0.50
    max_trade_age_hours: float = 48.0
    action: str = "sit_out"  # "sit_out" | "raise_bar"
    raise_bar_min_edge_bonus: float = 0.05
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
            high_conf_threshold=_f("high_conf_threshold", 0.60),
            window_trades=max(1, _i("window_trades", 25)),
            min_high_conf_samples=max(1, _i("min_high_conf_samples", 8)),
            fade_below_wr=_f("fade_below_wr", 0.45),
            recover_above_wr=_f("recover_above_wr", 0.50),
            max_trade_age_hours=_f("max_trade_age_hours", 48.0),
            action=action,
            raise_bar_min_edge_bonus=_f("raise_bar_min_edge_bonus", 0.05),
            cache_ttl_sec=_f("cache_ttl_sec", 60.0),
        )


@dataclass
class RegimeFadeState:
    """Result of one fade evaluation."""

    active: bool = False
    rolling_wr: Optional[float] = None
    n_high_conf: int = 0
    n_window: int = 0
    high_conf_threshold: float = 0.60
    fade_below_wr: float = 0.45
    recover_above_wr: float = 0.50
    action: str = "sit_out"
    reason: str = "init"
    computed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "rolling_wr": (round(self.rolling_wr, 4) if self.rolling_wr is not None else None),
            "n_high_conf": self.n_high_conf,
            "n_window": self.n_window,
            "high_conf_threshold": self.high_conf_threshold,
            "fade_below_wr": self.fade_below_wr,
            "recover_above_wr": self.recover_above_wr,
            "action": self.action,
            "reason": self.reason,
            "computed_at": self.computed_at,
        }


def predicted_p_win(side: Any, est_prob: Optional[float]) -> Optional[float]:
    """Predicted probability that the trade WINS.

    ``est_prob`` is P(YES). For a BUY_YES that is the win prob directly; for a
    BUY_NO / SHORT the win prob is ``1 - est_prob``. Mirrors the WR-diagnostic.
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
    """Predicted P(YES) for a settled trade row — prefer calibrated, fall back."""
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
    """Read up to ``max_lines`` final lines of a file without loading all of it.

    Reads at most the trailing ``max_bytes`` so this stays cheap even if
    trades.jsonl grows large. Returns lines oldest->newest.
    """
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
    text = chunk.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    # If we started mid-file, the first partial line is unreliable — drop it.
    if read_bytes < size and lines:
        lines = lines[1:]
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

    # Pull a generous tail (the high-conf subset is a fraction of all trades),
    # then keep the most recent ``window_trades`` *eligible* settled trades.
    raw_lines = _read_last_lines(trades_path, max_lines=cfg.window_trades * 12)
    cutoff = now.timestamp() - cfg.max_trade_age_hours * 3600.0

    eligible: List[Tuple[float, bool, float]] = []  # (ts_epoch, win, pred_p_win)
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("shadow_mode"):
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
        if ts is None:
            continue
        ts_epoch = ts.timestamp()
        if ts_epoch < cutoff:
            continue
        eligible.append((ts_epoch, bool(win), float(p_win)))

    eligible.sort(key=lambda r: r[0])
    window = eligible[-cfg.window_trades:]
    high_conf = [(w, p) for (_ts, w, p) in window if p >= cfg.high_conf_threshold]
    n_window = len(window)
    n_high_conf = len(high_conf)

    state = RegimeFadeState(
        high_conf_threshold=cfg.high_conf_threshold,
        fade_below_wr=cfg.fade_below_wr,
        recover_above_wr=cfg.recover_above_wr,
        action=cfg.action,
        n_window=n_window,
        n_high_conf=n_high_conf,
        computed_at=computed_at,
    )

    if n_high_conf < cfg.min_high_conf_samples:
        # Not enough realized high-conf trades to judge the regime. In the
        # calibration phase we do NOT suppress on thin evidence — stay inactive.
        state.active = False
        state.reason = f"insufficient_high_conf_samples(n={n_high_conf}<{cfg.min_high_conf_samples})"
        return state

    wins = sum(1 for (w, _p) in high_conf if w)
    rolling_wr = wins / n_high_conf
    state.rolling_wr = rolling_wr

    # Hysteresis: once faded, stay faded until WR recovers above recover_above_wr.
    if prev_active:
        active = rolling_wr < cfg.recover_above_wr
    else:
        active = rolling_wr < cfg.fade_below_wr
    state.active = active
    if active:
        state.reason = (
            f"high_conf_wr={rolling_wr:.3f}<{cfg.fade_below_wr:.2f}"
            if not prev_active
            else f"high_conf_wr={rolling_wr:.3f}<recover={cfg.recover_above_wr:.2f}(held)"
        )
    else:
        state.reason = f"high_conf_wr={rolling_wr:.3f}>=recover={cfg.recover_above_wr:.2f}"
    return state


# --- process-local cache + hysteresis memory ------------------------------

_cache_state: Optional[RegimeFadeState] = None
_cache_at: float = 0.0
_cache_mtime: float = -1.0


def evaluate(
    config: Optional[Dict[str, Any]],
    *,
    trades_path: Optional[Path] = None,
    status_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    force: bool = False,
) -> RegimeFadeState:
    """Return the current fade state (TTL-cached on file mtime).

    ``config`` is the full bot config dict; the ``regime_fade`` block is read
    from it. When the block is missing/disabled the returned state is inactive.
    """
    global _cache_state, _cache_at, _cache_mtime

    cfg = RegimeFadeConfig.from_dict((config or {}).get("regime_fade"))
    if not cfg.enabled:
        return RegimeFadeState(active=False, reason="disabled", action=cfg.action)

    path = Path(trades_path) if trades_path else DEFAULT_TRADES_PATH

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0

    nowt = time.time()
    if (
        not force
        and _cache_state is not None
        and (nowt - _cache_at) < cfg.cache_ttl_sec
        and mtime == _cache_mtime
    ):
        return _cache_state

    prev_active = bool(_cache_state.active) if _cache_state is not None else False
    try:
        state = _compute_state(cfg, path, prev_active=prev_active, now=now)
    except Exception as exc:  # fail-open: never let the filter starve entries
        logger.warning("regime_fade evaluate failed (fail-open): %s", exc)
        state = RegimeFadeState(active=False, reason=f"error:{type(exc).__name__}", action=cfg.action)

    _cache_state = state
    _cache_at = nowt
    _cache_mtime = mtime

    _write_status(state, status_path)
    return state


def should_suppress(
    state: RegimeFadeState,
    pred_p_win: Optional[float],
    config: Optional[Dict[str, Any]] = None,
    *,
    edge: Optional[float] = None,
) -> Tuple[bool, str]:
    """Decide whether a candidate should be suppressed given the fade state.

    Returns ``(suppress, reason)``. Only high-confidence candidates
    (``pred_p_win >= high_conf_threshold``) are ever suppressed — low-conviction
    entries are left alone (they overperform in adverse tape per the diagnostic).
    """
    if not state.active:
        return False, "fade_inactive"
    if pred_p_win is None or pred_p_win < state.high_conf_threshold:
        return False, "not_high_conf"

    cfg = RegimeFadeConfig.from_dict((config or {}).get("regime_fade")) if config else None
    action = (cfg.action if cfg else state.action) or "sit_out"

    if action == "raise_bar":
        bonus = cfg.raise_bar_min_edge_bonus if cfg else 0.05
        # With no edge supplied we cannot apply a higher bar — fall back to sit_out.
        if edge is None:
            return True, (
                f"regime_fade_high_conf_chop(sit_out;wr={_fmt(state.rolling_wr)};"
                f"p_win={pred_p_win:.3f};no_edge)"
            )
        # Require the candidate to clear an elevated edge bar; block if it doesn't.
        if float(edge) < bonus:
            return True, (
                f"regime_fade_high_conf_chop(raise_bar;wr={_fmt(state.rolling_wr)};"
                f"p_win={pred_p_win:.3f};edge={float(edge):.3f}<{bonus:.3f})"
            )
        return False, f"regime_fade_raise_bar_cleared(edge={float(edge):.3f}>={bonus:.3f})"

    return True, (
        f"regime_fade_high_conf_chop(sit_out;wr={_fmt(state.rolling_wr)};"
        f"n_hc={state.n_high_conf};p_win={pred_p_win:.3f})"
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
    global _cache_state, _cache_at, _cache_mtime
    _cache_state = None
    _cache_at = 0.0
    _cache_mtime = -1.0
