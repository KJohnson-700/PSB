"""Fee-aware edge admission (2026-07-29).

WHY: the bot's `edge = est_prob - price` is a PRE-FEE quantity. On Olympus (the run
that won ~$80 at high WR) the venue charged $0, so pre-fee edge == net edge and the
min_edge gates were correct. On Polymarket direct-CLOB the taker fee is real:

    fee_per_share = taker_fee_rate * p * (1 - p)      # p = fill price of the token bought

and it is SYMMETRIC in p (so fee(yes_price) == fee(no_price) — side-agnostic). The
admission gates (min_edge 0.04-0.09) were calibrated in the fee=$0 era and never
subtract this, so they admit trades that are +EV pre-fee and -EV after the fee — the
mechanical reason lanes that won on Olympus bleed on Polymarket-local.

This returns the fee HURDLE in edge units to subtract from raw edge (equivalently, add
to min_edge) so admission is net-of-fee. VENUE-CONDITIONAL: keyed off
trading.execution_provider — Olympus uses its own rate (default 0, matching every
smoke receipt), direct CLOB uses the Polymarket taker rate. CONFIG-GATED + reversible:
disabled -> returns 0.0 -> byte-identical to the old gate.

    trading:
      fee_aware_edge:
        enabled: true
        taker_fee_rate: 0.07          # Polymarket crypto up/down taker (Gamma feeSchedule, verified 06-13)
        olympus_taker_fee_rate: 0.0   # Olympus smokes showed $0; set if a real Olympus fee is confirmed
        fee_legs: 1.0                 # 1.0 = entry-leg only (hold-to-resolution floor); ->2.0 = full round-trip taker
"""
from typing import Any, Dict


def _execution_provider(config: Dict[str, Any]) -> str:
    trading = config.get("trading") or {}
    return str(trading.get("execution_provider") or config.get("execution_provider") or "").lower()


def fee_aware_edge_hurdle(config: Dict[str, Any], price: Any) -> float:
    """Fee hurdle (in edge units) to subtract from raw edge before the min_edge gate.

    Returns 0.0 when disabled, on a bad/degenerate price, or on a $0-fee venue — so a
    disabled config is a pure no-op. price = the yes_price (fee is price-symmetric, so
    the same value applies to BUY_YES buying YES@p and BUY_NO buying NO@(1-p))."""
    cfg = (config.get("trading") or {}).get("fee_aware_edge") or {}
    if not cfg.get("enabled", False):
        return 0.0
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 0.0
    if not (0.0 < p < 1.0):
        return 0.0
    if _execution_provider(config) == "olympus":
        rate = float(cfg.get("olympus_taker_fee_rate", 0.0) or 0.0)
    else:
        rate = float(cfg.get("taker_fee_rate", 0.07) or 0.0)
    if rate <= 0.0:
        return 0.0
    legs = float(cfg.get("fee_legs", 1.0) or 0.0)
    if legs <= 0.0:
        return 0.0
    return rate * p * (1.0 - p) * legs
