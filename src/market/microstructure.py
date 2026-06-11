"""Order-book / trade-flow microstructure features for up/down markets.

The TA stack (RSI/MACD/ATR/momentum) provably fails to separate winning from
losing trades on several alt up/down lanes — winners and losers are identical on
every logged TA feature. These two features look at the *market*'s own
microstructure instead, which the TA stack can't see:

- ``ob_imbalance``   — resting order-book depth skew (bid vs ask).
- ``trade_flow_ratio`` — executed taker pressure (who's hitting the book).

Pure functions only (no I/O); the scanner does the fetching and feeds these the
raw book / trades. They never raise — bad input returns ``None`` so the caller
can log a missing value instead of breaking the scan.
"""

from __future__ import annotations

from typing import Any, List, Optional


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if v == v else None  # drop NaN
    except (TypeError, ValueError):
        return None


def ob_imbalance(bids: Any, asks: Any, levels: int = 5) -> Optional[float]:
    """Price-weighted order-book imbalance over the best ``levels`` levels.

    ``bids``/``asks`` are lists of dicts with ``price`` and ``size`` (the shape
    returned by ``CLOBClient.fetch_order_book_snapshot``). Robust to input
    ordering: bids are taken highest-price-first, asks lowest-price-first.

    Returns ``bid_notional / (bid_notional + ask_notional)`` in ``[0, 1]``:
    ``>0.5`` = more bid depth (bullish lean), ``<0.5`` = more ask depth. Returns
    ``None`` when either side is empty/unusable (can't form a ratio).
    """
    if not isinstance(bids, list) or not isinstance(asks, list):
        return None

    def _levels(side: List[Any], best_high: bool) -> List[tuple]:
        out = []
        for o in side:
            if not isinstance(o, dict):
                continue
            p, s = _f(o.get("price")), _f(o.get("size"))
            if p is None or s is None or p <= 0 or s < 0:
                continue
            out.append((p, s))
        out.sort(key=lambda ps: ps[0], reverse=best_high)
        return out[:levels]

    best_bids = _levels(bids, best_high=True)
    best_asks = _levels(asks, best_high=False)
    if not best_bids or not best_asks:
        return None
    bid_notional = sum(p * s for p, s in best_bids)
    ask_notional = sum(p * s for p, s in best_asks)
    denom = bid_notional + ask_notional
    if denom <= 0:
        return None
    return bid_notional / denom


def trade_flow_ratio(trades: Any) -> Optional[float]:
    """Signed taker-flow ratio from recent trades on an up/down market.

    ``trades`` is a list of dicts with ``side`` (BUY/SELL), ``size``, and
    ``outcome`` (Up/Down) — the Polymarket Data-API ``/trades`` shape. A BUY of
    the Up outcome OR a SELL of the Down outcome is bullish taker pressure; the
    mirror is bearish.

    Returns ``net / total`` in ``[-1, 1]``: ``>0`` = net bullish taker flow.
    ``None`` when there are no usable trades.
    """
    if not isinstance(trades, list) or not trades:
        return None
    bullish = 0.0
    bearish = 0.0
    for t in trades:
        if not isinstance(t, dict):
            continue
        size = _f(t.get("size"))
        if size is None or size <= 0:
            continue
        side = str(t.get("side", "")).upper()
        outcome = str(t.get("outcome", "")).strip().lower()
        if outcome in ("up", "yes"):
            if side == "BUY":
                bullish += size
            elif side == "SELL":
                bearish += size
        elif outcome in ("down", "no"):
            if side == "BUY":
                bearish += size
            elif side == "SELL":
                bullish += size
    total = bullish + bearish
    if total <= 0:
        return None
    return (bullish - bearish) / total
