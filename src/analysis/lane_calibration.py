"""Phase 6 per-lane probability calibration.

For each lane (``<strategy>|<window>|<side>|<regime>|<family>``) we track two
small posteriors that are updated on every closed trade:

1. ``alpha_ewma`` — exponentially-weighted running estimate of
   ``realized_pct / (stated_est_prob - 0.5)``. Multiplies the deviation of the
   model's predicted probability from 50/50 to produce a calibrated probability:
   ``p_cal = 0.5 + alpha * (p_raw - 0.5)``. Lanes the model under-predicts
   can print raw ``alpha > 1`` for telemetry, but live correction caps at
   identity so calibration never amplifies confidence. Lanes it over-predicts
   can still converge to ``alpha < 1`` (shrink toward 0.5).

2. ``Beta(a, b)`` — Bernoulli win-rate posterior with prior ``Beta(2, 3)``. Used
   only for reporting and for Phase 7 drift detection; does not feed the
   probability correction directly.

Shadow mode (``shadow_mode=True``) updates posteriors on each close but
``calibrate()`` returns ``raw_est_prob`` unchanged so production behavior is
identical. The plan calls for 1-2 sessions of shadow before flipping live.

Persistence: ``data/calibration/lane_posteriors.json`` — read on init, rewritten
on each ``record()`` under an ``fcntl.flock`` exclusive lock so concurrent
writes serialize.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "calibration"
)
DEFAULT_POSTERIORS_PATH = DEFAULT_CALIBRATION_DIR / "lane_posteriors.json"

# Bound constants — see the plan's "Bounds rationale" section.
EWMA_LAMBDA = 0.15
ALPHA_CLAMP_LO = 0.30
ALPHA_CLAMP_HI = 1.00
A_OBS_CLAMP = 5.0          # raw observation clamp before EWMA folds it in
SHRINK_N = 10              # blend toward identity below this sample count
PRIOR_A = 2.0
PRIOR_B = 3.0
DEV_FLOOR = 0.005          # |stated_prob - 0.5| guard before computing a_obs
SCHEMA_VERSION = 1
DEFAULT_POSTERIOR_VERSION = ""

# β_mean veto: lanes with established losing history get forced to 0.5
# (zero edge → rejected by lane_min_edge) regardless of α magnitude.
# Defaults target the death-spiral pattern: high α + low β_mean from
# directionally-wrong calibration drift. Override via constructor.
BETA_VETO_MAX_MEAN = 0.40
BETA_VETO_MIN_N = 30

# Pull-to-beta blend: when a lane has enough live samples, blend the raw
# probability toward the observed posterior mean instead of toward 0.5.
# The previous "alpha * (p - 0.5)" formula could only reduce confidence,
# never flip direction — so lanes whose true WR was on the opposite side of
# 0.5 from the model's prediction bled indefinitely until the binary veto
# fired at n>=BETA_VETO_MIN_N. The blend closes that gap smoothly.
BETA_BLEND_N_FLOOR = 30   # no blend below this many live samples
BETA_BLEND_N_FULL = 100   # max bias-shift weight reached at this many samples
BETA_BLEND_W_MAX = 0.60   # cap on bias-shift weight — stays out of the
                          # discrimination-collapse regime (w→1 flattened every
                          # lane to a constant in the e665c6e version)
BETA_BLEND_MIN_BIAS = 0.05  # ignore directional bias smaller than this


@dataclass
class LanePosterior:
    """Mutable per-lane state. JSON-serialised via ``asdict``."""

    n: int = 0
    alpha_ewma: float = 1.0           # initialised to identity (no correction)
    beta_a: float = PRIOR_A
    beta_b: float = PRIOR_B
    last_updated: str = ""

    @classmethod
    def fresh(cls) -> "LanePosterior":
        return cls()


class LaneCalibrator:
    """Per-lane probability calibration with optional shadow mode.

    The class is intentionally tolerant — JSON read/write failures fall back to
    an in-memory empty dict and log a warning. Trade execution must never be
    blocked by calibration bookkeeping.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        shadow_mode: bool = True,
        min_samples_to_apply: int = 0,
        beta_veto_max_mean: float = BETA_VETO_MAX_MEAN,
        beta_veto_min_n: int = BETA_VETO_MIN_N,
        per_lane_thresholds_enabled: bool = False,
        per_lane_thresholds: Optional[Dict[str, Dict[str, Any]]] = None,
        posterior_version: str = DEFAULT_POSTERIOR_VERSION,
        beta_blend_enabled: bool = True,
        beta_blend_n_floor: int = BETA_BLEND_N_FLOOR,
        beta_blend_n_full: int = BETA_BLEND_N_FULL,
        beta_blend_w_max: float = BETA_BLEND_W_MAX,
        beta_blend_min_bias: float = BETA_BLEND_MIN_BIAS,
    ):
        self.path: Path = Path(path) if path is not None else DEFAULT_POSTERIORS_PATH
        self.shadow_mode: bool = bool(shadow_mode)
        self.min_samples_to_apply: int = max(0, int(min_samples_to_apply or 0))
        # β_mean veto thresholds. Set max_mean=0.0 or min_n<=0 to disable.
        self.beta_veto_max_mean: float = float(beta_veto_max_mean)
        self.beta_veto_min_n: int = int(beta_veto_min_n)
        self.beta_blend_enabled: bool = bool(beta_blend_enabled)
        self.beta_blend_n_floor: int = max(1, int(beta_blend_n_floor))
        self.beta_blend_n_full: int = max(
            self.beta_blend_n_floor + 1, int(beta_blend_n_full)
        )
        self.beta_blend_w_max: float = max(0.0, min(1.0, float(beta_blend_w_max)))
        self.beta_blend_min_bias: float = max(0.0, float(beta_blend_min_bias))
        # Per-lane threshold overrides derived from ghost data. Off by default
        # — the operator inspects the recommendations (lane_thresholds.json)
        # and flips per_lane_thresholds_enabled to True when satisfied.
        self.per_lane_thresholds_enabled: bool = bool(per_lane_thresholds_enabled)
        self.per_lane_thresholds: Dict[str, Dict[str, Any]] = dict(
            per_lane_thresholds or {}
        )
        self.posterior_version: str = str(posterior_version or "").strip()
        self._posteriors: Dict[str, LanePosterior] = {}
        self._load()

    def _lane_key(self, lane_id: str) -> str:
        lane = str(lane_id or "").strip()
        if not lane or not self.posterior_version:
            return lane
        prefix = f"{self.posterior_version}::"
        return lane if lane.startswith(prefix) else f"{prefix}{lane}"

    # ---------------------------------------------------------------- loading

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return
            blob = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            self._archive_corrupt(reason=f"parse_failed: {exc!r}")
            return

        if not isinstance(blob, dict):
            self._archive_corrupt(reason=f"top_level_not_object: {type(blob).__name__}")
            return

        schema = blob.get("schema_version")
        lanes = blob.get("lanes") or {}
        if schema != SCHEMA_VERSION:
            self._archive_corrupt(reason=f"schema_version_mismatch: {schema!r}")
            return
        if not isinstance(lanes, dict):
            self._archive_corrupt(reason="lanes_not_object")
            return

        for lane_id, entry in lanes.items():
            if not isinstance(entry, dict):
                continue
            try:
                self._posteriors[str(lane_id)] = LanePosterior(
                    n=int(entry.get("n", 0) or 0),
                    alpha_ewma=float(entry.get("alpha_ewma", 1.0) or 1.0),
                    beta_a=float(entry.get("beta_a", PRIOR_A) or PRIOR_A),
                    beta_b=float(entry.get("beta_b", PRIOR_B) or PRIOR_B),
                    last_updated=str(entry.get("last_updated", "")),
                )
            except (TypeError, ValueError):
                continue

    def _archive_corrupt(self, *, reason: str) -> None:
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = self.path.with_suffix(self.path.suffix + f".corrupt.{ts}")
            shutil.move(str(self.path), str(dest))
            logger.warning(
                "lane_calibration: archived corrupt %s -> %s (%s)",
                self.path.name, dest.name, reason,
            )
        except OSError as exc:
            logger.warning("lane_calibration: failed to archive corrupt file: %s", exc)
        self._posteriors = {}

    # ---------------------------------------------------------------- writing

    def _serialise(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "posterior_version": self.posterior_version,
            "lanes": {lid: asdict(p) for lid, p in self._posteriors.items()},
        }

    def _flush(self) -> bool:
        """Atomic-ish write under flock. Returns True on success."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            # Acquire lock on a sidecar file so concurrent writers serialise.
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            with open(lock_path, "a+") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(self._serialise(), f, indent=2, sort_keys=True)
                    os.replace(tmp, self.path)
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("lane_calibration: flush failed: %s", exc)
            return False

    # ---------------------------------------------------------------- math

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        if value < lo:
            return lo
        if value > hi:
            return hi
        return value

    def _beta_mean(self, p: LanePosterior) -> float:
        """Posterior Beta mean = realized win rate proxy."""
        total = p.beta_a + p.beta_b
        if total <= 0:
            return PRIOR_A / (PRIOR_A + PRIOR_B)
        return p.beta_a / total

    def _is_vetoed(self, p: Optional[LanePosterior]) -> bool:
        """Lane has enough samples AND realized WR below the veto floor.

        Note: this signature can't see lane_id, so it only checks the
        live-β floor. Per-lane overrides are applied separately via
        ``_is_vetoed_for_lane`` which DOES have the key.
        """
        if p is None or p.n <= 0:
            return False
        if self.beta_veto_min_n <= 0 or self.beta_veto_max_mean <= 0.0:
            return False
        if p.n < self.beta_veto_min_n:
            return False
        return self._beta_mean(p) < self.beta_veto_max_mean

    def _is_vetoed_for_lane(self, lane_id: str) -> bool:
        """Per-lane-aware veto check. Consults ghost-derived overrides when
        ``per_lane_thresholds_enabled`` is True; otherwise behaves identically
        to ``_is_vetoed``.

        Three outcomes per lane when per-lane mode is on:
          1. Override marks lane as ``veto_recommended: True`` → VETO
             (independent of global β state — per-lane data says this lane loses)
          2. Override exists with custom ``recommended_max_mean`` → use that
             instead of the global floor against live β (only when global β
             gating is configured)
          3. No override → fall back to global β-veto check
        """
        lane_key = self._lane_key(lane_id)
        p = self._posteriors.get(lane_key)
        # Hard veto path runs even when global β-veto is disabled: the
        # per-lane threshold pipeline is an independent decision source
        # (live + ghost outcomes per lane_id, see lane_thresholds.py).
        if self.per_lane_thresholds_enabled:
            override = self.per_lane_thresholds.get(lane_id) or self.per_lane_thresholds.get(lane_key)
            if override is not None and bool(override.get("veto_recommended")):
                return True
        # Remaining paths apply a β floor against live posteriors and
        # require the global β-veto thresholds to be configured.
        if self.beta_veto_min_n <= 0 or self.beta_veto_max_mean <= 0.0:
            return False
        if not self.per_lane_thresholds_enabled:
            return self._is_vetoed(p)
        override = self.per_lane_thresholds.get(lane_id) or self.per_lane_thresholds.get(lane_key)
        if override is None:
            return self._is_vetoed(p)
        # Per-lane β floor against live data (override.recommended_max_mean).
        if p is None or p.n <= 0:
            return False
        max_mean = float(
            override.get("recommended_max_mean", self.beta_veto_max_mean)
        )
        if max_mean <= 0.0:
            return False
        if p.n < self.beta_veto_min_n:
            return False
        return self._beta_mean(p) < max_mean

    def _shrunk_alpha(self, p: LanePosterior) -> float:
        """Blend ``alpha_ewma`` toward identity (1.0) when sample is small."""
        if p.n >= SHRINK_N:
            return self._clamp(p.alpha_ewma, ALPHA_CLAMP_LO, ALPHA_CLAMP_HI)
        w = p.n / SHRINK_N
        blended = w * p.alpha_ewma + (1.0 - w) * 1.0
        return self._clamp(blended, ALPHA_CLAMP_LO, ALPHA_CLAMP_HI)

    # ---------------------------------------------------------------- API

    def alpha(self, lane_id: str) -> float:
        """Effective α used for correction (shrunk + clamped). Identity if unknown."""
        if not lane_id:
            return 1.0
        p = self._posteriors.get(self._lane_key(lane_id))
        if p is None or p.n == 0:
            return 1.0
        if p.n < self.min_samples_to_apply:
            return 1.0
        return self._shrunk_alpha(p)

    def is_vetoed(self, lane_id: str) -> bool:
        """Public veto check. True if lane has established losing WR.

        When ``per_lane_thresholds_enabled`` is True, also consults the
        ghost-derived per-lane overrides loaded from ``lane_thresholds.json``.
        """
        if not lane_id:
            return False
        return self._is_vetoed_for_lane(lane_id)

    def record_ghost(
        self,
        lane_id: str,
        win: bool,
        *,
        weight: float = 0.5,
    ) -> None:
        """Partial-weight β update from a settled ghost (would-have-been) outcome.

        Self-healing path for β-vetoed lanes: vetoed lanes stop getting live
        trades, so β would freeze and the veto would persist indefinitely. By
        feeding ghost outcomes at a reduced weight (default 0.5x of a live
        trade), vetoed lanes can climb back above the veto threshold if their
        would-have-been WR genuinely recovers — but ghost data can't dominate
        live data 1:1 because there's no slippage / fills / exit-timing risk.

        No α update — ghost records don't carry the stated_est_prob /
        realized_pct pair in the same form. Only β moves.
        """
        lane = str(lane_id or "").strip()
        if not lane:
            return
        w = max(0.0, float(weight))
        if w <= 0.0:
            return
        p = self._posteriors.setdefault(self._lane_key(lane), LanePosterior.fresh())
        if win:
            p.beta_a += w
        else:
            p.beta_b += w
        p.n = int(p.n)  # n counts only live trades; ghost contributes to β not n

    def raw_alpha(self, lane_id: str) -> Optional[float]:
        """The unshrunk, unclamped EWMA value (or None if no samples)."""
        p = self._posteriors.get(self._lane_key(lane_id))
        if p is None or p.n == 0:
            return None
        return p.alpha_ewma

    def posterior(self, lane_id: str) -> Dict[str, Any]:
        """Snapshot for telemetry: n, beta posterior mean, current α (shrunk)."""
        p = self._posteriors.get(self._lane_key(lane_id))
        if p is None or p.n == 0:
            return {
                "n": 0,
                "alpha": 1.0,
                "alpha_raw": None,
                "beta_a": PRIOR_A,
                "beta_b": PRIOR_B,
                "beta_mean": PRIOR_A / (PRIOR_A + PRIOR_B),
                "min_samples_to_apply": self.min_samples_to_apply,
                "vetoed": False,
                "posterior_version": self.posterior_version,
            }
        return {
            "n": p.n,
            "alpha": self._shrunk_alpha(p),
            "alpha_raw": p.alpha_ewma,
            "beta_a": p.beta_a,
            "beta_b": p.beta_b,
            "beta_mean": self._beta_mean(p),
            "min_samples_to_apply": self.min_samples_to_apply,
            "vetoed": self._is_vetoed(p),
            "posterior_version": self.posterior_version,
        }

    def calibrate(self, lane_id: str, raw_est_prob: float) -> float:
        """Return calibrated p (identity in shadow mode).

        Two stacked corrections:
          1. α-shrink (legacy): ``0.5 + α * (p_raw - 0.5)`` — pulls toward
             0.5 when the model is over-confident in a direction that's
             panning out. Cannot flip direction.
          2. β-blend (added 2026-05-29): when lane has enough live samples,
             additionally blend toward the observed posterior mean (which
             IS the side-specific reality). Closes the gap where realized
             WR is on the *opposite* side of 0.5 from the model's
             prediction — the α-only formula could never reach that.
        """
        try:
            p_raw = float(raw_est_prob)
        except (TypeError, ValueError):
            return 0.5
        # β_mean veto runs regardless of shadow_mode — lanes with established
        # losing WR get forced to 0.5 (zero edge → lane_min_edge reject) until
        # WR recovers. Breaks the α-inflation death spiral on hot losers.
        if self.is_vetoed(lane_id):
            return 0.5
        if self.shadow_mode:
            return p_raw
        a = self.alpha(lane_id)
        p_cal = 0.5 + a * (p_raw - 0.5)
        if self.beta_blend_enabled:
            p_cal = self._beta_blend(lane_id, p_cal)
        # Safety clamp — never hand off something outside (0, 1).
        return max(0.01, min(0.99, p_cal))

    def _beta_blend(self, lane_id: str, p_cal: float) -> float:
        """Bias-SHIFT p toward the lane's observed directional reality.

        This is the corrected pull-to-beta (the e665c6e version blended toward
        the constant ``beta_target_yes`` with weight → 1.0, which replaced every
        trade's estimate with a single per-lane number and destroyed the model's
        ability to discriminate good setups from bad — fabricated flat edge).

        Instead we add a per-lane *offset* equal to how far the lane's realized
        side-win-rate sits from a coin flip, scaled by a bounded weight:

            p_cal' = p_cal + w * (beta_target_yes - 0.5)

        The per-signal spread of ``p_cal`` is preserved (still translated, not
        flattened), so within-lane discrimination survives. When a lane is
        strongly inverted the offset is large enough to carry the estimate
        across 0.5 — flipping the side — which is exactly the correction an
        inverted short lane needs.

        β tracks P(this lane's chosen side wins). Lane slot 2 is the side
        ('up' for BUY_YES, 'down' for BUY_NO), so for a BUY_NO lane the YES
        probability is ``1 - beta_mean``.
        """
        if not lane_id:
            return p_cal
        p = self._posteriors.get(self._lane_key(lane_id))
        if p is None or p.n < self.beta_blend_n_floor:
            return p_cal
        parts = lane_id.split("|")
        side_slot = parts[2] if len(parts) > 2 else ""
        beta_mean_side = self._beta_mean(p)  # P(chosen side wins)
        beta_target_yes = (
            beta_mean_side if side_slot == "up" else (1.0 - beta_mean_side)
        )
        bias = beta_target_yes - 0.5
        # Ignore tiny directional bias — only correct lanes with a real lean.
        if abs(bias) < self.beta_blend_min_bias:
            return p_cal
        # Shrinkage ramp 0 → w_max between n_floor and n_full. Capped well below
        # 1.0 so the shift can never dominate the per-signal estimate.
        span = max(1, self.beta_blend_n_full - self.beta_blend_n_floor)
        w = self.beta_blend_w_max * min(1.0, (p.n - self.beta_blend_n_floor) / span)
        return p_cal + w * bias

    def record(
        self,
        lane_id: str,
        stated_est_prob: Optional[float],
        realized_pct: float,
        win: bool,
    ) -> Dict[str, Any]:
        """Update posterior with one closed-trade outcome.

        Returns a snapshot dict ({n, alpha, alpha_raw, beta_a, beta_b}) — the
        caller can copy it into the calibration log row for telemetry.
        """
        lane = str(lane_id or "").strip()
        if not lane:
            return self.posterior("")

        p = self._posteriors.setdefault(self._lane_key(lane), LanePosterior.fresh())

        # Beta update — independent of probability calibration; always applied.
        if win:
            p.beta_a += 1.0
        else:
            p.beta_b += 1.0

        # α update — guarded against near-0.5 stated prob (avoid div blowups).
        if stated_est_prob is not None:
            try:
                dev = float(stated_est_prob) - 0.5
                rp = float(realized_pct)
            except (TypeError, ValueError):
                dev = 0.0
                rp = 0.0
            if abs(dev) >= DEV_FLOOR and math.isfinite(rp):
                a_obs = self._clamp(rp / dev, -A_OBS_CLAMP, A_OBS_CLAMP)
                if p.n == 0:
                    p.alpha_ewma = a_obs
                else:
                    p.alpha_ewma = (1.0 - EWMA_LAMBDA) * p.alpha_ewma + EWMA_LAMBDA * a_obs

        p.n += 1
        p.last_updated = datetime.now(timezone.utc).isoformat()

        self._flush()
        return self.posterior(lane)
