"""Favorite-lane net tracker — per (asset|window|side) rolling REALIZED NET of
FAVORITE closes, so the favorite sit-out can fire on the payoff-trap.

Why this exists (2026-08-10, operator-directed): favorite closes bleed in a
LANE-specific, WINDOW-specific way — 15m favorites round-trip to $0 (doge|15m|down
-$163, xrp|15m|up -$129, ...) while 1h favorites win (xrp|1h|down +$59). The
favorite sit-out hook (`favorite_lane.tape_sit_out_delta`, sol_macro/bitcoin
_favorite_lane_signals) already exists but was fed by `lane_tape_adapter`, whose
key mixes FAVORITE and BAND closes and whose signal is MFE/green-rate based — so a
favorite's net loss gets diluted (sol|15m|up read -0.013 = "loosen" while bleeding
-$99). Favorites are HIGH-WR-but-NEGATIVE-NET (payoff-trap), so a WR/green signal
cannot see them. This tracker isolates the FAVORITE closes and keys on realized
NET, the thing that is actually negative.

Adaptive + self-flipping: the sit-out reads avg-net-per-favorite-close over the
lane's recent window; when the lane's favorites stop bleeding (tape turned, or the
window rolls off the losers) the avg climbs back above the sit-out floor and the
lane re-admits — no hardcoded per-window/side direction, no tape-blind gate.

Mirrors adaptive_lane_sizer's build/write/read shape: build() reads the settled
trades journal, write() persists the per-lane state json, get_favorite_net() is the
cheap mtime-cached reader the strategy scan loops call. Dependency-free + fully
defensive: any error path yields "no signal" (None) so the sit-out fails OPEN
(never blocks a favorite on a read error).
"""

from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from typing import Dict, Optional

DEFAULT_TRADES_FILE = os.path.join("data", "calibration", "trades.jsonl")
DEFAULT_STATE_FILE = os.path.join("data", "calibration", "favorite_net_state.json")

_CACHE: Dict[str, object] = {"path": None, "mtime": 0.0, "data": {}}


def lane_key(asset: str, window: str, side: str) -> str:
    """Normalize (asset, window, side) -> 'asset|window|side' with side in up/down.

    Matches lane_tape_adapter.lane_key so readers/writers agree on the key shape.
    """
    a = str(asset or "").lower().replace("_macro", "").strip()
    w = str(window or "").lower().strip()
    s = str(side or "").lower().strip()
    if s in ("buy_yes", "yes", "long", "up"):
        s = "up"
    elif s in ("buy_no", "no", "short", "down"):
        s = "down"
    return f"{a}|{w}|{s}"


def _fav_floor(config: Optional[Dict]) -> float:
    try:
        return float(((config or {}).get("favorite_lane", {}) or {}).get("floor", 0.85) or 0.85)
    except (TypeError, ValueError):
        return 0.85


def build(config: Optional[Dict] = None, trades_path: str = DEFAULT_TRADES_FILE) -> Dict:
    """Read the settled trades journal and compute per-lane rolling favorite net.

    A row is a FAVORITE close when its entry_price >= the favorite floor (the same
    >=0.85 band the favorite lane admits at). Keeps the last `window` favorite closes
    per lane (chronological), and only lanes with >= `min_samples` emit a signal.
    Returns {"lanes": {key: {"avg_net": float, "net": float, "n": int}}, "meta": {...}}.
    Pure/defensive: unreadable rows are skipped.
    """
    cfg = ((config or {}).get("favorite_lane", {}) or {})
    window = int(cfg.get("fav_net_window", 12) or 12)
    min_samples = int(cfg.get("fav_net_min_samples", 5) or 5)
    floor = _fav_floor(config)
    # Per-window effective floor (e.g. {"1h": 0.90}) — MUST match the favorite lane's actual
    # admission (sol_macro/bitcoin _favorite_lane_signals use _window_floors.get(tf, floor)),
    # else the tracker counts 1h closes at 0.85-0.89 the live lane no longer admits.
    _window_floors = cfg.get("window_floors", {}) or {}

    per_lane: Dict[str, "deque"] = defaultdict(lambda: deque(maxlen=window))
    rows = []
    try:
        with open(trades_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                try:
                    ep = float(r.get("entry_price") or 0.0)
                except (TypeError, ValueError):
                    continue
                _eff_floor = floor
                try:
                    _eff_floor = float(_window_floors.get(str(r.get("window") or "").strip(), floor))
                except (TypeError, ValueError):
                    _eff_floor = floor
                if ep < _eff_floor:
                    continue  # band/dog close (or sub-window-floor 1h) — not an admitted favorite
                rows.append(r)
    except FileNotFoundError:
        return {"lanes": {}, "meta": {"floor": floor, "window": window, "min_samples": min_samples, "n_fav": 0}}

    rows.sort(key=lambda r: str(r.get("ts", "")))  # chronological → last `window` survive
    for r in rows:
        key = lane_key(r.get("strategy", ""), r.get("window", ""), r.get("action", ""))
        try:
            pnl = float(r.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        per_lane[key].append(pnl)

    lanes = {}
    for key, buf in per_lane.items():
        n = len(buf)
        if n < min_samples:
            continue
        net = float(sum(buf))
        lanes[key] = {"avg_net": round(net / n, 4), "net": round(net, 2), "n": n}
    return {
        "lanes": lanes,
        "meta": {"floor": floor, "window": window, "min_samples": min_samples, "n_fav": len(rows)},
    }


def write(state: Dict, path: str = DEFAULT_STATE_FILE) -> None:
    """Persist the built state atomically for the strategy readers."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except Exception:
        pass  # persistence is best-effort; a failed write just leaves the prior state


def get_favorite_net(strategy: str, window: str, side: str,
                     path: str = DEFAULT_STATE_FILE) -> Optional[float]:
    """Return the lane's AVG realized net per recent favorite close, or None.

    None = no signal (lane unknown / too few samples / missing file / read error) =>
    the sit-out must fail OPEN on None. mtime-cached so per-candidate reads are cheap.
    """
    try:
        mtime = os.path.getmtime(path)
        if _CACHE["path"] != path or _CACHE["mtime"] != mtime:
            with open(path) as fh:
                _CACHE["data"] = json.load(fh)
            _CACHE["path"] = path
            _CACHE["mtime"] = mtime
        row = ((_CACHE["data"] or {}).get("lanes") or {}).get(lane_key(strategy, window, side))
        if not row:
            return None
        return float(row.get("avg_net"))
    except Exception:
        return None
