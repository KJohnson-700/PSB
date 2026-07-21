"""Deterministic pre-AI quality gates for short-window up/down candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional
import math


@dataclass(frozen=True)
class OracleValidation:
    passed: bool
    reason: str
    oracle_price: Optional[float]
    exchange_spot: Optional[float]
    basis_bps: Optional[float]
    freshness_sec: Optional[float]


@dataclass(frozen=True)
class CompositeScore:
    score: float
    passed: bool
    floor: float
    components: Dict[str, float]
    convergence_score: float
    reason: str


WEIGHTS: Dict[str, float] = {
    "edge_quality": 0.20,
    "quant_confidence": 0.15,
    "micro_momentum": 0.20,
    "timeframe_alignment": 0.15,
    "oracle_integrity": 0.15,
    "entry_timing": 0.10,
    "market_price_quality": 0.05,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def convergence_score_from_components(components: Dict[str, float]) -> float:
    """Consensus-style quality score: rewards broad agreement, penalizes dispersion."""
    vals = [_clamp(v) for v in components.values()]
    if not vals:
        return 0.0
    mean = sum(vals) / len(vals)
    strong_share = sum(1 for v in vals if v >= 0.65) / len(vals)
    variance = sum((v - mean) ** 2 for v in vals) / len(vals)
    std_penalty = min(0.25, math.sqrt(variance))
    return _clamp((0.60 * mean) + (0.40 * strong_share) - std_penalty)


def _freshness_seconds(updated_at: datetime, now: Optional[datetime]) -> float:
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return max(0.0, (ref - updated_at).total_seconds())


def validate_oracle_reference(
    *,
    oracle_price: Optional[float],
    exchange_spot: Optional[float],
    oracle_updated_at: Optional[datetime],
    max_age_sec: float,
    max_basis_bps: float,
    require_oracle: bool,
    now: Optional[datetime] = None,
    allow_exchange_when_oracle_missing: bool = False,
    stale_basis_relax_max_bps: Optional[float] = None,
    basis_relax_max_bps: Optional[float] = None,
    stale_spot_is_settlement: bool = False,
    stale_spot_settlement_max_basis_bps: Optional[float] = 500.0,
) -> OracleValidation:
    """Validate oracle freshness and basis against the exchange spot feed.

    ``allow_exchange_when_oracle_missing``: when Chainlink fields are absent but
    ``require_oracle`` is True, still admit up/down if exchange spot exists (basis
    integrity unknown — use only when ops accepts exchange-only resolution risk).

    ``stale_basis_relax_max_bps``: when the feed on-chain ``updatedAt`` is older than
    ``max_age_sec`` but spot vs oracle still agrees within this many bps, pass anyway
    (slow-updating feeds vs tight freshness caps — common on some alt feeds).
    """
    def _spot_positive(sp: Optional[float]) -> Optional[float]:
        if sp is None:
            return None
        try:
            v = float(sp)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    spot_ok = _spot_positive(exchange_spot)

    if oracle_price is None and oracle_updated_at is None:
        if require_oracle and allow_exchange_when_oracle_missing and spot_ok is not None:
            return OracleValidation(
                passed=True,
                reason="oracle_exchange_only_missing_chainlink",
                oracle_price=None,
                exchange_spot=spot_ok,
                basis_bps=None,
                freshness_sec=None,
            )
        return OracleValidation(
            passed=not require_oracle,
            reason="oracle_missing" if require_oracle else "oracle_optional_missing",
            oracle_price=None,
            exchange_spot=exchange_spot,
            basis_bps=None,
            freshness_sec=None,
        )

    if oracle_price is None or oracle_updated_at is None or exchange_spot is None:
        return OracleValidation(
            passed=not require_oracle,
            reason="oracle_missing" if require_oracle else "oracle_optional_missing",
            oracle_price=oracle_price,
            exchange_spot=exchange_spot,
            basis_bps=None,
            freshness_sec=None,
        )

    oracle_f = float(oracle_price)
    spot_f = float(exchange_spot)
    if oracle_f <= 0 or spot_f <= 0:
        return OracleValidation(
            passed=not require_oracle,
            reason="oracle_missing" if require_oracle else "oracle_optional_invalid",
            oracle_price=oracle_price,
            exchange_spot=exchange_spot,
            basis_bps=None,
            freshness_sec=None,
        )

    freshness = _freshness_seconds(oracle_updated_at, now)
    basis = ((spot_f - oracle_f) / oracle_f) * 10000.0
    if freshness > float(max_age_sec):
        # 2026-07-03 staleness RECOVERY (operator order): when the lane's exchange
        # spot IS the market's settlement source (hype: HL native mid, 5s TTL),
        # a slow Chainlink round must not starve the lane — the fresh spot is the
        # better truth. Basis vs the stale print is only used as a gross-failure
        # cap (bad spot print / feed insanity), not as an agreement test.
        if stale_spot_is_settlement:
            # Codex amend 2026-07-04: cap must be FINITE — it is the only sanity
            # check left against a bad spot print when the oracle is stale.
            try:
                _gross_cap = float(stale_spot_settlement_max_basis_bps)
            except (TypeError, ValueError):
                _gross_cap = 500.0
            if abs(basis) <= _gross_cap:
                return OracleValidation(
                    passed=True,
                    reason="oracle_stale_settlement_spot_ok",
                    oracle_price=oracle_f,
                    exchange_spot=spot_f,
                    basis_bps=basis,
                    freshness_sec=freshness,
                )
        relax_cap = stale_basis_relax_max_bps
        if relax_cap is not None and abs(basis) <= float(relax_cap):
            return OracleValidation(
                passed=True,
                reason="oracle_stale_basis_relaxed",
                oracle_price=oracle_f,
                exchange_spot=spot_f,
                basis_bps=basis,
                freshness_sec=freshness,
            )
        return OracleValidation(
            passed=False,
            reason="oracle_stale",
            oracle_price=oracle_f,
            exchange_spot=spot_f,
            basis_bps=basis,
            freshness_sec=freshness,
        )
    if abs(basis) > float(max_basis_bps):
        if basis_relax_max_bps is not None and abs(basis) <= float(basis_relax_max_bps):
            return OracleValidation(
                passed=True,
                reason="oracle_basis_relaxed",
                oracle_price=oracle_f,
                exchange_spot=spot_f,
                basis_bps=basis,
                freshness_sec=freshness,
            )
        return OracleValidation(
            passed=False,
            reason="oracle_basis_block",
            oracle_price=oracle_f,
            exchange_spot=spot_f,
            basis_bps=basis,
            freshness_sec=freshness,
        )
    return OracleValidation(
        passed=True,
        reason="oracle_ok",
        oracle_price=oracle_f,
        exchange_spot=spot_f,
        basis_bps=basis,
        freshness_sec=freshness,
    )


def apply_fresh_cross_override(
    *,
    est_prob_up: float,
    action: str,
    allowed_side: str,
    direction: str,
    side_source: Optional[str],
    reason_parts: list,
    crossover: Optional[str],
    tf_label: str,
    faster_crossover: Optional[str] = None,
    faster_tf_label: Optional[str] = None,
    strategy_name: str = "",
    primary_htf_bias: str = "",
    logger=None,
    enabled: bool = True,
    rsi_14: Optional[float] = None,
    window: str = "",
    momentum_flip_enabled: bool = False,
    momentum_flip_min_rsi: float = 55.0,
    macd_hist_5m: Optional[float] = None,
    macd_flip_enabled: bool = False,
    macd_flip_long_to_short_enabled: bool = False,
    long_to_short_enabled: bool = False,  # F6 2026-07-14: gate the LONG->SHORT fresh-cross mirror (default OFF; shorts into strength in a rising tape). Keep protective SHORT->LONG always.
):
    """Flip to the momentum side when a FRESH MACD cross contradicts the
    lagging-bias-chosen side.

    Background (2026-06-04): direction is selected from a lagging trend label and
    ``est_prob_up`` is pinned to it, so in a reversal the bot keeps choosing the
    trend-following side and shorts a rising market (observed: 5m hist bullish 84%
    while bot 96% short into a tape that rose on every asset). ``est_prob_up`` is
    P(UP) and side-independent, so flipping the action/side and pulling est_prob
    across neutral yields a real-edge trade in the direction price just turned.
    Only fires on a discrete cross; downstream edge/price-band/oracle gates still
    apply. Pure function — returns the updated tuple, appends to ``reason_parts``.

    FASTER-TF-LEADS (2026-06-05): a market checks its OWN-timeframe cross first,
    then optionally a NEXT-FASTER timeframe's cross (``faster_crossover``) as a
    leading trigger. The 5m fast MACD catches reversals the slow 15m/1h MACD lags
    by minutes, so 15m markets read 5m and 1h markets read 15m — otherwise the
    slow windows stay short-jammed in a rising tape (1h produced 0 flips, ~all
    candidates SHORT). 5m markets pass no faster TF.

    RSI MOMENTUM FLIP (2026-06-08, default-off via ``momentum_flip_enabled``):
    a separate, standing flip for the case the fresh-cross path can't catch — a
    SUSTAINED rising 1h tape where the cross already happened and the lagging
    bias stays BEARISH-short (fresh cross fired 0/77k live). When enabled, a
    BEARISH-bias SHORT on the ``window=="1h"`` market with ``rsi_14 >=
    momentum_flip_min_rsi`` (default 55) flips to LONG. Ghost-validated 1h-only
    (55-60 -> 63% UP, 60-65 -> 69%); asymmetric (SHORT->LONG only); never
    overrides a genuine fresh bearish cross (guarded on ``flipped is None``).
    NOTE: ``rsi_14`` is the caller's canonical RSI (15m for sol-family/eth, 4h for
    BTC), NOT the 1h RSI — ``window`` gates the market horizon, not the RSI TF.
    """
    if not enabled:
        return est_prob_up, action, allowed_side, direction, side_source

    def _src(want: str) -> Optional[str]:
        # own-TF preferred; fall back to the faster leading TF
        if crossover == want:
            return tf_label
        if faster_crossover == want:
            return faster_tf_label or tf_label
        return None

    flipped = None
    src_tf = None
    if allowed_side == "SHORT":
        src_tf = _src("BULLISH_CROSS")
        if src_tf is not None:
            est_prob_up = max(est_prob_up, 0.55)
            flipped = "BUY_YES"
    elif allowed_side == "LONG" and long_to_short_enabled:
        src_tf = _src("BEARISH_CROSS")
        if src_tf is not None:
            est_prob_up = min(est_prob_up, 0.45)
            flipped = "BUY_NO"
    if flipped is not None and flipped != action:
        tf_label = src_tf or tf_label
        action = flipped
        direction = "UP" if action == "BUY_YES" else "DOWN"
        allowed_side = "LONG" if action == "BUY_YES" else "SHORT"
        side_source = f"{side_source or ''}+fresh_{tf_label}_cross_flip"
        reason_parts.append(f"fresh_{tf_label}_cross_flip->{action}")
        if logger is not None:
            logger.info(
                "  %s FRESH %s CROSS FLIP -> %s (vs lagging bias %s)",
                strategy_name, tf_label, action, primary_htf_bias,
            )

    # Standing RSI momentum-disagree flip (2026-06-08, DEFAULT-OFF). The fresh-cross
    # flip above only fires on a discrete cross event (measured 0/77k live) so it
    # cannot rescue a SUSTAINED rising tape where the cross already happened and the
    # lagging bias stays BEARISH-short. Ghost validation (rejected_candidates_settled,
    # ts>=06-01): for BEARISH-bias SHORT candidates on the 1h MARKET window the
    # LONG-flip win rate rises monotonically with RSI and only clears coin-flip in
    # the disagreement tail — 55-60 -> 63% UP (n=328), 60-65 -> 69% (n=130). 5m/15m
    # MARKET-window tails too thin to trust, so this gate is 1h-market-only.
    #
    # RSI timeframe: callers now pass the 15m RSI (tf_15m.rsi_14) — the validated
    # FASTER-LEAD for a 1h market — uniformly for ALL assets. (2026-06-08: BTC was
    # fixed from its canonical 4h RSI, which is flat/useless on short windows, to the
    # 15m; alts already used 15m as canonical so they are unchanged.) ``window ==
    # "1h"`` gates the MARKET horizon, not the RSI TF. When the flip is extended to
    # the 15m/5m windows, callers should pass the corresponding faster-lead RSI
    # (15m->5m) — per-window rsi_5m/15m/1h is logged on candidates to validate that.
    #
    # Asymmetric (SHORT->LONG only); the down-side mirror is intentionally NOT
    # included pending its own validation. Opt-in per strategy via
    # `rsi_momentum_flip_1h`. Guard `flipped is None` so a genuine fresh bearish
    # cross is never overridden by RSI.
    if (
        momentum_flip_enabled
        and flipped is None
        and allowed_side == "SHORT"
        and str(window) == "1h"
        and rsi_14 is not None
        and float(rsi_14) >= momentum_flip_min_rsi
        and "BEAR" in str(primary_htf_bias).upper()
    ):
        est_prob_up = max(est_prob_up, 0.55)
        action = "BUY_YES"
        direction = "UP"
        allowed_side = "LONG"
        side_source = f"{side_source or ''}+rsi_momentum_flip_1h"
        reason_parts.append(f"rsi_momentum_flip_1h(rsi={float(rsi_14):.0f})->BUY_YES")
        if logger is not None:
            logger.info(
                "  %s RSI MOMENTUM FLIP -> BUY_YES (rsi=%.0f, 1h, vs lagging bias %s)",
                strategy_name, float(rsi_14), primary_htf_bias,
            )

    # Continuous 5m-momentum flip (2026-06-09, DEFAULT-OFF via ``macd_flip_enabled``).
    # The fresh-cross flip above only fires on a DISCRETE cross event (measured
    # 0/77k live) and the RSI flip is 1h-market-only, so neither protects 5m/15m
    # SHORTs — which bleed when the lagging BEARISH bias shorts a tape whose 5m
    # momentum has already turned up (observed live: 38/42 losing taken shorts were
    # `aligned`/BEARISH with mfe~0, i.e. wrong from entry). Ghost validation
    # (rejected_candidates_settled, macd_hist_5m logged from 06-08): the SIGN of
    # macd_hist_5m splits BEARISH-bias SHORT win rate ~in half —
    #   5m  market: hist<=0 -> 56.4% (n=951)  vs  hist>0 -> 43.3% (n=601)
    #   15m market: hist<=0 -> 58.4% (n=3956) vs  hist>0 -> 49.7% (n=3788)
    # gap holds in both time-halves (15m high-n, stable). So a SHORT into rising 5m
    # momentum is the losing half; flip it to LONG (5m is +EV at P(up)=0.567; 15m is
    # break-even — strictly better than the -EV short and keeps frequency). Uses the
    # CONTINUOUS hist sign (so it actually fires, unlike the discrete cross).
    # Asymmetric (SHORT->LONG only); 1h excluded (covered by the RSI flip). Opt-in
    # per strategy via `macd_momentum_flip_5m15m`. Guard `flipped is None` so a
    # genuine fresh bearish cross is never overridden. NOTE: ~8h validation window so
    # far (macd_hist_5m newly logged) — 15m solid, 5m forward-test.
    if (
        macd_flip_enabled
        and flipped is None
        and allowed_side == "SHORT"
        and str(window) in ("5m", "15m")
        and macd_hist_5m is not None
        and float(macd_hist_5m) > 0.0
        and "BEAR" in str(primary_htf_bias).upper()
    ):
        est_prob_up = max(est_prob_up, 0.55)
        action = "BUY_YES"
        direction = "UP"
        allowed_side = "LONG"
        side_source = f"{side_source or ''}+macd5m_momentum_flip"
        reason_parts.append(
            f"macd5m_momentum_flip(hist={float(macd_hist_5m):.4f},{window})->BUY_YES"
        )
        if logger is not None:
            logger.info(
                "  %s MACD5m MOMENTUM FLIP -> BUY_YES (hist=%.4f, %s, vs lagging bias %s)",
                strategy_name, float(macd_hist_5m), window, primary_htf_bias,
            )

    # Symmetric LONG->SHORT twin (2026-06-09). Mirror of the block above: a lagging
    # BULLISH bias keeps the bot LONG into a tape whose 5m momentum has already
    # turned DOWN (live: "longs into a falling tape" is the dominant
    # window-delta-disagree block, and exactly the losing lanes from the lane audit
    # — doge/eth/bnb 15m LONG). Instead of just BLOCKING that -EV long, flip it to a
    # SHORT that trades the actual down-move — recovers frequency in the CORRECT
    # direction. ``macd_hist_5m < 0`` is mutually exclusive with the SHORT->LONG
    # trigger, so no double-flip. Opt-in via ``macd_momentum_flip_long_to_short``;
    # same 5m/15m scope + flipped-None guard. FORWARD-TEST (not yet ghost-validated
    # like the SHORT->LONG side — watch macd5m_momentum_flip->BUY_NO outcomes).
    if (
        macd_flip_long_to_short_enabled
        and flipped is None
        and allowed_side == "LONG"
        and str(window) in ("5m", "15m")
        and macd_hist_5m is not None
        and float(macd_hist_5m) < 0.0
        and "BULL" in str(primary_htf_bias).upper()
    ):
        est_prob_up = min(est_prob_up, 0.45)
        action = "BUY_NO"
        direction = "DOWN"
        allowed_side = "SHORT"
        side_source = f"{side_source or ''}+macd5m_momentum_flip_short"
        reason_parts.append(
            f"macd5m_momentum_flip(hist={float(macd_hist_5m):.4f},{window})->BUY_NO"
        )
        if logger is not None:
            logger.info(
                "  %s MACD5m MOMENTUM FLIP -> BUY_NO (hist=%.4f, %s, vs lagging bias %s)",
                strategy_name, float(macd_hist_5m), window, primary_htf_bias,
            )

    return est_prob_up, action, allowed_side, direction, side_source


def score_updown_candidate(
    *,
    edge: float,
    min_edge: float,
    quant_confidence: float,
    micro_momentum: float,
    timeframe_alignment: float,
    oracle: OracleValidation,
    minutes_to_resolution: float,
    yes_price: float,
    floor: float,
    action: Optional[str] = None,
    btc_1h_regime: Optional[str] = None,
    regime_action_gate_enabled: bool = False,
    regime_action_min_convergence: float = 0.55,
) -> CompositeScore:
    """Return an auditable 0-1 candidate quality score before AI/sizing."""
    min_edge_f = max(0.0001, float(min_edge))
    edge_quality = _clamp(float(edge) / min_edge_f)
    confidence_quality = _clamp((float(quant_confidence) - 0.45) / 0.40)
    oracle_integrity = 1.0 if oracle.passed else 0.0
    # Prefer entries with some candle formed but not in the final scramble.
    mins = float(minutes_to_resolution)
    if mins <= 0:
        timing = 0.0
    elif mins < 1.0:
        timing = 0.25
    elif mins <= 14.5:
        timing = 1.0
    elif mins <= 16.0:
        timing = 0.70
    else:
        timing = 0.35
    # Centered books are usually cleaner for short-window up/down entries.
    market_price_quality = _clamp(1.0 - (abs(float(yes_price) - 0.50) / 0.12))

    components = {
        "edge_quality": edge_quality,
        "quant_confidence": confidence_quality,
        "micro_momentum": _clamp(micro_momentum),
        "timeframe_alignment": _clamp(timeframe_alignment),
        "oracle_integrity": oracle_integrity,
        "entry_timing": timing,
        "market_price_quality": market_price_quality,
    }
    score = sum(components[name] * weight for name, weight in WEIGHTS.items())
    chase_regime = False
    regime = str(btc_1h_regime or "").upper()
    side = str(action or "").upper()
    if regime in {"BULL", "RANGE", "BEAR"} and side in {"BUY_YES", "BUY_NO"}:
        # Treat same-side BTC 1H trend entries as lower-quality unless other gates agree.
        # This avoids "chasing" in extended regimes while allowing strong consensus through.
        if (regime == "BULL" and side == "BUY_YES") or (
            regime == "BEAR" and side == "BUY_NO"
        ):
            regime_quality = 0.25
            chase_regime = True
        elif regime == "RANGE":
            regime_quality = 0.55
        else:
            regime_quality = 0.85
        components["btc_1h_regime_alignment"] = regime_quality
        score = (0.90 * score) + (0.10 * regime_quality)
    score = _clamp(score)
    convergence_score = convergence_score_from_components(components)
    floor_f = _clamp(floor)
    if (
        regime_action_gate_enabled
        and chase_regime
        and convergence_score < float(regime_action_min_convergence)
    ):
        return CompositeScore(
            score=score,
            passed=False,
            floor=floor_f,
            components=components,
            convergence_score=convergence_score,
            reason="btc_regime_action_block",
        )
    return CompositeScore(
        score=score,
        passed=score >= floor_f,
        floor=floor_f,
        components=components,
        convergence_score=convergence_score,
        reason="composite_ok" if score >= floor_f else "composite_score_below_floor",
    )
