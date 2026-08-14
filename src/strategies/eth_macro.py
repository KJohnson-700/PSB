"""
ETH Macro Strategy.

ETH is its own alt leg: ETH spot/HTF/oracle data drives primary direction.
BTC is secondary context/follow-quality input only.
"""
import asyncio
import logging
import time
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.analysis.ai_agent import AIAgent
from src.analysis.btc_price_service import CandleMomentum, MACDResult, TechnicalAnalysis
from src.analysis.math_utils import PositionSizer
from src.strategies._scan_timeout import analysis_with_timeout
from src.strategies.fee_util import fee_aware_edge_hurdle
from src.analysis.sol_btc_service import SOLBTCService
from src.analysis.window_watch import log_window_reject
from src.execution.exposure_manager import ExposureManager, ExposureTier
from src.market.scanner import Market, is_tradably_priced, resolved_updown_window_minutes, updown_timeframe_label
from src.strategies.strategy_config import resolve_enabled_flag, resolve_side_policy
from src.analysis.btc_1h_regime import regime_price
from src.analysis.lane_entry_policy import entry_policy_to_dict
from src.analysis.updown_composite_score import apply_fresh_cross_override
from src.analysis.buy_yes_lane_repair import resolve_buy_yes_lane_repair
from src.analysis.lane_identity import build_lane_metadata
from src.strategies.sol_macro import (
    SolMacroSignal,
    SolMacroStrategy,
    build_alt_resolver_metadata,
    macd_bearish_momentum_ok,
    macd_bullish_momentum_ok,
    side_from_est_prob_up,
    side_from_momentum_bias,
)
from src.strategies.strategy_ai_context import (
    ai_recommendation_supports_action,
    format_market_metadata,
)
from src.analysis.lane_tape_adapter import get_tape_admission_delta
from src.analysis.tape_map import (
    snapshot_and_log as _tape_map_snapshot,
    latest_tape_state as _latest_tape_state,
    log_side_veto_shadow as _log_side_veto_shadow,
)
from src.analysis.tape_freshness import compute_freshness_penalty
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
from src.analysis.window_delta import evaluate_window_delta
from src.analysis import no_signal_gate as _no_signal_gate
from src.analysis import asset_regime as _asset_regime

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


