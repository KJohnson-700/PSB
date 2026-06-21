"""Entry-liquidity shadow logger (instrumentation ONLY — never affects trading).

WHY (2026-06-21): deep-gap stop-losses on 5m markets (price gapping past the stop
to -28/-68%) are concentrated on THIN books, but trades.jsonl records no liquidity,
so the 5m liquidity floor can't be calibrated. This logs the market liquidity of
each TAKEN signal at creation, keyed by market_id, so gapped trades can be joined
to their entry liquidity and the floor set from data (not a guess).

Pure append-only side-effect, fully wrapped — it can NEVER raise into the scan/
trade loop and changes NO decision on any lane. Remove once the floor is calibrated.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _finite(v: Any) -> Optional[float]:
    """Coerce to float only if finite; NaN/Inf/garbage -> None (strict-JSONL safe)."""
    if v is None:
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None

logger = logging.getLogger(__name__)

_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data" / "calibration" / "entry_liquidity_shadow.jsonl"
)


def log_signal_liquidity(
    *,
    market_id: Any,
    strategy: str,
    window: Optional[str],
    action: Optional[str],
    liquidity: Any,
    est_prob: Any = None,
) -> None:
    """Append one liquidity record for a taken signal. Never raises."""
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market_id": str(market_id) if market_id is not None else None,
            "strategy": strategy,
            "window": window,
            "action": action,
            "liquidity": _finite(liquidity),
            "est_prob": _finite(est_prob),
        }
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # instrumentation must never break trading
        logger.debug("entry_liquidity_shadow log failed (ignored)", exc_info=True)
