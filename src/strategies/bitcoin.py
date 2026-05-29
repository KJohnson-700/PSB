"""
Bitcoin Up/Down Strategy v4 — Hierarchical Trend Filter

RULE HIERARCHY (strict — no exceptions):
═══════════════════════════════════════

LAYER 1: HIGHER TIMEFRAME TREND (4H) — THE LAW
  Determined by: Trend Sabre trend direction + price vs Sabre MA + 4H MACD above/below zero
  ► If 4H is BULLISH → ONLY allow LONG signals (BUY_YES on UP markets, BUY_NO on DOWN markets)
  ► If 4H is BEARISH → ONLY allow SHORT signals (BUY_NO on UP, BUY_YES on DOWN)
  ► Signals against the higher TF are DROPPED — no exceptions

LAYER 2: LOWER TIMEFRAME ENTRY CONFIRMATION (15m)
  ► 15m MACD crossover must align with allowed direction
     - Bullish cross (signal line cross up) or rising histogram (red→green) → confirms LONG
     - Bearish cross (signal line cross down) or falling histogram (green→red) → confirms SHORT
  ► Trend Sabre on 4H must agree (buy signals only in bull trend, sell only in bear)

LAYER 3: ENTRY TIMING
  ► Early-candle momentum (first 4 min of 15m candle): if fast spike in the allowed
    direction, that's a strong confirmation signal
  ► Prediction window (9-12 min of 15m candle): preferred entry timing when possible
  ► If NOT in prediction window and no early spike, confidence is reduced

LAYER 4: EDGE CALCULATION
  ► Compare actual BTC price vs market threshold to estimate probability
  ► Technical adjustments from RSI, S/R proximity, tension
  ► AI called ONLY when edge is marginal AND technicals conflict at the lower TF level
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, Field

from src.market.scanner import Market, resolved_updown_window_minutes, updown_timeframe_label
from src.analysis.ai_agent import AIAgent
from src.analysis.ai_decision_broker import (
    PendingDecision as _BrokerPendingDecision,
    STATE_PENDING as _BROKER_STATE_PENDING,
)
from src.analysis.rejected_candidate_log import (
    build_market_context,
    build_threshold_probe_variants,
    log_rejected_candidate,
)
from src.analysis.math_utils import PositionSizer
from src.analysis.btc_price_service import BTCPriceService, TechnicalAnalysis
from src.analysis.btc_1h_regime import (
    DEFAULT_SIZE_MULT,
    classify_btc_1h_sma_regime,
)
from src.analysis.kelly_sizer import KellySizer
from src.analysis.updown_composite_score import OracleValidation, score_updown_candidate
from src.analysis.lane_entry_policy import (
    entry_policy_to_dict,
    resolve_entry_policy_side,
    resolve_lane_entry_policy,
)
from src.execution.exposure_manager import ExposureManager, MarketConditions, ExposureTier
from src.strategies.strategy_config import (
    resolve_enabled_flag,
    resolve_tf_config_value,
    tf_config_override_snapshot,
)
from src.strategies.strategy_ai_context import (
    ai_recommendation_supports_action,
    format_market_metadata,
)
from src.execution.performance_feedback import (
    get_drift_min_edge_mult,
    get_loosen_min_edge_mult,
)
from src.analysis.lane_identity import build_lane_metadata
from src.strategies.btc_updown_5m import (
    btc_5m_hist_gate_reject_reason,
    compute_btc_5m_quant,
    edge_for_action,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BTCDirectionDecision:
    """Canonical BTC side-resolution result for audit and lane metadata."""

    action: str
    direction: str
    effective_side: str
    side_source: str
    conflict_type: str
    resolver_path: str
    htf_side: str
    quant_side: Optional[str] = None
    momentum_side: Optional[str] = None


@dataclass(frozen=True)
class BTCBiasResolution:
    allowed_side: Optional[str]
    side_source: str
    horizon_tf: str
    horizon_bias: str
    slower_biases: Dict[str, str]
    primary_htf_bias: str
    confidence_penalty: float = 0.0


class BitcoinSignal(BaseModel):
    """Represents a signal on a Bitcoin price market."""
    market_id: str = Field(..., description="Market identifier")
    market_question: str = Field(..., description="The market question")
    action: str = Field(..., description="BUY_YES or BUY_NO")
    price: float = Field(..., description="Order price")
    size: float = Field(..., description="Position size in USDC")
    confidence: float = Field(..., description="Strategy confidence")
    edge: float = Field(..., description="Estimated edge")
    token_id_yes: str = Field(..., description="YES token ID")
    token_id_no: str = Field(..., description="NO token ID")
    end_date: Optional[datetime] = Field(None, description="Resolution date")
    direction: str = Field(..., description="UP or DOWN — what this market is betting on")
    btc_threshold: Optional[float] = Field(None, description="BTC price threshold from question")
    btc_current: Optional[float] = Field(None, description="Current BTC price at signal time")
    ai_used: bool = Field(default=False, description="Whether AI was consulted")
    reason: str = Field(default="", description="Why this signal was generated")
    # Coach features — logged to journal extra dict for pattern analysis
    htf_bias: Optional[str] = Field(None, description="HTF bias at entry: BULLISH/BEARISH/NEUTRAL")
    btc_1h_regime: Optional[str] = Field(None, description="BTC 1H vs SMA(20) bucket: BULL/RANGE/BEAR")
    window_size: Optional[str] = Field(None, description="Market window: 5m or 15m")
    hour_utc: Optional[int] = Field(None, description="UTC hour at entry time")
    est_prob: Optional[float] = Field(None, description="Estimated prob of YES at entry (key diagnostic)")
    raw_est_prob: Optional[float] = Field(
        None,
        description="Uncalibrated estimated prob of YES before any lane correction",
    )
    rsi: Optional[float] = Field(None, description="BTC RSI-14 at entry")
    side_source: Optional[str] = Field(None, description="Directional source used for the trade call")
    conflict_type: Optional[str] = Field(None, description="BTC resolver conflict class at entry")
    resolver_path: Optional[str] = Field(None, description="BTC resolver decision path at entry")
    htf_side: Optional[str] = Field(None, description="HTF-implied side before exceptions")
    quant_side: Optional[str] = Field(None, description="Raw-quant-implied side when available")
    momentum_side: Optional[str] = Field(None, description="Short-window momentum side when available")
    oracle_basis_bps: Optional[float] = Field(None, description="Oracle basis at entry when applicable")
    indicator_snapshot: Optional[Dict[str, Any]] = Field(
        None,
        description="Compact indicator state persisted for calibration and forensics",
    )
    entry_policy: Optional[Dict[str, Any]] = Field(
        None,
        description="Resolved lane-specific entry policy used for this signal",
    )
    convergence_score: Optional[float] = Field(None, description="Entry-quality consensus score")
    entry_volatility: Optional[float] = Field(None, description="ATR-style volatility fraction at entry")


# Patterns to detect Bitcoin price markets
BTC_PATTERNS = [
    re.compile(r'\bbitcoin\b', re.IGNORECASE),
    re.compile(r'\bbtc\b', re.IGNORECASE),
    re.compile(r'\bxbt\b', re.IGNORECASE),
]
# Detect 15-minute or 5-minute "Up or Down" markets (pattern matches both)
UPDOWN_PATTERN = re.compile(r'(?:bitcoin|btc|xbt)\s+up\s+or\s+down', re.IGNORECASE)
BTC_UPDOWN_SLUG_PREFIXES = ("btc-updown-", "btc-up-or-down-", "bitcoin-up-or-down-", "xbt-up-or-down-")
NON_BTC_ASSET_TERMS = (
    "solana",
    "ethereum",
    "ether",
    "xrp",
    "ripple",
    "hyperliquid",
    "hype",
)


def _market_window_minutes(market: Market) -> int:
    """Candle window minutes from Gamma metadata or question/slug text."""
    return resolved_updown_window_minutes(market)

PRICE_PATTERNS = [
    re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:m|M)', re.IGNORECASE),  # $1m
    re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:k|K)', re.IGNORECASE),  # $80k
    re.compile(r'\$\s*([\d,]+(?:\.\d+)?)', re.IGNORECASE),            # $80,000
    re.compile(r'([\d,]+(?:\.\d+)?)\s*(?:dollars|usd)', re.IGNORECASE),
]
UP_WORDS = {'above', 'over', 'exceed', 'reach', 'hit', 'surpass', 'higher', 'rise', 'up'}
DOWN_WORDS = {'below', 'under', 'drop', 'fall', 'crash', 'decline', 'lower', 'down'}


class BitcoinStrategy:
    """Bitcoin strategy with strict hierarchical trend filter."""

    def __init__(self, config: Dict[str, Any], ai_agent: AIAgent, position_sizer: PositionSizer,
                 kelly_sizer=None, exposure_manager: ExposureManager = None,
                 ai_broker=None):
        self.full_config = config
        self.config = config.get('strategies', {}).get('bitcoin', {})
        self.ai_agent = ai_agent
        # AI decision broker: when set, the strategy enqueues per-market AI
        # requests instead of awaiting the provider in-line. None → legacy
        # synchronous behavior (used by tests that don't construct a broker).
        self.ai_broker = ai_broker
        self.position_sizer = position_sizer
        self.kelly_sizer = kelly_sizer or KellySizer(config)
        self.btc_service = BTCPriceService()
        self.exposure_manager = exposure_manager or ExposureManager(config)
        self._signal_strategy_name = "bitcoin"
        self.dead_zone_skip_callback = None
        self.buy_no_skip_callback = None
        self.lane_calibrator = None
        if self.exposure_manager:
            self.exposure_manager._on_pause_ai_callback = self._ai_kill_switch_analysis

        self.enabled = resolve_enabled_flag(
            "bitcoin",
            self.config,
            logger=logger,
        )
        self._log_tf_config_overrides()
        self.min_liquidity = self.config.get('min_liquidity', 10000)
        self.min_edge = self.config.get('min_edge', 0.08)
        self.min_edge_5m = float(self._tf_cfg("5m", "min_edge", self.min_edge))
        self.min_edge_15m_neutral = float(
            self.config.get("min_edge_15m_neutral", self.min_edge) or self.min_edge
        )
        self.neutral_15m_min_quant_confidence = float(
            self.config.get("neutral_15m_min_quant_confidence", 0.0) or 0.0
        )
        self.neutral_15m_requires_shadow_portfolio = bool(
            self.config.get("neutral_15m_requires_shadow_portfolio", False)
        )
        self.updown_composite_cfg = dict(self.full_config.get("updown_composite") or {})
        self.neutral_15m_min_composite_score = float(
            self.config.get(
                "neutral_15m_min_composite_score",
                self.updown_composite_cfg.get("btc_neutral_15m_min_score", 0.68),
            )
        )
        self.neutral_rsi_min = float(self.config.get("neutral_rsi_min", 0.0) or 0.0)
        self.neutral_rsi_max = float(self.config.get("neutral_rsi_max", 0.0) or 0.0)
        self.neutral_rsi_extra_min_edge = float(
            self.config.get("neutral_rsi_extra_min_edge", 0.0) or 0.0
        )
        self.ai_confidence_threshold = self.config.get('ai_confidence_threshold', 0.60)
        self.max_ai_calls_per_scan = int(self.config.get("max_ai_calls_per_scan", 8))
        legacy_ai_timeout = float(self.config.get("ai_call_timeout_sec", 15.0) or 15.0)
        self.ai_analysis_timeout_sec = float(
            self.config.get("ai_analysis_timeout_sec", legacy_ai_timeout) or legacy_ai_timeout
        )
        self.ai_decision_timeout_sec = float(
            self.config.get("ai_decision_timeout_sec", legacy_ai_timeout) or legacy_ai_timeout
        )
        self.use_ai_updown_5m = bool(self.config.get("use_ai_updown_5m", False))
        self.kelly_fraction = self.config.get('kelly_fraction', 0.15)
        self.entry_price_min = self.config.get('entry_price_min', 0.15)
        self.entry_price_max = self.config.get('entry_price_max', 0.85)
        self.clear_distance_pct = self.config.get('clear_distance_pct', 0.15)

        # ── AI-hold soft veto ────────────────────────────────────────────────
        # When AI says HOLD on a market, cache that decision for ai_hold_veto_ttl_sec.
        # Any quant-only 5m path entry on the same market within the TTL must meet
        # the higher min_edge_5m_ai_override threshold instead of min_edge_5m.
        # This closes the gap where AI correctly says HOLD but 5m quant fires anyway.
        self._ai_hold_cache: Dict[str, float] = {}  # market_id → timestamp of HOLD
        self.ai_hold_veto_ttl_sec = self.config.get("ai_hold_veto_ttl_sec", 300)     # 5m default
        self.min_edge_5m_ai_override = float(
            self._tf_cfg("5m", "ai_override_min_edge", 0.10)
        )
        self.bias_quant_disagree_override_enabled = bool(
            self.config.get("bias_quant_disagree_override_enabled", False)
        )
        self.bias_quant_disagree_aligned_max_gap_non5m = float(
            self.config.get("bias_quant_disagree_aligned_max_gap_non5m", 0.0) or 0.0
        )
        self.bias_quant_disagree_aligned_min_edge_non5m = float(
            self.config.get("bias_quant_disagree_aligned_min_edge_non5m", 0.0) or 0.0
        )
        self.bias_quant_disagree_size_multiplier = float(
            self.config.get("bias_quant_disagree_size_multiplier", 0.50) or 0.50
        )
        self.macro_event_guard_enabled = bool(self.config.get("macro_event_guard_enabled", False))
        self.macro_event_guard_before_min = int(self.config.get("macro_event_guard_before_min", 30))
        self.macro_event_guard_after_min = int(self.config.get("macro_event_guard_after_min", 30))
        self.macro_event_calendar = self.config.get("macro_event_calendar_utc", [])
        self._btc_1h_regime_gates: Dict[str, Any] = dict(
            self.config.get("btc_1h_regime_gates") or {}
        )
        self._open_positions_snapshot: list = []  # set by main.py before scan_and_analyze

        # Observability snapshot populated each scan (used by ops pulse / dashboard status).
        self.last_scan_stats: Dict[str, Any] = {}

    def _tf_cfg(self, tf: str, key: str, default: Any = None) -> Any:
        return resolve_tf_config_value(
            self.config,
            tf=tf,
            key=key,
            default=default,
        )

    def _log_tf_config_overrides(self) -> None:
        overrides = tf_config_override_snapshot(self.config)
        if overrides:
            logger.info("[config] BTC by_tf overrides: %s", overrides)

    def _min_edge_for_window(self, window_size: str) -> float:
        return float(self._tf_cfg(window_size, "min_edge", self.min_edge))

    def _ai_override_min_edge_for_window(self, window_size: str) -> float:
        return float(
            self._tf_cfg(window_size, "ai_override_min_edge", self.min_edge_5m_ai_override)
        )

    def _max_edge_cap_for_updown(self, *, window_label: str, side: str) -> float:
        base_cap = float(self.config.get("max_edge_updown", 0.12) or 0.12)
        side_key = str(side or "").lower()
        candidates = [
            f"max_edge_updown_{window_label}_{side_key}",
            f"max_edge_updown_{window_label}",
            f"max_edge_updown_{side_key}",
            "max_edge_updown",
        ]
        for key in candidates:
            value = self.config.get(key)
            if value is not None:
                return float(value)
        return base_cap

    @staticmethod
    def _btc_action_for_side(side: str) -> tuple[str, str, str]:
        if side == "SHORT":
            return "BUY_NO", "DOWN", "SHORT"
        return "BUY_YES", "UP", "LONG"

    @staticmethod
    def _btc_momentum_side(mom: Any = None) -> Optional[str]:
        m15_dir = getattr(mom, "m15_direction", None) if mom is not None else None
        m5_dir = getattr(mom, "m5_direction", None) if mom is not None else None
        if m15_dir in ("SPIKE_DOWN", "DRIFT_DOWN") or m5_dir in ("SPIKE_DOWN", "DRIFT_DOWN"):
            return "SHORT"
        if m15_dir in ("SPIKE_UP", "DRIFT_UP") or m5_dir in ("SPIKE_UP", "DRIFT_UP"):
            return "LONG"
        return None

    def _btc_quant_side(self, raw_est_prob: Optional[float]) -> Optional[str]:
        if raw_est_prob is None:
            return None
        try:
            thresh = float(self.config.get("quant_disagree_flip_thresh", 0.48))
        except (TypeError, ValueError):
            thresh = 0.48
        upper = 1.0 - thresh
        raw = float(raw_est_prob)
        if raw < thresh:
            return "SHORT"
        if raw > upper:
            return "LONG"
        return None

    def _resolve_btc_direction(
        self,
        *,
        htf_bias: str,
        allowed_side: str,
        macd_4h: Any,
        reason_parts: list,
        raw_est_prob: Optional[float] = None,
        mom: Any = None,
        current: Optional[BTCDirectionDecision] = None,
    ) -> BTCDirectionDecision:
        """Resolve BTC side in one compact path with explicit suppression rules."""
        htf_side = "LONG" if htf_bias == "BULLISH" else "SHORT" if htf_bias == "BEARISH" else allowed_side
        momentum_side = self._btc_momentum_side(mom)
        counter_trend_disabled = bool(self.config.get("disable_buy_no_counter_trend", False))
        rollover_short = (
            not counter_trend_disabled
            and htf_bias == "BULLISH"
            and not bool(getattr(macd_4h, "histogram_rising", False))
        )

        base_side = "SHORT" if rollover_short else allowed_side
        if rollover_short:
            reason_parts.append("counter_trend=btc_4h_hist_declining")
            base_source = "btc_bull_rollover_countertrend"
            base_conflict = "bullish_htf_rollover_down"
            base_path = "htf_bull__4h_hist_declining"
        else:
            base_source = "btc_htf_bias"
            base_conflict = "none" if base_side == htf_side else "neutral_resolver"
            base_path = f"htf_{htf_bias.lower()}__side_{base_side.lower()}"

        final_side = base_side
        side_source = base_source
        conflict_type = base_conflict
        resolver_path = base_path

        quant_side = self._btc_quant_side(raw_est_prob)
        if quant_side is not None and quant_side != base_side:
            if quant_side == "SHORT" and bool(self.config.get("disable_buy_no_counter_trend", False)):
                pass
            elif quant_side == "LONG" and bool(self.config.get("disable_buy_yes", False)):
                pass
            else:
                require_mom = bool(self.config.get("quant_flip_require_momentum_confirm", True))
                if require_mom and mom is not None and momentum_side != quant_side:
                    if quant_side == "SHORT":
                        reason_parts.append(f"quant_flip_suppressed=no_down_momentum(raw={float(raw_est_prob):.3f})")
                    else:
                        reason_parts.append(f"quant_flip_suppressed=no_up_momentum(raw={float(raw_est_prob):.3f})")
                else:
                    try:
                        thresh = float(self.config.get("quant_disagree_flip_thresh", 0.48))
                    except (TypeError, ValueError):
                        thresh = 0.48
                    if quant_side == "SHORT":
                        reason_parts.append(f"quant_flip=raw({float(raw_est_prob):.3f})<{thresh:.2f}")
                    else:
                        reason_parts.append(f"quant_flip=raw({float(raw_est_prob):.3f})>{1.0 - thresh:.2f}")
                    final_side = quant_side
                    side_source = "btc_quant_disagree_flip"
                    conflict_type = f"{base_side.lower()}_to_{quant_side.lower()}_quant_disagree"
                    resolver_path = f"{base_path}__quant_{quant_side.lower()}"

        action, direction, effective_side = self._btc_action_for_side(final_side)
        return BTCDirectionDecision(
            action=action,
            direction=direction,
            effective_side=effective_side,
            side_source=side_source,
            conflict_type=conflict_type,
            resolver_path=resolver_path,
            htf_side=htf_side,
            quant_side=quant_side,
            momentum_side=momentum_side,
        )

    def _btc_direction_guard_reason(
        self,
        *,
        window_size: str,
        decision: BTCDirectionDecision,
        yes_price: float,
        btc_1h_regime: Optional[str],
        eval_mins_left: float,
    ) -> Optional[str]:
        """Block known-bad BTC direction paths before threshold tuning.

        Session review showed the main BTC loss engine was not the clean HTF-bias
        short path; it was quant-disagree short flips plus very expensive/late
        BUY_NO entries while BTC 1H regime remained bullish.
        """
        regime = str(btc_1h_regime or "").upper()

        if (
            decision.action == "BUY_NO"
            and decision.side_source == "btc_quant_disagree_flip"
            and not bool(self.config.get("allow_quant_disagree_flip_buy_no", False))
        ):
            return "quant_disagree_flip_buy_no_disabled"

        if decision.action != "BUY_NO" or regime != "BULL":
            return None

        max_yes_key = f"bull_regime_buy_no_max_yes_price_{window_size}"
        max_yes_price = self.config.get(max_yes_key)
        if max_yes_price is None:
            max_yes_price = self.config.get("bull_regime_buy_no_max_yes_price")
        if max_yes_price is not None and yes_price >= float(max_yes_price):
            return "bull_regime_expensive_short"

        min_left_key = f"bull_regime_buy_no_min_mins_left_{window_size}"
        min_mins_left = self.config.get(min_left_key)
        if min_mins_left is None:
            min_mins_left = self.config.get("bull_regime_buy_no_min_mins_left")
        if min_mins_left is not None and eval_mins_left <= float(min_mins_left):
            return "bull_regime_late_short"

        return None

    def _calibrate_est_prob(
        self,
        raw_est_prob: float,
        *,
        action: str,
        direction: str,
        window_size: str,
        signal_reason: str,
        htf_bias: Optional[str],
        side_source: Optional[str] = None,
        resolver_path: Optional[str] = None,
    ) -> float:
        self._last_calibration_vetoed = False
        self._last_calibration_lane_id = ""
        cal = getattr(self, "lane_calibrator", None)
        if cal is None:
            return raw_est_prob
        lane_meta = build_lane_metadata(
            strategy="bitcoin",
            window_size=window_size,
            action=action,
            direction=direction,
            entry_leg=("NO" if action == "BUY_NO" else "YES"),
            side_source=side_source or "btc_htf_bias",
            resolver_path=resolver_path,
            ai_used=False,
            reason=signal_reason,
            signal_reason=signal_reason,
            htf_bias=htf_bias,
        )
        lane_id = str(lane_meta.get("lane_id") or "").strip()
        if not lane_id:
            return raw_est_prob
        self._last_calibration_lane_id = lane_id
        self._last_calibration_vetoed = bool(cal.is_vetoed(lane_id))
        return float(cal.calibrate(lane_id, raw_est_prob))

    def _maybe_bias_quant_disagree_override(
        self,
        *,
        is_updown: bool,
        window_size: str,
        action: str,
        htf_bias: Optional[str],
        yes_price: float,
        raw_est_prob: Optional[float],
        edge: float,
        reason_parts: list,
    ) -> bool:
        """Route HTF/quant disagreement through sizing instead of hard rejection."""
        if (
            not self.bias_quant_disagree_override_enabled
            or not is_updown
            or window_size == "5m"
            or raw_est_prob is None
            or htf_bias not in {"BULLISH", "BEARISH"}
        ):
            return False

        aligned_with_bias = (
            (htf_bias == "BULLISH" and action == "BUY_YES")
            or (htf_bias == "BEARISH" and action == "BUY_NO")
        )
        if not aligned_with_bias:
            return False

        gap = (
            float(yes_price) - float(raw_est_prob)
            if action == "BUY_YES"
            else float(raw_est_prob) - float(yes_price)
        )
        if gap < 0:
            return False
        if window_size != "5m":
            if gap > self.bias_quant_disagree_aligned_max_gap_non5m:
                return False
            if edge < self.bias_quant_disagree_aligned_min_edge_non5m:
                return False

        reason_parts.append(f"bias_quant_override_gap={gap:.3f}")
        reason_parts.append(f"bias_quant_override_edge={edge:.3f}")
        return True

    def _score_neutral_15m_candidate(
        self,
        *,
        edge: float,
        effective_min_edge: float,
        confidence: float,
        ltf_strength: float,
        minutes_left: float,
        yes_price: float,
        action: Optional[str] = None,
        btc_1h_regime: Optional[str] = None,
    ):
        return score_updown_candidate(
            edge=edge,
            min_edge=effective_min_edge,
            quant_confidence=confidence,
            micro_momentum=ltf_strength,
            timeframe_alignment=0.50,
            oracle=OracleValidation(
                passed=True,
                reason="oracle_not_applicable",
                oracle_price=None,
                exchange_spot=None,
                basis_bps=None,
                freshness_sec=None,
            ),
            minutes_to_resolution=minutes_left,
            yes_price=yes_price,
            floor=self.neutral_15m_min_composite_score,
            action=action,
            btc_1h_regime=btc_1h_regime,
            regime_action_gate_enabled=bool(
                self.updown_composite_cfg.get("regime_action_gate_enabled", True)
            ),
            regime_action_min_convergence=float(
                self.updown_composite_cfg.get("regime_action_min_convergence", 0.55)
            ),
        )

    async def _ai_kill_switch_analysis(self, reason: str, loss_count: int) -> None:
        if not self.ai_agent or not self.ai_agent.is_available():
            return
        try:
            context = (
                f"Lane: BITCOIN\n"
                f"Kill switch triggered: {reason}\n"
                f"Consecutive losses: {loss_count}\n"
                f"This is a diagnostic call to understand why the lane is struggling."
            )
            result = await self.ai_agent.analyze_market(
                market_question=f"Why is bitcoin strategy losing? {reason}",
                market_description=context,
                current_yes_price=0.5,
                market_id="kill_switch_bitcoin",
            )
            if result:
                logger.warning(
                    f"OPS_JSON kill_switch_ai lane=bitcoin "
                    f"reasoning={result.reasoning!r} confidence={result.confidence_score:.2f}"
                )
        except Exception:
            pass

    async def _analyze_market_with_timeout(self, **kwargs):
        """Bound BTC AI latency so one LLM call cannot dominate a scan cycle."""
        market_id = str(kwargs.get("market_id", ""))
        kwargs.setdefault("provider_scope", "decision")
        try:
            return await asyncio.wait_for(
                self.ai_agent.analyze_market(**kwargs),
                timeout=self.ai_analysis_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "BTC: analyze_market timeout for market %s after %.1fs",
                market_id,
                self.ai_analysis_timeout_sec,
            )
            return None

    def _resolve_or_enqueue_ai(
        self,
        *,
        lane_id: str,
        market,
        ai_context: str,
        yes_price: float,
        edge: float,
        confidence: float,
        action: str,
        quant_threshold: float,
        raw_est_prob,
        estimated_prob,
        require_shadow_portfolio: bool,
        htf_bias=None,
        open_position_ids=None,
    ):
        """Broker-aware AI lookup. Returns (state, ai_decision):
          ("resolved", AIDecision) — usable decision; proceed as if legacy await returned it
          ("pending", None)        — enqueued for background resolution; skip this candidate
          ("unavailable", None)    — no broker wired; caller must use legacy await
        """
        if self.ai_broker is None:
            return "unavailable", None
        key = ("bitcoin", str(market.id), lane_id, action)
        resolved = self.ai_broker.get_resolved(
            key,
            current_yes_price=yes_price,
            current_action=action,
            current_edge=edge,
            open_position_ids=open_position_ids,
        )
        if resolved is not None:
            return "resolved", resolved
        snapshot = _BrokerPendingDecision(
            key=key,
            state=_BROKER_STATE_PENDING,
            created_at=time.time(),
            cycle_enqueued=0,
            yes_price_at_enqueue=float(yes_price),
            edge_sign=1 if float(edge) >= 0 else -1,
            action=action,
            market_question=market.question,
            market_description=ai_context,
            current_yes_price=float(yes_price),
            edge=float(edge),
            confidence=float(confidence),
            estimated_prob=(float(estimated_prob) if estimated_prob is not None else None),
            raw_est_prob=(float(raw_est_prob) if raw_est_prob is not None else None),
            quant_threshold=float(quant_threshold),
            require_shadow_portfolio=bool(require_shadow_portfolio),
            htf_bias=htf_bias,
        )
        self.ai_broker.enqueue(snapshot)
        return "pending", None

    async def _evaluate_trade_decision_with_timeout(self, **kwargs):
        """Bound BTC AI decision latency on 5m/15m updown assists."""
        market_id = str(kwargs.get("market_id", ""))
        try:
            return await asyncio.wait_for(
                self.ai_agent.evaluate_trade_decision(**kwargs),
                timeout=self.ai_decision_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "BTC: evaluate_trade_decision timeout for market %s after %.1fs",
                market_id,
                self.ai_decision_timeout_sec,
            )
            return None

    def _requires_shadow_for_lane(self, lane: str) -> bool:
        check = getattr(self.ai_agent, "decision_layer_lane_requires_shadow", None)
        if callable(check) and check("bitcoin", lane) is True:
            return True
        if lane == "neutral_15m":
            return bool(self.neutral_15m_requires_shadow_portfolio)
        return False

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────

    def _is_bitcoin_market(self, market: Market) -> bool:
        text = (
            f"{market.question} {market.description} "
            f"{market.group_item_title} {market.slug}"
        ).lower()
        has_btc = any(p.search(text) for p in BTC_PATTERNS)
        if not has_btc:
            return False
        # Prevent cross-asset leakage when market text mentions multiple coins.
        if any(term in text for term in NON_BTC_ASSET_TERMS):
            q = (market.question or "").lower()
            if not any(p.search(q) for p in BTC_PATTERNS):
                return False
        return True

    def _is_updown_market(self, market: Market) -> bool:
        """Check if this is a Bitcoin Up or Down market (matches both 15m and 5m)."""
        slug = (market.slug or "").lower()
        if slug.startswith(BTC_UPDOWN_SLUG_PREFIXES):
            return True
        text = f"{market.question} {market.group_item_title}"
        return bool(UPDOWN_PATTERN.search(text))

    def _is_5m_market(self, market: Market) -> bool:
        """Check if this is a 5-minute candle Up or Down market (≤5 min window)."""
        return _market_window_minutes(market) <= 5

    def _resolve_entry_window_bounds(self, *, tf: str, default_min: float, default_max: float) -> tuple[float, float]:
        """Return entry window bounds, optionally widened to align with scan cadence."""
        if tf not in ("5m", "15m", "1h"):
            tf = "15m"
        win_min = float(
            self._tf_cfg(tf, "entry_window_min", default_min)
        )
        win_max = float(
            self._tf_cfg(tf, "entry_window_max", default_max)
        )
        if win_min > win_max:
            win_min, win_max = win_max, win_min

        if not self.config.get("entry_window_auto_align", False):
            return win_min, win_max

        # The main loop scans every ~5m. Expand slightly so scans/latency don't
        # repeatedly miss a narrow valid window by seconds.
        scan_interval_sec = float(self.config.get("entry_window_align_scan_interval_sec", 300))
        if tf == "5m":
            default_expand = 1.0
        elif tf == "1h":
            default_expand = 5.0  # hourly tolerates wider cadence drift
        else:
            default_expand = 1.5
        max_expand_min = float(self.config.get("entry_window_auto_align_max_expand_min", default_expand))
        jitter_sec = float(self.config.get("entry_window_auto_align_jitter_sec", 15))
        cadence_half_min = scan_interval_sec / 120.0
        expansion_min = max(cadence_half_min, max_expand_min) + max(0.0, jitter_sec) / 60.0

        aligned_min = max(0.0, win_min - expansion_min)
        expanded_upper = win_max + expansion_min
        # Do not cap expanded_upper by a fixed 15m/6m candle — configs above 15 were
        # silently ignored (early-listed Polymarket contracts often report mins_left > 15).
        hard_cap = float(self.config.get("entry_window_hard_cap_mins_left", 0.0) or 0.0)
        aligned_max = min(expanded_upper, hard_cap) if hard_cap > 0 else expanded_upper
        if aligned_max <= aligned_min:
            return win_min, win_max
        return aligned_min, aligned_max

    def _default_entry_window_bounds(self, tf: str) -> tuple[float, float]:
        if tf == "5m":
            return self._resolve_entry_window_bounds(
                tf="5m",
                default_min=2.5,
                default_max=4.5,
            )
        if tf == "1h":
            # Hourly products: wide passthrough until tuned from live data.
            return self._resolve_entry_window_bounds(
                tf="1h",
                default_min=1.0,
                default_max=59.0,
            )
        return self._resolve_entry_window_bounds(
            tf="15m",
            default_min=12.5,
            default_max=14.5,
        )

    def _legacy_entry_policy(
        self,
        *,
        window_size: str,
        action: str,
    ) -> Dict[str, Any]:
        if window_size == "5m":
            min_edge = float(self._tf_cfg("5m", "min_edge", self.min_edge))
        elif window_size == "1h":
            min_edge = float(self._tf_cfg("1h", "min_edge", self.min_edge))
        else:
            min_edge = float(self._tf_cfg(window_size, "min_edge", self.min_edge))
        min_edge_buy_no = float(
            self._tf_cfg(window_size, "min_edge_buy_no", 0.0)
        )
        if action == "BUY_NO" and min_edge_buy_no > 0:
            min_edge = min_edge_buy_no
        win_min, win_max = self._default_entry_window_bounds(window_size)
        entry_price_max = float(self.config.get("entry_price_max_updown", 0.54))
        if action == "BUY_YES" and window_size == "1h":
            entry_price_max = float(
                self.config.get(
                    "entry_price_max_1h_yes_side",
                    self.config.get("entry_price_max_1h_buy_yes", entry_price_max),
                )
            )
        return {
            "enabled": True,
            "min_edge": float(min_edge),
            "hard_min_edge": 0.0,
            "ai_override_min_edge": float(
                self._ai_override_min_edge_for_window(window_size)
            ),
            "entry_price_min": float(self.config.get("entry_price_min_updown", 0.46)),
            "entry_price_max": float(entry_price_max),
            "entry_window_min": float(win_min),
            "entry_window_max": float(win_max),
            "size_multiplier": 1.0,
        }

    def _resolve_lane_entry_policy(
        self,
        *,
        window_size: str,
        action: str,
        direction: str,
    ):
        side = resolve_entry_policy_side(direction=direction, action=action)
        policy = resolve_lane_entry_policy(
            strategy_name=self._signal_strategy_name,
            window_size=window_size,
            side=side,
            full_config=self.full_config,
            legacy_policy=self._legacy_entry_policy(window_size=window_size, action=action),
        )
        return side, policy

    def _resolve_entry_timing_window_bounds(self, *, tf: str) -> tuple[float, float]:
        """Return the preferred minutes-left band for marginal up/down tie-break timing."""
        if tf not in ("5m", "15m", "1h"):
            tf = "15m"
        presets = {"5m": (1.5, 2.5), "15m": (8.0, 13.0), "1h": (5.0, 55.0)}
        default_min, default_max = presets[tf]
        new_k = f"entry_timing_window_{tf}_min"
        leg_k = f"ai_entry_window_{tf}_min"
        win_min = float(self.config.get(new_k, self.config.get(leg_k, default_min)))
        new_kx = f"entry_timing_window_{tf}_max"
        leg_kx = f"ai_entry_window_{tf}_max"
        win_max = float(self.config.get(new_kx, self.config.get(leg_kx, default_max)))
        if win_min > win_max:
            win_min, win_max = win_max, win_min
        return win_min, win_max

    def _within_entry_timing_window(self, *, mins_left: float, tf: str) -> bool:
        win_min, win_max = self._resolve_entry_timing_window_bounds(tf=tf)
        return win_min <= mins_left <= win_max

    def _active_macro_event_name(self, now_utc: datetime) -> Optional[str]:
        """Return matching macro event name when inside a configured event window."""
        if not self.macro_event_guard_enabled:
            return None
        if not isinstance(self.macro_event_calendar, list):
            return None

        for item in self.macro_event_calendar:
            if not isinstance(item, dict):
                continue
            dt_raw = item.get("datetime_utc")
            if not dt_raw:
                continue
            try:
                dt = datetime.fromisoformat(str(dt_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)

            before_min = int(item.get("before_min", self.macro_event_guard_before_min))
            after_min = int(item.get("after_min", self.macro_event_guard_after_min))
            start = dt - timedelta(minutes=max(0, before_min))
            end = dt + timedelta(minutes=max(0, after_min))
            if start <= now_utc <= end:
                return str(item.get("name") or "macro_event")
        return None

    def _extract_direction(self, question: str) -> str:
        q = question.lower()
        up = sum(1 for w in UP_WORDS if w in q)
        dn = sum(1 for w in DOWN_WORDS if w in q)
        return "UP" if up >= dn else "DOWN"

    def _extract_price_threshold(self, question: str) -> Optional[float]:
        for pattern in PRICE_PATTERNS:
            match = pattern.search(question)
            if match:
                price_str = match.group(1).replace(',', '')
                try:
                    price = float(price_str)
                    # Check the pattern itself and surrounding text for suffixes
                    full_match = match.group(0).lower()
                    remaining = question[match.end():match.end() + 2].lower()
                    if 'm' in full_match or 'm' in remaining:
                        price *= 1_000_000
                    elif 'k' in full_match or 'k' in remaining:
                        price *= 1000
                    if 1000 < price < 1_000_000_000:
                        return price
                except ValueError:
                    continue
        return None

    # ──────────────────────────────────────────────────────────────
    # LAYER 1: Higher Timeframe Trend Filter (4H)
    # ──────────────────────────────────────────────────────────────

    def _get_higher_tf_bias(self, ta: TechnicalAnalysis) -> str:
        """Determine the 4H trend bias. This is THE LAW.

        Uses three inputs from the 4H chart:
        1. Trend Sabre direction (trend == 1 or -1)
        2. Price position vs Sabre MA (above = bullish, below = bearish)
        3. 4H MACD above/below zero line

        Returns: "BULLISH", "BEARISH", or "NEUTRAL" (rare — all three conflict)
        """
        sabre = ta.trend_sabre
        macd_4h = ta.macd_4h
        price = ta.current_price

        bull_votes = 0
        bear_votes = 0

        # Vote 1: Trend Sabre trend direction
        if sabre.trend == 1:
            bull_votes += 1
        elif sabre.trend == -1:
            bear_votes += 1

        # Vote 2: Price vs Sabre SMA(35) — is price above or below the moving range?
        if price > sabre.ma_value:
            bull_votes += 1
        elif price < sabre.ma_value:
            bear_votes += 1

        # Vote 3: 4H MACD momentum direction
        # Three cases for bull vote:
        #   a) MACD line above zero (confirmed uptrend)
        #   b) BULLISH_CROSS crossover (fresh bull cross, even if below zero)
        #   c) Histogram rising strongly below zero — recovery in progress before zero-line cross.
        #      Live data: large positive histograms (+200 to +300) below zero were counting as
        #      bear votes (bug), causing false BEARISH HTF calls during BTC recoveries.
        _early_bull = (macd_4h.crossover == "BULLISH_CROSS" and macd_4h.histogram_rising)
        _early_bear = (macd_4h.crossover == "BEARISH_CROSS" and not macd_4h.histogram_rising)
        # Recovery signal: histogram positive while still below zero line
        # MACD above signal line (histogram > 0) is bullish even if decelerating.
        # Requiring histogram_rising was causing false BEARISH calls during pumps
        # where histogram was large/positive but momentarily decelerating.
        _recovery = (not macd_4h.above_zero
                     and macd_4h.histogram > 0)
        if _early_bear:
            bear_votes += 1
        elif macd_4h.above_zero or _early_bull or _recovery:
            bull_votes += 1
        else:
            bear_votes += 1

        if bull_votes >= 2:
            bias = "BULLISH"
        elif bear_votes >= 2:
            bias = "BEARISH"
        else:
            return "NEUTRAL"

        # ── Conviction gate: require meaningful MACD histogram magnitude ──
        # Without this, 2/3 vote with a histogram near zero (e.g. +5 when typical
        # range is +/-200) produces weak directional calls → 50/50 coin-flip entries.
        # Require |histogram| > min_hist_magnitude (default 20) to confirm direction.
        # If below threshold, downgrade to NEUTRAL — tighter edge will be enforced.
        _min_hist = self.config.get("min_4h_hist_magnitude", 20.0)
        if abs(macd_4h.histogram) < _min_hist:
            logger.info(
                f"BTC HTF: {bias} by vote but 4H MACD hist={macd_4h.histogram:+.1f} "
                f"below conviction threshold ({_min_hist}) — downgrading to NEUTRAL"
            )
            return "NEUTRAL"

        return bias

    @staticmethod
    def _bias_to_side(bias: str) -> Optional[str]:
        if bias == "BULLISH":
            return "LONG"
        if bias == "BEARISH":
            return "SHORT"
        return None

    def _get_btc_tf_state(self, ta: TechnicalAnalysis, tf: str) -> Any:
        state = getattr(ta, f"tf_{tf}", None)
        if state is not None and (
            float(getattr(state, "price", 0.0) or 0.0) > 0
            or float(getattr(state, "ema_9", 0.0) or 0.0) > 0
        ):
            return state
        macd = getattr(ta, f"macd_{tf}", None) if tf in {"15m", "1h", "4h"} else None
        if tf == "5m":
            macd = None
        return type(
            "FallbackTFState",
            (),
            {
                "timeframe": tf,
                "price": float(getattr(ta, "current_price", 0.0) or 0.0),
                "ema_9": float(getattr(ta, "ema_9", 0.0) or 0.0),
                "ema_21": float(getattr(ta, "ema_21", 0.0) or 0.0),
                "ema_50": float(getattr(ta, "ema_50", 0.0) or 0.0),
                "rsi_14": float(getattr(ta, "rsi_14", 50.0) or 50.0),
                "macd": macd,
            },
        )()

    def _vote_macd_bias(self, macd: Any, *, min_hist_magnitude: float) -> str:
        if macd is None:
            return "NEUTRAL"
        bull_votes = 0
        bear_votes = 0
        if bool(getattr(macd, "above_zero", False)):
            bull_votes += 1
        else:
            bear_votes += 1
        crossover = str(getattr(macd, "crossover", "") or "")
        if crossover == "BULLISH_CROSS":
            bull_votes += 1
        elif crossover == "BEARISH_CROSS":
            bear_votes += 1
        hist = float(getattr(macd, "histogram", 0.0) or 0.0)
        if abs(hist) < float(min_hist_magnitude):
            return "NEUTRAL"
        if bool(getattr(macd, "histogram_rising", False)):
            bull_votes += 1
        else:
            bear_votes += 1
        if bull_votes >= 2:
            return "BULLISH"
        if bear_votes >= 2:
            return "BEARISH"
        return "NEUTRAL"

    @staticmethod
    def _vote_rsi_bias(rsi: float) -> str:
        if rsi >= 55.0:
            return "BULLISH"
        if rsi <= 45.0:
            return "BEARISH"
        return "NEUTRAL"

    @staticmethod
    def _vote_ema_bias(price: float, ema_9: float, ema_21: float, ema_50: float) -> str:
        if ema_50 > 0 and price > ema_9 > ema_21 > ema_50:
            return "BULLISH"
        if ema_50 > 0 and price < ema_9 < ema_21 < ema_50:
            return "BEARISH"
        if price > ema_9 > ema_21:
            return "BULLISH"
        if price < ema_9 < ema_21:
            return "BEARISH"
        return "NEUTRAL"

    def _resolve_voted_bias(
        self,
        *,
        macd: Any,
        price: float,
        ema_9: float,
        ema_21: float,
        ema_50: float,
        rsi: float,
        min_hist_magnitude: float,
    ) -> str:
        votes = [
            self._vote_macd_bias(macd, min_hist_magnitude=min_hist_magnitude),
            self._vote_rsi_bias(rsi),
            self._vote_ema_bias(price, ema_9, ema_21, ema_50),
        ]
        bull_votes = sum(1 for vote in votes if vote == "BULLISH")
        bear_votes = sum(1 for vote in votes if vote == "BEARISH")
        if bull_votes >= 2:
            return "BULLISH"
        if bear_votes >= 2:
            return "BEARISH"
        return "NEUTRAL"

    def _get_5m_bias(self, ta: TechnicalAnalysis) -> str:
        state = self._get_btc_tf_state(ta, "5m")
        if state is None:
            return "NEUTRAL"
        return self._resolve_voted_bias(
            macd=getattr(state, "macd", None),
            price=float(getattr(state, "price", 0.0) or 0.0),
            ema_9=float(getattr(state, "ema_9", 0.0) or 0.0),
            ema_21=float(getattr(state, "ema_21", 0.0) or 0.0),
            ema_50=float(getattr(state, "ema_50", 0.0) or 0.0),
            rsi=float(getattr(state, "rsi_14", 50.0) or 50.0),
            min_hist_magnitude=float(self._tf_cfg("5m", "min_hist_magnitude", 0.0) or 0.0),
        )

    def _get_15m_bias(self, ta: TechnicalAnalysis) -> str:
        state = self._get_btc_tf_state(ta, "15m")
        if state is None:
            return "NEUTRAL"
        return self._resolve_voted_bias(
            macd=getattr(state, "macd", None),
            price=float(getattr(state, "price", 0.0) or 0.0),
            ema_9=float(getattr(state, "ema_9", 0.0) or 0.0),
            ema_21=float(getattr(state, "ema_21", 0.0) or 0.0),
            ema_50=float(getattr(state, "ema_50", 0.0) or 0.0),
            rsi=float(getattr(state, "rsi_14", 50.0) or 50.0),
            min_hist_magnitude=float(self._tf_cfg("15m", "min_hist_magnitude", 0.0) or 0.0),
        )

    def _get_1h_bias(self, ta: TechnicalAnalysis) -> str:
        state = self._get_btc_tf_state(ta, "1h")
        if state is None:
            return "NEUTRAL"
        return self._resolve_voted_bias(
            macd=getattr(state, "macd", None),
            price=float(getattr(state, "price", 0.0) or 0.0),
            ema_9=float(getattr(state, "ema_9", 0.0) or 0.0),
            ema_21=float(getattr(state, "ema_21", 0.0) or 0.0),
            ema_50=float(getattr(state, "ema_50", 0.0) or 0.0),
            rsi=float(getattr(state, "rsi_14", 50.0) or 50.0),
            min_hist_magnitude=float(self._tf_cfg("1h", "min_hist_magnitude", 0.0) or 0.0),
        )

    def _resolve_bias_for_tf(self, ta: TechnicalAnalysis, tf: str) -> BTCBiasResolution:
        backup_4h_bias = self._get_higher_tf_bias(ta)
        if tf == "5m":
            horizon_bias = self._get_5m_bias(ta)
            slower_biases = {"15m": self._get_15m_bias(ta), "1h": self._get_1h_bias(ta), "4h": backup_4h_bias}
        elif tf == "15m":
            horizon_bias = self._get_15m_bias(ta)
            slower_biases = {"1h": self._get_1h_bias(ta), "4h": backup_4h_bias}
        else:
            horizon_bias = self._get_1h_bias(ta)
            slower_biases = {"4h": backup_4h_bias}

        if horizon_bias in {"BULLISH", "BEARISH"}:
            side_source = f"btc_{tf}_native"
            penalty = 0.0
            for slower_tf, slower_bias in slower_biases.items():
                if slower_bias not in {"BULLISH", "BEARISH"}:
                    continue
                if slower_bias != horizon_bias:
                    penalty += 0.03
                    side_source = f"btc_{tf}_vs_slower"
            return BTCBiasResolution(
                allowed_side=self._bias_to_side(horizon_bias),
                side_source=side_source,
                horizon_tf=tf,
                horizon_bias=horizon_bias,
                slower_biases=slower_biases,
                primary_htf_bias=horizon_bias,
                confidence_penalty=penalty,
            )

        for slower_tf, slower_bias in slower_biases.items():
            if slower_bias not in {"BULLISH", "BEARISH"}:
                continue
            return BTCBiasResolution(
                allowed_side=self._bias_to_side(slower_bias),
                side_source=f"btc_{tf}_neutral_fallback_{slower_tf}",
                horizon_tf=tf,
                horizon_bias=horizon_bias,
                slower_biases=slower_biases,
                primary_htf_bias=slower_bias,
                confidence_penalty=0.04,
            )

        return BTCBiasResolution(
            allowed_side=None,
            side_source=f"btc_{tf}_neutral",
            horizon_tf=tf,
            horizon_bias=horizon_bias,
            slower_biases=slower_biases,
            primary_htf_bias="NEUTRAL",
        )

    # ──────────────────────────────────────────────────────────────
    # LAYER 2: Lower Timeframe Confirmation (15m MACD)
    # ──────────────────────────────────────────────────────────────

    def _check_lower_tf_confirmation(self, ta: TechnicalAnalysis, allowed_side: str) -> tuple:
        """Check if 15m MACD confirms the allowed direction.

        allowed_side: "LONG" or "SHORT"

        Returns: (confirmed: bool, strength: float, reasons: list)
        """
        macd_15m = ta.macd_15m
        reasons = []
        strength = 0.0

        if allowed_side == "LONG":
            # Need: bullish cross OR rising histogram (red→green) OR MACD above signal
            if macd_15m.crossover == "BULLISH_CROSS":
                strength += 0.40
                reasons.append("15m MACD bull cross")
            if macd_15m.histogram_rising and macd_15m.histogram > macd_15m.prev_histogram:
                # Histogram turning from red to green (or getting more green)
                if macd_15m.prev_histogram < 0 and macd_15m.histogram > 0:
                    strength += 0.35
                    reasons.append("15m hist red->green")
                elif macd_15m.histogram_rising:
                    strength += 0.20
                    reasons.append("15m hist rising")
            if macd_15m.macd_line > macd_15m.signal_line:
                strength += 0.15
                reasons.append("15m MACD>signal")

        elif allowed_side == "SHORT":
            if macd_15m.crossover == "BEARISH_CROSS":
                strength += 0.40
                reasons.append("15m MACD bear cross")
            if not macd_15m.histogram_rising and macd_15m.histogram < macd_15m.prev_histogram:
                if macd_15m.prev_histogram > 0 and macd_15m.histogram < 0:
                    strength += 0.35
                    reasons.append("15m hist green->red")
                elif not macd_15m.histogram_rising:
                    strength += 0.20
                    reasons.append("15m hist falling")
            if macd_15m.macd_line < macd_15m.signal_line:
                strength += 0.15
                reasons.append("15m MACD<signal")

        # Require stronger composite confirmation (0.50 instead of 0.35).
        # Single signals (crossover=0.40, hist flip=0.35) no longer auto-confirm —
        # must combine with rising/falling histogram or MACD>signal to block entry.
        # 60s scan was catching every crossover as "late entry" before 15m could realign.
        confirmed = strength >= 0.50
        return confirmed, min(1.0, strength), reasons

    # ──────────────────────────────────────────────────────────────
    # LAYER 3: Entry Timing
    # ──────────────────────────────────────────────────────────────

    def _check_timing(self, ta: TechnicalAnalysis, allowed_side: str) -> tuple:
        """Check candle momentum and prediction window.

        Returns: (timing_bonus: float, reasons: list)
        """
        mom = ta.candle_momentum
        bonus = 0.0
        reasons = []

        # Early-candle spike in allowed direction = strong confirmation
        if allowed_side == "LONG":
            if mom.m15_direction in ("SPIKE_UP", "DRIFT_UP"):
                bonus += 0.08 if "SPIKE" in mom.m15_direction else 0.04
                reasons.append(f"15m early {mom.m15_direction} ({mom.m15_move_pct:+.3f}%)")
            elif mom.m15_direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
                bonus -= 0.05  # Early candle going against us
                reasons.append(f"15m early AGAINST ({mom.m15_direction})")
            if mom.m5_direction in ("SPIKE_UP", "DRIFT_UP"):
                bonus += 0.04 if "SPIKE" in mom.m5_direction else 0.02
                reasons.append(f"5m early {mom.m5_direction}")
        else:  # SHORT
            if mom.m15_direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
                bonus += 0.08 if "SPIKE" in mom.m15_direction else 0.04
                reasons.append(f"15m early {mom.m15_direction} ({mom.m15_move_pct:+.3f}%)")
            elif mom.m15_direction in ("SPIKE_UP", "DRIFT_UP"):
                bonus -= 0.05
                reasons.append(f"15m early AGAINST ({mom.m15_direction})")
            if mom.m5_direction in ("SPIKE_DOWN", "DRIFT_DOWN"):
                bonus += 0.04 if "SPIKE" in mom.m5_direction else 0.02
                reasons.append(f"5m early {mom.m5_direction}")

        # Prediction window bonus. Keep this configurable because paper data can
        # distinguish plain timing-window entries from stronger drift/spike moves.
        prediction_bonus_15m = float(self.config.get("prediction_window_bonus_15m", 0.03) or 0.0)
        prediction_bonus_5m = float(self.config.get("prediction_window_bonus_5m", 0.02) or 0.0)
        if mom.m15_in_prediction_window and prediction_bonus_15m > 0:
            bonus += prediction_bonus_15m
            reasons.append("15m predict window")
        if mom.m5_in_prediction_window and prediction_bonus_5m > 0:
            bonus += prediction_bonus_5m
            reasons.append("5m predict window")

        return bonus, reasons

    # ──────────────────────────────────────────────────────────────
    # LAYER 4: Edge Calculation
    # ──────────────────────────────────────────────────────────────

    def _classify_btc_1h_regime(self, ta: TechnicalAnalysis) -> str:
        cfg = self._btc_1h_regime_gates
        price = ta.btc_1h_close or ta.current_price
        sma = ta.sma_1h_20
        band = float(cfg.get("range_band_pct", 0.0012))
        return classify_btc_1h_sma_regime(price, sma, band)

    def _regime_size_mult(self, regime: str) -> float:
        cfg = self._btc_1h_regime_gates.get("size_mult") or {}
        base = DEFAULT_SIZE_MULT.get(regime, 1.0)
        return float(cfg.get(regime, base))

    def _estimate_probability(
        self,
        btc_price: float,
        threshold: float,
        direction: str,
        ta: TechnicalAnalysis,
        days_to_resolution: int,
        ltf_strength: float,
        timing_bonus: float,
        regime: str = "BULL",
    ) -> float:
        """Estimate true probability incorporating all layers."""
        sabre = ta.trend_sabre

        # 1. Distance factor — how far is BTC from the threshold
        distance_pct = (btc_price - threshold) / threshold

        if direction == "UP":
            base_prob = 0.50 + distance_pct * 2.0
        else:
            base_prob = 0.50 - distance_pct * 2.0
        base_prob = max(0.05, min(0.95, base_prob))

        # 2. Lower TF confirmation strength (already gated by Layer 2)
        ltf_adj = ltf_strength * 0.10  # Up to +0.10

        # 3. Timing bonus from Layer 3
        # (already calculated, just add it)

        # 4. Tension — mean reversion risk
        tension_adj = 0.0
        if sabre.tension_abs > 2.0:
            # Snap-back hurts the stretched side and helps the opposite side.
            if direction == "UP":
                tension_adj = -0.02 if sabre.tension > 0 else 0.02
            else:
                tension_adj = 0.02 if sabre.tension > 0 else -0.02

        # 5. RSI extremes
        rsi_adj = 0.0
        if ta.rsi_14 > 75:
            if direction == "UP":
                rsi_adj = -0.04  # Overbought, careful on longs
            else:
                rsi_adj = 0.04  # Overbought supports shorts
        elif ta.rsi_14 < 25:
            if direction == "UP":
                rsi_adj = 0.04  # Oversold bounce supports longs
            else:
                rsi_adj = -0.04

        # 6. S/R proximity
        sr_adj = 0.0
        if direction == "UP" and ta.nearest_resistance > 0:
            if abs(threshold - ta.nearest_resistance) / max(threshold, 1) < 0.03:
                sr_adj = -0.04  # Threshold near resistance
        if direction == "DOWN" and ta.nearest_support > 0:
            if abs(threshold - ta.nearest_support) / max(threshold, 1) < 0.03:
                sr_adj = -0.04  # Threshold near support

        # 7. Anchored Volume Profile — confirmation/caution layer
        #    Anchored at the swing point that started the current trend.
        #    Tells us where meaningful participation happened since the move began.
        vp_adj = 0.0
        vp = ta.volume_profile
        if vp.poc_price > 0 and vp.val_price > 0 and vp.vah_price > 0:
            # Check if price is near any High Volume Node (strong S/R from participation)
            near_hvn = any(
                abs(btc_price - hvn) / btc_price < 0.005  # within 0.5%
                for hvn in vp.high_volume_nodes
            )
            # Check if price is in a Low Volume Node (fast-move zone)
            in_lvn = any(
                abs(btc_price - lvn) / btc_price < 0.005
                for lvn in vp.low_volume_nodes
            )

            if direction == "UP":
                # LONGS — where is price relative to volume participation?
                if btc_price > vp.vah_price:
                    # Price ABOVE value area — broke out of the volume zone
                    # If trend + momentum align, this is strong conviction
                    vp_adj += 0.04
                elif btc_price > vp.poc_price and near_hvn:
                    # Price above POC and sitting ON a high-volume support
                    # HVN below = buyers participated heavily here = support
                    vp_adj += 0.03
                elif vp.val_price <= btc_price <= vp.vah_price:
                    # Price STUCK in value area — participants are balanced here
                    # Reduce exposure, wait for breakout
                    vp_adj -= 0.03
                elif btc_price < vp.val_price:
                    # Price below value area — all that volume is overhead resistance
                    vp_adj -= 0.05
            else:  # DOWN
                if btc_price < vp.val_price:
                    # Price BELOW value area — broke down, sellers in control
                    vp_adj += 0.04
                elif btc_price < vp.poc_price and near_hvn:
                    # Below POC with HVN above = overhead resistance confirms shorts
                    vp_adj += 0.03
                elif vp.val_price <= btc_price <= vp.vah_price:
                    # Stuck in value area — reduce exposure
                    vp_adj -= 0.03
                elif btc_price > vp.vah_price:
                    # Price above value area — volume support below, hard to drop
                    vp_adj -= 0.05

            # LVN bonus: price in low-volume zone = fast moves expected
            # Good for directional trades if trend is clear
            if in_lvn and abs(tension_adj) < 0.02:  # Not already stretched
                vp_adj += 0.02  # Slight boost — less friction for price movement

        final = base_prob + ltf_adj + timing_bonus + tension_adj + rsi_adj + sr_adj + vp_adj

        # 8. Time decay — more time = more uncertainty. Applied to the full conviction
        # (not just base_prob) so directional adjustments also decay with horizon.
        if days_to_resolution > 0:
            time_factor = min(1.0, days_to_resolution / 60.0)
            final = final * (1 - time_factor * 0.3) + 0.50 * (time_factor * 0.3)

        # Regime dampening: compress conviction toward 0.50 in choppy/bear regimes
        if regime == "RANGE":
            final = 0.50 + (final - 0.50) * 0.80
        elif regime == "BEAR":
            final = 0.50 + (final - 0.50) * 0.70

        return max(0.05, min(0.95, final))

    # ──────────────────────────────────────────────────────────────
    # Main Scan — Enforces the Hierarchy
    # ──────────────────────────────────────────────────────────────

    async def scan_and_analyze(self, markets: List[Market], bankroll: float) -> List[BitcoinSignal]:
        """Scan BTC markets with strict hierarchical trend filtering."""
        if not self.enabled:
            self.last_scan_stats = {
                "enabled": False,
                "signals": 0,
                "ai_calls": 0,
                "ai_assists": 0,
                "ai_vetos": 0,
                "ai_holds": 0,
                "top_skip_reasons": {"disabled": 1},
                "gate_distributions": {},
            }
            return []

        # Filter to updown markets ONLY — threshold markets ("Will BTC hit $1m?",
        # "Will bitcoin hit $80k before GTA VI?") are noise for this strategy.
        # They have multi-week/month resolutions and our 15m/5m technical analysis
        # has zero predictive value on them.
        btc_markets = [m for m in markets if self._is_bitcoin_market(m) and self._is_updown_market(m)]
        if not btc_markets:
            self.last_scan_stats = {
                "enabled": True,
                "signals": 0,
                "ai_calls": 0,
                "ai_assists": 0,
                "ai_vetos": 0,
                "ai_holds": 0,
                "top_skip_reasons": {"no_updown_markets": 1},
                "gate_distributions": {},
            }
            logger.info(f"Bitcoin strategy: 0 BTC updown markets found out of {len(markets)} total")
            return []

        logger.info(f"Bitcoin strategy: Found {len(btc_markets)} BTC markets")

        # ── Fetch full technical analysis ONCE per cycle ──
        ta = self.btc_service.get_full_analysis()
        btc_1h_regime = "BULL"
        if ta and self._btc_1h_regime_gates.get("enabled", False):
            btc_1h_regime = self._classify_btc_1h_regime(ta)
        if not ta:
            self.last_scan_stats = {
                "enabled": True,
                "signals": 0,
                "ai_calls": 0,
                "ai_assists": 0,
                "ai_vetos": 0,
                "ai_holds": 0,
                "top_skip_reasons": {"no_ta": 1},
                "gate_distributions": {},
            }
            logger.warning("Bitcoin strategy: Could not fetch BTC price data")
            return []

        btc_price = ta.current_price
        mom = ta.candle_momentum
        macd_4h = ta.macd_4h
        macd_15m = ta.macd_15m
        sabre = ta.trend_sabre

        # ══════════════════════════════════════════════════════════
        # LAYER 0: Dynamic Exposure Check — are we even trading?
        # ══════════════════════════════════════════════════════════
        conditions = ExposureManager.conditions_from_ta(ta)
        exp_tier, exp_multiplier, exp_max_size, exp_reason = self.exposure_manager.get_exposure(conditions)

        if exp_tier == ExposureTier.PAUSED:
            self.last_scan_stats = {
                "enabled": True,
                "signals": 0,
                "ai_calls": 0,
                "ai_assists": 0,
                "ai_vetos": 0,
                "ai_holds": 0,
                "top_skip_reasons": {"exposure_paused": 1},
                "gate_distributions": {},
            }
            logger.info(f"Bitcoin strategy: PAUSED — {exp_reason}")
            return []

        htf_bias = self._get_higher_tf_bias(ta)
        bias_1h = self._get_1h_bias(ta)
        bias_15m = self._get_15m_bias(ta)
        bias_5m = self._get_5m_bias(ta)

        logger.info(
            f"BTC ${btc_price:,.0f} | bias_4h={htf_bias} bias_1h={bias_1h} bias_15m={bias_15m} bias_5m={bias_5m} | "
            f"Sabre={'BULL' if sabre.trend==1 else 'BEAR'} MA=${sabre.ma_value:,.0f} "
            f"Trail=${sabre.trail_value:,.0f} tension={sabre.tension:+.1f} | "
            f"4H MACD hist={macd_4h.histogram:+.0f} {'above' if macd_4h.above_zero else 'below'}0 "
            f"{macd_4h.crossover} | "
            f"15m MACD hist={macd_15m.histogram:+.2f} {macd_15m.crossover} | "
            f"RSI={ta.rsi_14:.0f} | "
            f"Mom 15m={mom.m15_direction}({mom.m15_move_pct:+.3f}%) "
            f"5m={mom.m5_direction}({mom.m5_move_pct:+.3f}%)"
        )

        # ══════════════════════════════════════════════════════════
        # LAYER 4: Evaluate each market
        # ══════════════════════════════════════════════════════════
        signals = []
        ai_calls = 0
        ai_assists = 0
        ai_vetos = 0
        ai_holds = 0
        shadow_pipeline_calls = 0
        shadow_pipeline_ok = 0
        preentry_veto_skips = 0
        ai_decision_layer_skips = 0
        skip_reasons: Dict[str, int] = {}
        gate_samples: Dict[str, list] = {}
        action_counts: Dict[str, int] = {}
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

        def _record_buy_no_skip(
            *,
            market,
            skip_reason: str,
            yes_price: float,
            edge: float,
            effective_min_edge: float,
            rsi: float,
            htf_bias_value: str,
            signal_reason: str,
            window_size: str,
        ) -> None:
            payload = {
                "strategy": "bitcoin",
                "market_id": market.id,
                "window_size": window_size,
                "skip_reason": skip_reason,
                "yes_price": float(yes_price),
                "edge": float(edge),
                "effective_min_edge": float(effective_min_edge),
                "rsi": float(rsi),
                "htf_bias": htf_bias_value,
                "signal_reason": signal_reason,
            }
            buy_no_skip_counts[skip_reason] = buy_no_skip_counts.get(skip_reason, 0) + 1
            last_buy_no_skip_sample.clear()
            last_buy_no_skip_sample.update(payload)
            if callable(self.buy_no_skip_callback):
                self.buy_no_skip_callback(
                    strategy="bitcoin",
                    market=market,
                    bankroll=bankroll,
                    payload=payload,
                )

        _latency_sec = float(self.config.get("entry_window_latency_buffer_sec", 0.0) or 0.0)
        # Loop-locals referenced in last_scan_stats below — init to safe defaults so
        # an early-continue path (e.g. all markets hit neutral_bias) doesn't trigger
        # UnboundLocalError when the stats dict is built post-loop.
        ltf_strength = 0.0

        for market in btc_markets:
            if market.liquidity > 0 and market.liquidity < self.min_liquidity:
                continue

            yes_price = market.yes_price
            is_updown = self._is_updown_market(market)
            _updown_tf = (
                updown_timeframe_label(resolved_updown_window_minutes(market))
                if is_updown
                else "15m"
            )
            is_5m = _updown_tf == "5m"
            is_1h = _updown_tf == "1h"
            resolution = self._resolve_bias_for_tf(ta, _updown_tf)
            allowed_side = resolution.allowed_side
            if allowed_side is None:
                _bump_skip("neutral_bias")
                logger.info(
                    "BTC skip '%s' — no usable %s bias (4h=%s 1h=%s 15m=%s 5m=%s)",
                    market.question[:40],
                    _updown_tf,
                    htf_bias,
                    bias_1h,
                    bias_15m,
                    bias_5m,
                )
                continue
            ltf_confirmed, ltf_strength, ltf_reasons = self._check_lower_tf_confirmation(ta, allowed_side)
            _sample("ltf_strength", ltf_strength)
            timing_bonus, timing_reasons = self._check_timing(ta, allowed_side)
            if (not is_5m) and ltf_confirmed:
                _bump_skip("ltf_confirmed_late_entry")
                logger.info(
                    "Bitcoin skip '%s' — LTF confirmed late-entry risk (strength=%.2f)",
                    market.question[:40],
                    ltf_strength,
                )
                continue
            ai_used = False
            threshold = None
            direction = "UP"  # default; overridden below
            side_source = resolution.side_source
            direction_decision: Optional[BTCDirectionDecision] = None
            reason_parts = [
                f"HTF4={htf_bias}",
                f"HTF1={bias_1h}",
                f"HTF15={bias_15m}",
                f"HTF5={bias_5m}",
                f"PRIMARY={resolution.primary_htf_bias}",
                f"side={allowed_side}",
                f"side_src={side_source}",
            ]
            if resolution.horizon_bias == "NEUTRAL":
                reason_parts.append(f"{_updown_tf}_neutral")
            if resolution.confidence_penalty > 0:
                reason_parts.append(f"bias_penalty={resolution.confidence_penalty:.3f}")
            dead_zone_would_block = False
            dead_zone_hour = None

                # ── UP/DOWN MARKETS (5m / 15m / 1h) ──
            # YES = "Up" (price goes up), NO = "Down" (price goes down)
            # Our technical analysis determines direction directly
            if is_updown:
                # is_5m was already detected above (True = 5m window, False = 15m window)

                # ── UTC hour filter ──
                # Loaded from config (strategies.bitcoin.blocked_utc_hours_updown).
                # OVERFIT RISK: these hours were identified from the same live sessions
                # they now gate. Only add an hour after it has ≥15 out-of-sample trades
                # with WR<0.46 AND avg_pnl<-$2. See config comment for full criteria.
                _dead_zone_enabled = self.config.get("dead_zone_enabled", True)
                _now_utc_hour = datetime.now(timezone.utc).hour
                _blocked_hours = self.config.get("blocked_utc_hours_updown", [])
                dead_zone_hour = _now_utc_hour
                dead_zone_would_block = _now_utc_hour in _blocked_hours
                if _dead_zone_enabled:
                    if dead_zone_would_block:
                        _bump_skip("blocked_utc_hour")
                        logger.info(
                            f"  BTC skip updown at UTC {_now_utc_hour:02d}:xx — "
                            f"dead-zone hour ({_now_utc_hour}:00 UTC <35% WR in live data)"
                        )
                        continue
                elif dead_zone_would_block:
                    logger.info(
                        f"  BTC dead_zone DISABLED — allowing UTC {_now_utc_hour:02d}:xx "
                        f"(blocked_hours={_blocked_hours})"
                    )

                _event_name = self._active_macro_event_name(datetime.now(timezone.utc))
                if _event_name:
                    _bump_skip("macro_event_window")
                    logger.info(
                        f"  BTC skip updown '{market.question[:40]}' — within macro event window ({_event_name})"
                    )
                    continue

                # ── Entry window guard ──
                # Only enter within a tight window near the candle open so that
                # momentum / indicator readings are fresh and relevant to the
                # actual window being traded.  Without this, the strategy would
                # evaluate windows 30+ minutes in the future using stale momentum,
                # producing inflated edges that the max_edge cap then rejects.
                #
                # Configurable per-timeframe so BTC 5m can be reverted
                # independently if needed (it was already performing well
                # before this change).  Set entry_window_Xm_min/max in
                # config.strategies.bitcoin to override.
                if not market.end_date:
                    _bump_skip("no_end_date")
                    logger.debug(f"  BTC skip '{market.question[:40]}' — no end_date")
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

                # Skip only when our entry-side price is in the unfavorable
                # long tail. Side derived from allowed_side (LONG=BUY_YES);
                # favorable tail (our side >= 0.80) wins 87–97% in ghost log.
                _sample("entry_price", yes_price)
                _our_price = (1.0 - yes_price) if allowed_side == "SHORT" else yes_price
                if _our_price < 0.20:
                    _bump_skip("price_too_far_from_50_50")
                    logger.debug(
                        f"  BTC skip '{market.question[:40]}' — our-side price "
                        f"{_our_price:.2f} < 0.20 (yes={yes_price:.2f}, {allowed_side})"
                    )
                    continue

                direction_decision = self._resolve_btc_direction(
                    htf_bias=htf_bias,
                    allowed_side=allowed_side,
                    macd_4h=macd_4h,
                    reason_parts=reason_parts,
                    mom=mom,
                )
                action = direction_decision.action
                direction = direction_decision.direction
                effective_side = direction_decision.effective_side
                side_source = direction_decision.side_source

                # ── BUY_YES manual disable ──
                # Live data: BUY_YES = 6 trades, 33% WR, -$4.93.
                # Downside leg uses BUY_NO on the NO token.
                # Config-driven so it can be re-enabled if market regime changes.
                if action == "BUY_YES" and self.config.get("disable_buy_yes", False):
                    _bump_skip("buy_yes_disabled")
                    logger.debug(
                        f"  BTC skip BUY_YES on '{market.question[:40]}' — "
                        f"disabled via config (live: 33% WR, -$4.93)"
                    )
                    continue

                if is_5m:
                    # ── [5m] FIVE-MINUTE UP/DOWN MARKET PATH ──
                    # Still requires HTF bias (4H) — the macro law applies
                    # LTF: use 5m candle momentum instead of 15m MACD for entry timing
                    # Tighter: m5_direction must align (DRIFT_UP/LEAN_UP for long)

                    # ── Correlated exposure skip ──
                    # If market_id (or market_question stripped of time) already has an
                    # open/recent BTC 5m position in the session, skip to avoid doubling up.
                    # Check both current open positions and the session snapshot.
                    _q_clean = re.sub(r'\d+:\d+[AP]M[-–]\d+:\d+[AP]M', '', market.question or '').strip()
                    for _pos in self._open_positions_snapshot:
                        _pq = getattr(_pos, 'market_question', None) or ''
                        _pq_clean = re.sub(r'\d+:\d+[AP]M[-–]\d+:\d+[AP]M', '', _pq).strip()
                        if _pq_clean == _q_clean:
                            _bump_skip("correlated_exposure_5m")
                            logger.info(
                                f"  BTC [5m] skip '{market.question[:40]}' — "
                                f"correlated_exposure_5m (open pos on same question)"
                            )
                            continue

                    macd_1h = ta.macd_1h
                    m5_dir = mom.m5_direction
                    hist_reject = btc_5m_hist_gate_reject_reason(
                        macd_4h, macd_1h, effective_side
                    )
                    _short_hist_telemetry_only = (
                        hist_reject == "hist_gate_5m_short_reject"
                        and not bool(
                            self.config.get("hist_gate_5m_short_hard_reject", True)
                        )
                    )
                    if _short_hist_telemetry_only:
                        reason_parts.append("hist_gate_5m_short_telemetry")
                        logger.info(
                            f"  BTC [5m] telemetry '{market.question[:40]}' — {hist_reject}"
                        )
                    elif hist_reject:
                        _bump_skip(hist_reject)
                        log_rejected_candidate(
                            strategy="bitcoin",
                            window="5m",
                            side=effective_side,
                            action=action,
                            reason=hist_reject,
                            market=market,
                            yes_price=yes_price,
                            est_prob_up=0.50,
                            htf_bias=htf_bias,
                            btc_1h_regime=btc_1h_regime if ta else None,
                            context={
                                "btc_1h_regime": btc_1h_regime if ta else None,
                                "macd_4h_histogram_rising": bool(macd_4h.histogram_rising),
                                "macd_1h_histogram_rising": bool(macd_1h.histogram_rising),
                                "macd_4h_above_zero": bool(macd_4h.above_zero),
                                "sabre_trend": int(getattr(sabre, "trend", 0) or 0),
                                **build_market_context(
                                    asset_spot=ta.current_price,
                                    btc_spot=ta.current_price,
                                    rsi_14=ta.rsi_14,
                                    atr_14=getattr(ta.trend_sabre, "atr", None),
                                ),
                            },
                            probe_variants=build_threshold_probe_variants(
                                metric_name="hist_support_count",
                                observed_value=float(
                                    (
                                        int(bool(macd_4h.histogram_rising))
                                        + int(bool(macd_1h.histogram_rising))
                                    )
                                    if effective_side == "LONG"
                                    else (
                                        int(not bool(macd_4h.histogram_rising))
                                        + int(not bool(macd_1h.histogram_rising))
                                    )
                                ),
                                baseline_threshold=1.0,
                                relax_steps=[1.0],
                                tighten_steps=[1.0],
                            ),
                            policy_version="hist_gate_support_count_v1",
                        )
                        logger.info(
                            f"  BTC [5m] skip '{market.question[:40]}' — {hist_reject}"
                        )
                        continue

                    quant = compute_btc_5m_quant(
                        sabre=sabre,
                        macd_4h=macd_4h,
                        macd_1h=macd_1h,
                        rsi_14=ta.rsi_14,
                        allowed_side=effective_side,
                        yes_price=yes_price,
                        m5_direction=m5_dir,
                        m5_in_prediction_window=bool(mom.m5_in_prediction_window),
                        hard_hist_gate=not (
                            hist_reject == "hist_gate_5m_short_reject"
                            and not bool(
                                self.config.get("hist_gate_5m_short_hard_reject", True)
                            )
                        ),
                    )
                    if quant.rsi_blocked:
                        _bump_skip("rsi_overbought_5m")
                        logger.debug(
                            f"  BTC skip '{market.question[:40]}' — "
                            f"5m LONG blocked: RSI={ta.rsi_14:.0f} > 65"
                        )
                        continue

                    htf_boost = quant.htf_boost
                    m5_adj = quant.m5_adj
                    m5_reasons = [f"5m {m5_dir}({mom.m5_move_pct:+.3f}%)"]
                    if mom.m5_in_prediction_window:
                        m5_reasons.append("5m predict window")

                    raw_est_prob = quant.est_prob_up
                    if resolution.confidence_penalty > 0:
                        raw_est_prob += (
                            -resolution.confidence_penalty
                            if allowed_side == "LONG"
                            else resolution.confidence_penalty
                        )
                    direction_decision = self._resolve_btc_direction(
                        htf_bias=htf_bias,
                        allowed_side=allowed_side,
                        macd_4h=macd_4h,
                        reason_parts=reason_parts,
                        raw_est_prob=raw_est_prob,
                        mom=mom,
                        current=direction_decision,
                    )
                    action = direction_decision.action
                    direction = direction_decision.direction
                    effective_side = direction_decision.effective_side
                    side_source = direction_decision.side_source
                    estimated_prob = self._calibrate_est_prob(
                        raw_est_prob,
                        action=action,
                        direction=direction,
                        window_size=_updown_tf,
                        signal_reason=" | ".join(r for r in reason_parts if r),
                        htf_bias=htf_bias,
                        side_source=direction_decision.side_source,
                        resolver_path=direction_decision.resolver_path,
                    )
                    edge = edge_for_action(
                        estimated_prob=estimated_prob,
                        yes_price=yes_price,
                        action=action,
                    )
                    confidence = quant.confidence
                    _direction_guard = self._btc_direction_guard_reason(
                        window_size=_updown_tf,
                        decision=direction_decision,
                        yes_price=yes_price,
                        btc_1h_regime=btc_1h_regime if ta else None,
                        eval_mins_left=float(_eval_left),
                    )
                    if _direction_guard:
                        _bump_skip(_direction_guard)
                        if action == "BUY_NO":
                            _record_buy_no_skip(
                                market=market,
                                skip_reason=_direction_guard,
                                yes_price=yes_price,
                                edge=edge,
                                effective_min_edge=float(self._tf_cfg(_updown_tf, "min_edge", self.config.get("min_edge", 0.0) or 0.0)),
                                rsi=ta.rsi_14,
                                htf_bias_value=htf_bias,
                                signal_reason=" | ".join(r for r in reason_parts if r),
                                window_size=_updown_tf,
                            )
                        continue

                    reason_parts.extend([
                        "[5m]",
                        "UPDOWN_5m",
                        f"btc=${btc_price:,.0f}",
                        f"est_up={estimated_prob:.3f}",
                        f"mkt_yes={yes_price:.3f}",
                        f"4H_MACD={'+'if macd_4h.above_zero else '-'}{abs(macd_4h.histogram):.0f}",
                        f"5m_mom={m5_dir}({mom.m5_move_pct:+.3f}%)",
                        f"RSI={ta.rsi_14:.0f}",
                        f"Sabre={'B' if sabre.trend==1 else 'S'} t={sabre.tension:+.1f}",
                    ])
                    reason_parts.extend(m5_reasons)
                    reason_parts.extend(ltf_reasons)

                    logger.debug(
                        f"  [5m] BTC updown '{market.question[:45]}' "
                        f"htf={htf_boost:+.2f} m5_adj={m5_adj:+.2f} "
                        f"est_up={estimated_prob:.3f} edge={edge:.4f}"
                    )

                else:
                    # ── LONGER-CYCLE UP/DOWN MARKET PATH (15m / 1h) ──
                    # Estimate probability from technical analysis
                    # Base: 0.50 (coin flip) + adjustments from HTF, LTF and timing
                    est_prob_up = 0.50
                    window_label = "1h" if is_1h else "15m"
                    # (2) HTF boost weights pulled from config so we can tune without code edits.
                    # Defaults bumped 2026-05-20 from 0.09/0.04 (1h) and 0.08/0.03 (15m).
                    # See config/settings.yaml btc_htf_boost_* keys for rationale.
                    if is_1h:
                        htf_boost_strong = float(self.config.get("btc_htf_boost_strong_1h", 0.14))
                        htf_boost_weak = float(self.config.get("btc_htf_boost_weak_1h", 0.07))
                    else:
                        htf_boost_strong = float(self.config.get("btc_htf_boost_strong_15m", 0.12))
                        htf_boost_weak = float(self.config.get("btc_htf_boost_weak_15m", 0.06))
                    ltf_weight = 0.12 if is_1h else 0.20
                    timing_weight = 0.50 if is_1h else 1.00
                    rsi_extreme = 0.02 if is_1h else 0.03
                    rsi_mid = 0.01 if is_1h else 0.02
                    vp_chop_penalty = 0.005 if is_1h else 0.01

                    # HTF bias — requires ALL 3 votes (Sabre + price vs MA + 4H MACD).
                    # 2/3 votes produce near-random win rates that can't cover slippage.
                    htf_boost = 0.0
                    _price_above_ma = btc_price > sabre.ma_value
                    if sabre.trend == 1 and _price_above_ma and macd_4h.above_zero:
                        htf_boost = htf_boost_strong
                    elif sabre.trend == -1 and not _price_above_ma and not macd_4h.above_zero:
                        htf_boost = -htf_boost_strong
                    elif sabre.trend == 1 and macd_4h.above_zero:
                        htf_boost = htf_boost_weak
                    elif sabre.trend == -1 and not macd_4h.above_zero:
                        htf_boost = -htf_boost_weak
                    # else: mixed votes → no directional boost (htf_boost stays 0.0)
                    est_prob_up += htf_boost
                    if resolution.confidence_penalty > 0:
                        est_prob_up += (
                            -resolution.confidence_penalty
                            if allowed_side == "LONG"
                            else resolution.confidence_penalty
                        )

                    # 4H/1H HISTOGRAM GATE (matches backtest engine)
                    # BTC 15m: without gate 50.7% WR; with gate 53.4% WR → improved further with anti-LTF.
                    # Primary: 4H histogram must be building in trade direction.
                    # Fallback: if 4H is decelerating but 1H is building, allow entry
                    # (catches local momentum recovery within larger trend structure).
                    macd_1h = ta.macd_1h
                    if effective_side == "LONG" and not macd_4h.histogram_rising:
                        if not macd_1h.histogram_rising:
                            _bump_skip(f"hist_gate_{window_label}_long_reject")
                            log_rejected_candidate(
                                strategy="bitcoin", window=window_label, side="LONG", action=action,
                                reason=f"hist_gate_{window_label}_long_reject", market=market,
                                yes_price=yes_price, est_prob_up=est_prob_up, htf_bias=htf_bias,
                                btc_1h_regime=btc_1h_regime if ta else None,
                                context={
                                    "btc_1h_regime": btc_1h_regime if ta else None,
                                    "macd_4h_histogram_rising": bool(macd_4h.histogram_rising),
                                    "macd_1h_histogram_rising": bool(macd_1h.histogram_rising),
                                    "macd_4h_above_zero": bool(macd_4h.above_zero),
                                    "sabre_trend": int(getattr(sabre, "trend", 0) or 0),
                                    **build_market_context(
                                        asset_spot=ta.current_price,
                                        btc_spot=ta.current_price,
                                        rsi_14=ta.rsi_14,
                                        atr_14=getattr(ta.trend_sabre, "atr", None),
                                    ),
                                },
                                probe_variants=build_threshold_probe_variants(
                                    metric_name="hist_support_count",
                                    observed_value=float(
                                        int(bool(macd_4h.histogram_rising))
                                        + int(bool(macd_1h.histogram_rising))
                                    ),
                                    baseline_threshold=1.0,
                                    relax_steps=[1.0],
                                    tighten_steps=[1.0],
                                ),
                                policy_version="hist_gate_support_count_v1",
                            )
                            logger.info(
                                f"  BTC [{window_label}] skip '{market.question[:40]}' — "
                                f"4H falling, 1H also falling — no momentum building for LONG"
                            )
                            continue
                        logger.info(
                            f"  BTC [{window_label}] 1H gate pass '{market.question[:40]}' — "
                            f"4H falling but 1H rising — local momentum recovery"
                        )
                    if effective_side == "SHORT" and macd_4h.histogram_rising:
                        if macd_1h.histogram_rising:
                            short_hist_penalty = float(
                                self.config.get(
                                    f"hist_gate_{window_label}_short_penalty",
                                    0.03 if is_1h else 0.025,
                                )
                            )
                            est_prob_up += short_hist_penalty
                            reason_parts.append(
                                f"hist_gate_{window_label}_short_penalty={short_hist_penalty:.3f}"
                            )
                            logger.info(
                                f"  BTC [{window_label}] penalize '{market.question[:40]}' — "
                                f"4H rising, 1H also rising — reduce SHORT conviction by {short_hist_penalty:.3f}"
                            )
                        else:
                            logger.info(
                                f"  BTC [{window_label}] 1H gate pass '{market.question[:40]}' — "
                                f"4H rising but 1H falling — local momentum recovery SHORT"
                            )

                    # LTF confirmation adds conviction
                    ltf_adj = ltf_strength * ltf_weight
                    est_prob_up += ltf_adj if effective_side == "LONG" else -ltf_adj

                    # Timing/momentum adds more
                    if effective_side == "LONG":
                        est_prob_up += timing_bonus * timing_weight
                    else:
                        est_prob_up -= timing_bonus * timing_weight

                    # RSI adjustments — expanded from 80/20 to 65/35 range.
                    # Live data: RSI 65-70 during SHORT had zero weight but is a real overbought signal.
                    # Very extreme (>80/<20) gets full -0.03/+0.03; mid-extreme (65-80/20-35) gets -0.02/+0.02
                    if ta.rsi_14 > 80:
                        est_prob_up -= rsi_extreme
                    elif ta.rsi_14 > 65:
                        est_prob_up -= rsi_mid
                    elif ta.rsi_14 < 20:
                        est_prob_up += rsi_extreme
                    elif ta.rsi_14 < 35:
                        est_prob_up += rsi_mid

                    # Sabre tension — lowered threshold from 4.0 to 2.0 to match threshold-market path.
                    # 72.9% of signals have tension_abs > 2.0; the 4.0 threshold almost never fired.
                    # Price stretched >2 ATR from the MA is meaningful mean-reversion risk.
                    if ta.trend_sabre.tension_abs > 2.0:
                        if effective_side == "LONG":
                            est_prob_up += -0.02 if ta.trend_sabre.tension > 0 else 0.02
                        else:
                            est_prob_up += 0.02 if ta.trend_sabre.tension > 0 else -0.02

                    # NOTE: 4H histogram hard gate is applied above (continue on mismatch).
                    # If we reach here, 4H histogram is already aligned — no extra soft boost needed.

                    # Volume profile context — price at key level
                    vp = ta.volume_profile
                    if vp.poc_price > 0:
                        price_vs_poc = (btc_price - vp.poc_price) / vp.poc_price
                        if abs(price_vs_poc) < 0.003:
                            est_prob_up -= vp_chop_penalty
                        reason_parts.append(f"VP_POC=${vp.poc_price:,.0f}")

                    est_prob_up = max(0.10, min(0.90, est_prob_up))
                    raw_est_prob = est_prob_up
                    direction_decision = self._resolve_btc_direction(
                        htf_bias=htf_bias,
                        allowed_side=allowed_side,
                        macd_4h=macd_4h,
                        reason_parts=reason_parts,
                        raw_est_prob=raw_est_prob,
                        mom=mom,
                        current=direction_decision,
                    )
                    action = direction_decision.action
                    direction = direction_decision.direction
                    effective_side = direction_decision.effective_side
                    side_source = direction_decision.side_source
                    estimated_prob = self._calibrate_est_prob(
                        raw_est_prob,
                        action=action,
                        direction=direction,
                        window_size=_updown_tf,
                        signal_reason=" | ".join(r for r in reason_parts if r),
                        htf_bias=htf_bias,
                        side_source=direction_decision.side_source,
                        resolver_path=direction_decision.resolver_path,
                    )

                    # BTC BUY_YES asymmetric floor — model systematically
                    # under-shoots UP probability under BULLISH bias, so in-window
                    # BUY_YES is rejected as negative edge while the ghost log
                    # settles those same candidates at 71–73% WR / +0.07 per $1
                    # (5m+15m, 2026-05-28). Per-window config'd lift, clamped to
                    # 0.90. Asymmetric — BUY_NO path untouched. Any window's bump
                    # = 0.0 disables it without redeploy.
                    if action == "BUY_YES" and htf_bias == "BULLISH":
                        _bump_default = 0.08 if is_1h else 0.0
                        _floor_bump = float(
                            self.config.get(
                                f"btc_{_updown_tf}_buy_yes_bullish_floor_bump",
                                _bump_default,
                            )
                        )
                        if _floor_bump > 0:
                            estimated_prob = min(0.90, estimated_prob + _floor_bump)
                            reason_parts.append(
                                f"btc_{_updown_tf}_buy_yes_floor=+{_floor_bump:.2f}"
                            )

                    if action == "BUY_YES":
                        edge = estimated_prob - yes_price
                    else:
                        edge = (1.0 - estimated_prob) - (1.0 - yes_price)

                    confidence = min(0.85, 0.50 + ltf_strength * ltf_weight + abs(timing_bonus) * timing_weight)
                    _direction_guard = self._btc_direction_guard_reason(
                        window_size=_updown_tf,
                        decision=direction_decision,
                        yes_price=yes_price,
                        btc_1h_regime=btc_1h_regime if ta else None,
                        eval_mins_left=float(_eval_left),
                    )
                    if _direction_guard:
                        _bump_skip(_direction_guard)
                        if action == "BUY_NO":
                            _record_buy_no_skip(
                                market=market,
                                skip_reason=_direction_guard,
                                yes_price=yes_price,
                                edge=edge,
                                effective_min_edge=float(self._tf_cfg(_updown_tf, "min_edge", self.config.get("min_edge", 0.0) or 0.0)),
                                rsi=ta.rsi_14,
                                htf_bias_value=htf_bias,
                                signal_reason=" | ".join(r for r in reason_parts if r),
                                window_size=_updown_tf,
                            )
                        continue

                    reason_parts.extend([
                        f"UPDOWN_{window_label}",
                        f"btc=${btc_price:,.0f}",
                        f"est_up={estimated_prob:.3f}",
                        f"mkt_yes={yes_price:.3f}",
                        f"4H_MACD={'+'if macd_4h.above_zero else '-'}{abs(macd_4h.histogram):.0f}",
                        (
                            f"1H_MACD={'+' if macd_1h.macd_line > macd_1h.signal_line else '-'}"
                            f"{abs(macd_1h.histogram):.1f}"
                            if is_1h
                            else f"15m_MACD={'+' if macd_15m.macd_line > macd_15m.signal_line else '-'}{abs(macd_15m.histogram):.1f}"
                        ),
                        f"RSI={ta.rsi_14:.0f}",
                        f"Sabre={'B' if sabre.trend==1 else 'S'} t={sabre.tension:+.1f}",
                    ])
                    reason_parts.extend(ltf_reasons)
                    if timing_reasons:
                        reason_parts.extend(timing_reasons)

            else:
                # ── TRADITIONAL THRESHOLD MARKETS ──
                direction = self._extract_direction(market.question)
                threshold = self._extract_price_threshold(market.question)

                days_to_resolution = 30
                if market.end_date:
                    end_date = market.end_date
                    if end_date.tzinfo is None:
                        end_date = end_date.replace(tzinfo=timezone.utc)
                    days_to_resolution = max(
                        1, (end_date - datetime.now(timezone.utc)).days
                    )

                # Enforce HTF gate on market direction
                if allowed_side == "LONG":
                    if direction == "UP":
                        action = "BUY_YES"
                    else:
                        action = "BUY_NO"
                else:
                    if direction == "UP":
                        action = "BUY_NO"
                    else:
                        action = "BUY_YES"
                action_counts[action] = action_counts.get(action, 0) + 1

                if threshold:
                    distance_pct = abs(btc_price - threshold) / threshold
                    estimated_prob = self._estimate_probability(
                        btc_price, threshold, direction, ta,
                        days_to_resolution, ltf_strength, timing_bonus,
                        regime=btc_1h_regime,
                    )
                    raw_est_prob = estimated_prob
                    estimated_prob = self._calibrate_est_prob(
                        raw_est_prob,
                        action=action,
                        direction=direction,
                        window_size="15m",
                        signal_reason=" | ".join(r for r in reason_parts if r),
                        htf_bias=htf_bias,
                        side_source=direction_decision.side_source if direction_decision else side_source,
                        resolver_path=direction_decision.resolver_path if direction_decision else None,
                    )

                    if action == "BUY_YES":
                        edge = estimated_prob - yes_price
                    else:
                        edge = (1.0 - estimated_prob) - (1.0 - yes_price)

                    reason_parts.extend([
                        f"btc=${btc_price:,.0f}",
                        f"target=${threshold:,.0f}",
                        f"dist={distance_pct:.1%}",
                        f"est_prob={estimated_prob:.2f}",
                        f"mkt_yes={yes_price:.2f}",
                        f"4H_MACD={'+'if macd_4h.above_zero else '-'}{abs(macd_4h.histogram):.0f}",
                        f"15m_MACD={'+' if macd_15m.macd_line > macd_15m.signal_line else '-'}{abs(macd_15m.histogram):.1f}",
                        f"Sabre={'B' if sabre.trend==1 else 'S'}",
                    ])
                    reason_parts.extend(ltf_reasons)
                    if timing_reasons:
                        reason_parts.extend(timing_reasons)
                    vp = ta.volume_profile
                    if vp.poc_price > 0:
                        reason_parts.append(f"VP_POC=${vp.poc_price:,.0f}")
                        reason_parts.append(f"VAH=${vp.vah_price:,.0f}")
                        reason_parts.append(f"VAL=${vp.val_price:,.0f}")

                    confidence = min(0.85, 0.50 + ltf_strength * 0.20 + timing_bonus + distance_pct * 0.5)

                    # Marginal edge → AI tiebreaker (skipped when AI offline or use_ai false)
                    _marginal_min_edge = self._min_edge_for_window(
                        _updown_tf if is_updown else "15m"
                    )
                    if edge < _marginal_min_edge and edge > 0.03:
                        if not self.config.get("use_ai", True):
                            logger.debug(
                                f"BTC: use_ai=false — skipping marginal trade "
                                f"'{market.question[:40]}...' edge={edge:.4f}"
                            )
                            continue
                        if not self.ai_agent.is_available():
                            logger.debug(
                                f"BTC: AI offline — skipping marginal trade "
                                f"'{market.question[:40]}...' edge={edge:.4f}"
                            )
                            continue
                        if ai_calls >= self.max_ai_calls_per_scan:
                            logger.debug(
                                f"BTC: max AI calls per scan ({self.max_ai_calls_per_scan}) — "
                                f"skipping marginal '{market.question[:40]}...'"
                            )
                            continue
                        ai_context = (
                            f"{market.description}\n\n"
                            f"=== LIVE BTC DATA ===\n"
                            f"BTC Price: ${btc_price:,.2f} | Threshold: ${threshold:,.0f} ({direction})\n"
                            f"Distance: {distance_pct:.1%} | Days left: {days_to_resolution}\n\n"
                            f"=== HIGHER TF (4H) — {htf_bias} ===\n"
                            f"Sabre: {'BULL' if sabre.trend==1 else 'BEAR'} MA=${sabre.ma_value:,.0f} Trail=${sabre.trail_value:,.0f}\n"
                            f"4H MACD: hist={macd_4h.histogram:+.0f} {'above' if macd_4h.above_zero else 'below'} zero {macd_4h.crossover}\n\n"
                            f"=== LOWER TF (15m) CONFIRMATION ===\n"
                            f"15m MACD: hist={macd_15m.histogram:+.2f} {macd_15m.crossover}\n"
                            f"Allowed side: {allowed_side}\n\n"
                            f"=== CONTEXT ===\n"
                            f"RSI: {ta.rsi_14:.1f} | S=${ta.nearest_support:,.0f} R=${ta.nearest_resistance:,.0f}\n"
                            f"Candle: 15m={mom.m15_direction}({mom.m15_move_pct:+.3f}%) 5m={mom.m5_direction}\n"
                            f"\nThe 4H trend bias is {htf_bias}. Based on ALL the data above, "
                            f"what is the probability BTC will be above ${threshold:,.0f} at resolution? "
                            f"Give your independent assessment — BUY_YES, BUY_NO, or HOLD.\n"
                            f"\n=== MARKET ===\n{format_market_metadata(market)}"
                        )
                        ai_lane_id = str(
                            build_lane_metadata(
                                strategy="bitcoin",
                                window_size="15m" if "15m" in (market.question or "").lower() else ("5m" if "5m" in (market.question or "").lower() else "unknown"),
                                action=action,
                                direction=direction,
                                entry_leg=("NO" if action == "BUY_NO" else "YES"),
                                ai_used=True,
                                reason="ai_confirm",
                                signal_reason="ai_confirm",
                                htf_bias=htf_bias,
                            ).get("lane_id")
                            or ""
                        )
                        ai_analysis = await self._analyze_market_with_timeout(
                            market_question=market.question,
                            market_description=ai_context,
                            current_yes_price=yes_price,
                            market_id=market.id,
                            strategy_hint="bitcoin",
                            lane_id=ai_lane_id,
                            quant_action=action,
                        )
                        ai_calls += 1
                        ai_used = True
                        if not ai_analysis:
                            logger.warning(
                                "BTC: AI returned None after provider call for market %s — "
                                "LLM chain failed or response invalid (see prior AI logs)",
                                market.id,
                            )
                            continue
                        if ai_analysis.recommendation == "HOLD":
                            logger.debug(f"BTC: AI says HOLD on '{market.question[:40]}...'")
                            self._ai_hold_cache[market.id] = time.time()
                            continue
                        if not ai_recommendation_supports_action(ai_analysis.recommendation, action):
                            logger.debug(
                                f"BTC: AI recommendation {ai_analysis.recommendation} conflicts with "
                                f"{action} on '{market.question[:40]}...'"
                            )
                            continue
                        if self.ai_agent.preentry_veto_active(ai_analysis.confidence_score):
                            preentry_veto_skips += 1
                            _bump_skip("ai_preentry_veto")
                            logger.info(
                                f"BTC: pre-entry veto — AI conf {ai_analysis.confidence_score:.2f} "
                                f"on '{market.question[:40]}...' (action={action})"
                            )
                            continue
                        ai_edge = (
                            ai_analysis.estimated_probability - yes_price
                            if action == "BUY_YES"
                            else yes_price - ai_analysis.estimated_probability
                        )
                        edge = max(edge, ai_edge)
                        confidence = ai_analysis.confidence_score
                        reason_parts.append("ai_confirm")
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
                                    strategy_hint="bitcoin",
                                    lane_id=ai_lane_id,
                                    marginal_recommendation=str(ai_analysis.recommendation),
                                    quant_action=action,
                                    quant_edge=edge,
                                    quant_threshold=_marginal_min_edge,
                                    existing_research=None,
                                )
                                if shadow_out and shadow_out.get("ok"):
                                    shadow_pipeline_ok += 1
                            except Exception as e:
                                logger.debug(
                                    "BTC shadow pipeline failed market=%s: %s",
                                    market.id,
                                    e,
                                )

                else:
                    # No threshold — requires AI for probability estimate.
                    # Skip entirely when use_ai is off or AI is offline (no quant signal available).
                    if not self.config.get("use_ai", True):
                        logger.debug(
                            f"BTC: use_ai=false — skipping non-threshold market "
                            f"'{market.question[:40]}...'"
                        )
                        continue
                    if not self.ai_agent.is_available():
                        logger.debug(
                            f"BTC: AI offline — skipping non-threshold market "
                            f"'{market.question[:40]}...'"
                        )
                        continue
                    if ai_calls >= self.max_ai_calls_per_scan:
                        logger.debug(
                            f"BTC: max AI calls per scan ({self.max_ai_calls_per_scan}) — "
                            f"skipping non-threshold '{market.question[:40]}...'"
                        )
                        continue
                    # AI only, but STILL gated by HTF
                    ai_context = (
                        f"{market.description}\n\n"
                        f"BTC: ${btc_price:,.2f} | 4H Trend: {htf_bias}\n"
                        f"Sabre: {'BULL' if sabre.trend==1 else 'BEAR'} | "
                        f"4H MACD: {macd_4h.crossover} hist={macd_4h.histogram:+.0f} | "
                        f"15m MACD: {macd_15m.crossover}\n"
                        f"RSI: {ta.rsi_14:.0f}\n"
                        f"\nBased on the data above, what is your independent probability "
                        f"assessment for this market? Reply BUY_YES, BUY_NO, or HOLD.\n"
                        f"\n=== MARKET ===\n{format_market_metadata(market)}"
                    )
                    ai_lane_id = str(
                        build_lane_metadata(
                            strategy="bitcoin",
                            window_size="15m" if "15m" in (market.question or "").lower() else ("5m" if "5m" in (market.question or "").lower() else "unknown"),
                            action=action,
                            direction=direction,
                            entry_leg=("NO" if action == "BUY_NO" else "YES"),
                            ai_used=True,
                            reason="ai_only",
                            signal_reason="ai_only",
                            htf_bias=htf_bias,
                        ).get("lane_id")
                        or ""
                    )
                    ai_analysis = await self._analyze_market_with_timeout(
                        market_question=market.question,
                        market_description=ai_context,
                        current_yes_price=yes_price,
                        market_id=market.id,
                        strategy_hint="bitcoin",
                        lane_id=ai_lane_id,
                        quant_action=action,
                    )
                    ai_calls += 1
                    ai_used = True
                    if not ai_analysis:
                        logger.warning(
                            "BTC: AI returned None after provider call for market %s — "
                            "LLM chain failed or response invalid",
                            market.id,
                        )
                        continue
                    if ai_analysis.recommendation == "HOLD":
                        self._ai_hold_cache[market.id] = time.time()
                        continue
                    if not ai_recommendation_supports_action(ai_analysis.recommendation, action):
                        logger.debug(
                            f"BTC: AI recommendation {ai_analysis.recommendation} conflicts with "
                            f"{action} on '{market.question[:40]}...'"
                        )
                        continue
                    if self.ai_agent.preentry_veto_active(ai_analysis.confidence_score):
                        preentry_veto_skips += 1
                        _bump_skip("ai_preentry_veto")
                        logger.info(
                            f"BTC: pre-entry veto (ai_only) — AI conf {ai_analysis.confidence_score:.2f} "
                            f"on '{market.question[:40]}...'"
                        )
                        continue
                    edge = abs(ai_analysis.estimated_probability - yes_price) - 0.02
                    confidence = ai_analysis.confidence_score
                    reason_parts.append(f"ai_only btc=${btc_price:,.0f}")
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
                                strategy_hint="bitcoin",
                                lane_id=ai_lane_id,
                                marginal_recommendation=str(ai_analysis.recommendation),
                                quant_action=action,
                                quant_edge=edge,
                                quant_threshold=_marginal_min_edge,
                                existing_research=None,
                            )
                            if shadow_out and shadow_out.get("ok"):
                                shadow_pipeline_ok += 1
                        except Exception as e:
                            logger.debug(
                                "BTC shadow pipeline failed (ai_only) market=%s: %s",
                                market.id,
                                e,
                            )

            # ── Final filters ──
            lane_side, lane_policy = self._resolve_lane_entry_policy(
                window_size=_updown_tf if is_updown else "15m",
                action=action,
                direction=direction,
            )
            entry_policy_meta = entry_policy_to_dict(
                lane_policy,
                strategy_name=self._signal_strategy_name,
                window_size=_updown_tf if is_updown else "15m",
                side=lane_side,
            )
            if is_updown and not lane_policy.enabled:
                _bump_skip("lane_disabled")
                logger.info(
                    "  BTC skip '%s' %s — lane disabled side=%s window=%s",
                    market.question[:45],
                    action,
                    lane_side,
                    _updown_tf,
                )
                continue
            if is_updown and (_eval_left < lane_policy.entry_window_min or _eval_left > lane_policy.entry_window_max):
                _bump_skip("lane_entry_window")
                logger.debug(
                    "  BTC skip '%s' — %.1fm left (eval %.2f), lane=%s needs %.2f–%.2fm",
                    market.question[:40],
                    _mins_left,
                    _eval_left,
                    lane_side,
                    lane_policy.entry_window_min,
                    lane_policy.entry_window_max,
                )
                continue
            effective_min_edge = max(lane_policy.min_edge, lane_policy.hard_min_edge)
            # NEUTRAL HTF: no confirmed bias — demand stronger edge for updown leans.
            # Applies to both 5m and 15m: the 5m path has zero backtest coverage under NEUTRAL
            # (all 1735 trades in Apr-2026 BTC 5m backtest were BULLISH/BEARISH only).
            if htf_bias == "NEUTRAL" and is_updown:
                effective_min_edge = max(effective_min_edge, 0.09)
                if _updown_tf != "5m":
                    effective_min_edge = max(effective_min_edge, self.min_edge_15m_neutral)
                    reason_parts.append(f"neutral_15m_min_edge={self.min_edge_15m_neutral:.3f}")
                    _sample("neutral_15m_min_edge", self.min_edge_15m_neutral)
            if (
                is_updown
                and self.neutral_rsi_extra_min_edge > 0
                and self.neutral_rsi_min <= ta.rsi_14 <= self.neutral_rsi_max
            ):
                effective_min_edge += self.neutral_rsi_extra_min_edge
                reason_parts.append(
                    f"neutral_rsi_penalty={self.neutral_rsi_extra_min_edge:.3f}"
                )
                _sample("neutral_rsi_penalty", self.neutral_rsi_extra_min_edge)

            effective_min_edge *= get_drift_min_edge_mult("bitcoin", self.full_config)
            effective_min_edge *= get_loosen_min_edge_mult(
                "bitcoin",
                self.full_config,
                window=_updown_tf if is_updown else "15m",
                side=lane_side,
                regime=htf_bias,
            )

            # ── AI-hold soft veto ────────────────────────────────────────────
            # If AI said HOLD on this market within the last ai_hold_veto_ttl_sec,
            # the 5m quant path must clear the higher min_edge_5m_ai_override
            # threshold. Closes the gap where AI is skeptical but quant fires anyway.
            _hold_ts = self._ai_hold_cache.get(market.id, 0)
            _hold_age = time.time() - _hold_ts
            if _hold_age < self.ai_hold_veto_ttl_sec:
                _lane_ai_override = max(
                    lane_policy.ai_override_min_edge,
                    lane_policy.min_edge,
                )
                if edge < _lane_ai_override:
                    _bump_skip("ai_hold_veto_active")
                    logger.info(
                        f"  BTC ai-hold veto '{market.question[:45]}' "
                        f"— edge={edge:.4f} < override={_lane_ai_override:.4f} "
                        f"(AI said HOLD {_hold_age:.0f}s ago)"
                    )
                    continue

            _needs_ai_for_low_conf_neutral_15m = (
                is_updown
                and _updown_tf != "5m"
                and htf_bias == "NEUTRAL"
                and self.neutral_15m_min_quant_confidence > 0
                and confidence < self.neutral_15m_min_quant_confidence
            )

            # Updown AI assist: 5m is optional and disabled by default because LLM
            # latency can consume most of a 60s unified cycle. 15m keeps AI for
            # borderline edge or low-confidence neutral HTF setups.
            _ai_updown_5m = is_updown and is_5m and self.use_ai_updown_5m
            _ai_updown_15m_borderline = (
                is_updown
                and _updown_tf != "5m"
                and (
                    (
                        edge < effective_min_edge
                        and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                    )
                    or _needs_ai_for_low_conf_neutral_15m
                )
            )
            if (
                (_ai_updown_5m or (_ai_updown_15m_borderline and _timing_window_open))
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and self.ai_agent.is_available()
                and ai_calls < self.max_ai_calls_per_scan
            ):
                _window = _updown_tf if is_updown else "15m"
                ai_context = (
                    f"{market.description}\n\n"
                    f"=== BTC UPDOWN CONTEXT ({_window}) ===\n"
                    f"BTC Price: ${btc_price:,.2f}\n"
                    f"Market YES Price: {yes_price:.3f} | Quant confidence={confidence:.2f}\n"
                    f"HTF bias: {htf_bias} | Quant edge={edge:.4f} (threshold={effective_min_edge:.4f})\n\n"
                    f"4H MACD hist={macd_4h.histogram:+.2f} above0={macd_4h.above_zero} rising={macd_4h.histogram_rising}\n"
                    f"15m MACD hist={macd_15m.histogram:+.2f} cross={macd_15m.crossover}\n"
                    f"1H MACD hist={ta.macd_1h.histogram:+.2f} rising={ta.macd_1h.histogram_rising}\n"
                    f"Momentum: 15m={mom.m15_direction}({mom.m15_move_pct:+.3f}%) "
                    f"5m={mom.m5_direction}({mom.m5_move_pct:+.3f}%)\n"
                    f"RSI={ta.rsi_14:.1f} | Sabre trend={sabre.trend} tension={sabre.tension:+.2f}\n\n"
                    f"=== MARKET ===\n{format_market_metadata(market)}\n\n"
                    "Answer with BUY_YES, BUY_NO, or HOLD."
                )
                _broker_lane = (
                    "btc_neutral_15m_borderline"
                    if _needs_ai_for_low_conf_neutral_15m
                    else "btc_updown_marginal"
                )
                _broker_state, ai_decision = self._resolve_or_enqueue_ai(
                    lane_id=_broker_lane,
                    market=market,
                    ai_context=ai_context,
                    yes_price=yes_price,
                    edge=edge,
                    confidence=confidence,
                    action=action,
                    quant_threshold=effective_min_edge,
                    raw_est_prob=raw_est_prob,
                    estimated_prob=estimated_prob,
                    require_shadow_portfolio=(
                        self._requires_shadow_for_lane("neutral_15m")
                        if _needs_ai_for_low_conf_neutral_15m
                        else self._requires_shadow_for_lane("marginal")
                    ),
                    htf_bias=htf_bias,
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
                        strategy_hint="bitcoin",
                        quant_action=action,
                        quant_edge=edge,
                        quant_confidence=confidence,
                        quant_threshold=effective_min_edge,
                        raw_probability=raw_est_prob,
                        post_calibration_probability=estimated_prob,
                        require_shadow_portfolio=(
                            self._requires_shadow_for_lane("neutral_15m")
                            if _needs_ai_for_low_conf_neutral_15m
                            else self._requires_shadow_for_lane("marginal")
                        ),
                    )
                ai_calls += 1
                ai_used = True

                if ai_decision is None:
                    _bump_skip("ai_decision_timeout")
                    continue

                if ai_decision.shadow_result is not None:
                    shadow_pipeline_calls += 1
                    if ai_decision.shadow_result.get("ok"):
                        shadow_pipeline_ok += 1

                if not ai_decision.approved:
                    ai_decision_layer_skips += 1
                    _bump_skip(f"ai_decision_{ai_decision.reason}")
                    if ai_decision.reason in {"direct_ai_hold", "shadow_portfolio_hold"}:
                        ai_holds += 1
                        self._ai_hold_cache[market.id] = time.time()
                    if "mismatch" in ai_decision.reason:
                        ai_vetos += 1
                    logger.info(
                        f"  BTC AI decision skip '{market.question[:45]}' — "
                        f"{ai_decision.reason} action={ai_decision.action} "
                        f"conf={ai_decision.confidence:.2f}"
                    )
                    continue

                logger.info(
                    f"  BTC AI decision [{ai_decision.action} conf={ai_decision.confidence:.2f} "
                    f"edge={float(ai_decision.edge or 0.0):.4f}] "
                    f"'{market.question[:45]}' | {ai_decision.reason}"
                )
                if not ai_recommendation_supports_action(ai_decision.action, action):
                    ai_vetos += 1
                    _bump_skip("ai_veto_marginal_updown")
                    logger.info(
                        f"  BTC AI veto '{market.question[:45]}' — rec={ai_decision.action} "
                        f"conflicts with action={action}"
                    )
                    continue
                if ai_decision.confidence < self.ai_confidence_threshold:
                    _bump_skip("ai_low_confidence_marginal_updown")
                    logger.info(
                        f"  BTC AI skip '{market.question[:45]}' — confidence "
                        f"{ai_decision.confidence:.2f} < {self.ai_confidence_threshold:.2f}"
                    )
                    continue

                ai_edge = float(ai_decision.edge or 0.0)
                if ai_edge <= 0:
                    _bump_skip("ai_nonpositive_edge_marginal_updown")
                    logger.info(
                        f"  BTC AI skip '{market.question[:45]}' — non-positive ai_edge={ai_edge:.4f}"
                    )
                    continue

                edge = max(edge, ai_edge)
                confidence = max(confidence, ai_decision.confidence)
                ai_assists += 1
                reason_parts.append(f"ai_decision={ai_decision.source}")
                if _needs_ai_for_low_conf_neutral_15m:
                    reason_parts.append(
                        f"low_conf_ai_confirm={self.neutral_15m_min_quant_confidence:.2f}"
                    )
            elif (
                is_updown
                and edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and not _timing_window_open
            ):
                logger.debug(
                    f"  BTC AI window closed for marginal updown '{market.question[:40]}...' "
                    f"({_mins_left:.1f}m left)"
                )

            if _needs_ai_for_low_conf_neutral_15m and "low_conf_ai_confirm" not in " ".join(reason_parts):
                _bump_skip("neutral_15m_low_conf_no_ai")
                logger.info(
                    f"  BTC skip '{market.question[:45]}' {action} "
                    f"confidence={confidence:.2f} < neutral_15m_min_quant_confidence="
                    f"{self.neutral_15m_min_quant_confidence:.2f} without AI confirmation"
                )
                continue

            if is_updown:
                action_counts[action] = action_counts.get(action, 0) + 1

            try:
                _sample("est_prob_up", est_prob_up)
            except NameError:
                pass
            _sample("edge", edge)
            _bias_quant_size_multiplier = 1.0
            if edge < effective_min_edge:
                # (3) Flag bias/quant directional disagreement so we can isolate this cohort
                # in analysis. True when raw_est is on the opposite side of 0.50 from the
                # direction the htf_bias picked, OR when raw_est < yes_price under BULLISH
                # bias / raw_est > yes_price under BEARISH bias.
                _bias_quant_disagree = False
                if htf_bias == "BULLISH":
                    _bias_quant_disagree = float(raw_est_prob) < float(yes_price)
                elif htf_bias == "BEARISH":
                    _bias_quant_disagree = float(raw_est_prob) > float(yes_price)
                if (
                    _bias_quant_disagree
                    and self._maybe_bias_quant_disagree_override(
                        is_updown=is_updown,
                        window_size=_updown_tf if is_updown else "15m",
                        action=action,
                        htf_bias=htf_bias,
                        yes_price=yes_price,
                        raw_est_prob=raw_est_prob,
                        edge=edge,
                        reason_parts=reason_parts,
                    )
                ):
                    logger.info(
                        "  BTC allow '%s' %s — HTF-aligned bias/quant disagreement override "
                        "(edge=%.4f < min=%.4f)",
                        market.question[:45],
                        action,
                        edge,
                        effective_min_edge,
                    )
                    _bias_quant_size_multiplier = max(
                        0.0,
                        min(1.0, float(self.bias_quant_disagree_size_multiplier)),
                    )
                    if _bias_quant_size_multiplier < 0.999:
                        reason_parts.append(
                            f"bias_quant_size={_bias_quant_size_multiplier:.2f}x"
                        )
                else:
                    _vetoed = bool(getattr(self, "_last_calibration_vetoed", False))
                    if _vetoed:
                        _skip_reason = "beta_vetoed"
                    elif _bias_quant_disagree:
                        _skip_reason = "lane_min_edge_bias_quant_disagree"
                    else:
                        _skip_reason = "lane_min_edge"
                    _bump_skip(_skip_reason)
                    log_rejected_candidate(
                        strategy=self._signal_strategy_name,
                        window=_updown_tf if is_updown else "15m",
                        side=allowed_side,
                        action=action,
                        reason=_skip_reason,
                        market=market,
                        yes_price=yes_price,
                        est_prob_up=estimated_prob,
                        htf_bias=htf_bias,
                        btc_1h_regime=btc_1h_regime if ta else None,
                        context={
                            "btc_1h_regime": btc_1h_regime if ta else None,
                            "edge": round(float(edge), 6),
                            "effective_min_edge": round(float(effective_min_edge), 6),
                            "raw_est_prob": round(float(raw_est_prob), 6),
                            "estimated_prob": round(float(estimated_prob), 6),
                            "confidence": round(float(confidence), 6),
                            "side_source": side_source,
                            "conflict_type": (
                                direction_decision.conflict_type
                                if direction_decision is not None
                                else ""
                            ),
                            "resolver_path": (
                                direction_decision.resolver_path
                                if direction_decision is not None
                                else ""
                            ),
                            "htf_side": (
                                direction_decision.htf_side
                                if direction_decision is not None
                                else allowed_side
                            ),
                            "quant_side": (
                                direction_decision.quant_side
                                if direction_decision is not None
                                else None
                            ),
                            "momentum_side": (
                                direction_decision.momentum_side
                                if direction_decision is not None
                                else self._btc_momentum_side(mom)
                            ),
                            "bias_quant_disagree": bool(_bias_quant_disagree),
                            "beta_vetoed": _vetoed,
                            "calibration_lane_id": getattr(self, "_last_calibration_lane_id", ""),
                            **build_market_context(
                                asset_spot=ta.current_price,
                                btc_spot=ta.current_price,
                                rsi_14=ta.rsi_14,
                                atr_14=getattr(ta.trend_sabre, "atr", None),
                            ),
                        },
                        probe_variants=build_threshold_probe_variants(
                            metric_name="min_edge",
                            observed_value=float(edge),
                            baseline_threshold=float(effective_min_edge),
                        ),
                        policy_version="lane_min_edge_v1",
                    )
                    if action == "BUY_NO":
                        _record_buy_no_skip(
                            market=market,
                            skip_reason=_skip_reason,
                            yes_price=yes_price,
                            edge=edge,
                            effective_min_edge=effective_min_edge,
                            rsi=ta.rsi_14,
                            htf_bias_value=htf_bias,
                            signal_reason=" | ".join(reason_parts),
                            window_size=_updown_tf if is_updown else "threshold",
                        )
                    if not is_updown:
                        _mkt_type = "threshold"
                    elif htf_bias == "NEUTRAL":
                        _mkt_type = f"updown_{_updown_tf}_neutral"
                    else:
                        _mkt_type = f"updown_{_updown_tf}"
                    logger.info(
                        f"  BTC skip '{market.question[:45]}' {action} "
                        f"edge={edge:.4f} < min={effective_min_edge} | {_mkt_type}"
                    )
                    continue

            entry_convergence_score = None
            entry_composite_score = None
            _is_neutral_15m = is_updown and _updown_tf != "5m" and htf_bias == "NEUTRAL"
            if _is_neutral_15m:
                composite = self._score_neutral_15m_candidate(
                    edge=edge,
                    effective_min_edge=effective_min_edge,
                    confidence=confidence,
                    ltf_strength=ltf_strength,
                    minutes_left=_mins_left,
                    yes_price=yes_price,
                    action=action,
                    btc_1h_regime=btc_1h_regime if ta else None,
                )
                _sample("composite_score", composite.score)
                entry_convergence_score = composite.convergence_score
                entry_composite_score = composite.score
                reason_parts.append(f"composite={composite.score:.3f}")
                if not composite.passed:
                    _bump_skip(composite.reason)
                    logger.info(
                        "BTC neutral 15m composite skip '%s...' action=%s edge=%.4f "
                        "conf=%.2f score=%.3f floor=%.3f components=%s",
                        market.question[:45],
                        action,
                        edge,
                        confidence,
                        composite.score,
                        composite.floor,
                        composite.components,
                    )
                    continue

                if not ai_used:
                    if not self.config.get("use_ai", True) or not self.ai_agent.is_available():
                        _bump_skip("ai_unavailable_neutral_15m")
                        logger.info(
                            "BTC neutral 15m AI skip '%s...' action=%s edge=%.4f "
                            "conf=%.2f composite=%.3f",
                            market.question[:45],
                            action,
                            edge,
                            confidence,
                            composite.score,
                        )
                        continue
                    if ai_calls >= self.max_ai_calls_per_scan:
                        _bump_skip("ai_call_limit_neutral_15m")
                        continue
                    ai_context = (
                        f"{market.description}\n\n"
                        f"=== BTC NEUTRAL 15M ENFORCED CONTEXT ===\n"
                        f"BTC Price: ${btc_price:,.2f}\n"
                        f"Market YES Price: {yes_price:.3f} | Quant confidence={confidence:.2f}\n"
                        f"HTF bias: {htf_bias} | Quant edge={edge:.4f} "
                        f"(threshold={effective_min_edge:.4f})\n"
                        f"Composite={composite.score:.3f}/{composite.floor:.3f} "
                        f"components={composite.components}\n\n"
                        f"4H MACD hist={macd_4h.histogram:+.2f} above0={macd_4h.above_zero} rising={macd_4h.histogram_rising}\n"
                        f"15m MACD hist={macd_15m.histogram:+.2f} cross={macd_15m.crossover}\n"
                        f"1H MACD hist={ta.macd_1h.histogram:+.2f} rising={ta.macd_1h.histogram_rising}\n"
                        f"Momentum: 15m={mom.m15_direction}({mom.m15_move_pct:+.3f}%) "
                        f"5m={mom.m5_direction}({mom.m5_move_pct:+.3f}%)\n"
                        f"RSI={ta.rsi_14:.1f} | Sabre trend={sabre.trend} tension={sabre.tension:+.2f}\n\n"
                        f"=== MARKET ===\n{format_market_metadata(market)}\n\n"
                        "Answer with BUY_YES, BUY_NO, or HOLD."
                    )
                    _broker_state, ai_decision = self._resolve_or_enqueue_ai(
                        lane_id="btc_neutral_15m_enforced",
                        market=market,
                        ai_context=ai_context,
                        yes_price=yes_price,
                        edge=edge,
                        confidence=confidence,
                        action=action,
                        quant_threshold=effective_min_edge,
                        raw_est_prob=raw_est_prob,
                        estimated_prob=estimated_prob,
                        require_shadow_portfolio=self._requires_shadow_for_lane("neutral_15m"),
                        htf_bias=htf_bias,
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
                            strategy_hint="bitcoin",
                            quant_action=action,
                            quant_edge=edge,
                            quant_confidence=confidence,
                            quant_threshold=effective_min_edge,
                            raw_probability=raw_est_prob,
                            post_calibration_probability=estimated_prob,
                            require_shadow_portfolio=self._requires_shadow_for_lane("neutral_15m"),
                        )
                    ai_calls += 1
                    ai_used = True
                    if ai_decision is None:
                        _bump_skip("ai_decision_timeout")
                        continue
                    if ai_decision.shadow_result is not None:
                        shadow_pipeline_calls += 1
                        if ai_decision.shadow_result.get("ok"):
                            shadow_pipeline_ok += 1
                    if not ai_decision.approved:
                        ai_decision_layer_skips += 1
                        _bump_skip(f"ai_decision_{ai_decision.reason}")
                        logger.info(
                            "BTC neutral 15m AI rejected '%s...' reason=%s action=%s "
                            "conf=%.2f edge=%s composite=%.3f",
                            market.question[:45],
                            ai_decision.reason,
                            ai_decision.action,
                            ai_decision.confidence,
                            ai_decision.edge,
                            composite.score,
                        )
                        continue
                    ai_edge = float(ai_decision.edge or 0.0)
                    if ai_edge <= 0:
                        _bump_skip("ai_nonpositive_edge_neutral_15m")
                        continue
                    edge = max(edge, ai_edge)
                    confidence = max(confidence, ai_decision.confidence)
                    ai_assists += 1
                    reason_parts.append(f"ai_decision={ai_decision.source}")

            # ── Edge cap for updown markets ──
            # Treat high edge as a sizing issue, not an admission veto. Keep the
            # trade if the rest of the lane passes, but clamp Kelly sizing input
            # so inflated edge estimates cannot create oversized positions.
            sizing_edge = edge
            if is_updown:
                _max_edge_updown = self._max_edge_cap_for_updown(
                    window_label=_updown_tf,
                    side=effective_side,
                )
                if _max_edge_updown > 0 and edge > _max_edge_updown:
                    sizing_edge = _max_edge_updown
                    reason_parts.append(f"size_edge_cap={_max_edge_updown:.3f}")
                    logger.info(
                        f"  BTC sizing cap '{market.question[:45]}' {action} "
                        f"edge={edge:.4f} -> size_edge={sizing_edge:.4f} (max={_max_edge_updown})"
                    )

                # Updown-specific entry price band — symmetric around 0.50.
                # self.entry_price_min/max are for directional threshold markets (0.10-0.90).
                # Updown markets need a tighter band to avoid betting against strong consensus.
                #   BUY_YES at yes_price < 0.46: market is already bearish → no momentum edge
                #   BUY_NO at yes_price > 0.54: allow (cheap NO when YES is rich)
                #   BUY_YES at yes_price > 0.54 and BUY_NO at yes_price < 0.46: consensus extremes
                _up_min = lane_policy.entry_price_min
                _up_max = lane_policy.entry_price_max
                _updown_band_bad = (
                    (yes_price < _up_min or yes_price > _up_max)
                    if action == "BUY_YES"
                    else (
                        yes_price < _up_min
                        if action == "BUY_NO"
                        else (yes_price < _up_min or yes_price > _up_max)
                    )
                )
                if _updown_band_bad:
                    _bump_skip("lane_price_band")
                    if action == "BUY_NO":
                        _record_buy_no_skip(
                            market=market,
                            skip_reason="lane_price_band",
                            yes_price=yes_price,
                            edge=edge,
                            effective_min_edge=effective_min_edge,
                            rsi=ta.rsi_14,
                            htf_bias_value=htf_bias,
                            signal_reason=" | ".join(reason_parts),
                            window_size=_updown_tf if is_updown else "15m",
                        )
                    logger.info(
                        f"  BTC skip '{market.question[:45]}' {action} "
                        f"yes_price={yes_price:.3f} outside updown band [{_up_min:.2f}, {_up_max:.2f}]"
                    )
                    continue

            entry_price = yes_price if action == "BUY_YES" else (1.0 - yes_price)
            if entry_price < self.entry_price_min or entry_price > self.entry_price_max:
                _bump_skip("entry_price_out_of_range")
                continue

            if not self.kelly_sizer:
                _bump_skip("kelly_unavailable")
                logger.error("Bitcoin strategy: KellySizer unavailable — skipping entry sizing")
                continue
            raw_size = self.kelly_sizer.size_from_edge(
                self._signal_strategy_name, bankroll, sizing_edge
            )
            if raw_size <= 0:
                _bump_skip("kelly_nonpositive")
                continue

            if self._btc_1h_regime_gates.get("enabled", False):
                raw_size *= self._regime_size_mult(btc_1h_regime)

            if lane_policy.size_multiplier > 0:
                raw_size *= lane_policy.size_multiplier
            if _bias_quant_size_multiplier > 0 and _bias_quant_size_multiplier < 0.999:
                raw_size *= _bias_quant_size_multiplier

            # Apply dynamic exposure scaling
            size = self.exposure_manager.scale_size(raw_size)
            if size <= 0:
                _bump_skip("lane_size_too_small")
                if action == "BUY_NO":
                    _record_buy_no_skip(
                        market=market,
                        skip_reason="lane_size_too_small",
                        yes_price=yes_price,
                        edge=edge,
                        effective_min_edge=effective_min_edge,
                        rsi=ta.rsi_14,
                        htf_bias_value=htf_bias,
                        signal_reason=" | ".join(reason_parts),
                        window_size=_updown_tf if is_updown else "threshold",
                    )
                continue
            if lane_policy.size_multiplier > 0 and lane_policy.size_multiplier < 0.999:
                reason_parts.append(f"lane_size={lane_policy.size_multiplier:.2f}x")
            reason_parts.append(f"exp={exp_tier.value}(x{exp_multiplier:.1f})")

            if action == "BUY_YES":
                order_price = yes_price - 0.01
            else:
                order_price = (1.0 - yes_price) - 0.01
            order_price = max(0.01, min(0.99, order_price))

            reason = " | ".join(reason_parts)
            # Reconstruct est_prob from edge + yes_price for journal logging.
            # BUY_YES:  edge = est_prob - yes_price  → est_prob = edge + yes_price
            # BUY_NO: edge = yes_price - est_prob  → est_prob = yes_price - edge
            _signal_est_prob = round(
                (edge + yes_price) if action == "BUY_YES" else (yes_price - edge),
                4,
            )
            _signal_raw_est_prob = round(float(raw_est_prob), 4)

            signal = BitcoinSignal(
                market_id=market.id,
                market_question=market.question,
                action=action,
                price=order_price,
                size=size,
                confidence=confidence,
                edge=edge,
                token_id_yes=market.token_id_yes,
                token_id_no=market.token_id_no,
                end_date=market.end_date,
                direction=direction,
                btc_threshold=threshold,
                btc_current=btc_price,
                ai_used=ai_used,
                reason=reason,
                htf_bias=htf_bias,
                btc_1h_regime=btc_1h_regime if ta else None,
                window_size=_updown_tf if is_updown else "15m",
                hour_utc=datetime.now(timezone.utc).hour,
                est_prob=_signal_est_prob,
                raw_est_prob=_signal_raw_est_prob,
                rsi=round(ta.rsi_14, 1),
                side_source=side_source,
                conflict_type=(
                    direction_decision.conflict_type if direction_decision is not None else None
                ),
                resolver_path=(
                    direction_decision.resolver_path if direction_decision is not None else None
                ),
                htf_side=direction_decision.htf_side if direction_decision is not None else allowed_side,
                quant_side=direction_decision.quant_side if direction_decision is not None else None,
                momentum_side=(
                    direction_decision.momentum_side
                    if direction_decision is not None
                    else self._btc_momentum_side(mom)
                ),
                oracle_basis_bps=None,
                convergence_score=(
                    round(float(entry_convergence_score), 4)
                    if entry_convergence_score is not None
                    else None
                ),
                entry_volatility=round(
                    float((getattr(ta.trend_sabre, "atr", 0.0) or 0.0) / max(float(ta.current_price or 1.0), 1.0)),
                    6,
                ),
                entry_policy=entry_policy_meta,
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
                    "entry_volatility": round(
                        float((getattr(ta.trend_sabre, "atr", 0.0) or 0.0) / max(float(ta.current_price or 1.0), 1.0)),
                        6,
                    ),
                    "btc_4h_histogram": round(float(macd_4h.histogram or 0.0), 4),
                    "btc_4h_histogram_rising": bool(macd_4h.histogram_rising),
                    "btc_4h_crossover": str(getattr(macd_4h, "crossover", "NONE") or "NONE"),
                    "btc_4h_above_zero": bool(getattr(macd_4h, "above_zero", False)),
                    "btc_1h_histogram": round(float(ta.macd_1h.histogram or 0.0), 4),
                    "btc_1h_histogram_rising": bool(ta.macd_1h.histogram_rising),
                    "btc_1h_crossover": str(getattr(ta.macd_1h, "crossover", "NONE") or "NONE"),
                    "btc_1h_above_zero": bool(getattr(ta.macd_1h, "above_zero", False)),
                    "btc_30m_histogram": round(float(getattr(ta, "macd_30m", macd_4h).histogram or 0.0), 4),
                    "btc_30m_histogram_rising": bool(getattr(getattr(ta, "macd_30m", macd_4h), "histogram_rising", False)),
                    "btc_30m_crossover": str(getattr(getattr(ta, "macd_30m", macd_4h), "crossover", "NONE") or "NONE"),
                    "btc_30m_above_zero": bool(getattr(getattr(ta, "macd_30m", macd_4h), "above_zero", False)),
                    "btc_15m_histogram": round(float(ta.macd_15m.histogram or 0.0), 4),
                    "btc_15m_histogram_rising": bool(ta.macd_15m.histogram_rising),
                    "btc_15m_crossover": str(getattr(ta.macd_15m, "crossover", "NONE") or "NONE"),
                    "btc_15m_above_zero": bool(getattr(ta.macd_15m, "above_zero", False)),
                    # FSM ema_alignment + rsi_zone inputs (for lane_direction_fsm replay)
                    "btc_ema_9": round(float(getattr(ta, "ema_9", 0.0) or 0.0), 4),
                    "btc_ema_21": round(float(getattr(ta, "ema_21", 0.0) or 0.0), 4),
                    "btc_ema_50": round(float(getattr(ta, "ema_50", 0.0) or 0.0), 4),
                    "btc_rsi_14": round(float(getattr(ta, "rsi_14", 50.0) or 50.0), 2),
                    "sabre_trend": int(ta.trend_sabre.trend or 0),
                },
            )
            if (
                is_updown
                and dead_zone_would_block
                and not self.config.get("dead_zone_enabled", True)
                and callable(self.dead_zone_skip_callback)
            ):
                self.dead_zone_skip_callback(
                    strategy=self._signal_strategy_name,
                    market=market,
                    action=action,
                    edge=float(edge),
                    hour_utc=int(
                        dead_zone_hour
                        if dead_zone_hour is not None
                        else datetime.now(timezone.utc).hour
                    ),
                    blocked_hours=list(self.config.get("blocked_utc_hours_updown", [])),
                    bankroll=float(bankroll),
                    metadata={
                        "confidence": float(confidence),
                        "yes_price": float(yes_price),
                        "window_size": _updown_tf if is_updown else "15m",
                        "htf_bias": htf_bias,
                        "reason": reason,
                    },
                )
            signals.append(signal)
            logger.info(
                f"BTC SIGNAL: {action} '{market.question[:50]}...' "
                f"edge={edge:.3f} conf={confidence:.2f} ai={ai_used} | {reason}"
            )

        if signals:
            logger.info(f"Bitcoin strategy: {len(signals)} signals")
        elif btc_markets:
            top_reason = max(skip_reasons, key=skip_reasons.get) if skip_reasons else "no_eligible_markets"
            logger.info(
                f"Bitcoin strategy: 0 signals (HTF={htf_bias}, top_skip={top_reason}, ai_calls={ai_calls})"
            )

        top_skip_pairs = sorted(skip_reasons.items(), key=lambda kv: kv[1], reverse=True)[:6]
        gate_distributions = {k: _summarize(v) for k, v in gate_samples.items()}
        if gate_samples:
            logger.info(f"  [gate-dist] {gate_distributions}")
        self.last_scan_stats = {
            "enabled": True,
            "signals": len(signals),
            "btc_markets_considered": len(btc_markets),
            "btc_spot_usd": round(float(btc_price), 2),
            "htf_bias": htf_bias,
            "allowed_side": allowed_side,
            "ltf_strength": round(float(ltf_strength), 4),
            "ai_calls": ai_calls,
            "ai_assists": ai_assists,
            "ai_vetos": ai_vetos,
            "ai_holds": ai_holds,
            "shadow_pipeline_calls": shadow_pipeline_calls,
            "shadow_pipeline_ok": shadow_pipeline_ok,
            "preentry_veto_skips": preentry_veto_skips,
            "ai_decision_layer_skips": ai_decision_layer_skips,
            "action_counts": dict(sorted(action_counts.items())),
            "buy_no_skip_counts": dict(sorted(buy_no_skip_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]),
            "last_buy_no_skip_sample": dict(last_buy_no_skip_sample),
            "top_skip_reasons": {k: v for k, v in top_skip_pairs},
            "gate_distributions": gate_distributions,
        }

        return signals
