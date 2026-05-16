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
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Optional, Tuple

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
    updown_lane_overrides: Dict[str, Dict[str, float]]
    updown_overrides: Dict[str, Dict[str, float]]


@dataclass(frozen=True)
class UpdownResolvedExitParams:
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


_UPDOWN_EXIT_PARAM_KEYS = frozenset(
    {
        "take_profit_pct",
        "updown_stop_loss_pct",
        "updown_stop_cents",
        "updown_exit_window_mins",
        "updown_max_hold_mins",
        "updown_exit_window_max_fraction",
        "updown_stop_cents_high_entry",
        "updown_high_entry_threshold",
        "updown_in_profit_stop_trigger_pct",
        "updown_in_profit_stop_tighten_to_pct",
    }
)


def _normalize_override_map(raw: Any) -> Dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, float] = {}
    for key in _UPDOWN_EXIT_PARAM_KEYS:
        if key in raw:
            out[key] = float(raw[key])
    return out


def _normalize_strategy_overrides(raw_overrides: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw_overrides, dict):
        return out
    for strategy, raw in raw_overrides.items():
        if not isinstance(raw, dict):
            continue
        strategy_cfg: Dict[str, Any] = _normalize_override_map(raw)
        lane_overrides: Dict[str, Dict[str, float]] = {}
        for lane, lane_raw in (raw.get("lane_overrides") or {}).items():
            lane_overrides[str(lane)] = _normalize_override_map(lane_raw)
        if lane_overrides:
            strategy_cfg["lane_overrides"] = lane_overrides
        window_lane_overrides: Dict[str, Dict[str, Dict[str, float]]] = {}
        for window, window_raw in (raw.get("window_lane_overrides") or {}).items():
            if not isinstance(window_raw, dict):
                continue
            lane_map: Dict[str, Dict[str, float]] = {}
            for lane, lane_raw in window_raw.items():
                lane_map[str(lane)] = _normalize_override_map(lane_raw)
            if lane_map:
                window_lane_overrides[str(window)] = lane_map
        if window_lane_overrides:
            strategy_cfg["window_lane_overrides"] = window_lane_overrides
        out[str(strategy)] = strategy_cfg
    return out


def parse_updown_exit_globals(exit_cfg: Dict[str, Any]) -> UpdownExitGlobals:
    """Parse ``trading.exit_rules`` subset used by crypto up/down exits."""
    ec = exit_cfg or {}
    base_stop = float(ec.get("updown_stop_cents", 0.03) or 0.03)
    base_win = float(ec.get("updown_exit_window_mins", 2.0) or 2.0)
    base_hold = float(ec.get("updown_max_hold_mins", 20.0) or 20.0)
    base_frac = float(ec.get("updown_exit_window_max_fraction", 0.5) or 0.5)
    lane_overrides: Dict[str, Dict[str, float]] = {}
    for lane, raw in (ec.get("updown_lane_overrides") or {}).items():
        lane_overrides[str(lane)] = _normalize_override_map(raw)
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
        updown_lane_overrides=lane_overrides,
        updown_overrides=_normalize_strategy_overrides(ec.get("updown_overrides") or {}),
    )


def resolve_updown_exit_params(
    g: UpdownExitGlobals, strategy_name: str
) -> Tuple[float, float, float, float]:
    """Compatibility helper returning the legacy tuple shape."""
    ov = g.updown_overrides.get(str(strategy_name), {}) if isinstance(g.updown_overrides, dict) else {}
    return (
        float(ov.get("updown_stop_cents", g.updown_stop_cents)),
        float(ov.get("updown_exit_window_mins", g.updown_exit_window_mins)),
        float(ov.get("updown_max_hold_mins", g.updown_max_hold_mins)),
        float(ov.get("updown_exit_window_max_fraction", g.updown_exit_window_max_fraction)),
    )


