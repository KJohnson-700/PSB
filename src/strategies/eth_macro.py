"""
ETH Macro Strategy.

ETH is its own alt leg: ETH spot/HTF/oracle data drives primary direction.
BTC is secondary context/follow-quality input only.
"""
import asyncio
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
from src.market.scanner import Market, resolved_updown_window_minutes, updown_timeframe_label
from src.strategies.strategy_config import resolve_enabled_flag
from src.analysis.btc_1h_regime import regime_price
from src.analysis.lane_entry_policy import entry_policy_to_dict
from src.analysis.lane_identity import build_lane_metadata
from src.strategies.sol_macro import SolMacroSignal, SolMacroStrategy, macd_bearish_momentum_ok
from src.strategies.strategy_ai_context import (
    ai_recommendation_supports_action,
    format_market_metadata,
)
from src.execution.performance_feedback import (
    get_drift_min_edge_mult,
    get_loosen_min_edge_mult,
)
from src.analysis.rejected_candidate_log import (
    build_market_context,
    build_range_probe_variants,
    build_threshold_probe_variants,
    build_upper_cap_probe_variants,
    log_rejected_candidate,
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

    def _macro_leg_blocks_updown_side(
        self, market_allowed_side: str, lag_magnitude: Optional[float]
    ) -> tuple[bool, str, float]:
        """Return whether the journaled macro leg contradicts the intended side."""
        if lag_magnitude is None:
            return False, "", 0.0
        if market_allowed_side == "LONG":
            floor = float(self.config.get("updown_macro_leg_min_for_long", 0.0))
            blocked = lag_magnitude < floor
            return blocked, "macro_leg_blocks_long" if blocked else "", floor
        if market_allowed_side == "SHORT":
            ceiling = float(self.config.get("updown_macro_leg_max_for_short", 0.0))
            blocked = lag_magnitude > ceiling
            return blocked, "macro_leg_blocks_short" if blocked else "", ceiling
        return False, "", 0.0

    def __init__(
        self,
        config: Dict[str, Any],
        ai_agent: AIAgent,
        position_sizer: PositionSizer,
        kelly_sizer=None,
        exposure_manager: ExposureManager = None,
        ai_broker=None,
    ):
        super().__init__(config, ai_agent, position_sizer, kelly_sizer, exposure_manager, ai_broker=ai_broker)
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
        self.eth_follow_15m_min_adj_long = float(
            self.config.get("eth_follow_15m_min_adj_long", self.eth_follow_15m_min_adj)
        )
        self.eth_follow_15m_min_adj_short = float(
            self.config.get("eth_follow_15m_min_adj_short", self.eth_follow_15m_min_adj)
        )
        legacy_ai_timeout = float(self.config.get("ai_call_timeout_sec", 15.0) or 15.0)
        self.ai_decision_timeout_sec = float(
            self.config.get("ai_decision_timeout_sec", legacy_ai_timeout) or legacy_ai_timeout
        )
        observer_timeout_default = min(8.0, max(3.0, legacy_ai_timeout))
        self.ai_observer_timeout_sec = float(
            self.config.get("ai_observer_timeout_sec", observer_timeout_default)
            or observer_timeout_default
        )
        self._refresh_shadow_observer_controls()
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
            self.config.get("btc_follow_1h_allow_floor_without_rising", False)
        )
        self.direction_source = str(self.config.get("direction_source", "btc")).strip().lower()
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

    async def _evaluate_trade_decision_with_timeout(self, **kwargs):
        market_id = str(kwargs.get("market_id", ""))
        try:
            return await asyncio.wait_for(
                self.ai_agent.evaluate_trade_decision(**kwargs),
                timeout=self.ai_decision_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "ETH: evaluate_trade_decision timeout for market %s after %.1fs",
                market_id,
                self.ai_decision_timeout_sec,
            )
            return None

    async def _observe_rejected_candidate_with_timeout(self, **kwargs):
        market_id = str(kwargs.get("market_id", ""))
        try:
            return await asyncio.wait_for(
                self.ai_agent.observe_rejected_candidate(**kwargs),
                timeout=self.ai_observer_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "ETH: rejected observer timeout for market %s after %.1fs",
                market_id,
                self.ai_observer_timeout_sec,
            )
            return None

    def _eth_stf_bypass_when_macro_agrees(
        self, btc_htf_bias: Optional[str], market_allowed_side: str
    ) -> bool:
        """True when BULLISH+LONG or BEARISH+SHORT — STF BTC follow can still fail in grindy tape."""
        if not self.btc_follow_stf_bypass_when_macro_agrees:
            return False
        if btc_htf_bias == "BULLISH" and market_allowed_side == "LONG":
            return True
        if btc_htf_bias == "BEARISH" and market_allowed_side == "SHORT":
            return True
        return False

    def _eth_follow_15m_required_adj(self, market_allowed_side: str) -> float:
        if str(market_allowed_side or "").upper() == "SHORT":
            return float(self.eth_follow_15m_min_adj_short)
        return float(self.eth_follow_15m_min_adj_long)

    def _record_eth_abort(self, reason: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "enabled": self.enabled,
            "signals": 0,
            "abort_reason": reason,
            "markets_considered": 0,
            "buy_no_skip_counts": {},
            "last_buy_no_skip_sample": {},
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
    def _eth_1h_follow_score(macd_1h: MACDResult, allowed_side: str) -> tuple[float, List[str]]:
        reasons: List[str] = []
        score = 0.0
        if allowed_side == "LONG":
            if macd_1h.crossover == "BULLISH_CROSS":
                score = 0.05
                reasons.append("ETH1h bull cross")
            elif macd_1h.histogram > 0 and macd_1h.histogram_rising:
                score = 0.04
                reasons.append("ETH1h green+rising")
            elif macd_1h.crossover == "BEARISH_CROSS" or macd_1h.histogram < 0:
                score = -0.04
                reasons.append("ETH1h against")
        else:
            if macd_1h.crossover == "BEARISH_CROSS":
                score = 0.05
                reasons.append("ETH1h bear cross")
            elif macd_1h.histogram < 0 and not macd_1h.histogram_rising:
                score = 0.04
                reasons.append("ETH1h red+falling")
            elif macd_1h.crossover == "BULLISH_CROSS" or macd_1h.histogram > 0:
                score = -0.04
                reasons.append("ETH1h against")
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
        # - btc: legacy label; live side remains the alt-derived base side.
        # - hybrid: alt-derived base side remains primary; market/BTC only annotate strength.
        # - signal_first: test mode where 15m market signal can set side directly
        if self.direction_source == "btc":
            return base_side, "alt_1h_legacy_btc_mode"

        signal_15m = float(market_yes_price)

        if self.direction_source == "signal_first":
            # Market 15m signal can set side, but require BTC HTF agreement (or NEUTRAL)
            # so we don't LONG into a BEARISH macro purely from market YES price.
            if signal_15m >= self.signal_15m_long_threshold and btc_htf_bias != "BEARISH":
                return "LONG", "signal_first_long"
            if signal_15m <= self.signal_15m_short_threshold and btc_htf_bias != "BULLISH":
                return "SHORT", "signal_first_short"
            return base_side, "signal_first_fallback"

        if base_side == "LONG" and signal_15m >= self.signal_15m_long_threshold:
            return base_side, "hybrid_alt_long_confirmed"
        if base_side == "SHORT" and signal_15m <= self.signal_15m_short_threshold:
            return base_side, "hybrid_alt_short_confirmed"
        return base_side, "hybrid_alt_first"

    async def scan_and_analyze(self, markets: List[Market], bankroll: float) -> List[SolMacroSignal]:
        _phase_t0 = time.perf_counter()
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
        if not eth_ta:
            logger.warning("ETH Macro strategy: ETH analysis unavailable")
            self._record_eth_abort(
                "analysis_unavailable",
                {"markets_considered": len(eth_markets)},
            )
            return []

        btc_full_ok = btc_ta is not None
        btc_htf_details = None
        if btc_ta:
            btc_htf_details = self._get_btc_htf_bias_details(btc_ta)
            btc_htf_bias = str(btc_htf_details["bias"])
        else:
            btc_htf_bias = "NEUTRAL"
            logger.warning(
                "ETH Macro: BTC full analysis unavailable — continuing on ETH leg "
                "(correlation/BTC klines from alt service may still inform calc_correlation)"
            )

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
        # 2026-05-16 calibration: always classify the BTC 1H regime so signal
        # diagnostics and dampener decisions reflect reality. The `enabled` flag
        # continues to gate the min_edge/size multiplier *application*.
        btc_1h_regime = "BULL"
        if btc_ta:
            btc_1h_regime = self._classify_btc_1h_regime(btc_ta)
            if self._btc_1h_regime_gates.get("enabled", False):
                logger.info(
                    "ETH Macro BTC 1H regime: %s | min_edge×%.2f size×%.2f | spot=%.0f SMA20=%.0f",
                    btc_1h_regime,
                    self._regime_min_edge_mult(btc_1h_regime),
                    self._regime_size_mult(btc_1h_regime),
                    regime_price(btc_ta),
                    float(getattr(btc_ta, "sma_1h_20", 0.0) or 0.0),
                )
            else:
                logger.debug(
                    "ETH Macro BTC 1H regime classified (multipliers gated off): %s",
                    btc_1h_regime,
                )

        # Non-BTC strategies are alt-first: ETH 1H establishes direction; BTC
        # is secondary context/fallback when ETH has no usable bias.
        skip_btc_follow_1h = False
        alt_1h_trend = corr.sol_trend
        if alt_1h_trend in {"BULLISH", "BEARISH"}:
            allowed_side = "LONG" if alt_1h_trend == "BULLISH" else "SHORT"
            skip_btc_follow_1h = True
            logger.info(
                "ETH Macro: ETH 1H %s is primary — side %s; BTC HTF %s is secondary",
                alt_1h_trend,
                allowed_side,
                btc_htf_bias,
            )
            # Symmetric four-path resolver — gates LONG and SHORT with their own LTF
            # momentum in both regimes. Falls back to legacy regime-default + buy_no
            # override when only the buy_no flag is on.
            resolver_active = (
                self.buy_yes_ltf_override_enabled
                or self.buy_no_ltf_override_enabled
                or self.buy_yes_4h_hist_override_enabled
                or self.buy_no_4h_hist_override_enabled
            )
            if resolver_active:
                _resolved_side, _resolved_source, _resolved_detail = (
                    self._resolve_allowed_side_with_ltf_overrides(eth_ta, alt_1h_trend)
                )
                # Additive-only resolver: side is never None for BULLISH/BEARISH inputs.
                # Defaults match legacy; only exception path can flip side.
                if _resolved_side and _resolved_side != allowed_side:
                    logger.info(
                        "ETH Macro: LTF resolver flipped to exception → %s (%s) — %s",
                        _resolved_side,
                        _resolved_source,
                        _resolved_detail,
                    )
                if _resolved_side is not None:
                    allowed_side = _resolved_side
            elif alt_1h_trend == "BULLISH" and self.buy_no_ltf_override_enabled:
                _short_override, _short_override_reason = self._buy_no_ltf_override(eth_ta)
                if _short_override:
                    allowed_side = "SHORT"
                    logger.info(
                        "ETH Macro: bullish ETH 1H SHORT override enabled — %s",
                        _short_override_reason,
                    )
        else:
            # ETH 1H NEUTRAL: alt has no usable bias → sit out.
            # 2026-05-21 audit: previously this block had THREE BTC-decides-side
            # paths (BTC spike → catch-up side, lag_opportunity → opportunity_direction,
            # and btc_htf_bias → LONG/SHORT fallback). All violated the "alts decided
            # by alt-native indicators" rule. BTC spike/lag/HTF stay useful as
            # diagnostic context but must not pick ETH's side.
            _btc_ctx_bits = []
            if corr.btc_spike_detected:
                _btc_ctx_bits.append(f"BTC spike {corr.btc_move_5m_pct:+.2f}%")
            if corr.lag_opportunity:
                _btc_ctx_bits.append(
                    f"BTC lag dir={corr.opportunity_direction} mag={abs(corr.opportunity_magnitude):.2f}%"
                )
            if btc_htf_bias in ("BULLISH", "BEARISH"):
                _btc_ctx_bits.append(f"BTC HTF={btc_htf_bias}")
            _btc_ctx = f" [diagnostic: {', '.join(_btc_ctx_bits)}]" if _btc_ctx_bits else ""
            logger.info(
                "ETH Macro: ETH 1H NEUTRAL — sitting out (alt-native only)%s",
                _btc_ctx,
            )
            self._record_eth_abort(
                "neutral_alt_no_bias",
                {"markets_considered": len(eth_markets), "btc_ctx": _btc_ctx},
            )
            return []

        if (
            btc_ta
            and self.btc_follow_1h_required
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
        btc_mom = btc_ta.candle_momentum if btc_ta else CandleMomentum()
        mtt = eth_ta.multi_tf

        if btc_ta:
            logger.info(
                f"ETH ${eth_price:,.2f} | BTC_HTF={btc_htf_bias} raw={btc_htf_details['raw_bias'] if btc_htf_details else '?'} "
                f"votes[s={btc_htf_details['sabre_vote'] if btc_htf_details else '?'} "
                f"p={btc_htf_details['price_vs_ma_vote'] if btc_htf_details else '?'} "
                f"m={btc_htf_details['macd_vote'] if btc_htf_details else '?'}:{btc_htf_details['macd_state'] if btc_htf_details else '?'}] "
                f"BTC4H hist={btc_htf_details['macd_4h_histogram']:+.1f} "
                f"BTC1H hist={btc_ta.macd_1h.histogram:+.2f} "
                f"BTC15m={btc_ta.macd_15m.histogram:+.3f} BTC5m={btc_mom.m5_direction}({btc_mom.m5_move_pct:+.3f}%) "
                f"| ETH15m={eth.macd_15m.histogram:+.3f} {eth.macd_15m.crossover} "
                f"| ETH5m={eth.macd_5m.histogram:+.3f} {eth.macd_5m.crossover} | RSI={eth.rsi_14:.0f}"
            )
        else:
            logger.info(
                f"ETH ${eth_price:,.2f} | BTC_HTF=UNAVAILABLE | "
                f"ETH15m={eth.macd_15m.histogram:+.3f} {eth.macd_15m.crossover} "
                f"| ETH5m={eth.macd_5m.histogram:+.3f} {eth.macd_5m.crossover} | RSI={eth.rsi_14:.0f}"
            )

        signals: List[SolMacroSignal] = []
        ai_calls = 0
        research_calls = 0
        research_plans_logged = 0
        shadow_pipeline_calls = 0
        shadow_pipeline_ok = 0
        shadow_observer_calls = 0
        shadow_observer_ok = 0
        shadow_marginal_mismatch = 0
        research_enabled = self.ai_agent.research_narrative_enabled()
        research_max_calls = self.ai_agent.research_narrative_max_calls_per_scan()
        research_min_conf = self.ai_agent.research_narrative_min_confidence()
        skip_reasons: Dict[str, int] = {}
        gate_samples: Dict[str, list] = {}
        action_counts: Dict[str, int] = {}
        side_source_counts: Dict[str, int] = {}
        buy_no_skip_counts: Dict[str, int] = {}
        last_buy_no_skip_sample: Dict[str, Any] = {}

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

        def _log_skip_reject(
            *,
            market: Any,
            window: str,
            side: str,
            action: str,
            reason: str,
            yes_price: Optional[float],
            est_prob_up: float = 0.50,
            htf_bias: Optional[str] = None,
            context: Optional[Dict[str, Any]] = None,
            probe_variants: Optional[List[Dict[str, Any]]] = None,
            policy_version: Optional[str] = None,
            stage: Optional[str] = None,
        ) -> None:
            merged_context: Dict[str, Any] = dict(context or {})
            merged_context.update(
                build_market_context(
                    asset_spot=getattr(eth, "current_price", None),
                    btc_spot=getattr(corr, "btc_price", None),
                    rsi_14=getattr(eth, "rsi_14", None),
                    atr_14=getattr(eth, "atr_14", None),
                )
            )
            if "btc_1h_regime" in locals():
                merged_context["btc_1h_regime"] = btc_1h_regime
            log_rejected_candidate(
                strategy=self._signal_strategy_name,
                window=window,
                side=side,
                action=action,
                reason=reason,
                market=market,
                yes_price=yes_price,
                est_prob_up=est_prob_up,
                htf_bias=htf_bias,
                context=merged_context,
                probe_variants=probe_variants or [],
                policy_version=policy_version,
                stage=stage,
                btc_1h_regime=btc_1h_regime if "btc_1h_regime" in locals() else None,
            )

        observer_tasks: List[asyncio.Task] = []

        async def _observe_structural_reject(
            *,
            market: Any,
            window: str,
            side: str,
            action: str,
            reason: str,
            yes_price: float,
            quant_edge: Optional[float],
            quant_threshold: Optional[float],
            htf_bias: Optional[str],
            context_lines: List[str],
            metadata: Optional[Dict[str, Any]] = None,
        ) -> None:
            nonlocal shadow_pipeline_calls, shadow_pipeline_ok
            nonlocal shadow_observer_calls, shadow_observer_ok
            enabled = getattr(self.ai_agent, "shadow_observer_enabled", lambda: False)()
            if not isinstance(enabled, bool) or not enabled:
                return
            try:
                max_calls = int(
                    getattr(self.ai_agent, "shadow_observer_max_calls_per_scan", lambda: 0)()
                )
            except (TypeError, ValueError):
                return
            if max_calls <= 0:
                return
            if shadow_pipeline_calls >= max_calls:
                return
            market_id = str(getattr(market, "id", "") or "")
            self._prune_shadow_observer_state()
            if len(self._shadow_observer_tasks) >= self.ai_observer_max_inflight:
                return
            observer_key = self._shadow_observer_key(market_id=market_id, reason=reason)
            now = time.monotonic()
            if self._shadow_observer_retry_after.get(observer_key, 0.0) > now:
                return
            self._shadow_observer_retry_after[observer_key] = (
                now + self.ai_observer_retry_cooldown_sec
            )
            try:
                market_metadata = format_market_metadata(market)
            except Exception:
                market_metadata = (
                    f"id={getattr(market, 'id', '')}\n"
                    f"question={getattr(market, 'question', '')}\n"
                    f"slug={getattr(market, 'slug', '')}"
                )
            lane_id = str(
                build_lane_metadata(
                    strategy=self._signal_strategy_name,
                    window_size=window,
                    action=action,
                    direction=("down" if action == "BUY_NO" else "up"),
                    entry_leg=("NO" if action == "BUY_NO" else "YES"),
                    side_source="rejected_observer",
                    ai_used=True,
                    reason=reason,
                    signal_reason=f"rejected_candidate_{reason}",
                    htf_bias=htf_bias,
                    primary_htf_bias=htf_bias,
                ).get("lane_id")
                or ""
            )
            observer_context = "\n".join(
                [
                    market.description or market.question,
                    "",
                    "=== REJECTED CANDIDATE OBSERVER ===",
                    f"reason={reason}",
                    f"window={window}",
                    f"side={side}",
                    f"action={action}",
                    f"yes_price={yes_price:.4f}",
                    *[line for line in context_lines if line],
                    "",
                    f"=== MARKET ===\n{market_metadata}",
                ]
            )
            # Consume the scan budget before awaiting the observer. Timed-out
            # observer calls should still count as attempts; otherwise one slow
            # provider can trigger repeated best-effort AI calls across many
            # rejected markets in the same scan.
            shadow_pipeline_calls += 1
            shadow_observer_calls += 1
            async def _run_observer() -> Optional[Dict[str, Any]]:
                return await self._observe_rejected_candidate_with_timeout(
                    rejection_reason=reason,
                    market_question=market.question,
                    market_description=observer_context,
                    current_yes_price=yes_price,
                    market_id=market_id,
                    strategy_hint=self._signal_strategy_name,
                    lane_id=lane_id,
                    quant_action=action,
                    quant_edge=quant_edge,
                    quant_threshold=quant_threshold,
                    metadata=metadata or {},
                )

            def _finalize_observer(task: asyncio.Task) -> None:
                nonlocal shadow_pipeline_ok, shadow_observer_ok
                self._shadow_observer_tasks.discard(task)
                try:
                    observer_out = task.result()
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.debug(
                        "%s: rejected observer task failed for market %s",
                        self._signal_strategy_name,
                        getattr(market, "id", ""),
                        exc_info=True,
                    )
                    return
                if observer_out and observer_out.get("ok"):
                    shadow_pipeline_ok += 1
                    shadow_observer_ok += 1

            task = asyncio.create_task(_run_observer())
            self._shadow_observer_tasks.add(task)
            task.add_done_callback(_finalize_observer)
            observer_tasks.append(task)

        _latency_sec = float(self.config.get("entry_window_latency_buffer_sec", 0.0) or 0.0)

        _phase_t_preloop = time.perf_counter()
        for market in eth_markets:
            rsi_soft_delta = 0.0
            rsi_soft_penalty = 0.0
            _updown_tf = updown_timeframe_label(resolved_updown_window_minutes(market))
            is_5m = _updown_tf == "5m"
            is_1h = _updown_tf == "1h"
            yes_price = market.yes_price
            market_allowed_side, side_source = self._resolve_market_side(
                allowed_side, btc_htf_bias, yes_price
            )
            action = "BUY_YES" if market_allowed_side == "LONG" else "BUY_NO"
            primary_htf_bias = "BULLISH" if market_allowed_side == "LONG" else "BEARISH"

            # ETH-native momentum guards. 2026-05-23 ghost-counterfactual review:
            # default-on guards were breakeven-to-harmful (BUY_NO n=9826 WR=48%,
            # 1h SHORT specifically WR=62% — guard blocking winners). Now an
            # explicit per-(side, window) allowlist via `eth_momentum_confirm:
            # {buy_yes: [...], buy_no: [...]}`. Empty/missing = guard off.
            _eth_mc_cfg = self.config.get("eth_momentum_confirm") or {}
            if (
                action == "BUY_NO"
                and _updown_tf in (_eth_mc_cfg.get("buy_no") or [])
            ):
                _eth_bear_confirmed = (
                    eth.macd_5m.crossover == "BEARISH_CROSS"
                    or (eth.macd_5m.histogram < 0 and not eth.macd_5m.histogram_rising)
                    or eth.macd_15m.crossover == "BEARISH_CROSS"
                    or (eth.macd_15m.histogram < 0 and not eth.macd_15m.histogram_rising)
                )
                if not _eth_bear_confirmed:
                    _bump_skip("buy_no_no_eth_momentum_confirm")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=market_allowed_side,
                        action=action,
                        reason="buy_no_no_eth_momentum_confirm",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={
                            "eth_macd_5m_hist": float(eth.macd_5m.histogram or 0.0),
                            "eth_macd_5m_rising": bool(eth.macd_5m.histogram_rising),
                            "eth_macd_5m_crossover": eth.macd_5m.crossover,
                            "eth_macd_15m_hist": float(eth.macd_15m.histogram or 0.0),
                            "eth_macd_15m_rising": bool(eth.macd_15m.histogram_rising),
                            "eth_macd_15m_crossover": eth.macd_15m.crossover,
                            "side_source": side_source,
                        },
                    )
                    continue
            if (
                action == "BUY_YES"
                and _updown_tf in (_eth_mc_cfg.get("buy_yes") or [])
            ):
                _eth_bull_confirmed = (
                    eth.macd_5m.crossover == "BULLISH_CROSS"
                    or (eth.macd_5m.histogram > 0 and eth.macd_5m.histogram_rising)
                    or eth.macd_15m.crossover == "BULLISH_CROSS"
                    or (eth.macd_15m.histogram > 0 and eth.macd_15m.histogram_rising)
                )
                if not _eth_bull_confirmed:
                    _bump_skip("buy_yes_no_eth_momentum_confirm")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=market_allowed_side,
                        action=action,
                        reason="buy_yes_no_eth_momentum_confirm",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={
                            "eth_macd_5m_hist": float(eth.macd_5m.histogram or 0.0),
                            "eth_macd_5m_rising": bool(eth.macd_5m.histogram_rising),
                            "eth_macd_5m_crossover": eth.macd_5m.crossover,
                            "eth_macd_15m_hist": float(eth.macd_15m.histogram or 0.0),
                            "eth_macd_15m_rising": bool(eth.macd_15m.histogram_rising),
                            "eth_macd_15m_crossover": eth.macd_15m.crossover,
                            "side_source": side_source,
                        },
                    )
                    continue

            _liq_floor = self._resolve_min_liquidity_floor(
                window_size=_updown_tf,
                action=action,
            )
            if market.liquidity > 0 and market.liquidity < _liq_floor:
                _bump_skip("liquidity")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason="liquidity",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    stage="liquidity",
                    context={
                        "market_liquidity": float(market.liquidity),
                        "min_liquidity": float(_liq_floor),
                    },
                )
                await _observe_structural_reject(
                    market=market,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason="liquidity",
                    yes_price=yes_price,
                    quant_edge=None,
                    quant_threshold=float(_liq_floor),
                    htf_bias=primary_htf_bias,
                    context_lines=[
                        f"market_liquidity={float(market.liquidity):.2f}",
                        f"min_liquidity={float(_liq_floor):.2f}",
                    ],
                    metadata={"market_liquidity": float(market.liquidity)},
                )
                continue

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
            _sample("mins_left", _mins_left)
            _timing_window_open = self._within_entry_timing_window(
                mins_left=_eval_left,
                tf=_updown_tf,
            )

            if getattr(corr, "degraded", False) and self.skip_on_degraded_correlation:
                _bump_skip("degraded_correlation")
                logger.info(
                    f"  ETH skip '{market.question[:40]}' — correlation degraded "
                    f"({', '.join(getattr(corr, 'degraded_reasons', [])) or 'unknown'})"
                )
                continue

            # 2026-05-22: btc_min_move_dollars gate REMOVED. Previously skipped ETH
            # entries when BTC hadn't moved enough in dollars (BTC deciding ETH
            # admission, with a partial alt-aligned bypass). Per "alts decided by
            # alt-native indicators", BTC volatility must not gate ETH entry.
            # Diagnostic-only: log low-BTC-move context in reason_parts.
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
                reason_parts.append(f"diag_btc_flat(${_btc_move:.0f}<${_btc_min_move:.0f})")

            _sample("entry_price", yes_price)
            if yes_price < 0.20 or yes_price > 0.80:
                _bump_skip("price_too_far")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason="price_too_far",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={
                        "entry_price": float(yes_price),
                        "entry_price_min": 0.20,
                        "entry_price_max": 0.80,
                    },
                    probe_variants=build_range_probe_variants(
                        metric_name="entry_price_band",
                        observed_value=float(yes_price),
                        baseline_min=0.20,
                        baseline_max=0.80,
                        relax_steps=[0.02, 0.05, 0.10],
                        tighten_steps=[0.02, 0.05],
                    ),
                    policy_version="entry_price_band_v1",
                )
                continue

            side_source_counts[side_source] = side_source_counts.get(side_source, 0) + 1

            action_counts[action] = action_counts.get(action, 0) + 1
            direction = "UP" if market_allowed_side == "LONG" else "DOWN"
            reason_parts = [
                f"ETH_HTF={alt_1h_trend}",
                f"BTC_HTF={btc_htf_bias}",
                f"PRIMARY_HTF={primary_htf_bias}",
                f"side={market_allowed_side}",
                f"side_src={side_source}",
            ]
            follow_penalty_min_edge_add = 0.0
            if btc_htf_details:
                reason_parts.append(
                    "BTC4H_votes="
                    f"sabre:{btc_htf_details['sabre_vote']},"
                    f"price_ma:{btc_htf_details['price_vs_ma_vote']},"
                    f"macd:{btc_htf_details['macd_vote']}:{btc_htf_details['macd_state']},"
                    f"raw:{btc_htf_details['raw_bias']},"
                    f"final:{btc_htf_details['bias']}"
                )
                reason_parts.append(
                    "BTC4H_ctx="
                    f"spot:{btc_htf_details['btc_price']:.0f},"
                    f"ma:{btc_htf_details['sabre_ma']:.0f},"
                    f"hist:{btc_htf_details['macd_4h_histogram']:+.1f}/"
                    f"{btc_htf_details['min_hist']:.1f},"
                    f"rising:{btc_htf_details['macd_4h_histogram_rising']}"
                )

            if self.enforce_alt_1h_alignment:
                if action == "BUY_NO" and mtt.h1_trend == "BULLISH":
                    reason_parts.append("buy_no_against_alt_1h_bullish")
                    logger.info(
                        f"  ETH allow BUY_NO on '{market.question[:40]}' — "
                        f"ETH 1H BULLISH retained as diagnostic only"
                    )
                if action == "BUY_YES" and mtt.h1_trend == "BEARISH":
                    reason_parts.append("buy_yes_against_alt_1h_bearish")
                    logger.info(
                        f"  ETH allow BUY_YES on '{market.question[:40]}' — "
                        f"ETH 1H BEARISH retained as diagnostic only"
                    )
            _rsi_hard_block, rsi_soft_delta = self._resolve_rsi_gate(action, eth.rsi_14)
            if _rsi_hard_block:
                _bump_skip("rsi_hard_blocked")
                if action == "BUY_NO":
                    self._emit_buy_no_skip(
                        market=market,
                        bankroll=bankroll,
                        payload=self._make_buy_no_skip_payload(
                            market=market,
                            skip_reason="rsi_hard_blocked",
                            window_size=_updown_tf,
                            yes_price=yes_price,
                            edge=0.0,
                            effective_min_edge=0.0,
                            rsi=eth.rsi_14,
                            htf_bias=btc_htf_bias,
                            signal_reason=" | ".join(reason_parts),
                            alt_1h_trend=mtt.h1_trend,
                        ),
                        counts=buy_no_skip_counts,
                        last_sample=last_buy_no_skip_sample,
                    )
                continue
            rsi_soft_penalty = abs(rsi_soft_delta)
            if rsi_soft_penalty > 0:
                reason_parts.append(f"rsi_soft_penalty={rsi_soft_penalty:.3f}")
                _sample("rsi_soft_penalty", rsi_soft_penalty)
            oracle_validation = self._validate_updown_oracle(eth)
            if not oracle_validation.passed:
                _bump_skip(oracle_validation.reason)
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason=oracle_validation.reason,
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={
                        "oracle_basis_bps": (
                            float(oracle_validation.basis_bps)
                            if oracle_validation.basis_bps is not None
                            else None
                        ),
                        "oracle_freshness_sec": (
                            float(oracle_validation.freshness_sec)
                            if oracle_validation.freshness_sec is not None
                            else None
                        ),
                        "oracle_max_age_sec": float(
                            self.config.get("oracle_max_age_sec", 0.0) or 0.0
                        ),
                        "oracle_max_basis_bps": float(
                            self.config.get("oracle_max_basis_bps", 0.0) or 0.0
                        ),
                    },
                    probe_variants=build_upper_cap_probe_variants(
                        metric_name="oracle_basis_abs_bps",
                        observed_value=abs(float(oracle_validation.basis_bps or 0.0)),
                        baseline_cap=float(self.config.get("oracle_max_basis_bps", 0.0) or 0.0),
                        relax_steps=[2.0, 5.0, 10.0],
                        tighten_steps=[2.0, 5.0],
                    ) if oracle_validation.reason == "oracle_basis_block" else [],
                    policy_version=(
                        "oracle_basis_block_v1"
                        if oracle_validation.reason == "oracle_basis_block"
                        else f"{oracle_validation.reason}_v1"
                    ),
                    stage="oracle",
                )
                continue

            est_prob_up = 0.50
            confidence = 0.50
            ai_used = False

            if is_5m:
                btc_impulse, btc_reasons = self._btc_follow_5m_impulse_score(
                    btc_mom, market_allowed_side
                )
                _impulse_gate_ok = btc_impulse > 0
                if not btc_full_ok:
                    _impulse_gate_ok = True
                    btc_reasons.append("btc_full_analysis_unavailable_eth_leg_only")
                elif (
                    self._eth_stf_bypass_when_macro_agrees(btc_htf_bias, market_allowed_side)
                    and not _impulse_gate_ok
                ):
                    _impulse_gate_ok = True
                    btc_reasons.append("bypass_5m_stf_macro_agrees")
                if self.btc_follow_5m_requires_impulse and not _impulse_gate_ok:
                    _bump_skip("btc_5m_no_impulse")
                    log_rejected_candidate(
                        strategy=self._signal_strategy_name, window="5m",
                        side=market_allowed_side, action=action,
                        reason="btc_5m_no_impulse", market=market,
                        yes_price=yes_price, est_prob_up=est_prob_up,
                        htf_bias=primary_htf_bias,
                        stage="signal_strength_5m",
                        context={
                            "btc_impulse": float(btc_impulse),
                            "btc_reasons": list(btc_reasons),
                            **build_market_context(
                                asset_spot=eth.current_price,
                                btc_spot=corr.btc_price,
                                rsi_14=eth.rsi_14,
                                atr_14=eth.atr_14,
                            ),
                        },
                    )
                    continue
                eth_5m_adj, eth_reasons = self._eth_5m_macd_score(
                    eth.macd_5m, market_allowed_side
                )
                if eth_5m_adj < self.eth_follow_5m_min_adj:
                    _bump_skip("eth_5m_weak_confirm")
                    log_rejected_candidate(
                        strategy=self._signal_strategy_name, window="5m",
                        side=market_allowed_side, action=action,
                        reason="eth_5m_weak_confirm", market=market,
                        yes_price=yes_price, est_prob_up=est_prob_up,
                        htf_bias=primary_htf_bias,
                        stage="signal_strength_5m",
                        context={
                            "eth_5m_adj": float(eth_5m_adj),
                            "min_required": float(self.eth_follow_5m_min_adj),
                            **build_market_context(
                                asset_spot=eth.current_price,
                                btc_spot=corr.btc_price,
                                rsi_14=eth.rsi_14,
                                atr_14=eth.atr_14,
                            ),
                        },
                    )
                    continue
                est_prob_up = self._apply_primary_htf_bias(est_prob_up, primary_htf_bias, 0.04)
                # Move 2 (2026-05-16): dampen est_prob when ETH 1H trend disagrees with side.
                if self.enforce_alt_1h_alignment:
                    if market_allowed_side == "LONG" and mtt.h1_trend == "BEARISH":
                        est_prob_up -= 0.04
                        reason_parts.append("h1_dampen_long_5m")
                    elif market_allowed_side == "SHORT" and mtt.h1_trend == "BULLISH":
                        est_prob_up += 0.04
                        reason_parts.append("h1_dampen_short_5m")
                est_prob_up += btc_impulse if market_allowed_side == "LONG" else -btc_impulse
                est_prob_up += eth_5m_adj if market_allowed_side == "LONG" else -eth_5m_adj
                if eth.rsi_14 > 75:
                    est_prob_up -= 0.02
                elif eth.rsi_14 < 25:
                    est_prob_up += 0.02
                confidence = max(0.55, min(0.85, 0.50 + abs(btc_impulse) * 1.8 + abs(eth_5m_adj) * 2.0))
                reason_parts.extend(["UPDOWN_5m", *btc_reasons, *eth_reasons])
            else:
                if is_1h:
                    if btc_full_ok and not self._btc_follow_1h_ok(btc_ta, market_allowed_side):
                        if market_allowed_side == "SHORT":
                            est_prob_up += float(
                                self.config.get("btc_1h_not_following_short_penalty", 0.04)
                            )
                            follow_penalty_min_edge_add += float(
                                self.config.get("btc_1h_not_following_short_min_edge_add", 0.01)
                            )
                            reason_parts.append("btc_1h_follow_penalty_short")
                        else:
                            _bump_skip("btc_1h_not_following")
                            log_rejected_candidate(
                                strategy=self._signal_strategy_name, window="1h",
                                side=market_allowed_side, action=action,
                                reason="btc_1h_not_following", market=market,
                                yes_price=yes_price, est_prob_up=est_prob_up,
                                htf_bias=primary_htf_bias,
                                stage="signal_strength_1h",
                                context=build_market_context(
                                    asset_spot=eth.current_price,
                                    btc_spot=corr.btc_price,
                                    rsi_14=eth.rsi_14,
                                    atr_14=eth.atr_14,
                                ),
                            )
                            continue
                    eth_1h_adj, eth_reasons = self._eth_1h_follow_score(
                        eth.macd_1h, market_allowed_side
                    )
                    eth_1h_min_adj = float(
                        self.config.get(
                            "eth_follow_1h_min_adj",
                            max(0.03, self.eth_follow_15m_min_adj * 0.8),
                        )
                    )
                    if eth_1h_adj < eth_1h_min_adj:
                        _bump_skip("eth_1h_weak_confirm")
                        log_rejected_candidate(
                            strategy=self._signal_strategy_name, window="1h",
                            side=market_allowed_side, action=action,
                            reason="eth_1h_weak_confirm", market=market,
                            yes_price=yes_price, est_prob_up=est_prob_up,
                            htf_bias=primary_htf_bias,
                            stage="signal_strength_1h",
                            context={
                                "eth_1h_adj": float(eth_1h_adj),
                                "min_required": float(eth_1h_min_adj),
                                **build_market_context(
                                    asset_spot=eth.current_price,
                                    btc_spot=corr.btc_price,
                                    rsi_14=eth.rsi_14,
                                    atr_14=eth.atr_14,
                                ),
                            },
                        )
                        continue
                    est_prob_up = self._apply_primary_htf_bias(est_prob_up, primary_htf_bias, 0.09)
                    if self.enforce_alt_1h_alignment:
                        if market_allowed_side == "LONG" and mtt.h1_trend == "BEARISH":
                            est_prob_up -= 0.06
                            reason_parts.append("h1_dampen_long_1h")
                        elif market_allowed_side == "SHORT" and mtt.h1_trend == "BULLISH":
                            est_prob_up += 0.06
                            reason_parts.append("h1_dampen_short_1h")
                    est_prob_up += eth_1h_adj if market_allowed_side == "LONG" else -eth_1h_adj
                    if eth.rsi_14 > 75:
                        est_prob_up -= 0.02
                    elif eth.rsi_14 < 25:
                        est_prob_up += 0.02
                    confidence = max(0.55, min(0.85, 0.50 + abs(eth_1h_adj) * 1.8))
                    reason_parts.extend(
                        [
                            "UPDOWN_1h",
                            *eth_reasons,
                            f"slug={market.slug or '?'}",
                            f"mins_left={_mins_left:.2f}",
                            f"end={_end_utc.isoformat()}",
                        ]
                    )
                else:
                    if btc_full_ok and not self._btc_follow_15m_impulse_ok(
                        btc_ta, market_allowed_side
                    ):
                        if self._eth_stf_bypass_when_macro_agrees(btc_htf_bias, market_allowed_side):
                            pass
                        elif (
                            self.btc_follow_stf_bypass_if_1h_ok
                            and self._btc_follow_1h_ok(btc_ta, market_allowed_side)
                        ):
                            pass
                        elif market_allowed_side == "SHORT":
                            est_prob_up += float(
                                self.config.get("btc_15m_not_following_short_penalty", 0.03)
                            )
                            follow_penalty_min_edge_add += float(
                                self.config.get("btc_15m_not_following_short_min_edge_add", 0.01)
                            )
                            reason_parts.append("btc_15m_follow_penalty_short")
                        else:
                            _bump_skip("btc_15m_not_following")
                            log_rejected_candidate(
                                strategy=self._signal_strategy_name, window="15m",
                                side=market_allowed_side, action=action,
                                reason="btc_15m_not_following", market=market,
                                yes_price=yes_price, est_prob_up=est_prob_up,
                                htf_bias=primary_htf_bias,
                                stage="signal_strength_15m",
                                context=build_market_context(
                                    asset_spot=eth.current_price,
                                    btc_spot=corr.btc_price,
                                    rsi_14=eth.rsi_14,
                                    atr_14=eth.atr_14,
                                ),
                            )
                            continue
                    eth_15m_adj, eth_reasons = self._eth_15m_follow_score(
                        eth.macd_15m, market_allowed_side
                    )
                    required_eth_15m_adj = self._eth_follow_15m_required_adj(
                        market_allowed_side
                    )
                    if eth_15m_adj < required_eth_15m_adj:
                        _bump_skip("eth_15m_weak_confirm")
                        log_rejected_candidate(
                            strategy=self._signal_strategy_name, window="15m",
                            side=market_allowed_side, action=action,
                            reason="eth_15m_weak_confirm", market=market,
                            yes_price=yes_price, est_prob_up=est_prob_up,
                            htf_bias=primary_htf_bias,
                            stage="signal_strength_15m",
                            context={
                                "eth_15m_adj": float(eth_15m_adj),
                                "min_required": float(required_eth_15m_adj),
                                "base_min_required": float(self.eth_follow_15m_min_adj),
                                "lane_specific_relaxation": bool(
                                    required_eth_15m_adj != self.eth_follow_15m_min_adj
                                ),
                                **build_market_context(
                                    asset_spot=eth.current_price,
                                    btc_spot=corr.btc_price,
                                    rsi_14=eth.rsi_14,
                                    atr_14=eth.atr_14,
                                ),
                            },
                            probe_variants=build_threshold_probe_variants(
                                metric_name="eth_15m_confirm_adj",
                                observed_value=float(eth_15m_adj),
                                baseline_threshold=float(required_eth_15m_adj),
                            ),
                            policy_version="eth_15m_confirm_v2",
                        )
                        continue
                    est_prob_up = self._apply_primary_htf_bias(est_prob_up, primary_htf_bias, 0.08)
                    # Move 2 (2026-05-16): dampen est_prob when ETH 1H trend disagrees with side.
                    if self.enforce_alt_1h_alignment:
                        if market_allowed_side == "LONG" and mtt.h1_trend == "BEARISH":
                            est_prob_up -= 0.05
                            reason_parts.append("h1_dampen_long_15m")
                        elif market_allowed_side == "SHORT" and mtt.h1_trend == "BULLISH":
                            est_prob_up += 0.05
                            reason_parts.append("h1_dampen_short_15m")
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

            if rsi_soft_delta != 0.0:
                est_prob_up += rsi_soft_delta
            est_prob_up = max(0.10, min(0.90, est_prob_up))
            raw_est_prob = est_prob_up
            estimated_prob = self._calibrate_est_prob(
                raw_est_prob,
                action=action,
                direction=direction,
                window_size=_updown_tf,
                side_source=side_source,
                signal_reason=" | ".join(r for r in reason_parts if r),
                htf_bias=primary_htf_bias,
                btc_1h_regime=btc_1h_regime if btc_ta else None,
            )
            edge = estimated_prob - yes_price if action == "BUY_YES" else yes_price - estimated_prob
            if edge <= 0:
                _bump_skip("nonpositive_edge")
                continue

            lane_side, lane_policy = self._resolve_lane_entry_policy(
                window_size=_updown_tf,
                action=action,
                direction=direction,
            )
            entry_policy_meta = entry_policy_to_dict(
                lane_policy,
                strategy_name=self._signal_strategy_name,
                window_size=_updown_tf,
                side=lane_side,
            )
            effective_min_edge = max(
                lane_policy.min_edge,
                float(getattr(self, "hard_min_edge", 0.0) or 0.0),
                lane_policy.hard_min_edge,
            )
            effective_min_edge += follow_penalty_min_edge_add
            if not lane_policy.enabled:
                _bump_skip("lane_disabled")
                continue
            if _eval_left < lane_policy.entry_window_min or _eval_left > lane_policy.entry_window_max:
                _bump_skip("lane_entry_window")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason="lane_entry_window",
                    yes_price=yes_price,
                    est_prob_up=estimated_prob,
                    htf_bias=primary_htf_bias,
                    context={
                        "eval_mins_left": float(_eval_left),
                        "mins_left": float(_mins_left),
                        "entry_window_min": float(lane_policy.entry_window_min),
                        "entry_window_max": float(lane_policy.entry_window_max),
                    },
                    probe_variants=build_range_probe_variants(
                        metric_name="entry_window_mins_left",
                        observed_value=float(_eval_left),
                        baseline_min=float(lane_policy.entry_window_min),
                        baseline_max=float(lane_policy.entry_window_max),
                        relax_steps=[1.0, 2.0, 5.0],
                        tighten_steps=[1.0, 2.0],
                    ),
                    policy_version="lane_entry_window_v1",
                )
                await _observe_structural_reject(
                    market=market,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason="lane_entry_window",
                    yes_price=yes_price,
                    quant_edge=edge,
                    quant_threshold=float(effective_min_edge),
                    htf_bias=primary_htf_bias,
                    context_lines=[
                        f"eval_mins_left={float(_eval_left):.2f}",
                        f"entry_window_min={float(lane_policy.entry_window_min):.2f}",
                        f"entry_window_max={float(lane_policy.entry_window_max):.2f}",
                        f"quant_edge={float(edge):.4f}",
                        f"effective_min_edge={float(effective_min_edge):.4f}",
                    ],
                    metadata={"eval_mins_left": float(_eval_left)},
                )
                continue
            if self._btc_1h_regime_gates.get("enabled", False) and btc_ta:
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
                _blocked, _macro_skip, _macro_threshold = self._macro_leg_blocks_updown_side(
                    market_allowed_side, _lm
                )
                if _blocked:
                    _bump_skip(_macro_skip)
                    if market_allowed_side == "SHORT":
                        _cmp = ">"
                        _label = "short_ceiling"
                    else:
                        _cmp = "<"
                        _label = "long_floor"
                    logger.info(
                        f"  ETH skip '{market.question[:40]}' — "
                        f"macro_leg={_lm:+.4f}% {_cmp} {_label}={_macro_threshold:+.4f} (updown)"
                    )
                    if action == "BUY_NO":
                        self._emit_buy_no_skip(
                            market=market,
                            bankroll=bankroll,
                            payload=self._make_buy_no_skip_payload(
                                market=market,
                                skip_reason=_macro_skip,
                                window_size=_updown_tf,
                                yes_price=yes_price,
                                edge=edge,
                                effective_min_edge=effective_min_edge,
                                rsi=eth.rsi_14,
                                htf_bias=btc_htf_bias,
                                signal_reason=" | ".join(r for r in reason_parts if r),
                                alt_1h_trend=mtt.h1_trend,
                            ),
                            counts=buy_no_skip_counts,
                            last_sample=last_buy_no_skip_sample,
                        )
                    continue

            _late_ok, effective_min_edge, _late_reason = self._apply_late_window_guard(
                mins_left=_eval_left,
                effective_min_edge=effective_min_edge,
                tf=_updown_tf,
            )
            if not _late_ok:
                _bump_skip("late_window_blocked")
                logger.info(
                    f"  ETH skip '{market.question[:40]}' — "
                    f"mins_left={_eval_left:.2f} <= late_window_block_mins={self.late_window_block_mins:.2f}"
                )
                continue
            if _late_reason:
                reason_parts.append(_late_reason)

            effective_min_edge *= get_drift_min_edge_mult("eth_macro", self.full_config)
            effective_min_edge *= get_loosen_min_edge_mult(
                "eth_macro",
                self.full_config,
                window=_updown_tf,
                side=lane_side,
                regime=primary_htf_bias,
            )

            _hold_ts = self._ai_hold_cache.get(market.id, 0)
            _hold_age = time.time() - _hold_ts
            _ai_override_bar = max(lane_policy.ai_override_min_edge, lane_policy.min_edge)
            if _hold_age < self.ai_hold_veto_ttl_sec and edge < _ai_override_bar:
                _bump_skip("ai_hold_veto")
                continue

            if (
                edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and _timing_window_open
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and self.ai_agent.is_available()
                and ai_calls < self.max_ai_calls_per_scan
            ):
                _window = _updown_tf
                if btc_ta:
                    _btc_ai_block = (
                        f"BTC 1H hist={btc_ta.macd_1h.histogram:+.2f} rising={btc_ta.macd_1h.histogram_rising}\n"
                        f"BTC 15m hist={btc_ta.macd_15m.histogram:+.3f} cross={btc_ta.macd_15m.crossover}\n"
                        f"BTC 5m={btc_mom.m5_direction} ({btc_mom.m5_move_pct:+.3f}%)\n"
                    )
                else:
                    _btc_ai_block = (
                        "BTC full analysis unavailable (ETH-only cycle).\n"
                        f"BTC moves from correlation path: 5m={corr.btc_move_5m_pct:+.3f}% "
                        f"15m={corr.btc_move_15m_pct:+.3f}%\n"
                    )
                ai_context = (
                    f"{market.description}\n\n"
                    f"=== ETH BTC-FOLLOW CONTEXT ({_window}) ===\n"
                    f"ETH Price: ${eth_price:,.2f} | YES={yes_price:.3f} | action={action}\n"
                    f"BTC_HTF={btc_htf_bias} | side={market_allowed_side}({side_source}) | Quant edge={edge:.4f} "
                    f"(threshold={effective_min_edge:.4f})\n"
                    f"Minutes left={_mins_left:.1f}\n\n"
                    f"{_btc_ai_block}"
                    f"ETH 15m hist={eth.macd_15m.histogram:+.3f} cross={eth.macd_15m.crossover}\n"
                    f"ETH 5m hist={eth.macd_5m.histogram:+.3f} cross={eth.macd_5m.crossover}\n"
                    f"ETH RSI={eth.rsi_14:.1f} | ETH 1H trend={mtt.h1_trend}\n"
                    f"ETH Chainlink={eth.chainlink_price if eth.chainlink_price is not None else 'n/a'} "
                    f"basis_bps={eth.oracle_basis_bps if eth.oracle_basis_bps is not None else 'n/a'}\n\n"
                    f"=== MARKET ===\n{format_market_metadata(market)}\n\n"
                    "Answer with BUY_YES, BUY_NO, or HOLD."
                )
                ai_lane_id = str(
                    build_lane_metadata(
                        strategy=self._signal_strategy_name,
                        window_size=_window,
                        action=action,
                        direction=("down" if action == "BUY_NO" else "up"),
                        entry_leg=("NO" if action == "BUY_NO" else "YES"),
                        side_source=side_source,
                        ai_used=True,
                        reason="ai_decision",
                        signal_reason="ai_decision",
                        htf_bias=btc_htf_bias,
                        primary_htf_bias=mtt.h1_trend,
                    ).get("lane_id")
                    or ""
                )
                _broker_state, ai_decision = self._resolve_or_enqueue_ai(
                    lane_id=ai_lane_id,
                    market=market,
                    ai_context=ai_context,
                    yes_price=yes_price,
                    edge=edge,
                    confidence=confidence,
                    action=action,
                    quant_threshold=effective_min_edge,
                    raw_est_prob=raw_est_prob,
                    estimated_prob=estimated_prob,
                    require_shadow_portfolio=False,
                    htf_bias=btc_htf_bias,
                )
                if _broker_state == "pending":
                    ai_calls += 1
                    _bump_skip("ai_pending")
                    continue
                if _broker_state == "unavailable":
                    ai_decision = await self._evaluate_trade_decision_with_timeout(
                        market_question=market.question,
                        market_description=ai_context,
                        current_yes_price=yes_price,
                        market_id=market.id,
                        strategy_hint=self._signal_strategy_name,
                        lane_id=ai_lane_id,
                        quant_action=action,
                        quant_edge=edge,
                        quant_confidence=confidence,
                        quant_threshold=effective_min_edge,
                        raw_probability=raw_est_prob,
                        post_calibration_probability=estimated_prob,
                        require_shadow_portfolio=False,
                    )
                ai_calls += 1
                def _log_ai_veto(_reason: str, **extra: Any) -> None:
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=market_allowed_side,
                        action=action,
                        reason=_reason,
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        stage="ai_veto",
                        context={
                            "edge": round(float(edge), 6),
                            "effective_min_edge": round(float(effective_min_edge), 6),
                            **extra,
                        },
                    )
                if ai_decision is None:
                    _bump_skip("ai_decision_timeout")
                    _log_ai_veto("ai_decision_timeout")
                    continue
                ai_used = True
                ai_analysis = ai_decision.direct_analysis
                if not ai_decision.approved:
                    _bump_skip(f"ai_decision_{ai_decision.reason}")
                    _log_ai_veto(f"ai_decision_{ai_decision.reason}", ai_reason=str(ai_decision.reason))
                    if ai_decision.reason in {"direct_ai_hold", "shadow_portfolio_hold"}:
                        self._ai_hold_cache[market.id] = time.time()
                    continue
                if ai_analysis is None:
                    _bump_skip("ai_none")
                    _log_ai_veto("ai_none")
                    continue
                if ai_decision.action == "HOLD":
                    self._ai_hold_cache[market.id] = time.time()
                    _bump_skip("ai_hold")
                    _log_ai_veto("ai_hold")
                    continue
                if not ai_recommendation_supports_action(ai_decision.action, action):
                    _bump_skip("ai_veto")
                    _log_ai_veto("ai_veto", ai_action=str(ai_decision.action))
                    continue
                if ai_decision.confidence < self.ai_confidence_threshold:
                    _bump_skip("ai_low_confidence")
                    _log_ai_veto("ai_low_confidence", ai_confidence=float(ai_decision.confidence))
                    continue
                ai_edge = float(ai_decision.edge or 0.0)
                if ai_edge <= 0:
                    _bump_skip("ai_nonpositive_edge")
                    _log_ai_veto("ai_nonpositive_edge", ai_edge=ai_edge)
                    continue
                edge = max(edge, ai_edge)
                confidence = max(confidence, ai_decision.confidence)
                reason_parts.append(f"ai_decision={ai_decision.source}")
                research_plan = None
                if (
                    research_enabled
                    and research_calls < research_max_calls
                    and ai_analysis.confidence_score >= research_min_conf
                ):
                    research_calls += 1
                    try:
                        research_plan = await self.ai_agent.analyze_research_plan(
                            market_question=market.question,
                            market_description=ai_context,
                            current_yes_price=yes_price,
                            market_id=market.id,
                            strategy_hint=self._signal_strategy_name,
                            lane_id=ai_lane_id,
                            quant_action=action,
                            quant_edge=edge,
                            quant_threshold=effective_min_edge,
                        )
                    except Exception as e:
                        research_plan = None
                        logger.debug(
                            "ETH research narrative failed market=%s: %s", market.id, e
                        )
                    if research_plan is not None:
                        research_plans_logged += 1
                        reason_parts.append(
                            f"research={research_plan.recommendation.value}"
                        )
                if (
                    self.ai_agent.shadow_pipeline_enabled()
                    and shadow_pipeline_calls
                    < self.ai_agent.shadow_pipeline_max_calls_per_scan()
                    and ai_analysis.confidence_score
                    >= self.ai_agent.shadow_pipeline_min_confidence()
                ):
                    shadow_pipeline_calls += 1
                    try:
                        shadow_out = await self.ai_agent.run_shadow_pipeline(
                            market_question=market.question,
                            market_description=ai_context,
                            current_yes_price=yes_price,
                            market_id=market.id,
                            strategy_hint=self._signal_strategy_name,
                            lane_id=ai_lane_id,
                            marginal_recommendation=str(ai_analysis.recommendation),
                            quant_action=action,
                            quant_edge=edge,
                            quant_threshold=effective_min_edge,
                            existing_research=research_plan,
                        )
                    except Exception as e:
                        shadow_out = None
                        logger.debug(
                            "ETH shadow pipeline failed market=%s: %s",
                            market.id,
                            e,
                        )
                    if shadow_out and shadow_out.get("ok"):
                        shadow_pipeline_ok += 1
                        if shadow_out.get("marginal_mismatch"):
                            shadow_marginal_mismatch += 1
                        reason_parts.append(
                            f"shadow_pm={shadow_out.get('portfolio_action', '')}"
                        )
            elif (
                edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and not _timing_window_open
            ):
                _bump_skip("ai_window_closed")
                continue

            _sample("est_prob_up", est_prob_up)
            _sample("edge", edge)
            if edge < effective_min_edge:
                if rsi_soft_penalty > 0 and (edge + rsi_soft_penalty) >= effective_min_edge:
                    _bump_skip("edge_after_penalty_below_threshold")
                _bump_skip("lane_min_edge")
                log_rejected_candidate(
                    strategy=self._signal_strategy_name,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason="lane_min_edge",
                    market=market,
                    yes_price=yes_price,
                    est_prob_up=estimated_prob,
                    htf_bias=primary_htf_bias,
                    context={
                        "edge": round(float(edge), 6),
                        "effective_min_edge": round(float(effective_min_edge), 6),
                        "raw_est_prob": round(float(raw_est_prob), 6),
                        "estimated_prob": round(float(estimated_prob), 6),
                        "confidence": round(float(confidence), 6),
                        "side_source": side_source,
                        "rsi_soft_penalty": round(float(rsi_soft_penalty), 6),
                        **build_market_context(
                            asset_spot=eth.current_price,
                            btc_spot=corr.btc_price,
                            rsi_14=eth.rsi_14,
                            atr_14=eth.atr_14,
                        ),
                    },
                    probe_variants=build_threshold_probe_variants(
                        metric_name="min_edge",
                        observed_value=float(edge),
                        baseline_threshold=float(effective_min_edge),
                    ),
                    policy_version="lane_min_edge_v1",
                    stage="lane_min_edge",
                )
                if action == "BUY_NO":
                    _skip_reason = (
                        "edge_after_penalty_below_threshold"
                        if rsi_soft_penalty > 0 and (edge + rsi_soft_penalty) >= effective_min_edge
                        else "lane_min_edge"
                    )
                    self._emit_buy_no_skip(
                        market=market,
                        bankroll=bankroll,
                        payload=self._make_buy_no_skip_payload(
                            market=market,
                            skip_reason=_skip_reason,
                            window_size=_updown_tf,
                            yes_price=yes_price,
                            edge=edge,
                            effective_min_edge=effective_min_edge,
                            rsi=eth.rsi_14,
                            htf_bias=btc_htf_bias,
                            signal_reason=" | ".join(r for r in reason_parts if r),
                            alt_1h_trend=mtt.h1_trend,
                        ),
                        counts=buy_no_skip_counts,
                        last_sample=last_buy_no_skip_sample,
                    )
                continue

            # Centered-price gate: near 50/50 entries need a higher edge bar.
            # 2026-05-22: BTC catalyst requirement (center_price_requires_catalyst)
            # REMOVED — was gating ETH admission on BTC spike/lag. The higher
            # min-edge bar for centered prices remains (alt-native edge check).
            if self.center_price_band > 0:
                _is_centered = abs(yes_price - 0.50) <= self.center_price_band
                if _is_centered:
                    _center_min_edge = max(effective_min_edge, self.min_edge_when_centered)
                    if edge < _center_min_edge:
                        _bump_skip("centered_price_edge_below_min")
                        continue

            if action == "BUY_YES":
                _entry_price_bad = (
                    yes_price < lane_policy.entry_price_min
                    or yes_price > lane_policy.entry_price_max
                )
            else:
                # BUY_NO: allow rich-YES / cheap-NO setups above max YES;
                # still reject overly bearish YES where NO is already expensive.
                _entry_price_bad = yes_price < lane_policy.entry_price_min
            if _entry_price_bad:
                _bump_skip("lane_price_band")
                if action == "BUY_NO":
                    self._emit_buy_no_skip(
                        market=market,
                        bankroll=bankroll,
                        payload=self._make_buy_no_skip_payload(
                            market=market,
                            skip_reason="lane_price_band",
                            window_size=_updown_tf,
                            yes_price=yes_price,
                            edge=edge,
                            effective_min_edge=effective_min_edge,
                            rsi=eth.rsi_14,
                            htf_bias=btc_htf_bias,
                            signal_reason=" | ".join(r for r in reason_parts if r),
                            alt_1h_trend=mtt.h1_trend,
                        ),
                        counts=buy_no_skip_counts,
                        last_sample=last_buy_no_skip_sample,
                    )
                continue

            max_edge_updown = float(self.config.get("max_edge_updown", 0.15))
            sizing_edge = edge
            if max_edge_updown > 0 and edge > max_edge_updown:
                sizing_edge = max_edge_updown
                reason_parts.append(f"size_edge_cap={max_edge_updown:.3f}")
                logger.info(
                    "  ETH sizing cap '%s...' edge=%.4f -> size_edge=%.4f (max=%.4f)",
                    market.question[:40],
                    edge,
                    sizing_edge,
                    max_edge_updown,
                )

            if not self.kelly_sizer:
                _bump_skip("kelly_unavailable")
                logger.error("ETH strategy: KellySizer unavailable — skipping entry sizing")
                continue
            raw_size = self.kelly_sizer.size_from_edge(
                self._signal_strategy_name, bankroll, sizing_edge
            )
            if self._btc_1h_regime_gates.get("enabled", False) and btc_ta:
                raw_size *= self._regime_size_mult(btc_1h_regime)
            if getattr(corr, "degraded", False) and not self.skip_on_degraded_correlation:
                raw_size *= self.degraded_correlation_size_multiplier
            if lane_policy.size_multiplier > 0:
                raw_size *= lane_policy.size_multiplier
            final_size = self.exposure_manager.scale_size(raw_size)
            if final_size < 0.5:
                _bump_skip("lane_size_too_small")
                if action == "BUY_NO":
                    self._emit_buy_no_skip(
                        market=market,
                        bankroll=bankroll,
                        payload=self._make_buy_no_skip_payload(
                            market=market,
                            skip_reason="lane_size_too_small",
                            window_size=_updown_tf,
                            yes_price=yes_price,
                            edge=edge,
                            effective_min_edge=effective_min_edge,
                            rsi=eth.rsi_14,
                            htf_bias=btc_htf_bias,
                            signal_reason=" | ".join(r for r in reason_parts if r),
                            alt_1h_trend=mtt.h1_trend,
                        ),
                        counts=buy_no_skip_counts,
                        last_sample=last_buy_no_skip_sample,
                    )
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
            if lane_policy.size_multiplier > 0 and lane_policy.size_multiplier < 0.999:
                reason_parts.append(f"lane_size={lane_policy.size_multiplier:.2f}x")
            _btc_spot_for_signal = (
                float(btc_ta.current_price)
                if btc_ta
                else float(getattr(corr, "btc_price", 0.0) or 0.0)
            )
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
                btc_current=round(_btc_spot_for_signal, 2),
                lag_magnitude=None,
                ai_used=ai_used,
                reason=" | ".join(reason_parts),
                strategy_name=self._signal_strategy_name,
                alt_asset_code="eth",
                htf_bias=primary_htf_bias,
                btc_1h_regime=btc_1h_regime,
                entry_policy=entry_policy_meta,
                window_size=_updown_tf,
                hour_utc=datetime.now(timezone.utc).hour,
                est_prob=round(estimated_prob, 4),
                raw_est_prob=round(raw_est_prob, 4),
                rsi=round(eth.rsi_14, 1),
                corr_1h=round(corr.correlation_1h, 4),
                side_source=side_source,
                convergence_score=(
                    round(float(entry_convergence_score), 4)
                    if "entry_convergence_score" in locals() and entry_convergence_score is not None
                    else None
                ),
                entry_volatility=round(float(getattr(conditions, "volatility", 0.0) or 0.0), 6),
                oracle_basis_bps=(
                    round(float(eth.oracle_basis_bps), 2)
                    if eth.oracle_basis_bps is not None
                    else None
                ),
                indicator_snapshot={
                    "composite_score": (
                        round(float(entry_composite_score), 4)
                        if "entry_composite_score" in locals() and entry_composite_score is not None
                        else None
                    ),
                    "convergence_score": (
                        round(float(entry_convergence_score), 4)
                        if "entry_convergence_score" in locals() and entry_convergence_score is not None
                        else None
                    ),
                    "entry_volatility": round(float(getattr(conditions, "volatility", 0.0) or 0.0), 6),
                    "btc_4h_bias": btc_htf_details["bias"] if btc_htf_details else None,
                    "btc_4h_raw_bias": btc_htf_details["raw_bias"] if btc_htf_details else None,
                    "btc_4h_sabre_vote": btc_htf_details["sabre_vote"] if btc_htf_details else None,
                    "btc_4h_price_vs_ma_vote": (
                        btc_htf_details["price_vs_ma_vote"] if btc_htf_details else None
                    ),
                    "btc_4h_macd_vote": btc_htf_details["macd_vote"] if btc_htf_details else None,
                    "btc_4h_macd_state": btc_htf_details["macd_state"] if btc_htf_details else None,
                    "btc_4h_histogram": (
                        round(float(btc_htf_details["macd_4h_histogram"]), 4)
                        if btc_htf_details
                        else None
                    ),
                    "btc_4h_histogram_rising": (
                        bool(btc_htf_details["macd_4h_histogram_rising"])
                        if btc_htf_details
                        else None
                    ),
                    "btc_4h_hist_conviction_ok": (
                        bool(btc_htf_details["hist_conviction_ok"]) if btc_htf_details else None
                    ),
                    "alt_1h_histogram": round(float(eth.macd_1h.histogram or 0.0), 4),
                    "alt_1h_histogram_rising": bool(eth.macd_1h.histogram_rising),
                    "alt_15m_histogram": round(float(eth.macd_15m.histogram or 0.0), 4),
                    "alt_15m_histogram_rising": bool(eth.macd_15m.histogram_rising),
                    "alt_5m_histogram": round(float(eth.macd_5m.histogram or 0.0), 4),
                    "alt_5m_histogram_rising": bool(eth.macd_5m.histogram_rising),
                    "btc_1h_histogram": round(float(btc_ta.macd_1h.histogram or 0.0), 4)
                    if btc_ta
                    else None,
                    "btc_1h_histogram_rising": bool(btc_ta.macd_1h.histogram_rising)
                    if btc_ta
                    else None,
                },
            )
            signals.append(signal)

        _phase_t_postloop = time.perf_counter()
        if observer_tasks:
            await asyncio.wait(observer_tasks, timeout=0.01)

        _phase_setup_ms = int((_phase_t_preloop - _phase_t0) * 1000)
        _phase_loop_ms = int((_phase_t_postloop - _phase_t_preloop) * 1000)
        _phase_n = max(len(eth_markets), 1)
        logger.info(
            "ETH Macro PHASE_TIMING setup=%dms loop=%dms markets=%d per_market_avg=%dms",
            _phase_setup_ms,
            _phase_loop_ms,
            len(eth_markets),
            int(_phase_loop_ms / _phase_n),
        )

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
            "btc_htf_vote_details": dict(btc_htf_details) if btc_htf_details else None,
            "allowed_side": allowed_side,
            "direction_source": self.direction_source,
            "action_counts": dict(sorted(action_counts.items())),
            "side_source_counts": side_source_counts,
            "alt_1h_trend": mtt.h1_trend,
            "enforce_alt_1h_alignment": self.enforce_alt_1h_alignment,
            "ai_calls": ai_calls,
            "research_calls": research_calls,
            "research_plans_logged": research_plans_logged,
            "shadow_pipeline_calls": shadow_pipeline_calls,
            "shadow_pipeline_ok": shadow_pipeline_ok,
            "shadow_observer_calls": shadow_observer_calls,
            "shadow_observer_ok": shadow_observer_ok,
            "shadow_marginal_mismatch": shadow_marginal_mismatch,
            "buy_no_skip_counts": dict(sorted(buy_no_skip_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]),
            "last_buy_no_skip_sample": dict(last_buy_no_skip_sample),
            "top_skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: kv[1], reverse=True)[:8]),
            "gate_distributions": gate_distributions,
        }
        return signals