@dataclass(frozen=True)
class ETHDirectionDecision:
    action: str
    direction: str
    effective_side: str
    side_source: str
    conflict_type: str
    resolver_path: str
    htf_side: Optional[str]
    quant_side: Optional[str] = None
    momentum_side: Optional[str] = None


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
        self._log_tf_config_overrides()

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
        self.min_edge_5m_ai_override = float(
            self._tf_cfg("5m", "ai_override_min_edge", 0.10)
        )
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

    def _marginal_ev_admit_ok(self, window, action) -> bool:
        """2026-06-09: per-(window,side) EV allowlist mirror of the sol-family fix.
        Admits marginals on quant terms for ghost-validated +EV cells instead of the
        anti-selective AI tiebreaker. Config: `marginal_ev_admit_lanes` = list of
        "<window>:<SIDE>" (LONG|SHORT). ETH currently ships with no entries (no +EV
        marginal lanes in the ghost) so this is a no-op until its own validation."""
        try:
            if self.ai_agent.decision_layer_enabled():
                return False
            lanes = self.config.get("marginal_ev_admit_lanes") or []
            side = "LONG" if str(action).upper() == "BUY_YES" else "SHORT"
            key = f"{str(window).strip()}:{side}".upper()
            return key in {str(x).strip().upper() for x in lanes}
        except Exception:
            return False

    def _admit_marginal_quant_short(self, edge, action, timing_open, window=None) -> bool:
        """When the AI decision layer is OFF, admit sub-threshold marginal candidates
        on quant terms instead of dying on the AI tiebreaker (no-op when the layer is
        enabled) or the lane_min_edge gate. Default OFF, timing-open, edge above the
        marginal floor. Self-disables when the decision layer is re-enabled.

        Two admit paths (both require timing-open + edge >= marginal floor):
          1. EV allowlist (`marginal_ev_admit_lanes`, 2026-06-09) — ghost-validated
             +EV (window,side) cells; see _marginal_ev_admit_ok.
          2. Legacy scope (`admit_marginal_on_quant_sides`: SHORT|LONG|BOTH; default
             SHORT=BUY_NO-only). ETH left at default pending its own ghost validation."""
        try:
            if not bool(timing_open):
                return False
            if float(edge) < float(self.config.get("ai_updown_marginal_min_edge", 0.03)):
                return False
            # 2026-07-03 AI-SHADOW FIX (ETH port of sol_macro fix): yield the marginal
            # band to the AI tiebreaker when up/down AI is ON+available.
            if (
                bool(self.config.get("use_ai", True))
                and bool(self.config.get("use_ai_updown", True))
                and self.ai_agent.is_available()
            ):
                return False
            if window is not None and self._marginal_ev_admit_ok(window, action):
                return True
            if not bool(self.config.get("admit_marginal_on_quant_when_ai_disabled", False)):
                return False
            if self.ai_agent.decision_layer_enabled():
                return False
            scope = str(self.config.get("admit_marginal_on_quant_sides", "SHORT")).upper()
            want = {"SHORT": "BUY_NO", "LONG": "BUY_YES"}
            if scope != "BOTH" and str(action).upper() != want.get(scope, "BUY_NO"):
                return False
            return True
        except Exception:
            return False

    def _resolve_eth_direction(
        self,
        *,
        market_allowed_side: str,
        side_source: str,
        raw_est_prob: Optional[float] = None,
        momentum_bias: Optional[str] = None,
    ) -> ETHDirectionDecision:
        action = "BUY_YES" if market_allowed_side == "LONG" else "BUY_NO"
        direction = "UP" if market_allowed_side == "LONG" else "DOWN"
        resolver_meta = build_alt_resolver_metadata(
            side_source=side_source,
            htf_side=market_allowed_side,
            quant_side=side_from_est_prob_up(raw_est_prob),
            momentum_side=side_from_momentum_bias(momentum_bias),
        )
        return ETHDirectionDecision(
            action=action,
            direction=direction,
            effective_side=market_allowed_side,
            side_source=side_source,
            conflict_type=str(resolver_meta.get("conflict_type") or "aligned"),
            resolver_path=str(resolver_meta.get("resolver_path") or side_source),
            htf_side=resolver_meta.get("htf_side"),
            quant_side=resolver_meta.get("quant_side"),
            momentum_side=resolver_meta.get("momentum_side"),
        )

    def _eth_direction_guard_reason(
        self,
        *,
        window_size: str,
        decision: ETHDirectionDecision,
        yes_price: float,
        btc_htf_bias: Optional[str],
        btc_1h_regime: Optional[str],
        alt_h1_trend: Optional[str],
        rsi_14: float,
    ) -> Optional[str]:
        regime = str(btc_1h_regime or "").upper()
        btc_bias = str(btc_htf_bias or "").upper()
        alt_bias = str(alt_h1_trend or "").upper()

        # NOTE: neutral_fallback is now sat out at the source in the shared
        # _resolve_alt_bias_for_tf (alt_neutral_fallback_sit_out, inherited from
        # SolMacroStrategy), covering both BUY_YES and BUY_NO before this guard.

        # BTC→ETH decoupling (2026-05-29): same issue as the SOL 15m guard — a
        # BTC-1h-regime gate on an ETH short, violating "alts not decided by BTC".
        # Opt-in only via `eth_5m_bull_regime_short_block` (default OFF). See the
        # parallel note in sol_macro._sol_signal_guard_reason.
        if (
            self._btc_trade_inputs_enabled()
            and
            window_size == "5m"
            and decision.action == "BUY_NO"
            and regime == "BULL"
            and bool(self.config.get("eth_5m_bull_regime_short_block", False))
        ):
            max_yes = float(self.config.get("eth_5m_buy_no_max_yes_price_bull_1h", 0.68))
            if yes_price >= max_yes:
                return "eth_5m_bull_regime_expensive_short"

        if (
            self._btc_trade_inputs_enabled()
            and
            window_size == "15m"
            and decision.action == "BUY_YES"
            and alt_bias == "NEUTRAL"
            and btc_bias == "BEARISH"
        ):
            max_rsi = float(
                self.config.get(
                    "eth_15m_buy_yes_max_rsi_when_btc_bearish_alt_neutral",
                    68.0,
                )
            )
            if rsi_14 >= max_rsi:
                return "eth_15m_overbought_long_vs_btc"

        # 2026-07-30 ETH SETUP — 15m BUY_YES is a COUNTER-TREND reversal long, not a
        # bull-follow (operator "create the eth setup"). Across the full paper history
        # (n=266) ETH 15m BUY_YES splits cleanly by ETH's OWN 1h tape: BEARISH +$107.71
        # (53% WR, n=62) vs NEUTRAL -$42.98 and BULLISH -$47.16 (bull-chasing bleed).
        # RSI does NOT separate (winners span RSI 30-79) — tape does. Gate the long to
        # the bearish-tape reversal only; ETH-NATIVE (own 1h, no BTC dependency, per
        # "alts decided by alt-native indicators"). Side-isolated (BUY_YES), 15m only
        # (5m stays a collection lane). Opt-in: eth_15m_buy_yes_bearish_tape_only_enabled.
        if (
            window_size == "15m"
            and decision.action == "BUY_YES"
            and bool(self.config.get("eth_15m_buy_yes_bearish_tape_only_enabled", False))
        ):
            if str(alt_h1_trend or "").upper() != "BEARISH":
                return "eth_15m_long_not_bearish_tape"

        return None

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
            # 2026-08-14 SIDE-SYMMETRY FIX (Codex GO-WITH-CHANGES, Option B).
            # WAS: `macd_5m.histogram > 0 and macd_5m.histogram_rising`.
            # That was NOT the mirror of the SHORT clause below. `histogram_rising` is
            # `curr_hist > prev_hist` (STRICT — sol_btc_service.py:744), so a FLAT
            # histogram is False. SHORT's 0.04 asks only for `not rising`, which INCLUDES
            # flat; LONG's asked for strictly rising. The true mirror of "positive and
            # rising" is "negative and falling", not "negative and not-rising".
            #
            # Effect on the fall-through (score 0.0 => below eth_follow_5m_min_adj 0.02
            # => rejected as eth_5m_weak_confirm):
            #   LONG  fell through on hist > 0 AND not rising  -> SUPPORTING evidence discarded
            #   SHORT falls through on hist < 0 AND rising     -> DETERIORATING evidence discarded
            # So the LONG side was throwing away good evidence and the SHORT side was not.
            #
            # MEASURED 2026-08-14 (live --paper, one day, eth_macro 5m):
            #   eth_5m_weak_confirm rejections   LONG 509 (19.1%)  SHORT 201 (7.7%)  = 2.5x
            #     of those, scored exactly 0.0   LONG 29.1%        SHORT 7.0%
            #     deduped by market_id           LONG 36 markets   SHORT 7 markets
            #   Candidate pool by quote:  yes>0.50 (LONG) 58.9%  /  yes<0.50 (SHORT) 38.1%
            #   Actual entries, last 20:  BUY_NO 85%  /  BUY_YES 15%
            # The market offered LONG and the book went 85% SHORT. On a flat tape (ETH
            # -0.116% over 26 bars) the histogram is flat constantly, which is exactly the
            # state that passed SHORT and rejected LONG — so every eth 5m position became
            # the same correlated short and they lost as a clump.
            #
            # ⚠ THIS IS NOT ADMISSION-ONLY. eth_5m_adj also feeds est_prob_up (line ~2026),
            # `confidence` (~2041, via abs()), and through those the edge and Kelly sizing.
            # Flipping this cohort 0.0 -> 0.04 therefore also adds +0.04 to est_prob_up and
            # lifts confidence 0.55 -> 0.58 on these LONGs. That is the intended direction
            # (the side was under-scored), but it is a real scope note, not a no-op.
            #
            # ⛔ REJECTED ALTERNATIVE (Codex NO on Option A): also dropping `not rising`
            # from the SHORT clause. That would admit `hist < 0 AND rising` — a RECOVERING
            # downtrend — on a side that already carries b=0.77 / 56.4% breakeven, and via
            # the est_prob path it would make NO look stronger and size it larger.
            # Fixing the symmetry by TIGHTENING short is also off the table: this project
            # is in a calibration phase where frequency is priority #1.
            elif macd_5m.histogram > 0:
                score = 0.04
                reasons.append("ETH5m green")
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

    async def scan_and_analyze(self, markets: List[Market], bankroll: float) -> List[SolMacroSignal]:
        _phase_t0 = time.perf_counter()
        if not self.enabled:
            self._record_eth_abort("strategy_disabled")
            return []

        _eth_candidates = [m for m in markets if self._is_solana_market(m) and self._is_updown_market(m)]
        # Fail-closed price guard (2026-06-22): never trade an UNPRICED market (0.5-default
        # phantom edge). Deferred, not dropped — re-prices next cycle, entered at a real quote.
        eth_markets = [m for m in _eth_candidates if is_tradably_priced(m)]
        _unpriced = len(_eth_candidates) - len(eth_markets)
        if _unpriced:
            logger.info(
                "ETH Macro strategy: skipped %d UNPRICED market(s) (0.5-default, deferred to next priced cycle)",
                _unpriced,
            )
        if not eth_markets:
            logger.info("ETH Macro strategy: 0 ETH updown markets found")
            self._record_eth_abort("no_eth_markets")
            return []

        # Off-loop with a hard timeout so a slow data fetch can't wedge the cycle.
        _scan_to = float(self.config.get("scan_analysis_timeout_sec", 15.0) or 15.0)
        eth_ta = await analysis_with_timeout(
            self.sol_service.get_full_analysis, lane="eth_macro", timeout_sec=_scan_to
        )
        # BTC analysis is diagnostic-only here. Prefer the once-per-cycle value
        # injected by main.py; only fetch our own if none was injected (e.g. tests).
        if self._btc_ta_inject_set:
            btc_ta = self._injected_btc_ta
            self._btc_ta_inject_set = False  # consume; main re-sets each cycle
        else:
            btc_ta = await analysis_with_timeout(
                self.btc_service.get_full_analysis, lane="eth_macro:btc", timeout_sec=_scan_to
            )
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
            logger.debug("ETH Macro: external market context unavailable; continuing on ETH leg")

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
                pass
            else:
                pass

        alt_1h_trend = self._get_1h_bias(eth_ta)
        alt_15m_trend = self._get_15m_bias(eth_ta)
        alt_5m_trend = self._get_5m_bias(eth_ta)
        logger.info(
            "ETH Macro bias stack: 1h=%s 15m=%s 5m=%s | BTC HTF=%s",
            alt_1h_trend,
            alt_15m_trend,
            alt_5m_trend,
            btc_htf_bias,
        )

        eth = eth_ta.sol
        eth_price = eth.current_price

        # 2026-08-02 TAPE MAP (Phase 1, SHADOW — operator directive). Log ETH's mechanical tape
        # state once per cycle (deduped) to data/calibration/tape_map.jsonl. OBSERVE-ONLY; no
        # trade depends on it. Fail-silent.
        _tape_map_snapshot(
            "eth_macro",
            current_price=getattr(eth, "current_price", None),
            atr_14=getattr(eth, "atr_14", None),
            trend_direction=getattr(eth, "trend_direction", None),
            trend_strength=getattr(eth, "trend_strength", None),
            macd_5m=getattr(eth, "macd_5m", None),
            macd_15m=getattr(eth, "macd_15m", None),
            macd_1h=getattr(eth, "macd_1h", None),
            rsi_14=getattr(eth, "rsi_14", None),
            ema_9=getattr(eth, "ema_9", None),
            ema_21=getattr(eth, "ema_21", None),
            ema_50=getattr(eth, "ema_50", None),
        )

        # 2026-08-02 SIDE-VETO SHADOW (observe-only, operator GO; high-confidence lane). Diagnosed
        # bleed: eth 5m LONG (BUY_YES) fires into DOWN tape = wrong-direction (its short side wins).
        # Log what a tape-adaptive veto WOULD do each cycle (tape==DOWN AND realized up-adapter not
        # loosening) — DOES NOT block. Measured offline before ever activating. R4.
        try:
            _sv_ts = _latest_tape_state("eth_macro")
            _sv_dir = str((_sv_ts or {}).get("direction") or "")
            _sv_adm = float(get_tape_admission_delta("eth_macro", "5m", "up") or 0.0)
            _log_side_veto_shadow(
                strategy="eth_macro", window="5m", side="up", target_action="BUY_YES",
                tape_dir=(_sv_dir or None), tape_strength=(_sv_ts or {}).get("strength"),
                tape_conf=(_sv_ts or {}).get("confidence"),  # 2026-08-12: was NEVER logged -> tape_conf null in 22k rows
                adm=round(_sv_adm, 4), would_veto=bool(_sv_dir == "DOWN" and _sv_adm >= 0.0),
            )
        except Exception:
            pass
        btc_mom = btc_ta.candle_momentum if btc_ta else CandleMomentum()
        mtt = eth_ta.multi_tf

        if btc_ta:
            logger.info(
                f"ETH ${eth_price:,.2f} | ETH15m={eth.macd_15m.histogram:+.3f} {eth.macd_15m.crossover} "
                f"| ETH5m={eth.macd_5m.histogram:+.3f} {eth.macd_5m.crossover} | RSI={eth.rsi_14:.0f}"
            )
        else:
            logger.info(
                f"ETH ${eth_price:,.2f} | "
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
                    macd_hist_5m=getattr(getattr(eth, "macd_5m", None), "histogram", None),
                    macd_hist_15m=getattr(getattr(eth, "macd_15m", None), "histogram", None),
                    macd_hist_1h=getattr(getattr(eth, "macd_1h", None), "histogram", None),
                    rsi_5m=getattr(getattr(eth, "tf_5m", None), "rsi_14", None),
                    rsi_15m=getattr(getattr(eth, "tf_15m", None), "rsi_14", None),
                    rsi_1h=getattr(getattr(eth, "tf_1h", None), "rsi_14", None),
                )
            )
            # Always stamp eval_mins_left so post-hoc analysis can distinguish
            # in-window vs pre-window on every rejection reason. Mirrors sol_macro
            # fix for the ghost-log blind spot found 2026-05-22.
            if "eval_mins_left" not in merged_context and "mins_left" not in merged_context:
                try:
                    _end = getattr(market, "end_date", None)
                    if _end is not None:
                        if _end.tzinfo is None:
                            _end = _end.replace(tzinfo=timezone.utc)
                        merged_context["eval_mins_left"] = float(
                            max(0.0, (_end - datetime.now(timezone.utc)).total_seconds() / 60.0)
                        )
                except Exception:
                    pass
            # Model-independent window-delta on EVERY reject (forward-validation of
            # the confirmation gate). ETH-native price only; fails open. Don't
            # overwrite values the gate block already stamped.
            if "window_delta_prob" not in merged_context and window in ("5m", "15m", "1h"):
                try:
                    _wd_ml = merged_context.get("eval_mins_left")
                    if _wd_ml is None:
                        _wd_ml = merged_context.get("mins_left", 0.0)
                    _wd = evaluate_window_delta(eth, window, float(_wd_ml or 0.0))
                    if _wd is not None:
                        merged_context["window_delta_pct"] = round(_wd[0], 6)
                        merged_context["window_delta_prob"] = round(_wd[1], 6)
                except Exception:
                    pass
            # Microstructure features (default-off enrichment; None when not enriched)
            try:
                _obi = getattr(market, "ob_imbalance", None)
                _tfr = getattr(market, "trade_flow_ratio", None)
                if _obi is not None:
                    merged_context["ob_imbalance"] = round(float(_obi), 6)
                if _tfr is not None:
                    merged_context["trade_flow_ratio"] = round(float(_tfr), 6)
            except Exception:
                pass
            if "btc_1h_regime" in locals():
                merged_context["btc_1h_regime"] = btc_1h_regime
            # Forward the resolved lane so rejects bucket by side instead of the
            # catch-all pre_resolver_reject. The resolver runs at the top of each
            # eth candidate iteration (side_source set before any skip), so this is
            # known for every skip here. Mirrors the sol_macro fix. Fail open.
            _reject_side_source = None
            _reject_resolver_path = None
            try:
                _reject_side_source = side_source  # closure var, set per-candidate
            except NameError:
                _reject_side_source = None
            try:
                _reject_resolver_path = getattr(resolution, "resolver_path", None)
            except NameError:
                _reject_resolver_path = None
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
                side_source=_reject_side_source,
                resolver_path=_reject_resolver_path,
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
            rsi_min_edge_add = 0.0  # 2026-07-31 Phase-1: consolidated RSI pocket-floor add
            _updown_tf = updown_timeframe_label(resolved_updown_window_minutes(market))
            is_updown = self._is_updown_market(market)  # 2026-07-15 FIX: was referenced at ~2819/2845 but never assigned in eth_macro (latent NameError, only reachable once pocket_only=false let candidates flow to sizing). Mirrors sol_macro:3327.
            is_5m = _updown_tf == "5m"
            is_1h = _updown_tf == "1h"
            yes_price = market.yes_price
            resolution = self._resolve_alt_bias_for_tf(eth_ta, _updown_tf)
            market_allowed_side = resolution.allowed_side
            side_source = resolution.side_source
            # 2026-07-11 ETH 15m FADE PORT (sol parity): the inherited
            # _resolve_alt_bias_for_tf already inverted the side + retagged
            # eth_<tf>_fade_native when fade_regime + fade_regime_windows enable
            # this window (align-gate suppresses on fully-aligned trends). This
            # flag gates the loop's late flips (fresh_cross / wd_flip / 5m
            # no->yes) so a faded side is never double-flipped, and gates the
            # 07-06 fade-shadow logger (live fade supersedes the counterfactual).
            # 2026-08-10 Codex-fix (port of sol_macro): treat rsi_fade as fade-active too, else
            # late flip paths (fresh_cross_override / window_delta_flip / posterior) silently UNDO
            # the deliberate RSI fade. Both are intentional side inversions that must stick.
            _fade_active = any(t in (side_source or "") for t in ("fade_native", "rsi_fade"))
            # 2026-08-13 SIDE POLICY (port of sol_macro — eth duplicates the scan loop, so
            # this is a SEPARATE wiring, per the repo rule). The resolver picks direction
            # worse than a coin flip (48.24% on 171,380 settled ghosts in the 0.45-0.55
            # band, EV -0.0525/$1 vs the market favorite's -0.0215); eth 15m specifically
            # is 47.3% vs the favorite's 51.2%. On non-exempt lanes the MARKET picks the
            # side. Applied EARLY and made STICKY — applying it late would admit on one
            # side's edge and execute the other. See strategy_config.resolve_side_policy.
            _sp = resolve_side_policy(
                self.full_config,
                lane_key=f"{self._signal_strategy_name or 'eth_macro'}|{_updown_tf}",
                yes_price=market.yes_price,
                resolver_side=market_allowed_side,
            )
            _side_policy_active = bool(_sp.get("active"))
            _side_policy_flat_edge = float(_sp.get("flat_edge") or 0.0)
            if _side_policy_active and market_allowed_side is not None:
                if _sp.get("side") is None:
                    _bump_skip(_sp.get("skip") or "favorite_policy_skip")
                    _log_skip_reject(
                        market=market, window=_updown_tf, side=market_allowed_side,
                        action=("BUY_YES" if market_allowed_side == "LONG" else "BUY_NO"),
                        reason=_sp.get("skip") or "favorite_policy_skip",
                        yes_price=market.yes_price,
                        htf_bias=resolution.primary_htf_bias,
                        context=dict(_sp.get("meta") or {}),
                    )
                    continue
                market_allowed_side = _sp["side"]
                side_source = (side_source or "") + "+" + str(_sp.get("tag") or "")
            # STICKY: the late flips (fresh_cross_override / window_delta_flip / 5m
            # NO->YES) must key off THIS, not _fade_active alone, or they silently undo
            # the policy's side — the same bug the 08-10 Codex fix caught for rsi_fade.
            _sticky_side_active = _fade_active or _side_policy_active
            if market_allowed_side is None:
                # No usable bias = the lane has no side, so there is no rejected
                # *candidate* to counterfactually score (action would be NONE).
                # Match the SolMacro parent: count the sit-out but do NOT spend
                # shadow-observer budget on it — that budget is shared per-scan and
                # should go to real structural rejects (liquidity / oracle /
                # momentum) where a concrete side was rejected.
                _bump_skip("neutral_bias")
                logger.info(
                    "ETH Macro skip '%s' — no usable %s bias (1h=%s 15m=%s 5m=%s)",
                    market.question[:40],
                    _updown_tf,
                    alt_1h_trend,
                    alt_15m_trend,
                    alt_5m_trend,
                )
                # 2026-08-12 OVERRIDES the "don't spend shadow budget on this" note above. That
                # decision is exactly WHY this gate has no evidence: neutral_bias is 41.6% of ALL
                # strategy evaluations bot-wide (eth 46.4% of its own) and had never written one
                # ghost row, so it was invisible to every frequency audit run this session. The
                # budget argument assumed "no concrete side was rejected" makes it unscoreable —
                # but logging BOTH sides makes it perfectly scoreable, and answers the only
                # question worth asking: when the 2-of-3 bias vote deadlocks, would either
                # direction have won? GRADUATION IS BINDING (docs/PSB_RESEARCH_ROADMAP.md OPEN
                # CONFIRMATIONS): n>=200 settled per side, then loosen the beating side or CLOSE
                # the ghost out as answered. It does not sit forever.
                try:
                    for _nb_act, _nb_side in (("BUY_YES", "LONG"), ("BUY_NO", "SHORT")):
                        _log_skip_reject(
                            market=market,
                            window=_updown_tf,
                            side=_nb_side,
                            action=_nb_act,
                            reason="neutral_bias",
                            yes_price=market.yes_price,
                            htf_bias=alt_1h_trend,
                            context={"gate": "neutral_bias_both_sides_probe",
                                     "macro_1h": alt_1h_trend, "bias_15m": alt_15m_trend,
                                     "bias_5m": alt_5m_trend,
                                     "graduation": "n>=200/side then loosen-or-close"},
                        )
                except Exception:
                    pass
                continue
            direction_decision = self._resolve_eth_direction(
                market_allowed_side=market_allowed_side,
                side_source=side_source,
                momentum_bias=getattr(mtt, "m5_trend", None),
            )
            action = direction_decision.action
            primary_htf_bias = resolution.primary_htf_bias
            _preflip_disabled_reason = None
            if action == "BUY_NO" and bool(self.config.get(f"disable_buy_no_{_updown_tf}", False)):
                _preflip_disabled_reason = f"buy_no_{_updown_tf}_disabled_lane"
            elif action == "BUY_YES" and bool(self.config.get(f"disable_buy_yes_{_updown_tf}", False)):
                _preflip_disabled_reason = f"buy_yes_{_updown_tf}_disabled_lane"
            if _preflip_disabled_reason:
                _bump_skip(_preflip_disabled_reason)
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason=_preflip_disabled_reason,
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={
                        "side_source": side_source,
                        "stage": "pre_flip_lane_disable",
                    },
                )
                continue

            # 2026-08-02 KNOB A — OVERSOLD-SHORT gate, TAPE-ADAPTIVE (ETH port; ETH duplicates
            # the scan loop). operator directive: no tape-blind gates. eth 15m shorts were the
            # biggest native-short bleed (-9.67, 0W/4) ALL at eth RSI 19-25 shorting a bounce in
            # a bull. But an oversold short is +EV in a DOWN tape (continuation). So defer to the
            # realized tape-adapter for eth's short ("down") side: delta >= admit_below => short
            # net-losing (up/bounce tape) => BLOCK; < admit_below => short winning (down tape) =>
            # ADMIT. Non-oversold eth shorts keep feeding the adapter so it self-flips with the
            # tape. Config buy_no_oversold_hard_block_rsi (0=off) + buy_no_oversold_adapter_admit_below.
            # 2026-08-03 P0 FLAT-ENTRY BLOCK (Codex bundle, operator GO; mirrors sol_macro): 23% of
            # trades fired when the tape had NO direction (tape_map FLAT), ~15% WR = fee-bleed coin
            # flips. Block entry (BOTH sides) on a FRESH FLAT/low-conf tape read; FAIL OPEN on a stale/
            # missing snapshot so a feed stall doesn't starve entries. Per-window; default OFF. Config
            # (by_tf): require_tape_direction, require_tape_direction_min_conf (0.5), _max_age_s (90).
            if bool(self._tf_cfg(_updown_tf, "require_tape_direction", False)):
                try:
                    _efd_tm = _latest_tape_state("eth_macro") or {}
                except Exception:
                    _efd_tm = {}
                _efd_dir = str(_efd_tm.get("direction") or "").upper()
                _efd_conf = float(_efd_tm.get("confidence", 0.0) or 0.0)
                _efd_minconf = float(self._tf_cfg(_updown_tf, "require_tape_direction_min_conf", 0.5) or 0.0)
                _efd_max_age = float(self._tf_cfg(_updown_tf, "require_tape_direction_max_age_s", 90.0) or 0.0)
                try:
                    _efd_age = time.time() - float(_efd_tm.get("ts", 0.0) or 0.0)
                except Exception:
                    _efd_age = 1e9
                _efd_fresh = _efd_max_age <= 0.0 or _efd_age <= _efd_max_age
                if _efd_fresh and (_efd_dir not in ("UP", "DOWN") or _efd_conf < _efd_minconf):
                    _bump_skip(f"{_updown_tf}_flat_tape_no_direction")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=market_allowed_side,
                        action=action,
                        reason=f"{_updown_tf}_flat_tape_no_direction(dir={_efd_dir or 'NONE'},conf={_efd_conf:.2f})",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={"side_source": side_source, "tape_dir": _efd_dir, "tape_conf": _efd_conf},
                    )
                    continue
            # 2026-08-03 (c) TAPE-MAP SIDE-VETO (operator GO, Codex-reviewed; mirrors sol_macro):
            # general wrong-side veto for shorts — veto BUY_NO when tape_map direction is UP with
            # confidence >= threshold (shorting into an up-tape = wrong side). Covers RSI 40-70
            # shorts the oversold gate below does not. Self-flips (silent DOWN/FLAT). Per-window via
            # _tf_cfg. Config (by_tf): buy_no_tape_map_veto_up (False), buy_no_tape_map_veto_min_confidence (0.6).
            if action == "BUY_NO" and bool(self._tf_cfg(_updown_tf, "buy_no_tape_map_veto_up", False)):
                try:
                    _esv_tm = _latest_tape_state("eth_macro") or {}
                except Exception:
                    _esv_tm = {}
                _esv_dir = str(_esv_tm.get("direction") or "").upper()
                _esv_conf = float(_esv_tm.get("confidence", 0.0) or 0.0)
                _esv_minconf = float(self._tf_cfg(_updown_tf, "buy_no_tape_map_veto_min_confidence", 0.6) or 0.0)
                # Codex fix: freshness guard — fail OPEN (no veto) on a stale map snapshot.
                _esv_max_age = float(self._tf_cfg(_updown_tf, "buy_no_tape_map_veto_max_age_s", 90.0) or 0.0)
                try:
                    _esv_age = time.time() - float(_esv_tm.get("ts", 0.0) or 0.0)
                except Exception:
                    _esv_age = 1e9
                if _esv_dir == "UP" and _esv_conf >= _esv_minconf and _esv_age <= _esv_max_age:
                    _bump_skip(f"buy_no_{_updown_tf}_tape_map_side_veto")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=market_allowed_side,
                        action=action,
                        reason=f"buy_no_{_updown_tf}_tape_map_side_veto(dir=UP,conf={_esv_conf:.2f})",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={"side_source": side_source, "tape_dir": _esv_dir, "tape_conf": _esv_conf},
                    )
                    continue
            # 2026-08-03 #1 REBUILD (operator GO; mirrors sol_macro): gate on the TAPE MAP
            # direction (fed by price/indicators, HAS data even when the realized adapter is
            # starved). The old adapter-only gate DEADLOCKED in a deep oversold down-tape (every
            # short RSI<floor -> all blocked -> nothing fills -> adapter n=1-5/delta 0.0 ->
            # block-on-no-data fails CLOSED -> static RSI<floor block fighting the down-tape).
            #   map DOWN -> ADMIT (continuation); map UP -> BLOCK (bounce);
            #   map FLAT/low-conf/no-map -> realized-adapter tie-break (old logic).
            # Config: buy_no_oversold_hard_block_rsi (0=off), buy_no_oversold_use_tape_map (True),
            # buy_no_oversold_map_min_confidence (0.5), buy_no_oversold_adapter_admit_below.
            _eth_bn_floor = float(self.config.get("buy_no_oversold_hard_block_rsi", 0.0) or 0.0)
            if action == "BUY_NO" and _eth_bn_floor > 0.0:
                _eth_rsi = getattr(eth, "rsi_14", None)
                if _eth_rsi is not None and float(_eth_rsi) < _eth_bn_floor:
                    _eos_block = None
                    _eos_dbg = ""
                    if bool(self.config.get("buy_no_oversold_use_tape_map", True)):
                        try:
                            _eos_tm = _latest_tape_state("eth_macro") or {}
                        except Exception:
                            _eos_tm = {}
                        _eos_dir = str(_eos_tm.get("direction") or "").upper()
                        _eos_conf = float(_eos_tm.get("confidence", 0.0) or 0.0)
                        _eos_min_conf = float(self.config.get("buy_no_oversold_map_min_confidence", 0.5) or 0.0)
                        if _eos_dir in ("DOWN", "UP") and _eos_conf >= _eos_min_conf:
                            _eos_block = (_eos_dir == "UP")
                            _eos_dbg = f"map={_eos_dir},conf={_eos_conf:.2f}"
                        else:
                            _eos_dbg = f"map={_eos_dir or 'NONE'},conf={_eos_conf:.2f}->adptr"
                    if _eos_block is None:
                        try:
                            _eth_adm = float(
                                get_tape_admission_delta("eth_macro", _updown_tf, "down") or 0.0
                            )
                        except Exception:
                            _eth_adm = 0.0
                        _eth_admit_below = float(
                            self.config.get("buy_no_oversold_adapter_admit_below", 0.0) or 0.0
                        )
                        _eos_block = (_eth_adm >= _eth_admit_below)
                        _eos_dbg = (f"{_eos_dbg},adm={_eth_adm:+.3f}" if _eos_dbg else f"adm={_eth_adm:+.3f}")
                    if _eos_block:
                        _bump_skip(f"buy_no_{_updown_tf}_oversold_adaptive_block")
                        _log_skip_reject(
                            market=market,
                            window=_updown_tf,
                            side=market_allowed_side,
                            action=action,
                            reason=f"buy_no_{_updown_tf}_oversold_adaptive_block(rsi={float(_eth_rsi):.0f}<{_eth_bn_floor:.0f},{_eos_dbg})",
                            yes_price=yes_price,
                            htf_bias=primary_htf_bias,
                            context={"side_source": side_source, "rsi_14": _eth_rsi, "oversold_block_dbg": _eos_dbg},
                        )
                        continue
                    # else: down-tape continuation (or adapter says short winning) -> admit.

            # 2026-07-06 ETH FADE-SHADOW (Codex-GO, observe-only): eth is the only
            # alt with NO fade machinery (sol_macro: 39 fade refs, eth: 0), so it can
            # never take the contrarian side and we have zero native-vs-fade evidence
            # (~4 lifetime live trades). Log the fade COUNTERFACTUAL for every resolved
            # native candidate; the ghost settler scores it against the real outcome.
            # NON-TRADING by construction: no writes to action/side/decision, no
            # continue, fail-silent. Rows carry stage=shadow_fade + reason suffix
            # handled by the *_shadow exclusion in decision tooling. Kill switch:
            # eth_fade_shadow_enabled: false.
            if bool(self.config.get("eth_fade_shadow_enabled", True)) and not _fade_active:
                try:
                    _fs_side = "SHORT" if market_allowed_side == "LONG" else "LONG"
                    _fs_action = "BUY_NO" if _fs_side == "SHORT" else "BUY_YES"
                    _fs_src = f"eth_{_updown_tf}_fade_native"
                    try:
                        _fs_bucket = f"{(int(float(yes_price) * 20) / 20.0):.2f}"
                    except Exception:
                        _fs_bucket = "?"
                    log_rejected_candidate(
                        strategy=self._signal_strategy_name,
                        window=_updown_tf,
                        side=_fs_side,
                        action=_fs_action,
                        reason="eth_fade_shadow",
                        market=market,
                        yes_price=yes_price,
                        est_prob_up=0.50,
                        htf_bias=primary_htf_bias,
                        stage="shadow_fade",
                        side_source=_fs_src,
                        resolver_path=_fs_src,
                        context={
                            "shadow_kind": "eth_fade_counterfactual",
                            "native_side": market_allowed_side,
                            "native_action": action,
                            "native_side_source": side_source,
                            "fade_side": _fs_side,
                            "fade_action": _fs_action,
                            "yes_price_bucket": _fs_bucket,
                        },
                    )
                except Exception:
                    pass

            # ETH-native momentum guards. 2026-05-23 ghost-counterfactual review:
            # default-on guards were breakeven-to-harmful (BUY_NO n=9826 WR=48%,
            # 1h SHORT specifically WR=62% — guard blocking winners). Now an
            # explicit per-(side, window) allowlist via `eth_momentum_confirm:
            # {buy_yes: [...], buy_no: [...]}`. Empty/missing = guard off.
            #
            # Shadow mode (2026-05-23): a window listed under `shadow.buy_yes`
            # / `shadow.buy_no` runs the check and logs the would-have-been-
            # rejected candidate to the ghost log (reason suffixed `_shadow`),
            # but does NOT block the trade. Used to accumulate fresh ghost
            # data without sacrificing trade frequency. A window listed in
            # BOTH the block list and the shadow list takes the block path.
            _eth_mc_cfg = self.config.get("eth_momentum_confirm") or {}
            _eth_mc_shadow = _eth_mc_cfg.get("shadow") or {}

            def _eth_mc_context() -> Dict[str, Any]:
                return {
                    "eth_macd_5m_hist": float(eth.macd_5m.histogram or 0.0),
                    "eth_macd_5m_rising": bool(eth.macd_5m.histogram_rising),
                    "eth_macd_5m_crossover": eth.macd_5m.crossover,
                    "eth_macd_15m_hist": float(eth.macd_15m.histogram or 0.0),
                    "eth_macd_15m_rising": bool(eth.macd_15m.histogram_rising),
                    "eth_macd_15m_crossover": eth.macd_15m.crossover,
                    "side_source": side_source,
                }

            # Bias-aligned bypass: when the trade aligns with primary_htf_bias the
            # gate is provably inverting on alts (ghost data 5/22→5/27: BEARISH×SHORT
            # blocked WR > traded WR by +13.2pp on eth_macro; LONG side neutral but
            # contributes to LONG starvation). Keep gate active for counter-trend
            # trades.
            _eth_bias_aligned_short = (
                action == "BUY_NO"
                and (primary_htf_bias or "").upper() == "BEARISH"
            )
            _eth_bias_aligned_long = (
                action == "BUY_YES"
                and (primary_htf_bias or "").upper() == "BULLISH"
            )

            if action == "BUY_NO" and not _eth_bias_aligned_short:
                _block = _updown_tf in (_eth_mc_cfg.get("buy_no") or [])
                _shadow = (not _block) and _updown_tf in (_eth_mc_shadow.get("buy_no") or [])
                if _block or _shadow:
                    # Horizon-coherent: own timeframe + next-larger fallback only.
                    if _updown_tf == "1h":
                        _own, _larger = eth.macd_1h, eth.macd_1h
                    elif _updown_tf == "15m":
                        _own, _larger = eth.macd_15m, eth.macd_1h
                    else:  # 5m
                        _own, _larger = eth.macd_5m, eth.macd_15m
                    _eth_bear_confirmed = (
                        _own.crossover == "BEARISH_CROSS"
                        or (_own.histogram < 0 and not _own.histogram_rising)
                        or _larger.crossover == "BEARISH_CROSS"
                        or (_larger.histogram < 0 and not _larger.histogram_rising)
                    )
                    if not _eth_bear_confirmed:
                        _reason = "buy_no_no_eth_momentum_confirm" + ("_shadow" if _shadow else "")
                        ctx = _eth_mc_context()
                        ctx["shadow"] = bool(_shadow)
                        if _block:
                            _bump_skip(_reason)
                        _log_skip_reject(
                            market=market,
                            window=_updown_tf,
                            side=market_allowed_side,
                            action=action,
                            reason=_reason,
                            yes_price=yes_price,
                            htf_bias=primary_htf_bias,
                            context=ctx,
                        )
                        if _block:
                            continue
            if action == "BUY_YES" and not _eth_bias_aligned_long:
                _block = _updown_tf in (_eth_mc_cfg.get("buy_yes") or [])
                _shadow = (not _block) and _updown_tf in (_eth_mc_shadow.get("buy_yes") or [])
                if _block or _shadow:
                    # Horizon-coherent: own timeframe + next-larger fallback only.
                    if _updown_tf == "1h":
                        _own, _larger = eth.macd_1h, eth.macd_1h
                    elif _updown_tf == "15m":
                        _own, _larger = eth.macd_15m, eth.macd_1h
                    else:  # 5m
                        _own, _larger = eth.macd_5m, eth.macd_15m
                    _eth_bull_confirmed = (
                        _own.crossover == "BULLISH_CROSS"
                        or (_own.histogram > 0 and _own.histogram_rising)
                        or _larger.crossover == "BULLISH_CROSS"
                        or (_larger.histogram > 0 and _larger.histogram_rising)
                    )
                    if not _eth_bull_confirmed:
                        _reason = "buy_yes_no_eth_momentum_confirm" + ("_shadow" if _shadow else "")
                        ctx = _eth_mc_context()
                        ctx["shadow"] = bool(_shadow)
                        if _block:
                            _bump_skip(_reason)
                        _log_skip_reject(
                            market=market,
                            window=_updown_tf,
                            side=market_allowed_side,
                            action=action,
                            reason=_reason,
                            yes_price=yes_price,
                            htf_bias=primary_htf_bias,
                            context=ctx,
                        )
                        if _block:
                            continue

            # 2026-07-21 TAPE ARBITRATION (operator GO; Codex design-reviewed) — ETH port of the
            # sol_macro gate. Same root: a bias-aligned entry (LONG into BULLISH 1h) bypasses the
            # momentum-confirm above, admitting with no 5m/15m check -> stale-bias side into a turned
            # tape. Chop-restricted (er < tape_arbitration_er_chop_max), own-TF contradiction only,
            # shadow-capable. Config-gated, default OFF.
            if (
                self.config.get("tape_arbitration_enabled", False)
                and is_updown
                and (_eth_bias_aligned_long or _eth_bias_aligned_short)
                and _updown_tf in set(self.config.get("tape_arbitration_windows", ["5m", "15m"]) or [])
            ):
                _ta_own = (
                    eth.macd_5m if _updown_tf == "5m"
                    else eth.macd_15m if _updown_tf == "15m"
                    else eth.macd_1h
                )
                _ta_fast_against = (
                    macd_bearish_momentum_ok(_ta_own) if action == "BUY_YES"
                    else macd_bullish_momentum_ok(_ta_own)
                )
                _ta_er = None
                try:
                    _ta_rstate = _asset_regime.get_state(
                        getattr(self.sol_service, "alt_symbol", None)
                    )
                    if _ta_rstate is not None and _ta_rstate.get("er") is not None:
                        _ta_er = float(_ta_rstate.get("er"))
                except Exception:
                    _ta_er = None
                _ta_chop = _ta_er is not None and _ta_er < float(
                    self.config.get("tape_arbitration_er_chop_max", 0.30) or 0.30
                )
                if _ta_fast_against and _ta_chop:
                    _ta_shadow = bool(self.config.get("tape_arbitration_shadow_only", False))
                    _ta_reason = "tape_arbitration_stale_side_chop" + ("_shadow" if _ta_shadow else "")
                    _bump_skip(_ta_reason)
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=market_allowed_side,
                        action=action,
                        reason=_ta_reason,
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={
                            "er": _ta_er,
                            "own_tf": _updown_tf,
                            "own_macd_hist": float(getattr(_ta_own, "histogram", 0.0) or 0.0),
                            "own_macd_crossover": getattr(_ta_own, "crossover", None),
                            "shadow": _ta_shadow,
                        },
                    )
                    if not _ta_shadow:
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

            # Deadzone / blocked-UTC-hour gate purged 2026-06-10.

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

            # NOTE: window-delta confirmation runs LATER, after the side is finalized
            # by apply_fresh_cross_override + _resolve_eth_direction. See the
            # `_window_delta_disagrees` call site below (post-override). Checking the
            # stale pre-override side here would pre-empt the momentum flip.

            if getattr(corr, "degraded", False) and self.skip_on_degraded_correlation:
                if self._btc_trade_inputs_enabled():
                    _bump_skip("degraded_correlation")
                    logger.info(
                        f"  ETH skip '{market.question[:40]}' — correlation degraded "
                        f"({', '.join(getattr(corr, 'degraded_reasons', [])) or 'unknown'})"
                    )
                    continue

            # 2026-05-22: btc_min_move_dollars gate REMOVED (BTC must not gate ETH
            # entry — "alts decided by alt-native indicators"). 2026-06-09: dropped the
            # leftover dead diagnostic computation (it only fed `if ...: pass` and never
            # actually logged to reason_parts); config keys btc_min_move_dollars_* are
            # now vestigial.

            # Skip only when our entry-side price is in the unfavorable long
            # tail. Favorable tail (our side >= 0.80) ghost-WR 87–97%; kept.
            _sample("entry_price", yes_price)
            _our_price = (1.0 - yes_price) if action == "BUY_NO" else yes_price
            # 2026-07-13 operator GO (unlock 3): port sol_macro 07-08 removal — config-driven,
            # default OFF. Set price_too_far_min_our_price: 0.12 to restore. 373 rejects/48h.
            _ptf_min = float(self.config.get("price_too_far_min_our_price", 0.0) or 0.0)
            if _ptf_min > 0.0 and _our_price < _ptf_min:
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
                        "entry_price": float(_our_price),
                        "yes_price": float(yes_price),
                        "our_side_price_min": _ptf_min,
                    },
                    probe_variants=build_range_probe_variants(
                        metric_name="our_side_entry_price",
                        observed_value=float(_our_price),
                        baseline_min=_ptf_min,
                        baseline_max=1.0,
                        relax_steps=[0.02, 0.04],
                        tighten_steps=[0.03, 0.08],
                    ),
                    policy_version="entry_price_band_v2_side_aware",
                )
                continue

            side_source_counts[side_source] = side_source_counts.get(side_source, 0) + 1

            action_counts[action] = action_counts.get(action, 0) + 1
            direction = direction_decision.direction
            reason_parts = [
                f"ETH_HTF={alt_1h_trend}",
                f"ETH_15M={alt_15m_trend}",
                f"ETH_5M={alt_5m_trend}",
                f"PRIMARY_ETH_HTF={primary_htf_bias}",
                f"side={market_allowed_side}",
                f"side_src={side_source}",
            ]
            if resolution.horizon_bias == "NEUTRAL":
                reason_parts.append(f"{_updown_tf}_neutral")
            if resolution.penalty_reasons:
                reason_parts.append(
                    f"bias_penalty={resolution.confidence_penalty:.3f}:{','.join(resolution.penalty_reasons)}"
                )
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

            # 2026-07-29 (Codex) SHORT-THE-BULL 5m gate — placed OUTSIDE the
            # enforce_alt_1h_alignment wrapper (which is False for eth, so an inside block
            # would never fire). Hard-block 5m BUY_NO into a BULLISH eth 1h — the
            # eth_5m_native short-into-bull leak. NARROW: 5m only; 15m/1h unaffected.
            # Opt-out: short_the_bull_5m_guard_enabled: false.
            if (
                action == "BUY_NO"
                and mtt.h1_trend == "BULLISH"
                and _updown_tf == "5m"
                and bool(self.config.get("short_the_bull_5m_guard_enabled", True))
            ):
                _sb_src = str(side_source or "")
                if _sb_src.endswith("_vs_slower") or "5m_native" in _sb_src:
                    _bump_skip("eth_5m_short_against_h1")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=market_allowed_side,
                        action=action,
                        reason="eth_5m_short_against_h1",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={
                            "alt_1h_trend": mtt.h1_trend,
                            "window_size": _updown_tf,
                            "side_source": side_source,
                        },
                    )
                    logger.info(
                        "  ETH skip BUY_NO on '%s' — ETH 1H BULLISH blocks 5m short "
                        "(short-the-bull)",
                        market.question[:40],
                    )
                    continue

            if self.enforce_alt_1h_alignment:
                _alt_1h_block_reason = self._alt_1h_alignment_blocks_entry(
                    action=action,
                    window_size=_updown_tf,
                    alt_1h_trend=mtt.h1_trend,
                )
                if _alt_1h_block_reason:
                    _bump_skip(_alt_1h_block_reason)
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=market_allowed_side,
                        action=action,
                        reason=_alt_1h_block_reason,
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={
                            "alt_1h_trend": mtt.h1_trend,
                            "window_size": _updown_tf,
                            "side_source": side_source,
                        },
                    )
                    logger.info(
                        "  ETH skip %s on '%s' — ETH 1H=%s blocks 5m BUY_YES",
                        action,
                        market.question[:40],
                        mtt.h1_trend,
                    )
                    continue
                # 2026-08-04 (operator GO) — port of sol_macro alt_1h_require_confirm. The BULLISH/
                # BEARISH diagnostics below were annotate-only (let the trade through). When
                # alt_1h_require_confirm is set, SKIP a native entry ETH's OWN 1H does not confirm
                # (BUY_NO needs 1H BEARISH; BUY_YES needs 1H BULLISH; NEUTRAL/opposing blocks it) —
                # the all-short-into-bull leak. Hot-reloadable, reversible. Reduces frequency.
                # 2026-08-04 loosen: alt_1h_allow_neutral=true blocks ONLY an OPPOSING 1H
                # (short w/ 1H BULLISH, long w/ 1H BEARISH); NEUTRAL passes. Un-starves neutral tape.
                _eth_need_1h = "BEARISH" if action == "BUY_NO" else "BULLISH"
                _eth_oppose_1h = "BULLISH" if action == "BUY_NO" else "BEARISH"
                _eth_h1u = str(mtt.h1_trend or "NEUTRAL").upper()
                _eth_blocked_1h = (
                    (_eth_h1u == _eth_oppose_1h)
                    if bool(self.config.get("alt_1h_allow_neutral", False))
                    else (_eth_h1u != _eth_need_1h)
                )
                if (
                    bool(self.config.get("alt_1h_require_confirm", False))
                    and _eth_blocked_1h
                ):
                    reason_parts.append(
                        f"alt_1h_unconfirmed_{action.lower()}_{str(mtt.h1_trend or 'neutral').lower()}"
                    )
                    _bump_skip("alt_1h_unconfirmed")  # Codex: count it so starvation is measurable
                    logger.info(
                        "  ETH SKIP %s on '%s' — own 1H=%s does not confirm (need %s)",
                        action, market.question[:40], mtt.h1_trend, _eth_need_1h,
                    )
                    continue
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
            # 2026-06-16: ETH pocket-only selection (ghost-validated, scripts pocket-hunt).
            # ETH est_prob is ~0.50 AUC (no model to "fix"); its edge is pure SELECTION.
            # The ONLY +EV ETH pocket is BUY_NO (short) at elevated RSI: 15m/1h BUY_NO
            # RSI>=55 = +0.10 to +0.26 net-of-fee EV (n=2.0k/0.35k, stable recent). ALL
            # ETH longs are -EV (-0.09 to -0.17) and 5m is dead both sides. Admit only the
            # pocket; ghost-log the rest so the counterfactual keeps settling. Opt-in
            # (eth_pocket_only), default off = no-op.
            if _updown_tf in ("5m", "15m", "1h") and bool(self.config.get("eth_pocket_only", False)):
                _pocket_skip = None
                if action == "BUY_YES":
                    # 2026-07-04 operator: reopen eth 1h BUY_YES only — blocked pop
                    # settled 66% (n=7,541, 36h). 15m/5m longs stay pocketed.
                    if _updown_tf == "1h" and bool(
                        self.config.get("eth_pocket_allow_buy_yes_1h", False)
                    ):
                        _pocket_skip = None
                    # 2026-07-13 operator GO (completeness audit, Codex GO): the pocket
                    # predates the fade — 15m fade-longs died here (144 rejects in 30min)
                    # after the momentum-confirm fix opened them. Allow ONLY fade-sourced
                    # 15m longs; momentum/native 15m longs stay pocketed.
                    elif (
                        _updown_tf == "15m"
                        and _fade_active
                        and bool(self.config.get("eth_pocket_allow_15m_buy_yes_fade", False))
                    ):
                        _pocket_skip = None
                    # 2026-07-14 eth-long-unlock A/B (operator GO): bull tape ran eth
                    # native-LONG 600 scans vs SHORT 28 (since 10:31) and every long
                    # died here — the pocket had no 5m BUY_YES allowance and 15m only
                    # passed fade-sourced longs, so the eth5-win SHORT unlock could
                    # never fill when the tape refuses to go native-short. Open both
                    # through a quality bar mirroring the short side RSI floor:
                    # pullback longs (RSI <= eth_buy_yes_rsi_max) OR rising MACD on
                    # the lane timeframe. Sized 0.25x via lane config. Kill switches:
                    # eth_pocket_allow_5m_buy_yes / eth_pocket_allow_15m_buy_yes_native.
                    elif (
                        _updown_tf == "5m"
                        and bool(self.config.get("eth_pocket_allow_5m_buy_yes", False))
                        and (
                            eth.rsi_14 <= float(self.config.get("eth_buy_yes_rsi_max", 45.0))
                            or (
                                bool(eth.macd_5m.histogram_rising)
                                and eth.rsi_14
                                <= float(self.config.get("eth_buy_yes_rsi_momentum_max", 60.0))
                            )
                        )
                    ):
                        _pocket_skip = None
                    elif (
                        _updown_tf == "15m"
                        and not _fade_active
                        and bool(self.config.get("eth_pocket_allow_15m_buy_yes_native", False))
                        and (
                            eth.rsi_14 <= float(self.config.get("eth_buy_yes_rsi_max", 45.0))
                            or (
                                bool(eth.macd_15m.histogram_rising)
                                and eth.rsi_14
                                <= float(self.config.get("eth_buy_yes_rsi_momentum_max", 60.0))
                            )
                        )
                    ):
                        _pocket_skip = None
                    else:
                        _pocket_skip = "eth_pocket_buy_yes_off"
                elif _updown_tf == "5m":
                    # 2026-07-13 E2a (operator GO): eth 5m is a COLLECTION lane per
                    # standing rule — reopen the short side THROUGH the same RSI>=55
                    # floor as 15m/1h (Codex caveat: don't bypass the quality bar).
                    if (
                        action == "BUY_NO"
                        and bool(self.config.get("eth_pocket_allow_5m_buy_no", False))
                        and eth.rsi_14 >= float(self.config.get("eth_buy_no_rsi_min", 55.0))
                    ):
                        _pocket_skip = None
                    else:
                        _pocket_skip = "eth_pocket_5m_off"
                elif eth.rsi_14 < float(self.config.get("eth_buy_no_rsi_min", 55.0)):
                    _pocket_skip = "eth_pocket_low_rsi_off"
                if _pocket_skip:
                    _bump_skip(_pocket_skip)
                    log_rejected_candidate(
                        strategy=self._signal_strategy_name, window=_updown_tf,
                        side=market_allowed_side, action=action,
                        reason=_pocket_skip, market=market,
                        yes_price=yes_price, est_prob_up=0.50,
                        htf_bias=primary_htf_bias, stage="eth_pocket",
                        context=build_market_context(
                            asset_spot=eth.current_price, btc_spot=corr.btc_price,
                            rsi_14=eth.rsi_14, atr_14=eth.atr_14,
                        ),
                    )
                    continue
            _own_rsi, _own_macd = self._own_tf_rsi_macd(eth, _updown_tf)
            _rsi_hard_block, rsi_soft_delta, rsi_min_edge_add = self._resolve_rsi_gate(
                action,
                _own_rsi,
                macd=_own_macd,
                window=_updown_tf,
            )
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
                if (
                    self._btc_trade_inputs_enabled()
                    and self.btc_follow_5m_requires_impulse
                    and not _impulse_gate_ok
                ):
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
                                macd_hist_5m=getattr(getattr(eth, "macd_5m", None), "histogram", None),
                                macd_hist_15m=getattr(getattr(eth, "macd_15m", None), "histogram", None),
                                macd_hist_1h=getattr(getattr(eth, "macd_1h", None), "histogram", None),
                                rsi_5m=getattr(getattr(eth, "tf_5m", None), "rsi_14", None),
                                rsi_15m=getattr(getattr(eth, "tf_15m", None), "rsi_14", None),
                                rsi_1h=getattr(getattr(eth, "tf_1h", None), "rsi_14", None),
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
                                macd_hist_5m=getattr(getattr(eth, "macd_5m", None), "histogram", None),
                                macd_hist_15m=getattr(getattr(eth, "macd_15m", None), "histogram", None),
                                macd_hist_1h=getattr(getattr(eth, "macd_1h", None), "histogram", None),
                                rsi_5m=getattr(getattr(eth, "tf_5m", None), "rsi_14", None),
                                rsi_15m=getattr(getattr(eth, "tf_15m", None), "rsi_14", None),
                                rsi_1h=getattr(getattr(eth, "tf_1h", None), "rsi_14", None),
                            ),
                        },
                    )
                    continue
                est_prob_up = self._apply_primary_htf_bias(est_prob_up, primary_htf_bias, 0.04)
                if resolution.confidence_penalty > 0:
                    est_prob_up += (
                        -resolution.confidence_penalty
                        if market_allowed_side == "LONG"
                        else resolution.confidence_penalty
                    )
                # Move 2 (2026-05-16): dampen est_prob when ETH 1H trend disagrees with side.
                if self.enforce_alt_1h_alignment:
                    if market_allowed_side == "LONG" and mtt.h1_trend == "BEARISH":
                        est_prob_up -= 0.04
                        reason_parts.append("h1_dampen_long_5m")
                    elif market_allowed_side == "SHORT" and mtt.h1_trend == "BULLISH":
                        est_prob_up += 0.04
                        reason_parts.append("h1_dampen_short_5m")
                if self._btc_trade_inputs_enabled():
                    est_prob_up += btc_impulse if market_allowed_side == "LONG" else -btc_impulse
                est_prob_up += eth_5m_adj if market_allowed_side == "LONG" else -eth_5m_adj
                if eth.rsi_14 > 75:
                    est_prob_up -= 0.02
                elif eth.rsi_14 < 25:
                    est_prob_up += 0.02
                confidence = max(
                    0.55,
                    min(
                        0.85,
                        0.50
                        + (
                            abs(btc_impulse) * 1.8
                            if self._btc_trade_inputs_enabled()
                            else 0.0
                        )
                        + abs(eth_5m_adj) * 2.0,
                    ),
                )
                reason_parts.extend(["UPDOWN_5m", *btc_reasons, *eth_reasons])
            else:
                if is_1h:
                    if (
                        self._btc_trade_inputs_enabled()
                        and btc_full_ok
                        and not self._btc_follow_1h_ok(btc_ta, market_allowed_side)
                    ):
                        if market_allowed_side == "SHORT":
                            est_prob_up += float(
                                self.config.get("btc_1h_not_following_short_penalty", 0.04)
                            )
                            follow_penalty_min_edge_add += float(
                                self.config.get("btc_1h_not_following_short_min_edge_add", 0.01)
                            )
                            reason_parts.append("btc_1h_follow_penalty_short")
                        else:
                            est_prob_up -= float(
                                self.config.get(
                                    "btc_1h_not_following_long_penalty",
                                    self.config.get("btc_1h_not_following_short_penalty", 0.04),
                                )
                            )
                            follow_penalty_min_edge_add += float(
                                self.config.get(
                                    "btc_1h_not_following_long_min_edge_add",
                                    self.config.get("btc_1h_not_following_short_min_edge_add", 0.01),
                                )
                            )
                            reason_parts.append("btc_1h_follow_penalty_long")
                    eth_1h_adj, eth_reasons = self._eth_1h_follow_score(
                        eth.macd_1h, market_allowed_side
                    )
                    eth_1h_min_adj = float(
                        self.config.get(
                            "eth_follow_1h_min_adj",
                            max(0.03, self.eth_follow_15m_min_adj * 0.8),
                        )
                    )
                    # Ghost-validated 2026-05-30: this hard skip is correct for SHORT
                    # (42.5% WR rejects) but destroys profitable LONGs (61.4% WR / +19.6%
                    # ROI on 593 ghosts). eth_1h_adj is ALSO applied as a soft penalty at
                    # est_prob_up below, so weak-confirm LONGs already get dampened + face
                    # min_edge — the hard veto was redundant for them. Scope it to SHORT;
                    # opt back in for longs via eth_1h_weak_confirm_skip_longs: true.
                    _eth_1h_skip_longs = bool(
                        self.config.get("eth_1h_weak_confirm_skip_longs", False)
                    )
                    if eth_1h_adj < eth_1h_min_adj and (
                        market_allowed_side == "SHORT" or _eth_1h_skip_longs
                    ):
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
                                    macd_hist_5m=getattr(getattr(eth, "macd_5m", None), "histogram", None),
                                    macd_hist_15m=getattr(getattr(eth, "macd_15m", None), "histogram", None),
                                    macd_hist_1h=getattr(getattr(eth, "macd_1h", None), "histogram", None),
                                    rsi_5m=getattr(getattr(eth, "tf_5m", None), "rsi_14", None),
                                    rsi_15m=getattr(getattr(eth, "tf_15m", None), "rsi_14", None),
                                    rsi_1h=getattr(getattr(eth, "tf_1h", None), "rsi_14", None),
                                ),
                            },
                        )
                        continue
                    est_prob_up = self._apply_primary_htf_bias(est_prob_up, primary_htf_bias, 0.09)
                    if resolution.confidence_penalty > 0:
                        est_prob_up += (
                            -resolution.confidence_penalty
                            if market_allowed_side == "LONG"
                            else resolution.confidence_penalty
                        )
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
                    if (
                        self._btc_trade_inputs_enabled()
                        and btc_full_ok
                        and not self._btc_follow_15m_impulse_ok(
                        btc_ta, market_allowed_side
                        )
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
                            est_prob_up -= float(
                                self.config.get(
                                    "btc_15m_not_following_long_penalty",
                                    self.config.get("btc_15m_not_following_short_penalty", 0.03),
                                )
                            )
                            follow_penalty_min_edge_add += float(
                                self.config.get(
                                    "btc_15m_not_following_long_min_edge_add",
                                    self.config.get("btc_15m_not_following_short_min_edge_add", 0.01),
                                )
                            )
                            reason_parts.append("btc_15m_follow_penalty_long")
                    eth_15m_adj, eth_reasons = self._eth_15m_follow_score(
                        eth.macd_15m, market_allowed_side
                    )
                    required_eth_15m_adj = self._eth_follow_15m_required_adj(
                        market_allowed_side
                    )
                    eth_15m_hard_gate = bool(
                        self.config.get("eth_15m_weak_confirm_hard_gate_enabled", True)
                    )
                    if eth_15m_adj < required_eth_15m_adj and eth_15m_hard_gate:
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
                                    macd_hist_5m=getattr(getattr(eth, "macd_5m", None), "histogram", None),
                                    macd_hist_15m=getattr(getattr(eth, "macd_15m", None), "histogram", None),
                                    macd_hist_1h=getattr(getattr(eth, "macd_1h", None), "histogram", None),
                                    rsi_5m=getattr(getattr(eth, "tf_5m", None), "rsi_14", None),
                                    rsi_15m=getattr(getattr(eth, "tf_15m", None), "rsi_14", None),
                                    rsi_1h=getattr(getattr(eth, "tf_1h", None), "rsi_14", None),
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
                    if eth_15m_adj < required_eth_15m_adj:
                        follow_penalty_min_edge_add += max(
                            0.0, float(required_eth_15m_adj) - float(eth_15m_adj)
                        )
                        reason_parts.append("eth_15m_weak_confirm_soft")
                    est_prob_up = self._apply_primary_htf_bias(est_prob_up, primary_htf_bias, 0.08)
                    if resolution.confidence_penalty > 0:
                        est_prob_up += (
                            -resolution.confidence_penalty
                            if market_allowed_side == "LONG"
                            else resolution.confidence_penalty
                        )
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

            # Fresh-opposing cross override — same-TF MACD (5m/15m/1h). ETH derives
            # its action from market_allowed_side in the resolver below, so we flip
            # the side here and the resolver re-derives the matching action.
            _eth_xover_macd = (
                eth.macd_1h if _updown_tf == "1h"
                else eth.macd_5m if _updown_tf == "5m"
                else eth.macd_15m
            )
            # faster-TF leads for the slow windows: 1h reads 15m, 15m reads 5m.
            _eth_faster_macd = (
                eth.macd_15m if _updown_tf == "1h"
                else eth.macd_5m if _updown_tf == "15m"
                else None
            )
            _eth_faster_tf = "15m" if _updown_tf == "1h" else "5m" if _updown_tf == "15m" else None
            _eth_pre_action = "BUY_YES" if market_allowed_side == "LONG" else "BUY_NO"
            est_prob_up, _eth_pre_action, market_allowed_side, _, side_source = apply_fresh_cross_override(
                est_prob_up=est_prob_up, action=_eth_pre_action, allowed_side=market_allowed_side,
                direction=("UP" if market_allowed_side == "LONG" else "DOWN"),
                side_source=side_source, reason_parts=reason_parts,
                crossover=_eth_xover_macd.crossover, tf_label=_updown_tf,
                faster_crossover=(_eth_faster_macd.crossover if _eth_faster_macd is not None else None),
                faster_tf_label=_eth_faster_tf,
                strategy_name=self._signal_strategy_name, primary_htf_bias=primary_htf_bias,
                logger=logger, enabled=(not _sticky_side_active) and self.config.get("fresh_cross_override", True),
                # 2026-06-08: window-aware faster-lead RSI (1h->15m, 15m/5m->5m).
                rsi_14=getattr(getattr(eth, "tf_15m" if _updown_tf == "1h" else "tf_5m", None), "rsi_14", None), window=_updown_tf,
                momentum_flip_enabled=(not _sticky_side_active) and self.config.get("rsi_momentum_flip_1h", False),
                macd_hist_5m=getattr(getattr(eth, "macd_5m", None), "histogram", None),
                macd_flip_enabled=(not _sticky_side_active) and self.config.get("macd_momentum_flip_5m15m", False),
                macd_flip_long_to_short_enabled=(not _sticky_side_active) and self.config.get("macd_momentum_flip_long_to_short", False),
            )

            est_prob_up = max(0.10, min(0.90, est_prob_up))
            raw_est_prob = est_prob_up
            direction_decision = self._resolve_eth_direction(
                market_allowed_side=market_allowed_side,
                side_source=side_source,
                raw_est_prob=raw_est_prob,
                momentum_bias=getattr(mtt, "m5_trend", None),
            )
            action = direction_decision.action
            direction = direction_decision.direction
            # Window-delta confirmation — side is FINAL here (post-flip + resolve).
            # Inherited from SolMacroStrategy; ETH-native price only.
            _wd_flip = None if _sticky_side_active else self._window_delta_flip(eth, _updown_tf, _eval_left, action)
            if _wd_flip is not None:
                action, market_allowed_side, direction, est_prob_up, _wd_prob = _wd_flip
                raw_est_prob = est_prob_up
                side_source = f"{side_source or ''}+window_delta_flip"
                reason_parts.append(f"window_delta_flip->{action}({_wd_prob:.3f})")
            # Re-apply per-window sit-out post-flip (inherited helper): window_delta_flip
            # can turn a native long into a BUY_NO (or vice-versa), bypassing the pre-flip
            # disable_buy_no_<tf> / disable_buy_yes_<tf> gate. (2026-06-16 fix, ETH parity.)
            _postflip_reason = self._post_flip_disabled_side(
                action, _updown_tf, side_source
            )
            if _postflip_reason:
                _bump_skip(_postflip_reason)
                _log_skip_reject(
                    market=market, window=_updown_tf, side=market_allowed_side,
                    action=action, reason=_postflip_reason, yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                )
                continue
            # 2026-07-24 (operator GO + Codex): ETH BUY_NO RSI floor at FINAL action
            # (post-flip) so it binds on native + fade + window_delta_flip paths — unlike
            # the eth_pocket_only block (pre-flip, default off). ETH shorts are -EV below
            # RSI ~55 (live: 15m<50 -$47, 1h<60 -$23) and +EV above (15m>=60 +$61, 1h>=60
            # +$14). NEW key eth_buy_no_rsi_min_floor to avoid stacking with the existing
            # eth_buy_no_rsi_min/eth_pocket_only gate; unset => no-op.
            # 15m/1h ONLY (Codex 2026-07-24): the edge is on 15m/1h shorts (+$61/+$14);
            # excluding 5m avoids clobbering the later eth_5m_buy_no_flip_to_yes inversion
            # path AND protects the eth 5m collection lane. 5m short is a tiny -$21 lane.
            _bn_rsi_floor = self.config.get("eth_buy_no_rsi_min_floor")
            if action == "BUY_NO" and _updown_tf in ("15m", "1h") and _bn_rsi_floor is not None:
                _eth_rsi = getattr(eth, "rsi_14", None)
                if _eth_rsi is not None and float(_eth_rsi) < float(_bn_rsi_floor):
                    _bump_skip("eth_buy_no_rsi_floor_off")
                    _log_skip_reject(
                        market=market, window=_updown_tf, side=market_allowed_side,
                        action=action, reason="eth_buy_no_rsi_floor_off", yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                    )
                    continue
            # Low-ATR volatility gate (inherited) — configured losing lanes only
            # trade in low vol; mid/high-ATR is where they bleed. Side is final.
            _atr_block = self._low_atr_gate_blocks(eth, _updown_tf, action)
            if _atr_block is not None:
                _atr_pct, _atr_thr = _atr_block
                _bump_skip("low_atr_gate_skip")
                _log_skip_reject(
                    market=market, window=_updown_tf, side=market_allowed_side,
                    action=action, reason="low_atr_gate_skip", yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={"atr_pct": round(_atr_pct, 6), "atr_threshold": _atr_thr},
                )
                continue
            estimated_prob = self._calibrate_est_prob(
                raw_est_prob,
                action=action,
                direction=direction,
                window_size=_updown_tf,
                side_source=side_source,
                signal_reason=" | ".join(r for r in reason_parts if r),
                htf_bias=primary_htf_bias,
                primary_htf_bias=primary_htf_bias,
                alt_htf_bias=mtt.h1_trend,
                btc_1h_regime=btc_1h_regime if btc_ta else None,
            )
            # ── eth 5m BUY_NO inversion flip (forward-test 2026-06-11) ──────────
            # eth 5m BUY_NO is a structurally inverted lane: held-to-resolution
            # WR 26-33% over n=133-259 (since 2026-05-25), -$375 live PnL. On those
            # SAME markets the YES side resolves in-the-money ~67%, so the short is
            # anti-selective and the cheap long is +EV. The candidate has already
            # cleared every short-side gate above (alt_1h, rsi, eth_5m_weak_confirm,
            # momentum-confirm), so we redirect it to the long here rather than
            # suppressing it. The native est_prob was built to JUSTIFY the short
            # (dragged below 0.5 by bearish eth adjustments); its complement is the
            # long's P(up). The normal edge gate below then admits only the cheap
            # longs (low yes_price) — exactly the +EV pocket. All downstream
            # directional guards are inert here (_btc_trade_inputs_enabled()==False).
            # Default-on; opt-out via strategies.eth_macro.eth_5m_buy_no_flip_to_yes: false.
            if (
                bool(self.config.get("eth_5m_buy_no_flip_to_yes", True))
                and not _sticky_side_active
                and _updown_tf == "5m"
                and action == "BUY_NO"
            ):
                estimated_prob = max(1.0 - float(estimated_prob), 0.50)
                action = "BUY_YES"
                direction = "UP"
                market_allowed_side = "LONG"
                side_source = f"{side_source or ''}+eth_5m_no_to_yes_flip"
                reason_parts.append("eth_5m_no_to_yes_flip")
            # 2026-07-30 WRONG-DIRECTION FIX (P0): re-gate the FINAL post-flip action. The gate above
            # ran on the pre-flip side; flips (fresh-cross/window-delta/posterior/5m-to-yes) can turn a
            # long into a BUY_NO oversold short the exhaustion gate never saw. Mirrors the post-flip
            # _post_flip_disabled_side re-check.
            _pf_rsi, _pf_macd = self._own_tf_rsi_macd(eth, _updown_tf)
            _pf_hard, _, rsi_min_edge_add = self._resolve_rsi_gate(
                action,
                _pf_rsi,
                macd=_pf_macd,
                window=_updown_tf,
            )
            if _pf_hard:
                _bump_skip("rsi_hard_blocked_postflip")
                continue
            # 2026-07-03 ETH port of admission shrink (sol/btc: _admission_prob):
            # deflate overconfident est toward 0.5 for the min_edge comparison only;
            # estimated_prob itself stays raw for floors/logging.
            try:
                _shrink = float(self.config.get("entry_admission_calibration_shrink", 1.0))
            except (TypeError, ValueError):
                _shrink = 1.0
            _adm_prob = float(estimated_prob) if _shrink >= 1.0 else 0.5 + _shrink * (float(estimated_prob) - 0.5)
            edge = _adm_prob - yes_price if action == "BUY_YES" else yes_price - _adm_prob
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
            # 2026-08-10 RSI-FADE marker (port of sol_macro). Faded candidates bet AGAINST
            # est_prob, so est_prob-based gates (conviction floor, lane_min_edge, centered edge)
            # must be exempted or the fade takes zero trades. Reverts byte-identical when
            # risk.rsi_fade.enabled:false (=> no *_rsi_fade producer => _is_rsi_fade always False).
            _is_rsi_fade = bool(side_source and "rsi_fade" in side_source)
            # 2026-07-02 Deploy2(U1): RAW conviction floor, updown BUY_YES only.
            # 2026-07-28: per-tf resolution (by_tf.<tf> > defaults > flat) so the floor
            # is per-lane, matching sol/btc — flat self.config.get ignored by_tf overrides.
            _yes_conv_floor = float(
                self._tf_cfg(_updown_tf, "min_est_prob_conviction_buy_yes", 0.0) or 0.0
            )
            if (
                action == "BUY_YES"
                and _yes_conv_floor > 0.0
                and float(estimated_prob) < _yes_conv_floor
                and not _is_rsi_fade  # fade bets against est_prob => conviction floor N/A
                and not _side_policy_active  # market picks the side => est_prob describes the OTHER side
            ):
                _bump_skip("buy_yes_conviction_floor")
                continue
            if not lane_policy.enabled:
                _bump_skip("lane_disabled")
                continue
            if _no_signal_gate.lane_blocked(self._signal_strategy_name, _updown_tf, action):
                _bump_skip("no_signal_gate")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason="no_signal_gate",
                    yes_price=yes_price,
                    context={"gate": "no_signal_per_lane"},
                )
                continue
            # 2026-07-03 (Codex-amended): 5m BUY_YES first-~75s skip band. Lower
            # bound on latency-adjusted _eval_left, upper bound on RAW _mins_left
            # so pre-open candidates are never pulled into the band by latency.
            _sz_lo = self.config.get(f"buy_yes_{_updown_tf}_skip_mins_lo")
            _sz_hi = self.config.get(f"buy_yes_{_updown_tf}_skip_mins_hi")
            if (
                action == "BUY_YES"
                and _sz_lo is not None
                and _sz_hi is not None
                and float(_sz_lo) <= _eval_left
                and _mins_left < float(_sz_hi)
            ):
                _bump_skip(f"buy_yes_{_updown_tf}_timing_deadzone")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason=f"buy_yes_{_updown_tf}_timing_deadzone",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={
                        "eval_mins_left": float(_eval_left),
                        "mins_left": float(_mins_left),
                    },
                )
                continue
            if _eval_left < lane_policy.entry_window_min or _eval_left > lane_policy.entry_window_max:
                _bump_skip("lane_entry_window")
                log_window_reject(
                    self.full_config, market=market, strategy=self._signal_strategy_name,
                    window=_updown_tf, side=market_allowed_side, action=action,
                    side_source=locals().get("side_source") or locals().get("_reject_side_source"),
                    eval_mins_left=_eval_left,
                    entry_window_min=lane_policy.entry_window_min,
                    entry_window_max=lane_policy.entry_window_max,
                    yes_price=yes_price, est_prob=estimated_prob, edge=locals().get("edge"),
                )
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
            _direction_guard = self._eth_direction_guard_reason(
                window_size=_updown_tf,
                decision=direction_decision,
                yes_price=yes_price,
                btc_htf_bias=btc_htf_bias,
                btc_1h_regime=btc_1h_regime if btc_ta else None,
                alt_h1_trend=mtt.h1_trend,
                rsi_14=float(eth.rsi_14 or 0.0),
            )
            if _direction_guard:
                _bump_skip(_direction_guard)
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason=_direction_guard,
                    yes_price=yes_price,
                    est_prob_up=estimated_prob,
                    htf_bias=primary_htf_bias,
                    stage="direction_guard",
                    context={
                        "btc_htf_bias": btc_htf_bias,
                        "btc_1h_regime": btc_1h_regime if btc_ta else None,
                        "alt_1h_trend": mtt.h1_trend,
                        "rsi_14": float(eth.rsi_14 or 0.0),
                        "side_source": side_source,
                        "raw_est_prob": float(raw_est_prob),
                    },
                )
                continue
            if (
                self._btc_trade_inputs_enabled()
                and self._btc_1h_regime_gates.get("enabled", False)
                and btc_ta
            ):
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
            if self._btc_trade_inputs_enabled() and self.block_counter_macro_leg_updown:
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

            # DYNAMIC ADMISSION (2026-07-26 operator GO) — tape-aware min_edge delta,
            # same as the sol_macro path. LOOSEN a winning+green lane (frequency),
            # TIGHTEN a losing never-green lane. Guarded; 0.0 when off/unknown/error.
            try:
                _tape_adm = get_tape_admission_delta("eth_macro", _updown_tf, lane_side)
                if _tape_adm:
                    effective_min_edge = max(0.0, effective_min_edge + _tape_adm)
                    reason_parts.append(f"tape_adm={_tape_adm:+.3f}")
            except Exception:
                pass

            # 2026-07-26 (#3/#4 candidate-time TAPE FRESHNESS): graded edge+size penalty
            # for a stale/exhausted entry (own-TF MACD rolled over against the side or
            # RSI-exhausted). Generic, never hard-blocks. This is the 1h stale-bias
            # cover (#4) tape_arbitration lacks. size_mult carried to final_size below.
            _freshness_size_mult = 1.0
            try:
                _fresh_macd = (
                    eth.macd_5m if _updown_tf == "5m"
                    else eth.macd_1h if _updown_tf == "1h"
                    else eth.macd_15m
                )
                _fresh = compute_freshness_penalty(
                    action=action,
                    own_macd=_fresh_macd,
                    rsi=getattr(eth, "rsi_14", None),
                    cfg=self.full_config.get("tape_freshness", {}),
                )
                if _fresh.get("edge_add"):
                    effective_min_edge += float(_fresh["edge_add"])
                _freshness_size_mult = float(_fresh.get("size_mult", 1.0) or 1.0)
                if _fresh.get("staleness"):
                    reason_parts.append(
                        f"tape_fresh={_fresh['staleness']:.2f}"
                        f"(e+{float(_fresh.get('edge_add', 0.0)):.3f},"
                        f"x{_freshness_size_mult:.2f})"
                    )
            except Exception:
                _freshness_size_mult = 1.0

            if action == "BUY_YES":
                _lane_meta = build_lane_metadata(
                    strategy=self._signal_strategy_name,
                    window_size=_updown_tf,
                    action=action,
                    direction=direction,
                    entry_leg="YES",
                    side_source=side_source,
                    signal_reason=" | ".join(str(r) for r in reason_parts if r),
                    primary_htf_bias=primary_htf_bias,
                    alt_htf_bias=mtt.h1_trend,
                    btc_1h_regime=btc_1h_regime if btc_ta else None,
                )
                _repair = resolve_buy_yes_lane_repair(
                    strategy_config=self.config,
                    strategy=self._signal_strategy_name,
                    window_size=_updown_tf,
                    action=action,
                    lane_side=lane_side,
                    entry_family=str(_lane_meta.get("entry_family") or ""),
                    estimated_prob=estimated_prob,
                    yes_price=yes_price,
                    edge=edge,
                    effective_min_edge=effective_min_edge,
                    oracle_basis_bps=eth.oracle_basis_bps,
                )
                if _repair.matched:
                    estimated_prob = _repair.estimated_prob
                    edge = _repair.edge
                    effective_min_edge = _repair.effective_min_edge
                    reason_parts.append(_repair.reason_token)
                    entry_policy_meta["buy_yes_lane_repair"] = {
                        "rule": _repair.rule_name,
                        "lane_key": _repair.lane_key,
                        "probability_haircut": _repair.probability_haircut,
                        "min_edge_add": _repair.min_edge_add,
                        "oracle_basis_min_edge_add": _repair.oracle_basis_min_edge_add,
                    }

            # 2026-07-26 (operator GO "A" + Codex): ETH BUY_YES overbought soft
            # ceiling — symmetric twin of eth_buy_no_rsi_min_floor. eth 1h longs
            # bought overbought bleed: live RSI>65 = 21% WR / -$37.7 over n24
            # (RSI 85+ = 0% WR / -$14.8), while RSI<=65 = 48% WR / +$18.2. SOFT
            # (require extra min_edge, NOT a hard block) so a rare fat-edge
            # overbought long still admits. 1h ONLY — eth 5m>65 WINS (+$63) and
            # eth 15m>65 is ~break-even; scoping to 1h avoids clobbering them.
            # Runs AFTER buy_yes_lane_repair so the repair's effective_min_edge
            # overwrite can't wipe the penalty. Unset key => no-op.
            _by_rsi_max = self.config.get("eth_buy_yes_rsi_max_floor")
            if (
                action == "BUY_YES"
                and _updown_tf == "1h"
                and _by_rsi_max is not None
            ):
                # All float coercions guarded (Codex 2026-07-26): a malformed
                # TA RSI or config value must no-op, never abort the scan.
                try:
                    _eth_rsi_by = getattr(eth, "rsi_14", None)
                    if _eth_rsi_by is not None and float(_eth_rsi_by) > float(_by_rsi_max):
                        try:
                            _by_add = float(
                                self.config.get("eth_buy_yes_rsi_max_edge_add", 0.06)
                            )
                        except (TypeError, ValueError):
                            _by_add = 0.06
                        effective_min_edge += _by_add
                        reason_parts.append(
                            f"eth_by_overbought_edge_add={_by_add:+.3f}"
                        )
                except (TypeError, ValueError):
                    pass

            # 2026-07-31 Phase-1: apply the consolidated BUY_NO pocket-RSI soft add (parity
            # with sol_macro; a no-op today since eth sets no buy_no_{tf}_pocket_rsi_min, but
            # wired so an eth pocket floor works without another code change).
            _pkt_soft_applied = False
            if rsi_min_edge_add > 0.0:
                effective_min_edge = float(effective_min_edge) + rsi_min_edge_add
                _pkt_soft_applied = True
                reason_parts.append(f"pocket_rsi_soft={rsi_min_edge_add:.3f}")

            _hold_ts = self._ai_hold_cache.get(market.id, 0)
            _hold_age = time.time() - _hold_ts
            _ai_override_bar = max(lane_policy.ai_override_min_edge, lane_policy.min_edge)
            if _hold_age < self.ai_hold_veto_ttl_sec and edge < _ai_override_bar:
                _bump_skip("ai_hold_veto")
                continue

            _ai_updown_observe_only = bool(
                self.config.get("ai_updown_observe_only", False)
            )
            if (
                edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and _timing_window_open
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and self.ai_agent.is_available()
                and ai_calls < self.max_ai_calls_per_scan
                # 5m never calls AI — quant only. AI tiebreaker is 15m/1h.
                and _updown_tf in self._DECISION_GATE_WINDOWS
                and not self._admit_marginal_quant_short(
                    edge, action, _timing_window_open, window=_updown_tf
                )
            ):
                _window = _updown_tf
                if btc_ta:
                    _btc_ai_block = ""
                else:
                    _btc_ai_block = ""
                ai_context = (
                    f"{market.description}\n\n"
                    f"=== ETH UPDOWN CONTEXT ({_window}) ===\n"
                    f"ETH Price: ${eth_price:,.2f} | YES={yes_price:.3f} | action={action}\n"
                    f"PRIMARY_ETH_HTF={primary_htf_bias} | side={market_allowed_side}({side_source}) | Quant edge={edge:.4f} "
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
                # Synchronous (no async enqueue/expire). 15m/1h only. FAIL-CLOSED:
                # below-threshold marginal extras only trade WITH AI blessing.
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
                    veto_only=True,
                )
                ai_calls += 1
                self._log_decision_layer(
                    market=market, window=_window, quant_action=action,
                    ai_decision=ai_decision, lane="marginal",
                    fail_open_reason=None if ai_decision is not None else "timeout",
                    entry_context={
                        "lane_id": ai_lane_id,
                        "yes_price": yes_price,
                        "quant_edge": edge,
                        "quant_confidence": confidence,
                        "quant_threshold": effective_min_edge,
                        "raw_est_prob": raw_est_prob,
                        "estimated_prob": estimated_prob,
                    },
                )
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
                    if not _ai_updown_observe_only:
                        _bump_skip("ai_decision_timeout")
                        _log_ai_veto("ai_decision_timeout")
                        continue
                ai_used = True
                ai_analysis = (
                    ai_decision.direct_analysis if ai_decision is not None else None
                )
                if ai_decision is not None and not ai_decision.approved:
                    if not _ai_updown_observe_only:
                        _bump_skip(f"ai_decision_{ai_decision.reason}")
                        _log_ai_veto(f"ai_decision_{ai_decision.reason}", ai_reason=str(ai_decision.reason))
                        if ai_decision.reason in {"direct_ai_hold", "shadow_portfolio_hold"}:
                            self._ai_hold_cache[market.id] = time.time()
                        continue
                if ai_analysis is None:
                    if not _ai_updown_observe_only:
                        _bump_skip("ai_none")
                        _log_ai_veto("ai_none")
                        continue
                # veto-only marginal pass: central layer cleared this (no confident
                # opposition) — admit on quant terms, skip the redundant local re-gate.
                _mpass = (
                    ai_decision is not None
                    and ai_decision.reason in ("direct_ai_marginal_pass", "direct_ai_marginal_confirm")  # 2026-07-03: accept new fail-closed confirm reason
                )
                if (
                    ai_decision is not None
                    and not _mpass
                    and ai_decision.action == "HOLD"
                ):
                    if not _ai_updown_observe_only:
                        self._ai_hold_cache[market.id] = time.time()
                        _bump_skip("ai_hold")
                        _log_ai_veto("ai_hold")
                        continue
                if (
                    ai_decision is not None
                    and not _mpass
                    and not ai_recommendation_supports_action(ai_decision.action, action)
                ):
                    if not _ai_updown_observe_only:
                        _bump_skip("ai_veto")
                        _log_ai_veto("ai_veto", ai_action=str(ai_decision.action))
                        continue
                if (
                    ai_decision is not None
                    and not _mpass
                    and ai_decision.confidence < self.ai_confidence_threshold
                ):
                    if not _ai_updown_observe_only:
                        _bump_skip("ai_low_confidence")
                        _log_ai_veto("ai_low_confidence", ai_confidence=float(ai_decision.confidence))
                        continue
                ai_edge = float(ai_decision.edge or 0.0) if ai_decision is not None else 0.0
                if ai_decision is not None and not _mpass and ai_edge <= 0:
                    if not _ai_updown_observe_only:
                        _bump_skip("ai_nonpositive_edge")
                        _log_ai_veto("ai_nonpositive_edge", ai_edge=ai_edge)
                        continue
                if not _ai_updown_observe_only and ai_decision is not None:
                    edge = max(edge, ai_edge)
                    confidence = max(confidence, ai_decision.confidence)
                    reason_parts.append(f"ai_decision={ai_decision.source}")
                research_plan = None
                if (
                    ai_analysis is not None
                    and
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
                    ai_decision is not None
                    and ai_analysis is not None
                    and
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
                # 2026-07-28: AI is OBSERVE-ONLY on eth (config ai_updown_observe_only), so its
                # timing window must NOT block a marginal entry — observing != enforcing, matching
                # the open-window admit path. Same fix as the sol-family gate; eth duplicates the
                # scan loop so it needs its own copy.
                not _ai_updown_observe_only
                and edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and _updown_tf in self._DECISION_GATE_WINDOWS
                and not _timing_window_open
            ):
                _bump_skip("ai_window_closed")
                continue

            _sample("est_prob_up", est_prob_up)
            _sample("edge", edge)
            # 2026-06-02: admit sub-threshold marginal BUY_NO on quant terms when the
            # AI decision layer is OFF (no-op tiebreaker can't rescue them). The upstream
            # AI block is also skipped for these so they reach here, not ai_none.
            _admit_marginal_no_ai = self._admit_marginal_quant_short(
                edge, action, _timing_window_open, window=_updown_tf
            )
            # 2026-08-10 RSI-FADE min-edge EXEMPTION (port of sol_macro). Placed AFTER all
            # effective_min_edge mutations (tape_adm/repair/rsi_min_edge_add) and BEFORE the
            # fee-aware net_edge calc so the floored edge flows into _net_edge. est_prob is
            # unusable on the fade side => size on flat fade edge, drop the min_edge bar.
            if _is_rsi_fade:
                _rf_cfg = (self.full_config.get("risk", {}) or {}).get("rsi_fade", {}) or {}
                _rf_flat_edge = float(_rf_cfg.get("flat_edge", 0.05) or 0.05)
                effective_min_edge = 0.0
                if edge < _rf_flat_edge:
                    edge = _rf_flat_edge
            # 2026-08-13 SIDE-POLICY exemption (port of sol_macro; Codex q2 option (c)).
            # When the MARKET picks the side, est_prob describes the side the resolver
            # wanted, not the one we are taking, so the model edge is ~0/negative by
            # construction and lane_min_edge + the conviction floors would reject nearly
            # every favorite candidate. Size on the flat edge rather than fabricating a
            # probability. Byte-identical under direction.side_policy: resolver.
            if _side_policy_active and _side_policy_flat_edge > 0.0:
                effective_min_edge = 0.0
                if edge < _side_policy_flat_edge:
                    edge = _side_policy_flat_edge
            # 2026-07-29 FEE-AWARE GATE (mirror of sol_macro): subtract the venue taker-fee
            # hurdle so admission is net-of-fee, and drop the marginal rescue if the net edge
            # no longer clears the marginal floor. No-op when fee_aware_edge.enabled=false.
            _fee_hurdle = fee_aware_edge_hurdle(self.config, yes_price)
            _net_edge = edge - _fee_hurdle
            if _admit_marginal_no_ai and _fee_hurdle > 0.0 and _net_edge < float(
                self.config.get("ai_updown_marginal_min_edge", 0.03)
            ):
                _admit_marginal_no_ai = False
            # EXPERIMENT (2026-08-03, operator-directed): drop the min_edge floor to test whether
            # it is an OLYMPUS PRE-FEE relic (edge is pre-fee; the uniform 0.09 bar was calibrated
            # at fee=$0) strangling frequency. Global flag, hot-reloadable, ONE-flip reversible.
            # 0.0 keeps only the breakeven floor (net-of-fee edge must still be >= 0).
            if self.full_config.get("experiment_disable_min_edge_gate", False):
                effective_min_edge = 0.0
            if _net_edge < effective_min_edge and not _admit_marginal_no_ai:
                if rsi_soft_penalty > 0 and (_net_edge + rsi_soft_penalty) >= effective_min_edge:
                    _bump_skip("edge_after_penalty_below_threshold")
                _vetoed = bool(getattr(self, "_last_calibration_vetoed", False))
                _reject_reason = "beta_vetoed" if _vetoed else "lane_min_edge"
                _bump_skip(_reject_reason)
                log_rejected_candidate(
                    strategy=self._signal_strategy_name,
                    window=_updown_tf,
                    side=market_allowed_side,
                    action=action,
                    reason=_reject_reason,
                    market=market,
                    yes_price=yes_price,
                    est_prob_up=estimated_prob,
                    htf_bias=primary_htf_bias,
                    context={
                        "edge": round(float(edge), 6),
                        "effective_min_edge": round(float(effective_min_edge), 6),
                        "fee_hurdle": round(float(_fee_hurdle), 6),
                        "net_edge": round(float(_net_edge), 6),
                        "raw_est_prob": round(float(raw_est_prob), 6),
                        "estimated_prob": round(float(estimated_prob), 6),
                        "confidence": round(float(confidence), 6),
                        "side_source": side_source,
                        "rsi_soft_penalty": round(float(rsi_soft_penalty), 6),
                        "beta_vetoed": _vetoed,
                        "calibration_lane_id": getattr(self, "_last_calibration_lane_id", ""),
                        **build_market_context(
                            asset_spot=eth.current_price,
                            btc_spot=corr.btc_price,
                            rsi_14=eth.rsi_14,
                            atr_14=eth.atr_14,
                            macd_hist_5m=getattr(getattr(eth, "macd_5m", None), "histogram", None),
                            macd_hist_15m=getattr(getattr(eth, "macd_15m", None), "histogram", None),
                            macd_hist_1h=getattr(getattr(eth, "macd_1h", None), "histogram", None),
                            rsi_5m=getattr(getattr(eth, "tf_5m", None), "rsi_14", None),
                            rsi_15m=getattr(getattr(eth, "tf_15m", None), "rsi_14", None),
                            rsi_1h=getattr(getattr(eth, "tf_1h", None), "rsi_14", None),
                        ),
                    },
                    probe_variants=build_threshold_probe_variants(
                        metric_name="min_edge",
                        observed_value=float(edge),
                        baseline_threshold=float(effective_min_edge),
                    ),
                    policy_version="lane_min_edge_v1",
                    stage=_reject_reason,
                )
                if action == "BUY_NO":
                    _skip_reason = (
                        "edge_after_penalty_below_threshold"
                        if rsi_soft_penalty > 0 and (edge + rsi_soft_penalty) >= effective_min_edge
                        else _reject_reason
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

            if (
                self._requires_ai_for_lane("default")
                and not ai_used
                # 5m bypasses the AI gate entirely (latency >> entry window).
                and _updown_tf in self._DECISION_GATE_WINDOWS
            ):
                # Synchronous, FAIL-OPEN gate. AI off / unavailable / over budget /
                # timed out => take the quant trade. Can only REJECT on a real
                # verdict, never drop a trade to latency.
                if not self.config.get("use_ai", True) or not self.ai_agent.is_available():
                    self._log_decision_layer(
                        market=market, window=_updown_tf, quant_action=action,
                        ai_decision=None, fail_open_reason="ai_unavailable",
                        entry_context={
                            "yes_price": yes_price,
                            "quant_edge": edge,
                            "quant_confidence": confidence,
                            "quant_threshold": effective_min_edge,
                            "raw_est_prob": raw_est_prob,
                            "estimated_prob": estimated_prob,
                        },
                    )
                    reason_parts.append("ai_decision=fail_open_unavailable")
                elif ai_calls >= self.max_ai_calls_per_scan:
                    self._log_decision_layer(
                        market=market, window=_updown_tf, quant_action=action,
                        ai_decision=None, fail_open_reason="ai_call_limit",
                        entry_context={
                            "yes_price": yes_price,
                            "quant_edge": edge,
                            "quant_confidence": confidence,
                            "quant_threshold": effective_min_edge,
                            "raw_est_prob": raw_est_prob,
                            "estimated_prob": estimated_prob,
                        },
                    )
                    reason_parts.append("ai_decision=fail_open_budget")
                else:
                    ai_context_default = (
                        f"{market.description}\n\n"
                        f"=== ETH ENFORCED ENTRY CONTEXT ({_updown_tf}) ===\n"
                        f"Action={action} YES={yes_price:.3f} edge={edge:.4f} "
                        f"threshold={effective_min_edge:.4f} confidence={confidence:.2f}\n"
                        f"ETH price=${eth.current_price:,.2f} RSI={eth.rsi_14:.1f} "
                        f"ETH 1H={mtt.h1_trend} ETH 15m={mtt.m15_trend} ETH 5m={mtt.m5_trend}\n"
                        f"PRIMARY_ETH_HTF={primary_htf_bias} side_source={side_source}\n"
                        f"Oracle basis={oracle_validation.basis_bps if oracle_validation else 'n/a'} "
                        f"freshness={oracle_validation.freshness_sec if oracle_validation else 'n/a'}\n\n"
                        f"=== MARKET ===\n{format_market_metadata(market)}\n\n"
                        "Answer with BUY_YES, BUY_NO, or HOLD."
                    )
                    ai_lane_id = str(
                        build_lane_metadata(
                            strategy=self._signal_strategy_name,
                            window_size=_updown_tf,
                            action=action,
                            direction=("down" if action == "BUY_NO" else "up"),
                            entry_leg=("NO" if action == "BUY_NO" else "YES"),
                            side_source="default",
                            ai_used=True,
                            reason="ai_decision",
                            signal_reason="ai_decision_default",
                            htf_bias=btc_htf_bias,
                            primary_htf_bias=primary_htf_bias,
                            alt_htf_bias=mtt.h1_trend,
                            btc_1h_regime=btc_1h_regime if btc_ta else None,
                        ).get("lane_id")
                        or ""
                    )
                    # Direct synchronous call with bounded timeout — no async
                    # enqueue/expire broker (that path silently dropped trades).
                    ai_decision = await self._evaluate_trade_decision_with_timeout(
                        market_question=market.question,
                        market_description=ai_context_default,
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
                        require_shadow_portfolio=self._requires_shadow_for_lane("default"),
                    )
                    ai_calls += 1
                    if ai_decision is None:
                        # FAIL-OPEN on timeout — do NOT drop the trade.
                        self._log_decision_layer(
                            market=market, window=_updown_tf, quant_action=action,
                            ai_decision=None, fail_open_reason="timeout",
                            entry_context={
                                "lane_id": ai_lane_id,
                                "yes_price": yes_price,
                                "quant_edge": edge,
                                "quant_confidence": confidence,
                                "quant_threshold": effective_min_edge,
                                "raw_est_prob": raw_est_prob,
                                "estimated_prob": estimated_prob,
                            },
                        )
                        reason_parts.append("ai_decision=fail_open_timeout")
                    else:
                        ai_used = True
                        self._log_decision_layer(
                            market=market, window=_updown_tf, quant_action=action,
                            ai_decision=ai_decision,
                            entry_context={
                                "lane_id": ai_lane_id,
                                "yes_price": yes_price,
                                "quant_edge": edge,
                                "quant_confidence": confidence,
                                "quant_threshold": effective_min_edge,
                                "raw_est_prob": raw_est_prob,
                                "estimated_prob": estimated_prob,
                            },
                        )
                        if ai_decision.shadow_result is not None:
                            shadow_pipeline_calls += 1
                            if ai_decision.shadow_result.get("ok"):
                                shadow_pipeline_ok += 1
                        if not ai_decision.approved:
                            _bump_skip(f"ai_decision_{ai_decision.reason}")
                            if ai_decision.reason in {"direct_ai_hold", "shadow_portfolio_hold"}:
                                self._ai_hold_cache[market.id] = time.time()
                            continue
                        ai_edge = float(ai_decision.edge or 0.0)
                        if ai_edge <= 0:
                            _bump_skip("ai_nonpositive_edge_default")
                            continue
                        edge = max(edge, ai_edge)
                        confidence = max(confidence, ai_decision.confidence)
                        reason_parts.append(f"ai_decision={ai_decision.source}")

            # Centered-price gate: near 50/50 entries need a higher edge bar.
            # 2026-05-22: BTC catalyst requirement (center_price_requires_catalyst)
            # REMOVED — was gating ETH admission on BTC spike/lag. The higher
            # min-edge bar for centered prices remains (alt-native edge check).
            if self.center_price_band > 0:
                _is_centered = abs(yes_price - 0.50) <= self.center_price_band
                if _is_centered:
                    # 2026-08-10 RSI-fade exemption (port of sol_macro): the fade lives in the
                    # CENTERED coinflip band by construction; min_edge_when_centered would
                    # re-reject the exact candidates the fade targets. Skip for faded.
                    # side policy trades the centred band BY DESIGN (0.45-0.55).
                    _center_min_edge = 0.0 if (_is_rsi_fade or _side_policy_active) else max(effective_min_edge, self.min_edge_when_centered)
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
                # Floor the NO price though: NO<0.20 wins ~5% held-to-resolution
                # across every asset (n~8k ghost), −$97 realized. Block cheap NO.
                _buy_no_min_no = float(self.config.get(f"buy_no_min_no_price_{_updown_tf}", self.config.get("buy_no_min_no_price", 0.20)))  # 2026-07-16 eth 5m|down cap (operator+Codex GO): per-window override; buy_no_min_no_price_5m=0.49 => yes<=0.51, cuts high-yes shorts (-$16/25%). 15m/1h fall back 0.20.
                _entry_price_bad = (
                    yes_price < lane_policy.entry_price_min
                    or yes_price > (1.0 - _buy_no_min_no)
                )
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
            # 2026-07-21 TRUE-KELLY conviction sizing (operator ORDER) — mirror of the
            # sol_macro change (eth duplicates the scan loop). Legacy size_from_edge
            # clamped the sizing edge to max_edge_updown (0.15 here), flattening
            # high-conviction trades. size_binary_position scales size with real
            # win-prob + price odds. win_prob = our_price + edge (exact per side).
            # Flag-gated (default on). TypeError = kelly_sizer version-skew guard.
            _kf_kw = dict(window=_updown_tf) if is_updown else {}
            if bool(self.config.get("use_true_kelly_sizing", True)):
                _our_price = yes_price if action == "BUY_YES" else (1.0 - yes_price)
                _win_prob = min(0.99, max(0.01, float(_our_price) + float(edge)))
                # 2026-07-22 calibration-correction apply hook (flag-gated, default OFF).
                # ETH port of the sol_macro hook — eth|15m|up is the primary corrected lane.
                # Sizing-only; matches the shadow that earned the apply-gate. Fail-safe.
                if is_updown and bool(self.config.get("apply_calibration_correction", False)):
                    try:
                        from src.analysis.calibration_apply import corrected_win_prob as _cwp
                        _cal_key = f"{str(self._signal_strategy_name).replace('_macro', '')}|{_updown_tf}|{'up' if action == 'BUY_YES' else 'down'}"
                        _win_prob = _cwp(_win_prob, _cal_key)
                    except Exception:
                        pass
                try:
                    raw_size = self.kelly_sizer.size_binary_position(
                        self._signal_strategy_name, bankroll, _win_prob, _our_price, **_kf_kw
                    )
                except TypeError:
                    raw_size = self.kelly_sizer.size_binary_position(
                        self._signal_strategy_name, bankroll, _win_prob, _our_price
                    )
            else:
                try:
                    raw_size = self.kelly_sizer.size_from_edge(
                        self._signal_strategy_name, bankroll, sizing_edge, **_kf_kw
                    )
                except TypeError:
                    raw_size = self.kelly_sizer.size_from_edge(
                        self._signal_strategy_name, bankroll, sizing_edge
                    )
            if (
                self._btc_trade_inputs_enabled()
                and self._btc_1h_regime_gates.get("enabled", False)
                and btc_ta
            ):
                raw_size *= self._regime_size_mult(btc_1h_regime)
            if (
                self._btc_trade_inputs_enabled()
                and getattr(corr, "degraded", False)
                and not self.skip_on_degraded_correlation
            ):
                raw_size *= self.degraded_correlation_size_multiplier
            if lane_policy.size_multiplier > 0:
                raw_size *= lane_policy.size_multiplier
            final_size = self.exposure_manager.scale_size(raw_size)
            # 2026-07-13 restart passenger (operator GO, Codex conditional-GO honored):
            # per-lane max-notional lift — tier floor=cap flattens winners; when
            # lane_max_notional_{tf}_{side} is set, let this lane size up to
            # min(kelly raw, lane max). Guards: only lifts an already-admitted size,
            # and NEVER during the MINIMAL loss-streak tier (hard risk brake stays).
            if final_size >= 0.5 and is_updown:
                _cur_tier = getattr(self.exposure_manager, "_current_tier", None)
                _tier_val = str(getattr(_cur_tier, "value", ""))
                if _tier_val != "PAUSED":
                    _ln_side = 'up' if action == 'BUY_YES' else 'down'
                    _ln_max = float(self.config.get(f"lane_max_notional_{_updown_tf}_{_ln_side}", 0.0) or 0.0)
                    # lift toward kelly ask; still skips MINIMAL
                    if _tier_val != "MINIMAL" and _ln_max > 0:
                        final_size = max(final_size, min(raw_size, _ln_max))
                    # 2026-07-13 operator ORDER (Codex GO): winner-lane floors SURVIVE the
                    # MINIMAL loss-streak tier — the $5 brake was shaving known winners
                    # (btc 1h expiry wins at $5; xrp 5m engine at $5). Per-lane opt-in via
                    # lane_min_notional_ignores_minimal_{tf}_{side}. PAUSED blocks all.
                    _lnf = float(self.config.get(f"lane_min_notional_{_updown_tf}_{_ln_side}", 0.0) or 0.0)
                    _lnf_hard = bool(self.config.get(f"lane_min_notional_ignores_minimal_{_updown_tf}_{_ln_side}", False))
                    if _lnf > 0 and (_tier_val != "MINIMAL" or _lnf_hard):
                        final_size = max(final_size, _lnf)
                    # lane_max is a HARD CAP last (per-lane downsize lever, e.g. sol 5m $15)
                    if _ln_max > 0:
                        final_size = min(final_size, _ln_max)
            # #3 tape-freshness SIZE penalty (computed at the min_edge block above):
            # de-size a stale/exhausted entry rather than block it. Applied last.
            try:
                final_size *= float(_freshness_size_mult)
            except Exception:
                pass
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
            resolver_meta = {
                "conflict_type": direction_decision.conflict_type,
                "resolver_path": direction_decision.resolver_path,
                "htf_side": direction_decision.htf_side,
                "quant_side": direction_decision.quant_side,
                "momentum_side": direction_decision.momentum_side,
            }
            # eth_macro does not compute a composite/convergence object (unlike
            # sol_macro); define these so the telemetry fields are honestly None
            # instead of an F821 landmine guarded by a fragile `in locals()` check.
            entry_convergence_score = None
            entry_composite_score = None
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
                condition_id=getattr(market, "condition_id", None),
                market_slug=getattr(market, "slug", None),
                outcome_label_yes=getattr(market, "outcome_label_yes", None),
                outcome_label_no=getattr(market, "outcome_label_no", None),
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
                primary_htf_bias=primary_htf_bias,
                alt_htf_bias=mtt.h1_trend,
                btc_htf_bias=btc_htf_bias,
                btc_1h_regime=btc_1h_regime,
                entry_policy=entry_policy_meta,
                window_size=_updown_tf,
                hour_utc=datetime.now(timezone.utc).hour,
                est_prob=round(estimated_prob, 4),
                raw_est_prob=round(raw_est_prob, 4),
                rsi=round(eth.rsi_14, 1),
                corr_1h=round(corr.correlation_1h, 4),
                side_source=side_source,
                **resolver_meta,
                convergence_score=(
                    round(float(entry_convergence_score), 4)
                    if entry_convergence_score is not None
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
                        if entry_composite_score is not None
                        else None
                    ),
                    "convergence_score": (
                        round(float(entry_convergence_score), 4)
                        if entry_convergence_score is not None
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
                    **self._build_alt_indicator_snapshot(
                        eth,
                        correlation=corr,
                        composite_score=entry_composite_score,
                        convergence_score=entry_convergence_score,
                        entry_volatility=getattr(conditions, "volatility", 0.0),
                    ),
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
            f"ETH Macro SCAN_DIAG base_side={locals().get('market_allowed_side')} "
            f"side_sources={side_source_counts} eth_1H_trend={mtt.h1_trend} "
            f"enforce_alt_1h={self.enforce_alt_1h_alignment} markets={len(eth_markets)} signals={len(signals)} "
            f"skips_top6={_skip_top}"
        )
        self.last_scan_stats = {
            "enabled": True,
            "signals": len(signals),
            "markets_considered": len(eth_markets),
            "asset_regime": _asset_regime.get_state(
                getattr(self.sol_service, "alt_symbol", None)
            ),
            "btc_1h_regime": btc_1h_regime,
            "btc_1h_regime_gates_enabled": bool(
                self._btc_1h_regime_gates.get("enabled", False)
            ),
            "btc_htf_bias": btc_htf_bias,
            "btc_htf_vote_details": dict(btc_htf_details) if btc_htf_details else None,
            "allowed_side": locals().get("market_allowed_side"),
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
        # 2026-07-07 Fix A: near-0.50 overconfidence sit-out. Stated edge is inverted
        # near coin-flip prices; entry 0.49-0.51 & edge>=0.12 = densest clean-loss
        # cohort (34% WR, -$161/wk). Config-gated per strategy; default OFF (inert
        # until overconfidence_sitout.enabled=true).
        _oc = self.config.get("overconfidence_sitout") or {}
        if signals and bool(_oc.get("enabled", False)):
            _oc_band = float(_oc.get("band", 0.01))
            _oc_min_edge = float(_oc.get("min_edge", 0.12))
            _oc_kept = []
            for _oc_sig in signals:
                try:
                    _pp = float(getattr(_oc_sig, "price", 0.5) or 0.5)
                    _ee = float(getattr(_oc_sig, "edge", 0.0) or 0.0)
                except Exception:
                    _oc_kept.append(_oc_sig)
                    continue
                if abs(_pp - 0.5) <= _oc_band and _ee >= _oc_min_edge:
                    logger.info(
                        "  overconfidence sit-out (near-0.50 high-edge): price=%.3f edge=%.3f action=%s",
                        _pp, _ee, getattr(_oc_sig, "action", "?"),
                    )
                    continue
                _oc_kept.append(_oc_sig)
            _oc_dropped = len(signals) - len(_oc_kept)
            signals = _oc_kept
            if _oc_dropped:
                # 2026-07-11: surface sit-out drops in ops telemetry (was log-line only)
                # and keep the stats dict's signal count consistent with the post-sitout
                # reality (Codex: 'signals' was assembled pre-sitout). Never raises.
                try:
                    _st = self.last_scan_stats
                    if isinstance(_st, dict):
                        if "signals" in _st:
                            _st["signals"] = len(_oc_kept)
                        _tsr = _st.get("top_skip_reasons")
                        if isinstance(_tsr, dict):
                            _tsr["overconfidence_sitout"] = _tsr.get("overconfidence_sitout", 0) + _oc_dropped
                except Exception:
                    pass
        def _lane_disabled(sig):
            w = getattr(sig, "window_size", None)
            if not w:
                return False
            a = getattr(sig, "action", None)
            if a == "BUY_NO" and bool(self.config.get(f"disable_buy_no_{w}", False)):
                return True
            if a == "BUY_YES" and bool(self.config.get(f"disable_buy_yes_{w}", False)):
                return True
            return False
        _before_lane_disable = len(signals)
        signals = [sig for sig in signals if not _lane_disabled(sig)]
        if len(signals) != _before_lane_disable:
            logger.info(
                "%s lane-disable filter dropped %d emitted signal(s) (late-flip/native bypass)",
                self._signal_strategy_name,
                _before_lane_disable - len(signals),
            )
            try:
                _st = self.last_scan_stats
                if isinstance(_st, dict) and "signals" in _st:
                    _st["signals"] = len(signals)
            except Exception:
                pass
        # 2026-07-29 per-pulse fan-out dedup (PORTED from sol_macro — eth_macro's
        # duplicated scan loop never had it). eth 15m UP was firing the SAME (window,side)
        # signal on EVERY open future 15m market in one scan (e.g. 4 entries on the
        # 8:15/8:30/8:45/9:00 windows within 6s), betting one coin-flip 4-6x in parallel
        # then never-green-cutting the whole batch. Collapse to ONE market per
        # (window_size, action): the nearest-resolving. Same config key as sol; opt-out,
        # default on.
        if bool(self.config.get("dedup_concurrent_window_signals", True)):
            def _end_ts(sig):
                end = getattr(sig, "end_date", None)
                try:
                    return end.timestamp() if end is not None else None
                except Exception:
                    return None
            best: Dict[tuple, Any] = {}
            passthrough = []
            for s in signals:
                w = getattr(s, "window_size", None)
                a = getattr(s, "action", None)
                ts = _end_ts(s)
                if w is None or a is None or ts is None:
                    passthrough.append(s)  # can't key/rank → keep (fail-safe)
                    continue
                k = (w, a)
                cur = best.get(k)
                if cur is None or ts < cur[0]:
                    best[k] = (ts, s)
            deduped = passthrough + [v[1] for v in best.values()]
            if len(deduped) != len(signals):
                logger.info(
                    "%s fan-out dedup: %d -> %d signals (collapsed duplicate "
                    "(window,side) markets; kept nearest-resolving)",
                    self._signal_strategy_name, len(signals), len(deduped),
                )
                try:
                    _st = self.last_scan_stats
                    if isinstance(_st, dict) and "signals" in _st:
                        _st["signals"] = len(deduped)
                except Exception:
                    pass
            signals = deduped
        return signals
