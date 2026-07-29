"""Entry-time TAPE SHADOW — fill-instant sub-30s spot micro-move, logged per entry.

WHY (2026-07-28 research, n=231 over 15 sessions): 100% of losing trades never go
green (mfe_pct ~ 0); 50% have mfe EXACTLY 0.0 (the mid never ticks up once). The bot
behaves like a MOMENTUM CHASER — 5m entries fire ~40% INTO the window (gated to
2.75-3.75 min remaining), i.e. AFTER the mid has already absorbed the move the signal
reacted to, so follow-through is structurally unlikely and reversion hits instantly.
Critically, counter-tape entries currently win MORE (OPP 47% vs ALIGN 38%), so the
eventual block rule may be EXHAUST (skip chasing an exhausted spike), NOT align. We do
not know which — so this SHADOW logs the RAW SIGNED spot move at the instant of fill so
both hypotheses can be tested post-hoc against the settled mfe/pnl (join on trade_id).

SHADOW ONLY: this module NEVER affects an entry, a size, or an exit. It maintains a
lightweight per-asset spot ring buffer (sampled off the existing 3s fast-exit tick) and
writes one row per entry to data/calibration/tape_entry_shadow.jsonl. Everything is
fail-open (any error -> no-op) so it can never destabilise the live bot.

Phase 2 (separate, later): a per-market yes-mid buffer for the mid micro-move. Not built
here because candidate markets are not WS-subscribed pre-entry (no sub-minute mid exists).

Usage of the collected data (post-hoc, after ~200 entries): join tape_entry_shadow.jsonl
to entries.jsonl EXIT records on trade_id; bucket never_green (mfe<=0.5%) vs winners by
signed spot_move_bps per lane; evaluate H_align vs H_exhaust; only then design enforce.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG = ROOT / "data" / "calibration" / "tape_entry_shadow.jsonl"

# strategy name -> Binance symbol for the spot buffer
STRATEGY_SYMBOL = {
    "bitcoin": "BTCUSDT",
    "eth_macro": "ETHUSDT",
    "sol_macro": "SOLUSDT",
    "xrp_macro": "XRPUSDT",
    "hype_macro": "HYPEUSDT",
    "doge_macro": "DOGEUSDT",
    "bnb_macro": "BNBUSDT",
}

_MAXLEN = 90  # ~4.5 min of history at a 3s sample cadence
_LOCK = threading.Lock()
_SPOT: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=_MAXLEN))


def sample_spot(symbol: str, spot: Optional[float], ts: float) -> None:
    """Append (ts, spot) for a symbol. Fail-open; called off the 3s exit tick."""
    try:
        if not symbol or spot is None:
            return
        spot = float(spot)
        if spot <= 0:
            return
        with _LOCK:
            _SPOT[symbol.upper()].append((float(ts), spot))
    except Exception:
        return


def _move_bps(symbol: str, now_ts: float, lookback_sec: float) -> Optional[float]:
    """Signed spot move (bps) from ~lookback_sec ago to the latest sample. None if the
    buffer doesn't reach back far enough (fail-open)."""
    try:
        with _LOCK:
            buf = list(_SPOT.get((symbol or "").upper(), ()))
        if len(buf) < 2:
            return None
        now_spot = buf[-1][1]
        target = now_ts - float(lookback_sec)
        prev = None
        for ts, sp in reversed(buf):
            if ts <= target:
                prev = sp
                break
        if prev is None or prev <= 0:
            return None
        return round(10000.0 * (now_spot - prev) / prev, 2)
    except Exception:
        return None


def capture_entry(
    *,
    trade_id: str,
    strategy: str,
    window: Optional[str],
    action: Optional[str],
    now_ts: float,
    lookbacks_sec: List[float],
    extra: Optional[Dict[str, Any]] = None,
    log_path: Optional[Path] = None,
) -> None:
    """Build one shadow row for an entry and append it. Fail-open — never raises."""
    try:
        symbol = STRATEGY_SYMBOL.get(strategy)
        a = (action or "").upper()
        side_dir = "LONG" if "YES" in a else ("SHORT" if "NO" in a else "?")
        row: Dict[str, Any] = {
            "trade_id": trade_id,
            "strategy": strategy,
            "symbol": symbol,
            "window": window,
            "side_dir": side_dir,
            "ts": round(float(now_ts), 3),
        }
        for lb in lookbacks_sec:
            row["spot_move_bps_%ds" % int(lb)] = (
                _move_bps(symbol, now_ts, lb) if symbol else None
            )
        # signed toward the side: >0 means the last Δ moved IN FAVOUR of the taken side
        base = row.get("spot_move_bps_%ds" % int(lookbacks_sec[0])) if lookbacks_sec else None
        if base is not None:
            row["favour_bps_first"] = round(base if side_dir == "LONG" else -base, 2)
        if extra:
            row.update(extra)
        path = Path(log_path) if log_path else DEFAULT_LOG
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        return
