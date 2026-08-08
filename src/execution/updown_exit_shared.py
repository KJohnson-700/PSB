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

import logging

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
        "doge_macro",
        "bnb_macro",
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
        "DOGE": "doge_macro",
        "BNB": "bnb_macro",
    }.get(str(symbol).upper(), "sol_macro")


@dataclass(frozen=True)
class UpdownExitGlobals:
    take_profit_pct: float
    updown_hold_winners_to_resolution: bool
    updown_stop_loss_pct: float
    updown_stop_cents: float
    updown_exit_window_mins: float
    updown_max_hold_mins: float
    updown_exit_window_max_fraction: float
    updown_flatten_before_resolution_sec: float
    updown_stop_cents_high_entry: float
    updown_high_entry_threshold: float
    updown_in_profit_stop_trigger_pct: float
    updown_in_profit_stop_tighten_to_pct: float
    updown_trail_arm_pct: float
    updown_trail_gap_pct: float
    dynamic_stop_enabled: bool
    dynamic_stop_bull_mult: float
    dynamic_stop_range_mult: float
    dynamic_stop_bear_mult: float
    dynamic_stop_high_vol_mult: float
    dynamic_stop_volatility_threshold: float
    dynamic_stop_low_convergence_mult: float
    dynamic_stop_high_convergence_mult: float
    dynamic_stop_low_convergence_threshold: float
    dynamic_stop_high_convergence_threshold: float
    updown_lane_overrides: Dict[str, Dict[str, float]]
    updown_overrides: Dict[str, Dict[str, float]]
    # ── Regime-conditioned exits (default OFF) ──────────────────────────────
    # When enabled, the hold/trail dimension is chosen at exit-resolution time
    # from (position side x BTC 1h regime): hold+trail only when the position is
    # trend-side in a trending tape (LONG in BULL, SHORT in BEAR); otherwise the
    # tight global/lane TP/SL is forced. Everything except the three hold/trail
    # keys is left to the existing lane resolution. See
    # ``_apply_regime_conditioned_exit``. EXIT change — ghost log cannot validate;
    # forward-test on trades_settled held-vs-realized gap convergence.
    regime_conditioned_exits_enabled: bool = False
    rce_trend_hold_winners: bool = True
    rce_trend_trail_arm_pct: float = 0.10
    rce_trend_trail_gap_pct: float = 0.15
    rce_off_trend_force_tight: bool = True
    # Lanes exempt from regime conditioning: each entry is "strategy" (all windows)
    # or "strategy|window". Exempt lanes keep their static config (e.g. take-profit),
    # never forced to hold. Used to keep BTC on pure take-profit while alts hold trend-side.
    rce_exclude: FrozenSet[str] = frozenset()
    # ── Time-gated late take-profit (default OFF: 0.0 = disabled everywhere) ──
    # 2026-07-17 operator GO. Banks a GREEN position only inside the final
    # `take_profit_late_gate_mins` of the market, and only if it is up >=
    # `take_profit_late_pct`. NOT gated by hold_winners — that is the point.
    # WHY: an UNGATED peak exit (trail 07-13, Prop-A 07-16, flat-TP sim 07-17)
    # kills the +85%% runner at minute 5 and lost every time. Winners on
    # btc/doge 1h|up peak at +75..127%% and resolve; round-trip losers cap at
    # +51..68%%. Gating on TIME separates them: a +40%% fade with 5min left is
    # not a +40%% runner with 50min left. Sim on live journal: btc|1h|up
    # +$14.80 -> +$53.26 (TP.40/gate5, positive at 6/6 TP levels); doge|1h|up
    # +$7.20 -> +$23.42 (TP.40/gate15, 5/6). Whole GATE<=20 block positive;
    # GATE30 and ungated mostly negative. CAVEAT: n=37/n=15, grid-picked —
    # the BLOCK positivity is the evidence, not any single cell; sim assumes a
    # fill at the qualifying tick, real near-resolution books may be worse.
    take_profit_late_pct: float = 0.0
    take_profit_late_gate_mins: float = 0.0
    # ── 5m HOLD-ALL (default OFF) ────────────────────────────────────────────
    # 2026-08-04 operator GO. Data: recent-8 5m n=62 realized WR 15% but HELD-to-
    # resolution WR 45% and sum(hold_minus_exit)=+$212 — stops/ngc amputate a coinflip's
    # winners (82% of 5m exits are stops). hold_5m_all forces EVERY 5m lane to hold-to-
    # resolution with a uniform loser-floor (hold_5m_loser_floor_pct) as the resolver's
    # FINAL word — one flag instead of smearing hold across 14 per-lane blocks. hold_means_hold
    # then suppresses ngc/stop/time/flatten; only the floor + -50% catastrophic fire. Reversible.
    hold_5m_all: bool = False
    hold_5m_loser_floor_pct: float = 0.30
    # 2026-08-06 (operator GO) HOLD-ALL — pure hold-to-resolution on EVERY window (not just 5m). The %
    # stop (updown_stop_loss) is THE leak: 243 exits / 2% WR / -$730, knifing directionally-right (57%
    # clean Binance) shorts at -29% right before they'd resolve green. Binary math: hold at 57% right =
    # +14%/trade; the stop locks ~-$3/trade. Forces hold=true + stop=0.0 (pure hold, loser-floor won't
    # fire); only the -50% catastrophic backstop remains. Safe NOW because sizing was flattened to $11-15
    # (a full loser is bounded) and <50% lanes are sat out. OFF by default => zero behavior change.
    hold_all: bool = False


