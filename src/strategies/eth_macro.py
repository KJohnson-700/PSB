"""
ETH Macro Strategy — BTC-follow execution for ETH up/down markets.

Design:
- BTC 4H decides regime.
- BTC 1H confirms continuation quality.
- BTC short-window momentum triggers the follow setup.
- ETH 5m/15m momentum only confirms follow-through.
"""
import logging
import time
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.analysis.ai_agent import AIAgent
from src.analysis.btc_price_service import CandleMomentum, MACDResult, TechnicalAnalysis
from src.analysis.math_utils import PositionSizer
from src.analysis.sol_btc_service import SOLBTCService
from src.execution.exposure_manager import ExposureManager, ExposureTier
from src.market.scanner import Market
from src.strategies.strategy_config import resolve_enabled_flag
from src.analysis.btc_1h_regime import regime_price
from src.strategies.sol_macro import SolMacroSignal, SolMacroStrategy
from src.strategies.strategy_ai_context import (
    ai_recommendation_supports_action,
    format_market_metadata,
)

logger = logging.getLogger(__name__)

ETH_PATTERNS = [
    re.compile(r"\bethereum\b", re.IGNORECASE),
    re.compile(r"\beth\b", re.IGNORECASE),
    re.compile(r"\bether\b", re.IGNORECASE),
]
ETH_UPDOWN_PATTERN = re.compile(
    r"(?:ethereum|eth|ether)\s+up\s+or\s+down", re.IGNORECASE
)
ETH_UPDOWN_SLUG_PREFIXES = ("eth-updown-", "eth-up-or-down-", "ethereum-up-or-down-", "ether-up-or-down-")
NON_ETH_ASSET_TERMS = ("bitcoin", "btc", "solana", "xrp", "ripple", "hyperliquid", "hype")


