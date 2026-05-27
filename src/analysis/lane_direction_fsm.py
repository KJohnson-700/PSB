"""Per-(asset, timeframe) lane direction FSM with neutral-recovery.

For each lane (``{asset}|{timeframe}``) this module computes a continuous
conviction score in ``[-1.0, +1.0]`` from the asset's own per-timeframe quant
indicators (MACD direction/momentum/crossover, EMA alignment, RSI zone, and an
optional neighbour-timeframe agreement signal). The global ``htf_bias`` is
applied as a small *additive modifier* (``±alpha``), not an override. Posteriors
gate *confidence*, not direction: low-data lanes get scaled-down scores and
therefore land in ``NEUTRAL`` more easily.

The score discretises into ``BULLISH`` / ``BEARISH`` / ``NEUTRAL`` with
hysteresis (separate ``T_enter`` and ``T_exit``) so the state does not
flip-flop at the threshold. ``NEUTRAL`` then routes through a sub-FSM keyed by
the previous non-neutral state and the momentum sign captured at the moment of
transition — see :class:`LaneDirectionFSM._neutral_directive`.

State persists to ``data/calibration/lane_direction_state.json``. Every
non-trivial transition emits a ``direction_event`` line to
``data/lane_state_audit.jsonl``.

The module is deliberately tolerant — any I/O or input failure degrades to a
``SIT_OUT`` directive with ``source="fallback_error"``. Trade execution must
never be blocked by FSM bookkeeping.

The active feature gate is ``lane_direction_fsm_active`` in config; default
``False`` so the module computes and logs without affecting live decisions
until validated.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_STATE_PATH = REPO_ROOT / "data" / "calibration" / "lane_direction_state.json"
DEFAULT_AUDIT_PATH = REPO_ROOT / "data" / "lane_state_audit.jsonl"
DEFAULT_POSTERIORS_PATH = REPO_ROOT / "data" / "calibration" / "lane_posteriors.json"

# Defaults — overridable via constructor / config.
T_ENTER_DEFAULT = 0.30      # |score| to enter BULLISH/BEARISH
T_EXIT_DEFAULT = 0.10       # opposite-side crossing to leave (hysteresis)
HTF_ALPHA_DEFAULT = 0.15    # htf modulator magnitude
POSTERIOR_N_REF = 200       # n at which posterior_confidence reaches 1.0
NEUTRAL_STUCK_SEC = 1800    # 30 min in NEUTRAL_* before promoting to NEUTRAL_STUCK
RECOVERY_SIZE_MULT = 0.30   # fade/exploration size multiplier
EPSILON = 1e-9

VALID_TIMEFRAMES = ("4h", "1h", "30m", "15m", "5m")
# Neighbour-TF lookup: each TF peeks at the one above to corroborate.
NEIGHBOUR_TF: Dict[str, Optional[str]] = {
    "5m": "15m",
    "15m": "30m",
    "30m": "1h",
    "1h": "4h",
    "4h": None,
}

# Per-timeframe default contributor weights. MACD weighted more on faster
# timeframes; EMA-alignment matters more on slower ones.
DEFAULT_WEIGHTS_BY_TF: Dict[str, Dict[str, float]] = {
    "5m":  {"macd_direction": 0.30, "macd_momentum": 0.25, "macd_crossover": 0.20,
            "ema_alignment":  0.10, "rsi_zone":      0.05, "neighbor_tf":    0.10},
    "15m": {"macd_direction": 0.25, "macd_momentum": 0.25, "macd_crossover": 0.15,
            "ema_alignment":  0.15, "rsi_zone":      0.10, "neighbor_tf":    0.10},
    "30m": {"macd_direction": 0.20, "macd_momentum": 0.25, "macd_crossover": 0.10,
            "ema_alignment":  0.25, "rsi_zone":      0.10, "neighbor_tf":    0.10},
    "1h":  {"macd_direction": 0.20, "macd_momentum": 0.20, "macd_crossover": 0.10,
            "ema_alignment":  0.30, "rsi_zone":      0.10, "neighbor_tf":    0.10},
    "4h":  {"macd_direction": 0.20, "macd_momentum": 0.20, "macd_crossover": 0.10,
            "ema_alignment":  0.35, "rsi_zone":      0.15, "neighbor_tf":    0.00},
}

# State labels
STATE_BULLISH = "BULLISH"
STATE_BEARISH = "BEARISH"
STATE_NEUTRAL_INITIAL = "NEUTRAL_INITIAL"
STATE_NEUTRAL_FROM_BULL = "NEUTRAL_FROM_BULL"
STATE_NEUTRAL_FROM_BEAR = "NEUTRAL_FROM_BEAR"
STATE_NEUTRAL_STUCK = "NEUTRAL_STUCK"


# ────────────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class LaneDirective:
    """The decision a strategy consumes."""
    side: str                       # "LONG" | "SHORT" | "SIT_OUT" | "SIGNAL_TIME"
    size_multiplier: float          # 1.0 normal; <1.0 for recovery/exploration
    state: str                      # STATE_* constant
    score: float                    # final score (post htf + post calib), clamped
    raw_score: float                # pre htf modifier, pre calibration
    htf_modifier: float             # the additive nudge applied
    posterior_confidence: float     # in [0, 1]
    contributors: Dict[str, float]  # raw per-contributor signed scores
    source: str                     # "posterior_calibrated_quant" | "fsm_recovery" | "fallback_error"
    lane_id: str = ""
    note: str = ""


@dataclass
class LaneDirectionState:
    """Per-lane persisted state."""
    lane_id: str
    current_state: str = STATE_NEUTRAL_INITIAL
    previous_non_neutral: str = ""      # last BULLISH or BEARISH seen
    transition_ts: str = ""             # ISO ts of current_state entry
    momentum_at_transition: int = 0     # sign at moment we entered current_state
    last_score: float = 0.0
    last_updated: str = ""


# ────────────────────────────────────────────────────────────────────────────
# Score math
# ────────────────────────────────────────────────────────────────────────────

def _safe_sign(x: float) -> int:
    if x > EPSILON:
        return 1
    if x < -EPSILON:
        return -1
    return 0


def _clamp(v: float, lo: float, hi: float) -> float:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _macd_contributors(macd: Any) -> Dict[str, float]:
    """Extract MACD-derived contributor values from a MACDResult-like object."""
    hist = float(getattr(macd, "histogram", 0.0) or 0.0)
    rising = bool(getattr(macd, "histogram_rising", False))
    above_zero = bool(getattr(macd, "above_zero", False))
    crossover = str(getattr(macd, "crossover", "NONE") or "NONE").upper()

    macd_direction = float(_safe_sign(hist))

    # Require an actual non-zero histogram before concluding momentum direction.
    # An all-zero MACD result (initial state, no data) must read as neutral, not
    # bearish-stuck.
    if abs(hist) < EPSILON:
        macd_momentum = 0.0
    elif rising and above_zero:
        macd_momentum = 1.0
    elif (not rising) and (not above_zero):
        macd_momentum = -1.0
    else:
        macd_momentum = 0.0

    if "BULL" in crossover:
        macd_crossover = 1.0
    elif "BEAR" in crossover:
        macd_crossover = -1.0
    else:
        macd_crossover = 0.0

    return {
        "macd_direction": macd_direction,
        "macd_momentum": macd_momentum,
        "macd_crossover": macd_crossover,
    }


def _ema_alignment(ta_asset: Any) -> float:
    """+1 if EMA9 > EMA21 > EMA50 (bull stack); -1 if reversed; 0 mixed."""
    e9 = float(getattr(ta_asset, "ema_9", 0.0) or 0.0)
    e21 = float(getattr(ta_asset, "ema_21", 0.0) or 0.0)
    e50 = float(getattr(ta_asset, "ema_50", 0.0) or 0.0)
    if e9 <= 0 or e21 <= 0 or e50 <= 0:
        return 0.0
    if e9 > e21 > e50:
        return 1.0
    if e9 < e21 < e50:
        return -1.0
    return 0.0


def _rsi_zone(rsi: float) -> float:
    """+1 in mid-bull zone (50-70), -1 in mid-bear (30-50), 0 in extremes/transitions."""
    if rsi <= 0:
        return 0.0
    if 50.0 < rsi < 70.0:
        return 1.0
    if 30.0 < rsi < 50.0:
        return -1.0
    return 0.0  # >=70 (overbought) or <=30 (oversold) → neutral here; trend exhaustion


def _get_macd_for_tf(ta_asset: Any, tf: str) -> Optional[Any]:
    """Return the MACD result for the given timeframe, or None if not present."""
    attr = f"macd_{tf}"
    return getattr(ta_asset, attr, None)


def compute_lane_quant_signal(
    ta: Any,
    timeframe: str,
    weights: Optional[Dict[str, float]] = None,
) -> Tuple[float, Dict[str, float]]:
    """Compute the raw [-1, +1] conviction score for one (asset, timeframe).

    Reads:
      - ``ta.sol.macd_{timeframe}`` (asset-generic naming; see sol_btc_service)
      - ``ta.sol.ema_9 / ema_21 / ema_50``
      - ``ta.sol.rsi_14``
      - ``ta.sol.macd_{neighbour_tf}`` for neighbour agreement, when defined

    Returns ``(raw_score, contributors)`` where ``contributors`` is the raw
    signed per-feature value (each in [-1, +1]) before weighting.
    """
    tf = str(timeframe or "").lower().strip()
    if tf not in VALID_TIMEFRAMES:
        return 0.0, {}

    ta_asset = getattr(ta, "sol", None)
    if ta_asset is None:
        return 0.0, {}

    macd = _get_macd_for_tf(ta_asset, tf)
    if macd is None:
        return 0.0, {}

    contributors: Dict[str, float] = {}
    contributors.update(_macd_contributors(macd))
    contributors["ema_alignment"] = _ema_alignment(ta_asset)
    contributors["rsi_zone"] = _rsi_zone(float(getattr(ta_asset, "rsi_14", 50.0) or 50.0))

    neighbour = NEIGHBOUR_TF.get(tf)
    if neighbour:
        neigh_macd = _get_macd_for_tf(ta_asset, neighbour)
        if neigh_macd is not None:
            contributors["neighbor_tf"] = float(
                _safe_sign(float(getattr(neigh_macd, "histogram", 0.0) or 0.0))
            )
        else:
            contributors["neighbor_tf"] = 0.0
    else:
        contributors["neighbor_tf"] = 0.0

    w = dict(weights or DEFAULT_WEIGHTS_BY_TF.get(tf, DEFAULT_WEIGHTS_BY_TF["15m"]))
    total_w = sum(abs(v) for v in w.values()) or 1.0
    raw = 0.0
    for key, val in contributors.items():
        raw += w.get(key, 0.0) * val
    raw = raw / total_w
    return _clamp(raw, -1.0, 1.0), contributors


def apply_htf_modifier(raw_score: float, htf_bias: str, alpha: float = HTF_ALPHA_DEFAULT) -> Tuple[float, float]:
    """Apply additive htf bias nudge. Returns (modified_score, modifier_applied)."""
    bias = (htf_bias or "NEUTRAL").upper()
    if bias == "BULLISH":
        mod = +abs(alpha)
    elif bias == "BEARISH":
        mod = -abs(alpha)
    else:
        mod = 0.0
    return _clamp(raw_score + mod, -1.0, 1.0), mod


# ────────────────────────────────────────────────────────────────────────────
# Posterior confidence
# ────────────────────────────────────────────────────────────────────────────

class _PosteriorReader:
    """Tolerant on-demand reader for ``lane_posteriors.json``.

    Caches the parsed dict and refreshes when the file mtime advances. Failures
    fall back to an empty mapping; the FSM degrades to ``posterior_confidence=0``
    in that case (which biases lanes toward NEUTRAL — the safe behaviour).
    """

    def __init__(self, path: Path = DEFAULT_POSTERIORS_PATH):
        self.path = Path(path)
        self._cached: Dict[str, Dict[str, Any]] = {}
        self._mtime: float = 0.0

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._cached = {}
            return
        if mtime <= self._mtime and self._cached:
            return
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("lane_direction_fsm: posterior read failed: %s", exc)
            self._cached = {}
            return
        lanes = blob.get("lanes") if isinstance(blob, dict) else None
        self._cached = lanes if isinstance(lanes, dict) else {}
        self._mtime = mtime

    def total_n_for(self, asset: str, timeframe: str) -> int:
        """Sum ``n`` across all posterior entries matching ``{asset}|{tf}|*``."""
        self._refresh()
        prefix = f"{asset}|{timeframe}|"
        total = 0
        for key, entry in self._cached.items():
            # Strip optional posterior_version prefix (``ver::`` form).
            stripped = key.split("::", 1)[-1] if "::" in key else key
            if not stripped.startswith(prefix):
                continue
            try:
                total += int(entry.get("n", 0) or 0)
            except (TypeError, ValueError):
                continue
        return total


def posterior_confidence(total_n: int, n_ref: int = POSTERIOR_N_REF) -> float:
    if n_ref <= 0:
        return 1.0
    return _clamp(total_n / float(n_ref), 0.0, 1.0)


# ────────────────────────────────────────────────────────────────────────────
# FSM
# ────────────────────────────────────────────────────────────────────────────

class LaneDirectionFSM:
    """Per-lane direction state machine.

    Threading note: a single process holds one instance and serialises access
    through a flock on the state file. Read-modify-write is performed inside
    :meth:`resolve` so concurrent strategy threads cannot race the state.
    """

    def __init__(
        self,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        audit_path: Path = DEFAULT_AUDIT_PATH,
        posteriors_path: Path = DEFAULT_POSTERIORS_PATH,
        t_enter: float = T_ENTER_DEFAULT,
        t_exit: float = T_EXIT_DEFAULT,
        htf_alpha: float = HTF_ALPHA_DEFAULT,
        n_ref: int = POSTERIOR_N_REF,
        weights_by_tf: Optional[Dict[str, Dict[str, float]]] = None,
        neutral_stuck_sec: int = NEUTRAL_STUCK_SEC,
        recovery_size_mult: float = RECOVERY_SIZE_MULT,
        per_lane_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.state_path = Path(state_path)
        self.audit_path = Path(audit_path)
        self.t_enter = float(t_enter)
        self.t_exit = float(t_exit)
        self.htf_alpha = float(htf_alpha)
        self.n_ref = int(n_ref)
        self.weights_by_tf = dict(weights_by_tf or DEFAULT_WEIGHTS_BY_TF)
        self.neutral_stuck_sec = int(neutral_stuck_sec)
        self.recovery_size_mult = float(recovery_size_mult)
        self.per_lane_overrides = dict(per_lane_overrides or {})
        self._states: Dict[str, LaneDirectionState] = {}
        self._posteriors = _PosteriorReader(posteriors_path)
        self._load()

    # -------- lane id ----------------------------------------------------

    @staticmethod
    def lane_id(asset: str, timeframe: str) -> str:
        return f"{str(asset or '').strip().lower()}|{str(timeframe or '').strip().lower()}"

    # -------- persistence -----------------------------------------------

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            blob = json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("lane_direction_fsm: state read failed: %s", exc)
            return
        if not isinstance(blob, dict):
            return
        for lid, entry in (blob.get("lanes") or {}).items():
            if not isinstance(entry, dict):
                continue
            try:
                self._states[str(lid)] = LaneDirectionState(
                    lane_id=str(lid),
                    current_state=str(entry.get("current_state", STATE_NEUTRAL_INITIAL)),
                    previous_non_neutral=str(entry.get("previous_non_neutral", "")),
                    transition_ts=str(entry.get("transition_ts", "")),
                    momentum_at_transition=int(entry.get("momentum_at_transition", 0) or 0),
                    last_score=float(entry.get("last_score", 0.0) or 0.0),
                    last_updated=str(entry.get("last_updated", "")),
                )
            except (TypeError, ValueError):
                continue

    def _flush(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
            payload = {"lanes": {lid: asdict(st) for lid, st in self._states.items()}}
            with open(lock_path, "a+") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(payload, f, indent=2, sort_keys=True)
                    os.replace(tmp, self.state_path)
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            logger.warning("lane_direction_fsm: state flush failed: %s", exc)

    def _audit(self, event: Dict[str, Any]) -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except OSError as exc:
            logger.warning("lane_direction_fsm: audit append failed: %s", exc)

    # -------- discretisation with hysteresis ----------------------------

    def _next_discrete_state(self, current_state: str, score: float) -> Tuple[str, bool]:
        """Returns (next_label, transitioned)."""
        if current_state == STATE_BULLISH:
            # Stay bullish until score drops below -T_exit (cross into bear zone)
            if score < -self.t_exit:
                return STATE_BEARISH, True
            if score < self.t_exit:
                return STATE_NEUTRAL_FROM_BULL, True
            return STATE_BULLISH, False
        if current_state == STATE_BEARISH:
            if score > self.t_exit:
                return STATE_BULLISH, True
            if score > -self.t_exit:
                return STATE_NEUTRAL_FROM_BEAR, True
            return STATE_BEARISH, False
        # neutral states: need full T_enter to leave
        if score > self.t_enter:
            return STATE_BULLISH, True
        if score < -self.t_enter:
            return STATE_BEARISH, True
        return current_state, False

    # -------- neutral sub-FSM directive ---------------------------------

    def _neutral_directive(self, state_label: str, momentum_sign: int) -> Tuple[str, float, str]:
        """Map a NEUTRAL_* state + momentum to (side, size_mult, note).

        Transition intent (see plan):
          - BULLISH→NEUTRAL with up-momentum: wait (likely topping, let it confirm).
          - BULLISH→NEUTRAL with down-momentum: contrarian-fade SHORT at small size.
          - BEARISH→NEUTRAL with down-momentum: wait.
          - BEARISH→NEUTRAL with up-momentum: contrarian-fade LONG at small size.
          - NEUTRAL_STUCK: exploration; let the signal pick at signal-time.
          - NEUTRAL_INITIAL: no history yet, sit out.
        """
        if state_label == STATE_NEUTRAL_INITIAL:
            return "SIT_OUT", 0.0, "no_history"
        if state_label == STATE_NEUTRAL_STUCK:
            return "SIGNAL_TIME", self.recovery_size_mult, "stuck_exploration"
        if state_label == STATE_NEUTRAL_FROM_BULL:
            if momentum_sign >= 0:
                return "SIT_OUT", 0.0, "topping_wait_confirm"
            return "SHORT", self.recovery_size_mult, "fade_from_bull"
        if state_label == STATE_NEUTRAL_FROM_BEAR:
            if momentum_sign <= 0:
                return "SIT_OUT", 0.0, "bottoming_wait_confirm"
            return "LONG", self.recovery_size_mult, "fade_from_bear"
        return "SIT_OUT", 0.0, "unknown_neutral"

    # -------- main API ---------------------------------------------------

    def resolve(
        self,
        asset: str,
        timeframe: str,
        ta: Any,
        htf_bias: str,
        *,
        persist: bool = True,
        write_audit: bool = True,
    ) -> LaneDirective:
        """Compute the LaneDirective for one (asset, timeframe) lane."""
        lid = self.lane_id(asset, timeframe)
        overrides = self.per_lane_overrides.get(lid, {})
        t_enter = float(overrides.get("t_enter", self.t_enter))
        t_exit = float(overrides.get("t_exit", self.t_exit))
        alpha = float(overrides.get("htf_alpha", self.htf_alpha))
        weights = overrides.get("weights") or self.weights_by_tf.get(
            timeframe, DEFAULT_WEIGHTS_BY_TF.get(timeframe, DEFAULT_WEIGHTS_BY_TF["15m"])
        )

        try:
            raw_score, contributors = compute_lane_quant_signal(ta, timeframe, weights=weights)
        except Exception as exc:  # defensive
            logger.warning("lane_direction_fsm: compute failed for %s: %s", lid, exc)
            return LaneDirective(
                side="SIT_OUT", size_multiplier=0.0, state=STATE_NEUTRAL_INITIAL,
                score=0.0, raw_score=0.0, htf_modifier=0.0, posterior_confidence=0.0,
                contributors={}, source="fallback_error", lane_id=lid, note=str(exc),
            )

        score_pre_calib, htf_mod = apply_htf_modifier(raw_score, htf_bias, alpha=alpha)

        try:
            total_n = self._posteriors.total_n_for(asset, timeframe)
        except Exception as exc:
            logger.warning("lane_direction_fsm: posterior read failed for %s: %s", lid, exc)
            total_n = 0
        post_conf = posterior_confidence(total_n, n_ref=self.n_ref)
        score = _clamp(score_pre_calib * (0.5 + 0.5 * post_conf), -1.0, 1.0)

        # Fetch or initialise state.
        prev = self._states.get(lid) or LaneDirectionState(lane_id=lid)
        now_iso = datetime.now(timezone.utc).isoformat()
        # Use override thresholds if present.
        old_enter, old_exit = self.t_enter, self.t_exit
        self.t_enter, self.t_exit = t_enter, t_exit
        try:
            next_state, transitioned = self._next_discrete_state(prev.current_state, score)
        finally:
            self.t_enter, self.t_exit = old_enter, old_exit

        # Detect NEUTRAL_STUCK promotion.
        if next_state in (STATE_NEUTRAL_FROM_BULL, STATE_NEUTRAL_FROM_BEAR) and not transitioned:
            try:
                entered = datetime.fromisoformat(prev.transition_ts) if prev.transition_ts else None
            except ValueError:
                entered = None
            if entered is not None:
                age = (datetime.now(timezone.utc) - entered).total_seconds()
                if age > self.neutral_stuck_sec:
                    next_state = STATE_NEUTRAL_STUCK
                    transitioned = True

        # Determine momentum sign captured at this moment (for FSM use).
        momentum_sign = int(contributors.get("macd_direction", 0.0))

        # Persist updated state.
        new_state = LaneDirectionState(
            lane_id=lid,
            current_state=next_state,
            previous_non_neutral=(
                prev.current_state
                if prev.current_state in (STATE_BULLISH, STATE_BEARISH) and next_state.startswith("NEUTRAL")
                else prev.previous_non_neutral
                if next_state.startswith("NEUTRAL")
                else ""  # cleared when we're in a directional state
            ),
            transition_ts=now_iso if transitioned else (prev.transition_ts or now_iso),
            momentum_at_transition=momentum_sign if transitioned else prev.momentum_at_transition,
            last_score=score,
            last_updated=now_iso,
        )
        self._states[lid] = new_state
        if persist:
            self._flush()

        if transitioned and write_audit:
            self._audit({
                "event": "direction_event",
                "ts": now_iso,
                "lane_id": lid,
                "prev_state": prev.current_state,
                "new_state": next_state,
                "score": score,
                "raw_score": raw_score,
                "htf_modifier": htf_mod,
                "posterior_confidence": post_conf,
                "posterior_total_n": total_n,
                "contributors": contributors,
                "momentum_sign": momentum_sign,
                "htf_bias": (htf_bias or "NEUTRAL").upper(),
            })

        # Translate state → directive.
        if next_state == STATE_BULLISH:
            return LaneDirective(
                side="LONG", size_multiplier=1.0, state=next_state,
                score=score, raw_score=raw_score, htf_modifier=htf_mod,
                posterior_confidence=post_conf, contributors=contributors,
                source="posterior_calibrated_quant", lane_id=lid,
            )
        if next_state == STATE_BEARISH:
            return LaneDirective(
                side="SHORT", size_multiplier=1.0, state=next_state,
                score=score, raw_score=raw_score, htf_modifier=htf_mod,
                posterior_confidence=post_conf, contributors=contributors,
                source="posterior_calibrated_quant", lane_id=lid,
            )

        # NEUTRAL family.
        side, size_mult, note = self._neutral_directive(next_state, new_state.momentum_at_transition)
        return LaneDirective(
            side=side, size_multiplier=size_mult, state=next_state,
            score=score, raw_score=raw_score, htf_modifier=htf_mod,
            posterior_confidence=post_conf, contributors=contributors,
            source="fsm_recovery", lane_id=lid, note=note,
        )

    # -------- introspection (for tests / dashboards) --------------------

    def state_for(self, asset: str, timeframe: str) -> Optional[LaneDirectionState]:
        return self._states.get(self.lane_id(asset, timeframe))


# ────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ────────────────────────────────────────────────────────────────────────────

_SINGLETON: Optional[LaneDirectionFSM] = None


def get_fsm(
    *,
    config: Optional[Dict[str, Any]] = None,
    force_new: bool = False,
) -> LaneDirectionFSM:
    """Return the process-wide FSM singleton, constructing it on first call.

    ``config`` is expected to come from the top-level YAML; only known keys are
    consumed (see :class:`LaneDirectionFSM`). Unknown keys are ignored so the
    YAML can be extended without code churn.
    """
    global _SINGLETON
    if _SINGLETON is not None and not force_new:
        return _SINGLETON
    cfg = dict(config or {})
    _SINGLETON = LaneDirectionFSM(
        t_enter=float(cfg.get("lane_direction_t_enter", T_ENTER_DEFAULT)),
        t_exit=float(cfg.get("lane_direction_t_exit", T_EXIT_DEFAULT)),
        htf_alpha=float(cfg.get("lane_direction_htf_alpha", HTF_ALPHA_DEFAULT)),
        n_ref=int(cfg.get("lane_direction_posterior_n_ref", POSTERIOR_N_REF)),
        weights_by_tf=cfg.get("lane_direction_contributor_weights") or DEFAULT_WEIGHTS_BY_TF,
        neutral_stuck_sec=int(cfg.get("lane_direction_neutral_stuck_sec", NEUTRAL_STUCK_SEC)),
        recovery_size_mult=float(cfg.get("lane_direction_recovery_size_mult", RECOVERY_SIZE_MULT)),
        per_lane_overrides=cfg.get("lane_direction_overrides") or {},
    )
    return _SINGLETON


def is_active(config: Optional[Dict[str, Any]] = None) -> bool:
    """Feature-flag accessor; default OFF (shadow-only compute)."""
    return bool((config or {}).get("lane_direction_fsm_active", False))


__all__ = [
    "LaneDirective",
    "LaneDirectionState",
    "LaneDirectionFSM",
    "compute_lane_quant_signal",
    "apply_htf_modifier",
    "posterior_confidence",
    "get_fsm",
    "is_active",
    "STATE_BULLISH",
    "STATE_BEARISH",
    "STATE_NEUTRAL_INITIAL",
    "STATE_NEUTRAL_FROM_BULL",
    "STATE_NEUTRAL_FROM_BEAR",
    "STATE_NEUTRAL_STUCK",
    "DEFAULT_WEIGHTS_BY_TF",
    "T_ENTER_DEFAULT",
    "T_EXIT_DEFAULT",
    "HTF_ALPHA_DEFAULT",
    "POSTERIOR_N_REF",
]