@dataclass(frozen=True)
class UpdownResolvedExitParams:
    take_profit_pct: float
    updown_hold_winners_to_resolution: bool
    updown_stop_loss_pct: float
    updown_stop_cents: float
    updown_exit_window_mins: float
    updown_max_hold_mins: float
    updown_exit_window_max_fraction: float
    updown_flatten_before_resolution_sec: float
    updown_stop_cents_high_entry: float
    updown_high_entry_threshold: float
    updown_in_profit_stop_trigger_pct: float
    updown_in_profit_stop_tighten_to_pct: float
    updown_trail_arm_pct: float
    updown_trail_gap_pct: float
    dynamic_stop_enabled: bool
    dynamic_stop_bull_mult: float
    dynamic_stop_range_mult: float
    dynamic_stop_bear_mult: float
    dynamic_stop_high_vol_mult: float
    dynamic_stop_volatility_threshold: float
    dynamic_stop_low_convergence_mult: float
    dynamic_stop_high_convergence_mult: float
    dynamic_stop_low_convergence_threshold: float
    dynamic_stop_high_convergence_threshold: float
    take_profit_late_pct: float = 0.0
    take_profit_late_gate_mins: float = 0.0


_UPDOWN_EXIT_PARAM_KEYS = frozenset(
    {
        "take_profit_pct",
        "updown_hold_winners_to_resolution",
        "updown_stop_loss_pct",
        "updown_stop_cents",
        "updown_exit_window_mins",
        "updown_max_hold_mins",
        "updown_exit_window_max_fraction",
        "updown_flatten_before_resolution_sec",
        "updown_stop_cents_high_entry",
        "updown_high_entry_threshold",
        "updown_in_profit_stop_trigger_pct",
        "updown_in_profit_stop_tighten_to_pct",
        "updown_trail_arm_pct",
        "updown_trail_gap_pct",
        "dynamic_stop_enabled",
        "dynamic_stop_bull_mult",
        "dynamic_stop_range_mult",
        "dynamic_stop_bear_mult",
        "dynamic_stop_high_vol_mult",
        "dynamic_stop_volatility_threshold",
        "dynamic_stop_low_convergence_mult",
        "dynamic_stop_high_convergence_mult",
        "dynamic_stop_low_convergence_threshold",
        "dynamic_stop_high_convergence_threshold",
        "take_profit_late_pct",
        "take_profit_late_gate_mins",
    }
)


_LATE_TP_LANE_ONLY_KEYS = frozenset({"take_profit_late_pct", "take_profit_late_gate_mins"})

logger = logging.getLogger(__name__)

# Warn-once dedupe: the resolver runs per position per exit tick (~3s), so an
# unguarded warning here would flood the log.
_LATE_TP_MISPLACED_WARNED: set = set()


def _warn_misplaced_late_tp(dropped: Any, where: str) -> None:
    key = (where, tuple(sorted(dropped)))
    if key in _LATE_TP_MISPLACED_WARNED:
        return
    _LATE_TP_MISPLACED_WARNED.add(key)
    logger.warning(
        "late-TP key(s) %s set at %s are IGNORED — they are settable ONLY at "
        "updown_overrides.<strategy>.window_lane_overrides.<window>.<side>. "
        "Move them there or the late take-profit will never fire.",
        sorted(dropped),
        where,
    )