class ETHMacroStrategy(SolMacroStrategy):
    """ETH strategy using BTC-follow regime logic instead of SOL lag logic."""

    def _build_alt_service(self) -> SOLBTCService:
        return SOLBTCService(
            alt_symbol="ETHUSDT",
            dynamic_beta_min=self.dynamic_beta_min,
            dynamic_beta_max=self.dynamic_beta_max,
            dynamic_beta_extreme_max=self.dynamic_beta_extreme_max,
            btc_spike_floor_pct_5m=self.btc_spike_floor_pct_5m,
            btc_spike_floor_pct_15m=self.btc_spike_floor_pct_15m,
            lag_signal_min_pct=self.lag_signal_min_pct,
        )

    def __init__(
        self,
        config: Dict[str, Any],
        ai_agent: AIAgent,
        position_sizer: PositionSizer,
        kelly_sizer=None,
        exposure_manager: ExposureManager = None,
    ):
        super().__init__(config, ai_agent, position_sizer, kelly_sizer, exposure_manager)
        self.config = config.get("strategies", {}).get("eth_macro", {})
        self.enabled = resolve_enabled_flag(
            "eth_macro",
            self.config,
            logger=logger,
        )
        self._apply_strategy_config(rebuild_service=True)
        self._signal_strategy_name = "eth_macro"

        self.btc_follow_1h_hist_min = float(self.config.get("btc_follow_1h_hist_min", 8.0))
        self.btc_follow_15m_hist_min = float(self.config.get("btc_follow_15m_hist_min", 0.03))
        self.btc_follow_5m_requires_impulse = bool(
            self.config.get("btc_follow_5m_requires_impulse", True)
        )
        self.eth_follow_5m_min_adj = float(self.config.get("eth_follow_5m_min_adj", 0.04))
        self.eth_follow_15m_hist_min = float(self.config.get("eth_follow_15m_hist_min", 0.03))
        self.eth_follow_15m_min_adj = float(self.config.get("eth_follow_15m_min_adj", 0.04))
        self.ai_hold_veto_ttl_sec = self.config.get("ai_hold_veto_ttl_sec", 300)
        self.min_edge_5m_ai_override = self.config.get("min_edge_5m_ai_override", 0.10)
        self.btc_follow_1h_required = bool(self.config.get("btc_follow_1h_required", True))
        self.btc_follow_1h_allow_rising_recovery = bool(
            self.config.get("btc_follow_1h_allow_rising_recovery", True)
        )
        self.btc_follow_1h_recovery_hist_floor = float(
            self.config.get("btc_follow_1h_recovery_hist_floor", 80.0)
        )
        self.btc_follow_1h_allow_floor_without_rising = bool(
            self.config.get("btc_follow_1h_allow_floor_without_rising", True)
        )
        self.direction_source = str(self.config.get("direction_source", "hybrid")).strip().lower()
        if self.direction_source not in {"btc", "hybrid", "signal_first"}:
            self.direction_source = "btc"
        self.signal_15m_long_threshold = float(self.config.get("signal_15m_long_threshold", 0.55))
        self.signal_15m_short_threshold = float(self.config.get("signal_15m_short_threshold", 0.45))
        self.signal_4h_long_threshold = float(self.config.get("signal_4h_long_threshold", 0.70))
        self.signal_4h_short_threshold = float(self.config.get("signal_4h_short_threshold", 0.30))
        # When BTC HTF is bullish but 5m/15m lack SPIKE/DRIFT candle labels, entries starve.
        # If BTC 1H continuation already passes, allow bypassing strict STF impulse requirements.
        _stf_bypass = self.config.get("btc_follow_stf_bypass_if_1h_ok")
        if _stf_bypass is None:
            _stf_bypass = self.config.get("btc_follow_5m_bypass_if_1h_ok", False)
        self.btc_follow_stf_bypass_if_1h_ok = bool(_stf_bypass)
        # 15m path otherwise requires MACD + candle momentum aligned (SPIKE_UP/DRIFT_UP for LONG).
        # When True, LONG also passes if 15m MACD histogram is rising above min (grind / no sharp impulse).
        self.btc_follow_15m_allow_macd_grind = bool(
            self.config.get("btc_follow_15m_allow_macd_grind", False)
        )
        # When BTC 4H HTF already matches trade side, bypass strict 5m/15m BTC impulse gates (ETH leg still gates).
        self.btc_follow_stf_bypass_when_macro_agrees = bool(
            self.config.get("btc_follow_stf_bypass_when_macro_agrees", False)
        )
        # When False, 5m never uses bypass_5m_impulse_btc_1h_ok; 15m still uses btc_follow_stf_bypass_if_1h_ok where coded.
        self.btc_follow_5m_allow_1h_impulse_bypass = bool(
            self.config.get("btc_follow_5m_allow_1h_impulse_bypass", True)
        )

    def _eth_stf_bypass_when_macro_agrees(
        self, btc_htf_bias: str, market_allowed_side: str
    ) -> bool:
        """True when BULLISH+LONG or BEARISH+SHORT — STF BTC follow can still fail in grindy tape."""
        if not self.btc_follow_stf_bypass_when_macro_agrees:
            return False
        if btc_htf_bias == "BULLISH" and market_allowed_side == "LONG":
            return True
        if btc_htf_bias == "BEARISH" and market_allowed_side == "SHORT":
            return True
        return False

    def _record_eth_abort(self, reason: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "enabled": self.enabled,
            "signals": 0,
            "abort_reason": reason,
            "markets_considered": 0,
            "top_skip_reasons": {},
            "gate_distributions": {},
        }
        if extra:
            payload.update(extra)
        self.last_scan_stats = payload

    def _is_solana_market(self, market: Market) -> bool:
        text = (
            f"{market.question} {market.description} "
            f"{market.group_item_title} {market.slug}"
        ).lower()
        has_eth = any(p.search(text) for p in ETH_PATTERNS) or (market.slug or "").lower().startswith(ETH_UPDOWN_SLUG_PREFIXES)
        if not has_eth:
            return False
        if any(term in text for term in NON_ETH_ASSET_TERMS):
            primary = f"{market.question} {market.group_item_title} {market.slug}".lower()
            if not any(p.search(primary) for p in ETH_PATTERNS):
                return False
        return True

    def _is_updown_market(self, market: Market) -> bool:
        slug = (market.slug or "").lower()
        if slug.startswith(ETH_UPDOWN_SLUG_PREFIXES):
            return True
        text = f"{market.question} {market.group_item_title}"
        return bool(ETH_UPDOWN_PATTERN.search(text))

    def _btc_follow_1h_ok(self, btc_ta: TechnicalAnalysis, allowed_side: str) -> bool:
        macd_1h = btc_ta.macd_1h
        min_hist = self.btc_follow_1h_hist_min
        rec_mag = abs(float(self.btc_follow_1h_recovery_hist_floor))
        if allowed_side == "LONG":
            base_ok = (
                macd_1h.histogram > min_hist
                or (macd_1h.histogram > 0 and macd_1h.histogram_rising)
                or macd_1h.crossover == "BULLISH_CROSS"
            )
            if base_ok:
                return True
            if self.btc_follow_1h_allow_rising_recovery:
                # Negative histogram repairing upward (still below strict bullish thresholds).
                if macd_1h.histogram_rising and macd_1h.histogram > -rec_mag:
                    return True
            if self.btc_follow_1h_allow_floor_without_rising:
                # Shallow bearish 1H histogram while HTF still bullish (tape often lagging MACD slope).
                if macd_1h.histogram > -rec_mag:
                    return True
            return False
        base_ok = (
            macd_1h.histogram < -min_hist
            or (macd_1h.histogram < 0 and not macd_1h.histogram_rising)
            or macd_1h.crossover == "BEARISH_CROSS"
        )
        if base_ok:
            return True
        if self.btc_follow_1h_allow_rising_recovery:
            # Positive histogram rolling over / repairing downward for SHORT continuation.
            if (not macd_1h.histogram_rising) and macd_1h.histogram < rec_mag:
                return True
        if self.btc_follow_1h_allow_floor_without_rising:
            # Shallow bullish histogram failing stronger bear rules — still below ceiling.
            if macd_1h.histogram >= 0 and macd_1h.histogram < rec_mag:
                return True
        return False

    def _btc_follow_15m_impulse_ok(self, btc_ta: TechnicalAnalysis, allowed_side: str) -> bool:
        macd_15m = btc_ta.macd_15m
        min_hist = self.btc_follow_15m_hist_min
        direction = btc_ta.candle_momentum.m15_direction
        if allowed_side == "LONG":
            strict = (
                macd_15m.crossover == "BULLISH_CROSS"
                or (
                    macd_15m.histogram > min_hist
                    and macd_15m.histogram_rising
                    and direction in ("SPIKE_UP", "DRIFT_UP")
                )
            )
            if strict:
                return True
            if self.btc_follow_15m_allow_macd_grind:
                return macd_15m.histogram > min_hist and macd_15m.histogram_rising
            return False
        strict_short = (
            macd_15m.crossover == "BEARISH_CROSS"
            or (
                macd_15m.histogram < -min_hist
                and not macd_15m.histogram_rising
                and direction in ("SPIKE_DOWN", "DRIFT_DOWN")
            )
        )
        if strict_short:
            return True
        if self.btc_follow_15m_allow_macd_grind:
            return macd_15m.histogram < -min_hist and not macd_15m.histogram_rising
        return False

    def _btc_follow_5m_impulse_score(self, momentum: CandleMomentum, allowed_side: str) -> tuple[float, List[str]]:
        direction = momentum.m5_direction
        reasons: List[str] = []
        score = 0.0
        if allowed_side == "LONG":
            if direction == "SPIKE_UP":
                score = 0.06
                reasons.append(f"BTC5m SPIKE_UP ({momentum.m5_move_pct:+.3f}%)")
            elif direction == "DRIFT_UP":
                score = 0.04
                reasons.append(f"BTC5m DRIFT_UP ({momentum.m5_move_pct:+.3f}%)")
            elif direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
                score = -0.05
                reasons.append(f"BTC5m against ({direction})")
        else:
            if direction == "SPIKE_DOWN":
                score = 0.06
                reasons.append(f"BTC5m SPIKE_DOWN ({momentum.m5_move_pct:+.3f}%)")
            elif direction == "DRIFT_DOWN":
                score = 0.04
                reasons.append(f"BTC5m DRIFT_DOWN ({momentum.m5_move_pct:+.3f}%)")
            elif direction in ("SPIKE_UP", "DRIFT_UP"):
                score = -0.05
                reasons.append(f"BTC5m against ({direction})")
        if momentum.m5_in_prediction_window and score > 0:
            score += 0.02
            reasons.append("BTC5m predict window")
        return score, reasons

    @staticmethod
    def _eth_5m_macd_score(macd_5m: MACDResult, allowed_side: str) -> tuple[float, List[str]]:
        reasons: List[str] = []
        score = 0.0
        if allowed_side == "LONG":
            if macd_5m.crossover == "BULLISH_CROSS":
                score = 0.06
                reasons.append("ETH5m bull cross")
            elif macd_5m.histogram > 0 and macd_5m.histogram_rising:
                score = 0.04
                reasons.append("ETH5m green+rising")
            elif macd_5m.crossover == "BEARISH_CROSS" or macd_5m.histogram < 0:
                score = -0.05
                reasons.append("ETH5m against")
        else:
            if macd_5m.crossover == "BEARISH_CROSS":
                score = 0.06
                reasons.append("ETH5m bear cross")
            elif macd_5m.histogram < 0 and not macd_5m.histogram_rising:
                score = 0.04
                reasons.append("ETH5m red+falling")
            elif macd_5m.crossover == "BULLISH_CROSS" or macd_5m.histogram > 0:
                score = -0.05
                reasons.append("ETH5m against")
        return score, reasons

    def _eth_15m_follow_score(self, macd_15m: MACDResult, allowed_side: str) -> tuple[float, List[str]]:
        reasons: List[str] = []
        score = 0.0
        min_hist = self.eth_follow_15m_hist_min
        if allowed_side == "LONG":
            if macd_15m.crossover == "BULLISH_CROSS":
                score = 0.06
                reasons.append("ETH15m bull cross")
            elif macd_15m.histogram >= min_hist and macd_15m.histogram_rising:
                score = 0.05
                reasons.append(f"ETH15m green+rising>{min_hist:.2f}")
            elif macd_15m.crossover == "BEARISH_CROSS" or macd_15m.histogram < 0:
                score = -0.05
                reasons.append("ETH15m against")
        else:
            if macd_15m.crossover == "BEARISH_CROSS":
                score = 0.06
                reasons.append("ETH15m bear cross")
            elif macd_15m.histogram <= -min_hist and not macd_15m.histogram_rising:
                score = 0.05
                reasons.append(f"ETH15m red+falling>{min_hist:.2f}")
            elif macd_15m.crossover == "BULLISH_CROSS" or macd_15m.histogram > 0:
                score = -0.05
                reasons.append("ETH15m against")
        return score, reasons

    @staticmethod
    def _btc_htf_proxy_signal(bias: str) -> float:
        if bias == "BULLISH":
            return 0.75
        if bias == "BEARISH":
            return 0.25
        return 0.50

    def _resolve_market_side(self, base_side: str, btc_htf_bias: str, market_yes_price: float) -> tuple[str, str]:
        # Modes:
        # - btc: legacy behavior (BTC HTF decides side)
        # - hybrid: strong market-side override only when 15m + 4h proxy both agree
        # - signal_first: test mode where 15m market signal can set side directly
        if self.direction_source == "btc":
            return base_side, "btc_bias"

        signal_15m = float(market_yes_price)
        signal_4h = self._btc_htf_proxy_signal(btc_htf_bias)

        if self.direction_source == "signal_first":
            # Market 15m signal can set side, but require BTC HTF agreement (or NEUTRAL)
            # so we don't LONG into a BEARISH macro purely from market YES price.
            if signal_15m >= self.signal_15m_long_threshold and btc_htf_bias != "BEARISH":
                return "LONG", "signal_first_long"
            if signal_15m <= self.signal_15m_short_threshold and btc_htf_bias != "BULLISH":
                return "SHORT", "signal_first_short"
            return base_side, "signal_first_fallback"

        if (
            signal_15m >= self.signal_15m_long_threshold
            and signal_4h >= self.signal_4h_long_threshold
        ):
            return "LONG", "hybrid_strong_long"
        if (
            signal_15m <= self.signal_15m_short_threshold
            and signal_4h <= self.signal_4h_short_threshold
        ):
            return "SHORT", "hybrid_strong_short"
        return base_side, "hybrid_fallback"

    async def scan_and_analyze(self, markets: List[Market], bankroll: float) -> List[SolMacroSignal]:
        if not self.enabled:
            self._record_eth_abort("strategy_disabled")
            return []

        eth_markets = [m for m in markets if self._is_solana_market(m) and self._is_updown_market(m)]
        if not eth_markets:
            logger.info("ETH Macro strategy: 0 ETH updown markets found")
            self._record_eth_abort("no_eth_markets")
            return []

        eth_ta = self.sol_service.get_full_analysis()
        btc_ta = self.btc_service.get_full_analysis()
        if not eth_ta or not btc_ta:
            logger.warning("ETH Macro strategy: BTC or ETH analysis unavailable")
            self._record_eth_abort(
                "analysis_unavailable",
                {"markets_considered": len(eth_markets)},
            )
            return []

        conditions = self.conditions_from_ta(eth_ta)
        exp_tier, exp_multiplier, _exp_max_size, exp_reason = self.exposure_manager.get_exposure(conditions)
        if exp_tier == ExposureTier.PAUSED:
            logger.info(f"ETH Macro strategy: PAUSED — {exp_reason}")
            self._record_eth_abort(
                "exposure_paused",
                {"markets_considered": len(eth_markets), "detail": exp_reason},
            )
            return []

        corr = eth_ta.correlation
        btc_htf_bias = self._get_btc_htf_bias(btc_ta)
        _ovr = (self.config.get("btc_htf_bias_dry_run_override") or "").strip().upper()
        if _ovr in {"BULLISH", "BEARISH", "NEUTRAL"}:
            if self.full_config.get("trading", {}).get("dry_run", True):
                logger.warning(
                    "ETH Macro: btc_htf_bias_dry_run_override=%s (paper / dry_run only)",
                    _ovr,
                )
                btc_htf_bias = _ovr
            else:
                logger.warning(
                    "ETH Macro: ignoring btc_htf_bias_dry_run_override=%s — trading.dry_run is false",
                    _ovr,
                )
        btc_1h_regime = "BULL"
        if self._btc_1h_regime_gates.get("enabled", False):
            btc_1h_regime = self._classify_btc_1h_regime(btc_ta)
            logger.info(
                "ETH Macro BTC 1H regime: %s | min_edge×%.2f size×%.2f | spot=%.0f SMA20=%.0f",
                btc_1h_regime,
                self._regime_min_edge_mult(btc_1h_regime),
                self._regime_size_mult(btc_1h_regime),
                regime_price(btc_ta),
                float(getattr(btc_ta, "sma_1h_20", 0.0) or 0.0),
            )

        # Align with SolMacroStrategy: BTC 4H NEUTRAL must not idle ETH while up/down markets refresh.
        skip_btc_follow_1h = False
        if btc_htf_bias == "NEUTRAL":
            # SOLBTCCorrelation.sol_trend is multi_tf.h1_trend on the alt leg — for ETH this is ETH 1H bias.
            alt_1h_trend = corr.sol_trend
            if corr.btc_spike_detected:
                allowed_side = "LONG" if corr.btc_move_5m_pct > 0 else "SHORT"
                skip_btc_follow_1h = True
                logger.info(
                    "ETH Macro: BTC HTF NEUTRAL, BTC spike (%+.2f%%) — catch-up side %s",
                    corr.btc_move_5m_pct,
                    allowed_side,
                )
            elif corr.lag_opportunity:
                _min_lag_mag = float(self.config.get("min_lag_magnitude_pct", 0.30))
                _lag_mag = abs(corr.opportunity_magnitude)
                if _lag_mag >= _min_lag_mag:
                    allowed_side = corr.opportunity_direction
                    skip_btc_follow_1h = True
                    logger.info(
                        "ETH Macro: BTC HTF NEUTRAL, lag opp mag=%.2f%% — side %s",
                        _lag_mag,
                        allowed_side,
                    )
                else:
                    allowed_side = (
                        "LONG"
                        if alt_1h_trend == "BULLISH"
                        else "SHORT"
                        if alt_1h_trend == "BEARISH"
                        else None
                    )
                    if allowed_side is None:
                        logger.info(
                            "ETH Macro: BTC HTF NEUTRAL, weak lag, no ETH 1H bias — sitting out"
                        )
                        self._record_eth_abort(
                            "neutral_weak_lag_no_alt_bias",
                            {"markets_considered": len(eth_markets)},
                        )
                        return []
                    skip_btc_follow_1h = True
                    logger.info(
                        "ETH Macro: BTC HTF NEUTRAL, weak lag — ETH 1H bias side %s",
                        allowed_side,
                    )
            else:
                if self.neutral_macro_require_spike_or_lag:
                    logger.info(
                        "ETH Macro: BTC HTF NEUTRAL, no BTC spike/lag "
                        "(neutral_macro_require_spike_or_lag) — sitting out"
                    )
                    self._record_eth_abort(
                        "neutral_macro_no_catalyst",
                        {"markets_considered": len(eth_markets)},
                    )
                    return []
                allowed_side = (
                    "LONG"
                    if alt_1h_trend == "BULLISH"
                    else "SHORT"
                    if alt_1h_trend == "BEARISH"
                    else None
                )
                if allowed_side is None:
                    logger.info(
                        "ETH Macro: BTC HTF NEUTRAL, no catalyst, no ETH 1H bias — sitting out"
                    )
                    self._record_eth_abort(
                        "neutral_no_alt_bias",
                        {"markets_considered": len(eth_markets)},
                    )
                    return []
                skip_btc_follow_1h = True
                logger.info(
                    "ETH Macro: BTC HTF NEUTRAL — ETH 1H bias side %s",
                    allowed_side,
                )
        else:
            allowed_side = "LONG" if btc_htf_bias == "BULLISH" else "SHORT"

        if (
            self.btc_follow_1h_required
            and not skip_btc_follow_1h
            and not self._btc_follow_1h_ok(btc_ta, allowed_side)
        ):
            logger.info(
                "ETH Macro strategy: BTC 1H continuation not strong enough "
                f"(bias={btc_htf_bias}, hist={btc_ta.macd_1h.histogram:+.2f})"
            )
            self._record_eth_abort(
                "btc_follow_1h_blocked",
                {
                    "markets_considered": len(eth_markets),
                    "btc_htf_bias": btc_htf_bias,
                    "btc_1h_histogram": btc_ta.macd_1h.histogram,
                },
            )
            return []

        eth = eth_ta.sol
        eth_price = eth.current_price
        btc_mom = btc_ta.candle_momentum
        mtt = eth_ta.multi_tf

        logger.info(
            f"ETH ${eth_price:,.2f} | BTC_HTF={btc_htf_bias} | BTC1H hist={btc_ta.macd_1h.histogram:+.2f} "
            f"BTC15m={btc_ta.macd_15m.histogram:+.3f} BTC5m={btc_mom.m5_direction}({btc_mom.m5_move_pct:+.3f}%) "
            f"| ETH15m={eth.macd_15m.histogram:+.3f} {eth.macd_15m.crossover} "
            f"| ETH5m={eth.macd_5m.histogram:+.3f} {eth.macd_5m.crossover} | RSI={eth.rsi_14:.0f}"
        )

        signals: List[SolMacroSignal] = []
        ai_calls = 0
        skip_reasons: Dict[str, int] = {}
        gate_samples: Dict[str, list] = {}
        side_source_counts: Dict[str, int] = {}

        def _bump_skip(reason: str) -> None:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        def _sample(metric: str, value) -> None:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return
            if not (v == v):  # NaN check
                return
            gate_samples.setdefault(metric, []).append(v)

        def _summarize(values: list) -> dict:
            if not values:
                return {}
            vs = sorted(values)
            n = len(vs)
            def pct(p):
                idx = max(0, min(n - 1, int(round((n - 1) * p))))
                return round(vs[idx], 4)
            return {"n": n, "min": round(vs[0], 4), "p25": pct(0.25), "p50": pct(0.50), "p75": pct(0.75), "max": round(vs[-1], 4)}

        _latency_sec = float(self.config.get("entry_window_latency_buffer_sec", 0.0) or 0.0)

        for market in eth_markets:
            if market.liquidity > 0 and market.liquidity < self.min_liquidity:
                _bump_skip("liquidity")
                continue

            is_5m = self._is_5m_market(market)
            yes_price = market.yes_price

            # UTC dead-zone — same config keys as sol_macro / bitcoin updown.
            _dead_zone_enabled = self.config.get("dead_zone_enabled", True)
            _blocked_hours = self.config.get("blocked_utc_hours_updown", [0, 18, 22])
            _now_utc_hour = datetime.now(timezone.utc).hour
            dead_zone_would_block = _now_utc_hour in _blocked_hours
            if _dead_zone_enabled:
                if dead_zone_would_block:
                    _bump_skip("blocked_utc_hour")
                    logger.info(
                        f"  ETH skip updown at UTC hour {_now_utc_hour}:xx — "
                        f"blocked dead zone (config: {_blocked_hours})"
                    )
                    continue
            elif dead_zone_would_block:
                logger.info(
                    f"  ETH dead_zone DISABLED — allowing UTC hour {_now_utc_hour:02d} "
                    f"(would-be blocked_hours={_blocked_hours})"
                )

            if not market.end_date:
                _bump_skip("no_end_date")
                continue

            _end_utc = (
                market.end_date.replace(tzinfo=timezone.utc)
                if market.end_date.tzinfo is None else market.end_date
            )
            _mins_left = (_end_utc - datetime.now(timezone.utc)).total_seconds() / 60.0
            _eval_left = max(0.0, _mins_left - _latency_sec / 60.0)
            if is_5m:
                _win_min, _win_max = self._resolve_entry_window_bounds(
                    is_5m=True, default_min=2.75, default_max=3.75
                )
            else:
                _win_min, _win_max = self._resolve_entry_window_bounds(
                    is_5m=False, default_min=13.0, default_max=14.33
                )
            _sample("mins_left", _mins_left)
            if _eval_left < _win_min or _eval_left > _win_max:
                _bump_skip("outside_entry_window")
                continue
            _ai_window_open = self._within_ai_decision_window(
                mins_left=_eval_left,
                is_5m=is_5m,
            )

            _btc_corr = corr.correlation_1h
            _low_corr_btc_bypass = float(self.config.get("btc_min_move_low_corr_threshold", 0.30))
            _btc_price = corr.btc_price or 0.0
            _btc_move_5m_dollars = abs(corr.btc_move_5m_pct / 100.0 * _btc_price)
            _btc_move_15m_dollars = abs(corr.btc_move_15m_pct / 100.0 * _btc_price)
            if is_5m:
                _btc_min_move = float(self.config.get("btc_min_move_dollars_5m", 37.0))
                _btc_move = _btc_move_5m_dollars
            else:
                _btc_min_move = float(self.config.get("btc_min_move_dollars_15m", 70.0))
                _btc_move = max(_btc_move_5m_dollars, _btc_move_15m_dollars)
            if _btc_price > 0 and _btc_move < _btc_min_move:
                if _btc_corr < _low_corr_btc_bypass:
                    logger.debug(
                        f"  ETH btc_min_move bypassed (corr={_btc_corr:.2f} < {_low_corr_btc_bypass}) "
                        f"BTC moved ${_btc_move:.0f} < min ${_btc_min_move:.0f}"
                    )
                else:
                    _bump_skip("btc_min_move_dollars")
                    logger.debug(
                        f"  ETH skip '{market.question[:40]}' — "
                        f"BTC moved ${_btc_move:.0f} < min ${_btc_min_move:.0f}"
                    )
                    continue

            _sample("entry_price", yes_price)
            if yes_price < 0.20 or yes_price > 0.80:
                _bump_skip("price_too_far")
                continue

            market_allowed_side, side_source = self._resolve_market_side(
                allowed_side, btc_htf_bias, yes_price
            )
            side_source_counts[side_source] = side_source_counts.get(side_source, 0) + 1

            action = "BUY_YES" if market_allowed_side == "LONG" else "BUY_NO"
            direction = "UP" if market_allowed_side == "LONG" else "DOWN"

            if self.enforce_alt_1h_alignment:
                if action == "BUY_NO" and mtt.h1_trend == "BULLISH":
                    _bump_skip("eth_1h_bullish")
                    continue
                if action == "BUY_YES" and mtt.h1_trend == "BEARISH":
                    _bump_skip("eth_1h_bearish")
                    continue
            if self._rsi_blocks_entry(action, eth.rsi_14):
                _bump_skip("rsi_block")
                continue
            if self._oracle_basis_blocks_entry(eth.oracle_basis_bps):
                _bump_skip("oracle_basis_block")
                continue

            est_prob_up = 0.50
            reason_parts = [
                f"BTC_HTF={btc_htf_bias}",
                f"side={market_allowed_side}",
                f"side_src={side_source}",
            ]
            confidence = 0.50
            ai_used = False

            if is_5m:
                btc_impulse, btc_reasons = self._btc_follow_5m_impulse_score(
                    btc_mom, market_allowed_side
                )
                _impulse_gate_ok = btc_impulse > 0
                if (
                    self.btc_follow_stf_bypass_if_1h_ok
                    and self.btc_follow_5m_allow_1h_impulse_bypass
                    and not _impulse_gate_ok
                    and self._btc_follow_1h_ok(btc_ta, market_allowed_side)
                ):
                    _impulse_gate_ok = True
                    btc_reasons.append("bypass_5m_impulse_btc_1h_ok")
                if (
                    self._eth_stf_bypass_when_macro_agrees(btc_htf_bias, market_allowed_side)
                    and not _impulse_gate_ok
                ):
                    _impulse_gate_ok = True
                    btc_reasons.append("bypass_5m_stf_macro_agrees")
                if self.btc_follow_5m_requires_impulse and not _impulse_gate_ok:
                    _bump_skip("btc_5m_no_impulse")
                    continue
                eth_5m_adj, eth_reasons = self._eth_5m_macd_score(
                    eth.macd_5m, market_allowed_side
                )
                if eth_5m_adj < self.eth_follow_5m_min_adj:
                    _bump_skip("eth_5m_weak_confirm")
                    continue
                est_prob_up = self._apply_primary_htf_bias(est_prob_up, btc_htf_bias, 0.04)
                est_prob_up += btc_impulse if market_allowed_side == "LONG" else -btc_impulse
                est_prob_up += eth_5m_adj if market_allowed_side == "LONG" else -eth_5m_adj
                if eth.rsi_14 > 75:
                    est_prob_up -= 0.02
                elif eth.rsi_14 < 25:
                    est_prob_up += 0.02
                confidence = max(0.55, min(0.85, 0.50 + abs(btc_impulse) * 1.8 + abs(eth_5m_adj) * 2.0))
                reason_parts.extend(["UPDOWN_5m", *btc_reasons, *eth_reasons])
            else:
                if not self._btc_follow_15m_impulse_ok(btc_ta, market_allowed_side):
                    if self._eth_stf_bypass_when_macro_agrees(btc_htf_bias, market_allowed_side):
                        pass
                    elif (
                        self.btc_follow_stf_bypass_if_1h_ok
                        and self._btc_follow_1h_ok(btc_ta, market_allowed_side)
                    ):
                        pass
                    else:
                        _bump_skip("btc_15m_not_following")
                        continue
                eth_15m_adj, eth_reasons = self._eth_15m_follow_score(
                    eth.macd_15m, market_allowed_side
                )
                if eth_15m_adj < self.eth_follow_15m_min_adj:
                    _bump_skip("eth_15m_weak_confirm")
                    continue
                est_prob_up = self._apply_primary_htf_bias(est_prob_up, btc_htf_bias, 0.08)
                est_prob_up += eth_15m_adj if market_allowed_side == "LONG" else -eth_15m_adj
                if eth.rsi_14 > 75:
                    est_prob_up -= 0.03
                elif eth.rsi_14 < 25:
                    est_prob_up += 0.03
                confidence = max(0.55, min(0.85, 0.50 + abs(eth_15m_adj) * 2.2))
                reason_parts.extend(
                    [
                        "UPDOWN_15m",
                        *eth_reasons,
                        f"slug={market.slug or '?'}",
                        f"mins_left={_mins_left:.2f}",
                        f"end={_end_utc.isoformat()}",
                    ]
                )

            est_prob_up = max(0.10, min(0.90, est_prob_up))
            edge = est_prob_up - yes_price if action == "BUY_YES" else yes_price - est_prob_up
            if edge <= 0:
                _bump_skip("nonpositive_edge")
                continue

            effective_min_edge = self.min_edge_5m if is_5m else self.min_edge
            if self._btc_1h_regime_gates.get("enabled", False):
                effective_min_edge *= self._regime_min_edge_mult(btc_1h_regime)

            # Far from expiry → more time-stop risk; require extra min_edge.
            _rstart = float(
                self.config.get("updown_min_edge_mins_left_ramp_start_min", 0.0) or 0.0
            )
            _rmax = float(
                self.config.get("updown_min_edge_mins_left_ramp_max_add", 0.0) or 0.0
            )
            _rspan = float(
                self.config.get("updown_min_edge_mins_left_ramp_span_min", 18.0) or 18.0
            )
            if _rstart > 0 and _rmax > 0 and _rspan > 0 and _eval_left > _rstart:
                _edge_addon = _rmax * min(1.0, (_eval_left - _rstart) / _rspan)
                effective_min_edge += _edge_addon

            # Block updown when journaled macro_leg disagrees with side (catch-up thesis off).
            if self.block_counter_macro_leg_updown:
                _lm = self._signal_lag_magnitude(corr)
                if _lm is not None:
                    _long_floor = float(
                        self.config.get("updown_macro_leg_min_for_long", 0.0)
                    )
                    if market_allowed_side == "LONG" and _lm < _long_floor:
                        _bump_skip("macro_leg_blocks_long")
                        logger.info(
                            f"  ETH skip '{market.question[:40]}' — "
                            f"macro_leg={_lm:+.4f}% < long_floor={_long_floor:+.4f} (updown)"
                        )
                        continue

            _hold_ts = self._ai_hold_cache.get(market.id, 0)
            _hold_age = time.time() - _hold_ts
            _ai_override_bar = float(self.min_edge_5m_ai_override)
            if self._btc_1h_regime_gates.get("enabled", False):
                _ai_override_bar *= self._regime_min_edge_mult(btc_1h_regime)
            if _hold_age < self.ai_hold_veto_ttl_sec and edge < _ai_override_bar:
                _bump_skip("ai_hold_veto")
                continue

            if (
                edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and _ai_window_open
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and self.ai_agent.is_available()
                and ai_calls < self.max_ai_calls_per_scan
            ):
                _window = "5m" if is_5m else "15m"
                ai_context = (
                    f"{market.description}\n\n"
                    f"=== ETH BTC-FOLLOW CONTEXT ({_window}) ===\n"
                    f"ETH Price: ${eth_price:,.2f} | YES={yes_price:.3f} | action={action}\n"
                    f"BTC_HTF={btc_htf_bias} | side={market_allowed_side}({side_source}) | Quant edge={edge:.4f} "
                    f"(threshold={effective_min_edge:.4f})\n"
                    f"Minutes left={_mins_left:.1f}\n\n"
                    f"BTC 1H hist={btc_ta.macd_1h.histogram:+.2f} rising={btc_ta.macd_1h.histogram_rising}\n"
                    f"BTC 15m hist={btc_ta.macd_15m.histogram:+.3f} cross={btc_ta.macd_15m.crossover}\n"
                    f"BTC 5m={btc_mom.m5_direction} ({btc_mom.m5_move_pct:+.3f}%)\n"
                    f"ETH 15m hist={eth.macd_15m.histogram:+.3f} cross={eth.macd_15m.crossover}\n"
                    f"ETH 5m hist={eth.macd_5m.histogram:+.3f} cross={eth.macd_5m.crossover}\n"
                    f"ETH RSI={eth.rsi_14:.1f} | ETH 1H trend={mtt.h1_trend}\n"
                    f"ETH Chainlink={eth.chainlink_price if eth.chainlink_price is not None else 'n/a'} "
                    f"basis_bps={eth.oracle_basis_bps if eth.oracle_basis_bps is not None else 'n/a'}\n\n"
                    f"=== MARKET ===\n{format_market_metadata(market)}\n\n"
                    "Answer with BUY_YES, BUY_NO, or HOLD."
                )
                ai_analysis = await self.ai_agent.analyze_market(
                    market_question=market.question,
                    market_description=ai_context,
                    current_yes_price=yes_price,
                    market_id=market.id,
                    strategy_hint=self._signal_strategy_name,
                )
                ai_calls += 1
                ai_used = True
                if not ai_analysis:
                    _bump_skip("ai_none")
                    continue
                if ai_analysis.recommendation == "HOLD":
                    self._ai_hold_cache[market.id] = time.time()
                    _bump_skip("ai_hold")
                    continue
                if not ai_recommendation_supports_action(ai_analysis.recommendation, action):
                    _bump_skip("ai_veto")
                    continue
                if ai_analysis.confidence_score < self.ai_confidence_threshold:
                    _bump_skip("ai_low_confidence")
                    continue
                ai_prob_yes = float(ai_analysis.estimated_probability)
                ai_edge = ai_prob_yes - yes_price if action == "BUY_YES" else yes_price - ai_prob_yes
                if ai_edge <= 0:
                    _bump_skip("ai_nonpositive_edge")
                    continue
                edge = max(edge, ai_edge)
                confidence = max(confidence, ai_analysis.confidence_score)
                reason_parts.append("ai_updown_confirm")
            elif (
                edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and not _ai_window_open
            ):
                _bump_skip("ai_window_closed")
                continue

            _sample("est_prob_up", est_prob_up)
            _sample("edge", edge)
            if edge < effective_min_edge:
                _bump_skip("edge_below_min")
                continue

            # Centered-price gate: near 50/50 entries need a BTC catalyst and a higher edge bar.
            if self.center_price_band > 0:
                _is_centered = abs(yes_price - 0.50) <= self.center_price_band
                if _is_centered:
                    if (
                        self.center_price_requires_catalyst
                        and not corr.lag_opportunity
                        and not corr.btc_spike_detected
                    ):
                        _bump_skip("centered_price_no_catalyst")
                        continue
                    _center_min_edge = max(effective_min_edge, self.min_edge_when_centered)
                    if edge < _center_min_edge:
                        _bump_skip("centered_price_edge_below_min")
                        continue

            if action == "BUY_YES":
                _entry_price_bad = (
                    yes_price < self.entry_price_min
                    or yes_price > self.entry_price_max
                )
            else:
                # BUY_NO: allow rich-YES / cheap-NO setups above max YES;
                # still reject overly bearish YES where NO is already expensive.
                _entry_price_bad = yes_price < self.entry_price_min
            if _entry_price_bad:
                _bump_skip("entry_price_band")
                continue

            max_edge_updown = float(self.config.get("max_edge_updown", 0.15))
            if edge > max_edge_updown:
                _bump_skip("edge_above_cap")
                continue

            if not self.kelly_sizer:
                _bump_skip("kelly_unavailable")
                logger.error("ETH strategy: KellySizer unavailable — skipping entry sizing")
                continue
            raw_size = self.kelly_sizer.size_from_edge(
                self._signal_strategy_name, bankroll, edge
            )
            if self._btc_1h_regime_gates.get("enabled", False):
                raw_size *= self._regime_size_mult(btc_1h_regime)
            final_size = self.exposure_manager.scale_size(raw_size)
            if final_size < 0.5:
                _bump_skip("size_too_small")
                continue

            reason_parts.extend([
                f"ETH=${eth_price:,.2f}",
                f"BTC5m={btc_mom.m5_direction}",
                f"est_up={est_prob_up:.3f}",
                f"mkt_yes={yes_price:.3f}",
                f"RSI={eth.rsi_14:.0f}",
                f"oracle_basis={eth.oracle_basis_bps:+.1f}bps" if eth.oracle_basis_bps is not None else "",
                f"exp={exp_tier.value}(x{exp_multiplier:.1f})",
            ])
            signal = SolMacroSignal(
                market_id=market.id,
                market_question=market.question,
                action=action,
                price=yes_price if action == "BUY_YES" else (1 - yes_price),
                size=round(final_size, 2),
                confidence=round(confidence, 3),
                edge=round(edge, 4),
                token_id_yes=market.token_id_yes,
                token_id_no=market.token_id_no,
                end_date=market.end_date,
                direction=direction,
                sol_threshold=None,
                sol_current=round(eth_price, 2),
                btc_current=round(btc_ta.current_price, 2),
                lag_magnitude=None,
                ai_used=ai_used,
                reason=" | ".join(reason_parts),
                strategy_name=self._signal_strategy_name,
                alt_asset_code="eth",
                htf_bias=btc_htf_bias,
                btc_1h_regime=btc_1h_regime,
                window_size="5m" if is_5m else "15m",
                hour_utc=datetime.now(timezone.utc).hour,
                est_prob=round(est_prob_up, 4),
                rsi=round(eth.rsi_14, 1),
                corr_1h=round(corr.correlation_1h, 4),
            )
            signals.append(signal)

        gate_distributions = {k: _summarize(v) for k, v in gate_samples.items()}
        if gate_samples:
            logger.info(f"  [gate-dist] {gate_distributions}")
        _skip_top = dict(sorted(skip_reasons.items(), key=lambda kv: kv[1], reverse=True)[:6])
        logger.info(
            f"ETH Macro SCAN_DIAG base_side={allowed_side} mode={self.direction_source} "
            f"side_sources={side_source_counts} BTC_HTF={btc_htf_bias} eth_1H_trend={mtt.h1_trend} "
            f"enforce_alt_1h={self.enforce_alt_1h_alignment} markets={len(eth_markets)} signals={len(signals)} "
            f"skips_top6={_skip_top}"
        )
        self.last_scan_stats = {
            "enabled": True,
            "signals": len(signals),
            "markets_considered": len(eth_markets),
            "btc_1h_regime": btc_1h_regime,
            "btc_1h_regime_gates_enabled": bool(
                self._btc_1h_regime_gates.get("enabled", False)
            ),
            "btc_htf_bias": btc_htf_bias,
            "allowed_side": allowed_side,
            "direction_source": self.direction_source,
            "side_source_counts": side_source_counts,
            "alt_1h_trend": mtt.h1_trend,
            "enforce_alt_1h_alignment": self.enforce_alt_1h_alignment,
            "top_skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: kv[1], reverse=True)[:8]),
            "gate_distributions": gate_distributions,
        }
        return signals