def resolve_updown_lane(*, entry_leg: str, outcome: str) -> str:
    leg = str(entry_leg or "YES").upper()
    out = str(outcome or "YES").upper()
    if leg == "NO" or out == "NO":
        return "down"
    return "up"


def infer_updown_window_size(
    window_size: Optional[str],
    *,
    opened_at: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> str:
    w = str(window_size or "").strip().lower()
    if w in {"5m", "15m", "30m"}:
        return w
    if opened_at is None or end_date is None:
        return ""
    start = opened_at
    finish = end_date
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if finish.tzinfo is None:
        finish = finish.replace(tzinfo=timezone.utc)
    mins = (finish - start).total_seconds() / 60.0
    rounded = int(round(mins))
    if abs(mins - 5.0) <= 0.6 or rounded == 5:
        return "5m"
    if abs(mins - 15.0) <= 0.6 or rounded == 15:
        return "15m"
    if abs(mins - 30.0) <= 1.0 or rounded == 30:
        return "30m"
    return ""


def resolve_updown_exit_params_for_position(
    g: UpdownExitGlobals,
    *,
    strategy_name: str,
    window_size: Optional[str],
    entry_leg: str,
    outcome: str,
    opened_at: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> UpdownResolvedExitParams:
    lane = resolve_updown_lane(entry_leg=entry_leg, outcome=outcome)
    resolved_window = infer_updown_window_size(window_size, opened_at=opened_at, end_date=end_date)
    strategy_cfg = g.updown_overrides.get(str(strategy_name), {})
    params: Dict[str, float] = {
        "take_profit_pct": g.take_profit_pct,
        "updown_stop_loss_pct": g.updown_stop_loss_pct,
        "updown_stop_cents": g.updown_stop_cents,
        "updown_exit_window_mins": g.updown_exit_window_mins,
        "updown_max_hold_mins": g.updown_max_hold_mins,
        "updown_exit_window_max_fraction": g.updown_exit_window_max_fraction,
        "updown_stop_cents_high_entry": g.updown_stop_cents_high_entry,
        "updown_high_entry_threshold": g.updown_high_entry_threshold,
        "updown_in_profit_stop_trigger_pct": g.updown_in_profit_stop_trigger_pct,
        "updown_in_profit_stop_tighten_to_pct": g.updown_in_profit_stop_tighten_to_pct,
    }
    params.update(g.updown_lane_overrides.get(lane, {}))
    params.update({k: v for k, v in strategy_cfg.items() if k in _UPDOWN_EXIT_PARAM_KEYS})
    strategy_lane = strategy_cfg.get("lane_overrides", {})
    if isinstance(strategy_lane, dict):
        params.update(strategy_lane.get(lane, {}))
    strategy_window_lane = strategy_cfg.get("window_lane_overrides", {})
    if resolved_window and isinstance(strategy_window_lane, dict):
        window_cfg = strategy_window_lane.get(resolved_window, {})
        if isinstance(window_cfg, dict):
            params.update(window_cfg.get(lane, {}))
    return UpdownResolvedExitParams(
        take_profit_pct=float(params["take_profit_pct"]),
        updown_stop_loss_pct=float(params["updown_stop_loss_pct"]),
        updown_stop_cents=float(params["updown_stop_cents"]),
        updown_exit_window_mins=float(params["updown_exit_window_mins"]),
        updown_max_hold_mins=float(params["updown_max_hold_mins"]),
        updown_exit_window_max_fraction=float(params["updown_exit_window_max_fraction"]),
        updown_stop_cents_high_entry=float(params["updown_stop_cents_high_entry"]),
        updown_high_entry_threshold=float(params["updown_high_entry_threshold"]),
        updown_in_profit_stop_trigger_pct=float(params["updown_in_profit_stop_trigger_pct"]),
        updown_in_profit_stop_tighten_to_pct=float(params["updown_in_profit_stop_tighten_to_pct"]),
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
