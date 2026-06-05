"""
SOL Macro Strategy — BTC-to-Solana Correlation Lag Trading

THESIS (Updated from live data):
═════════════════════════════════
The PRIMARY edge is macro trend alignment + LTF confirmation at a near-50/50 entry price.
BTC-SOL lag detection is a SECONDARY confirmer, not a gate.

Live data evidence:
  - lag=None trades: 63% WR (macro + LTF = sufficient signal)
  - lag=value trades: 50% WR (lag signal arrives after market partially prices in the move)
  - EP 0.47–0.49 (near-50/50): 100% WR — entering before market has formed a view
  - EP 0.44–0.46: 40% WR — fighting the market's existing lean
  - H18 UTC: 20% WR dead zone (blocked)

RULE HIERARCHY:
═══════════════

LAYER 1: MACRO TREND (1H)   [PRIMARY — entry gate]
  ► Determined by: SOL 1H EMA crossover (9 vs 21 vs 50) + RSI zone
  ► BULLISH macro → LONG only  |  BEARISH macro → SHORT only
  ► NEUTRAL macro → requires lag signal or BTC spike (no macro direction = sit out)

LAYER 2: LTF CONFIRMATION (15m)   [PRIMARY — probability driver]
  ► 15m MACD confirming the macro direction is required for updown market entries
  ► ltf_strength drives edge estimate; stronger confirmation = larger position
  ► No LTF confirmation + no spike/lag = sit out updown markets

LAYER 3: ENTRY TIMING (5m)   [SECONDARY — probability booster]
  ► 5m MACD crossover timing bonus applied to est_prob
  ► Volume confirmation: above-average volume = stronger signal

LAYER 4: BTC-SOL LAG   [SECONDARY CONFIRMER — small probability boost]
  ► Adds +0.03 to est_prob when lag aligns with direction
  ► BTC spike adds +0.02 timing boost
  ► NOT required; absence does not block entry when macro + LTF confirm

LAYER 5: EDGE CALCULATION
  ► Entry price filter: 0.46–0.49 only (near-50/50 with no strong directional lean)
  ► Combined probability vs market price = edge
  ► Exposure scaled by ExposureManager (same risk framework as BTC strategy)
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, Field

from src.market.scanner import Market, resolved_updown_window_minutes, updown_timeframe_label
from src.analysis.ai_agent import AIAgent
from src.analysis.ai_decision_broker import (
    PendingDecision as _BrokerPendingDecision,
    STATE_PENDING as _BROKER_STATE_PENDING,
)
from src.analysis.btc_price_service import BTCPriceService, TechnicalAnalysis
from src.analysis.math_utils import PositionSizer
from src.analysis.sol_btc_service import SOLBTCService, SOLTechnicalAnalysis, BTCSOLCorrelation
from src.analysis.updown_composite_score import (
    CompositeScore,
    OracleValidation,
    apply_fresh_cross_override,
    score_updown_candidate,
    validate_oracle_reference,
)
from src.analysis.lane_entry_policy import (
    entry_policy_to_dict,
    resolve_entry_policy_side,
    resolve_lane_entry_policy,
)
from src.analysis.buy_yes_lane_repair import resolve_buy_yes_lane_repair
from src.analysis.kelly_sizer import KellySizer
from src.execution.exposure_manager import ExposureManager, MarketConditions, ExposureTier
from src.strategies.strategy_config import (
    resolve_enabled_flag,
    resolve_tf_config_value,
    tf_config_override_snapshot,
)
from src.execution.performance_feedback import (
    get_drift_min_edge_mult,
    get_loosen_min_edge_mult,
)
from src.strategies.strategy_ai_context import (
    ai_recommendation_supports_action,
    format_market_metadata,
)
from src.analysis.btc_1h_regime import (
    classify_btc_1h_sma_regime,
    regime_price,
    DEFAULT_MIN_EDGE_MULT,
    DEFAULT_SIZE_MULT,
)
from src.analysis.lane_identity import build_lane_metadata
from src.analysis.rejected_candidate_log import (
    build_market_context,
    build_range_probe_variants,
    build_threshold_probe_variants,
    build_upper_cap_probe_variants,
    log_rejected_candidate,
)

logger = logging.getLogger(__name__)


@dataclass
class BiasResolution:
    allowed_side: Optional[str]
    side_source: str
    horizon_tf: str
    horizon_bias: str
    slower_biases: Dict[str, str] = field(default_factory=dict)
    primary_htf_bias: str = "NEUTRAL"
    confidence_penalty: float = 0.0
    penalty_reasons: List[str] = field(default_factory=list)


def macd_bearish_momentum_ok(m: Any) -> bool:
    """True when MACD bundle shows momentum favoring DOWN (alt leg), for BUY_NO override."""
    if m is None:
        return False
    crossover = getattr(m, "crossover", None) or ""
    if crossover == "BEARISH_CROSS":
        return True
    try:
        hist = float(getattr(m, "histogram", 0.0) or 0.0)
    except (TypeError, ValueError):
        hist = 0.0
    rising = bool(getattr(m, "histogram_rising", False))
    try:
        macd_line = float(getattr(m, "macd_line", 0.0) or 0.0)
        signal_line = float(getattr(m, "signal_line", 0.0) or 0.0)
    except (TypeError, ValueError):
        macd_line = signal_line = 0.0
    if not rising and hist < 0:
        return True
    if macd_line < signal_line and hist <= 0:
        return True
    return False


def macd_bullish_momentum_ok(m: Any) -> bool:
    """True when MACD bundle shows momentum favoring UP (alt leg), for BUY_YES override.

    Mirror of macd_bearish_momentum_ok — used for symmetric bullish-rally LTF gating.
    """
    if m is None:
        return False
    crossover = getattr(m, "crossover", None) or ""
    if crossover == "BULLISH_CROSS":
        return True
    try:
        hist = float(getattr(m, "histogram", 0.0) or 0.0)
    except (TypeError, ValueError):
        hist = 0.0
    rising = bool(getattr(m, "histogram_rising", False))
    try:
        macd_line = float(getattr(m, "macd_line", 0.0) or 0.0)
        signal_line = float(getattr(m, "signal_line", 0.0) or 0.0)
    except (TypeError, ValueError):
        macd_line = signal_line = 0.0
    if rising and hist > 0:
        return True
    if macd_line > signal_line and hist >= 0:
        return True
    return False


def side_from_bias(bias: Optional[str]) -> Optional[str]:
    """Map a directional bias label to the canonical LONG/SHORT side."""
    token = str(bias or "").strip().upper()
    if token == "BULLISH":
        return "LONG"
    if token == "BEARISH":
        return "SHORT"
    return None


def side_from_est_prob_up(est_prob_up: Optional[float]) -> Optional[str]:
    """Infer the probability-implied side from YES probability."""
    try:
        prob = float(est_prob_up)
    except (TypeError, ValueError):
        return None
    if prob > 0.5:
        return "LONG"
    if prob < 0.5:
        return "SHORT"
    return None


def side_from_momentum_bias(momentum_bias: Optional[str]) -> Optional[str]:
    """Infer side from a BULLISH/BEARISH short-window momentum label."""
    return side_from_bias(momentum_bias)


def build_alt_resolver_metadata(
    *,
    side_source: Optional[str],
    htf_side: Optional[str],
    quant_side: Optional[str],
    momentum_side: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Build BTC-compatible resolver metadata for alt macro signals."""
    htf = str(htf_side or "").strip().upper() or None
    quant = str(quant_side or "").strip().upper() or None
    momentum = str(momentum_side or "").strip().upper() or None
    source = str(side_source or "alt_macro").strip() or "alt_macro"
    conflict_bits: List[str] = []
    if htf and quant and htf != quant:
        conflict_bits.append("quant")
    if htf and momentum and htf != momentum:
        conflict_bits.append("momentum")
    if not conflict_bits:
        conflict_type = "aligned"
    else:
        conflict_type = "alt_macro_" + "_".join(conflict_bits) + "_disagree"
    tokens = [source]
    if htf:
        tokens.append(f"htf_{htf.lower()}")
    if quant:
        tokens.append(f"quant_{quant.lower()}")
    if momentum:
        tokens.append(f"momentum_{momentum.lower()}")
    return {
        "conflict_type": conflict_type,
        "resolver_path": "__".join(tokens),
        "htf_side": htf,
        "quant_side": quant,
        "momentum_side": momentum,
    }


class SolMacroSignal(BaseModel):
    """Represents a signal on a Solana price market."""
    market_id: str = Field(..., description="Market identifier")
    market_question: str = Field(..., description="The market question")
    action: str = Field(..., description="BUY_YES or BUY_NO (journal may contain legacy SELL_YES)")
    price: float = Field(..., description="Order price")
    size: float = Field(..., description="Position size in USDC")
    confidence: float = Field(..., description="Strategy confidence")
    edge: float = Field(..., description="Estimated edge")
    token_id_yes: str = Field(..., description="YES token ID")
    token_id_no: str = Field(..., description="NO token ID")
    end_date: Optional[datetime] = Field(None, description="Resolution date")
    direction: str = Field(..., description="UP or DOWN")
    sol_threshold: Optional[float] = Field(None, description="SOL price threshold")
    sol_current: Optional[float] = Field(None, description="Current SOL price")
    btc_current: Optional[float] = Field(None, description="Current BTC price")
    lag_magnitude: Optional[float] = Field(
        None, description="Signed BTC-alt catch-up gap % (opportunity mag or alt lag vs BTC)"
    )
    ai_used: bool = Field(default=False, description="Whether AI was consulted")
    # Coach features — logged to journal extra dict for pattern analysis
    htf_bias: Optional[str] = Field(None, description="HTF bias at entry: BULLISH/BEARISH/NEUTRAL")
    primary_htf_bias: Optional[str] = Field(
        None,
        description="Primary directional bias used for entry; normally alt macro for alt strategies",
    )
    alt_htf_bias: Optional[str] = Field(
        None,
        description="Alt-native HTF/1H bias at entry, kept separate from primary/fallback bias",
    )
    btc_htf_bias: Optional[str] = Field(
        None,
        description="BTC higher-timeframe context bias at entry",
    )
    btc_1h_regime: Optional[str] = Field(
        None, description="BTC 1H vs SMA(20) bucket: BULL/RANGE/BEAR when regime gates enabled"
    )
    window_size: Optional[str] = Field(None, description="Market window: 5m or 15m")
    hour_utc: Optional[int] = Field(None, description="UTC hour at entry time")
    est_prob: Optional[float] = Field(None, description="Estimated prob of YES at entry (key diagnostic)")
    raw_est_prob: Optional[float] = Field(
        None,
        description="Uncalibrated estimated prob of YES before any lane correction",
    )
    rsi: Optional[float] = Field(None, description="SOL RSI-14 at entry")
    corr_1h: Optional[float] = Field(None, description="BTC–alt 1h correlation at entry (SOL/ETH/HYPE/XRP)")
    side_source: Optional[str] = Field(None, description="Directional source used for the trade call")
    conflict_type: Optional[str] = Field(None, description="Alt resolver conflict class at entry")
    resolver_path: Optional[str] = Field(None, description="Alt resolver decision path at entry")
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
    reason: str = Field(default="", description="Why this signal was generated")
    strategy_name: str = Field(default="sol_macro", description="Journal/risk strategy key")
    alt_asset_code: str = Field(
        default="sol",
        description="Alt leg ticker (sol/eth/hype/xrp) for logs and journal spot price key",
    )

    def spot_price_journal_key(self) -> str:
        return f"{self.alt_asset_code}_price"


# Patterns to detect Solana markets
SOL_PATTERNS = [
    re.compile(r'\bsolana\b', re.IGNORECASE),
    re.compile(r'\bsol\b', re.IGNORECASE),
]
# Detect 15-minute or 5-minute "Up or Down" markets (pattern matches both)
UPDOWN_PATTERN = re.compile(r'(?:solana|sol)\s+up\s+or\s+down', re.IGNORECASE)
SOL_UPDOWN_SLUG_PREFIXES = ("sol-updown-", "sol-up-or-down-", "solana-up-or-down-")
NON_SOL_ASSET_TERMS = (
    "bitcoin",
    "btc",
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
    re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:k|K)', re.IGNORECASE),
    re.compile(r'\$\s*([\d,]+(?:\.\d+)?)', re.IGNORECASE),
    re.compile(r'([\d,]+(?:\.\d+)?)\s*(?:dollars|usd)', re.IGNORECASE),
]
UP_WORDS = {'above', 'over', 'exceed', 'reach', 'hit', 'surpass', 'higher', 'rise', 'up'}
DOWN_WORDS = {'below', 'under', 'drop', 'fall', 'crash', 'decline', 'lower', 'down'}


