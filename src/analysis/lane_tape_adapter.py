"""Lane tape adapter — per-lane, per-side adaptive size multiplier.

The problem this solves (operator-directed, 2026-07-26): a lane's edge ROTATES
with the tape. The good session (+562) earned almost entirely on the SHORT side;
when the tape flips, those shorts idle and weaker lanes bleed at full size. Static
per-lane caps can't track that — they're set once and go stale. This adapter lets
the bot READ the tape per lane and scale its size DOWN as a lane degrades and back
UP as it recovers, symmetrically, without a human edit.

Signal (fast, not lagging): the max-favorable-excursion (``mfe_pct``) of a lane's
recent closes. A fill that never goes green (mfe ~ 0) before stopping is the
earliest evidence the tape turned against that side — it shows up 1-2 closes before
the realized-loss streak the old kill switch waits for. We combine the recent
GREEN-RATE (did fills reach +arm) with the recent realized sign, recency-weighted,
and map it to a multiplier in ``[min_mult, max_mult]``.

Keyed by lane = ``asset|window|side`` (side in {up, down}) — per-lane AND per-side,
never blanket. Symmetric: the same math sizes a lane back up when its fills start
going green again.

Modes (config ``lane_tape_adapter.mode``): off | shadow | live.
  - off:    multiplier always 1.0 (no effect).
  - shadow: computes + logs what it WOULD size; returns 1.0 (no live effect).
  - live:   returns the real multiplier, applied to final notional.

Pure/stateful but dependency-free so it is unit-testable and replayable over
historical session journals (see scripts/replay_lane_tape_adapter.py).
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple

# Decoupling: the adapter (writer, in main.py) persists per-lane admission deltas to
# this file; the strategy scan loops (readers) call get_tape_admission_delta() which
# reads it — no need to plumb the adapter object through every strategy. Mirrors how
# lane_posteriors.json / the get_loosen_min_edge_mult hook already work.
DEFAULT_TAPE_STATE_FILE = os.path.join("data", "calibration", "lane_tape_state.json")
_TAPE_CACHE: Dict[str, object] = {"path": None, "mtime": 0.0, "data": {}}


def get_tape_admission_delta(strategy: str, window: str, side: str,
                             path: str = DEFAULT_TAPE_STATE_FILE) -> float:
    """Read the persisted per-lane admission delta (min_edge adjustment).

    Returns 0.0 when the file is missing, the lane is unknown, or admission is off.
    Negative = loosen (more frequency on a winning lane); positive = tighten (fewer
    entries on a losing never-green lane). mtime-cached so per-candidate reads are cheap.
    Fully defensive — any error returns 0.0 (no admission change).
    """
    try:
        mtime = os.path.getmtime(path)
        if _TAPE_CACHE["path"] != path or _TAPE_CACHE["mtime"] != mtime:
            with open(path) as fh:
                _TAPE_CACHE["data"] = json.load(fh)
            _TAPE_CACHE["path"] = path
            _TAPE_CACHE["mtime"] = mtime
        key = lane_key(strategy, window, side)
        row = (_TAPE_CACHE["data"] or {}).get(key)
        if not row:
            return 0.0
        return float(row.get("admission_delta", 0.0) or 0.0)
    except Exception:
        return 0.0


def lane_key(asset: str, window: str, side: str) -> str:
    """Normalize (asset, window, side) -> 'asset|window|side' with side in up/down.

    ``side`` accepts BUY_YES/BUY_NO/up/down/long/short/yes/no (case-insensitive).
    """
    a = str(asset or "").lower().replace("_macro", "").strip()
    w = str(window or "").lower().strip()
    s = str(side or "").lower().strip()
    if s in ("buy_yes", "yes", "long", "up"):
        s = "up"
    elif s in ("buy_no", "no", "short", "down"):
        s = "down"
    return f"{a}|{w}|{s}"


@dataclass
class _Close:
    mfe_pct: float          # max favorable excursion this trade (0.10 = +10%)
    pnl: float              # realized $ (exit-sum truth)
    green: bool             # did it reach the green arm


@dataclass
class LaneState:
    buf: Deque[_Close] = field(default_factory=lambda: deque(maxlen=8))

    def add(self, c: _Close, maxlen: int) -> None:
        if self.buf.maxlen != maxlen:
            # re-window on config change
            self.buf = deque(self.buf, maxlen=maxlen)
        self.buf.append(c)


class LaneTapeAdapter:
    """Turns each lane's recent close-outcomes into a size multiplier."""

    def __init__(self, config: Optional[Dict] = None) -> None:
        cfg = dict(config or {})

        def _f(key, default):
            try:
                v = cfg.get(key, default)
                return float(v) if v is not None else float(default)
            except (TypeError, ValueError):
                return float(default)

        def _i(key, default):
            try:
                v = cfg.get(key, default)
                return int(v) if v is not None else int(default)
            except (TypeError, ValueError):
                return int(default)

        self.mode: str = str(cfg.get("mode", "off") or "off").lower()
        if self.mode not in ("off", "shadow", "live"):
            self.mode = "off"
        # rolling window of closes per lane (small = fast reaction)
        self.k: int = max(1, _i("window_closes", 5))
        # a close counts "green" if its mfe reached this favorable excursion
        self.green_arm: float = _f("green_arm_pct", 0.08)
        # need at least this many closes before adapting; below -> neutral 1.0
        self.min_samples: int = max(1, _i("min_samples", 2))
        # multiplier bounds. max_mult > 1.0 opts INTO data-driven upsize (operator
        # asked for size-up when a lane improves); default 1.0 = de-size only.
        self.min_mult: float = _f("min_mult", 0.25)
        self.max_mult: float = _f("max_mult", 1.0)
        # a lane whose recent recency-weighted avg pnl is <= -loss_ref de-sizes to
        # the floor; between 0 and -loss_ref it ramps. WINNER PROTECTION: a lane
        # with avg pnl >= 0 is never de-sized, however choppy (interspersed losers
        # in a net-profitable lane must not clip the big take-profits that follow).
        self.loss_ref: float = _f("loss_ref_dollars", 4.0)
        # green GATE: a net-losing lane is only de-sized to the extent its fills have
        # STOPPED going green. green_rate >= green_keep => no de-size (the lane still
        # reaches profit, so it's variance, not the tape turning — e.g. a choppy TP
        # lane whose big wins follow a loss patch). green_rate -> 0 => full de-size
        # (never-green = the tape genuinely turned against this side).
        self.green_keep: float = _f("green_keep_rate", 0.5)
        # recency: weight of the newest close relative to the oldest in the window.
        # 2.0 => newest counts 2x the oldest (linear ramp between).
        self.recency: float = _f("recency_ramp", 2.0)
        # --- dynamic ADMISSION (separate gate from sizing) ---
        # Adjusts a lane's effective min_edge with the tape: LOOSEN (negative delta =
        # admit more, MORE FREQUENCY) on a lane that's winning+going-green, TIGHTEN
        # (positive delta = admit only stronger edge) on a net-losing never-green lane.
        # Self-correcting: a loosened lane that starts losing flips to tighten within a
        # couple closes; a tightened lane that recovers loosens — no static floor to go
        # stale. Bounded so a bad read can't swing admission far. mode off|live.
        self.admission_mode: str = str(cfg.get("admission_mode", "off") or "off").lower()
        if self.admission_mode not in ("off", "live"):
            self.admission_mode = "off"
        self.loosen_max: float = _f("admission_loosen_max", 0.03)   # max min_edge cut on winners
        self.tighten_max: float = _f("admission_tighten_max", 0.05)  # max min_edge raise on losers
        self.win_ref: float = _f("admission_win_ref_dollars", 4.0)   # avg win at which loosen maxes
        self._lanes: Dict[str, LaneState] = {}

    # ---- ingestion -------------------------------------------------------
    def record_close(
        self,
        asset: str,
        window: str,
        side: str,
        *,
        mfe_pct: float,
        pnl: float,
    ) -> None:
        """Feed one settled close. Call at exit time."""
        key = lane_key(asset, window, side)
        st = self._lanes.setdefault(key, LaneState(deque(maxlen=self.k)))
        green = float(mfe_pct or 0.0) >= self.green_arm
        st.add(_Close(float(mfe_pct or 0.0), float(pnl or 0.0), green), self.k)

    def hydrate(self, closes) -> int:
        """Warm-start the per-lane buffers from recent closed trades (restart recovery).

        ``closes`` is an iterable of ``(asset, window, side, mfe_pct, pnl)`` in
        CHRONOLOGICAL order (oldest first). Each is fed through ``record_close`` so
        only the last ``k`` per lane survive in the rolling window — exactly what the
        live buffer would hold had the process never restarted. Pure/defensive: a bad
        row is skipped, never raised. Returns the count actually ingested.

        The adapter is de-size-only (max_mult <= 1.0), so the worst case of hydrating
        a stale close is "a lane starts slightly de-sized until a fresh close arrives"
        — it can never enlarge a position. Call ONCE at startup, only when ``_lanes``
        is empty (a hot-reload preserves ``_lanes`` and must NOT re-hydrate).
        """
        n = 0
        for row in closes:
            try:
                asset, window, side, mfe_pct, pnl = row
                self.record_close(
                    asset, window, side,
                    mfe_pct=float(mfe_pct or 0.0),
                    pnl=float(pnl or 0.0),
                )
                n += 1
            except Exception:
                continue
        return n

    # ---- scoring ---------------------------------------------------------
    def _weights(self, n: int):
        # linear recency ramp from 1.0 (oldest) to `recency` (newest)
        if n <= 1:
            return [1.0] * n
        step = (self.recency - 1.0) / (n - 1)
        return [1.0 + step * i for i in range(n)]  # index 0 = oldest

    def lane_stats(self, key: str) -> Optional[Tuple[float, float]]:
        """(recency-weighted avg pnl, weighted green-rate) or None if too few.

        avg_pnl >= 0 => lane is making money on its recent closes (tape aligned).
        avg_pnl <  0 => recently net-losing; green-rate says whether its fills are
        still going green at all (low = 'tape turned', de-size faster).
        """
        st = self._lanes.get(key)
        if st is None or len(st.buf) < self.min_samples:
            return None
        closes = list(st.buf)              # oldest -> newest
        w = self._weights(len(closes))
        wsum = sum(w)
        avg_pnl = sum(wi * c.pnl for wi, c in zip(w, closes)) / wsum
        green_rate = sum(wi * (1.0 if c.green else 0.0) for wi, c in zip(w, closes)) / wsum
        return avg_pnl, green_rate

    def raw_multiplier(self, asset: str, window: str, side: str) -> float:
        """The multiplier the adapter WOULD apply (ignores mode).

        Winner protection: recent avg pnl >= 0 -> full size (max_mult), always.
        Loser de-size: avg pnl in (-loss_ref, 0) ramps down; <= -loss_ref hits the
        floor. Never-green amplifier deepens the cut on a net-loser whose fills also
        stop going green (the fastest 'tape against this side' signal).
        """
        key = lane_key(asset, window, side)
        stats = self.lane_stats(key)
        if stats is None:
            return 1.0  # warmup / not enough data -> neutral
        avg_pnl, green_rate = stats
        if avg_pnl >= 0:
            return self.max_mult
        severity = min(1.0, (-avg_pnl) / self.loss_ref) if self.loss_ref > 0 else 1.0
        # green gate: scale the cut by how far the lane has stopped going green.
        # green_rate >= green_keep -> factor 0 (no de-size); 0 -> factor 1 (full).
        if self.green_keep > 0:
            green_factor = max(0.0, 1.0 - green_rate / self.green_keep)
        else:
            green_factor = 1.0
        severity *= green_factor
        return self.min_mult + (self.max_mult - self.min_mult) * (1.0 - severity)

    def size_multiplier(self, asset: str, window: str, side: str) -> float:
        """Live-effective multiplier: obeys mode (off/shadow -> 1.0)."""
        if self.mode != "live":
            return 1.0
        return self.raw_multiplier(asset, window, side)

    def raw_admission_delta(self, asset: str, window: str, side: str) -> float:
        """min_edge adjustment the adapter WOULD apply (ignores admission_mode).

        Negative = LOOSEN (admit more, more frequency) on a winning+green lane.
        Positive = TIGHTEN (admit only stronger edge) on a net-losing never-green lane.
        0 = warmup/neutral. Uses the SAME lane signal as sizing, so a lane the sizer
        is de-sizing also gets its admission tightened (consistent direction), and a
        healthy lane gets loosened to reclaim the frequency a static floor would cost.
        """
        key = lane_key(asset, window, side)
        stats = self.lane_stats(key)
        if stats is None:
            return 0.0
        avg_pnl, green_rate = stats
        if avg_pnl >= 0:
            # winning -> loosen, scaled by how strongly it wins AND still goes green
            strength = green_rate
            if self.win_ref > 0:
                strength *= min(1.0, avg_pnl / self.win_ref)
            return -self.loosen_max * strength
        # losing -> tighten, gated by never-green (same green gate as the sizer)
        severity = min(1.0, (-avg_pnl) / self.loss_ref) if self.loss_ref > 0 else 1.0
        if self.green_keep > 0:
            severity *= max(0.0, 1.0 - green_rate / self.green_keep)
        return self.tighten_max * severity

    def admission_delta(self, asset: str, window: str, side: str) -> float:
        """Live-effective min_edge delta: obeys admission_mode (off -> 0.0)."""
        if self.admission_mode != "live":
            return 0.0
        return self.raw_admission_delta(asset, window, side)

    def persist_state(self, path: str = DEFAULT_TAPE_STATE_FILE) -> None:
        """Write per-lane {size_mult, admission_delta} for the strategy readers.

        Writes the LIVE-effective values (0.0 admission delta when admission_mode is
        off; 1.0 size mult when mode is off) so the reader needs no mode knowledge.
        Atomic write; fully guarded so a persist failure never breaks the exit path.
        """
        try:
            snap = {}
            for key in self._lanes:
                parts = key.split("|")
                if len(parts) != 3:
                    continue
                a, w, s = parts
                snap[key] = {
                    "size_mult": round(self.size_multiplier(a, w, s), 4),
                    "admission_delta": round(self.admission_delta(a, w, s), 4),
                    "n": len(self._lanes[key].buf),
                }
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(snap, fh)
            os.replace(tmp, path)
        except Exception:
            pass

    def explain(self, asset: str, window: str, side: str) -> Dict:
        """Diagnostics for logging / dashboards."""
        key = lane_key(asset, window, side)
        st = self._lanes.get(key)
        n = len(st.buf) if st else 0
        stats = self.lane_stats(key)
        return {
            "lane": key,
            "mode": self.mode,
            "n": n,
            "avg_pnl": None if stats is None else round(stats[0], 3),
            "green_rate": None if stats is None else round(stats[1], 3),
            "raw_mult": round(self.raw_multiplier(asset, window, side), 4),
            "applied_mult": round(self.size_multiplier(asset, window, side), 4),
        }