def _without_late_tp(overrides: Any, where: str = "a non-lane override layer") -> Dict[str, Any]:
    """Strip late-TP keys from any override layer broader than window+side.

    2026-07-20 HARDENING (Codex catch — the 07-17 guard was incomplete). late-TP must
    be settable ONLY at window_lane_overrides.<window>.<side>. Every broader layer
    would apply one value across multiple windows or assets — the asset-wide leak this
    key set exists to prevent:
      * the globals base (top-level trading.exit_rules) -> EVERY lane of EVERY asset
      * g.updown_lane_overrides[lane]                   -> one side, ALL strategies
      * strategy_cfg.lane_overrides[lane]               -> one strategy, ALL windows
    The original guard only filtered the strategy level, so a top-level
    `trading.exit_rules.take_profit_late_pct` still reached every lane.
    """
    if not isinstance(overrides, dict):
        return {}
    dropped = [k for k in overrides if k in _LATE_TP_LANE_ONLY_KEYS]
    if dropped:
        _warn_misplaced_late_tp(dropped, where)
    return {k: v for k, v in overrides.items() if k not in _LATE_TP_LANE_ONLY_KEYS}


def _normalize_override_map(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in _UPDOWN_EXIT_PARAM_KEYS:
        if key in raw:
            out[key] = (
                bool(raw[key])
                if key == "updown_hold_winners_to_resolution"
                else float(raw[key])
            )
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
    _misplaced_late_tp = [k for k in ec if k in _LATE_TP_LANE_ONLY_KEYS]
    if _misplaced_late_tp:
        _warn_misplaced_late_tp(_misplaced_late_tp, "trading.exit_rules (top level)")
    base_stop = float(ec.get("updown_stop_cents", 0.03) or 0.03)
    base_win = float(ec.get("updown_exit_window_mins", 2.0) or 2.0)
    base_hold = float(ec.get("updown_max_hold_mins", 20.0) or 20.0)
    base_frac = float(ec.get("updown_exit_window_max_fraction", 0.5) or 0.5)
    lane_overrides: Dict[str, Dict[str, float]] = {}
    for lane, raw in (ec.get("updown_lane_overrides") or {}).items():
        lane_overrides[str(lane)] = _normalize_override_map(raw)
    return UpdownExitGlobals(
        take_profit_pct=float(ec.get("take_profit_pct", 0.15) or 0.15),
        updown_hold_winners_to_resolution=bool(
            ec.get("updown_hold_winners_to_resolution", False)
        ),
        hold_5m_all=bool(ec.get("hold_5m_all", False)),
        hold_5m_loser_floor_pct=float(ec.get("hold_5m_loser_floor_pct", 0.30) or 0.30),
        hold_all=bool(ec.get("hold_all", False)),
        updown_stop_loss_pct=float(ec.get("updown_stop_loss_pct", 0.20) or 0.20),
        updown_stop_cents=base_stop,
        updown_exit_window_mins=base_win,
        updown_max_hold_mins=base_hold,
        updown_exit_window_max_fraction=base_frac,
        updown_flatten_before_resolution_sec=float(
            ec.get("updown_flatten_before_resolution_sec", 0.0) or 0.0
        ),
        updown_stop_cents_high_entry=float(ec.get("updown_stop_cents_high_entry", 0.02) or 0.02),
        updown_high_entry_threshold=float(ec.get("updown_high_entry_threshold", 0.60) or 0.60),
        updown_in_profit_stop_trigger_pct=float(
            ec.get("updown_in_profit_stop_trigger_pct", 0.0) or 0.0
        ),
        updown_in_profit_stop_tighten_to_pct=float(
            ec.get("updown_in_profit_stop_tighten_to_pct", 0.0) or 0.0
        ),
        updown_trail_arm_pct=float(ec.get("updown_trail_arm_pct", 0.0) or 0.0),
        updown_trail_gap_pct=float(ec.get("updown_trail_gap_pct", 0.0) or 0.0),
        # 2026-07-20 HARDENING: deliberately NOT parsed from the top level — the fields
        # keep their 0.0 defaults. Reading them here is what let a single
        # `trading.exit_rules.take_profit_late_pct` reach every lane of every asset.
        # Nothing consumes UpdownExitGlobals.take_profit_late_* directly (verified).
        dynamic_stop_enabled=bool(ec.get("dynamic_stop_enabled", True)),
        dynamic_stop_bull_mult=float(ec.get("dynamic_stop_bull_mult", 0.95) or 0.95),
        dynamic_stop_range_mult=float(ec.get("dynamic_stop_range_mult", 1.05) or 1.05),
        dynamic_stop_bear_mult=float(ec.get("dynamic_stop_bear_mult", 1.15) or 1.15),
        dynamic_stop_high_vol_mult=float(ec.get("dynamic_stop_high_vol_mult", 1.15) or 1.15),
        dynamic_stop_volatility_threshold=float(
            ec.get("dynamic_stop_volatility_threshold", 0.02) or 0.02
        ),
        dynamic_stop_low_convergence_mult=float(
            ec.get("dynamic_stop_low_convergence_mult", 1.10) or 1.10
        ),
        dynamic_stop_high_convergence_mult=float(
            ec.get("dynamic_stop_high_convergence_mult", 0.95) or 0.95
        ),
        dynamic_stop_low_convergence_threshold=float(
            ec.get("dynamic_stop_low_convergence_threshold", 0.55) or 0.55
        ),
        dynamic_stop_high_convergence_threshold=float(
            ec.get("dynamic_stop_high_convergence_threshold", 0.75) or 0.75
        ),
        updown_lane_overrides=lane_overrides,
        updown_overrides=_normalize_strategy_overrides(ec.get("updown_overrides") or {}),
        **_parse_regime_conditioned_exits(ec.get("regime_conditioned_exits") or {}),
    )


def _parse_regime_conditioned_exits(raw: Any) -> Dict[str, Any]:
    """Parse the optional ``regime_conditioned_exits`` block (all default-off)."""
    rc = raw if isinstance(raw, dict) else {}
    excl_raw = rc.get("exclude_lanes") or []
    exclude = frozenset(
        str(x or "").strip().lower() for x in excl_raw if str(x or "").strip()
    )
    return {
        "regime_conditioned_exits_enabled": bool(rc.get("enabled", False)),
        "rce_trend_hold_winners": bool(rc.get("trend_side_hold_winners", True)),
        "rce_trend_trail_arm_pct": float(rc.get("trend_side_trail_arm_pct", 0.10) or 0.0),
        "rce_trend_trail_gap_pct": float(rc.get("trend_side_trail_gap_pct", 0.15) or 0.0),
        "rce_off_trend_force_tight": bool(rc.get("off_trend_force_tight", True)),
        "rce_exclude": exclude,
    }


def _apply_regime_conditioned_exit(
    params: Dict[str, Any],
    *,
    lane: str,
    btc_1h_regime: Optional[str],
    g: UpdownExitGlobals,
    strategy_name: Any = None,
    window: Any = None,
) -> None:
    """Override only the hold/trail keys in ``params`` from (side x regime).

    No-op unless ``regime_conditioned_exits_enabled``. Trend-side (LONG in BULL,
    SHORT in BEAR) gets hold+trail; counter-trend or chop/unknown regime is forced
    to tight TP/SL (when ``rce_off_trend_force_tight``). Lanes in ``rce_exclude``
    (by "strategy" or "strategy|window") are skipped entirely so they keep their
    static config (e.g. pure take-profit). Mutates ``params`` in place.
    """
    if not g.regime_conditioned_exits_enabled:
        return
    if g.rce_exclude:
        strat = str(strategy_name or "").strip().lower()
        win = str(window or "").strip().lower()
        if strat and (strat in g.rce_exclude or f"{strat}|{win}" in g.rce_exclude):
            return
    regime = str(btc_1h_regime or "").strip().upper()
    ln = str(lane or "").strip().lower()
    trend_side = (ln == "up" and regime == "BULL") or (ln == "down" and regime == "BEAR")
    if trend_side:
        params["updown_hold_winners_to_resolution"] = bool(g.rce_trend_hold_winners)
        params["updown_trail_arm_pct"] = float(g.rce_trend_trail_arm_pct)
        params["updown_trail_gap_pct"] = float(g.rce_trend_trail_gap_pct)
    elif g.rce_off_trend_force_tight:
        params["updown_hold_winners_to_resolution"] = False
        params["updown_trail_arm_pct"] = 0.0
        params["updown_trail_gap_pct"] = 0.0


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
    if w in {"5m", "15m", "30m", "1h"}:
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
        return "30m"  # legacy: 30m product discontinued but historic positions persist
    if abs(mins - 60.0) <= 2.0 or rounded == 60:
        return "1h"
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
    btc_1h_regime: Optional[str] = None,
) -> UpdownResolvedExitParams:
    lane = resolve_updown_lane(entry_leg=entry_leg, outcome=outcome)
    resolved_window = infer_updown_window_size(window_size, opened_at=opened_at, end_date=end_date)
    strategy_cfg = g.updown_overrides.get(str(strategy_name), {})
    params: Dict[str, float] = {
        "take_profit_pct": g.take_profit_pct,
        "updown_hold_winners_to_resolution": g.updown_hold_winners_to_resolution,
        "updown_stop_loss_pct": g.updown_stop_loss_pct,
        "updown_stop_cents": g.updown_stop_cents,
        "updown_exit_window_mins": g.updown_exit_window_mins,
        "updown_max_hold_mins": g.updown_max_hold_mins,
        "updown_exit_window_max_fraction": g.updown_exit_window_max_fraction,
        "updown_flatten_before_resolution_sec": g.updown_flatten_before_resolution_sec,
        "updown_stop_cents_high_entry": g.updown_stop_cents_high_entry,
        "updown_high_entry_threshold": g.updown_high_entry_threshold,
        "updown_in_profit_stop_trigger_pct": g.updown_in_profit_stop_trigger_pct,
        "updown_in_profit_stop_tighten_to_pct": g.updown_in_profit_stop_tighten_to_pct,
        "updown_trail_arm_pct": g.updown_trail_arm_pct,
        "updown_trail_gap_pct": g.updown_trail_gap_pct,
        # 2026-07-20 HARDENING: seeded 0.0, NOT from `g`. parse_updown_exit_globals()
        # reads top-level trading.exit_rules, so seeding from g let a single top-level
        # key enable late-TP on every lane of every asset. Only window_lane_overrides
        # below may set these. See _without_late_tp().
        "take_profit_late_pct": 0.0,
        "take_profit_late_gate_mins": 0.0,
        "dynamic_stop_enabled": g.dynamic_stop_enabled,
        "dynamic_stop_bull_mult": g.dynamic_stop_bull_mult,
        "dynamic_stop_range_mult": g.dynamic_stop_range_mult,
        "dynamic_stop_bear_mult": g.dynamic_stop_bear_mult,
        "dynamic_stop_high_vol_mult": g.dynamic_stop_high_vol_mult,
        "dynamic_stop_volatility_threshold": g.dynamic_stop_volatility_threshold,
        "dynamic_stop_low_convergence_mult": g.dynamic_stop_low_convergence_mult,
        "dynamic_stop_high_convergence_mult": g.dynamic_stop_high_convergence_mult,
        "dynamic_stop_low_convergence_threshold": g.dynamic_stop_low_convergence_threshold,
        "dynamic_stop_high_convergence_threshold": g.dynamic_stop_high_convergence_threshold,
    }
    params.update(_without_late_tp(
        g.updown_lane_overrides.get(lane, {}), "trading.exit_rules.updown_lane_overrides"
    ))
    # 2026-07-17 (Codex catch), tightened 2026-07-20: late-TP keys are LANE-ONLY by
    # design. Allowing them at strategy level would apply one value to 5m+15m+1h at once
    # — the exact asset-wide gate problem this change exists to avoid.
    # window_lane_overrides.<window>.<side> ONLY (lane_overrides is no longer accepted).
    params.update({
        k: v for k, v in strategy_cfg.items()
        if k in _UPDOWN_EXIT_PARAM_KEYS and k not in _LATE_TP_LANE_ONLY_KEYS
    })
    strategy_lane = strategy_cfg.get("lane_overrides", {})
    if isinstance(strategy_lane, dict):
        params.update(_without_late_tp(
            strategy_lane.get(lane, {}),
            "updown_overrides.%s.lane_overrides" % strategy_name,
        ))
    strategy_window_lane = strategy_cfg.get("window_lane_overrides", {})
    if resolved_window and isinstance(strategy_window_lane, dict):
        window_cfg = strategy_window_lane.get(resolved_window, {})
        if isinstance(window_cfg, dict):
            params.update(window_cfg.get(lane, {}))
    # Final word (when enabled): pick hold/trail from (side x BTC 1h regime).
    _apply_regime_conditioned_exit(
        params, lane=lane, btc_1h_regime=btc_1h_regime, g=g,
        strategy_name=strategy_name, window=resolved_window,
    )
    # 2026-08-04 (operator GO) 5m HOLD-ALL — the TRUE final word for 5m. Forces every 5m lane
    # to hold-to-resolution with a uniform loser-floor (hold_means_hold suppresses ngc/stop/
    # time/flatten; only the floor + -50% catastrophic fire). Data: 5m realized 15% WR vs held
    # 45% / +$212 left on the table. dynamic_stop OFF so the floor fires at the raw pct (the
    # loser-floor requires effective==raw). Applied AFTER regime-conditioning so nothing re-tightens
    # a 5m hold. OFF by default (hold_5m_all:false) => zero behavior change.
    if getattr(g, "hold_5m_all", False) and resolved_window == "5m":
        params["updown_hold_winners_to_resolution"] = True
        # Codex 2026-08-04: only WIDEN a tight stop up to the floor — NEVER tighten a lane that is
        # already pure-hold (stop==0.0, rides to catastrophic) or has a lane-calibrated WIDER floor
        # (e.g. sol 5m down 0.40, doge 5m down 0.0). Uniform 0.30 would have guillotined their winners.
        _cur_stop = float(params.get("updown_stop_loss_pct", 0.0) or 0.0)
        _floor = float(getattr(g, "hold_5m_loser_floor_pct", 0.30) or 0.30)
        if _cur_stop != 0.0:
            params["updown_stop_loss_pct"] = max(_cur_stop, _floor)
        params["dynamic_stop_enabled"] = False
    # 2026-08-06 (operator GO) HOLD-ALL — the TRUE final word for EVERY window. PURE hold-to-resolution:
    # kills the % stop entirely (stop=0.0 => loser-floor won't fire; only the -50% catastrophic remains).
    # This is the fix for the -$730 / 2%-WR updown_stop_loss leak on directionally-right lanes. Applied
    # AFTER hold_5m_all + regime-conditioning so nothing re-tightens. OFF by default => zero behavior change.
    if getattr(g, "hold_all", False):
        params["updown_hold_winners_to_resolution"] = True
        params["updown_stop_loss_pct"] = 0.0
        params["dynamic_stop_enabled"] = False
    return UpdownResolvedExitParams(
        take_profit_pct=float(params["take_profit_pct"]),
        updown_hold_winners_to_resolution=bool(
            params["updown_hold_winners_to_resolution"]
        ),
        updown_stop_loss_pct=float(params["updown_stop_loss_pct"]),
        updown_stop_cents=float(params["updown_stop_cents"]),
        updown_exit_window_mins=float(params["updown_exit_window_mins"]),
        updown_max_hold_mins=float(params["updown_max_hold_mins"]),
        updown_exit_window_max_fraction=float(params["updown_exit_window_max_fraction"]),
        updown_flatten_before_resolution_sec=float(
            params["updown_flatten_before_resolution_sec"]
        ),
        updown_stop_cents_high_entry=float(params["updown_stop_cents_high_entry"]),
        updown_high_entry_threshold=float(params["updown_high_entry_threshold"]),
        updown_in_profit_stop_trigger_pct=float(params["updown_in_profit_stop_trigger_pct"]),
        updown_in_profit_stop_tighten_to_pct=float(params["updown_in_profit_stop_tighten_to_pct"]),
        updown_trail_arm_pct=float(params["updown_trail_arm_pct"]),
        updown_trail_gap_pct=float(params["updown_trail_gap_pct"]),
        dynamic_stop_enabled=bool(params["dynamic_stop_enabled"]),
        dynamic_stop_bull_mult=float(params["dynamic_stop_bull_mult"]),
        dynamic_stop_range_mult=float(params["dynamic_stop_range_mult"]),
        dynamic_stop_bear_mult=float(params["dynamic_stop_bear_mult"]),
        dynamic_stop_high_vol_mult=float(params["dynamic_stop_high_vol_mult"]),
        dynamic_stop_volatility_threshold=float(params["dynamic_stop_volatility_threshold"]),
        dynamic_stop_low_convergence_mult=float(params["dynamic_stop_low_convergence_mult"]),
        dynamic_stop_high_convergence_mult=float(params["dynamic_stop_high_convergence_mult"]),
        dynamic_stop_low_convergence_threshold=float(params["dynamic_stop_low_convergence_threshold"]),
        dynamic_stop_high_convergence_threshold=float(params["dynamic_stop_high_convergence_threshold"]),
        take_profit_late_pct=float(params.get("take_profit_late_pct", 0.0) or 0.0),
        take_profit_late_gate_mins=float(params.get("take_profit_late_gate_mins", 0.0) or 0.0),
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
    peak_pnl_pct: Optional[float] = None,
    in_profit_trigger_pct: float,
    tighten_to_pct: float,
    trail_arm_pct: float = 0.0,
    trail_gap_pct: float = 0.0,
    dynamic_stop_enabled: bool = False,
    btc_1h_regime: Optional[str] = None,
    entry_volatility: Optional[float] = None,
    convergence_score: Optional[float] = None,
    dynamic_stop_bull_mult: float = 1.0,
    dynamic_stop_range_mult: float = 1.0,
    dynamic_stop_bear_mult: float = 1.0,
    dynamic_stop_high_vol_mult: float = 1.0,
    dynamic_stop_volatility_threshold: float = 0.0,
    dynamic_stop_low_convergence_mult: float = 1.0,
    dynamic_stop_high_convergence_mult: float = 1.0,
    dynamic_stop_low_convergence_threshold: float = 0.0,
    dynamic_stop_high_convergence_threshold: float = 1.0,
) -> float:
    """In-profit tightening of the adverse percentage stop (live semantics)."""
    effective_base = float(base_pct)
    if dynamic_stop_enabled:
        regime = str(btc_1h_regime or "").upper()
        if regime == "BULL":
            effective_base *= float(dynamic_stop_bull_mult)
        elif regime == "RANGE":
            effective_base *= float(dynamic_stop_range_mult)
        elif regime == "BEAR":
            effective_base *= float(dynamic_stop_bear_mult)
        try:
            if (
                entry_volatility is not None
                and float(entry_volatility) >= float(dynamic_stop_volatility_threshold)
            ):
                effective_base *= float(dynamic_stop_high_vol_mult)
        except (TypeError, ValueError):
            pass
        try:
            if convergence_score is not None:
                c = float(convergence_score)
                if c < float(dynamic_stop_low_convergence_threshold):
                    effective_base *= float(dynamic_stop_low_convergence_mult)
                elif c >= float(dynamic_stop_high_convergence_threshold):
                    effective_base *= float(dynamic_stop_high_convergence_mult)
        except (TypeError, ValueError):
            pass

    trigger_pnl_pct = (
        max(float(pnl_pct), float(peak_pnl_pct))
        if peak_pnl_pct is not None
        else float(pnl_pct)
    )
    if (
        in_profit_trigger_pct > 0
        and trigger_pnl_pct >= in_profit_trigger_pct
        and 0 < tighten_to_pct < effective_base
    ):
        stop_mag = float(tighten_to_pct)
    else:
        stop_mag = float(effective_base)

    # Positive trailing floor: once the high-water mark clears ``trail_arm_pct``,
    # lock an exit floor at ``peak - trail_gap`` (which may itself be positive,
    # banking gains rather than only capping the loss). Returned as a possibly
    # negative magnitude so the caller's ``pnl <= -mag`` test fires at that
    # floor. Only ever more protective than the from-entry stop, never wider.
    if (
        trail_arm_pct > 0
        and trail_gap_pct > 0
        and peak_pnl_pct is not None
        and float(peak_pnl_pct) >= trail_arm_pct
    ):
        trail_floor = float(peak_pnl_pct) - float(trail_gap_pct)
        exit_floor = max(-stop_mag, trail_floor)
        result = -exit_floor
        # 2026-07-10: when trail_gap_pct == trail_arm_pct (now the common case --
        # see xrp 5m / btc 1h-up / the global default), a position that peaks at
        # EXACTLY the arm threshold computes a floor of exactly 0.0 (breakeven).
        # The caller (PositionExitManager) uses `effective_stop_loss_pct != 0` to
        # distinguish "a stop is configured" from "no stop" (the latter is the
        # deliberate xrp-5m-style base_pct=0.0 pre-arm state). An exact 0.0 here
        # would be misread as "no stop" and the check would be skipped for that
        # tick even though the trail IS armed and should fire at breakeven. Nudge
        # by an epsilon well below any real price-tick granularity so the armed
        # trail always evaluates, without moving the actual trigger level.
        if result == 0.0:
            result = -1e-9
        return result
    return stop_mag


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