class SolMacroStrategy:
    """SOL Macro strategy — capitalize on BTC-to-SOL price lag."""

    def __init__(self, config: Dict[str, Any], ai_agent: AIAgent, position_sizer: PositionSizer,
                 kelly_sizer=None, exposure_manager: ExposureManager = None,
                 ai_broker=None):
        self.full_config = config
        self.config = config.get('strategies', {}).get('sol_macro', {})
        self._signal_strategy_name = "sol_macro"
        self.enabled = resolve_enabled_flag(
            "sol_macro",
            self.config,
            logger=logger,
        )
        self._log_tf_config_overrides()
        self.ai_agent = ai_agent
        # AI decision broker: when set, the strategy enqueues per-market AI
        # requests instead of awaiting the provider in-line. None → legacy
        # synchronous behavior.
        self.ai_broker = ai_broker
        self.position_sizer = position_sizer
        self.kelly_sizer = kelly_sizer or KellySizer(config)
        self.btc_service = BTCPriceService()
        self.exposure_manager = exposure_manager or ExposureManager(config)
        if self.exposure_manager:
            self.exposure_manager._on_pause_ai_callback = self._ai_kill_switch_analysis
        self.dead_zone_skip_callback = None
        self.buy_no_skip_callback = None
        self.lane_calibrator = None
        self._apply_strategy_config(rebuild_service=True)

        # AI-hold soft veto: cache market IDs where AI recently said HOLD so the
        # strong-signal path cannot bypass that decision within the TTL window.
        self._ai_hold_cache: Dict[str, float] = {}
        legacy_ai_timeout = float(self.config.get("ai_call_timeout_sec", 15.0) or 15.0)
        self.ai_decision_timeout_sec = float(
            self.config.get("ai_decision_timeout_sec", legacy_ai_timeout) or legacy_ai_timeout
        )
        observer_timeout_default = min(8.0, max(3.0, legacy_ai_timeout))
        self.ai_observer_timeout_sec = float(
            self.config.get("ai_observer_timeout_sec", observer_timeout_default)
            or observer_timeout_default
        )
        self._shadow_observer_tasks: set[asyncio.Task] = set()
        self._shadow_observer_retry_after: Dict[str, float] = {}
        self._refresh_shadow_observer_controls()
        self.ai_hold_veto_ttl_sec = self.config.get("ai_hold_veto_ttl_sec", 300)
        self.min_edge_5m_ai_override = float(
            self._tf_cfg("5m", "ai_override_min_edge", 0.10)
        )

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────

    def _build_alt_service(self) -> SOLBTCService:
        return SOLBTCService(
            dynamic_beta_min=self.dynamic_beta_min,
            dynamic_beta_max=self.dynamic_beta_max,
            dynamic_beta_extreme_max=self.dynamic_beta_extreme_max,
            btc_spike_floor_pct_5m=self.btc_spike_floor_pct_5m,
            btc_spike_floor_pct_15m=self.btc_spike_floor_pct_15m,
            lag_signal_min_pct=self.lag_signal_min_pct,
        )

    def _refresh_shadow_observer_controls(self) -> None:
        self.ai_observer_retry_cooldown_sec = float(
            self.config.get(
                "ai_observer_retry_cooldown_sec",
                max(60.0, float(self.ai_observer_timeout_sec) * 3.0),
            )
            or max(60.0, float(self.ai_observer_timeout_sec) * 3.0)
        )
        self.ai_observer_max_inflight = max(
            1,
            int(self.config.get("ai_observer_max_inflight", 1) or 1),
        )

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
            logger.info("[config] %s by_tf overrides: %s", self._signal_strategy_name.upper(), overrides)

    def _min_edge_for_window(self, window_size: str) -> float:
        return float(self._tf_cfg(window_size, "min_edge", self.min_edge))

    def _admit_marginal_quant_short(self, edge, allowed_side, timing_open) -> bool:
        """When the AI decision layer is OFF, admit sub-threshold marginal SHORTs on
        quant terms instead of letting them die on the AI tiebreaker (no-op when the
        layer is disabled) or the final lane_min_edge gate. Default OFF, SHORT-only,
        timing-open, edge above the marginal floor. Self-disables when the decision
        layer is re-enabled. Ghost-validated BEARISH×SHORT marginals (2026-06-02)."""
        try:
            return (
                bool(self.config.get("admit_marginal_on_quant_when_ai_disabled", False))
                and not self.ai_agent.decision_layer_enabled()
                and allowed_side == "SHORT"
                and bool(timing_open)
                and float(edge) >= float(self.config.get("ai_updown_marginal_min_edge", 0.03))
            )
        except Exception:
            return False

    def _ai_override_min_edge_for_window(self, window_size: str) -> float:
        return float(
            self._tf_cfg(window_size, "ai_override_min_edge", self.min_edge_5m_ai_override)
        )

    def _prune_shadow_observer_state(self) -> None:
        if self._shadow_observer_tasks:
            self._shadow_observer_tasks = {
                task for task in self._shadow_observer_tasks if not task.done()
            }
        if self._shadow_observer_retry_after:
            now = time.monotonic()
            self._shadow_observer_retry_after = {
                key: retry_after
                for key, retry_after in self._shadow_observer_retry_after.items()
                if retry_after > now
            }

    @staticmethod
    def _shadow_observer_key(*, market_id: str, reason: str) -> str:
        return f"{reason}:{market_id}"

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
          ("resolved", AIDecision)
          ("pending", None)
          ("unavailable", None)
        """
        if self.ai_broker is None:
            return "unavailable", None
        key = (self._signal_strategy_name, str(market.id), str(lane_id or ""), action)
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
        market_id = str(kwargs.get("market_id", ""))
        try:
            return await asyncio.wait_for(
                self.ai_agent.evaluate_trade_decision(**kwargs),
                timeout=self.ai_decision_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "%s: evaluate_trade_decision timeout for market %s after %.1fs",
                self._signal_strategy_name,
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
                "%s: rejected observer timeout for market %s after %.1fs",
                self._signal_strategy_name,
                market_id,
                self.ai_observer_timeout_sec,
            )
            return None

    # Windows where the AI decision gate is allowed to run. 5m is excluded on
    # purpose: AI round-trip latency is large vs a 5m entry window and would drop
    # the trade. 15m/1h windows are minutes-to-hours, so the latency is negligible.
    _DECISION_GATE_WINDOWS = frozenset({"15m", "1h"})

    def _log_decision_layer(
        self,
        *,
        market,
        window: str,
        quant_action: str,
        ai_decision,
        lane: str = "default",
        fail_open_reason: Optional[str] = None,
        entry_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append every real decision-layer verdict to decision_layer.jsonl.

        This is the record the settler scores against outcomes — the thing that
        was missing, which is why the gate was never testable. Best-effort; never
        let logging failure affect trading.
        """
        try:
            import json as _json
            from pathlib import Path as _Path

            rec = {
                "ts_utc": datetime.utcnow().isoformat() + "Z",
                "strategy": self._signal_strategy_name,
                "market_id": getattr(market, "id", None),
                "market_question": (getattr(market, "question", "") or "")[:140],
                "window": window,
                "lane": lane,
                "quant_action": quant_action,
                "approved": getattr(ai_decision, "approved", None) if ai_decision else None,
                "ai_action": getattr(ai_decision, "action", None) if ai_decision else None,
                "confidence": getattr(ai_decision, "confidence", None) if ai_decision else None,
                "estimated_probability": getattr(ai_decision, "estimated_probability", None) if ai_decision else None,
                "edge": getattr(ai_decision, "edge", None) if ai_decision else None,
                "reason": getattr(ai_decision, "reason", None) if ai_decision else None,
                "fail_open": fail_open_reason,
            }
            if entry_context:
                ctx = dict(entry_context)
                yes_price = ctx.get("yes_price")
                if yes_price is not None:
                    try:
                        yp = float(yes_price)
                        ctx.setdefault("yes_price", yp)
                        ctx.setdefault("no_price", 1.0 - yp)
                        ctx.setdefault(
                            "entry_price",
                            yp if quant_action == "BUY_YES" else 1.0 - yp,
                        )
                    except (TypeError, ValueError):
                        pass
                rec["entry_context_schema"] = 1
                rec.update(ctx)
            path = _Path("data/logs/ai_pipeline/decision_layer.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(rec) + "\n")
        except Exception:  # pragma: no cover - logging must never break trading
            pass

    def _apply_strategy_config(self, *, rebuild_service: bool = False) -> None:
        # Thresholds from config first — before any other init work — so
        # scan_and_analyze always sees instance values from YAML, not class fallbacks.
        self.min_liquidity = self.config.get("min_liquidity", 1000)
        self.min_liquidity_buy_no = self.config.get("min_liquidity_buy_no", None)
        self.min_edge = self.config.get("min_edge", 0.09)
        self.min_edge_5m = float(self._tf_cfg("5m", "min_edge", self.min_edge))
        self.min_edge_5m_ai_override = float(
            self._tf_cfg("5m", "ai_override_min_edge", 0.10)
        )
        # Non-bypassable absolute edge floor for this strategy lane.
        self.hard_min_edge = float(self.config.get("hard_min_edge", 0.0))
        # Extra floor applied only to BUY_NO entries (counter-trend / short bias).
        self.min_edge_buy_no = float(self.config.get("min_edge_buy_no", 0.0))
        # 15m updown when anti-LTF gate passed (ltf_strength==0): extra edge bar (was hardcoded 0.10)
        self.min_edge_15m_when_ltf_unconfirmed = float(
            self.config.get("min_edge_15m_when_ltf_unconfirmed", 0.10)
        )
        self.ai_confidence_threshold = self.config.get("ai_confidence_threshold", 0.60)
        self.max_ai_calls_per_scan = int(self.config.get("max_ai_calls_per_scan", 12))
        self.kelly_fraction = self.config.get("kelly_fraction", 0.15)
        self.entry_price_min = self.config.get("entry_price_min", 0.46)
        self.entry_price_max = self.config.get("entry_price_max", 0.54)
        self.min_positive_m5_adj_5m = float(self.config.get("min_positive_m5_adj_5m", 0.0))
        self.min_positive_m5_adj_5m_sell = float(
            self.config.get("min_positive_m5_adj_5m_sell", self.min_positive_m5_adj_5m)
        )
        self.sell_5m_min_corr = float(self.config.get("sell_5m_min_corr", -1.0))
        self.iql_15m_enabled = bool(self.config.get("iql_15m_enabled", False))
        self.iql_15m_hist_floor = float(self.config.get("iql_15m_hist_floor", 0.03))
        # Bias-aligned loose floor for (BEARISH, SHORT) only. Defaults to the standard
        # floor so omitting the key preserves prior behavior.
        self.iql_15m_hist_floor_aligned_short = float(
            self.config.get("iql_15m_hist_floor_aligned_short", self.iql_15m_hist_floor)
        )
        # 2026-05-30 horizon-coherence fix: 1h markets must gate on 1h MACD, not 15m
        # (per the horizon-coherent refactor). 1h histogram is a different magnitude
        # scale than 15m, so it gets its own floor. Defaults to the 15m floor — TUNE
        # this once 1h taken-trade data accrues; it may need a different scale.
        self.iql_1h_hist_floor = float(
            self.config.get("iql_1h_hist_floor", self.iql_15m_hist_floor)
        )
        # LTF policy switches.
        # Default behavior keeps the historical anti-LTF gate (skip confirmed entries).
        self.anti_ltf_gate_enabled = bool(self.config.get("anti_ltf_gate_enabled", True))
        self.require_ltf_confirmation = bool(self.config.get("require_ltf_confirmation", False))
        # 2026-05-30: opt-in (default OFF, so non-enabling alts are unchanged). When ON,
        # a BEARISH bias requires the responsive MACD vote — it will NOT short on lagging
        # EMA/RSI votes alone when MACD disagrees. Fixes eth's anti-predictive shorts:
        # eth's confident-down shorts won only 44.7% (price grinds up) because the lagging
        # votes call BEARISH on pullbacks inside an uptrend. Longs (which work) untouched.
        self.require_macd_for_bearish_bias = bool(
            self.config.get("require_macd_for_bearish_bias", False)
        )
        self.dynamic_beta_min = float(self.config.get("dynamic_beta_min", 0.8))
        self.dynamic_beta_max = float(self.config.get("dynamic_beta_max", 3.0))
        self.dynamic_beta_extreme_max = float(
            self.config.get("dynamic_beta_extreme_max", 5.0)
        )
        self.btc_spike_floor_pct_5m = float(self.config.get("btc_spike_floor_pct_5m", 0.3))
        self.btc_spike_floor_pct_15m = float(self.config.get("btc_spike_floor_pct_15m", 0.8))
        self.lag_signal_min_pct = float(self.config.get("lag_signal_min_pct", 0.2))
        self.neutral_macro_require_spike_or_lag = bool(
            self.config.get("neutral_macro_require_spike_or_lag", False)
        )
        # When True (default), block trades when alt 1H trend/histogram disagrees with
        # the chosen side (BTC-led LONG still vetoed if alt 1H is bearish). Set False
        # for BTC-lag/catch-up thesis: direction comes from BTC HTF + edges, not alt 1H sync.
        self.enforce_alt_1h_alignment = bool(
            self.config.get("enforce_alt_1h_alignment", True)
        )
        # RSI gating policy:
        # - default soft penalty (preserve trend participation)
        # - optional hard block fallback for emergency suppression
        self.rsi_hard_gate_enabled = bool(
            self.config.get("rsi_hard_gate_enabled", False)
        )
        self.rsi_soft_penalty_enabled = bool(
            self.config.get("rsi_soft_penalty_enabled", True)
        )
        self.rsi_soft_penalty_buy_yes = float(
            self.config.get("rsi_soft_penalty_buy_yes", 0.04)
        )
        self.rsi_soft_penalty_buy_no = float(
            self.config.get("rsi_soft_penalty_buy_no", 0.04)
        )
        self.low_corr_threshold_1h = float(
            self.config.get("low_corr_threshold_1h", 0.50)
        )
        self.low_corr_damping = float(self.config.get("low_corr_damping", 0.70))
        self.low_corr_suppresses_entries = bool(
            self.config.get("low_corr_suppresses_entries", False)
        )
        self.skip_on_degraded_correlation = bool(
            self.config.get("skip_on_degraded_correlation", True)
        )
        self.degraded_correlation_size_multiplier = float(
            self.config.get("degraded_correlation_size_multiplier", 0.50)
        )
        # Late-window guard for short-dated up/down markets:
        # hard-block the final minute and require a stronger edge in the last few minutes.
        self.late_window_block_mins = float(
            self.config.get("late_window_block_mins", 0.0)
        )
        self.late_window_tighten_mins = float(
            self.config.get("late_window_tighten_mins", 0.0)
        )
        self.late_window_block_mins_5m = float(
            self.config.get("late_window_block_mins_5m", 0.75)
        )
        self.late_window_tighten_mins_5m = float(
            self.config.get("late_window_tighten_mins_5m", 0.0)
        )
        self.late_window_extra_min_edge = float(
            self.config.get("late_window_extra_min_edge", 0.0)
        )
        # Temporary lane-specific size haircut while ETH/SOL are being re-tuned.
        self.tuning_size_multiplier = float(
            self.config.get("tuning_size_multiplier", 1.0)
        )
        self.calibration_size_multiplier_5m = float(
            self.config.get("calibration_size_multiplier_5m", 1.0)
        )
        self.require_oracle_for_updown = bool(
            self.config.get("require_oracle_for_updown", False)
        )
        self.oracle_max_age_sec = float(self.config.get("oracle_max_age_sec", 180.0))
        self.oracle_max_basis_bps = float(
            self.config.get("oracle_max_basis_bps", 10.0)
        )
        _basis_relax = self.config.get("oracle_basis_relax_max_bps")
        self.oracle_basis_relax_max_bps = (
            float(_basis_relax) if _basis_relax is not None else None
        )
        _relax = self.config.get("oracle_stale_basis_relax_max_bps")
        self.oracle_stale_basis_relax_max_bps = (
            float(_relax) if _relax is not None else None
        )
        self.updown_allow_exchange_when_oracle_missing = bool(
            self.config.get("updown_allow_exchange_when_oracle_missing", False)
        )
        self.updown_composite_cfg = dict(self.full_config.get("updown_composite") or {})
        self.default_min_composite_score = float(
            self.updown_composite_cfg.get("default_min_score", 0.62)
        )
        self.low_confidence_min_composite_score = float(
            self.updown_composite_cfg.get("low_confidence_min_score", 0.66)
        )
        self.degraded_bearish_est_up = float(
            self.config.get("degraded_bearish_est_up", 0.45)
        )
        # Optional centered-price hardening for markets parked at ~0.50.
        # When enabled, entries near exact 50/50 must clear a stricter edge bar
        # and can optionally require a BTC catalyst (lag/spike).
        self.center_price_band = float(self.config.get("center_price_band", 0.0))
        self.min_edge_when_centered = float(
            self.config.get("min_edge_when_centered", self.min_edge)
        )
        self.center_price_requires_catalyst = bool(
            self.config.get("center_price_requires_catalyst", False)
        )
        self.require_btc_volatility_gate = bool(
            self.config.get("require_btc_volatility_gate", False)
        )
        # When True, flat_btc_no_lag is bypassed if the alt itself has a 1h trend
        # aligned with the intended side (BUY_NO ↔ BEARISH, BUY_YES ↔ BULLISH). The
        # flat-BTC gate is a noise filter for BTC-derived signals; alt-driven setups
        # (especially BUY_NO in bear markets) shouldn't be suppressed just because BTC
        # is calm. Default True to unblock dead BUY_NO admissions. Set False to revert.
        self.flat_btc_alt_aligned_bypass = bool(
            self.config.get("flat_btc_alt_aligned_bypass", True)
        )
        self.flat_btc_only_blocks_when_alt_neutral = bool(
            self.config.get("flat_btc_only_blocks_when_alt_neutral", False)
        )
        # BTC 1H close vs SMA(20): scales min_edge bars and size for RANGE/BEAR chop / downtrends.
        self._btc_1h_regime_gates: Dict[str, Any] = dict(
            self.config.get("btc_1h_regime_gates") or {}
        )
        self.min_btc_move_pct_5m_for_lag_entries = float(
            self.config.get("min_btc_move_pct_5m_for_lag_entries", 0.15)
        )
        self.min_btc_move_pct_15m_for_lag_entries = float(
            self.config.get(
                "min_btc_move_pct_15m_for_lag_entries",
                self.min_btc_move_pct_5m_for_lag_entries,
            )
        )
        # Updown only: block LONG when journaled lag % is below floor / SHORT when above cap
        # (negative macro_leg on LONG = SOL not lagging a BTC impulse — catch-up thesis fails).
        self.block_counter_macro_leg_updown = bool(
            self.config.get("block_counter_macro_leg_updown", False)
        )
        # Paper/live calibration lane: allow a SHORT/BUY_NO lane during bullish macro
        # only when fast alt momentum is clearly bearish. This opens valid BUY_NO
        # candidates without bypassing later edge, price, oracle, liquidity, and AI gates.
        self.buy_no_ltf_override_enabled = bool(
            self.config.get("buy_no_ltf_override_enabled", False)
        )
        self.buy_no_ltf_override_rsi_max = float(
            self.config.get("buy_no_ltf_override_rsi_max", 45.0)
        )
        self.buy_no_ltf_override_max_btc_5m_pct = float(
            self.config.get("buy_no_ltf_override_max_btc_5m_pct", 0.0)
        )
        # Symmetric bullish-rally LTF gate — required for ALL LONG paths
        # (bull default + bear-exception rally). Mirrors buy_no thresholds.
        self.buy_yes_ltf_override_enabled = bool(
            self.config.get("buy_yes_ltf_override_enabled", False)
        )
        self.buy_yes_ltf_override_rsi_min = float(
            self.config.get("buy_yes_ltf_override_rsi_min", 55.0)
        )
        self.buy_yes_ltf_override_min_btc_5m_pct = float(
            self.config.get("buy_yes_ltf_override_min_btc_5m_pct", 0.0)
        )
        # Additive 4H-hist override (mirrors bitcoin.py:1233-1243 counter-trend trigger).
        # When enabled, an alt 4H MACD histogram slope alone can fire the dip/rally
        # exception path even if the 5m/15m+RSI+BTC-5m conditions don't confirm.
        # Default-off — opt-in per asset after backtest evidence.
        self.buy_no_4h_hist_override_enabled = bool(
            self.config.get("buy_no_4h_hist_override_enabled", False)
        )
        self.buy_yes_4h_hist_override_enabled = bool(
            self.config.get("buy_yes_4h_hist_override_enabled", False)
        )
        if rebuild_service or not hasattr(self, "sol_service"):
            self.sol_service = self._build_alt_service()

    def _resolve_min_liquidity_floor(self, *, window_size: str, action: str) -> float:
        window = str(window_size or "").strip().lower()
        side = "buy_no" if str(action or "").strip().upper() == "BUY_NO" else "buy_yes"
        candidates = [
            f"min_liquidity_{window}_{side}",
            f"min_liquidity_{window}",
        ]
        candidates.append(f"min_liquidity_{side}")
        candidates.append("min_liquidity")
        for key in candidates:
            raw = self.config.get(key)
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return float(self.min_liquidity)

    def _alt_asset_code(self) -> str:
        """Lowercase spot code for reason strings and journal keys (sol/eth/hype/xrp)."""
        raw = (getattr(self.sol_service, "alt_symbol", None) or "SOLUSDT").upper()
        base = raw.replace("USDT", "").replace("USD", "").strip()
        if not base:
            base = "SOL"
        return base.lower()

    def _alt_log_label(self) -> str:
        """Uppercase label for log lines (SOL, ETH, HYPE, XRP)."""
        return self._alt_asset_code().upper()

    def _build_alt_indicator_snapshot(
        self,
        alt: Any,
        *,
        correlation: Optional[Any] = None,
        composite_score: Optional[float] = None,
        convergence_score: Optional[float] = None,
        entry_volatility: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Build full-fidelity alt TA snapshot for replay/FSM validation.

        This is intentionally asset-neutral. SOL, XRP, HYPE, BNB, and DOGE all
        flow through this base path, while ETH can reuse the same contract.
        """

        def _macd(tf: str, fallback_tf: str = "15m") -> tuple[str, Any]:
            obj = getattr(alt, f"macd_{tf}", None)
            if obj is None and fallback_tf:
                obj = getattr(alt, f"macd_{fallback_tf}", None)
            return tf, obj

        def _hist(obj: Any) -> float:
            return round(float(getattr(obj, "histogram", 0.0) or 0.0), 4)

        def _rising(obj: Any) -> bool:
            return bool(getattr(obj, "histogram_rising", False))

        def _crossover(obj: Any) -> str:
            return str(getattr(obj, "crossover", "NONE") or "NONE")

        def _above_zero(obj: Any) -> bool:
            return bool(getattr(obj, "above_zero", False))

        snapshot: Dict[str, Any] = {
            "composite_score": (
                round(float(composite_score), 4) if composite_score is not None else None
            ),
            "convergence_score": (
                round(float(convergence_score), 4) if convergence_score is not None else None
            ),
            "entry_volatility": (
                round(float(entry_volatility), 6) if entry_volatility is not None else None
            ),
        }

        for tf, obj in (
            _macd("1h"),
            _macd("30m"),
            _macd("15m"),
            _macd("5m"),
        ):
            snapshot[f"alt_{tf}_histogram"] = _hist(obj)
            snapshot[f"alt_{tf}_histogram_rising"] = _rising(obj)
            snapshot[f"alt_{tf}_crossover"] = _crossover(obj)
            snapshot[f"alt_{tf}_above_zero"] = _above_zero(obj)

        snapshot.update(
            {
                "alt_ema_9": round(float(getattr(alt, "ema_9", 0.0) or 0.0), 4),
                "alt_ema_21": round(float(getattr(alt, "ema_21", 0.0) or 0.0), 4),
                "alt_ema_50": round(float(getattr(alt, "ema_50", 0.0) or 0.0), 4),
                "alt_rsi_14": round(float(getattr(alt, "rsi_14", 50.0) or 50.0), 2),
                # Raw atr/spot for replay + calibration vol-bucketing (audit only,
                # not read by any decision path). Keys mirror the ghost-log context
                # so trades.jsonl atr_bucket is computed identically to ghosts.
                "atr_14": (
                    round(float(getattr(alt, "atr_14", 0.0) or 0.0), 6)
                    if getattr(alt, "atr_14", None) is not None
                    else None
                ),
                "asset_spot": (
                    round(float(getattr(alt, "current_price", 0.0) or 0.0), 6)
                    if getattr(alt, "current_price", None) is not None
                    else None
                ),
                "btc_move_5m_pct": round(
                    float(getattr(correlation, "btc_move_5m_pct", 0.0) or 0.0), 4
                ),
                "btc_move_15m_pct": round(
                    float(getattr(correlation, "btc_move_15m_pct", 0.0) or 0.0), 4
                ),
            }
        )
        return snapshot

    def _btc_alt_corr_log_label(self) -> str:
        return f"BTC-{self._alt_log_label()} corr"

    def _bearish_dip_ltf_ok(self, ta: Any) -> tuple[bool, str]:
        """SHORT-side LTF gate — clear bearish short-window alt tape.

        Required for ALL SHORT paths: bear-regime default AND bull-regime dip exception.

        Two firing paths, OR-combined (additive):
          1. 5m/15m + RSI alt-native (gated by buy_no_ltf_override_enabled).
             2026-05-22: removed BTC-5m co-condition; per "alts decided by alt-native"
             rule, BTC must not gate admission. BTC 5m is now logged as diagnostic only.
          2. Alt 4H MACD histogram declining (gated by buy_no_4h_hist_override_enabled).
        """
        if not self.buy_no_ltf_override_enabled and not self.buy_no_4h_hist_override_enabled:
            return False, "disabled"
        sol = ta.sol
        corr = ta.correlation

        bearish_15m = bearish_5m = rsi_ok = False
        if self.buy_no_ltf_override_enabled:
            bearish_15m = macd_bearish_momentum_ok(sol.macd_15m)
            bearish_5m = macd_bearish_momentum_ok(sol.macd_5m)
            rsi_ok = float(sol.rsi_14 or 50.0) <= self.buy_no_ltf_override_rsi_max
            if bearish_15m and bearish_5m and rsi_ok:
                return True, (
                    f"bearish_ltf_override: 15m+5m bearish, RSI={sol.rsi_14:.1f} "
                    f"[diag BTC5m={corr.btc_move_5m_pct:+.3f}%]"
                )

        if self.buy_no_4h_hist_override_enabled:
            macd_4h = getattr(sol, "macd_4h", None)
            if macd_4h is not None and not macd_4h.histogram_rising:
                return True, (
                    f"4h_hist_override: alt 4H hist declining "
                    f"(curr={macd_4h.histogram:+.5f}, prev={macd_4h.prev_histogram:+.5f})"
                )

        missing = []
        if self.buy_no_ltf_override_enabled:
            if not bearish_15m:
                missing.append("15m_not_bearish")
            if not bearish_5m:
                missing.append("5m_not_bearish")
            if not rsi_ok:
                missing.append(f"rsi>{self.buy_no_ltf_override_rsi_max:.1f}")
        if self.buy_no_4h_hist_override_enabled:
            missing.append("4h_hist_not_declining")
        return False, ",".join(missing)

    def _bullish_rally_ltf_ok(self, ta: Any) -> tuple[bool, str]:
        """LONG-side LTF gate — clear bullish short-window alt tape.

        Required for ALL LONG paths: bull-regime default AND bear-regime rally exception.
        Mirrors _bearish_dip_ltf_ok with inverted thresholds.

        Two firing paths, OR-combined (additive):
          1. 5m/15m + RSI alt-native (gated by buy_yes_ltf_override_enabled).
             2026-05-22: removed BTC-5m co-condition; alt-native rule.
          2. Alt 4H MACD histogram rising (gated by buy_yes_4h_hist_override_enabled).
        """
        if not self.buy_yes_ltf_override_enabled and not self.buy_yes_4h_hist_override_enabled:
            return False, "disabled"
        sol = ta.sol
        corr = ta.correlation

        bullish_15m = bullish_5m = rsi_ok = False
        if self.buy_yes_ltf_override_enabled:
            bullish_15m = macd_bullish_momentum_ok(sol.macd_15m)
            bullish_5m = macd_bullish_momentum_ok(sol.macd_5m)
            rsi_ok = float(sol.rsi_14 or 50.0) >= self.buy_yes_ltf_override_rsi_min
            if bullish_15m and bullish_5m and rsi_ok:
                return True, (
                    f"bullish_ltf_override: 15m+5m bullish, RSI={sol.rsi_14:.1f} "
                    f"[diag BTC5m={corr.btc_move_5m_pct:+.3f}%]"
                )

        if self.buy_yes_4h_hist_override_enabled:
            macd_4h = getattr(sol, "macd_4h", None)
            if macd_4h is not None and macd_4h.histogram_rising:
                return True, (
                    f"4h_hist_override: alt 4H hist rising "
                    f"(curr={macd_4h.histogram:+.5f}, prev={macd_4h.prev_histogram:+.5f})"
                )

        missing = []
        if self.buy_yes_ltf_override_enabled:
            if not bullish_15m:
                missing.append("15m_not_bullish")
            if not bullish_5m:
                missing.append("5m_not_bullish")
            if not rsi_ok:
                missing.append(f"rsi<{self.buy_yes_ltf_override_rsi_min:.1f}")
        if self.buy_yes_4h_hist_override_enabled:
            missing.append("4h_hist_not_rising")
        return False, ",".join(missing)

    def _buy_no_ltf_override(self, ta: Any) -> tuple[bool, str]:
        """Back-compat alias — delegates to _bearish_dip_ltf_ok."""
        return self._bearish_dip_ltf_ok(ta)

    def _resolve_allowed_side_with_ltf_overrides(
        self, ta: Any, primary_htf_bias: str
    ) -> tuple[Optional[str], str, str]:
        """Additive-only side resolver — defaults always fire, exceptions are opt-in.

        Returns (side, side_source, detail). For BULLISH/BEARISH inputs, side is never None;
        defaults are byte-identical to pre-resolver behavior. The only behavior change is
        that an opposite-trend LTF momentum break can flip the side to its exception path.

        Paths:
          - BULL default LONG — always fires (no LTF gate).
          - BULL → SHORT exception — bearish_dip_ltf_ok AND NOT bullish_rally_ltf_ok.
            Clash rule: buy_no cannot fire when bullish rally also confirms (preserves rally LONG).
          - BEAR default SHORT — always fires (no LTF gate).
          - BEAR → LONG exception — bullish_rally_ltf_ok AND NOT bearish_dip_ltf_ok.
            Symmetric clash rule: BEAR-rally LONG only when bearish dip is absent.
        """
        bullish_ok, bullish_reason = self._bullish_rally_ltf_ok(ta)
        bearish_ok, bearish_reason = self._bearish_dip_ltf_ok(ta)

        if primary_htf_bias == "BULLISH":
            if bearish_ok and not bullish_ok:
                return "SHORT", "bearish_dip_exception", bearish_reason
            return "LONG", "bullish_rally_default", f"default_long: bullish={bullish_ok} bearish={bearish_ok}"

        if primary_htf_bias == "BEARISH":
            if bullish_ok and not bearish_ok:
                return "LONG", "bullish_rally_exception", bullish_reason
            return "SHORT", "bearish_dip_default", f"default_short: bullish={bullish_ok} bearish={bearish_ok}"

        return None, "skip", "neutral_htf_no_resolver"

    def _sol_signal_guard_reason(
        self,
        *,
        window_size: str,
        action: str,
        side_source: Optional[str],
        yes_price: float,
        btc_1h_regime: Optional[str],
        alt_h1_trend: Optional[str],
    ) -> Optional[str]:
        if str(self._signal_strategy_name or "") != "sol_macro":
            return None
        if action != "BUY_NO":
            return None

        source = str(side_source or "")
        regime = str(btc_1h_regime or "").upper()
        alt_h1 = str(alt_h1_trend or "").upper()

        # NOTE: neutral_fallback is now sat out at the source in
        # _resolve_alt_bias_for_tf (alt_neutral_fallback_sit_out), so it never
        # reaches this guard — both BUY_YES and BUY_NO are covered there.

        if window_size == "5m" and source.endswith("_vs_slower") and alt_h1 == "BULLISH":
            return "sol_vs_slower_short_against_h1"

        # BTC→SOL decoupling (2026-05-29): this branch gated a SOL-*native* 15m
        # short on BTC's 1h regime, violating the standing "alts not decided by
        # BTC" rule (see feedback_alts_not_decided_by_btc). It was also firing
        # unconditionally because the regime classifier was pinned to "BULL" while
        # btc_1h_regime_gates was disabled — ~877 SOL 15m shorts/day blocked vs 4
        # trades. Now opt-in only via `sol_15m_bull_regime_short_block` (default
        # OFF); these candidates are also ghost-logged so the block can be
        # validated against settled outcomes before anyone re-enables it.
        if (
            self._btc_trade_inputs_enabled()
            and
            window_size == "15m"
            and source.endswith("15m_native")
            and regime == "BULL"
            and bool(self.config.get("sol_15m_bull_regime_short_block", False))
        ):
            max_yes = float(self.config.get("sol_15m_buy_no_max_yes_price_bull_1h", 0.48))
            if yes_price >= max_yes:
                return "sol_15m_bull_regime_expensive_short"

        return None

    def _make_buy_no_skip_payload(
        self,
        *,
        market: Market,
        skip_reason: str,
        window_size: str,
        yes_price: float,
        edge: float,
        effective_min_edge: float,
        rsi: float,
        htf_bias: str,
        signal_reason: str,
        alt_1h_trend: Optional[str] = None,
        ghost_blind: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "strategy": self._signal_strategy_name,
            "market_id": market.id,
            "window_size": window_size,
            "skip_reason": skip_reason,
            "yes_price": float(yes_price),
            "edge": float(edge),
            "effective_min_edge": float(effective_min_edge),
            "rsi": float(rsi),
            "htf_bias": htf_bias,
            "signal_reason": signal_reason,
        }
        if alt_1h_trend:
            payload["alt_1h_trend"] = alt_1h_trend
        # _ghost_blind marks BUY_NO suppressions that are NOT already written to
        # the ghost log via a sibling log_rejected_candidate / _log_skip_reject
        # call on the same candidate. The central buy_no_skip_callback ghost-logs
        # only these, so they get settled against real outcomes (closing the
        # counterfactual blind spot found 2026-05-29). Default False = assume the
        # candidate is already ghosted elsewhere → never double-log.
        if ghost_blind:
            payload["_ghost_blind"] = True
        if extra:
            payload.update(extra)
        return payload

    def _emit_buy_no_skip(
        self,
        *,
        market: Market,
        bankroll: float,
        payload: Dict[str, Any],
        counts: Dict[str, int],
        last_sample: Dict[str, Any],
    ) -> None:
        reason = str(payload.get("skip_reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
        last_sample.clear()
        last_sample.update(payload)
        if callable(self.buy_no_skip_callback):
            self.buy_no_skip_callback(
                strategy=self._signal_strategy_name,
                market=market,
                bankroll=bankroll,
                payload=payload,
            )

    def _classify_btc_1h_regime(self, btc_ta: TechnicalAnalysis) -> str:
        """BULL / RANGE / BEAR from 1H close vs SMA(20).

        Always returns the *real* regime. The previous `if not enabled: return
        "BULL"` early-out (2026-05-29) silently pinned the regime to "BULL"
        whenever btc_1h_regime_gates was disabled — which poisoned lane labels
        and, worse, kept BTC-regime guards (e.g. sol_15m_bull_regime_expensive_short)
        permanently armed despite the gates being "off". The `enabled` flag still
        gates whether the min_edge/size multipliers actually fire (see call sites);
        it must NOT fabricate the regime value. classify_btc_1h_sma_regime returns
        "RANGE" on missing/zero data, so this is safe when btc_ta is sparse.
        """
        cfg = self._btc_1h_regime_gates
        band = float(cfg.get("range_band_pct", 0.0012))
        price = regime_price(btc_ta)
        sma = float(getattr(btc_ta, "sma_1h_20", 0.0) or 0.0)
        return classify_btc_1h_sma_regime(price, sma, band)

    def _regime_min_edge_mult(self, regime: str) -> float:
        cfg = self._btc_1h_regime_gates.get("min_edge_mult") or {}
        base = DEFAULT_MIN_EDGE_MULT.get(regime, 1.0)
        return float(cfg.get(regime, base))

    def _regime_size_mult(self, regime: str) -> float:
        cfg = self._btc_1h_regime_gates.get("size_mult") or {}
        base = DEFAULT_SIZE_MULT.get(regime, 1.0)
        return float(cfg.get(regime, base))

    @staticmethod
    def _signal_lag_magnitude(corr) -> Optional[float]:
        """Alt-aware lag % for journaling: opportunity mag when flagged, else signed alt lag vs BTC."""
        if corr.lag_opportunity:
            return round(float(corr.opportunity_magnitude), 4)
        if corr.btc_spike_detected and abs(float(corr.sol_lag_pct)) >= 1e-6:
            return round(float(corr.sol_lag_pct), 4)
        return None

    def _is_solana_market(self, market: Market) -> bool:
        text = (
            f"{market.question} {market.description} "
            f"{market.group_item_title} {market.slug}"
        ).lower()
        has_sol = any(p.search(text) for p in SOL_PATTERNS) or (market.slug or "").lower().startswith(SOL_UPDOWN_SLUG_PREFIXES)
        if not has_sol:
            return False
        # If other assets are present, require explicit SOL in the question/title/slug.
        if any(term in text for term in NON_SOL_ASSET_TERMS):
            primary = f"{market.question} {market.group_item_title} {market.slug}".lower()
            if not any(p.search(primary) for p in SOL_PATTERNS):
                return False
        return True

    def _is_updown_market(self, market: Market) -> bool:
        """Check if this is a Solana Up or Down market (matches both 15m and 5m)."""
        slug = (market.slug or "").lower()
        if slug.startswith(SOL_UPDOWN_SLUG_PREFIXES):
            return True
        text = f"{market.question} {market.group_item_title}"
        return bool(UPDOWN_PATTERN.search(text))

    def _is_5m_market(self, market: Market) -> bool:
        """Check if this is a 5-minute candle Up or Down market (≤5 min window)."""
        return _market_window_minutes(market) <= 5

    @staticmethod
    def _bias_to_side(bias: str) -> Optional[str]:
        if bias == "BULLISH":
            return "LONG"
        if bias == "BEARISH":
            return "SHORT"
        return None

    def _hist_conviction_threshold(self, tf: str) -> float:
        fallback = {
            "5m": 0.0,
            "15m": float(getattr(self, "iql_15m_hist_floor", 0.0) or 0.0),
            "1h": 0.0,
            "4h": 0.0,
        }.get(tf, 0.0)
        raw = self._tf_cfg(tf, "min_hist_magnitude", self.config.get(f"min_{tf}_hist_magnitude", fallback))
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            return fallback

    def _get_alt_tf_state(self, ta: SOLTechnicalAnalysis, tf: str) -> Any:
        state = getattr(ta.sol, f"tf_{tf}", None)
        if state is not None and (
            float(getattr(state, "price", 0.0) or 0.0) > 0
            or float(getattr(state, "ema_9", 0.0) or 0.0) > 0
        ):
            return state
        sol = ta.sol
        macd = getattr(sol, f"macd_{tf}", None) if tf in {"5m", "15m", "1h", "4h"} else None
        if macd is None and tf == "1h":
            macd = getattr(sol, "macd_1h", None)
        return type(
            "FallbackTFState",
            (),
            {
                "legacy_fallback": True,
                "timeframe": tf,
                "price": float(getattr(sol, "current_price", 0.0) or 0.0),
                "ema_9": float(getattr(sol, "ema_9", 0.0) or 0.0),
                "ema_21": float(getattr(sol, "ema_21", 0.0) or 0.0),
                "ema_50": float(getattr(sol, "ema_50", 0.0) or 0.0),
                "rsi_14": float(getattr(sol, "rsi_14", 50.0) or 50.0),
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

    def _resolve_voted_bias(self, *, macd: Any, price: float, ema_9: float, ema_21: float, ema_50: float, rsi: float, min_hist_magnitude: float) -> str:
        votes = [
            self._vote_macd_bias(macd, min_hist_magnitude=min_hist_magnitude),
            self._vote_rsi_bias(rsi),
            self._vote_ema_bias(price, ema_9, ema_21, ema_50),
        ]
        macd_vote = votes[0]
        bull_votes = sum(1 for vote in votes if vote == "BULLISH")
        bear_votes = sum(1 for vote in votes if vote == "BEARISH")
        if bull_votes >= 2:
            return "BULLISH"
        if bear_votes >= 2:
            # Asymmetric fix (eth): don't emit a BEARISH call carried by lagging EMA/RSI
            # when the responsive MACD vote disagrees — those shorts are anti-predictive
            # (eth's confident-down shorts won only 44.7%). Sit out instead of shorting.
            if self.require_macd_for_bearish_bias and macd_vote != "BEARISH":
                return "NEUTRAL"
            return "BEARISH"
        return "NEUTRAL"

    def _get_5m_bias(self, ta: SOLTechnicalAnalysis) -> str:
        state = self._get_alt_tf_state(ta, "5m")
        if state is None:
            return "NEUTRAL"
        return self._resolve_voted_bias(
            macd=getattr(state, "macd", None),
            price=float(getattr(state, "price", 0.0) or 0.0),
            ema_9=float(getattr(state, "ema_9", 0.0) or 0.0),
            ema_21=float(getattr(state, "ema_21", 0.0) or 0.0),
            ema_50=float(getattr(state, "ema_50", 0.0) or 0.0),
            rsi=float(getattr(state, "rsi_14", 50.0) or 50.0),
            min_hist_magnitude=self._hist_conviction_threshold("5m"),
        )

    def _get_15m_bias(self, ta: SOLTechnicalAnalysis) -> str:
        state = self._get_alt_tf_state(ta, "15m")
        if state is None:
            return "NEUTRAL"
        return self._resolve_voted_bias(
            macd=getattr(state, "macd", None),
            price=float(getattr(state, "price", 0.0) or 0.0),
            ema_9=float(getattr(state, "ema_9", 0.0) or 0.0),
            ema_21=float(getattr(state, "ema_21", 0.0) or 0.0),
            ema_50=float(getattr(state, "ema_50", 0.0) or 0.0),
            rsi=float(getattr(state, "rsi_14", 50.0) or 50.0),
            min_hist_magnitude=self._hist_conviction_threshold("15m"),
        )

    def _get_1h_bias(self, ta: SOLTechnicalAnalysis) -> str:
        state = self._get_alt_tf_state(ta, "1h")
        if state is None or bool(getattr(state, "legacy_fallback", False)):
            mtt = ta.multi_tf
            sol = ta.sol
            bull_votes = 0
            bear_votes = 0
            if mtt.h1_trend == "BULLISH":
                bull_votes += 1
            elif mtt.h1_trend == "BEARISH":
                bear_votes += 1
            ema_vote = self._vote_ema_bias(
                float(getattr(sol, "current_price", 0.0) or 0.0),
                float(getattr(sol, "ema_9", 0.0) or 0.0),
                float(getattr(sol, "ema_21", 0.0) or 0.0),
                float(getattr(sol, "ema_50", 0.0) or 0.0),
            )
            if ema_vote == "BULLISH":
                bull_votes += 1
            elif ema_vote == "BEARISH":
                bear_votes += 1
            rsi_vote = self._vote_rsi_bias(float(getattr(sol, "rsi_14", 50.0) or 50.0))
            if rsi_vote == "BULLISH":
                bull_votes += 1
            elif rsi_vote == "BEARISH":
                bear_votes += 1
            if bull_votes >= 2:
                return "BULLISH"
            if bear_votes >= 2:
                return "BEARISH"
            return "NEUTRAL"
        return self._resolve_voted_bias(
            macd=getattr(state, "macd", None),
            price=float(getattr(state, "price", 0.0) or 0.0),
            ema_9=float(getattr(state, "ema_9", 0.0) or 0.0),
            ema_21=float(getattr(state, "ema_21", 0.0) or 0.0),
            ema_50=float(getattr(state, "ema_50", 0.0) or 0.0),
            rsi=float(getattr(state, "rsi_14", 50.0) or 50.0),
            min_hist_magnitude=self._hist_conviction_threshold("1h"),
        )

    def _get_4h_bias(self, ta: SOLTechnicalAnalysis) -> str:
        """Asset's own 4h voted bias (same MACD/EMA/RSI vote as 1h).

        Used only as the 1h lane's fallback when the 1h horizon is NEUTRAL, so the
        1h lane resolves an alt-native direction instead of sitting out. 4h klines
        are already fetched by the service (tf_4h). Returns NEUTRAL if 4h is missing.
        """
        state = self._get_alt_tf_state(ta, "4h")
        if state is None:
            return "NEUTRAL"
        return self._resolve_voted_bias(
            macd=getattr(state, "macd", None),
            price=float(getattr(state, "price", 0.0) or 0.0),
            ema_9=float(getattr(state, "ema_9", 0.0) or 0.0),
            ema_21=float(getattr(state, "ema_21", 0.0) or 0.0),
            ema_50=float(getattr(state, "ema_50", 0.0) or 0.0),
            rsi=float(getattr(state, "rsi_14", 50.0) or 50.0),
            min_hist_magnitude=self._hist_conviction_threshold("4h"),
        )

    def _resolve_alt_bias_for_tf(self, ta: SOLTechnicalAnalysis, tf: str) -> BiasResolution:
        asset = self._alt_asset_code()
        if tf == "5m":
            horizon_bias = self._get_5m_bias(ta)
            # Larger-TF ladder for a neutral 5m (and disagreement check when
            # decided). Per-asset configurable: some assets' nearest-larger (15m)
            # fallback is weaker than a direct jump to 1h. Default keeps both.
            _fb_tfs = self.config.get("updown_5m_slower_tfs", ["15m", "1h"])
            slower_biases = {}
            if "15m" in _fb_tfs:
                slower_biases["15m"] = self._get_15m_bias(ta)
            if "1h" in _fb_tfs:
                slower_biases["1h"] = self._get_1h_bias(ta)
        elif tf == "15m":
            horizon_bias = self._get_15m_bias(ta)
            # Decided 15m keeps its existing 1h-only disagreement check. When 15m is
            # NEUTRAL, cascade 1h -> 4h so the lane can still resolve a direction if
            # 1h is also neutral but 4h has a clear trend (neutral-only; no penalty
            # change on confident 15m trades).
            if horizon_bias in {"BULLISH", "BEARISH"}:
                slower_biases = {"1h": self._get_1h_bias(ta)}
            else:
                slower_biases = {"1h": self._get_1h_bias(ta), "4h": self._get_4h_bias(ta)}
        else:
            horizon_bias = self._get_1h_bias(ta)
            # 1h is the top horizon for alts. When it resolves NEUTRAL, fall back to
            # the asset's OWN 4h so the 1h lane picks a direction instead of sitting
            # out. When 1h is already decided, do NOT consult 4h (no behavior change
            # / no disagreement penalty on confident 1h trades).
            if horizon_bias in {"BULLISH", "BEARISH"}:
                slower_biases = {}
            else:
                slower_biases = {"4h": self._get_4h_bias(ta)}

        primary_htf_bias = horizon_bias if horizon_bias in {"BULLISH", "BEARISH"} else "NEUTRAL"
        penalty = 0.0
        penalty_reasons: List[str] = []

        if horizon_bias in {"BULLISH", "BEARISH"}:
            allowed_side = self._bias_to_side(horizon_bias)
            side_source = f"{asset}_{tf}_native"
            for slower_tf, slower_bias in slower_biases.items():
                if slower_bias not in {"BULLISH", "BEARISH"}:
                    continue
                if slower_bias != horizon_bias:
                    penalty += 0.03
                    penalty_reasons.append(f"{slower_tf}_disagrees")
                    side_source = f"{asset}_{tf}_vs_slower"
            primary_htf_bias = horizon_bias
            return BiasResolution(
                allowed_side=allowed_side,
                side_source=side_source,
                horizon_tf=tf,
                horizon_bias=horizon_bias,
                slower_biases=slower_biases,
                primary_htf_bias=primary_htf_bias,
                confidence_penalty=penalty,
                penalty_reasons=penalty_reasons,
            )

        for slower_tf, slower_bias in slower_biases.items():
            # 2026-06-04: neutral_fallback sit-out (BOTH sides, all alt strategies).
            # The slower-TF fallback that manufactures a direction when this TF is
            # NEUTRAL settles at ~32% WR on both sides (BUY_NO n=40/32.5%/-13 PnL;
            # BUY_YES n=37/32.4%/-59 PnL, full settled history) — every sub-rung a
            # loser. Sit out instead (fall through to allowed_side=None below),
            # consistent with "alt sits out when its own TF is NEUTRAL". Reverts the
            # 2026-05-31 slower-TF fallback participation. Opt-out (default-on):
            # alt_neutral_fallback_sit_out.
            if bool(self.config.get("alt_neutral_fallback_sit_out", True)):
                break
            if slower_bias not in {"BULLISH", "BEARISH"}:
                continue
            penalty = 0.04
            penalty_reasons = [f"{tf}_neutral_fallback", f"{slower_tf}_fallback"]
            return BiasResolution(
                allowed_side=self._bias_to_side(slower_bias),
                side_source=f"{asset}_{tf}_neutral_fallback_{slower_tf}",
                horizon_tf=tf,
                horizon_bias=horizon_bias,
                slower_biases=slower_biases,
                primary_htf_bias=slower_bias,
                confidence_penalty=penalty,
                penalty_reasons=penalty_reasons,
            )

        return BiasResolution(
            allowed_side=None,
            side_source=f"{asset}_{tf}_neutral",
            horizon_tf=tf,
            horizon_bias=horizon_bias,
            slower_biases=slower_biases,
        )

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

        scan_interval_sec = float(self.config.get("entry_window_align_scan_interval_sec", 300))
        if tf == "5m":
            default_expand = 1.0
        elif tf == "1h":
            default_expand = 5.0  # hourly tolerates wider cadence drift
        else:
            default_expand = 1.5
        max_expand_min = float(self.config.get("entry_window_auto_align_max_expand_min", default_expand))
        jitter_sec = float(self.config.get("entry_window_auto_align_jitter_sec", 15))
        # At least half the scan interval (minutes), but do not cap expansion *below*
        # entry_window_auto_align_max_expand_min — min() previously made max_expand > cadence useless.
        cadence_half_min = scan_interval_sec / 120.0
        expansion_min = max(cadence_half_min, max_expand_min) + max(0.0, jitter_sec) / 60.0

        aligned_min = max(0.0, win_min - expansion_min)
        expanded_upper = win_max + expansion_min
        hard_cap = float(self.config.get("entry_window_hard_cap_mins_left", 0.0) or 0.0)
        aligned_max = min(expanded_upper, hard_cap) if hard_cap > 0 else expanded_upper
        if aligned_max <= aligned_min:
            return win_min, win_max
        return aligned_min, aligned_max

    def _default_entry_window_bounds(self, tf: str) -> tuple[float, float]:
        if tf == "5m":
            return self._resolve_entry_window_bounds(
                tf="5m",
                default_min=2.75,
                default_max=3.75,
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
            default_min=13.0,
            default_max=14.33,
        )

    def _legacy_entry_policy(
        self,
        *,
        window_size: str,
        action: str,
        direction: str,
    ) -> Dict[str, Any]:
        if window_size == "5m":
            min_edge = float(self._tf_cfg("5m", "min_edge", self.min_edge))
        elif window_size == "1h":
            min_edge = float(self._tf_cfg("1h", "min_edge", self.min_edge))
        else:
            min_edge = float(self._tf_cfg(window_size, "min_edge", self.min_edge))
        hard_min_edge = float(
            self._tf_cfg(window_size, "hard_min_edge", self.hard_min_edge)
        )
        min_edge = max(min_edge, hard_min_edge)
        min_edge_buy_no = float(
            self._tf_cfg(window_size, "min_edge_buy_no", self.min_edge_buy_no)
        )
        if action == "BUY_NO" and min_edge_buy_no > 0:
            min_edge = max(hard_min_edge, min_edge_buy_no)
        win_min, win_max = self._default_entry_window_bounds(window_size)
        size_multiplier = float(self.tuning_size_multiplier)
        if window_size == "5m" and self.calibration_size_multiplier_5m > 0:
            size_multiplier *= float(self.calibration_size_multiplier_5m)
        thesis_side = resolve_entry_policy_side(direction=direction, action=action)
        lane_mult = self._size_multiplier_for_lane(thesis_side)
        if lane_mult > 0:
            size_multiplier *= lane_mult
        entry_price_max = float(self.entry_price_max)
        if action == "BUY_YES" and window_size == "1h":
            entry_price_max = float(
                self.config.get(
                    "entry_price_max_1h_yes_side",
                    self.config.get("entry_price_max_1h_buy_yes", entry_price_max),
                )
            )
        elif action == "BUY_YES" and window_size != "5m":
            entry_price_max = float(
                self.config.get(
                    "entry_price_max_15m_yes_side",
                    self.config.get("entry_price_max_15m_buy_yes", entry_price_max),
                )
            )
        return {
            "enabled": True,
            "min_edge": float(min_edge),
            "hard_min_edge": float(hard_min_edge),
            "ai_override_min_edge": float(
                self._ai_override_min_edge_for_window(window_size)
            ),
            "entry_price_min": float(self.entry_price_min),
            "entry_price_max": float(entry_price_max),
            "entry_window_min": float(win_min),
            "entry_window_max": float(win_max),
            "size_multiplier": float(size_multiplier),
        }

    def _directional_flip_enabled(self) -> bool:
        """Opt-in flag for posterior-driven side flips (default OFF)."""
        cfg = getattr(self, "full_config", None) or {}
        lc = cfg.get("lane_calibration") or {}
        return bool((lc.get("directional_flip") or {}).get("enabled", False))

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
            legacy_policy=self._legacy_entry_policy(
                window_size=window_size,
                action=action,
                direction=direction,
            ),
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

    def _apply_late_window_guard(
        self, *, mins_left: float, effective_min_edge: float, tf: str = "15m"
    ) -> tuple[bool, float, Optional[str]]:
        """Return late-window admission decision and any tightened edge reason."""
        if tf == "5m":
            block_mins = self.late_window_block_mins_5m
            tighten_mins = self.late_window_tighten_mins_5m
        else:
            block_mins = self.late_window_block_mins
            tighten_mins = self.late_window_tighten_mins
        if block_mins > 0 and mins_left <= block_mins:
            return False, effective_min_edge, "late_window_blocked"
        if (
            tighten_mins > 0
            and self.late_window_extra_min_edge > 0
            and mins_left <= tighten_mins
        ):
            tightened_edge = max(effective_min_edge, self.late_window_extra_min_edge)
            if tightened_edge > effective_min_edge:
                return True, tightened_edge, f"late_window_edge>={tightened_edge:.3f}"
        return True, effective_min_edge, None

    def _flat_btc_gate_bypassed(
        self,
        *,
        action: str,
        alt_1h_trend: Optional[str],
    ) -> bool:
        """Return True when a native alt 1h bias should bypass the flat-BTC hard skip."""
        alt_h1 = str(alt_1h_trend or "NEUTRAL").upper()
        if self.flat_btc_only_blocks_when_alt_neutral:
            return alt_h1 in {"BULLISH", "BEARISH"}
        if not self.flat_btc_alt_aligned_bypass:
            return False
        if action == "BUY_NO" and alt_h1 == "BEARISH":
            return True
        if action == "BUY_YES" and alt_h1 == "BULLISH":
            return True
        return False

    def _resolve_rsi_gate(self, action: str, rsi: float) -> tuple[bool, float]:
        """Return (hard_block, est_prob_delta) for RSI-based suppression policy."""
        buy_ceiling = self.config.get("rsi_buy_block_above")
        sell_floor = self.config.get("rsi_sell_block_below")
        hit = (
            action == "BUY_YES"
            and buy_ceiling is not None
            and rsi >= float(buy_ceiling)
        ) or (
            action == "BUY_NO"
            and sell_floor is not None
            and rsi <= float(sell_floor)
        )
        if not hit:
            return False, 0.0

        if self.rsi_hard_gate_enabled:
            return True, 0.0
        if not self.rsi_soft_penalty_enabled:
            return False, 0.0

        if action == "BUY_YES":
            penalty = max(0.0, self.rsi_soft_penalty_buy_yes)
            return False, -penalty
        penalty = max(0.0, self.rsi_soft_penalty_buy_no)
        return False, penalty

    def _oracle_basis_blocks_entry(self, oracle_basis_bps: Optional[float]) -> bool:
        """Optional hard gate when spot diverges too far from the oracle reference."""
        max_basis_bps = self.config.get("oracle_max_basis_bps")
        if max_basis_bps is None or oracle_basis_bps is None:
            return False
        return abs(float(oracle_basis_bps)) > float(max_basis_bps)

    def _validate_updown_oracle(
        self,
        sol: Any,
        *,
        action: Optional[str] = None,
        window_size: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> OracleValidation:
        def _float_config(key: str, default: Optional[float]) -> Optional[float]:
            raw = self.config.get(key, default)
            if raw is None:
                return default
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        side = "buy_no" if str(action or "").strip().upper() == "BUY_NO" else "buy_yes"
        window = str(window_size or "").strip().lower()
        max_basis_bps = self.oracle_max_basis_bps
        basis_relax_max_bps = self.oracle_basis_relax_max_bps
        stale_basis_relax_max_bps = self.oracle_stale_basis_relax_max_bps
        if window:
            max_basis_bps = _float_config(f"oracle_max_basis_bps_{window}_{side}", max_basis_bps)
            basis_relax_max_bps = _float_config(
                f"oracle_basis_relax_max_bps_{window}_{side}",
                basis_relax_max_bps,
            )
            stale_basis_relax_max_bps = _float_config(
                f"oracle_stale_basis_relax_max_bps_{window}_{side}",
                stale_basis_relax_max_bps,
            )
        return validate_oracle_reference(
            oracle_price=getattr(sol, "chainlink_price", None),
            exchange_spot=getattr(sol, "current_price", None),
            oracle_updated_at=getattr(sol, "chainlink_updated_at", None),
            max_age_sec=self.oracle_max_age_sec,
            max_basis_bps=max_basis_bps,
            require_oracle=self.require_oracle_for_updown,
            now=now,
            allow_exchange_when_oracle_missing=self.updown_allow_exchange_when_oracle_missing,
            stale_basis_relax_max_bps=stale_basis_relax_max_bps,
            basis_relax_max_bps=basis_relax_max_bps,
        )

    def _updown_composite_floor(self, *, lane: str, quant_confidence: Optional[float] = None) -> float:
        floor = self.default_min_composite_score
        if quant_confidence is not None and float(quant_confidence) < self.ai_confidence_threshold:
            floor = max(floor, self.low_confidence_min_composite_score)
        return float(floor)

    def _requires_ai_for_lane(self, lane: str) -> bool:
        check = getattr(self.ai_agent, "decision_layer_lane_enforced", None)
        return bool(callable(check) and check(self._signal_strategy_name, lane) is True)

    def _requires_shadow_for_lane(self, lane: str) -> bool:
        check = getattr(self.ai_agent, "decision_layer_lane_requires_shadow", None)
        return bool(callable(check) and check(self._signal_strategy_name, lane) is True)

    def _size_multiplier_for_lane(self, lane: str) -> float:
        return 1.0

    def _score_updown_candidate(
        self,
        *,
        edge: float,
        effective_min_edge: float,
        confidence: float,
        ltf_strength: float,
        timeframe_alignment: float,
        oracle: OracleValidation,
        minutes_left: float,
        yes_price: float,
        lane: str,
        action: Optional[str] = None,
        btc_1h_regime: Optional[str] = None,
    ) -> CompositeScore:
        return score_updown_candidate(
            edge=edge,
            min_edge=effective_min_edge,
            quant_confidence=confidence,
            micro_momentum=ltf_strength,
            timeframe_alignment=timeframe_alignment,
            oracle=oracle,
            minutes_to_resolution=minutes_left,
            yes_price=yes_price,
            floor=self._updown_composite_floor(lane=lane, quant_confidence=confidence),
            action=action,
            btc_1h_regime=btc_1h_regime,
            regime_action_gate_enabled=bool(
                self.updown_composite_cfg.get("regime_action_gate_enabled", True)
            ),
            regime_action_min_convergence=float(
                self.updown_composite_cfg.get("regime_action_min_convergence", 0.55)
            ),
        )

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
                    remaining = question[match.end():match.end() + 2].lower()
                    if 'k' in remaining:
                        price *= 1000
                    # SOL range: $1 - $10,000 (reasonable)
                    if 1 < price < 10000:
                        return price
                except ValueError:
                    continue
        return None

    # ──────────────────────────────────────────────────────────────
    # LAYER 1: Macro Trend (1H)
    # ──────────────────────────────────────────────────────────────

    def _get_macro_trend(self, ta: SOLTechnicalAnalysis) -> str:
        """Back-compat alias for the explicit 1H bias producer."""
        return self._get_1h_bias(ta)

    def _get_btc_htf_bias_details(self, ta: TechnicalAnalysis) -> Dict[str, Any]:
        """Return BTC 4H bias plus vote-level diagnostics for entry logging."""
        sabre = ta.trend_sabre
        macd_4h = ta.macd_4h
        price = ta.current_price

        bull_votes = 0
        bear_votes = 0
        sabre_vote = "NEUTRAL"
        price_vs_ma_vote = "NEUTRAL"
        macd_vote = "NEUTRAL"
        macd_state = "neutral"

        if sabre.trend == 1:
            bull_votes += 1
            sabre_vote = "BULLISH"
        elif sabre.trend == -1:
            bear_votes += 1
            sabre_vote = "BEARISH"

        if price > sabre.ma_value:
            bull_votes += 1
            price_vs_ma_vote = "BULLISH"
        elif price < sabre.ma_value:
            bear_votes += 1
            price_vs_ma_vote = "BEARISH"

        early_bull = macd_4h.crossover == "BULLISH_CROSS" and macd_4h.histogram_rising
        early_bear = macd_4h.crossover == "BEARISH_CROSS" and not macd_4h.histogram_rising
        recovery = not macd_4h.above_zero and macd_4h.histogram > 0
        if early_bear:
            bear_votes += 1
            macd_vote = "BEARISH"
            macd_state = "early_bear"
        elif macd_4h.above_zero or early_bull or recovery:
            bull_votes += 1
            macd_vote = "BULLISH"
            if early_bull:
                macd_state = "early_bull"
            elif recovery:
                macd_state = "recovery"
            elif macd_4h.above_zero:
                macd_state = "above_zero"
        else:
            bear_votes += 1
            macd_vote = "BEARISH"
            macd_state = "below_zero"

        if bull_votes >= 2:
            raw_bias = "BULLISH"
        elif bear_votes >= 2:
            raw_bias = "BEARISH"
        else:
            raw_bias = "NEUTRAL"

        min_hist = float(self.config.get("btc_min_4h_hist_magnitude", 20.0))
        hist_ok = abs(macd_4h.histogram) >= min_hist
        final_bias = raw_bias
        if raw_bias != "NEUTRAL" and not hist_ok:
            logger.info(
                "BTC HTF: %s by vote but 4H MACD hist=%+.1f below conviction threshold (%s) "
                "— downgrading to NEUTRAL",
                raw_bias,
                macd_4h.histogram,
                min_hist,
            )
            final_bias = "NEUTRAL"

        return {
            "bias": final_bias,
            "raw_bias": raw_bias,
            "bull_votes": bull_votes,
            "bear_votes": bear_votes,
            "sabre_vote": sabre_vote,
            "price_vs_ma_vote": price_vs_ma_vote,
            "macd_vote": macd_vote,
            "macd_state": macd_state,
            "btc_price": float(price or 0.0),
            "sabre_ma": float(getattr(sabre, "ma_value", 0.0) or 0.0),
            "macd_4h_histogram": float(getattr(macd_4h, "histogram", 0.0) or 0.0),
            "macd_4h_histogram_rising": bool(getattr(macd_4h, "histogram_rising", False)),
            "macd_4h_above_zero": bool(getattr(macd_4h, "above_zero", False)),
            "macd_4h_crossover": str(getattr(macd_4h, "crossover", "") or ""),
            "min_hist": min_hist,
            "hist_conviction_ok": hist_ok,
        }

    def _get_btc_htf_bias(self, ta: TechnicalAnalysis) -> str:
        """Use BTC 4H structure as the primary macro gate for alt strategies."""
        return str(self._get_btc_htf_bias_details(ta)["bias"])

    def _apply_primary_htf_bias(
        self, est_prob_up: float, primary_htf_bias: str, weight: float
    ) -> float:
        """Apply the same HTF bias that determined the allowed side.

        Once BTC 4H became the primary gate for alt strategies, probability estimation
        needs to use that same resolved bias. Otherwise the action can be chosen from
        BTC HTF while the probability model still leans the other way from alt-only HTF.
        """
        if primary_htf_bias == "BULLISH":
            return est_prob_up + weight
        if primary_htf_bias == "BEARISH":
            return est_prob_up - weight
        return est_prob_up

    def _apply_degraded_corr_bias(
        self, est_prob_up: float, primary_htf_bias: str, corr: BTCSOLCorrelation
    ) -> float:
        """When correlation is degraded, avoid defaulting bearish setups to near-coinflip."""
        if not self._btc_trade_inputs_enabled():
            return est_prob_up
        if not getattr(corr, "degraded", False):
            return est_prob_up
        if primary_htf_bias == "BEARISH":
            return min(est_prob_up, self.degraded_bearish_est_up)
        return est_prob_up

    def _hourly_buy_yes_native_bonus(
        self,
        *,
        window_size: str,
        allowed_side: str,
        resolution: BiasResolution,
        ltf_strength: float,
    ) -> float:
        """Small uplift for clean native 1H bullish lanes with real LTF confirmation."""
        if window_size != "1h" or allowed_side != "LONG":
            return 0.0
        if resolution.horizon_tf != "1h" or resolution.horizon_bias != "BULLISH":
            return 0.0
        if not str(resolution.side_source or "").endswith("_1h_native"):
            return 0.0
        min_ltf_strength = float(
            self.config.get("hourly_buy_yes_native_bonus_min_ltf_strength_1h", 0.30)
        )
        if float(ltf_strength) < min_ltf_strength:
            return 0.0
        return float(self.config.get("hourly_buy_yes_native_bonus_1h", 0.0))

    def _alt_buy_yes_bullish_floor_bump(
        self,
        *,
        window_size: str,
        action: str,
        htf_bias: str,
    ) -> float:
        """Post-calibration BUY_YES floor bump — mirror of the BTC hook.

        The alt model under-shoots UP probability under bullish bias, so in-window
        BUY_YES is rejected as negative edge while the ghost log settles those same
        candidates at 68–76% WR / +EV (2026-05-28). Applied in calibrated/edge space
        (unlike `_hourly_buy_yes_native_bonus`, which adds pre-calibration), so the
        config'd amount maps ~1:1 onto edge. Asymmetric — BUY_NO untouched. Per-asset
        per-window via `{strategy}.<tf>_buy_yes_bullish_floor_bump`; unset => 0.0 (off).
        SOL and DOGE/XRP 15m are intentionally left unset (ghost −EV).
        """
        if action != "BUY_YES" or htf_bias != "BULLISH":
            return 0.0
        if window_size not in ("5m", "15m", "1h"):
            return 0.0
        return float(
            self.config.get(f"{window_size}_buy_yes_bullish_floor_bump", 0.0)
        )

    def _strong_enough_5m_signal(self, m5_adj: float, action: str) -> bool:
        """Optional guard for weak 5m-only entries.

        Some assets perform poorly when the 5m path is allowed to enter on the
        weakest MACD state (`macd_line > signal_line`, worth only +0.02). When
        configured, require at least the configured positive 5m adjustment.
        """
        threshold = (
            self.min_positive_m5_adj_5m_sell
            if action == "BUY_NO"
            else self.min_positive_m5_adj_5m
        )
        if threshold <= 0:
            return True
        return m5_adj >= threshold

    def _effective_iql_15m_floor(
        self, allowed_side: str, htf_bias: Optional[str]
    ) -> float:
        """Select the IQL floor based on (htf_bias, side).

        Only BEARISH×SHORT uses the loose floor — ghost data shows the standard
        floor inverts signal there. All other combos use the default tight floor.
        """
        bias = (htf_bias or "").upper()
        if bias == "BEARISH" and allowed_side == "SHORT":
            return self.iql_15m_hist_floor_aligned_short
        return self.iql_15m_hist_floor

    def _passes_iql(
        self,
        ta: SOLTechnicalAnalysis,
        allowed_side: str,
        htf_bias: Optional[str] = None,
        tf: str = "15m",
    ) -> bool:
        """Indicator Quality Layer (IQL), horizon-coherent.

        A market gates on its OWN timeframe's MACD: 1h markets use macd_1h, 15m use
        macd_15m (per the horizon-coherent refactor). Reuses
        `_check_macd_confirmation` so cycle-level LTF strength and IQL agree on the
        same scoring: if the timeframe is already "confirmed" (late, strong
        structure), IQL passes; otherwise apply the relaxed cross / hist-floor rule.
        """
        if not self.iql_15m_enabled:
            return True
        if tf == "1h":
            return True
        is_hourly = tf == "1h"
        macd = ta.sol.macd_1h if is_hourly else ta.sol.macd_15m
        label = "1h" if is_hourly else "15m"
        confirmed, _, _ = self._check_macd_confirmation(macd, allowed_side, label=label)
        if confirmed:
            return True
        hist = float(macd.histogram)
        floor = (
            self.iql_1h_hist_floor
            if is_hourly
            else self._effective_iql_15m_floor(allowed_side, htf_bias)
        )
        if allowed_side == "LONG":
            return (
                macd.crossover == "BULLISH_CROSS"
                or (hist >= floor and macd.histogram_rising)
            )
        return (
            macd.crossover == "BEARISH_CROSS"
            or (hist <= -floor and not macd.histogram_rising)
        )

    def _calibrate_est_prob(
        self,
        raw_est_prob: float,
        *,
        action: str,
        direction: str,
        window_size: str,
        side_source: str,
        signal_reason: str,
        htf_bias: Optional[str],
        primary_htf_bias: Optional[str] = None,
        alt_htf_bias: Optional[str] = None,
        btc_1h_regime: Optional[str] = None,
    ) -> float:
        self._last_calibration_vetoed = False
        self._last_calibration_lane_id = ""
        cal = getattr(self, "lane_calibrator", None)
        if cal is None:
            return raw_est_prob
        lane_meta = build_lane_metadata(
            strategy=self._signal_strategy_name,
            window_size=window_size,
            action=action,
            direction=direction,
            entry_leg=("NO" if action == "BUY_NO" else "YES"),
            side_source=side_source,
            ai_used=False,
            reason=signal_reason,
            signal_reason=signal_reason,
            primary_htf_bias=primary_htf_bias or htf_bias,
            alt_htf_bias=alt_htf_bias or htf_bias,
            btc_1h_regime=btc_1h_regime,
        )
        lane_id = str(lane_meta.get("lane_id") or "").strip()
        if not lane_id:
            return raw_est_prob
        self._last_calibration_lane_id = lane_id
        self._last_calibration_vetoed = bool(cal.is_vetoed(lane_id))
        return float(cal.calibrate(lane_id, raw_est_prob))

    def _low_corr_blocks_entry(self, corr: BTCSOLCorrelation) -> bool:
        """Optional hard gate for assets whose BTC-lag thesis breaks when decoupled."""
        return (
            self._btc_trade_inputs_enabled()
            and
            self.low_corr_suppresses_entries
            and corr.correlation_1h < self.low_corr_threshold_1h
        )

    def _btc_trade_inputs_enabled(self) -> bool:
        """Whether BTC-derived context is allowed to change trade admission/edge/size."""
        return False

    # ──────────────────────────────────────────────────────────────
    # LAYER 2: 15m Trend Confirmation
    # ──────────────────────────────────────────────────────────────

    def _check_15m_confirmation(self, ta: SOLTechnicalAnalysis, allowed_side: str) -> tuple:
        """Check if 15m MACD confirms the allowed direction (back-compat wrapper)."""
        return self._check_macd_confirmation(ta.sol.macd_15m, allowed_side, label="15m")

    def _check_macd_confirmation(self, macd: Any, allowed_side: str, label: str = "15m") -> tuple:
        """Check if the given-timeframe MACD confirms the allowed direction.

        Horizon-agnostic: callers pass the MACD for the candidate's own timeframe
        (1h markets pass macd_1h, 15m markets pass macd_15m). Returns
        (confirmed: bool, strength: float, reasons: list).
        """
        reasons = []
        strength = 0.0

        if allowed_side == "LONG":
            if macd.crossover == "BULLISH_CROSS":
                strength += 0.40
                reasons.append(f"{label} MACD bull cross")
            if macd.histogram_rising:
                if macd.prev_histogram < 0 and macd.histogram > 0:
                    strength += 0.35
                    reasons.append(f"{label} hist red-to-green")
                elif macd.histogram > macd.prev_histogram:
                    strength += 0.15
                    reasons.append(f"{label} hist rising")
            if macd.macd_line > macd.signal_line:
                strength += 0.10
                reasons.append(f"{label} MACD above signal")
        else:  # SHORT
            if macd.crossover == "BEARISH_CROSS":
                strength += 0.40
                reasons.append(f"{label} MACD bear cross")
            if not macd.histogram_rising:
                if macd.prev_histogram > 0 and macd.histogram < 0:
                    strength += 0.35
                    reasons.append(f"{label} hist green-to-red")
                elif macd.histogram < macd.prev_histogram:
                    strength += 0.15
                    reasons.append(f"{label} hist falling")
            if macd.macd_line < macd.signal_line:
                strength += 0.10
                reasons.append(f"{label} MACD below signal")

        # Keep at 0.50: cached SOL 15m Jan20-Apr20 comparison beat 0.35
        # on WR and net PnL, and this gate treats confirmed LTF as late-entry risk.
        confirmed = strength >= 0.50
        return confirmed, strength, reasons

    # ──────────────────────────────────────────────────────────────
    # LAYER 3: 5m Entry Timing + Lag Detection
    # ──────────────────────────────────────────────────────────────

    def _check_entry_timing(self, ta: SOLTechnicalAnalysis, allowed_side: str) -> tuple:
        """Check 5m MACD for entry timing ONLY.

        Returns: (bonus: float, reasons: list)

        NOTE: BTC-SOL lag bonus was REMOVED from this function (2026-04-07).
        Previously lag was applied here AND again in the 15m/5m scan loops,
        causing double-counting. Live data shows lag is a weak signal (50% WR
        vs 63% WR for lag=None trades). The scan loops now handle lag exclusively.
        Correlation strength is also moved to scan loops for consistency.
        """
        macd_5m = ta.sol.macd_5m
        corr = ta.correlation
        reasons = []
        bonus = 0.0

        # 5m MACD entry trigger — intentionally modest weights for 15m market context.
        # 5m is a timing nudge only; 15m MACD confirmation (Layer 2) carries primary weight.
        # Reduced from 0.05/0.03 to 0.02/0.02 to prevent 5m noise overriding absent 15m signal.
        if allowed_side == "LONG":
            if macd_5m.crossover == "BULLISH_CROSS":
                bonus += 0.02
                reasons.append("5m MACD bull cross")
            if macd_5m.histogram_rising and macd_5m.histogram > 0:
                bonus += 0.02
                reasons.append("5m hist green+rising")
        else:
            if macd_5m.crossover == "BEARISH_CROSS":
                bonus += 0.02
                reasons.append("5m MACD bear cross")
            if not macd_5m.histogram_rising and macd_5m.histogram < 0:
                bonus += 0.02
                reasons.append("5m hist red+falling")

        # Correlation context logged (no probability adjustment here —
        # scan loops handle corr damping per-market to avoid double-counting)
        if corr.correlation_1h > 0.85:
            reasons.append(f"high corr ({corr.correlation_1h:.2f})")
        elif corr.correlation_1h < 0.5:
            reasons.append(f"low corr ({corr.correlation_1h:.2f})")

        return bonus, reasons

    async def _ai_kill_switch_analysis(self, reason: str, loss_count: int) -> None:
        if not self.ai_agent or not self.ai_agent.is_available():
            return
        try:
            context = (
                f"Lane: {self._signal_strategy_name.upper()}\n"
                f"Kill switch triggered: {reason}\n"
                f"Consecutive losses: {loss_count}\n"
                f"This is a diagnostic call to understand why the lane is struggling."
            )
            result = await self.ai_agent.analyze_market(
                market_question=f"Why is {self._signal_strategy_name} losing? {reason}",
                market_description=context,
                current_yes_price=0.5,
                market_id=f"kill_switch_{self._signal_strategy_name}",
            )
            if result:
                logger = logging.getLogger(__name__)
                logger.warning(
                    f"OPS_JSON kill_switch_ai lane={self._signal_strategy_name} "
                    f"reasoning={result.reasoning!r} confidence={result.confidence_score:.2f}"
                )
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────
    # LAYER 4: Edge Estimation
    # ──────────────────────────────────────────────────────────────

    def _estimate_probability(
        self, sol_price: float, threshold: float, direction: str,
        ta: SOLTechnicalAnalysis, days_to_resolution: int,
        ltf_strength: float, timing_bonus: float,
    ) -> float:
        """Estimate probability of SOL being above/below threshold at resolution."""
        # Base: distance from threshold
        if direction == "UP":
            distance_pct = (sol_price - threshold) / threshold
        else:
            distance_pct = (threshold - sol_price) / threshold

        # Logistic-ish base from distance
        base_prob = 0.50 + min(0.35, max(-0.35, distance_pct * 3.0))

        # LTF confirmation strength
        ltf_adj = ltf_strength * 0.12 if ltf_strength > 0 else -0.05

        # Timing bonus from Layer 3
        timing_adj = timing_bonus

        # RSI adjustment
        rsi = ta.sol.rsi_14
        rsi_adj = 0.0
        if direction == "UP":
            if rsi > 75:   rsi_adj = -0.06   # Overbought — strongly against UP
            elif rsi > 65: rsi_adj = -0.02   # Elevated — mild headwind for UP
            elif rsi < 30: rsi_adj =  0.04   # Oversold bounce
            # Removed: 50<rsi<65 = +0.02 bonus. Live data: 14.3% WR -$14.68 in that bucket (worst of all)
        else:
            if rsi < 25:   rsi_adj = -0.06   # Oversold — strongly against DOWN
            elif rsi < 35: rsi_adj = -0.02   # Low RSI — mild headwind for DOWN
            elif rsi > 70: rsi_adj =  0.04   # Overbought crash potential
            # Removed: mirror of removed UP bonus

        # BTC-alt lag — REMOVED from alt est_prob calculation 2026-05-22.
        # Per "alts decided by alt-native indicators" rule, BTC lag must not
        # adjust alt edge. The prior nudge was data-supported as harmful anyway
        # (live: lag=None = 63% WR, lag=value = 50% WR — lag arrives after the
        # market has already priced in the move). Kept as zero for downstream
        # arithmetic compatibility.
        lag_adj = 0.0

        # ATR-based volatility context
        vol_adj = 0.0
        atr_pct = ta.sol.atr_14 / sol_price if sol_price > 0 else 0
        if atr_pct > 0.03:  # High vol SOL
            vol_adj = 0.02 if direction == "UP" else 0.02  # More room to move
        elif atr_pct < 0.01:
            vol_adj = -0.03  # Low vol, harder to reach threshold

        # Time decay
        if days_to_resolution > 0:
            time_factor = min(1.0, days_to_resolution / 60.0)
            base_prob = base_prob * (1 - time_factor * 0.3) + 0.50 * (time_factor * 0.3)

        final = base_prob + ltf_adj + timing_adj + rsi_adj + lag_adj + vol_adj
        return max(0.05, min(0.95, final))

    # ──────────────────────────────────────────────────────────────
    # Exposure conditions from SOL TA
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def conditions_from_ta(ta: SOLTechnicalAnalysis) -> MarketConditions:
        """Build MarketConditions from SOL technical analysis."""
        sol = ta.sol
        sol_price = sol.current_price
        atr_pct = sol.atr_14 / sol_price if sol_price > 0 else 0.01

        # Volume ratio approximation from correlation data
        volume_ratio = 1.0
        if ta.correlation.correlation_1h > 0.8:
            volume_ratio = 1.2  # High correlation = active market
        elif ta.correlation.correlation_1h < 0.4:
            volume_ratio = 0.7

        # Derive alignment score: 1.0 if all TFs agree, else fraction
        alignment_score = 1.0 if ta.multi_tf.aligned else sol.trend_strength
        # MACD-EMA divergence: EMAs stacked but momentum (15m MACD) still negative = false signal.
        # Price EMAs align before momentum confirms — reduce score to avoid inflated exposure sizing.
        if (ta.multi_tf.aligned and sol.macd_15m.histogram < 0
                and sol.macd_15m.crossover != "BULLISH_CROSS"):
            alignment_score = min(alignment_score, 0.6)

        return MarketConditions(
            volatility=atr_pct,
            volume_ratio=volume_ratio,
            trend_strength=alignment_score,
            trend_direction=ta.multi_tf.h1_trend,
            weekend_penalty=_get_weekend_penalty(),
        )


    async def scan_and_analyze(self, markets: List[Market], bankroll: float) -> List[SolMacroSignal]:
        """Scan SOL markets with BTC-lag detection."""
        if not self.enabled:
            return []

        _alt_label = self._alt_log_label()
        _spot_key = self._alt_asset_code()
        _brand = f"{_alt_label} Macro"
        _corr_lbl = self._btc_alt_corr_log_label()

        # Filter to updown markets ONLY — long-dated SOL threshold markets
        # ("Will SOL hit $200?") are noise for the 15m/5m macro strategy.
        sol_markets = [m for m in markets if self._is_solana_market(m) and self._is_updown_market(m)]
        if not sol_markets:
            logger.info(
                f"{_brand} strategy: 0 {_alt_label} updown markets found out of {len(markets)} total markets"
            )
            return []

        logger.info(f"{_brand} strategy: Found {len(sol_markets)} {_alt_label} markets")

        # Fetch full technical analysis ONCE per cycle
        ta = self.sol_service.get_full_analysis()
        if not ta:
            logger.warning(f"{_brand} strategy: Could not fetch {_alt_label}/BTC price data")
            return []

        btc_ta = self.btc_service.get_full_analysis()
        btc_1h_regime = "BULL"
        if btc_ta:
            btc_htf_details = self._get_btc_htf_bias_details(btc_ta)
            btc_htf_bias = str(btc_htf_details["bias"])
            logger.info(
                "BTC HTF: %s raw=%s votes(sabre=%s price_vs_ma=%s macd=%s:%s) "
                "hist=%+.1f rising=%s spot=%.0f ma=%.0f",
                btc_htf_bias,
                btc_htf_details["raw_bias"],
                btc_htf_details["sabre_vote"],
                btc_htf_details["price_vs_ma_vote"],
                btc_htf_details["macd_vote"],
                btc_htf_details["macd_state"],
                btc_htf_details["macd_4h_histogram"],
                btc_htf_details["macd_4h_histogram_rising"],
                btc_htf_details["btc_price"],
                btc_htf_details["sabre_ma"],
            )
            # 2026-05-16 calibration: always classify the BTC 1H regime so signal
            # diagnostics (lane_id regime token, dampeners) reflect reality. The
            # `enabled` flag continues to gate whether the min_edge/size multipliers
            # actually fire; the regime *value* is now always real, not a fake "BULL"
            # default. Was: btc_1h_regime fell through as the string "BULL" when
            # gates were disabled (e.g. HYPE post-2026-05-13), which silently
            # poisoned lane labels and dampener decisions.
            btc_1h_regime = self._classify_btc_1h_regime(btc_ta)
            if self._btc_1h_regime_gates.get("enabled", False):
                pass
            else:
                pass
        else:
            btc_htf_bias = None
            logger.warning("External HTF context unavailable — continuing with alt-only analysis")

        sol_price = ta.sol.current_price
        sol = ta.sol
        corr = ta.correlation
        mtt = ta.multi_tf

        # ═══════════════════════════════════════════════
        # LAYER 0: Exposure check
        # ═══════════════════════════════════════════════
        conditions = self.conditions_from_ta(ta)
        exp_tier, exp_multiplier, exp_max_size, exp_reason = self.exposure_manager.get_exposure(conditions)

        if exp_tier == ExposureTier.PAUSED:
            logger.info(f"{_brand} strategy: PAUSED — {exp_reason}")
            return []

        macro_trend = self._get_1h_bias(ta)
        bias_15m = self._get_15m_bias(ta)
        bias_5m = self._get_5m_bias(ta)

        logger.info(
            f"{_alt_label} ${sol_price:,.2f} | bias_1h={macro_trend} bias_15m={bias_15m} bias_5m={bias_5m} | "
            f"1H={mtt.h1_trend} 15m={mtt.m15_trend} 5m={mtt.m5_trend} | "
            f"15m MACD hist={sol.macd_15m.histogram:+.3f} {sol.macd_15m.crossover} | "
            f"RSI={sol.rsi_14:.0f}"
        )
        if getattr(corr, "degraded", False):
            logger.warning(
                f"{_brand}: correlation degraded "
                f"({', '.join(getattr(corr, 'degraded_reasons', [])) or 'unknown'})"
            )
        if ta.multi_tf.aligned:
            logger.info(
                f"{_brand}: MTF fully aligned — entry price filter will gate quality "
                f"(lag is secondary; macro+LTF is primary signal)"
            )
        if corr.lag_opportunity:
            logger.info(
                f"{_brand}: Lag confirmer active — {corr.opportunity_direction} "
                f"mag={corr.opportunity_magnitude:+.2f}% (secondary boost applied)"
            )
        elif corr.btc_spike_detected:
            logger.info(
                f"{_brand}: BTC spike ({corr.btc_move_5m_pct:+.2f}%) — timing boost applied"
            )

        # ═══════════════════════════════════════════════
        # LAYER 4: Evaluate each market
        # ═══════════════════════════════════════════════
        signals = []
        ai_calls = 0
        shadow_pipeline_calls = 0
        shadow_pipeline_ok = 0
        shadow_observer_calls = 0
        shadow_observer_ok = 0
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
                    asset_spot=getattr(sol, "current_price", None),
                    btc_spot=getattr(corr, "btc_price", None),
                    rsi_14=getattr(sol, "rsi_14", None),
                    atr_14=getattr(sol, "atr_14", None),
                )
            )
            # Always stamp eval_mins_left so post-hoc analysis can distinguish
            # in-window (eml <= entry_window_max) from pre-window noise on EVERY
            # rejection reason, not just lane_entry_window. Closes the ghost-log
            # blind spot found 2026-05-22 where alt 1h liquidity/momentum/oracle
            # rejections dropped eml and looked indistinguishable from too-early.
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
            if btc_1h_regime is not None:
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
                btc_1h_regime=btc_1h_regime,
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

        for market in sol_markets:
            is_updown = self._is_updown_market(market)
            _updown_tf = (
                updown_timeframe_label(resolved_updown_window_minutes(market))
                if is_updown
                else "15m"
            )
            # Threshold-market-only locals. Up/down markets never assign these, but the
            # shared AI-tiebreaker context path below reads them. Bind safe defaults so
            # up/down candidates can't raise UnboundLocalError on the AI path (regression
            # surfaced when MiniMax came back online and the AI tiebreaker became
            # reachable for marginal-edge up/down candidates).
            threshold: Optional[float] = None
            distance_pct: float = 0.0
            days_to_resolution: int = 30
            resolution = self._resolve_alt_bias_for_tf(ta, _updown_tf)
            allowed_side = resolution.allowed_side
            side_source = resolution.side_source
            primary_htf_bias = resolution.primary_htf_bias
            if allowed_side is None:
                _bump_skip("neutral_bias")
                logger.info(
                    "%s skip '%s' — no usable %s bias (1h=%s 15m=%s 5m=%s)",
                    _brand,
                    market.question[:40],
                    _updown_tf,
                    macro_trend,
                    bias_15m,
                    bias_5m,
                )
                continue
            yes_price = market.yes_price
            action = "BUY_YES" if allowed_side == "LONG" else "BUY_NO"
            direction = "UP" if allowed_side == "LONG" else "DOWN"
            # 2026-05-31: 5m-native BUY_NO is anti-predictive — held-to-resolution WR
            # ~22% across eth/xrp/doge/sol vs 50-65% on 15m-native; MACD-confirmed 5m
            # shorts lose, so the signal (not the gate) is inverted. Opt-in sit-out,
            # ghost-logged via _log_skip_reject so the counterfactual keeps settling.
            # Longs (BUY_YES) are ~50% and unaffected.
            if (
                is_updown
                and _updown_tf == "5m"
                and action == "BUY_NO"
                and bool(self.config.get("disable_buy_no_5m_native", False))
            ):
                _bump_skip("buy_no_5m_native_suppressed")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=allowed_side,
                    action=action,
                    reason="buy_no_5m_native_suppressed",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={"side_source": side_source},
                )
                continue
            # 2026-05-30 horizon-coherence: a 1h market confirms on 1h MACD, not 15m
            # (per the refactor). 5m/15m keep macd_15m (higher-tf confirm is intentional
            # there); only the incoherent 15m-on-1h case changes.
            _ltf_macd = ta.sol.macd_1h if _updown_tf == "1h" else ta.sol.macd_15m
            _ltf_label = "1h" if _updown_tf == "1h" else "15m"
            ltf_confirmed, ltf_strength, ltf_reasons = self._check_macd_confirmation(
                _ltf_macd, allowed_side, label=_ltf_label
            )
            _sample("ltf_strength", ltf_strength)
            skip_15m_reason = None
            if self.require_ltf_confirmation:
                if not ltf_confirmed:
                    skip_15m_reason = "ltf_required_unconfirmed_15m"
                else:
                    logger.info(
                        "  LTF confirmation required and passed: %s, strength=%.2f",
                        allowed_side,
                        ltf_strength,
                    )
            elif self.anti_ltf_gate_enabled and ltf_confirmed:
                skip_15m_reason = "anti_ltf_confirmed_15m"

            timing_bonus, timing_reasons = self._check_entry_timing(ta, allowed_side)

            # Alt-native momentum guards. 2026-05-23 ghost-counterfactual review:
            # default-on guards were over-pruning (blocking trades that would have
            # won 47-52% aggregate; 78-91% on alt 1h LONG cells). Now an explicit
            # per-(side, window) allowlist via `alt_momentum_confirm:
            # {buy_yes: [...], buy_no: [...]}`. Empty/missing = guard off for that
            # side. Uses the alt's own MACD only — BTC is not consulted.
            _alt_mc_cfg = self.config.get("alt_momentum_confirm") or {}
            _alt_mc_window = _updown_tf if is_updown else "15m"
            # Bias-aligned bypass: when the trade aligns with primary_htf_bias the
            # gate is provably inverting (ghost data 5/22→5/27: BEARISH×SHORT blocked
            # WR > traded WR by +11 to +16pp across sol/hype/xrp/bnb; LONG side
            # parallel — ETH BULLISH×LONG was neutral, alt LONG-blocked WRs 48-55%).
            # Keep gate active for counter-trend trades, where momentum confirm has
            # genuine signal value.
            _bias_aligned_short = (
                action == "BUY_NO"
                and (primary_htf_bias or "").upper() == "BEARISH"
            )
            _bias_aligned_long = (
                action == "BUY_YES"
                and (primary_htf_bias or "").upper() == "BULLISH"
            )
            if (
                action == "BUY_NO"
                and not _bias_aligned_short
                and _alt_mc_window in (_alt_mc_cfg.get("buy_no") or [])
            ):
                # Horizon-coherent: confirm on the lane's OWN timeframe, with the
                # next-LARGER timeframe as fallback. Never a smaller TF on a larger lane.
                if _alt_mc_window == "1h":
                    _own, _larger = sol.macd_1h, sol.macd_1h
                elif _alt_mc_window == "15m":
                    _own, _larger = sol.macd_15m, sol.macd_1h
                else:  # 5m
                    _own, _larger = sol.macd_5m, sol.macd_15m
                _alt_bear_confirmed = (
                    _own.crossover == "BEARISH_CROSS"
                    or (_own.histogram < 0 and not _own.histogram_rising)
                    or _larger.crossover == "BEARISH_CROSS"
                    or (_larger.histogram < 0 and not _larger.histogram_rising)
                )
                if not _alt_bear_confirmed:
                    _bump_skip("buy_no_no_alt_momentum_confirm")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf if is_updown else "15m",
                        side=allowed_side,
                        action=action,
                        reason="buy_no_no_alt_momentum_confirm",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={
                            "alt_macd_5m_hist": float(sol.macd_5m.histogram or 0.0),
                            "alt_macd_5m_rising": bool(sol.macd_5m.histogram_rising),
                            "alt_macd_5m_crossover": sol.macd_5m.crossover,
                            "alt_macd_15m_hist": float(sol.macd_15m.histogram or 0.0),
                            "alt_macd_15m_rising": bool(sol.macd_15m.histogram_rising),
                            "alt_macd_15m_crossover": sol.macd_15m.crossover,
                            "side_source": side_source if "side_source" in locals() else None,
                        },
                    )
                    continue
            if (
                action == "BUY_YES"
                and not _bias_aligned_long
                and _alt_mc_window in (_alt_mc_cfg.get("buy_yes") or [])
            ):
                # Horizon-coherent: own timeframe + next-larger fallback only.
                if _alt_mc_window == "1h":
                    _own, _larger = sol.macd_1h, sol.macd_1h
                elif _alt_mc_window == "15m":
                    _own, _larger = sol.macd_15m, sol.macd_1h
                else:  # 5m
                    _own, _larger = sol.macd_5m, sol.macd_15m
                _alt_bull_confirmed = (
                    _own.crossover == "BULLISH_CROSS"
                    or (_own.histogram > 0 and _own.histogram_rising)
                    or _larger.crossover == "BULLISH_CROSS"
                    or (_larger.histogram > 0 and _larger.histogram_rising)
                )
                if not _alt_bull_confirmed:
                    _bump_skip("buy_yes_no_alt_momentum_confirm")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf if is_updown else "15m",
                        side=allowed_side,
                        action=action,
                        reason="buy_yes_no_alt_momentum_confirm",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={
                            "alt_macd_5m_hist": float(sol.macd_5m.histogram or 0.0),
                            "alt_macd_5m_rising": bool(sol.macd_5m.histogram_rising),
                            "alt_macd_5m_crossover": sol.macd_5m.crossover,
                            "alt_macd_15m_hist": float(sol.macd_15m.histogram or 0.0),
                            "alt_macd_15m_rising": bool(sol.macd_15m.histogram_rising),
                            "alt_macd_15m_crossover": sol.macd_15m.crossover,
                            "side_source": side_source if "side_source" in locals() else None,
                        },
                    )
                    continue

            _liq_floor = self._resolve_min_liquidity_floor(
                window_size=_updown_tf if is_updown else "15m",
                action=action,
            )
            if market.liquidity > 0 and market.liquidity < _liq_floor:
                _bump_skip("liquidity")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf if is_updown else "15m",
                    side=allowed_side,
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
                    window=_updown_tf if is_updown else "15m",
                    side=allowed_side,
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
            is_5m = _updown_tf == "5m"
            if is_updown and _updown_tf != "5m" and skip_15m_reason:
                _bump_skip(skip_15m_reason)
                logger.debug(
                    f"  {_brand} skip '{market.question[:40]}' — {skip_15m_reason}"
                )
                continue
            ai_used = False
            rsi_soft_delta = 0.0
            rsi_soft_penalty = 0.0
            reason_parts = [
                f"ALT_1H={macro_trend}",
                f"ALT_15M={bias_15m}",
                f"ALT_5M={bias_5m}",
                f"PRIMARY_ALT_HTF={primary_htf_bias}",
                f"side={allowed_side}",
                f"side_src={side_source}",
            ]
            if resolution.horizon_bias == "NEUTRAL":
                reason_parts.append(f"{_updown_tf}_neutral")
            if resolution.penalty_reasons:
                reason_parts.append(
                    f"bias_penalty={resolution.confidence_penalty:.3f}:{','.join(resolution.penalty_reasons)}"
                )
            dead_zone_would_block = False
            dead_zone_hour = None

            # ── UP/DOWN MARKETS (15m or 5m) ──
            if is_updown:
                # ── High-volatility hour filter (UTC) ──
                # Reads from blocked_utc_hours_updown in settings.yaml.
                # Live data: H18=20% WR (current session), H22=17% WR, H00=33% WR
                _dead_zone_enabled = self.config.get("dead_zone_enabled", True)
                _blocked_hours = self.config.get("blocked_utc_hours_updown", [0, 18, 22])
                _now_utc_hour = datetime.now(timezone.utc).hour
                dead_zone_hour = _now_utc_hour
                dead_zone_would_block = _now_utc_hour in _blocked_hours
                if _dead_zone_enabled:
                    if dead_zone_would_block:
                        _bump_skip("blocked_utc_hour")
                        logger.info(
                            f"  {_alt_label} skip updown at UTC hour {_now_utc_hour}:xx — "
                            f"blocked dead zone (config: {_blocked_hours})"
                        )
                        continue
                elif dead_zone_would_block:
                    logger.info(
                        f"  {_alt_label} dead_zone DISABLED — allowing UTC hour {_now_utc_hour:02d} "
                        f"(would-be blocked_hours={_blocked_hours})"
                    )

                # ── Entry window guard ──
                # Only enter within a tight window of the candle. If end_date is None
                # we skip — entering a market with unknown resolution time is too risky.
                if not market.end_date:
                    _bump_skip("missing_end_date")
                    logger.debug(f"  {_brand} skip '{market.question[:40]}' — no end_date, can't check window")
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

                # 2026-05-22: btc_min_move_dollars gate REMOVED. Previously skipped alt
                # updown entries when BTC hadn't moved enough in dollars (BTC deciding
                # alt admission, with a partial low-correlation bypass). Per "alts
                # decided by alt-native indicators", BTC volatility must not gate alt
                # entry. Diagnostic-only.
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
                    pass

                # Skip only when our entry-side price is in the unfavorable long
                # tail (paying high premium against the market). The favorable
                # tail (market already agrees with our side) is left in — ghost
                # log 2026-05-27 shows our-side price >= 0.80 wins 87–97% across
                # ~6k settled rejections; symmetric reject was throwing them out.
                _sample("entry_price", yes_price)
                _our_price = (1.0 - yes_price) if action == "BUY_NO" else yes_price
                if _our_price < 0.12:
                    _bump_skip("price_too_far_from_even")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=allowed_side,
                        action=action,
                        reason="price_too_far_from_even",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={
                            "entry_price": float(_our_price),
                            "yes_price": float(yes_price),
                            "our_side_price_min": 0.12,
                        },
                        probe_variants=build_range_probe_variants(
                            metric_name="our_side_entry_price",
                            observed_value=float(_our_price),
                            baseline_min=0.12,
                            baseline_max=1.0,
                            relax_steps=[0.02, 0.04],
                            tighten_steps=[0.03, 0.08],
                        ),
                        policy_version="entry_price_band_v2_side_aware",
                    )
                    logger.debug(
                        f"  {_brand} skip '{market.question[:40]}' — our-side "
                        f"price {_our_price:.2f} < 0.20 (yes={yes_price:.2f}, {action})"
                    )
                    continue

                # YES = "Up", NO = "Down"
                if allowed_side == "LONG":
                    action = "BUY_YES"
                    direction = "UP"
                else:
                    action = "BUY_NO"
                    direction = "DOWN"
                action_counts[action] = action_counts.get(action, 0) + 1
                side_source_counts[side_source if "side_source" in locals() else "neutral_macro"] = (
                    side_source_counts.get(side_source if "side_source" in locals() else "neutral_macro", 0) + 1
                )

                if getattr(corr, "degraded", False):
                    if self.skip_on_degraded_correlation:
                        if self._btc_trade_inputs_enabled():
                            _bump_skip("degraded_correlation")
                            logger.info(
                                f"  {_brand} skip '{market.question[:40]}' — correlation degraded "
                                f"({', '.join(getattr(corr, 'degraded_reasons', [])) or 'unknown'})"
                            )
                            continue
                        reason_parts.append("diag_corr_degraded")
                    elif self._btc_trade_inputs_enabled():
                        reason_parts.append("corr_degraded")
                    else:
                        reason_parts.append("diag_corr_degraded")

                # 2026-05-22: require_btc_volatility_gate previously skipped alt trades
                # when BTC was below a volatility floor (BTC deciding alt admission).
                # Per "alts decided by alt-native indicators" rule, BTC volatility must
                # not gate alt entry. The flag is preserved for back-compat but is now
                # diagnostic-only: low BTC volatility is logged in reason_parts and
                # surfaces in scan diagnostics, never blocks.
                if self.require_btc_volatility_gate:
                    _abs_btc_move_5m = abs(float(corr.btc_move_5m_pct))
                    _abs_btc_move_15m = abs(float(corr.btc_move_15m_pct))
                    _btc_min_move_pct = (
                        self.min_btc_move_pct_5m_for_lag_entries
                        if is_5m
                        else self.min_btc_move_pct_15m_for_lag_entries
                    )
                    _btc_move_for_gate = (
                        _abs_btc_move_5m
                        if is_5m
                        else max(_abs_btc_move_5m, _abs_btc_move_15m)
                    )
                    if _btc_move_for_gate < _btc_min_move_pct:
                        reason_parts.append(
                            f"diag_flat_btc({_btc_move_for_gate:.3f}%<{_btc_min_move_pct:.3f}%)"
                        )

                # Alt 1H context is diagnostic-only here. Keep logging the disagreement so
                # scans remain explainable, but do not block either side on this signal.
                _h1_trend = mtt.h1_trend  # "BULLISH", "BEARISH", or "NEUTRAL"
                if self.enforce_alt_1h_alignment:
                    if action == "BUY_NO" and _h1_trend == "BULLISH":
                        reason_parts.append("buy_no_against_alt_1h_bullish")
                        logger.info(
                            f"  {self._signal_strategy_name} allow {action} on '{market.question[:40]}' — "
                            f"alt 1H BULLISH retained as diagnostic only"
                        )
                    if action == "BUY_YES" and _h1_trend == "BEARISH":
                        reason_parts.append("buy_yes_against_alt_1h_bearish")
                        logger.info(
                            f"  {self._signal_strategy_name} allow BUY_YES on '{market.question[:40]}' — "
                            f"alt 1H BEARISH retained as diagnostic only"
                        )
                _rsi_hard_block, _rsi_soft_delta = self._resolve_rsi_gate(action, sol.rsi_14)
                if _rsi_hard_block:
                    _bump_skip("rsi_hard_blocked")
                    if action == "BUY_NO":
                        self._emit_buy_no_skip(
                            market=market,
                            bankroll=bankroll,
                            payload=self._make_buy_no_skip_payload(
                                market=market,
                                skip_reason="rsi_hard_blocked",
                                window_size=_updown_tf if is_updown else "15m",
                                yes_price=yes_price,
                                edge=0.0,
                                effective_min_edge=0.0,
                                rsi=sol.rsi_14,
                                htf_bias=primary_htf_bias,
                                signal_reason=" | ".join(reason_parts),
                                alt_1h_trend=mtt.h1_trend,
                                ghost_blind=True,
                            ),
                            counts=buy_no_skip_counts,
                            last_sample=last_buy_no_skip_sample,
                        )
                    logger.info(
                        f"  {self._signal_strategy_name} skip {action} on '{market.question[:40]}' — "
                        f"RSI={sol.rsi_14:.1f} hit configured hard gate"
                    )
                    continue
                rsi_soft_penalty = abs(_rsi_soft_delta)
                if rsi_soft_penalty > 0:
                    reason_parts.append(f"rsi_soft_penalty={rsi_soft_penalty:.3f}")
                    _sample("rsi_soft_penalty", rsi_soft_penalty)
                rsi_soft_delta = _rsi_soft_delta
                oracle_validation = self._validate_updown_oracle(
                    sol,
                    action=action,
                    window_size=_updown_tf if is_updown else "15m",
                )
                if not oracle_validation.passed:
                    _bump_skip(oracle_validation.reason)
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf if is_updown else "15m",
                        side=allowed_side,
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
                            "oracle_max_basis_bps": float(
                                self.config.get("oracle_max_basis_bps", 0.0) or 0.0
                            ),
                            "oracle_max_age_sec": float(self.oracle_max_age_sec),
                        },
                        probe_variants=build_upper_cap_probe_variants(
                            metric_name="oracle_basis_abs_bps",
                            observed_value=(
                                abs(float(oracle_validation.basis_bps))
                                if oracle_validation.basis_bps is not None
                                else None
                            ),
                            baseline_cap=float(
                                self.config.get("oracle_max_basis_bps", 0.0) or 0.0
                            ),
                            relax_steps=[2.0, 5.0, 10.0],
                            tighten_steps=[2.0, 5.0],
                        ) if oracle_validation.reason == "oracle_basis_block" else [],
                        policy_version="oracle_validation_v1",
                        stage="oracle",
                    )
                    if action == "BUY_NO":
                        self._emit_buy_no_skip(
                            market=market,
                            bankroll=bankroll,
                            payload=self._make_buy_no_skip_payload(
                                market=market,
                                skip_reason=oracle_validation.reason,
                                window_size=_updown_tf if is_updown else "15m",
                                yes_price=yes_price,
                                edge=0.0,
                                effective_min_edge=0.0,
                                rsi=sol.rsi_14,
                                htf_bias=primary_htf_bias,
                                signal_reason=" | ".join(reason_parts),
                                alt_1h_trend=mtt.h1_trend,
                                extra={
                                    "oracle_basis_bps": (
                                        round(float(oracle_validation.basis_bps), 2)
                                        if oracle_validation.basis_bps is not None
                                        else None
                                    ),
                                    "oracle_freshness_sec": (
                                        round(float(oracle_validation.freshness_sec), 1)
                                        if oracle_validation.freshness_sec is not None
                                        else None
                                    ),
                                    "oracle_max_basis_bps": float(
                                        self.config.get("oracle_max_basis_bps", 0.0) or 0.0
                                    ),
                                },
                            ),
                            counts=buy_no_skip_counts,
                            last_sample=last_buy_no_skip_sample,
                        )
                    logger.info(
                        f"  {self._signal_strategy_name} skip {action} on '{market.question[:40]}' — "
                        f"{oracle_validation.reason} "
                        f"basis={oracle_validation.basis_bps if oracle_validation.basis_bps is not None else 'n/a'} "
                        f"fresh={oracle_validation.freshness_sec if oracle_validation.freshness_sec is not None else 'n/a'}"
                    )
                    continue
                reason_parts.append(
                    f"oracle_basis={oracle_validation.basis_bps:+.1f}bps"
                    if oracle_validation.basis_bps is not None
                    else "oracle_basis=n/a"
                )

                if is_5m:
                    # ── [5m] FIVE-MINUTE UP/DOWN MARKET PATH ──
                    # Macro trend (1H) still gates direction
                    # Skip 15m confirmation layer — go straight to 5m entry signals
                    # BTC-SOL lag detection is MORE relevant for 5m (faster catch-up)
                    est_prob_up = 0.50

                    # Macro trend boost (lighter for 5m — shorter window)
                    est_prob_up = self._apply_primary_htf_bias(
                        est_prob_up, primary_htf_bias, 0.03
                    )
                    est_prob_up = self._apply_degraded_corr_bias(
                        est_prob_up, primary_htf_bias, corr
                    )
                    if resolution.confidence_penalty > 0:
                        est_prob_up += (
                            -resolution.confidence_penalty
                            if allowed_side == "LONG"
                            else resolution.confidence_penalty
                        )

                    # 1H histogram alignment (Move 2, 2026-05-16 calibration). Previously
                    # diagnostic-only; now dampens est_prob_up toward neutral when 1H
                    # disagrees with trade direction. Magnitude matches macro weight so
                    # disagreement roughly cancels the macro tilt.
                    _macd_1h = sol.macd_1h
                    _h1_bull_ok = _macd_1h.histogram_rising or _macd_1h.histogram > 0
                    _h1_bear_ok = (not _macd_1h.histogram_rising) or _macd_1h.histogram < 0
                    if self.enforce_alt_1h_alignment:
                        if allowed_side == "LONG" and not _h1_bull_ok:
                            est_prob_up -= 0.04
                            reason_parts.append("h1_dampen_long_5m")
                            logger.info(
                                f"  {_alt_label} [5m] allow '{market.question[:40]}' — "
                                f"1H histogram against LONG, est_prob dampened -0.04 "
                                f"(hist={_macd_1h.histogram:.4f})"
                            )
                        if allowed_side == "SHORT" and not _h1_bear_ok:
                            est_prob_up += 0.04
                            reason_parts.append("h1_dampen_short_5m")
                            logger.info(
                                f"  {_alt_label} [5m] allow '{market.question[:40]}' — "
                                f"1H histogram against SHORT, est_prob dampened +0.04 "
                                f"(hist={_macd_1h.histogram:.4f})"
                            )

                    # 2026-05-22: require_btc_catalyst_5m gate REMOVED.
                    # Previously this skipped 5m alt entries unless BTC was spiking or
                    # had a lag opportunity — i.e. BTC was deciding whether the alt
                    # trade could happen. Per "alts decided by alt-native indicators",
                    # admission must not depend on BTC state. The `require_btc_catalyst_5m`
                    # config key is now vestigial; BTC spike/lag still appear in scan
                    # diagnostics for observability outside trade reasons.
                    if bool(self.config.get("require_btc_catalyst_5m", False)):
                        pass

                    # 5m MACD — primary entry signal for 5m markets
                    # ta.sol.macd_5m exists on SOLAnalysis
                    macd_5m = sol.macd_5m
                    m5_adj = 0.0
                    m5_reasons = []
                    if allowed_side == "LONG":
                        if macd_5m.crossover == "BULLISH_CROSS":
                            m5_adj = 0.06
                            m5_reasons.append("5m MACD bull cross")
                        elif macd_5m.histogram_rising and macd_5m.histogram > 0:
                            m5_adj = 0.04
                            m5_reasons.append("5m hist green+rising")
                        elif macd_5m.macd_line > macd_5m.signal_line:
                            m5_adj = 0.02
                            m5_reasons.append("5m MACD>signal")
                        elif macd_5m.crossover == "BEARISH_CROSS" or macd_5m.histogram < 0:
                            m5_adj = -0.04
                            m5_reasons.append(f"5m against ({macd_5m.crossover})")
                    else:  # SHORT
                        if macd_5m.crossover == "BEARISH_CROSS":
                            m5_adj = 0.06
                            m5_reasons.append("5m MACD bear cross")
                        elif not macd_5m.histogram_rising and macd_5m.histogram < 0:
                            m5_adj = 0.04
                            m5_reasons.append("5m hist red+falling")
                        elif macd_5m.macd_line < macd_5m.signal_line:
                            m5_adj = 0.02
                            m5_reasons.append("5m MACD<signal")
                        elif macd_5m.crossover == "BULLISH_CROSS" or macd_5m.histogram > 0:
                            m5_adj = -0.04
                            m5_reasons.append(f"5m against ({macd_5m.crossover})")

                    _sample("m5_adj", m5_adj)
                    if action == "BUY_NO" and self.sell_5m_min_corr >= 0 and corr.correlation_1h < self.sell_5m_min_corr:
                        pass

                    if not self._strong_enough_5m_signal(m5_adj, action):
                        _min_req = (
                            self.min_positive_m5_adj_5m_sell
                            if action == "BUY_NO"
                            else self.min_positive_m5_adj_5m
                        )
                        # 2026-05-29: bearish_dip_default / bearish_dip_exception
                        # legitimately fire when 5m is counter-HTF (the "dip"
                        # is what we're trading) — so a weak 5m signal in the
                        # SHORT direction is expected. Baseline session
                        # test_20260527_042014 made +$43 on xrp 5m bearish_dip
                        # via this exact setup; converting hard reject to soft
                        # penalty for this side_source family recovers it
                        # without affecting other paths. Penalty caps the
                        # confidence boost so we don't trade weak setups at
                        # full conviction.
                        _is_dip_path = str(side_source or "").startswith("bearish_dip")
                        if _is_dip_path:
                            _dip_penalty = 0.02  # est_prob shrink toward 0.5
                            if allowed_side == "LONG":
                                est_prob_up -= _dip_penalty
                            else:
                                est_prob_up += _dip_penalty
                            reason_parts.append(
                                f"weak_5m_penalty_dip(m5_adj={m5_adj:+.2f})"
                            )
                            # fall through to the rest of scoring; do not reject
                        else:
                            _bump_skip("weak_5m_signal")
                            log_rejected_candidate(
                                strategy=self._signal_strategy_name, window="5m",
                                side=allowed_side, action=action,
                                reason="weak_5m_signal", market=market,
                                yes_price=yes_price, est_prob_up=est_prob_up,
                                htf_bias=primary_htf_bias,
                                stage="signal_strength_5m",
                                context={
                                    "m5_adj": float(m5_adj),
                                    "min_required": float(_min_req),
                                    "side_source": str(side_source or ""),
                                    "macd_5m_crossover": str(getattr(macd_5m, "crossover", "")),
                                    "macd_5m_histogram": float(getattr(macd_5m, "histogram", 0.0) or 0.0),
                                    **build_market_context(
                                        asset_spot=sol.current_price,
                                        btc_spot=corr.btc_price,
                                        rsi_14=sol.rsi_14,
                                        atr_14=sol.atr_14,
                                    ),
                                },
                            )
                            logger.info(
                                f"  {_alt_label} [5m] skip '{market.question[:40]}' — "
                                f"5m signal too weak (m5_adj={m5_adj:+.2f}, min={_min_req:.2f})"
                            )
                            continue

                    if allowed_side == "LONG":
                        est_prob_up += m5_adj
                    else:
                        est_prob_up -= m5_adj

                    # Also use mtt.m5_trend for additional 5m directional context
                    if mtt.m5_trend == "BULLISH" and allowed_side == "LONG":
                        est_prob_up += 0.02
                        m5_reasons.append("5m_trend_bull")
                    elif mtt.m5_trend == "BEARISH" and allowed_side == "SHORT":
                        est_prob_up -= 0.02
                        m5_reasons.append("5m_trend_bear")

                    # RSI extremes (very light for 5m)
                    if sol.rsi_14 > 75:
                        est_prob_up -= 0.02
                    elif sol.rsi_14 < 25:
                        est_prob_up += 0.02

                    # BTC correlation must not alter admission or probability.
                    if self._low_corr_blocks_entry(corr):
                        _bump_skip("low_corr_suppressed")
                        _log_skip_reject(
                            market=market,
                            window="5m",
                            side=allowed_side,
                            action=action,
                            reason="low_corr_suppressed",
                            yes_price=yes_price,
                            est_prob_up=est_prob_up,
                            htf_bias=primary_htf_bias,
                            stage="low_corr_suppressed",
                            context={
                                "correlation_1h": float(corr.correlation_1h),
                                "low_corr_threshold_1h": float(self.low_corr_threshold_1h),
                            },
                        )
                        logger.info(
                            f"  {_alt_label} [5m] skip '{market.question[:40]}' — "
                            f"1H corr {corr.correlation_1h:.2f} below hard floor "
                            f"{self.low_corr_threshold_1h:.2f}"
                        )
                        continue
                    elif self._btc_trade_inputs_enabled() and corr.correlation_1h < self.low_corr_threshold_1h:
                        est_prob_up = 0.50 + (est_prob_up - 0.50) * self.low_corr_damping
                        reason_parts.append(f"low_corr_5m({corr.correlation_1h:.2f})")

                    if rsi_soft_delta != 0.0:
                        est_prob_up += rsi_soft_delta

                    # Fresh-opposing 5m cross override (uniform helper; see
                    # apply_fresh_cross_override / changelog 2026-06-04).
                    est_prob_up, action, allowed_side, direction, side_source = apply_fresh_cross_override(
                        est_prob_up=est_prob_up, action=action, allowed_side=allowed_side,
                        direction=direction, side_source=side_source, reason_parts=reason_parts,
                        crossover=macd_5m.crossover, tf_label="5m",
                        strategy_name=self._signal_strategy_name, primary_htf_bias=primary_htf_bias,
                        logger=logger, enabled=self.config.get("fresh_cross_override", True),
                    )

                    est_prob_up = max(0.10, min(0.90, est_prob_up))
                    raw_est_prob = est_prob_up
                    estimated_prob = self._calibrate_est_prob(
                        raw_est_prob,
                        action=action,
                        direction=direction,
                        window_size=_updown_tf,
                        side_source=side_source if "side_source" in locals() else "neutral_macro",
                        signal_reason=" | ".join(r for r in reason_parts if r),
                        htf_bias=primary_htf_bias,
                        primary_htf_bias=primary_htf_bias,
                        alt_htf_bias=mtt.h1_trend,
                        btc_1h_regime=btc_1h_regime if btc_ta else None,
                    )

                    # Directional flip (opt-in). If the calibrator has high-sample
                    # evidence the bias-chosen side reliably LOSES, take the
                    # OPPOSITE side. est_prob is P(UP) — side-independent — so no
                    # recompute; the flipped action's edge is evaluated below with
                    # the same β-corrected est_prob and must clear the normal edge
                    # gate. Uses the exact lane_id the calibrator just keyed on.
                    if self._directional_flip_enabled():
                        _cal = getattr(self, "lane_calibrator", None)
                        _flip_lid = getattr(self, "_last_calibration_lane_id", "")
                        if _cal is not None and _flip_lid and _cal.flip_recommended(_flip_lid):
                            action = "BUY_YES" if action == "BUY_NO" else "BUY_NO"
                            direction = "UP" if action == "BUY_YES" else "DOWN"
                            allowed_side = "LONG" if action == "BUY_YES" else "SHORT"
                            side_source = f"{side_source or ''}+posterior_flip"
                            reason_parts.append(f"posterior_flip<-{_flip_lid}")
                            logger.info(
                                "  %s POSTERIOR FLIP -> %s (lane %s reliably lost)",
                                self._signal_strategy_name, action, _flip_lid,
                            )

                    _byn_floor_5m = self._alt_buy_yes_bullish_floor_bump(
                        window_size="5m", action=action, htf_bias=primary_htf_bias,
                    )
                    if _byn_floor_5m > 0:
                        estimated_prob = min(0.90, estimated_prob + _byn_floor_5m)
                        reason_parts.append(f"5m_buy_yes_floor=+{_byn_floor_5m:.2f}")

                    if action == "BUY_YES":
                        edge = estimated_prob - yes_price
                    else:
                        edge = (1.0 - estimated_prob) - (1.0 - yes_price)
                    # Confidence: alt-native 5m MACD momentum + RSI alignment.
                    _rsi_conf_5m = 0.03 if (
                        (action == "BUY_YES" and sol.rsi_14 < 40) or
                        (action == "BUY_NO" and sol.rsi_14 > 60)
                    ) else 0.0
                    confidence = max(0.50, min(0.85,
                        0.50 + abs(m5_adj) * 2.0 + _rsi_conf_5m + abs(timing_bonus) * 0.3
                    ))

                    reason_parts.extend([
                        "[5m]",
                        "UPDOWN_5m",
                        f"{_spot_key}=${sol_price:,.2f}",
                        f"est_up={estimated_prob:.3f}",
                        f"mkt_yes={yes_price:.3f}",
                        f"5m_MACD={'+' if macd_5m.macd_line > macd_5m.signal_line else '-'}{abs(macd_5m.histogram):.3f}",
                        f"RSI={sol.rsi_14:.0f}",
                    ])
                    reason_parts.extend(m5_reasons)

                    logger.debug(
                        f"  [5m] {_alt_label} updown '{market.question[:45]}' "
                        f"macro={macro_trend} m5_adj={m5_adj:+.2f} "
                        f"est_up={estimated_prob:.3f} edge={edge:.4f}"
                    )

                else:
                    # ── LONGER-CYCLE UP/DOWN MARKET PATH (15m / 1h) ──
                    # PRIMARY signal: macro trend + LTF confirmation (live data evidence)
                    # SECONDARY signal: lag / spike (small probability booster only)
                    est_prob_up = 0.50
                    is_hourly = _updown_tf == "1h"
                    window_label = "1h" if is_hourly else "15m"
                    primary_bias_weight = 0.09 if is_hourly else 0.07
                    h1_dampen = 0.07 if is_hourly else 0.05
                    ltf_weight = 0.12 if is_hourly else 0.22
                    timing_weight = 0.50 if is_hourly else 1.00
                    rsi_extreme = 0.02 if is_hourly else 0.03

                    if not self._passes_iql(ta, allowed_side, primary_htf_bias, _updown_tf):
                        _bump_skip("iql_15m_reject")
                        effective_floor = (
                            self.iql_1h_hist_floor
                            if is_hourly
                            else self._effective_iql_15m_floor(allowed_side, primary_htf_bias)
                        )
                        log_rejected_candidate(
                            strategy=self._signal_strategy_name, window=window_label,
                            side=allowed_side, action=action,
                            reason="iql_15m_reject", market=market,
                            yes_price=yes_price, est_prob_up=est_prob_up,
                            htf_bias=primary_htf_bias,
                            stage="iql_15m",
                            context={
                                "macd_15m_histogram": float(getattr(sol.macd_15m, "histogram", 0.0) or 0.0),
                                "macd_15m_crossover": str(getattr(sol.macd_15m, "crossover", "")),
                                "iql_15m_hist_floor": float(effective_floor),
                                "iql_15m_hist_floor_default": float(self.iql_15m_hist_floor),
                                "iql_15m_hist_floor_aligned_short": float(
                                    self.iql_15m_hist_floor_aligned_short
                                ),
                                **build_market_context(
                                    asset_spot=sol.current_price,
                                    btc_spot=corr.btc_price,
                                    rsi_14=sol.rsi_14,
                                    atr_14=sol.atr_14,
                                ),
                            },
                        )
                        logger.info(
                            f"  {_alt_label} [{window_label}] skip '{market.question[:40]}' — "
                            f"IQL reject (hist={sol.macd_15m.histogram:+.3f} "
                            f"cross={sol.macd_15m.crossover}, floor={effective_floor:.3f}, "
                            f"bias={primary_htf_bias})"
                        )
                        continue

                    # Macro trend — PRIMARY driver (increased from 0.05 since it's now the gate)
                    est_prob_up = self._apply_primary_htf_bias(
                        est_prob_up, primary_htf_bias, primary_bias_weight
                    )
                    est_prob_up = self._apply_degraded_corr_bias(
                        est_prob_up, primary_htf_bias, corr
                    )
                    if resolution.confidence_penalty > 0:
                        est_prob_up += (
                            -resolution.confidence_penalty
                            if allowed_side == "LONG"
                            else resolution.confidence_penalty
                        )

                    # 1H histogram alignment (Move 2, 2026-05-16 calibration). Every losing
                    # SHORT lane in this cohort had 1H disagreement; previously logged only.
                    # Dampen toward neutral when 1H disagrees (magnitude ~0.05, sized close
                    # to the 15m macro weight so disagreement cancels roughly the macro tilt).
                    _macd_1h = sol.macd_1h
                    _h1_bull_ok = _macd_1h.histogram_rising or _macd_1h.histogram > 0
                    _h1_bear_ok = (not _macd_1h.histogram_rising) or _macd_1h.histogram < 0
                    if self.enforce_alt_1h_alignment:
                        if allowed_side == "LONG" and not _h1_bull_ok:
                            est_prob_up -= h1_dampen
                            reason_parts.append(f"h1_dampen_long_{window_label}")
                            logger.info(
                                f"  {_alt_label} [{window_label}] allow '{market.question[:40]}' — "
                                f"1H histogram against LONG, est_prob dampened -{h1_dampen:.2f} "
                                f"(hist={_macd_1h.histogram:.4f})"
                            )
                        if allowed_side == "SHORT" and not _h1_bear_ok:
                            est_prob_up += h1_dampen
                            reason_parts.append(f"h1_dampen_short_{window_label}")
                            logger.info(
                                f"  {_alt_label} [{window_label}] allow '{market.question[:40]}' — "
                                f"1H histogram against SHORT, est_prob dampened +{h1_dampen:.2f} "
                                f"(hist={_macd_1h.histogram:.4f})"
                            )

                    # 2026-05-22: require_btc_catalyst_15m_when_unconfirmed REMOVED.
                    # Previously, when LTF was unconfirmed, skipped 15m alt entries
                    # unless BTC was spiking or had a lag opportunity (BTC gating
                    # admission). Per "alts decided by alt-native indicators", removed.
                    # Vestigial config key is ignored for alt trade reasons.
                    if ltf_strength == 0.0 and bool(
                        self.config.get("require_btc_catalyst_15m_when_unconfirmed", False)
                    ):
                        pass

                    # LTF confirmation — PRIMARY probability driver (increased from 0.18)
                    ltf_adj = ltf_strength * ltf_weight
                    est_prob_up += ltf_adj if allowed_side == "LONG" else -ltf_adj
                    hourly_buy_yes_bonus = self._hourly_buy_yes_native_bonus(
                        window_size=window_label,
                        allowed_side=allowed_side,
                        resolution=resolution,
                        ltf_strength=ltf_strength,
                    )
                    if hourly_buy_yes_bonus > 0:
                        est_prob_up += hourly_buy_yes_bonus
                        reason_parts.append(
                            f"hourly_buy_yes_native_bonus={hourly_buy_yes_bonus:+.3f}"
                        )

                    # BTC catalyst context is diagnostic-only for SOL.
                    if self._btc_trade_inputs_enabled():
                        lag_boost_min = 0.015 if is_hourly else 0.02
                        lag_boost_max = 0.03 if is_hourly else 0.04
                        spike_boost = 0.02 if is_hourly else 0.03
                        if corr.lag_opportunity and corr.opportunity_direction == allowed_side:
                            _lag_boost = min(
                                lag_boost_max,
                                max(lag_boost_min, abs(corr.opportunity_magnitude) * 0.015),
                            )
                            est_prob_up += _lag_boost if allowed_side == "LONG" else -_lag_boost
                        elif corr.btc_spike_detected:
                            est_prob_up += spike_boost if allowed_side == "LONG" else -spike_boost
                    elif corr.lag_opportunity or corr.btc_spike_detected:
                        pass

                    # Timing / 5m momentum
                    if allowed_side == "LONG":
                        est_prob_up += timing_bonus * timing_weight
                    else:
                        est_prob_up -= timing_bonus * timing_weight

                    # RSI extremes
                    if sol.rsi_14 > 75:
                        est_prob_up -= rsi_extreme
                    elif sol.rsi_14 < 25:
                        est_prob_up += rsi_extreme

                    # BTC correlation must not alter admission or probability.
                    if self._low_corr_blocks_entry(corr):
                        _bump_skip("low_corr_suppressed")
                        logger.info(
                            f"  {_alt_label} [15m] skip '{market.question[:40]}' — "
                            f"1H corr {corr.correlation_1h:.2f} below hard floor "
                            f"{self.low_corr_threshold_1h:.2f}"
                        )
                        continue
                    elif self._btc_trade_inputs_enabled() and corr.correlation_1h < self.low_corr_threshold_1h:
                        est_prob_up = 0.50 + (est_prob_up - 0.50) * self.low_corr_damping
                        reason_parts.append(f"low_corr({corr.correlation_1h:.2f})")

                    if rsi_soft_delta != 0.0:
                        est_prob_up += rsi_soft_delta

                    # Fresh-opposing cross override — own-TF (15m/1h) + faster-TF
                    # leads (1h reads 15m, 15m reads 5m) so the slow windows aren't
                    # short-jammed when the fast MACD has already turned.
                    est_prob_up, action, allowed_side, direction, side_source = apply_fresh_cross_override(
                        est_prob_up=est_prob_up, action=action, allowed_side=allowed_side,
                        direction=direction, side_source=side_source, reason_parts=reason_parts,
                        crossover=(ta.sol.macd_1h if is_hourly else ta.sol.macd_15m).crossover,
                        tf_label=window_label,
                        faster_crossover=(ta.sol.macd_15m if is_hourly else ta.sol.macd_5m).crossover,
                        faster_tf_label=("15m" if is_hourly else "5m"),
                        strategy_name=self._signal_strategy_name, primary_htf_bias=primary_htf_bias,
                        logger=logger, enabled=self.config.get("fresh_cross_override", True),
                    )

                    est_prob_up = max(0.10, min(0.90, est_prob_up))
                    raw_est_prob = est_prob_up
                    estimated_prob = self._calibrate_est_prob(
                        raw_est_prob,
                        action=action,
                        direction=direction,
                        window_size=_updown_tf,
                        side_source=side_source if "side_source" in locals() else "neutral_macro",
                        signal_reason=" | ".join(r for r in reason_parts if r),
                        htf_bias=primary_htf_bias,
                        primary_htf_bias=primary_htf_bias,
                        alt_htf_bias=mtt.h1_trend,
                        btc_1h_regime=btc_1h_regime if btc_ta else None,
                    )

                    _byn_floor = self._alt_buy_yes_bullish_floor_bump(
                        window_size=window_label, action=action, htf_bias=primary_htf_bias,
                    )
                    if _byn_floor > 0:
                        estimated_prob = min(0.90, estimated_prob + _byn_floor)
                        reason_parts.append(f"{window_label}_buy_yes_floor=+{_byn_floor:.2f}")

                    if action == "BUY_YES":
                        edge = estimated_prob - yes_price
                    else:
                        edge = (1.0 - estimated_prob) - (1.0 - yes_price)
                    # Confidence driven by LTF strength (primary); lag signal removed
                    confidence = min(0.85, 0.50 + ltf_strength * ltf_weight + abs(timing_bonus) * 0.5 * timing_weight)

                    reason_parts.extend([
                        f"UPDOWN_{window_label}",
                        f"{_spot_key}=${sol_price:,.2f}",
                        f"est_up={estimated_prob:.3f}",
                        f"mkt_yes={yes_price:.3f}",
                        f"RSI={sol.rsi_14:.0f}",
                    ])
                    reason_parts.extend(ltf_reasons)
                    if timing_reasons:
                        reason_parts.extend(timing_reasons)

            else:
                # ── TRADITIONAL THRESHOLD MARKETS ──
                direction = self._extract_direction(market.question)
                threshold = self._extract_price_threshold(market.question)

                # Entry price filter
                if yes_price < self.entry_price_min or yes_price > self.entry_price_max:
                    _bump_skip("threshold_entry_price_band")
                    continue

                days_to_resolution = 30
                if market.end_date:
                    end_date = market.end_date
                    if end_date.tzinfo is None:
                        end_date = end_date.replace(tzinfo=timezone.utc)
                    days_to_resolution = max(
                        1, (end_date - datetime.now(timezone.utc)).days
                    )

                # Enforce macro trend gate
                if allowed_side == "LONG":
                    action = "BUY_YES" if direction == "UP" else "BUY_NO"
                else:
                    action = "BUY_NO" if direction == "UP" else "BUY_YES"
                action_counts[action] = action_counts.get(action, 0) + 1
                side_source_counts[side_source if "side_source" in locals() else "neutral_macro"] = (
                    side_source_counts.get(side_source if "side_source" in locals() else "neutral_macro", 0) + 1
                )

                _rsi_hard_block, _rsi_soft_delta = self._resolve_rsi_gate(action, sol.rsi_14)
                if _rsi_hard_block:
                    _bump_skip("rsi_hard_blocked")
                    logger.info(
                        f"  {self._signal_strategy_name} skip {action} on '{market.question[:40]}' — "
                        f"RSI={sol.rsi_14:.1f} hit configured hard gate"
                    )
                    continue
                rsi_soft_penalty = abs(_rsi_soft_delta)
                if rsi_soft_penalty > 0:
                    reason_parts.append(f"rsi_soft_penalty={rsi_soft_penalty:.3f}")
                    _sample("rsi_soft_penalty", rsi_soft_penalty)
                if self._oracle_basis_blocks_entry(sol.oracle_basis_bps):
                    _bump_skip("threshold_oracle_basis_block")
                    logger.info(
                        f"  {self._signal_strategy_name} skip {action} on '{market.question[:40]}' — "
                        f"oracle basis {sol.oracle_basis_bps:+.1f}bps exceeds cap"
                    )
                    continue

                if not threshold:
                    _bump_skip("missing_threshold")
                    continue  # Can't calculate edge without threshold on traditional markets

                distance_pct = abs(sol_price - threshold) / threshold
                estimated_prob = self._estimate_probability(
                    sol_price, threshold, direction, ta,
                    days_to_resolution, ltf_strength, timing_bonus,
                )
                if _rsi_soft_delta != 0.0:
                    estimated_prob += _rsi_soft_delta
                estimated_prob = max(0.10, min(0.90, estimated_prob))
                raw_est_prob = estimated_prob
                estimated_prob = self._calibrate_est_prob(
                    raw_est_prob,
                    action=action,
                    direction=direction,
                    window_size="15m",
                    side_source=side_source if "side_source" in locals() else "neutral_macro",
                    signal_reason=" | ".join(r for r in reason_parts if r),
                    htf_bias=primary_htf_bias,
                    primary_htf_bias=primary_htf_bias,
                    alt_htf_bias=mtt.h1_trend,
                    btc_1h_regime=btc_1h_regime if btc_ta else None,
                )

                if action == "BUY_YES":
                    edge = estimated_prob - yes_price
                else:
                    edge = (1.0 - estimated_prob) - (1.0 - yes_price)
                reason_parts.extend([
                    f"{_spot_key}=${sol_price:,.2f}",
                    f"target=${threshold:,.2f}",
                    f"dist={distance_pct:.1%}",
                    f"est_prob={estimated_prob:.2f}",
                    f"mkt_yes={yes_price:.2f}",
                ])
                reason_parts.extend(ltf_reasons)
                if timing_reasons:
                    reason_parts.extend(timing_reasons)

                confidence = min(0.85, 0.50 + ltf_strength * 0.20 + timing_bonus + distance_pct * 0.5)

            if is_updown:
                sol_guard_reason = self._sol_signal_guard_reason(
                    window_size=_updown_tf,
                    action=action,
                    side_source=side_source,
                    yes_price=yes_price,
                    btc_1h_regime=btc_1h_regime if btc_ta else None,
                    alt_h1_trend=mtt.h1_trend,
                )
                if sol_guard_reason:
                    _bump_skip(sol_guard_reason)
                    logger.info(
                        "  %s [%s] skip '%s' — local SOL guard %s",
                        self._signal_strategy_name,
                        window_label,
                        market.question[:40],
                        sol_guard_reason,
                    )
                    if action == "BUY_NO":
                        self._emit_buy_no_skip(
                            market=market,
                            bankroll=bankroll,
                            payload=self._make_buy_no_skip_payload(
                                market=market,
                                skip_reason=sol_guard_reason,
                                window_size=_updown_tf,
                                yes_price=yes_price,
                                edge=edge,
                                effective_min_edge=float(self._min_edge_for_window(_updown_tf)),
                                rsi=sol.rsi_14,
                                htf_bias=primary_htf_bias,
                                signal_reason=" | ".join(r for r in reason_parts if r),
                                alt_1h_trend=mtt.h1_trend,
                                ghost_blind=True,
                                extra={
                                    "side_source": side_source,
                                    "btc_1h_regime": btc_1h_regime if btc_ta else None,
                                },
                            ),
                            counts=buy_no_skip_counts,
                            last_sample=last_buy_no_skip_sample,
                        )
                    continue

                # AI-hold soft veto: block any entry (marginal or strong) if AI said HOLD
                # on this market within the veto TTL.
                _hold_ts = self._ai_hold_cache.get(market.id, 0)
                _hold_age = time.time() - _hold_ts
                if _hold_age < self.ai_hold_veto_ttl_sec:
                    _hold_lane_side, _hold_lane_policy = self._resolve_lane_entry_policy(
                        window_size=_updown_tf if is_updown else "15m",
                        action=action,
                        direction=direction,
                    )
                    _lane_ai_override = max(
                        _hold_lane_policy.ai_override_min_edge,
                        _hold_lane_policy.min_edge,
                    )
                    if edge < _lane_ai_override:
                        logger.info(
                            f"  {self._signal_strategy_name} ai-hold veto '{market.question[:45]}' — "
                            f"edge={edge:.4f} < override={_lane_ai_override:.4f} "
                            f"(AI said HOLD {_hold_age:.0f}s ago)"
                        )
                        continue

                # AI tiebreaker for marginal edge (skipped when AI offline or use_ai false)
                _marginal_min_edge = self._min_edge_for_window(
                    _updown_tf if is_updown else "15m"
                )
                # Don't spend AI budget on markets outside the lane entry window
                # (too early) — they're rejected by lane_entry_window below anyway.
                # XRP this session: 100% of ai_call_limit rejects were 61-134 min
                # out of a 32-min window, exhausting max_ai_calls_per_scan before
                # in-window candidates could be scored. ETH's path already gates
                # marginal AI on its timing window; the sol-family path did not.
                # This early resolve is a budget pre-filter only — the authoritative
                # window gate at lane_entry_window (below) re-resolves lane_policy.
                _ai_pre_side, _ai_pre_policy = self._resolve_lane_entry_policy(
                    window_size=_updown_tf if is_updown else "15m",
                    action=action,
                    direction=direction,
                )
                _ai_in_entry_window = (not is_updown) or (
                    _ai_pre_policy.entry_window_min
                    <= _eval_left
                    <= _ai_pre_policy.entry_window_max
                )
                if (
                    edge < _marginal_min_edge and edge > 0.03
                    # 5m never calls AI — quant only. AI tiebreaker is 15m/1h.
                    and (_updown_tf if is_updown else "15m") in self._DECISION_GATE_WINDOWS
                    # don't burn AI budget on markets that aren't tradable yet
                    and _ai_in_entry_window
                    # decision layer off + flag on: skip AI, admit on quant terms downstream
                    and not self._admit_marginal_quant_short(edge, allowed_side, _timing_window_open)
                ):
                    def _log_ai_veto(_reason: str, **extra: Any) -> None:
                        _log_skip_reject(
                            market=market,
                            window=_updown_tf if is_updown else "15m",
                            side=allowed_side,
                            action=action,
                            reason=_reason,
                            yes_price=yes_price,
                            est_prob_up=estimated_prob,
                            htf_bias=primary_htf_bias,
                            stage="ai_veto",
                            context={
                                "edge": round(float(edge), 6),
                                "min_edge": float(_marginal_min_edge),
                                **extra,
                            },
                        )
                    if not self.config.get("use_ai", True):
                        _bump_skip("ai_disabled_marginal_threshold")
                        _log_ai_veto("ai_disabled_marginal_threshold")
                        logger.debug(
                            f"{_brand}: use_ai=false — skipping marginal trade "
                            f"'{market.question[:40]}...' edge={edge:.4f}"
                        )
                        continue
                    if not self.ai_agent.is_available():
                        _bump_skip("ai_offline_marginal_threshold")
                        _log_ai_veto("ai_offline_marginal_threshold")
                        logger.debug(
                            f"{_brand}: AI offline — skipping marginal trade "
                            f"'{market.question[:40]}...' edge={edge:.4f}"
                        )
                        continue
                    if ai_calls >= self.max_ai_calls_per_scan:
                        _bump_skip("ai_call_limit_marginal_threshold")
                        _log_ai_veto("ai_call_limit_marginal_threshold")
                        logger.debug(
                            f"{_brand}: max AI calls per scan ({self.max_ai_calls_per_scan}) — "
                            f"skipping marginal '{market.question[:40]}...'"
                        )
                        continue
                    # Up/down markets have no price threshold — give MiniMax a coherent
                    # instrument description instead of a meaningless "Threshold: $None".
                    if is_updown:
                        _ai_instrument_line = (
                            f"{_alt_label} Price: ${sol_price:,.2f} | "
                            f"{_updown_tf} up/down market, resolves {direction}\n"
                        )
                        _ai_horizon_line = f"Horizon: {_updown_tf} window to resolution\n\n"
                    else:
                        _ai_instrument_line = (
                            f"{_alt_label} Price: ${sol_price:,.2f} | "
                            f"Threshold: ${threshold:,.2f} ({direction})\n"
                        )
                        _ai_horizon_line = (
                            f"Distance: {distance_pct:.1%} | Days left: {days_to_resolution}\n\n"
                        )
                    ai_context = (
                        f"{market.description}\n\n"
                        f"=== LIVE {_alt_label} DATA ===\n"
                        + _ai_instrument_line
                        + f"{_alt_label} Oracle: {sol.chainlink_network or 'n/a'} "
                        + f"{f'${sol.chainlink_price:,.2f}' if sol.chainlink_price is not None else 'n/a'} | "
                        + f"basis={f'{sol.oracle_basis_bps:+.1f}bps' if sol.oracle_basis_bps is not None else 'n/a'}\n"
                        + _ai_horizon_line
                    ) + (
                        f"=== MACRO (1H) — {macro_trend} ===\n"
                        f"Primary alt HTF bias: {primary_htf_bias}\n"
                        f"EMA: 9=${sol.ema_9:,.2f} 21=${sol.ema_21:,.2f} 50=${sol.ema_50:,.2f}\n"
                        f"RSI: {sol.rsi_14:.1f}\n\n"
                        f"=== 15m CONFIRMATION ===\n"
                        f"15m MACD: hist={sol.macd_15m.histogram:+.3f} {sol.macd_15m.crossover}\n\n"
                        f"Allowed side: {allowed_side}\n"
                        f"Quant edge={edge:.4f} min_edge={_marginal_min_edge:.4f}\n"
                        f"Should we take this {action} trade, or HOLD?\n"
                        f"\n=== MARKET ===\n{format_market_metadata(market)}"
                    )
                    ai_lane_id = str(
                        build_lane_metadata(
                            strategy=self._signal_strategy_name,
                            window_size=_updown_tf if is_updown else "15m",
                            action=action,
                            direction=direction,
                            entry_leg=("NO" if action == "BUY_NO" else "YES"),
                            ai_used=True,
                            reason="ai_decision",
                            signal_reason="ai_decision",
                            htf_bias=btc_htf_bias,
                            primary_htf_bias=primary_htf_bias,
                            alt_htf_bias=macro_trend,
                            btc_1h_regime=btc_1h_regime,
                        ).get("lane_id")
                        or ""
                    )
                    # Synchronous (no async enqueue/expire). 15m/1h only. FAIL-CLOSED:
                    # marginal candidates are below threshold and only trade WITH AI
                    # blessing, so no AI => skip (returns to quant baseline).
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
                        quant_threshold=_marginal_min_edge,
                        raw_probability=raw_est_prob,
                        post_calibration_probability=estimated_prob,
                        require_shadow_portfolio=False,
                        veto_only=True,
                    )
                    ai_calls += 1
                    self._log_decision_layer(
                        market=market, window=(_updown_tf if is_updown else "15m"),
                        quant_action=action, ai_decision=ai_decision, lane="marginal",
                        fail_open_reason=None if ai_decision is not None else "timeout",
                        entry_context={
                            "lane_id": ai_lane_id,
                            "yes_price": yes_price,
                            "quant_edge": edge,
                            "quant_confidence": confidence,
                            "quant_threshold": _marginal_min_edge,
                            "raw_est_prob": raw_est_prob,
                            "estimated_prob": estimated_prob,
                        },
                    )
                    if ai_decision is None:
                        _bump_skip("ai_decision_timeout_marginal_threshold")
                        _log_ai_veto("ai_decision_timeout_marginal_threshold")
                        continue
                    ai_used = True
                    ai_analysis = ai_decision.direct_analysis
                    # veto-only marginal pass: central layer already cleared this
                    # (no confident opposition) — admit on quant terms, skip the
                    # redundant local HOLD/supports/confidence/edge re-gate.
                    _mpass = ai_decision.reason == "direct_ai_marginal_pass"
                    # Log reasoning so we can audit what the model is actually deciding
                    if ai_analysis:
                        logger.info(
                            f"  {self._signal_strategy_name} AI decision [{ai_decision.action} "
                            f"conf={ai_decision.confidence:.2f} edge={float(ai_decision.edge or 0.0):.4f}] "
                            f"'{market.question[:45]}' | {ai_analysis.reasoning[:120]}"
                        )
                    if not ai_decision.approved:
                        _bump_skip(f"ai_decision_{ai_decision.reason}")
                        _log_ai_veto(f"ai_decision_{ai_decision.reason}", ai_reason=str(ai_decision.reason))
                        logger.warning(
                            "%s: AI decision rejected market %s (%s): %s",
                            _brand,
                            market.id,
                            self._signal_strategy_name,
                            ai_decision.reason,
                        )
                        continue
                    if ai_analysis is None:
                        _bump_skip("ai_none_marginal_threshold")
                        _log_ai_veto("ai_none_marginal_threshold")
                        continue
                    if not _mpass and ai_decision.action == "HOLD":
                        self._ai_hold_cache[market.id] = time.time()
                        _bump_skip("ai_hold_marginal_threshold")
                        _log_ai_veto("ai_hold_marginal_threshold")
                        logger.debug(f"{_brand}: AI says HOLD on '{market.question[:40]}...' — veto cached {self.ai_hold_veto_ttl_sec}s")
                        continue
                    if not _mpass and not ai_recommendation_supports_action(
                        ai_decision.action, action
                    ):
                        _bump_skip("ai_veto_marginal_threshold")
                        _log_ai_veto("ai_veto_marginal_threshold", ai_action=str(ai_decision.action))
                        logger.debug(
                            f"{_brand}: AI {ai_decision.action} conflicts with {action} "
                            f"on '{market.question[:40]}...'"
                        )
                        continue
                    if not _mpass and ai_decision.confidence < self.ai_confidence_threshold:
                        _bump_skip("ai_low_confidence_marginal_threshold")
                        _log_ai_veto("ai_low_confidence_marginal_threshold", ai_confidence=float(ai_decision.confidence))
                        logger.debug(
                            f"{_brand}: AI confidence {ai_decision.confidence:.2f} "
                            f"< {self.ai_confidence_threshold} marginal '{market.question[:40]}...'"
                        )
                        continue
                    ai_edge = float(ai_decision.edge or 0.0)
                    if not _mpass and ai_edge <= 0:
                        _bump_skip("ai_nonpositive_edge_marginal_threshold")
                        _log_ai_veto("ai_nonpositive_edge_marginal_threshold", ai_edge=ai_edge)
                        logger.debug(
                            f"{_brand}: non-positive ai_edge={ai_edge:.4f} marginal "
                            f"'{market.question[:40]}...'"
                        )
                        continue
                    edge = max(edge, ai_edge)
                    confidence = max(confidence, ai_decision.confidence)
                    reason_parts.append(f"ai_decision={ai_decision.source}")
                    if (
                        self.ai_agent.shadow_pipeline_enabled()
                        and shadow_pipeline_calls
                        < self.ai_agent.shadow_pipeline_max_calls_per_scan()
                            and ai_decision.confidence
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
                                marginal_recommendation=str(ai_decision.action),
                                quant_action=action,
                                quant_edge=edge,
                                quant_threshold=(
                                    _marginal_min_edge
                                ),
                                existing_research=None,
                            )
                            if shadow_out and shadow_out.get("ok"):
                                shadow_pipeline_ok += 1
                        except Exception as e:
                            logger.debug(
                                "%s shadow pipeline failed market=%s: %s",
                                self._signal_strategy_name,
                                market.id,
                                e,
                            )

            # ── Final filters (both paths) ──
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
            effective_min_edge = max(lane_policy.min_edge, lane_policy.hard_min_edge)
            if is_updown and not lane_policy.enabled:
                _bump_skip("lane_disabled")
                logger.info(
                    "  %s skip '%s...' — lane disabled side=%s window=%s",
                    self._signal_strategy_name,
                    market.question[:40],
                    lane_side,
                    _updown_tf,
                )
                continue
            if is_updown and (_eval_left < lane_policy.entry_window_min or _eval_left > lane_policy.entry_window_max):
                _bump_skip("lane_entry_window")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=allowed_side,
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
                    window=_updown_tf if is_updown else "15m",
                    side=allowed_side,
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
                logger.debug(
                    "  %s skip '%s...' — %.1fm left (eval %.2f), lane=%s needs %.2f–%.2fm",
                    self._signal_strategy_name,
                    market.question[:40],
                    _mins_left,
                    _eval_left,
                    lane_side,
                    lane_policy.entry_window_min,
                    lane_policy.entry_window_max,
                )
                continue
            # No 15m LTF confirmation: require stronger edge for 15m updown (proceeding on macro only)
            if ltf_strength == 0.0 and is_updown and _updown_tf != "5m":
                effective_min_edge = max(
                    effective_min_edge, self.min_edge_15m_when_ltf_unconfirmed
                )
            if (
                self._btc_trade_inputs_enabled()
                and self._btc_1h_regime_gates.get("enabled", False)
                and btc_ta
            ):
                effective_min_edge *= self._regime_min_edge_mult(btc_1h_regime)

            # Far from expiry → more time-stop risk; require extra min_edge (SOL paper May 2026).
            if is_updown:
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
                    _edge_addon = _rmax * min(
                        1.0, (_eval_left - _rstart) / _rspan
                    )
                    effective_min_edge += _edge_addon

                if self._btc_trade_inputs_enabled() and self.block_counter_macro_leg_updown:
                    _lm = self._signal_lag_magnitude(corr)
                    if _lm is not None:
                        _long_floor = float(
                            self.config.get("updown_macro_leg_min_for_long", 0.0)
                        )
                        # LONG + negative journaled leg = SOL not lagging a BTC impulse (catch-up thesis off).
                        # SHORT path omitted: positive lag on BTC-down is often the valid SHORT setup.
                        if allowed_side == "LONG" and _lm < _long_floor:
                            _bump_skip("macro_leg_blocks_long")
                            _lag_o = getattr(corr, "lag_opportunity", False)
                            _opp_dir = getattr(
                                corr, "opportunity_direction", None
                            )
                            _sol_lag = float(getattr(corr, "sol_lag_pct", 0.0) or 0.0)
                            _spike = getattr(corr, "btc_spike_detected", False)
                            logger.info(
                                f"  {_brand} skip '{market.question[:40]}' — "
                                f"macro_leg={_lm:+.4f}% < long_floor={_long_floor:+.4f} (updown) | "
                                f"lag_opp={_lag_o} opp_dir={_opp_dir} sol_lag_pct={_sol_lag:+.4f}% "
                                f"btc_spike={_spike}"
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
                        f"  {_brand} skip '{market.question[:40]}' — "
                        f"mins_left={_eval_left:.2f} <= late_window_block_mins={self.late_window_block_mins:.2f}"
                    )
                    continue
                if _late_reason:
                    reason_parts.append(_late_reason)

            effective_min_edge *= get_drift_min_edge_mult(
                self._signal_strategy_name, self.full_config
            )
            effective_min_edge *= get_loosen_min_edge_mult(
                self._signal_strategy_name,
                self.full_config,
                window=_updown_tf if is_updown else "15m",
                side=lane_side,
                regime=macro_trend,
            )

            if is_updown and action == "BUY_YES":
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
                    oracle_basis_bps=sol.oracle_basis_bps,
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

            # Updown marginal (parity with BTC): quant edge just below bar — AI confirms action + edge
            if (
                is_updown
                and edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and _timing_window_open
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and self.ai_agent.is_available()
                and ai_calls < self.max_ai_calls_per_scan
                # 5m never calls AI — quant only. AI tiebreaker is 15m/1h.
                and (_updown_tf if is_updown else "15m") in self._DECISION_GATE_WINDOWS
                and not self._admit_marginal_quant_short(edge, allowed_side, _timing_window_open)
            ):
                _win = _updown_tf if is_updown else "15m"
                ai_context2 = (
                    f"{market.description}\n\n"
                    f"=== {_alt_label} UPDOWN CONTEXT ({_win}) ===\n"
                    f"{_alt_label}: ${sol_price:,.2f} | YES={yes_price:.3f} | action={action} | allowed={allowed_side}\n"
                    f"Oracle={sol.chainlink_network or 'n/a'} "
                    f"{f'${sol.chainlink_price:,.2f}' if sol.chainlink_price is not None else 'n/a'} "
                    f"basis={f'{sol.oracle_basis_bps:+.1f}bps' if sol.oracle_basis_bps is not None else 'n/a'}\n"
                    f"ALT_HTF={macro_trend} | PRIMARY_ALT_HTF={primary_htf_bias} | "
                    f"Quant edge={edge:.4f} required>={effective_min_edge:.4f}\n"
                    f"15m MACD hist={sol.macd_15m.histogram:+.3f} {sol.macd_15m.crossover}\n"
                    f"LTF_strength={ltf_strength:.2f}\n\n"
                    f"=== MARKET ===\n{format_market_metadata(market)}\n\n"
                    "Answer with BUY_YES, BUY_NO, or HOLD."
                )
                ai_lane_id = str(
                    build_lane_metadata(
                        strategy=self._signal_strategy_name,
                        window_size=_win,
                        action=action,
                        direction=direction,
                        entry_leg=("NO" if action == "BUY_NO" else "YES"),
                        ai_used=True,
                        reason="ai_decision",
                        signal_reason="ai_decision",
                        htf_bias=btc_htf_bias,
                        primary_htf_bias=primary_htf_bias,
                        alt_htf_bias=macro_trend,
                        btc_1h_regime=btc_1h_regime,
                    ).get("lane_id")
                    or ""
                )
                # Synchronous (no async enqueue/expire). 15m/1h only. VETO-ONLY:
                # the HTF gate already selected this below-threshold extra; the AI
                # can only block it with a confident, directly-opposing call.
                ai_decision = await self._evaluate_trade_decision_with_timeout(
                    market_question=market.question,
                    market_description=ai_context2,
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
                    market=market, window=_win, quant_action=action,
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
                if ai_decision is None:
                    _bump_skip("ai_decision_timeout")
                    continue
                ai_used = True
                ai2 = ai_decision.direct_analysis
                if not ai_decision.approved:
                    _bump_skip(f"ai_decision_{ai_decision.reason}")
                    logger.debug(
                        f"{_brand}: AI decision rejected updown marginal "
                        f"{ai_decision.reason} action={ai_decision.action} "
                        f"conf={ai_decision.confidence:.2f}"
                    )
                    continue
                ae = float(ai_decision.edge or 0.0)
                if ae > 0:
                    edge = max(edge, ae)
                    confidence = max(confidence, ai_decision.confidence)
                    reason_parts.append(f"ai_decision={ai_decision.source}")
                    if ai2 is not None:
                        if (
                            self.ai_agent.shadow_pipeline_enabled()
                            and shadow_pipeline_calls
                            < self.ai_agent.shadow_pipeline_max_calls_per_scan()
                            and ai_decision.confidence
                            >= self.ai_agent.shadow_pipeline_min_confidence()
                        ):
                            shadow_pipeline_calls += 1
                            try:
                                shadow_out = await self.ai_agent.run_shadow_pipeline(
                                    market_question=market.question,
                                    market_description=ai_context2,
                                    current_yes_price=yes_price,
                                    market_id=market.id,
                                    strategy_hint=self._signal_strategy_name,
                                    lane_id=ai_lane_id,
                                    marginal_recommendation=str(ai_decision.action),
                                    quant_action=action,
                                    quant_edge=edge,
                                    quant_threshold=effective_min_edge,
                                    existing_research=None,
                                )
                                if shadow_out and shadow_out.get("ok"):
                                    shadow_pipeline_ok += 1
                            except Exception as e:
                                logger.debug(
                                    "%s shadow pipeline failed (updown) market=%s: %s",
                                    self._signal_strategy_name,
                                    market.id,
                                    e,
                                )
                    else:
                        _bump_skip("ai_nonpositive_edge_marginal_updown")
            elif (
                is_updown
                and edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and not _timing_window_open
            ):
                _bump_skip("ai_window_closed_marginal_updown")
                logger.debug(
                    f"{_brand}: AI window closed for marginal updown '{market.question[:40]}...' "
                    f"({_mins_left:.1f}m left)"
                )
                continue

            try:
                _sample("est_prob_up", est_prob_up)
            except NameError:
                pass
            _sample("edge", edge)
            # 2026-06-02: when the AI decision layer is OFF, sub-threshold marginal
            # SHORTs are admitted on quant terms (see _admit_marginal_quant_short) —
            # the composite scorer is the quality gate instead of an auto lane_min_edge
            # reject. The two upstream AI tiebreaker blocks are also skipped for these.
            _admit_marginal_no_ai = self._admit_marginal_quant_short(
                edge, allowed_side, _timing_window_open
            )
            if edge < effective_min_edge and not _admit_marginal_no_ai:
                if rsi_soft_penalty > 0 and (edge + rsi_soft_penalty) >= effective_min_edge:
                    _bump_skip("edge_after_penalty_below_threshold")
                _vetoed = bool(getattr(self, "_last_calibration_vetoed", False))
                _reject_reason = "beta_vetoed" if _vetoed else "lane_min_edge"
                _bump_skip(_reject_reason)
                log_rejected_candidate(
                    strategy=self._signal_strategy_name,
                    window=_updown_tf if is_updown else "15m",
                    side=allowed_side,
                    action=action,
                    reason=_reject_reason,
                    market=market,
                    yes_price=yes_price,
                    est_prob_up=estimated_prob,
                    htf_bias=primary_htf_bias,
                    stage=_reject_reason,
                    context={
                        "edge": round(float(edge), 6),
                        "effective_min_edge": round(float(effective_min_edge), 6),
                        "raw_est_prob": round(float(raw_est_prob), 6),
                        "estimated_prob": round(float(estimated_prob), 6),
                        "confidence": round(float(confidence), 6),
                        "side_source": side_source,
                        "rsi_soft_penalty": round(float(rsi_soft_penalty), 6),
                        "beta_vetoed": _vetoed,
                        "calibration_lane_id": getattr(self, "_last_calibration_lane_id", ""),
                        **build_market_context(
                            asset_spot=sol.current_price,
                            btc_spot=corr.btc_price,
                            rsi_14=sol.rsi_14,
                            atr_14=sol.atr_14,
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
                            window_size=_updown_tf if is_updown else "threshold",
                            yes_price=yes_price,
                            edge=edge,
                            effective_min_edge=effective_min_edge,
                            rsi=sol.rsi_14,
                            htf_bias=primary_htf_bias,
                            signal_reason=" | ".join(r for r in reason_parts if r),
                            alt_1h_trend=mtt.h1_trend,
                        ),
                        counts=buy_no_skip_counts,
                        last_sample=last_buy_no_skip_sample,
                    )
                if not is_updown:
                    _mkt_type = "threshold"
                elif ltf_strength == 0.0:
                    _mkt_type = f"{_updown_tf}_unconf"
                else:
                    _mkt_type = _updown_tf
                logger.info(
                    f"  {_brand} skip '{market.question[:40]}...' edge={edge:.4f} < min={effective_min_edge} ({_mkt_type})"
                )
                continue

            entry_convergence_score = None
            entry_composite_score = None
            _updown_lane = "default"
            if is_updown:
                _tf_alignment = 1.0 if mtt.aligned else (0.70 if mtt.h1_trend == macro_trend else 0.35)
                composite = self._score_updown_candidate(
                    edge=edge,
                    effective_min_edge=effective_min_edge,
                    confidence=confidence,
                    ltf_strength=ltf_strength,
                    timeframe_alignment=_tf_alignment,
                    oracle=oracle_validation,
                    minutes_left=_eval_left,
                    yes_price=yes_price,
                    lane=_updown_lane,
                    action=action,
                    btc_1h_regime=btc_1h_regime,
                )
                _sample("composite_score", composite.score)
                entry_convergence_score = composite.convergence_score
                entry_composite_score = composite.score
                reason_parts.append(f"composite={composite.score:.3f}")
                if not composite.passed:
                    _bump_skip(composite.reason)
                    logger.info(
                        "%s composite skip '%s...' lane=%s action=%s edge=%.4f "
                        "conf=%.2f score=%.3f floor=%.3f components=%s "
                        "oracle_basis=%s oracle_fresh=%s",
                        self._signal_strategy_name,
                        market.question[:45],
                        _updown_lane,
                        action,
                        edge,
                        confidence,
                        composite.score,
                        composite.floor,
                        composite.components,
                        oracle_validation.basis_bps,
                        oracle_validation.freshness_sec,
                    )
                    continue

                _win = _updown_tf if is_updown else "15m"
                if (
                    self._requires_ai_for_lane(_updown_lane)
                    and not ai_used
                    # 5m bypasses the AI gate entirely (latency >> entry window).
                    and _win in self._DECISION_GATE_WINDOWS
                ):
                    # Synchronous, FAIL-OPEN gate. AI off / unavailable / over budget
                    # / timed out => take the quant trade. The gate can only ever
                    # REJECT on a real verdict, never drop a trade to latency.
                    if not self.config.get("use_ai", True) or not self.ai_agent.is_available():
                        self._log_decision_layer(
                            market=market, window=_win, quant_action=action,
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
                            market=market, window=_win, quant_action=action,
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
                        ai_context3 = (
                            f"{market.description}\n\n"
                            f"=== {_brand} ENFORCED UPDOWN CONTEXT ({_win}) ===\n"
                            f"Action={action} YES={yes_price:.3f} edge={edge:.4f} "
                            f"confidence={confidence:.2f} composite={composite.score:.3f}/{composite.floor:.3f}\n"
                            f"Oracle price={oracle_validation.oracle_price if oracle_validation.oracle_price is not None else 'n/a'} "
                            f"exchange_spot={oracle_validation.exchange_spot if oracle_validation.exchange_spot is not None else 'n/a'} "
                            f"basis_bps={oracle_validation.basis_bps if oracle_validation.basis_bps is not None else 'n/a'} "
                            f"freshness_sec={oracle_validation.freshness_sec if oracle_validation.freshness_sec is not None else 'n/a'}\n"
                            f"Components={composite.components}\n"
                            f"ALT_HTF={macro_trend} PRIMARY_ALT_HTF={primary_htf_bias} "
                            f"LTF_strength={ltf_strength:.2f}\n\n"
                            f"=== MARKET ===\n{format_market_metadata(market)}\n\n"
                            "Answer with BUY_YES, BUY_NO, or HOLD."
                        )
                        ai_lane_id = str(
                            build_lane_metadata(
                                strategy=self._signal_strategy_name,
                                window_size=_win,
                                action=action,
                                direction=direction,
                                entry_leg=("NO" if action == "BUY_NO" else "YES"),
                                side_source=_updown_lane,
                                ai_used=True,
                                reason="ai_decision",
                                signal_reason=f"ai_decision_{_updown_lane}",
                                htf_bias=btc_htf_bias,
                                primary_htf_bias=primary_htf_bias,
                                alt_htf_bias=macro_trend,
                                btc_1h_regime=btc_1h_regime,
                            ).get("lane_id")
                            or ""
                        )
                        # Direct synchronous call with a bounded timeout — no async
                        # enqueue/expire broker (that path silently dropped trades).
                        ai_decision = await self._evaluate_trade_decision_with_timeout(
                            market_question=market.question,
                            market_description=ai_context3,
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
                            require_shadow_portfolio=self._requires_shadow_for_lane(_updown_lane),
                        )
                        ai_calls += 1
                        if ai_decision is None:
                            # FAIL-OPEN on timeout — do NOT drop the trade.
                            self._log_decision_layer(
                                market=market, window=_win, quant_action=action,
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
                                market=market, window=_win, quant_action=action,
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
                                logger.info(
                                    "%s AI-enforced lane rejected '%s...' lane=%s reason=%s "
                                    "action=%s conf=%.2f edge=%s composite=%.3f",
                                    self._signal_strategy_name,
                                    market.question[:45],
                                    _updown_lane,
                                    ai_decision.reason,
                                    ai_decision.action,
                                    ai_decision.confidence,
                                    ai_decision.edge,
                                    composite.score,
                                )
                                continue
                            ai_edge = float(ai_decision.edge or 0.0)
                            if ai_edge <= 0:
                                _bump_skip(f"ai_nonpositive_edge_{_updown_lane}")
                                continue
                            edge = max(edge, ai_edge)
                            confidence = max(confidence, ai_decision.confidence)
                            reason_parts.append(f"ai_decision={ai_decision.source}")

            if is_updown and self.center_price_band > 0:
                _is_centered = abs(yes_price - 0.50) <= self.center_price_band
                if _is_centered:
                    if (
                        self._btc_trade_inputs_enabled()
                        and
                        self.center_price_requires_catalyst
                        and not corr.lag_opportunity
                        and not corr.btc_spike_detected
                    ):
                        _bump_skip("centered_price_no_catalyst")
                        logger.info(
                            f"  {_brand} skip '{market.question[:40]}...' centered YES={yes_price:.3f} "
                            f"without catalyst (spike={corr.btc_spike_detected}, lag={corr.lag_opportunity})"
                        )
                        continue
                    _center_min_edge = max(effective_min_edge, self.min_edge_when_centered)
                    if edge < _center_min_edge:
                        _bump_skip("centered_price_edge_below_min")
                        logger.info(
                            f"  {_brand} skip '{market.question[:40]}...' centered YES={yes_price:.3f} "
                            f"edge={edge:.4f} < centered_min={_center_min_edge:.4f}"
                        )
                        continue

            # ── Entry price filter for updown markets ──
            # Only trade when yes_price is within [entry_price_min, entry_price_max].
            # This band prevents entering when the market has already moved:
            #   - BUY_YES at yes_price > max: market too bullish, lag already priced in
            #   - BUY_YES at yes_price < min: market too bearish, going long against consensus
            #   - BUY_NO at yes_price < min: bearish YES consensus → NO too expensive
            #   - BUY_NO: allow yes_price > max (YES rich → cheap NO)
            #
            # Live data (2026-04-24 session, 29 trades):
            #   market YES in [0.46, 0.54] → 72% WR  (sweet spot)
            #   market YES < 0.46 or > 0.54 → ~30% WR (market consensus fighting signal)
            if is_updown:
                _yp_low = lane_policy.entry_price_min
                _yp_high = lane_policy.entry_price_max
                if action == "BUY_YES":
                    _updown_band_bad = yes_price < _yp_low or yes_price > _yp_high
                elif action == "BUY_NO":
                    # Floor the NO price: shorting cheap NO (yes_price rich) is
                    # adverse selection — NO<0.20 wins ~5% held-to-resolution
                    # across every asset (n~8k ghost), −$97 realized. Block it.
                    _buy_no_min_no = float(self.config.get("buy_no_min_no_price", 0.20))
                    _updown_band_bad = (
                        yes_price < _yp_low or yes_price > (1.0 - _buy_no_min_no)
                    )
                else:
                    _updown_band_bad = yes_price < _yp_low or yes_price > _yp_high
                if _updown_band_bad:
                    _bump_skip("lane_price_band")
                    if action == "BUY_NO":
                        self._emit_buy_no_skip(
                            market=market,
                            bankroll=bankroll,
                            payload=self._make_buy_no_skip_payload(
                                market=market,
                                skip_reason="lane_price_band",
                                window_size=_updown_tf if is_updown else "15m",
                                yes_price=yes_price,
                                edge=edge,
                                effective_min_edge=effective_min_edge,
                                rsi=sol.rsi_14,
                                htf_bias=primary_htf_bias,
                                signal_reason=" | ".join(r for r in reason_parts if r),
                                alt_1h_trend=mtt.h1_trend,
                                ghost_blind=True,
                            ),
                            counts=buy_no_skip_counts,
                            last_sample=last_buy_no_skip_sample,
                        )
                    logger.info(
                        f"  {self._signal_strategy_name} skip '{market.question[:40]}...' "
                        f"yes_price={yes_price:.3f} outside [{_yp_low:.3f}, {_yp_high:.3f}] "
                        f"({action}, market already moved, signal has no edge)"
                    )
                    continue

            # ── Edge cap for updown markets ──
            # Keep high-edge trades admissible and clamp only the Kelly sizing input.
            sizing_edge = edge
            if is_updown:
                _max_edge_updown = self.config.get("max_edge_updown", 0.09)
                if _max_edge_updown > 0 and edge > _max_edge_updown:
                    sizing_edge = float(_max_edge_updown)
                    reason_parts.append(f"size_edge_cap={float(_max_edge_updown):.3f}")
                    logger.info(
                        f"  {_brand} sizing cap '{market.question[:40]}...' edge={edge:.4f} "
                        f"-> size_edge={sizing_edge:.4f} (max={_max_edge_updown})"
                    )

            # Position sizing
            if not self.kelly_sizer:
                _bump_skip("kelly_unavailable")
                logger.error("%s strategy: KellySizer unavailable — skipping entry sizing", _brand)
                continue
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
            if final_size < 0.5:
                _bump_skip("lane_size_too_small")
                if action == "BUY_NO":
                    self._emit_buy_no_skip(
                        market=market,
                        bankroll=bankroll,
                        payload=self._make_buy_no_skip_payload(
                            market=market,
                            skip_reason="lane_size_too_small",
                            window_size=_updown_tf if is_updown else "threshold",
                            yes_price=yes_price,
                            edge=edge,
                            effective_min_edge=effective_min_edge,
                            rsi=sol.rsi_14,
                            htf_bias=primary_htf_bias,
                            signal_reason=" | ".join(r for r in reason_parts if r),
                            ghost_blind=True,
                            alt_1h_trend=mtt.h1_trend,
                        ),
                        counts=buy_no_skip_counts,
                        last_sample=last_buy_no_skip_sample,
                    )
                continue
            reason_parts.append(f"exp={exp_tier.value}(x{exp_multiplier:.1f})")
            if lane_policy.size_multiplier > 0 and lane_policy.size_multiplier < 0.999:
                reason_parts.append(f"lane_size={lane_policy.size_multiplier:.2f}x")

            reason_str = " | ".join(r for r in reason_parts if r)
            signal_side_source = side_source if "side_source" in locals() else "neutral_macro"
            resolver_meta = build_alt_resolver_metadata(
                side_source=signal_side_source,
                htf_side=allowed_side,
                quant_side=side_from_est_prob_up(raw_est_prob),
                momentum_side=side_from_momentum_bias(getattr(mtt, "m5_trend", None)),
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
                sol_threshold=self._extract_price_threshold(market.question) if not is_updown else None,
                sol_current=round(sol_price, 2),
                btc_current=round(corr.btc_price, 2) if corr.btc_price else None,
                lag_magnitude=self._signal_lag_magnitude(corr),
                ai_used=ai_used,
                reason=reason_str,
                strategy_name=self._signal_strategy_name,
                alt_asset_code=_spot_key,
                htf_bias=primary_htf_bias,
                primary_htf_bias=primary_htf_bias,
                alt_htf_bias=mtt.h1_trend,
                btc_htf_bias=btc_htf_bias,
                btc_1h_regime=btc_1h_regime if btc_ta else None,
                window_size=_updown_tf if is_updown else "15m",
                hour_utc=datetime.now(timezone.utc).hour,
                est_prob=round(estimated_prob, 4),
                raw_est_prob=round(raw_est_prob, 4),
                rsi=round(sol.rsi_14, 1),
                corr_1h=round(corr.correlation_1h, 4),
                side_source=signal_side_source,
                **resolver_meta,
                oracle_basis_bps=(
                    round(float(sol.oracle_basis_bps), 2)
                    if sol.oracle_basis_bps is not None
                    else None
                ),
                convergence_score=(
                    round(float(entry_convergence_score), 4)
                    if entry_convergence_score is not None
                    else None
                ),
                entry_volatility=round(float(getattr(conditions, "volatility", 0.0) or 0.0), 6),
                entry_policy=entry_policy_meta,
                indicator_snapshot=self._build_alt_indicator_snapshot(
                    sol,
                    correlation=corr,
                    composite_score=entry_composite_score,
                    convergence_score=entry_convergence_score,
                    entry_volatility=getattr(conditions, "volatility", 0.0),
                ),
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
                        "htf_bias": primary_htf_bias,
                        "alt_htf_bias": macro_trend,
                        "reason": reason_str,
                    },
                )
            signals.append(signal)

            logger.info(
                f"  {_brand} SIGNAL: {action} '{market.question[:50]}...' "
                f"edge={edge:.3f} prob={estimated_prob:.2f} "
                f"size=${final_size:.2f} conf={confidence:.2f}"
            )

        if observer_tasks:
            await asyncio.wait(observer_tasks, timeout=0.01)

        gate_distributions = {k: _summarize(v) for k, v in gate_samples.items()}
        if gate_samples:
            logger.info(f"  [gate-dist] {gate_distributions}")
        _skip_top = dict(sorted(skip_reasons.items(), key=lambda kv: kv[1], reverse=True)[:6])
        logger.info(
            f"{_brand} SCAN_DIAG side={locals().get('allowed_side')} source={locals().get('side_source', 'neutral_macro')} "
            f"ALT_HTF={macro_trend} PRIMARY_ALT_HTF={locals().get('primary_htf_bias', 'NEUTRAL')} "
            f"alt_1H_trend={mtt.h1_trend} enforce_alt_1h={self.enforce_alt_1h_alignment} "
            f"skip_15m={locals().get('skip_15m_reason')!s} markets={len(sol_markets)} signals={len(signals)} "
            f"skips_top6={_skip_top}"
        )
        self.last_scan_stats = {
            "enabled": True,
            "signals": len(signals),
            "markets_considered": len(sol_markets),
            "btc_1h_regime": btc_1h_regime,
            "btc_1h_regime_gates_enabled": bool(
                self._btc_1h_regime_gates.get("enabled", False)
            ),
            "btc_htf_bias": btc_htf_bias,
            "primary_htf_bias": locals().get("primary_htf_bias", "NEUTRAL"),
            "alt_htf_bias": macro_trend,
            "allowed_side": locals().get("allowed_side"),
            "action_counts": dict(sorted(action_counts.items())),
            "side_source_counts": dict(sorted(side_source_counts.items())),
            "alt_1h_trend": mtt.h1_trend,
            "enforce_alt_1h_alignment": self.enforce_alt_1h_alignment,
            "skip_15m_gate": locals().get("skip_15m_reason"),
            "ai_calls": ai_calls,
            "shadow_pipeline_calls": shadow_pipeline_calls,
            "shadow_pipeline_ok": shadow_pipeline_ok,
            "shadow_observer_calls": shadow_observer_calls,
            "shadow_observer_ok": shadow_observer_ok,
            "buy_no_skip_counts": dict(sorted(buy_no_skip_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]),
            "last_buy_no_skip_sample": dict(last_buy_no_skip_sample),
            "top_skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: kv[1], reverse=True)[:8]),
            "gate_distributions": gate_distributions,
        }
        return signals


def _get_weekend_penalty() -> float:
    """Return weekend penalty multiplier (1.0=normal, lower=tighter max size).

    Reduces position size during weekend / low-liquidity periods when
    HYPE-style manipulation (a4385 CEX pump) is most likely to occur.
    Kept in sync with ``exposure_manager._get_weekend_penalty``.
    """
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()  # 0=Mon … 5=Sat, 6=Sun
    utc_hour = now_utc.hour

    # Weekend (Sat/Sun full UTC days) — softer than 0.50 so MINIMAL tier keeps workable size
    if weekday >= 5:  # Saturday = 5, Sunday = 6
        return 0.65

    # Friday evening UTC — lighter touch than full weekend (still below 1.0)
    if weekday == 4 and utc_hour >= 20:
        return 0.85

    return 1.0
