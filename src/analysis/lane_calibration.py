"""Phase 6 per-lane probability calibration.

For each lane (``<strategy>|<window>|<side>|<regime>|<family>``) we track two
small posteriors that are updated on every closed trade:

1. ``alpha_ewma`` — exponentially-weighted running estimate of
   ``realized_pct / (stated_est_prob - 0.5)``. Multiplies the deviation of the
   model's predicted probability from 50/50 to produce a calibrated probability:
   ``p_cal = 0.5 + alpha * (p_raw - 0.5)``. Lanes the model under-predicts
   converge to ``alpha > 1`` (amplify); lanes it over-predicts converge to
   ``alpha < 1`` (shrink toward 0.5).

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
ALPHA_CLAMP_HI = 2.50
A_OBS_CLAMP = 5.0          # raw observation clamp before EWMA folds it in
SHRINK_N = 10              # blend toward identity below this sample count
PRIOR_A = 2.0
PRIOR_B = 3.0
DEV_FLOOR = 0.005          # |stated_prob - 0.5| guard before computing a_obs
SCHEMA_VERSION = 1


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
    ):
        self.path: Path = Path(path) if path is not None else DEFAULT_POSTERIORS_PATH
        self.shadow_mode: bool = bool(shadow_mode)
        self.min_samples_to_apply: int = max(0, int(min_samples_to_apply or 0))
        self._posteriors: Dict[str, LanePosterior] = {}
        self._load()

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

    def _shrunk_alpha(self, p: LanePosterior) -> float:
        """Blend ``alpha_ewma`` toward identity (1.0) when sample is small."""
        if p.n >= SHRINK_N:
            # Asymmetric clamp: positive alpha (over-predicting YES) capped at 1.0x;
            # negative alpha (under-predicting YES) allowed up to 2.5x.
            if p.alpha_ewma < 1.0:
                return 1.0
            return self._clamp(p.alpha_ewma, 1.0, ALPHA_CLAMP_HI)
        w = p.n / SHRINK_N
        blended = w * p.alpha_ewma + (1.0 - w) * 1.0
        if blended < 1.0:
            return 1.0
        return self._clamp(blended, 1.0, ALPHA_CLAMP_HI)

    # ---------------------------------------------------------------- API

    def alpha(self, lane_id: str) -> float:
        """Effective α used for correction (shrunk + clamped). Identity if unknown."""
        if not lane_id:
            return 1.0
        p = self._posteriors.get(lane_id)
        if p is None or p.n == 0:
            return 1.0
        if p.n < self.min_samples_to_apply:
            return 1.0
        return self._shrunk_alpha(p)

    def raw_alpha(self, lane_id: str) -> Optional[float]:
        """The unshrunk, unclamped EWMA value (or None if no samples)."""
        p = self._posteriors.get(lane_id)
        if p is None or p.n == 0:
            return None
        return p.alpha_ewma

    def posterior(self, lane_id: str) -> Dict[str, Any]:
        """Snapshot for telemetry: n, beta posterior mean, current α (shrunk)."""
        p = self._posteriors.get(lane_id)
        if p is None or p.n == 0:
            return {
                "n": 0,
                "alpha": 1.0,
                "alpha_raw": None,
                "beta_a": PRIOR_A,
                "beta_b": PRIOR_B,
                "beta_mean": PRIOR_A / (PRIOR_A + PRIOR_B),
                "min_samples_to_apply": self.min_samples_to_apply,
            }
        return {
            "n": p.n,
            "alpha": self._shrunk_alpha(p),
            "alpha_raw": p.alpha_ewma,
            "beta_a": p.beta_a,
            "beta_b": p.beta_b,
            "beta_mean": p.beta_a / (p.beta_a + p.beta_b),
            "min_samples_to_apply": self.min_samples_to_apply,
        }

    def calibrate(self, lane_id: str, raw_est_prob: float) -> float:
        """Return calibrated p (identity in shadow mode).

        Math: ``p_cal = clamp(0.5 + alpha * (p_raw - 0.5), 0.01, 0.99)``.
        The downstream caller clamps to its own range; we use a conservative
        absolute bound to avoid returning probabilities outside (0, 1).
        """
        try:
            p_raw = float(raw_est_prob)
        except (TypeError, ValueError):
            return 0.5
        if self.shadow_mode:
            return p_raw
        a = self.alpha(lane_id)
        p_cal = 0.5 + a * (p_raw - 0.5)
        # Safety clamp — never hand off something outside (0, 1).
        return max(0.01, min(0.99, p_cal))

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

        p = self._posteriors.setdefault(lane, LanePosterior.fresh())

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
