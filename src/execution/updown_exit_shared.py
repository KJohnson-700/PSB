"""
Shared crypto up/down exit parameters and decision helpers.

Live path: ``PositionExitManager`` uses CLOB YES/NO mids from the scanner.

Backtest path: ``UpdownBacktestEngine._settle_updown_with_live_exit_proxy`` replays
the same *rules* against a synthetic token mark derived from underlying OHLCV
(``_proxy_yes_price_from_underlying``). Ordering of TP / % stop / cents time-stop
matches live, but fills and marks are **not** CLOB-identical — treat crypto
up/down backtest exits as an approximation for research, not proof of live PnL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Tuple

CRYPTO_UPDOWN_STRATEGIES: FrozenSet[str] = frozenset(
    {
        "bitcoin",
        "sol_macro",
        "eth_macro",
        "hype_macro",
        "xrp_macro",
    }
)


def symbol_to_strategy_name(symbol: str) -> str:
    """Map backtest symbol keys to ``trading.exit_rules.updown_overrides`` keys."""
    return {
        "BTC": "bitcoin",
        "SOL": "sol_macro",
        "ETH": "eth_macro",
        "XRP": "xrp_macro",
        "HYPE": "hype_macro",
    }.get(str(symbol).upper(), "sol_macro")


@dataclass(frozen=True)
class UpdownExitGlobals:
    take_profit_pct: float
    updown_stop_loss_pct: float
    updown_stop_cents: float
    updown_exit_window_mins: float
    updown_max_hold_mins: float
    updown_exit_window_max_fraction: float
    updown_stop_cents_high_entry: float
    updown_high_entry_threshold: float
    updown_in_profit_stop_trigger_pct: float
    updown_in_profit_stop_tighten_to_pct: float
    updown_overrides: Dict[str, Dict[str, float]]


def parse_updown_exit_globals(exit_cfg: Dict[str, Any]) -> UpdownExitGlobals:
    """Parse ``trading.exit_rules`` subset used by crypto up/down exits."""
    ec = exit_cfg or {}
    raw_overrides = ec.get("updown_overrides") or {}
    overrides: Dict[str, Dict[str, float]] = {}
    base_stop = float(ec.get("updown_stop_cents", 0.03) or 0.03)
    base_win = float(ec.get("updown_exit_window_mins", 2.0) or 2.0)
    base_hold = float(ec.get("updown_max_hold_mins", 20.0) or 20.0)
    base_frac = float(ec.get("updown_exit_window_max_fraction", 0.5) or 0.5)
    if isinstance(raw_overrides, dict):
        for strategy, o in raw_overrides.items():
            if not isinstance(o, dict):
                continue
            overrides[str(strategy)] = {
                "updown_stop_cents": float(o.get("updown_stop_cents", base_stop)),
                "updown_exit_window_mins": float(o.get("updown_exit_window_mins", base_win)),
                "updown_max_hold_mins": float(o.get("updown_max_hold_mins", base_hold)),
                "updown_exit_window_max_fraction": float(
                    o.get("updown_exit_window_max_fraction", base_frac)
                ),
            }
    return UpdownExitGlobals(
        take_profit_pct=float(ec.get("take_profit_pct", 0.15) or 0.15),
        updown_stop_loss_pct=float(ec.get("updown_stop_loss_pct", 0.20) or 0.20),
        updown_stop_cents=base_stop,
        updown_exit_window_mins=base_win,
        updown_max_hold_mins=base_hold,
        updown_exit_window_max_fraction=base_frac,
        updown_stop_cents_high_entry=float(ec.get("updown_stop_cents_high_entry", 0.02) or 0.02),
        updown_high_entry_threshold=float(ec.get("updown_high_entry_threshold", 0.60) or 0.60),
        updown_in_profit_stop_trigger_pct=float(
            ec.get("updown_in_profit_stop_trigger_pct", 0.0) or 0.0
        ),
        updown_in_profit_stop_tighten_to_pct=float(
            ec.get("updown_in_profit_stop_tighten_to_pct", 0.0) or 0.0
        ),
        updown_overrides=overrides,
    )


def resolve_updown_exit_params(
    g: UpdownExitGlobals, strategy_name: str
) -> Tuple[float, float, float, float]:
    """Per-strategy updown params; same tuple shape as ``PositionExitManager._resolve_updown_exit_params``."""
    ov = g.updown_overrides.get(str(strategy_name), {})
    return (
        float(ov.get("updown_stop_cents", g.updown_stop_cents)),
        float(ov.get("updown_exit_window_mins", g.updown_exit_window_mins)),
        float(ov.get("updown_max_hold_mins", g.updown_max_hold_mins)),
        float(ov.get("updown_exit_window_max_fraction", g.updown_exit_window_max_fraction)),
    )


def cents_stop_for_entry_price(
    base_stop_cents: float,
    entry_token_price: float,
    *,
    high_threshold: float,
    high_stop_cents: float,
) -> float:
    """Tighter cents stop when entry is high (less room before token → 0)."""
    if (
        high_threshold > 0
        and high_stop_cents > 0
        and entry_token_price >= high_threshold
    ):
        return float(high_stop_cents)
    return float(base_stop_cents)


def effective_updown_stop_loss_pct(
    base_pct: float,
    pnl_pct: float,
    *,
    in_profit_trigger_pct: float,
    tighten_to_pct: float,
) -> float:
    """In-profit tightening of the adverse percentage stop (live semantics)."""
    if (
        in_profit_trigger_pct > 0
        and pnl_pct >= in_profit_trigger_pct
        and 0 < tighten_to_pct < base_pct
    ):
        return float(tighten_to_pct)
    return float(base_pct)


def adverse_for_updown_cents_time_stop(
    *,
    entry_leg: str,
    outcome: str,
    current_yes: float,
    current_no: float,
    entry_price: float,
    up_stop_cents: float,
) -> bool:
    """Whether the near-expiry cents-based adverse condition trips (live-aligned)."""
    if entry_leg == "NO":
        return current_no <= entry_price - up_stop_cents
    if outcome == "NO":
        # Short YES: lent YES — adverse when YES rises against us.
        return current_yes >= entry_price + up_stop_cents
    return current_yes <= entry_price - up_stop_cents


def scaled_exit_window_mins(
    up_exit_window_mins: float,
    up_exit_window_max_fraction: float,
    mins_at_entry: float,
) -> float:
    """Cap the cents-stop window at a fraction of runway-at-entry (late-entry guard)."""
    if up_exit_window_max_fraction >= 1.0 or mins_at_entry <= 0:
        return float(up_exit_window_mins)
    cap = mins_at_entry * up_exit_window_max_fraction
    return float(min(up_exit_window_mins, cap))
