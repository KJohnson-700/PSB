"""Order-book fill simulation for realistic paper P&L.

Paper/dry_run trading fills every order at the *requested* price — no spread, no
slippage, no partial fills. That makes paper exit P&L systematically optimistic
versus live, where a marketable order walks the book and pays progressively worse
prices as it consumes depth. This module turns a top-of-book ladder into a
size-weighted fill so paper P&L reflects what a real sweep would have realized.

Pure and side-effect free so it is trivially unit-testable; the caller supplies
the relevant side's ladder (bids for a SELL, asks for a BUY).
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple


def polymarket_taker_fee_usdc(size: float, price: float, fee_rate: float) -> float:
    """Return the USDC taker fee for a binary CLOB fill."""
    try:
        shares = max(0.0, float(size))
        p = min(1.0, max(0.0, float(price)))
        rate = max(0.0, float(fee_rate))
    except (TypeError, ValueError):
        return 0.0
    return round(shares * rate * p * (1.0 - p), 5)


def simulate_book_fill(
    side: str,
    size: float,
    levels: Sequence[Tuple[float, float]],
    *,
    marketable: bool = True,
    limit_price: Optional[float] = None,
    pad_remainder_at_worst: bool = False,
) -> Tuple[float, float]:
    """Walk a book ladder and return ``(avg_fill_price, filled_size)``.

    Args:
        side: ``"SELL"`` consumes the highest bids first; ``"BUY"`` consumes the
            lowest asks first.
        size: order size to fill.
        levels: iterable of ``(price, size)`` for the side we hit (bids for SELL,
            asks for BUY). Need not be sorted; non-positive prices/sizes ignored.
        marketable: when True (market / FAK) the sweep takes whatever is there
            until ``size`` is filled or the ladder is exhausted, ignoring
            ``limit_price``. When False (passive limit) it only fills levels at an
            acceptable price (SELL: price >= limit; BUY: price <= limit).
        limit_price: price bound for the non-marketable case and the fallback
            return price when nothing fills.
        pad_remainder_at_worst: when True (and at least one level filled), any size
            left after the ladder is exhausted is filled at the deepest consumed
            level price, so ``filled_size == size`` and the returned price is the
            full-size blend. Bounded-pessimistic: a real sweep through a thin book
            would pay at least this badly. Only meaningful when ``marketable``.

    Returns:
        ``(avg_fill_price, filled_size)`` — the size-weighted average over the
        filled portion and the filled size (``< size`` when the ladder runs out).
        When nothing fills, returns ``(best acceptable level or limit_price, 0.0)``.
    """
    s = str(side).upper()
    if s not in ("SELL", "BUY"):
        raise ValueError(f"side must be SELL or BUY, got {side!r}")

    clean = [
        (float(p), float(q))
        for p, q in levels
        if p is not None and q is not None and float(p) > 0 and float(q) > 0
    ]
    # SELL hits bids best-first = highest price; BUY hits asks best-first = lowest.
    clean.sort(key=lambda lvl: lvl[0], reverse=(s == "SELL"))

    def acceptable(price: float) -> bool:
        if marketable or limit_price is None:
            return True
        return price >= limit_price if s == "SELL" else price <= limit_price

    remaining = max(0.0, float(size))
    notional = 0.0
    filled = 0.0
    best_acceptable: Optional[float] = None
    worst_consumed: Optional[float] = None
    for price, avail in clean:
        if not acceptable(price):
            # Sorted best-first, so the first unacceptable level ends a passive fill.
            break
        if best_acceptable is None:
            best_acceptable = price
        take = min(remaining, avail)
        notional += take * price
        filled += take
        worst_consumed = price
        remaining -= take
        if remaining <= 1e-12:
            break

    if filled <= 0:
        fallback = best_acceptable if best_acceptable is not None else limit_price
        return (float(fallback) if fallback is not None else 0.0, 0.0)

    if pad_remainder_at_worst and remaining > 1e-12 and worst_consumed is not None:
        notional += remaining * worst_consumed
        filled += remaining
        remaining = 0.0

    return (notional / filled, filled)
