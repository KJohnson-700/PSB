"""Shared per-lane scan-timeout guard.

Strategy ``scan_and_analyze`` coroutines call blocking ``get_full_analysis()``
(synchronous Binance / Hyperliquid HTTP) directly on the asyncio event loop.
A single slow data source therefore blocks the loop — observed 2026-06-16:
hyperliquid ``/info`` taking 43s on the ``hype_macro`` lane, overrunning the
60s cycle to 92s and starving exits + the dashboard ``/api/*`` endpoints.

``analysis_with_timeout`` runs the blocking call in a worker thread with a hard
per-lane timeout. The event loop stays free (exits/dashboard keep serving) and
a stalled lane is skipped for the cycle instead of wedging the whole bot.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_SCAN_TIMEOUT_SEC = 15.0


async def analysis_with_timeout(
    fn: Callable[[], T],
    *,
    lane: str,
    timeout_sec: float = DEFAULT_SCAN_TIMEOUT_SEC,
) -> Optional[T]:
    """Run blocking ``fn`` off-loop with a hard timeout.

    Returns ``fn()`` on success, or ``None`` on timeout/error. Callers already
    treat a falsy analysis as "no data this cycle" and sit out, so a timed-out
    lane degrades to a one-cycle skip. On timeout the worker thread keeps
    running but its result is discarded; the event loop is freed immediately.
    """
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout_sec)
    except asyncio.TimeoutError:
        logger.warning(
            "strategy_scan_timeout: %s analysis exceeded %.0fs — skipping lane this cycle",
            lane,
            timeout_sec,
        )
        return None
    except Exception as exc:  # defensive: get_full_analysis already returns None on its own errors
        logger.warning("strategy_scan_error: %s analysis failed: %s", lane, exc)
        return None
