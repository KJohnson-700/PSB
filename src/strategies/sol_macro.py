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

from src.market.scanner import Market, is_tradably_priced, resolved_updown_window_minutes, updown_timeframe_label
from src.analysis.ai_agent import AIAgent
from src.analysis.ai_decision_broker import (
    PendingDecision as _BrokerPendingDecision,
    STATE_PENDING as _BROKER_STATE_PENDING,
)
from src.analysis.btc_price_service import BTCPriceService, TechnicalAnalysis
from src.analysis.math_utils import PositionSizer
from src.analysis.window_watch import log_window_reject
from src.strategies._scan_timeout import analysis_with_timeout
from src.strategies.fee_util import fee_aware_edge_hurdle
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
from src.analysis.lane_tape_adapter import get_tape_admission_delta
from src.analysis.favorite_net_tracker import get_favorite_net
from src.analysis.tape_map import (
    snapshot_and_log as _tape_map_snapshot,
    latest_tape_state as _latest_tape_state,
    log_side_veto_shadow as _log_side_veto_shadow,
)
from src.analysis.tape_freshness import compute_freshness_penalty
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
from src.analysis import no_signal_gate as _no_signal_gate
from src.analysis import asset_regime as _asset_regime
from src.analysis.window_delta import (
    delta_confirms_side,
    evaluate_window_delta,
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
    condition_id: Optional[str] = Field(None, description="Polymarket conditionId")
    market_slug: Optional[str] = Field(None, description="Polymarket market slug")
    outcome_label_yes: Optional[str] = Field(None, description="Label for the first CLOB outcome token")
    outcome_label_no: Optional[str] = Field(None, description="Label for the second CLOB outcome token")
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
        # BTC analysis is DIAGNOSTIC-ONLY for alts (they're decided by alt-native
        # indicators; _btc_trade_inputs_enabled() is False). main.py computes BTC
        # once per cycle and injects it here so the 6 alt/eth lanes don't each run a
        # full redundant BTC get_full_analysis just to log it. When not injected
        # (e.g. tests), the scan falls back to fetching its own.
        self._injected_btc_ta: Optional[TechnicalAnalysis] = None
        self._btc_ta_inject_set: bool = False
        self.exposure_manager = exposure_manager or ExposureManager(config)
        if self.exposure_manager:
            self.exposure_manager._on_pause_ai_callback = self._ai_kill_switch_analysis
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

    def _marginal_ev_admit_ok(self, window, allowed_side) -> bool:
        """2026-06-09: per-(window,side) EV allowlist for marginal admission, derived
        from the settled ghost (realized_pct by lane). This REPLACES the in-scan AI
        tiebreaker as the marginal-admission decider for lanes the ghost shows +EV —
        the AI was anti-selective (HOLD machine, ~18% rec accuracy) and the gated
        marginals would have won 54% / +0.068 EV overall, +0.122 on the long side.
        We admit only the +EV (strategy,window,side) cells and keep blocking the −EV
        ones (e.g. xrp 1h LONG −0.80). Config: `marginal_ev_admit_lanes` = list of
        "<window>:<SIDE>" (e.g. "1h:LONG"). Self-disables if the AI decision layer is
        re-enabled."""
        try:
            if self.ai_agent.decision_layer_enabled():
                return False
            lanes = self.config.get("marginal_ev_admit_lanes") or []
            key = f"{str(window).strip()}:{str(allowed_side).strip()}".upper()
            return key in {str(x).strip().upper() for x in lanes}
        except Exception:
            return False

    def _admit_marginal_quant_short(self, edge, allowed_side, timing_open, window=None) -> bool:
        """When the AI decision layer is OFF, admit sub-threshold marginal candidates on
        quant terms instead of letting them die on the AI tiebreaker (no-op when the
        layer is enabled) or the final lane_min_edge gate. Default OFF, timing-open,
        edge above the marginal floor. Self-disables when the decision layer is
        re-enabled.

        Two admit paths (both require timing-open + edge >= marginal floor):
          1. EV allowlist (`marginal_ev_admit_lanes`, 2026-06-09) — per-(window,side)
             ghost-validated +EV cells. The real decider; see _marginal_ev_admit_ok.
          2. Legacy scope (`admit_marginal_on_quant_sides`: SHORT|LONG|BOTH) — broad
             side-scoped admit kept for back-compat / lanes not in the EV allowlist."""
        try:
            if not bool(timing_open):
                return False
            if float(edge) < float(self.config.get("ai_updown_marginal_min_edge", 0.03)):
                return False
            # 2026-07-08 R1 (operator+Codex GO-cond): per-lane HARD allowlist that takes
            # precedence over the AI tiebreaker - lanes in marginal_ev_admit_lanes_override_ai
            # admit the marginal band on quant terms even while up/down AI is ON for the
            # strategy. Everything NOT listed keeps the 2026-07-03 yield-to-AI behavior.
            # Composite scorer remains the downstream quality gate. Shipped: xrp 15m LONG.
            _ovr_lanes = self.config.get("marginal_ev_admit_lanes_override_ai") or []
            if (
                window is not None
                and _ovr_lanes
                and not self.ai_agent.decision_layer_enabled()
            ):
                _ovr_key = f"{str(window).strip()}:{str(allowed_side).strip()}".upper()
                if _ovr_key in {str(x).strip().upper() for x in _ovr_lanes}:
                    return True
            # 2026-07-03 AI-SHADOW FIX (Codex): when up/down AI is ON+available this
            # helper must YIELD the marginal [0.03,min_edge) band to the AI tiebreaker.
            # Previously Path 1 (EV allowlist) admitted regardless, so the alt AI gate
            # (which is `... and not _admit_marginal_quant_short(...)`) never saw the
            # band -> alt ai_calls=0. Now it truly means "no-AI marginal admit".
            if (
                bool(self.config.get("use_ai", True))
                and bool(self.config.get("use_ai_updown", True))
                and self.ai_agent.is_available()
            ):
                return False
            # Path 1: ghost-derived per-lane EV allowlist (preferred).
            if window is not None and self._marginal_ev_admit_ok(window, allowed_side):
                return True
            # Path 2: legacy broad side-scope (only when AI layer is OFF).
            if not bool(self.config.get("admit_marginal_on_quant_when_ai_disabled", False)):
                return False
            if self.ai_agent.decision_layer_enabled():
                return False
            scope = str(self.config.get("admit_marginal_on_quant_sides", "SHORT")).upper()
            side = str(allowed_side).upper()
            if scope != "BOTH" and side != scope:
                return False
            return True
        except Exception:
            return False

    def _ai_override_min_edge_for_window(self, window_size: str) -> float:
        return float(
            self._tf_cfg(window_size, "ai_override_min_edge", self.min_edge_5m_ai_override)
        )

    def _admission_prob(self, est_prob: float, *, window_size: str, action: str) -> float:
        """Lane-scoped admission-only probability shrink.

        Sizing/logging/side selection keep the raw probability; this is only for
        min_edge admission edge, mirroring BTC without applying a blanket alt shrink.
        """
        lane = "down" if str(action).strip().upper() == "BUY_NO" else "up"
        strategy_cfg: Dict[str, Any] = {}
        try:
            exit_rules = (self.full_config.get("trading", {}) or {}).get("exit_rules", {}) or {}
            overrides = exit_rules.get("updown_overrides", {}) or {}
            raw_strategy_cfg = overrides.get(self._signal_strategy_name, {}) or {}
            if isinstance(raw_strategy_cfg, dict):
                strategy_cfg = raw_strategy_cfg
        except AttributeError:
            strategy_cfg = {}

        raw_shrink = strategy_cfg.get(
            "entry_admission_calibration_shrink",
            self.config.get("entry_admission_calibration_shrink", 1.0),
        )
        window_lane_overrides = strategy_cfg.get("window_lane_overrides", {})
        if isinstance(window_lane_overrides, dict):
            window_cfg = window_lane_overrides.get(str(window_size), {})
            if isinstance(window_cfg, dict):
                lane_cfg = window_cfg.get(lane, {})
                if (
                    isinstance(lane_cfg, dict)
                    and "entry_admission_calibration_shrink" in lane_cfg
                ):
                    raw_shrink = lane_cfg["entry_admission_calibration_shrink"]

        try:
            shrink = float(raw_shrink)
        except (TypeError, ValueError):
            shrink = 1.0
        if shrink >= 1.0:
            return float(est_prob)
        return 0.5 + shrink * (float(est_prob) - 0.5)

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
    # Alt live-entry AI is intentionally disabled. BTC owns the 15m/1h
    # evaluate_trade_decision path; alt AI is reserved for observer/tuning and
    # self-healing surfaces.
    _DECISION_GATE_WINDOWS = frozenset()  # 2026-07-30 REVERT of the 2026-07-03 alt-AI extension: alt decision-layer AI is OFF again (BTC-only owns the 15m/1h evaluate_trade_decision path, per the comment above). Empty set => every alt AI-gate site (`_updown_tf in self._DECISION_GATE_WINDOWS`) is False, so alt AI never fires. Guarded by test_alt_macro_ai_gate_removed.

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
        # ── Per-asset edge-math constants (2026-06-15) ─────────────────────
        # These RSI/ATR bands and confirmation cutoffs were historically bare
        # literals in shared base methods (_estimate_probability, _vote_rsi_bias,
        # _check_macd_confirmation), so every alt (sol/xrp/hype/bnb/doge) used
        # SOL-tuned values regardless of its own RSI/ATR scale. Exposed per-asset
        # here; defaults == legacy SOL values, so omitting the keys is a no-op.
        # Calibrate from the settled ghost log per asset — do not guess.
        ep = self.config.get("est_prob")
        ep = ep if isinstance(ep, dict) else {}
        self.ep_rsi_ob_strong = float(ep.get("rsi_overbought_strong", 75.0))   # UP: strong overbought headwind
        self.ep_rsi_ob_mild = float(ep.get("rsi_overbought_mild", 65.0))       # UP: mild overbought headwind
        self.ep_rsi_os_bounce = float(ep.get("rsi_oversold_bounce", 30.0))     # UP: oversold-bounce tailwind
        self.ep_rsi_os_strong = float(ep.get("rsi_oversold_strong", 25.0))     # DOWN: strong oversold headwind
        self.ep_rsi_os_mild = float(ep.get("rsi_oversold_mild", 35.0))         # DOWN: mild oversold headwind
        self.ep_rsi_ob_crash = float(ep.get("rsi_overbought_crash", 70.0))     # DOWN: overbought-crash tailwind
        self.ep_atr_high_pct = float(ep.get("atr_high_pct", 0.03))             # ATR% above = "high vol"
        self.ep_atr_low_pct = float(ep.get("atr_low_pct", 0.01))               # ATR% below = "low vol"
        # UP-side RSI adjustment magnitudes (probability points). Default = mean-revert
        # (overbought penalty / oversold boost). Momentum assets invert: ghost P(up)
        # RISES with RSI (HYPE: >75 P(up)=0.56-0.64, <30 P(up)=0.46). Per-asset; fit
        # from settled ghost log (scripts/fit_hype_rsi_momentum.py) — do not guess.
        self.ep_rsi_adj_up_ob_strong = float(ep.get("rsi_adj_up_overbought_strong", -0.06))
        self.ep_rsi_adj_up_ob_mild = float(ep.get("rsi_adj_up_overbought_mild", -0.02))
        self.ep_rsi_adj_up_os_bounce = float(ep.get("rsi_adj_up_oversold_bounce", 0.04))
        # RSI→bias vote cutoffs (was hardcoded 55/45, shared across all alts).
        self.bias_rsi_bull = float(self.config.get("bias_rsi_bull", 55.0))
        self.bias_rsi_bear = float(self.config.get("bias_rsi_bear", 45.0))
        # LTF MACD confirmation strength gate (was hardcoded 0.50, SOL-tuned).
        self.ltf_confirm_strength_min = float(self.config.get("ltf_confirm_strength_min", 0.50))
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
        # ── [1h] simple consensus-follow BUY_YES lane (DEFAULT OFF; per-alt) ──
        # Mirrors BTC's bitcoin_1h_simple_long as a price-band admission filter for
        # alt-native LONG signals. It must not choose side; alts keep direction from
        # _resolve_alt_bias_for_tf so bearish tape can short or sit out instead of
        # being force-flipped into BUY_YES.
        _a1hsl = self.config.get("alt_1h_simple_long", {}) or {}
        self._a1hsl_enabled = bool(_a1hsl.get("enabled", False))
        self._a1hsl_entry_min = float(_a1hsl.get("entry_min", 0.50) or 0.50)
        self._a1hsl_entry_max = float(_a1hsl.get("entry_max", 0.85) or 0.85)
        self._a1hsl_sizing_edge = float(_a1hsl.get("sizing_edge", 0.06) or 0.06)
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
        # 2026-06-07: surgical split from enforce_alt_1h_alignment. This flag gates ONLY
        # the HARD 5m-long veto (_alt_1h_alignment_blocks_entry); the soft h1_dampen
        # est_prob nudges stay under enforce_alt_1h_alignment. Default True preserves
        # behavior for all inheriting alts; set False per-asset (eth/sol) to un-starve
        # 5m longs the lagging 1h-bearish label was killing (ghost: ETH +52.7% EV/82% win).
        self.alt_1h_hard_block_5m_longs = bool(
            self.config.get("alt_1h_hard_block_5m_longs", True)
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
        # 2026-07-05 sol/bnb; 2026-07-29 (Codex) +xrp/doge — the 5m short-into-bull leak
        # (xrp_5m_vs_slower, sol_5m_native) was unguarded on xrp/doge and, for native
        # sources, on sol/bnb too. Config-scoped so scope can be narrowed/reverted without
        # a code edit.
        _guard_strats = set(
            self.config.get(
                "short_the_bull_5m_guard_strategies",
                ["sol_macro", "bnb_macro", "xrp_macro", "doge_macro"],
            )
            or []
        )
        if str(self._signal_strategy_name or "") not in _guard_strats:
            return None
        if action != "BUY_NO":
            return None

        source = str(side_source or "")
        regime = str(btc_1h_regime or "").upper()
        alt_h1 = str(alt_h1_trend or "").upper()

        # NOTE: neutral_fallback is now sat out at the source in
        # _resolve_alt_bias_for_tf (alt_neutral_fallback_sit_out), so it never
        # reaches this guard — both BUY_YES and BUY_NO are covered there.

        # 2026-07-29 (Codex): block 5m BUY_NO into a BULLISH OWN-1h for BOTH the vs-slower
        # AND native side-source (sol_5m_native / *_5m_vs_slower were run over by the bull).
        # NARROW by design — 5m only, own-1h BULLISH only; 15m/1h shorts and non-bull
        # regimes are untouched (paper shows some longer-tf bull-shorts win). Opt-out:
        # short_the_bull_5m_guard_enabled: false.
        _5m_native_or_vs = source.endswith("_vs_slower") or "5m_native" in source
        if (
            window_size == "5m"
            and _5m_native_or_vs
            and alt_h1 == "BULLISH"
            and bool(self.config.get("short_the_bull_5m_guard_enabled", True))
        ):
            _tag = "vs_slower" if source.endswith("_vs_slower") else "native"
            # Reason string kept backward-compatible ('<strat>_vs_slower_short_against_h1')
            # so existing tests/log consumers still match; native adds '<strat>_native_...'.
            return f"{str(self._signal_strategy_name or '').replace('_macro','')}_{_tag}_short_against_h1"

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

    def _bias_to_side(self, bias: str, tf: Optional[str] = None, aligned: bool = False) -> Optional[str]:
        # Primary regime->side mapping used by _resolve_alt_bias_for_tf (the live
        # side-decision path for all alts + ETH). COUNTER-REGIME (fade) mode —
        # 2026-06-22, default OFF: OOS ghost (347k settled, both time-halves) showed
        # WITH-regime/momentum edge -1.5%/-2.5% vs COUNTER-regime/fade +2.7%/+3.7%.
        # When config fade_regime=true, invert so the strategy fades the regime.
        # Fail-safe: missing/false flag => byte-identical momentum behaviour.
        if bias == "BULLISH":
            base = "LONG"
        elif bias == "BEARISH":
            base = "SHORT"
        else:
            # NEUTRAL: quant sits out. The direction-override seam may still supply a
            # side (ONLY when direction.override_when_quant_neutral). Default => None.
            return self._apply_direction_override(None, tf)
        # 2026-06-27 per-lane fade: fade ONLY on enabled windows (fade_regime_windows)
        # AND when the timeframes DISAGREE (not a fully-aligned trend). The fade edge is
        # per alt x window (5m universal +EV; xrp/hype 1h +EV; bnb/doge 1h -EV) and lives
        # in chop/disagreement — fading a fully-aligned strong trend gets run over (the
        # live sol 5m fully-aligned losses). aligned=True suppresses the fade.
        if self._fade_for_window(tf) and not aligned:
            quant_side = "SHORT" if base == "LONG" else "LONG"
        else:
            quant_side = base
        # Direction-override seam (default quant; SHADOW unless direction.enforce=true).
        return self._apply_direction_override(quant_side, tf)

    def _apply_direction_override(self, quant_side: Optional[str], tf: Optional[str]) -> Optional[str]:
        """Route the quant-resolved side through the direction-override seam. Fail-safe:
        any problem => the quant side, unchanged. Default config (mode=quant) is a no-op."""
        try:
            from src.analysis import direction_override as _dir_override
            return _dir_override.resolve(self._signal_strategy_name, tf, quant_side, self.full_config)
        except Exception:
            return quant_side

    def _fade_for_window(self, tf: Optional[str]) -> bool:
        """Per-lane fade enable. Master flag fade_regime + optional fade_regime_windows
        list. windows missing => fade ALL windows (legacy global behaviour); else fade
        only when tf is in the list. Fail-safe: flag false / unknown tf => no fade."""
        if not bool(self.config.get("fade_regime", False)):
            return False
        windows = self.config.get("fade_regime_windows")
        if windows is None:
            return True
        try:
            return str(tf) in {str(w) for w in windows}
        except TypeError:
            return False

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

    def _vote_rsi_bias(self, rsi: float) -> str:
        if rsi >= self.bias_rsi_bull:
            return "BULLISH"
        if rsi <= self.bias_rsi_bear:
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
            # 2026-06-27 align-gate: a fully-aligned trend (every DECIDED slower TF agrees
            # with this horizon) does not mean-revert, so fading it gets run over. Compute
            # alignment up front so _bias_to_side can suppress the fade when aligned.
            _slower_decided = [b for b in slower_biases.values() if b in {"BULLISH", "BEARISH"}]
            _fully_aligned = bool(_slower_decided) and all(b == horizon_bias for b in _slower_decided)
            allowed_side = self._bias_to_side(horizon_bias, tf=tf, aligned=_fully_aligned)
            side_source = f"{asset}_{tf}_native"
            for slower_tf, slower_bias in slower_biases.items():
                if slower_bias not in {"BULLISH", "BEARISH"}:
                    continue
                if slower_bias != horizon_bias:
                    penalty += 0.03
                    penalty_reasons.append(f"{slower_tf}_disagrees")
                    side_source = f"{asset}_{tf}_vs_slower"
            primary_htf_bias = horizon_bias
            if self._fade_for_window(tf) and not _fully_aligned:
                # The side WAS faded (per-lane window enabled + not a fully-aligned trend)
                # — _bias_to_side already inverted (single source); only retag here for
                # attribution. Late flips are gated in fade_mode below (no double-flip).
                side_source = f"{asset}_{tf}_fade_native"
            # 2026-08-10 RSI-GATED COINFLIP FADE (operator GO — the MEASURED edge, see
            # project_rsi_fade_edge_found_2026_08_10). In the coinflip band the momentum call is
            # ANTI-predictive at RSI extremes: RSI<40 => native 30% WR => FADE 69.6% (robust across
            # 4 sessions, never <56%). Invert the resolved side when RSI is oversold (< rsi_below)
            # or overbought (> rsi_above) AND the trend is NOT fully aligned (align-gate = the
            # regime safety: fading a strong aligned trend gets run over — the documented failure
            # mode). Config strategies.<name>.rsi_fade (hot-reloadable => REVERT by flipping enabled
            # if fade-WR < breakeven). Never double-flips a lane fade_regime already faded. Retag
            # _rsi_fade for attribution. Fail-safe: absent/false or missing RSI => byte-identical.
            _rf = (self.full_config.get("risk", {}) or {}).get("rsi_fade", {}) or {}
            if (
                bool(_rf.get("enabled", False))
                and allowed_side in ("LONG", "SHORT")
                and not _fully_aligned
                and "fade_native" not in side_source  # do NOT undo an existing fade_regime flip
            ):
                _rf_wins = _rf.get("windows")
                _rf_win_ok = (_rf_wins is None) or (str(tf) in {str(w) for w in _rf_wins})
                try:
                    # 2026-08-10 Codex-fix: ta has NO top-level rsi_14/rsi. The journal `rsi`
                    # (what the fade EDGE was measured on) = sol.rsi_14 = the 15m RSI. Use ta.sol.rsi_14
                    # to faithfully replicate the measured signal (same 15m RSI for every window).
                    _rf_rsi = getattr(getattr(ta, "sol", None), "rsi_14", None)
                    _rf_rsi = float(_rf_rsi) if _rf_rsi is not None else None
                except (TypeError, ValueError):
                    _rf_rsi = None
                if _rf_win_ok and _rf_rsi is not None:
                    _rf_below = float(_rf.get("rsi_below", 40.0) or 40.0)
                    _rf_above = float(_rf.get("rsi_above", 101.0) or 101.0)
                    if _rf_rsi < _rf_below or _rf_rsi > _rf_above:
                        _faded_to = "SHORT" if allowed_side == "LONG" else "LONG"
                        # 2026-08-12 FRESH-TAPE REGIME GATE (item 2): the fade bled -$90 buying the
                        # falling knife (oversold flips SHORT->LONG into a DOWN tape). The lagging
                        # align-gate missed it; use the FRESH tape_map instead — only fade when a
                        # CONFIDENT tape (conf>=0.6) is NOT running AGAINST the faded side. Fail-open
                        # (missing/low-conf tape => fade as before, preserving the measured edge).
                        _tape_ok = True
                        _tp_dir = None      # captured for the gate-decision log emit below
                        _tp_conf = 0.0
                        _tp_fresh = False
                        try:
                            import time as _t
                            from src.analysis.tape_map import latest_tape_state
                            _tp = latest_tape_state(self._signal_strategy_name or asset) or {}
                            # STALE-TAPE FAIL-OPEN (Codex fix): only a FRESH (<=90s) confident tape
                            # may veto the fade — a stale high-conf read must NOT silently kill the
                            # measured 69.6% edge (latest_tape_state is in-memory, age-blind on its own).
                            _tp_fresh = (_t.time() - float(_tp.get("ts") or 0.0)) <= 90.0
                            _tp_conf = float(_tp.get("confidence") or 0.0)
                            _tp_dir = _tp.get("direction")
                            if _tp_fresh and _tp_conf >= 0.6:
                                if _faded_to == "LONG" and _tp_dir == "DOWN":
                                    _tape_ok = False
                                elif _faded_to == "SHORT" and _tp_dir == "UP":
                                    _tape_ok = False
                        except Exception:
                            _tape_ok = True
                        # 2026-08-12 GATE EMIT (item-2 instrumentation): make the fade veto DECISION
                        # visible so we stop GUESSING whether it fired (the bnb fade-longs bled into a
                        # DOWN tape and we had no log to see if the veto engaged). Grep RSI_FADE_GATE.
                        try:
                            logger.info(
                                "RSI_FADE_GATE strat=%s tf=%s rsi=%.0f faded_to=%s tape_dir=%s "
                                "tape_conf=%.2f fresh=%s decision=%s",
                                self._signal_strategy_name or asset, tf, _rf_rsi, _faded_to,
                                _tp_dir, _tp_conf, _tp_fresh,
                                ("APPLIED" if _tape_ok else "VETOED"),
                            )
                        except Exception:
                            pass
                        if _tape_ok:
                            allowed_side = _faded_to
                            side_source = f"{asset}_{tf}_rsi_fade"
                            penalty_reasons.append(f"rsi_fade(rsi={_rf_rsi:.0f})")
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
                # aligned=True => NEVER fade this marginal neutral-fallback path (default
                # sat-out anyway). Avoids an untagged faded side that would escape the
                # _fade_active flip-gate and get double-flipped. Runs native here.
                allowed_side=self._bias_to_side(slower_bias, tf=tf, aligned=True),
                side_source=f"{asset}_{tf}_neutral_fallback_{slower_tf}",
                horizon_tf=tf,
                horizon_bias=horizon_bias,
                slower_biases=slower_biases,
                primary_htf_bias=slower_bias,
                confidence_penalty=penalty,
                penalty_reasons=penalty_reasons,
            )

        # 2026-07-14 ETH HTF-ALIGNED CARRY (operator GO): eth 5m/15m LTF classifier
        # sits NEUTRAL through bull pullbacks, so with alt_neutral_fallback_sit_out the
        # lane sat out ~9850x today (7240 under a DECIDED BULLISH 1h). Unlike the retired
        # manufactured-direction fallback (32% WR settled), this carries the DECIDED 1h
        # direction ALIGNED (aligned=True => never fades, never contradicts a decided LTF
        # since it only fires when THIS tf is NEUTRAL) at a confidence penalty; lane size
        # is already reduced via the 0.25x window override. Calibration-phase: a 0-trade
        # lane cannot be calibrated. Default OFF (all other alts byte-identical); enabled
        # per-strategy via alt_neutral_htf_aligned_carry (eth only).
        if tf in {"5m", "15m"} and bool(
            self.config.get("alt_neutral_htf_aligned_carry", False)
        ):
            _carry_1h = slower_biases.get("1h")
            if _carry_1h in {"BULLISH", "BEARISH"}:
                return BiasResolution(
                    allowed_side=self._bias_to_side(_carry_1h, tf=tf, aligned=True),
                    side_source=f"{asset}_{tf}_htf_aligned_carry",
                    horizon_tf=tf,
                    horizon_bias=horizon_bias,
                    slower_biases=slower_biases,
                    primary_htf_bias=_carry_1h,
                    confidence_penalty=float(
                        self.config.get("alt_neutral_htf_carry_penalty", 0.05)
                    ),
                    penalty_reasons=[f"{tf}_neutral_htf_carry"],
                )

        # 2026-08-06 TAPE-MAP SIDE-BACKUP (operator GO). Root of the alt freeze: the per-asset voted-bias
        # returns NEUTRAL when the MACD histogram is below its conviction floor, and with enforce_alt_1h
        # that froze the WHOLE asset (5 trades/3h prime; sol/xrp/bnb all NEUTRAL -> allowed_side=None ->
        # neutral_bias skips). Instead of sitting out, resolve the side from the ADAPTIVE tape_map — a
        # DIFFERENT signal than the MACD vote (realized-price tape), so it doesn't share the hist-floor
        # weakness. tape UP->LONG, DOWN->SHORT; FLAT/stale/low-conf -> still sit out (genuinely no
        # direction). NOT the retired 32%-WR slower-TF fallback. Self-flips with the tape (won't short an
        # up-tape by construction). Confidence-penalized (marginal path). Hot-reload (self.config.get).
        # Reversible: alt_neutral_tape_backup: false.
        if bool(self.config.get("alt_neutral_tape_backup", False)):
            try:
                _tb_tm = _latest_tape_state(self._signal_strategy_name) or {}
                _tb_dir = str(_tb_tm.get("direction") or "").upper()
                _tb_conf = float(_tb_tm.get("confidence", 0.0) or 0.0)
                _tb_age = time.time() - float(_tb_tm.get("ts", 0.0) or 0.0)
            except Exception:
                _tb_dir, _tb_conf, _tb_age = "", 0.0, 1e9
            _tb_minconf = float(self.config.get("alt_neutral_tape_backup_min_conf", 0.6) or 0.0)
            _tb_maxage = float(self.config.get("alt_neutral_tape_backup_max_age_s", 90.0) or 0.0)
            if _tb_dir in ("UP", "DOWN") and _tb_conf >= _tb_minconf and (_tb_maxage <= 0.0 or _tb_age <= _tb_maxage):
                _tb_side = "LONG" if _tb_dir == "UP" else "SHORT"
                _tb_bias = "BULLISH" if _tb_dir == "UP" else "BEARISH"
                return BiasResolution(
                    allowed_side=_tb_side,
                    side_source=f"{asset}_{tf}_tape_backup",
                    horizon_tf=tf,
                    horizon_bias=horizon_bias,
                    slower_biases=slower_biases,
                    primary_htf_bias=_tb_bias,
                    confidence_penalty=float(self.config.get("alt_neutral_tape_backup_penalty", 0.05) or 0.0),
                    penalty_reasons=[f"{tf}_neutral_tape_backup"],
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
        # 2026-08-06 (Codex bundle review): under flat sizing + the per-lane CEILING model, the legacy
        # static per-lane/5m size multipliers are NEUTRALIZED — the flat base must flow FULL to the adaptive
        # sizer, whose per-lane ceiling (lane_max_usd) + realized climb is the single size authority. Stacking
        # the old 0.3x 5m shrink AND the 0.3x lane_mult here shrank the base to ~$1.35, making the new $40
        # short ceiling physically unreachable ($15*0.09*2.5=$3.4). thesis_side/lane_mult are still COMPUTED
        # (used downstream/logging), only their APPLICATION to size is skipped. Reverts with flat_sizing:false.
        _flat_sizing = bool((self.config.get("trading", {}) or {}).get("flat_sizing_enabled", False))
        if window_size == "5m" and self.calibration_size_multiplier_5m > 0 and not _flat_sizing:
            size_multiplier *= float(self.calibration_size_multiplier_5m)
        thesis_side = resolve_entry_policy_side(direction=direction, action=action)
        lane_mult = self._size_multiplier_for_lane(thesis_side)
        if lane_mult > 0 and not _flat_sizing:
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

    # _pocket_rsi_floor_tape_adjusted REMOVED 2026-07-31 (Phase-1): orphaned — it read a
    # root lane_tape_adapter.pocket_rsi_tape_* config block that no longer exists, so it was
    # always a pass-through. Pocket-floor admission now lives in _resolve_rsi_gate.

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

    def _window_delta_disagrees(
        self, asset_obj: Any, tf: str, mins_left: float, action: str
    ):
        """Window-delta confirmation (model-independent direction check).

        Returns ``(move_pct, delta_prob, margin)`` when the asset's OWN % move
        since the window opened DISAGREES with the chosen side — the direct fix
        for "shorts a rising tape" (lagging htf_bias locks BUY_NO while price
        actually rose this window). Returns ``None`` to PASS: gate disabled, or
        the delta is unavailable (fail open — a missing signal must never block),
        or the side agrees. Uses the asset's own price only; BTC is never
        consulted. MUST be called after the side is final (post fresh-cross /
        momentum flip), else it pre-empts that flip. Inherited by ETH.
        """
        if not bool(self.config.get("window_delta_confirm_enabled", False)):
            return None
        wd = evaluate_window_delta(asset_obj, tf, mins_left)
        if wd is None:
            return None
        move, prob = wd
        margin = float(self.config.get("window_delta_confirm_margin", 0.0) or 0.0)
        if delta_confirms_side(prob, action, margin):
            return None
        return move, prob, margin

    def _window_delta_flip(
        self,
        asset_obj: Any,
        tf: str,
        mins_left: float,
        action: str,
        primary_htf_bias: Optional[str] = None,
        alt_htf_bias: Optional[str] = None,
    ):
        """When the window-delta (price since window-open) clearly OPPOSES the
        chosen side, return the flipped ``(action, allowed_side, direction,
        new_est_prob_up, prob)`` so the bot trades WITH the tape instead of being
        blocked. This is the FREEZE FIX (2026-06-09): the block-only gate froze the
        bot because most setups were longs into a falling tape; flipping them to
        shorts recovers frequency in the correct direction. Keys on the SAME signal
        that was blocking (window-delta), NOT macd — momentum lags price.

        Returns None to leave the side unchanged: gate off, delta unavailable, tape
        agrees, or tape too uncertain (within ``window_delta_flip_margin`` of 0.5 —
        don't flip on near-coinflip noise). Uses the window-delta's own P(up) as the
        new est_prob (model-independent override). Inherited by ETH.
        """
        if not bool(self.config.get("window_delta_confirm_enabled", False)):
            return None
        wd = evaluate_window_delta(asset_obj, tf, mins_left)
        if wd is None:
            return None
        _move, prob = wd
        fmargin = float(self.config.get("window_delta_flip_margin", 0.05) or 0.0)
        if action == "BUY_YES" and prob < 0.5 - fmargin:
            # 2026-07-30 SHORT-IN-BULL GUARD (operator GO, sol-family). The flip-to-
            # SHORT is the freeze-fix for longs blocked into a FALLING (bear/neutral)
            # tape. When the alt's OWN primary HTF *and* 1h bias are BOTH bullish, an
            # intra-window dip must NOT convert a native long into a full short: that
            # path shorted SOL 1h inside bull/bull/bull twice (raw 0.18/0.23), both
            # lost, and NEVER appeared in the +$88 winner (its SOL shorts were NATIVE,
            # in a BEARISH SOL HTF). Opt-out (legacy): set the flag true to re-allow.
            if (
                not bool(self.config.get("window_delta_flip_to_short_in_bull_enabled", True))
                and str(primary_htf_bias or "").upper() == "BULLISH"
                and str(alt_htf_bias or "").upper() == "BULLISH"
            ):
                return None
            return "BUY_NO", "SHORT", "DOWN", prob, prob
        if action == "BUY_NO" and prob > 0.5 + fmargin:
            # 2026-07-12: flip-to-LONG gated off (default-on preserves legacy).
            # The documented freeze-fix is flip-to-SHORT; the symmetric flip-to-LONG
            # was overriding correct bearish shorts into 0.95-pegged longs (xrp/doge
            # 1h, fade-off) => fake +0.44 edge, live-negative. Codex GO 2026-07-12.
            if not bool(self.config.get("window_delta_flip_to_long_enabled", True)):
                return None
            return "BUY_YES", "LONG", "UP", prob, prob
        return None

    def _post_flip_disabled_side(
        self, action: str, tf: str, side_source: Optional[str]
    ) -> Optional[str]:
        """Re-apply the per-window sit-out (disable_buy_no_<tf> / disable_buy_yes_<tf>)
        AFTER window_delta_flip. The pre-flip gate runs on the NATIVE side, so a flip
        to the opposite side would otherwise bypass the sit-out (2026-06-16: doge 1h
        SHORT was disabled but window_delta_flip re-introduced 100% of the live shorts).
        Returns a skip reason if the post-flip side is disabled, else None.
        """
        if not (side_source and "window_delta_flip" in side_source):
            return None
        if action == "BUY_NO" and bool(self.config.get(f"disable_buy_no_{tf}", False)):
            # `buy_no_<tf>_allow_postflip`: admit the window-delta-FLIP short even
            # while the native side stays disabled. The flip short is the tape-driven
            # off-bias edge; the native (htf-aligned) short is the bleed. 2026-06-17
            # ghost (current era): the flip subset is +EV where the native isn't —
            # sol 1h flip +0.220 vs native −0.344; doge 1h flip +0.163. Lets the
            # disable keep blocking native shorts while the proven flip shorts pass.
            if bool(self.config.get(f"buy_no_{tf}_allow_postflip", False)):
                return None
            return f"buy_no_{tf}_disabled_lane_postflip"
        if action == "BUY_YES" and bool(self.config.get(f"disable_buy_yes_{tf}", False)):
            if bool(self.config.get(f"buy_yes_{tf}_allow_postflip", False)):
                return None
            return f"buy_yes_{tf}_disabled_lane_postflip"
        return None

    def _low_atr_gate_blocks(self, asset_obj: Any, window: str, action: str):
        """Lane-specific volatility gate (2026-06-09, Kimi signal-hunt + EV vet).

        Configured losing lanes only trade in LOW volatility (ATR < 0.5% of spot).
        On these (asset,window,side) cells the EV splits hard by ATR regime:
        mid-ATR (0.5–1.5%) bleeds ~−13% EV / 19% WR (2/3 of the trades) while
        low-ATR is ~breakeven (−1.6% EV / 43% WR). This gate skips the mid/high
        regime where the loss lives — harm reduction, not a profit signal.

        Returns ``(atr_pct, threshold)`` when BLOCKED, else None (gate off for the
        lane, or ATR unavailable → fail open). Config (per strategy):
        `low_atr_only_lanes` = ["<window>:<SIDE>"] ; `low_atr_gate_max_atr_pct`
        (default 0.005 = the calibration_buckets low/mid boundary). Inherited by ETH.
        """
        lanes = self.config.get("low_atr_only_lanes") or []
        if not lanes:
            return None
        side = "LONG" if action == "BUY_YES" else "SHORT"
        key = f"{str(window).strip()}:{side}".upper()
        if key not in {str(x).strip().upper() for x in lanes}:
            return None
        atr = float(getattr(asset_obj, "atr_14", 0.0) or 0.0)
        px = float(getattr(asset_obj, "current_price", 0.0) or 0.0)
        if atr <= 0.0 or px <= 0.0:
            return None  # fail open — never block on a missing ATR
        atr_pct = atr / px
        thresh = float(self.config.get("low_atr_gate_max_atr_pct", 0.005) or 0.005)
        if atr_pct >= thresh:
            return atr_pct, thresh
        return None

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

    def _should_suppress_native_5m_buy_no(self) -> bool:
        """Honor 5m BUY_NO suppression only when the inversion flip is not enabled."""
        return bool(self.config.get("disable_buy_no_5m_native", False)) and not bool(
            self.config.get("buy_no_5m_flip_to_yes", False)
        )

    def _alt_1h_alignment_blocks_entry(
        self,
        *,
        action: str,
        window_size: str,
        alt_1h_trend: Optional[str],
    ) -> Optional[str]:
        """Block fast entries that fight the alt's own 1h direction."""
        if not self.enforce_alt_1h_alignment:
            return None
        if not self.alt_1h_hard_block_5m_longs:
            return None
        if str(window_size or "").lower() != "5m":
            return None
        alt_h1 = str(alt_1h_trend or "NEUTRAL").upper()
        if action == "BUY_YES" and alt_h1 == "BEARISH":
            return "alt_1h_bearish_blocks_5m_buy_yes"
        return None

    def _own_tf_rsi_macd(self, asset, window):
        """2026-07-30 Fix A: return the ENTRY-WINDOW's own RSI + MACD, not the 4h
        canonical `rsi_14`. The short guard was gating 5m/15m entries on the 4h RSI
        (Codex-confirmed wrong-timeframe gating). Falls back to the 4h rsi_14 only if
        the per-window snapshot is missing (fail-safe, never crashes)."""
        _w = str(window or "")
        _tf_attr = {"5m": "tf_5m", "15m": "tf_15m", "1h": "tf_1h"}.get(_w)
        _macd_attr = {"5m": "macd_5m", "15m": "macd_15m", "1h": "macd_1h"}.get(_w)
        _rsi = None
        if _tf_attr is not None:
            _tf_state = getattr(asset, _tf_attr, None)
            # Only trust the own-tf RSI if the TF snapshot is POPULATED. An empty
            # TimeframeIndicatorState defaults rsi_14=50.0 with price=0.0, which would read
            # as "not oversold" and silently skip the guard (Codex NO-GO). Require price>0;
            # otherwise fall through to the canonical 4h rsi_14 (fail-safe, some signal).
            if _tf_state is not None and float(getattr(_tf_state, "price", 0.0) or 0.0) > 0.0:
                _rsi = getattr(_tf_state, "rsi_14", None)
        if _rsi is None:
            _rsi = getattr(asset, "rsi_14", None)  # fallback: 4h canonical
        _macd = getattr(asset, _macd_attr, None) if _macd_attr else None
        return _rsi, _macd

    def _resolve_rsi_gate(
        self,
        action: str,
        rsi: float,
        *,
        macd=None,
        window=None,
    ) -> tuple[bool, float, float]:
        """Return (hard_block, est_prob_delta, min_edge_add) — the SINGLE BUY_NO/BUY_YES
        RSI admission policy (2026-07-31 Phase-1 consolidation).

        Folds three formerly-scattered mechanisms into one place:
          1. exhaustion gate: rsi<=rsi_sell_block_below (BUY_NO) / >=rsi_buy_block_above
             (BUY_YES). Oversold short is HARD-blocked unless own-tf MACD still confirms the
             down-move (real continuation) — that preserves real downtrends and cuts the
             exhaustion-bounce shorts, without a blanket floor (the +800 winner-cut risk).
          2. hard/soft rsi penalty (rsi_hard_gate_enabled / rsi_soft_penalty_*).
          3. per-TF pocket floor buy_no_{window}_pocket_rsi_min (the RSI 30-35 band the
             exhaustion gate leaks): if a soft penalty is set -> return min_edge_add (weak
             low-RSI shorts must clear a higher bar, strong continuation still passes);
             if NO soft penalty is set -> preserve the legacy HARD pocket reject (BNB)."""
        if rsi is None:
            return False, 0.0, 0.0

        # Legacy HARD pocket reject (BNB): buy_no_{window}_pocket_rsi_min set with NO soft
        # penalty => hard-block the WHOLE band rsi < pocket_min, MACD-INDEPENDENT (reproduces
        # the deleted early block's `continue`). Evaluated BEFORE the exhaustion gate so an
        # oversold rsi<=sell_floor with a still-falling MACD can't slip through as
        # "continuation" (Codex review 2026-07-31). Soft-penalty lanes (XRP) skip this and
        # fall through to the exhaustion gate + per-TF soft floor below.
        if action == "BUY_NO" and window is not None:
            _pf_min = self.config.get(f"buy_no_{window}_pocket_rsi_min")
            if _pf_min is not None and rsi < float(_pf_min):
                _pf_pen = max(
                    0.0,
                    float(self.config.get("buy_no_pocket_rsi_soft_penalty", 0.0) or 0.0),
                )
                if _pf_pen <= 0.0:
                    return True, 0.0, 0.0

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

        if hit:
            # Fix B: oversold + momentum-not-confirming-down => bounce risk => hard block.
            if (
                action == "BUY_NO"
                and macd is not None
                and bool(self.config.get("oversold_short_exhaustion_gate", True))
            ):
                _hist = float(getattr(macd, "histogram", 0.0) or 0.0)
                _rising = bool(getattr(macd, "histogram_rising", False))
                _cross = str(getattr(macd, "crossover", "") or "")
                _still_falling = (_hist < 0.0) and (not _rising) and (_cross != "BULLISH_CROSS")
                if not _still_falling:
                    return True, 0.0, 0.0

            if self.rsi_hard_gate_enabled:
                return True, 0.0, 0.0
            if not self.rsi_soft_penalty_enabled:
                return False, 0.0, 0.0

            if action == "BUY_YES":
                penalty = max(0.0, self.rsi_soft_penalty_buy_yes)
                return False, -penalty, 0.0
            penalty = max(0.0, self.rsi_soft_penalty_buy_no)
            return False, penalty, 0.0

        # Per-TF SOFT pocket floor for the band ABOVE the exhaustion sell_floor (e.g.
        # 30<rsi<35). Soft-penalty lanes only (XRP); the no-soft-penalty HARD case was already
        # handled MACD-independently at the top of this function.
        if action == "BUY_NO" and window is not None:
            soft_floor = self.config.get(f"buy_no_{window}_pocket_rsi_min")
            if soft_floor is not None and rsi < float(soft_floor):
                _pen = max(
                    0.0,
                    float(self.config.get("buy_no_pocket_rsi_soft_penalty", 0.0) or 0.0),
                )
                if _pen > 0.0:
                    return False, 0.0, _pen  # soft: raise the edge bar (XRP)

        return False, 0.0, 0.0

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
        # 2026-07-13 A (operator GO, re-applies lost 07-01 fix): assets whose
        # Chainlink runs minutes-stale (HYPE: 42-50min Arbitrum cadence) validate
        # against the fresh exchange/HL mid (option-C get_current_price IS the HL
        # mid for hype) instead of a dead oracle + stale-relax fallbacks.
        # Per-strategy opt-in: oracle_ref_use_exchange_spot (hype_macro only).
        _o_price = getattr(sol, "chainlink_price", None)
        _o_updated = getattr(sol, "chainlink_updated_at", None)
        if bool(self.config.get("oracle_ref_use_exchange_spot", False)):
            _spot_ref = getattr(sol, "current_price", None)
            if _spot_ref:
                _o_price = _spot_ref
                _o_updated = now if now is not None else datetime.now(timezone.utc)
        return validate_oracle_reference(
            oracle_price=_o_price,
            exchange_spot=getattr(sol, "current_price", None),
            oracle_updated_at=_o_updated,
            max_age_sec=self.oracle_max_age_sec,
            max_basis_bps=max_basis_bps,
            require_oracle=self.require_oracle_for_updown,
            now=now,
            allow_exchange_when_oracle_missing=self.updown_allow_exchange_when_oracle_missing,
            stale_basis_relax_max_bps=stale_basis_relax_max_bps,
            basis_relax_max_bps=basis_relax_max_bps,
            stale_spot_is_settlement=bool(
                self.config.get("oracle_stale_spot_is_settlement", False)
            ),
            stale_spot_settlement_max_basis_bps=_float_config(
                "oracle_stale_spot_settlement_max_basis_bps", 500.0
            ),
        )

    def _updown_composite_floor(
        self,
        *,
        lane: str,
        window_size: Optional[str] = None,
        quant_confidence: Optional[float] = None,
        side: Optional[str] = None,
    ) -> float:
        window_overrides = self.updown_composite_cfg.get("strategy_window_min_scores", {})
        strategy_overrides = {}
        if isinstance(window_overrides, dict):
            strategy_overrides = window_overrides.get(self._signal_strategy_name, {}) or {}
        if isinstance(strategy_overrides, dict) and window_size:
            # 2026-07-12 per-LANE (window:SIDE) override, checked before the
            # window-level one. Early return = also bypasses the low-confidence
            # bump below — intentional: the settled cohorts this unblocks (xrp
            # 1h SHORT 79%/112, bnb 1h 72%/61%, hype 1h SHORT 60%, doge 1h
            # SHORT 100%/29) are low-confidence BY CONSTRUCTION and still win.
            if side:
                _lane_key = f"{str(window_size).strip()}:{str(side).strip().upper()}"
                override = strategy_overrides.get(_lane_key)
                if override is None:
                    override = strategy_overrides.get(_lane_key.lower())
                if override is not None:
                    logger.debug(
                        "composite floor lane override %s %s=%.3f",
                        self._signal_strategy_name, _lane_key, float(override),
                    )
                    return float(override)
            override = strategy_overrides.get(str(window_size))
            if override is not None:
                return float(override)

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
        window_size: Optional[str] = None,
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
            floor=self._updown_composite_floor(
                lane=lane,
                window_size=window_size,
                quant_confidence=confidence,
                side=(
                    "LONG" if action == "BUY_YES"
                    else ("SHORT" if action == "BUY_NO" else None)
                ),
            ),
            action=action,
            btc_1h_regime=None,  # 2026-07-04 alts NOT decided by BTC (Codex GO): neutralize BTC-1h-regime blend in ALT composite; it set BULL+BUY_YES regime_quality=0.25 and dragged bull-tape alt longs below floor. bitcoin.py keeps its own (own call sites).
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

        `primary_htf_bias` here is the ALT's own resolved HTF bias from
        `_resolve_alt_bias_for_tf` (alt 5m/15m/1h/4h) — NOT BTC. (The old wording
        referencing "BTC 4H as the primary gate" is obsolete: alts are decided by
        alt-native indicators; BTC inputs are gated off via `_btc_trade_inputs_enabled`
        which returns False.) Keeping est_prob aligned with the same alt bias that
        chose the side avoids the model leaning the other way.
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
        yes_price: Optional[float] = None,
        rsi_14: Optional[float] = None,
        raw_est_prob: Optional[float] = None,
    ) -> float:
        """Post-calibration BUY_YES floor bump — mirror of the BTC hook.

        The alt model under-shoots UP probability under bullish bias, so in-window
        BUY_YES is rejected as negative edge while the ghost log settles those same
        candidates at 68–76% WR / +EV (2026-05-28). Applied in calibrated/edge space
        (unlike `_hourly_buy_yes_native_bonus`, which adds pre-calibration), so the
        config'd amount maps ~1:1 onto edge. Asymmetric — BUY_NO untouched. Per-asset
        per-window via `{strategy}.<tf>_buy_yes_bullish_floor_bump`; unset => 0.0 (off).
        SOL and DOGE/XRP 15m are intentionally left unset (ghost −EV).

        1h is additionally PRICE-BAND gated (mirror of the BTC 1h hook,
        bitcoin.py): the 1h +EV cohort is a mid yes-price band (per-asset ~0.50–0.88,
        ghost 2026-06-07), while the 0.90+ band is the cheap-money trap (~+3% EV,
        near-zero upside / −100% tail). A blanket 1h bump would admit that trap by
        edge-rank, so the 1h bump only fires inside
        `{strategy}.1h_buy_yes_floor_price_min/max`; outside the band => 0.0.
        5m/15m keep the blanket bump (ghost-validated 2026-05-28).
        """
        if action != "BUY_YES" or htf_bias != "BULLISH":
            return 0.0
        if window_size not in ("5m", "15m", "1h"):
            return 0.0
        bump = float(
            self.config.get(f"{window_size}_buy_yes_bullish_floor_bump", 0.0)
        )
        if bump <= 0.0:
            return 0.0
        # 2026-07-30 QUANT-AGREE GUARD (fix A, operator GO). The bullish-HTF floor must
        # NOT force-admit a BUY_YES when the quant model itself implies SHORT (raw
        # est_prob below the agree floor). Live root cause: xrp 5m BUY_YES 0W/3L -$9.78,
        # each a raw_est<0.50 / quant_side=SHORT candidate that this +0.22 bump lifted
        # over the edge bar into a catastrophic stop (0.48->0.16). Only blocks a CONFIRMED
        # disagreement (raw present AND < floor); None/simple_band unaffected. Per-window
        # opt-in via {strategy}.<tf>_buy_yes_floor_require_quant_agree (+ optional
        # ..._quant_agree_min, default 0.50); unset => off (byte-identical to prior).
        if bool(self.config.get(f"{window_size}_buy_yes_floor_require_quant_agree", False)):
            _agree_min = float(
                self.config.get(f"{window_size}_buy_yes_floor_quant_agree_min", 0.50) or 0.50
            )
            if raw_est_prob is not None and float(raw_est_prob) < _agree_min:
                return 0.0
        # RSI overbought guard (2026-06-28): don't inflate est into an overbought top
        # (live: sol 15m RSI 86 -> +0.21 bump -> forced LONG into a reversal, mfe 0).
        # Per-window opt-in via {strategy}.<tf>_buy_yes_bullish_floor_bump_rsi_max;
        # unset => off (byte-identical to prior behaviour).
        rsi_max = float(self.config.get(f"{window_size}_buy_yes_bullish_floor_bump_rsi_max", 0.0) or 0.0)
        if rsi_max > 0.0 and rsi_14 is not None and float(rsi_14) >= rsi_max:
            return 0.0
        if window_size == "1h" and yes_price is not None:
            fp_min = float(self.config.get("1h_buy_yes_floor_price_min", 0.50))
            fp_max = float(self.config.get("1h_buy_yes_floor_price_max", 0.88))
            if not (fp_min <= yes_price <= fp_max):
                return 0.0
        return bump

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

        # Default 0.50: cached SOL 15m Jan20-Apr20 comparison beat 0.35 on WR and
        # net PnL, and this gate treats confirmed LTF as late-entry risk. Now
        # per-asset via ltf_confirm_strength_min (default == legacy SOL value).
        confirmed = strength >= self.ltf_confirm_strength_min
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
            if rsi > self.ep_rsi_ob_strong:   rsi_adj = self.ep_rsi_adj_up_ob_strong   # overbought (mean-revert: penalty; momentum: boost)
            elif rsi > self.ep_rsi_ob_mild:   rsi_adj = self.ep_rsi_adj_up_ob_mild     # elevated
            elif rsi < self.ep_rsi_os_bounce: rsi_adj = self.ep_rsi_adj_up_os_bounce   # oversold (mean-revert: boost; momentum: penalty)
            # Removed: 50<rsi<65 = +0.02 bonus. Live data: 14.3% WR -$14.68 in that bucket (worst of all)
        else:
            if rsi < self.ep_rsi_os_strong:   rsi_adj = -0.06   # Oversold — strongly against DOWN
            elif rsi < self.ep_rsi_os_mild:   rsi_adj = -0.02   # Low RSI — mild headwind for DOWN
            elif rsi > self.ep_rsi_ob_crash:  rsi_adj =  0.04   # Overbought crash potential
            # Removed: mirror of removed UP bonus

        # BTC-alt lag adjustment removed 2026-05-22 (alts decided by alt-native
        # indicators; live: lag=None 63% WR vs lag=value 50% WR). 2026-07-31 Phase-1
        # cleanup: the dead `lag_adj = 0.0` term was deleted from the est sum below.

        # ATR-based volatility context
        vol_adj = 0.0
        atr_pct = ta.sol.atr_14 / sol_price if sol_price > 0 else 0
        if atr_pct > self.ep_atr_high_pct:  # High vol: more room to reach threshold
            vol_adj = 0.02  # 2026-07-31: was `0.02 if UP else 0.02` (identical branches)
        elif atr_pct < self.ep_atr_low_pct:
            vol_adj = -0.03  # Low vol, harder to reach threshold

        # Time decay
        if days_to_resolution > 0:
            time_factor = min(1.0, days_to_resolution / 60.0)
            base_prob = base_prob * (1 - time_factor * 0.3) + 0.50 * (time_factor * 0.3)

        final = base_prob + ltf_adj + timing_adj + rsi_adj + vol_adj
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
        _sol_candidates = [m for m in markets if self._is_solana_market(m) and self._is_updown_market(m)]
        # Fail-closed price guard (2026-06-22): never trade an UNPRICED market (0.5-default
        # phantom edge). Deferred, not dropped — re-prices next cycle, entered at a real quote.
        sol_markets = [m for m in _sol_candidates if is_tradably_priced(m)]
        _unpriced = len(_sol_candidates) - len(sol_markets)
        if _unpriced:
            logger.info(
                "%s: skipped %d UNPRICED market(s) (0.5-default, deferred to next priced cycle)",
                getattr(self, "asset_name", "sol_macro"), _unpriced,
            )
        if not sol_markets:
            logger.info(
                f"{_brand} strategy: 0 {_alt_label} updown markets found out of {len(markets)} total markets"
            )
            return []

        logger.info(f"{_brand} strategy: Found {len(sol_markets)} {_alt_label} markets")

        # Fetch full technical analysis ONCE per cycle.
        # Run off-loop with a hard per-lane timeout so a slow data source
        # (e.g. hyperliquid /info) can't block exits/dashboard or wedge the cycle.
        _scan_to = float(self.config.get("scan_analysis_timeout_sec", 15.0) or 15.0)
        ta = await analysis_with_timeout(
            self.sol_service.get_full_analysis, lane=_brand, timeout_sec=_scan_to
        )
        if not ta:
            logger.warning(f"{_brand} strategy: Could not fetch {_alt_label}/BTC price data")
            return []

        # BTC analysis is diagnostic-only here. Prefer the once-per-cycle value
        # injected by main.py; only fetch our own if none was injected (e.g. tests).
        if self._btc_ta_inject_set:
            btc_ta = self._injected_btc_ta
            self._btc_ta_inject_set = False  # consume; main re-sets each cycle
        else:
            btc_ta = await analysis_with_timeout(
                self.btc_service.get_full_analysis, lane=f"{_brand}:btc", timeout_sec=_scan_to
            )
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

        # 2026-08-02 TAPE MAP (Phase 1, SHADOW — operator directive: map the tape so the bot can
        # shift behavior with it). Log this alt's mechanical tape state once per cycle (deduped
        # inside) to data/calibration/tape_map.jsonl so we can SEE the regime + its transitions.
        # OBSERVE-ONLY: no trade depends on this yet. Fail-silent (snapshot_and_log never raises).
        _tape_map_snapshot(
            self._signal_strategy_name,
            current_price=getattr(sol, "current_price", None),
            atr_14=getattr(sol, "atr_14", None),
            trend_direction=getattr(sol, "trend_direction", None),
            trend_strength=getattr(sol, "trend_strength", None),
            macd_5m=getattr(sol, "macd_5m", None),
            macd_15m=getattr(sol, "macd_15m", None),
            macd_1h=getattr(sol, "macd_1h", None),
            rsi_14=getattr(sol, "rsi_14", None),
            ema_9=getattr(sol, "ema_9", None),
            ema_21=getattr(sol, "ema_21", None),
            ema_50=getattr(sol, "ema_50", None),
        )

        # 2026-08-02 SIDE-VETO SHADOW (observe-only, operator GO; high-confidence lane). Diagnosed
        # bleed: xrp 15m SHORT (BUY_NO) fires into UP tape = wrong-direction. Log what a tape-
        # adaptive veto WOULD do each cycle (tape==UP AND realized down-adapter not loosening) —
        # DOES NOT block. Measured offline vs trade outcomes before it is ever made active. R4.
        try:
            if self._signal_strategy_name == "xrp_macro":
                _sv_ts = _latest_tape_state("xrp_macro")
                _sv_dir = str((_sv_ts or {}).get("direction") or "")
                _sv_adm = float(get_tape_admission_delta("xrp_macro", "15m", "down") or 0.0)
                _log_side_veto_shadow(
                    strategy="xrp_macro", window="15m", side="down", target_action="BUY_NO",
                    tape_dir=(_sv_dir or None), tape_strength=(_sv_ts or {}).get("strength"),
                    tape_conf=(_sv_ts or {}).get("confidence"),  # 2026-08-12: was NEVER logged -> tape_conf null in 22k rows
                    adm=round(_sv_adm, 4), would_veto=bool(_sv_dir == "UP" and _sv_adm >= 0.0),
                )
        except Exception:
            pass

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
                    macd_hist_5m=getattr(getattr(sol, "macd_5m", None), "histogram", None),
                    macd_hist_15m=getattr(getattr(sol, "macd_15m", None), "histogram", None),
                    macd_hist_1h=getattr(getattr(sol, "macd_1h", None), "histogram", None),
                    rsi_5m=getattr(getattr(sol, "tf_5m", None), "rsi_14", None),
                    rsi_15m=getattr(getattr(sol, "tf_15m", None), "rsi_14", None),
                    rsi_1h=getattr(getattr(sol, "tf_1h", None), "rsi_14", None),
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
            # Model-independent window-delta on EVERY reject (forward-validation of
            # the confirmation gate). Don't overwrite values the gate block already
            # stamped. alt-native price only; fails open if unavailable.
            if "window_delta_prob" not in merged_context and window in ("5m", "15m", "1h"):
                try:
                    _wd_ml = merged_context.get("eval_mins_left")
                    if _wd_ml is None:
                        _wd_ml = merged_context.get("mins_left", 0.0)
                    _wd = evaluate_window_delta(sol, window, float(_wd_ml or 0.0))
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
            if btc_1h_regime is not None:
                merged_context["btc_1h_regime"] = btc_1h_regime
            # Attribute the reject to its resolved lane instead of the catch-all
            # pre_resolver_reject bucket. The bias resolver runs at the TOP of each
            # candidate iteration (before any skip fires), so side_source /
            # resolver_path are already set for EVERY skip in this loop — they were
            # just never forwarded, collapsing ~81% of settled rejects into
            # pre_resolver_reject and blinding per-lane (LONG vs SHORT) calibration.
            # Capture from the loop scope; fail open to None so any genuinely
            # pre-resolver path stays correctly unbucketed.
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
                btc_1h_regime=btc_1h_regime,
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

        for market in sol_markets:
            is_updown = self._is_updown_market(market)
            _updown_tf = (
                updown_timeframe_label(resolved_updown_window_minutes(market))
                if is_updown
                else "15m"
            )
            window_label = _updown_tf
            # 2026-07-31 Phase-1: per-candidate default for the consolidated RSI pocket-floor
            # min-edge add (set by _resolve_rsi_gate at the gate call sites; applied at the
            # final edge block). Bound here so no path can hit it undefined or carry a stale
            # value from the previous candidate.
            rsi_min_edge_add = 0.0
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
            # 2026-06-27 per-lane fade: the side was ACTUALLY faded for this candidate
            # only when resolution tagged it *_fade_native (window enabled + not a
            # fully-aligned trend). Gate the late flips on THIS, not the master
            # fade_regime flag — otherwise non-faded / aligned / excluded-window lanes
            # would run native momentum with their flips wrongly disabled (≠ baseline).
            # 2026-08-10 Codex-fix: treat rsi_fade as fade-active too, else late flip paths
            # (window_delta_flip / fresh_cross_override / posterior / 5m NO->YES) silently UNDO
            # the deliberate RSI fade. Both are intentional side inversions that must stick.
            _fade_active = any(t in (side_source or "") for t in ("fade_native", "rsi_fade"))
            primary_htf_bias = resolution.primary_htf_bias
            # [1h] simple band: price-band admission for native LONG signals only.
            # Do not choose side here; alt direction comes from the bias resolver.
            _simple_band_long = (
                is_updown and _updown_tf == "1h"
                and self._a1hsl_enabled
                and self._a1hsl_entry_min <= float(market.yes_price or 0) <= self._a1hsl_entry_max
            )
            if _simple_band_long and allowed_side == "LONG":
                # allowed_side is LONG here (a faded side would be SHORT), so this is a
                # native/aligned long band entry regardless of fade_regime — tag plainly.
                side_source = (side_source or "") + "+simple_band_long"
                # 2026-07-28 QUALITY VETO (opt-in, sol only). The 1h simple-band long
                # cohort admits on price-band consensus alone (est_prob unusable, so it's
                # EXEMPT from the quant-agreement gate), and is a verified realized leak:
                # sol n63 30%WR -$79.64, with NO salvageable +EV subset (every momentum
                # split negative; Codex-confirmed). RSI-pocket is a banned fix (was wrong
                # for doge's band). Veto + ghost-log so the counterfactual keeps settling.
                _a1hsl_cfg = self.config.get("alt_1h_simple_long", {}) or {}
                if bool(_a1hsl_cfg.get("quality_veto_enabled", False)):
                    _bump_skip("alt_1h_simple_long_quality_veto")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=allowed_side,
                        action="BUY_YES",
                        reason="alt_1h_simple_long_quality_veto",
                        yes_price=market.yes_price,
                        htf_bias=primary_htf_bias,
                        context={"side_source": side_source,
                                 "rule": "sol_1h_simple_band_disable_20260728"},
                    )
                    continue
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
            # 2026-06-14: per-asset BUY_YES sit-out. Data-driven lane cut — set
            # `disable_buy_yes: true` for an asset whose UP-side calls bleed (e.g. bnb:
            # 40% WR, -$67 over 113 taken-settled trades). Opt-in, default off, applies
            # to all windows for that asset; ghost-logged so the counterfactual settles.
            if is_updown and action == "BUY_YES" and bool(self.config.get("disable_buy_yes", False)):
                _bump_skip("buy_yes_disabled_lane")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=allowed_side,
                    action=action,
                    reason="buy_yes_disabled_lane",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={"side_source": side_source},
                )
                continue
            # 2026-06-15: per-asset, PER-WINDOW BUY_NO (SHORT) sit-out. Set
            # `disable_buy_no_<window>: true` (disable_buy_no_5m / _15m / _1h) for an
            # asset whose short-side on that window is -EV by ghost net-of-fee EV
            # (doge 1h -0.166, doge 15m -0.10/-0.15 over n=16k). Opt-in, default off;
            # ghost-logged so the counterfactual keeps settling and we can re-admit.
            if (
                is_updown
                and action == "BUY_NO"
                and bool(self.config.get(f"disable_buy_no_{_updown_tf}", False))
            ):
                _bump_skip(f"buy_no_{_updown_tf}_disabled_lane")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=allowed_side,
                    action=action,
                    reason=f"buy_no_{_updown_tf}_disabled_lane",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={"side_source": side_source},
                )
                continue
            # 2026-08-02 KNOB A — OVERSOLD-SHORT gate, TAPE-ADAPTIVE (operator directive: no
            # tape-blind gates that break when the tape flips). The oversold short is only -EV
            # when the tape is UP (it fades the bounce -> stop-out; that's the -23.8/0W bull
            # cluster). In a DOWN tape an oversold alt CONTINUES down = the short is +EV. So do
            # NOT hardcode "block RSI<40" — defer to the lane's REALIZED tape-adapter for the
            # short ("down") side: get_tape_admission_delta >= admit_below => short lane net-
            # losing/never-green (up/bounce tape) => BLOCK the oversold short; < admit_below =>
            # short lane winning (down tape) => ADMIT (momentum continuation). The lane's
            # non-oversold shorts (RSI >= floor) keep filling and feed the adapter, so the gate
            # SELF-FLIPS when the tape turns instead of staying fit to this bull. Config:
            # buy_no_oversold_hard_block_rsi (which shorts count as oversold, 0=off) +
            # buy_no_oversold_adapter_admit_below (delta threshold to admit, default 0.0 =>
            # block while losing/neutral incl no-data, admit only once realized-winning).
            # 2026-08-03 P0 FLAT-ENTRY BLOCK (Codex bundle, operator GO): realized forensics show 23%
            # of trades fired when the tape had NO direction (tape_map FLAT), ~15% WR = pure fee-bleed
            # coin-flips. Block entry (BOTH sides) when the tape_map read is FLAT / low-conf on a FRESH
            # snapshot. FAIL OPEN when the snapshot is stale/missing (feed stall must not starve all
            # entries). Per-window via _tf_cfg; default OFF (require_tape_direction=False). Config (by_tf):
            # require_tape_direction (bool), require_tape_direction_min_conf (0.5), _max_age_s (90).
            if is_updown and bool(self._tf_cfg(_updown_tf, "require_tape_direction", False)):
                try:
                    _fd_tm = _latest_tape_state(self._signal_strategy_name) or {}
                except Exception:
                    _fd_tm = {}
                _fd_dir = str(_fd_tm.get("direction") or "").upper()
                _fd_conf = float(_fd_tm.get("confidence", 0.0) or 0.0)
                _fd_minconf = float(self._tf_cfg(_updown_tf, "require_tape_direction_min_conf", 0.5) or 0.0)
                _fd_max_age = float(self._tf_cfg(_updown_tf, "require_tape_direction_max_age_s", 90.0) or 0.0)
                try:
                    _fd_age = time.time() - float(_fd_tm.get("ts", 0.0) or 0.0)
                except Exception:
                    _fd_age = 1e9
                _fd_fresh = _fd_max_age <= 0.0 or _fd_age <= _fd_max_age
                if _fd_fresh and (_fd_dir not in ("UP", "DOWN") or _fd_conf < _fd_minconf):
                    _bump_skip(f"{_updown_tf}_flat_tape_no_direction")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=allowed_side,
                        action=action,
                        reason=f"{_updown_tf}_flat_tape_no_direction(dir={_fd_dir or 'NONE'},conf={_fd_conf:.2f})",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={"side_source": side_source, "tape_dir": _fd_dir, "tape_conf": _fd_conf},
                    )
                    continue
            # 2026-08-03 (c) TAPE-MAP SIDE-VETO (operator GO, Codex-reviewed): a GENERAL wrong-side
            # veto for shorts — covers the RSI 40-70 shorts the oversold gate (below) does NOT catch.
            # Veto BUY_NO when tape_map direction is UP with confidence >= threshold = shorting into
            # an up-tape = wrong side (evidence: xrp 15m BUY_NO -30.06/WR13% n=15; side-veto shadow:
            # 29% of xrp 15m shorts fire into UP-tape). Self-flips: SILENT in DOWN/FLAT, fires ONLY
            # on a confident UP-tape. Per-window via _tf_cfg so it scopes to 15m only. Config (by_tf):
            # buy_no_tape_map_veto_up (bool, default False=off) + buy_no_tape_map_veto_min_confidence (0.6).
            if is_updown and action == "BUY_NO" and bool(self._tf_cfg(_updown_tf, "buy_no_tape_map_veto_up", False)):
                try:
                    _sv_tm = _latest_tape_state(self._signal_strategy_name) or {}
                except Exception:
                    _sv_tm = {}
                _sv_dir = str(_sv_tm.get("direction") or "").upper()
                _sv_conf = float(_sv_tm.get("confidence", 0.0) or 0.0)
                _sv_minconf = float(self._tf_cfg(_updown_tf, "buy_no_tape_map_veto_min_confidence", 0.6) or 0.0)
                # Codex fix: freshness guard — a stale cached UP after a feed stall must NOT over-block.
                # Fail OPEN (no veto) when the map snapshot is older than max_age_s.
                _sv_max_age = float(self._tf_cfg(_updown_tf, "buy_no_tape_map_veto_max_age_s", 90.0) or 0.0)
                try:
                    _sv_age = time.time() - float(_sv_tm.get("ts", 0.0) or 0.0)
                except Exception:
                    _sv_age = 1e9
                if _sv_dir == "UP" and _sv_conf >= _sv_minconf and _sv_age <= _sv_max_age:
                    _bump_skip(f"buy_no_{_updown_tf}_tape_map_side_veto")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=allowed_side,
                        action=action,
                        reason=f"buy_no_{_updown_tf}_tape_map_side_veto(dir=UP,conf={_sv_conf:.2f})",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={"side_source": side_source, "tape_dir": _sv_dir, "tape_conf": _sv_conf},
                    )
                    continue
            # 2026-08-03 #1 REBUILD (operator GO): gate on the TAPE MAP direction, which is
            # fed by price/indicators every ~30s and therefore HAS data even when the realized
            # adapter is starved. The old adapter-only version DEADLOCKED: in a deep one-sided
            # oversold down-tape every short is RSI<floor -> all blocked -> nothing fills ->
            # adapter never learns (n=1-5, delta 0.0) -> block-on-no-data fails CLOSED -> it
            # degenerates into a STATIC RSI<floor block that fights the very down-tape it should
            # ride (confirmed 2026-08-03: 828 oversold shorts blocked / 0 non-oversold fills).
            #   map DOWN (continuation) -> ADMIT the oversold short
            #   map UP   (bounce risk)  -> BLOCK
            #   map FLAT / low-conf / no-map -> fall back to the realized adapter (old logic:
            #       block if adm >= admit_below, else admit) so the adapter is consulted ONLY in
            #       the genuinely-ambiguous case and no-data no longer forces a static block.
            # Self-flips bull<->bear because the map is not fed by (gated) fills. Config:
            # buy_no_oversold_hard_block_rsi (0=off), buy_no_oversold_use_tape_map (default True),
            # buy_no_oversold_map_min_confidence (default 0.5), buy_no_oversold_adapter_admit_below.
            _a_bn_floor = float(self.config.get("buy_no_oversold_hard_block_rsi", 0.0) or 0.0)
            if is_updown and action == "BUY_NO" and _a_bn_floor > 0.0:
                _a_rsi = getattr(sol, "rsi_14", None)
                if _a_rsi is not None and float(_a_rsi) < _a_bn_floor:
                    _os_block = None  # None=undecided, True=block, False=admit
                    _os_dbg = ""
                    if bool(self.config.get("buy_no_oversold_use_tape_map", True)):
                        try:
                            _os_tm = _latest_tape_state(self._signal_strategy_name) or {}
                        except Exception:
                            _os_tm = {}
                        _os_dir = str(_os_tm.get("direction") or "").upper()
                        _os_conf = float(_os_tm.get("confidence", 0.0) or 0.0)
                        _os_min_conf = float(self.config.get("buy_no_oversold_map_min_confidence", 0.5) or 0.0)
                        if _os_dir in ("DOWN", "UP") and _os_conf >= _os_min_conf:
                            _os_block = (_os_dir == "UP")  # UP=bounce->block, DOWN=continuation->admit
                            _os_dbg = f"map={_os_dir},conf={_os_conf:.2f}"
                        else:
                            _os_dbg = f"map={_os_dir or 'NONE'},conf={_os_conf:.2f}->adptr"
                    if _os_block is None:  # FLAT / low-conf / map off -> realized-adapter tie-break
                        try:
                            _a_adm = float(
                                get_tape_admission_delta(self._signal_strategy_name, _updown_tf, "down") or 0.0
                            )
                        except Exception:
                            _a_adm = 0.0
                        _a_admit_below = float(
                            self.config.get("buy_no_oversold_adapter_admit_below", 0.0) or 0.0
                        )
                        _os_block = (_a_adm >= _a_admit_below)
                        _os_dbg = (f"{_os_dbg},adm={_a_adm:+.3f}" if _os_dbg else f"adm={_a_adm:+.3f}")
                    if _os_block:
                        _bump_skip(f"buy_no_{_updown_tf}_oversold_adaptive_block")
                        _log_skip_reject(
                            market=market,
                            window=_updown_tf,
                            side=allowed_side,
                            action=action,
                            reason=f"buy_no_{_updown_tf}_oversold_adaptive_block(rsi={float(_a_rsi):.0f}<{_a_bn_floor:.0f},{_os_dbg})",
                            yes_price=yes_price,
                            htf_bias=primary_htf_bias,
                            context={"side_source": side_source, "rsi_14": _a_rsi, "oversold_block_dbg": _os_dbg},
                        )
                        continue
                    # else: down-tape continuation (or adapter says short winning) -> admit, fall through.
            # 2026-06-15: per-asset, PER-WINDOW BUY_YES (LONG) sit-out. Set
            # `disable_buy_yes_<window>: true` (e.g. disable_buy_yes_15m) for an asset
            # whose long-side on that window is -EV (bnb 15m -0.048/-0.080 over n=16k)
            # while its OTHER windows stay +EV (bnb 1h/5m long). Narrower than the
            # all-window `disable_buy_yes` above. Opt-in, default off; ghost-logged.
            if (
                is_updown
                and action == "BUY_YES"
                and bool(self.config.get(f"disable_buy_yes_{_updown_tf}", False))
            ):
                _bump_skip(f"buy_yes_{_updown_tf}_disabled_lane")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=allowed_side,
                    action=action,
                    reason=f"buy_yes_{_updown_tf}_disabled_lane",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={"side_source": side_source},
                )
                continue
            # 2026-06-16: per-window BUY_YES BULLISH-htf sit-out. Set
            # `disable_buy_yes_<window>_when_bullish: true` for a lane whose long-side
            # is -EV only when chasing an already-bullish tape, while NEUTRAL/BEARISH
            # longs stay +EV. DOGE 1h: ghost recent BULLISH n=119 -0.318 vs NEUTRAL
            # +0.350 / BEARISH +0.086. Bias-conditioned, NOT a blanket disable_buy_yes.
            # Opt-in, default off; ghost-logged so the counterfactual keeps settling.
            if (
                is_updown
                and action == "BUY_YES"
                and str(primary_htf_bias or "").upper() == "BULLISH"
                and bool(self.config.get(f"disable_buy_yes_{_updown_tf}_when_bullish", False))
            ):
                _bump_skip(f"buy_yes_{_updown_tf}_bullish_disabled_lane")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=allowed_side,
                    action=action,
                    reason=f"buy_yes_{_updown_tf}_bullish_disabled_lane",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={"side_source": side_source},
                )
                continue
            # 2026-06-16: per-window BUY_YES POCKET restriction. For a lane whose BUY_YES
            # is ~break-even in aggregate but +EV only when oversold or counter-bias, admit
            # the long ONLY when alt RSI < `buy_yes_<window>_pocket_rsi_max` OR htf_bias is
            # BEARISH (contrarian-long pocket). DOGE 15m: aggregate -0.017 but RSI<35 +0.196,
            # BEARISH +0.073. Opt-in (key unset = off); ghost-logs the rejected rest.
            _by_pocket_rsi_max = self.config.get(f"buy_yes_{_updown_tf}_pocket_rsi_max")
            if is_updown and action == "BUY_YES" and _by_pocket_rsi_max is not None:
                _alt_rsi = getattr(sol, "rsi_14", None)
                # Bearish arm is per-lane: DOGE 15m bearish longs are +EV (+0.073) so it
                # helps; BNB 15m bearish&RSI>=35 longs are -EV (-0.040) so it must be OFF.
                _incl_bear = bool(self.config.get(f"buy_yes_{_updown_tf}_pocket_include_bearish", True))
                _is_bearish = _incl_bear and str(primary_htf_bias or "").upper() == "BEARISH"
                _in_pocket = _is_bearish or (
                    _alt_rsi is not None and float(_alt_rsi) < float(_by_pocket_rsi_max)
                )
                if not _in_pocket:
                    # 2026-08-01 (operator GO — "get alt participation on the fade side").
                    # OVERBOUGHT FADE-SHORT: a BUY_YES blocked here is an overbought long-chase
                    # (alt RSI >= pocket_rsi_max, non-bearish htf) — which is exactly a fade-short
                    # setup. When RSI is EXTREME (>= overbought_fade_short_rsi) flip the blocked
                    # long to a SHORT and fall through to the BUY_NO pocket + RSI gate + edge +
                    # momentum-veto below, which RE-PRICE the edge for the NO side (edge is the
                    # absolute formula yes-est off direction-agnostic est_prob_up, so the flip is
                    # correct as long as the NO gates key off `action` — they do). Per-asset opt-in
                    # (overbought_fade_short_enabled, DEFAULT OFF — inert until enabled per lane).
                    # Only fires in NON-bearish htf: a bearish overbought already shorts natively.
                    _ofs_on = bool(self.config.get("overbought_fade_short_enabled", False))
                    _ofs_rsi = float(self.config.get("overbought_fade_short_rsi", 75.0) or 75.0)
                    # 2026-08-02 (operator "go further" — xrp 15m fade audit + TAPE-LANE-EDGE-MAP):
                    # (1) WINDOW ALLOWLIST. The fade only carries tape signal at 5m; the 15m/1h
                    #     horizons show NO directional edge in the dataset, and live xrp 15m fade
                    #     went 2W/8 / -13.66 shorting a UNANIMOUS bull 7 of 8 times. When
                    #     `overbought_fade_short_windows` is set, only fade on those windows.
                    # Normalize (Codex E): a bare string "5m" would set()-split into
                    # {"5","m"} and mis-suppress; a non-iterable would raise. Coerce safely.
                    _ofs_windows = self.config.get("overbought_fade_short_windows")
                    if isinstance(_ofs_windows, str):
                        _ofs_windows_set = {_ofs_windows}
                    elif _ofs_windows:
                        try:
                            _ofs_windows_set = {str(w) for w in _ofs_windows}
                        except TypeError:
                            _ofs_windows_set = set()
                    else:
                        _ofs_windows_set = set()
                    _ofs_window_ok = (not _ofs_windows_set) or (str(_updown_tf or "") in _ofs_windows_set)
                    # (2) UNANIMOUS-BULL GUARD. Never fade a top when 1H+15M+5M are ALL bullish —
                    #     overbought keeps running in a strong trend; the fade is only +EV on a
                    #     high-vol overshoot / a crack in the trend, not a steady unanimous bull.
                    #     macro_trend/bias_15m/bias_5m are resolved above (~L3173). Default ON.
                    _ofs_block_unan = bool(
                        self.config.get("overbought_fade_short_block_unanimous_bull", True)
                    ) and (
                        str(macro_trend or "").upper() == "BULLISH"
                        and str(bias_15m or "").upper() == "BULLISH"
                        and str(bias_5m or "").upper() == "BULLISH"
                    )
                    if (
                        _ofs_on
                        and _ofs_window_ok
                        and not _ofs_block_unan
                        and _alt_rsi is not None
                        and float(_alt_rsi) >= _ofs_rsi
                        and str(primary_htf_bias or "").upper() != "BEARISH"
                        # RESPECT intentional short disables (Codex caveat): this flip runs AFTER
                        # the generic disable_buy_no_<tf> gate, so re-check it here — never fade
                        # into a lane whose BUY_NO is deliberately off (e.g. DOGE 15m/1h shorts).
                        and not bool(self.config.get(f"disable_buy_no_{_updown_tf}", False))
                        and not bool(self.config.get(f"disable_buy_no_{_updown_tf}_native", False))
                    ):
                        action = "BUY_NO"
                        allowed_side = "SHORT"
                        side_source = f"{side_source}__overbought_fade_short(rsi={float(_alt_rsi):.0f})"
                    else:
                        _bump_skip(f"buy_yes_{_updown_tf}_pocket_off")
                        _log_skip_reject(
                            market=market,
                            window=_updown_tf,
                            side=allowed_side,
                            action=action,
                            reason=f"buy_yes_{_updown_tf}_pocket_off",
                            yes_price=yes_price,
                            htf_bias=primary_htf_bias,
                            context={"side_source": side_source, "rsi_14": _alt_rsi},
                        )
                        continue
            # 2026-07-31 Phase-1: the early per-window BUY_NO pocket RSI floor moved into the
            # consolidated _resolve_rsi_gate (hard reject when no soft penalty = BNB; soft
            # min_edge_add when a penalty is set = XRP). This duplicated block is deleted.
            # 2026-07-28: per-window BUY_NO POCKET CEILING (RSI max) — mirror of the
            # buy_yes pocket_rsi_max but for shorts on FAST windows where the RSI dynamic
            # INVERTS vs 1h/15m. 5m shorts are +EV only when genuinely oversold (low RSI,
            # a real broken-down dump) and -EV when shorting mid-RSI dips into the bounce.
            # sol 5m data (recent 10 sess): RSI>=28 = 0W/5L, RSI<28 = 5W/2L. Admit BUY_NO
            # only when alt RSI <= buy_no_<window>_pocket_rsi_max. Opt-in (unset = off);
            # ghost-logs the rejected rest.
            _bn_pocket_rsi_max = self.config.get(f"buy_no_{_updown_tf}_pocket_rsi_max")
            if is_updown and action == "BUY_NO" and _bn_pocket_rsi_max is not None:
                _alt_rsi = getattr(sol, "rsi_14", None)
                if _alt_rsi is not None and float(_alt_rsi) > float(_bn_pocket_rsi_max):
                    _bump_skip(f"buy_no_{_updown_tf}_pocket_ceiling_off")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf,
                        side=allowed_side,
                        action=action,
                        reason=f"buy_no_{_updown_tf}_pocket_ceiling_off",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={"side_source": side_source, "rsi_14": _alt_rsi},
                    )
                    continue
            # 2026-05-31: 5m-native BUY_NO is anti-predictive — held-to-resolution WR
            # ~22% across eth/xrp/doge/sol vs 50-65% on 15m-native; MACD-confirmed 5m
            # shorts lose, so the signal (not the gate) is inverted. Opt-in sit-out,
            # ghost-logged via _log_skip_reject so the counterfactual keeps settling.
            # Longs (BUY_YES) are ~50% and unaffected.
            if (
                is_updown
                and _updown_tf == "5m"
                and action == "BUY_YES"
                and "fade_native" not in (side_source or "")
                and bool(self.config.get("disable_buy_yes_5m_native", False))
            ):
                # 2026-07-13 H1 (operator GO): hype 5m momentum longs WR19% -$114 July;
                # fade-sourced 5m longs 3W/0L +$27. Suppress native-source longs only.
                _bump_skip("buy_yes_5m_native_suppressed")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=allowed_side,
                    action=action,
                    reason="buy_yes_5m_native_suppressed",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={"side_source": side_source},
                )
                continue

            # 2026-07-15 HYPE-MIX (operator GO, Codex): mirror of the 5m native-long
            # suppressor for 15m. hype 15m NATIVE longs occur only when the MTF is
            # fully aligned (fade is suppressed on aligned trends), i.e. the aligned-
            # bull momentum-long = the -$22.82/45t/33% loser leg. Suppress native-source
            # 15m longs only; the align-gated 15m FADE (chop mean-reversion) still fires,
            # and the 1h BUY_YES winner (+$18/64%) carries long exposure. Config-gated
            # (disable_buy_yes_15m_native), hype-only. Break: 10 closed, revert false if
            # the suppressed cohort would have won (>=2 skipped-then-would-win) net-positive.
            if (
                is_updown
                and _updown_tf == "15m"
                and action == "BUY_YES"
                and "fade_native" not in (side_source or "")
                and bool(self.config.get("disable_buy_yes_15m_native", False))
            ):
                _bump_skip("buy_yes_15m_native_suppressed")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=allowed_side,
                    action=action,
                    reason="buy_yes_15m_native_suppressed",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={"side_source": side_source},
                )
                continue

            # 2026-07-25 NATIVE-SHORT CUT (operator GO, next-restart bundle): mirror of the
            # 15m native-long suppressor for BUY_NO. The plain disable_buy_no_15m gate does
            # NOT catch native-source shorts (side_source=doge_15m_native / hype_15m_native),
            # so hype/doge 15m-down kept firing after the 07-25 "cut". This closes them.
            # Per-strategy (self.config is the strategies.<name> dict), isolated. Excludes
            # fade_native sources for symmetry with the long hooks (no active fade-native
            # short in current config: hype fade_regime_windows [], doge fade_regime false).
            # Break: revert disable_buy_no_15m_native false if the suppressed cohort would
            # have won (>=2 skipped-then-would-win, net-positive) on either asset.
            if (
                is_updown
                and _updown_tf == "15m"
                and action == "BUY_NO"
                and "15m_native" in (side_source or "")
                and "fade_native" not in (side_source or "")
                and bool(self.config.get("disable_buy_no_15m_native", False))
            ):
                # 2026-07-26 Codex smoke-review fix: require "15m_native" in side_source so
                # this cuts ONLY native htf-aligned 15m shorts (doge_15m_native / hype_15m_native)
                # — NOT non-native shorts (override / vs_slower / neutral). This gate runs
                # BEFORE the flips, so a native short's source is a clean "*_15m_native" with no
                # flip suffix; tape-driven FLIP shorts (the +EV cohort) get their suffix later and
                # are handled by _post_flip_disabled_side, which intentionally admits them.
                _bump_skip("buy_no_15m_native_suppressed")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=allowed_side,
                    action=action,
                    reason="buy_no_15m_native_suppressed",
                    yes_price=yes_price,
                    htf_bias=primary_htf_bias,
                    context={"side_source": side_source},
                )
                continue

            if (
                is_updown
                and _updown_tf == "5m"
                and action == "BUY_NO"
                and self._should_suppress_native_5m_buy_no()
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
            # --- last-60s tape veto (2026-07-13 operator GO action 1, Codex GO) ---
            # Never-green autopsy: 22 losses -$96 (32% of trades) entered AGAINST an
            # in-flight ~1-minute move (shorts into bounces, longs into dumps), all on
            # fresh feeds. 1m SPIKEs have 80-94% direction accuracy (06-30 finding).
            # NOTE (Codex placement caveat): this sits after fade/side resolution and
            # the enabled assets run window_delta_flip disabled, so `action` is final
            # here — this is a final-side veto, not a pre-flip one.
            # 2-slot pulse history on the instance (reload resets = one blind pulse, ok).
            _tv_win = _updown_tf if is_updown else "15m"
            # 2026-07-17 STAGED per-window port (restart-only; operator GO). tape_veto_60s
            # was a FLAT per-strategy key: ONE bps threshold for 5m/15m/1h. Live evidence:
            # it kills 34% of sol 1h candidates but only 0-1% of sol 5m/15m — i.e. an
            # asset-wide gate suppressing ONE window its siblings do not need. Per-window
            # keys fall back to the flat key when unset, so every other asset/window is
            # byte-identical in behaviour. Set tape_veto_60s_bps_1h (or _enabled_1h) to
            # scope sol 1h without weakening the 5m engine the veto protects.
            if bool(self.config.get(f"tape_veto_60s_enabled_{_tv_win}",
                                   self.config.get("tape_veto_60s_enabled", False))):
                try:
                    import time as _tv_time
                    _tv_now = _tv_time.time()
                    _tv_px = float(getattr(sol, "current_price", 0.0) or 0.0)
                    _tv_hist = getattr(self, "_tape_veto_hist", [])
                    if _tv_px > 0 and (not _tv_hist or _tv_now - _tv_hist[-1][1] >= 45.0):
                        _tv_hist = (_tv_hist + [(_tv_px, _tv_now)])[-2:]
                        self._tape_veto_hist = _tv_hist
                    _tv_ref = _tv_hist[-2] if len(_tv_hist) >= 2 else None
                    if _tv_ref and _tv_px > 0 and 30.0 <= _tv_now - _tv_ref[1] <= 180.0:
                        _tv_bps = (_tv_px - _tv_ref[0]) / _tv_ref[0] * 10000.0
                        _tv_thr = float(self.config.get(f"tape_veto_60s_bps_{_tv_win}",
                                                        self.config.get("tape_veto_60s_bps", 20.0)) or 20.0)
                        if (action == "BUY_NO" and _tv_bps > _tv_thr) or (
                            action == "BUY_YES" and _tv_bps < -_tv_thr
                        ):
                            _bump_skip("tape_veto_60s")
                            _log_skip_reject(
                                market=market,
                                window=_updown_tf if is_updown else "15m",
                                side=allowed_side,
                                action=action,
                                reason="tape_veto_60s",
                                yes_price=yes_price,
                                htf_bias=primary_htf_bias,
                                context={"tape_bps_60s": round(_tv_bps, 1), "thr": _tv_thr},
                            )
                            continue
                except Exception:
                    pass
            # 2026-07-22 xrp 5m RISING-MOMENTUM CONFIRM (operator GO, Codex).
            # The bias-aligned bypass below SKIPS momentum-confirm for bias-aligned
            # entries; in chop this let xrp 5m WHIPSAW — bought the top (LONG, 5m
            # hist rising=False) at 00:20 and shorted the bottom (SHORT, 5m hist
            # ~-0.0003 then reversed +0.05% one min later) at 01:26 = 0W/2L -$26.32.
            # For xrp 5m ONLY (per-strategy config key => only xrp carries it, and
            # only when _updown_tf==5m), require the OWN 5m histogram to be moving in
            # the entry direction WITH magnitude, EVEN on bias-aligned entries. A
            # cross in-direction always qualifies. Trade both would have been blocked:
            # LONG rising=False -> fail; SHORT |hist|<floor (flat) -> fail. Default OFF.
            if (
                is_updown
                and _updown_tf == "5m"
                and bool(self.config.get("require_rising_momentum_confirm_5m", False))
            ):
                _rm = getattr(sol, "macd_5m", None)
                if _rm is not None:
                    _rm_hist = float(getattr(_rm, "histogram", 0.0) or 0.0)
                    _rm_rising = bool(getattr(_rm, "histogram_rising", False))
                    _rm_xover = getattr(_rm, "crossover", None)
                    _rm_floor = float(
                        self.config.get("rising_momentum_min_abs_hist_5m", 0.0) or 0.0
                    )
                    if action == "BUY_YES":
                        _rm_ok = (_rm_xover == "BULLISH_CROSS") or (
                            _rm_hist > _rm_floor and _rm_rising
                        )
                    else:  # BUY_NO
                        _rm_ok = (_rm_xover == "BEARISH_CROSS") or (
                            _rm_hist < -_rm_floor and not _rm_rising
                        )
                    if not _rm_ok:
                        _bump_skip("xrp_5m_no_rising_momentum")
                        _log_skip_reject(
                            market=market,
                            window="5m",
                            side=allowed_side,
                            action=action,
                            reason="xrp_5m_no_rising_momentum",
                            yes_price=yes_price,
                            htf_bias=primary_htf_bias,
                            context={
                                "hist_5m": round(_rm_hist, 5),
                                "rising": _rm_rising,
                                "floor": _rm_floor,
                                "xover": _rm_xover,
                            },
                        )
                        continue
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

            # 2026-07-21 TAPE ARBITRATION (operator GO; Codex design-reviewed). ROOT: the
            # momentum-confirm gate above runs ONLY when `not _bias_aligned`, so a bias-aligned
            # entry (LONG into a BULLISH 1h) is admitted with NO 5m/15m check -> the bot rides the
            # STALE 1h bias into a turned tape ("wrong direction into a regime change" — the live
            # failure this session: longs stopped as 5m/15m MACD turned bearish while 1h stayed bull).
            # CAVEAT (see comment ~3665): 5/22-5/27 ghost showed blocking bias-aligned trades BROADLY
            # was HARMFUL (+11-16pp WR), so this gate is CHOP-RESTRICTED: suppress a bias-aligned entry
            # ONLY when (a) the lane's own-TF MACD contradicts the side AND (b) the efficiency-ratio
            # regime is chop (er < tape_arbitration_er_chop_max) where the 1h bias is unreliable. In
            # trend (high er) the entry proceeds. shadow_only logs the counterfactual without blocking.
            # Config-gated, default OFF.
            if (
                self.config.get("tape_arbitration_enabled", False)
                and is_updown
                and (_bias_aligned_long or _bias_aligned_short)
                and _updown_tf in set(self.config.get("tape_arbitration_windows", ["5m", "15m"]) or [])
            ):
                _ta_own = (
                    sol.macd_5m if _updown_tf == "5m"
                    else sol.macd_15m if _updown_tf == "15m"
                    else sol.macd_1h
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
                        window=_updown_tf if is_updown else "15m",
                        side=allowed_side,
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
            if (
                is_updown and _updown_tf != "5m" and skip_15m_reason
                and not (_simple_band_long and allowed_side == "LONG")
            ):
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
            # ── UP/DOWN MARKETS (15m or 5m) ──
            if is_updown:
                # Deadzone / blocked-UTC-hour gate purged 2026-06-10.

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

                # NOTE: window-delta confirmation runs LATER, after the side is
                # finalized by apply_fresh_cross_override (the momentum/fresh-cross
                # flip). Checking here would pre-empt that flip and kill rising-tape
                # shorts the flip would correctly convert to longs. See
                # `_window_delta_disagrees` call sites below each override.

                # 2026-05-22: btc_min_move_dollars gate REMOVED (BTC must not gate alt
                # entry — "alts decided by alt-native indicators"). 2026-06-09: dropped
                # the leftover dead diagnostic computation (it only fed `if ...: pass`,
                # unused); config keys btc_min_move_dollars_5m/15m are now vestigial.

                # Skip only when our entry-side price is in the unfavorable long
                # tail (paying high premium against the market). The favorable
                # tail (market already agrees with our side) is left in — ghost
                # log 2026-05-27 shows our-side price >= 0.80 wins 87–97% across
                # ~6k settled rejections; symmetric reject was throwing them out.
                _sample("entry_price", yes_price)
                _our_price = (1.0 - yes_price) if action == "BUY_NO" else yes_price
                # 2026-07-08 operator REMOVE price_too_far_from_even: config-driven,
                # DEFAULT 0.0 (disabled). LIVE data: cheap entries 0.20-0.35 = +$106 WIN
                # while near-0.50 = -$555; the gate was blocking the profitable tail.
                # Set price_too_far_min_our_price: 0.12 to restore.
                _ptf_min = float(self.config.get("price_too_far_min_our_price", 0.0))
                if _ptf_min > 0.0 and _our_price < _ptf_min:
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
                        pass

                # Alt 1H alignment is a trade input; BTC context is not.
                _h1_trend = mtt.h1_trend  # "BULLISH", "BEARISH", or "NEUTRAL"
                _alt_1h_block_reason = self._alt_1h_alignment_blocks_entry(
                    action=action,
                    window_size=_updown_tf if is_updown else "15m",
                    alt_1h_trend=_h1_trend,
                )
                if _alt_1h_block_reason:
                    _bump_skip(_alt_1h_block_reason)
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf if is_updown else "15m",
                        side=allowed_side,
                        action=action,
                        reason=_alt_1h_block_reason,
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={
                            "alt_1h_trend": _h1_trend,
                            "window_size": _updown_tf if is_updown else "15m",
                            "side_source": side_source,
                        },
                    )
                    logger.info(
                        "  %s skip %s on '%s' — alt 1H=%s blocks 5m BUY_YES",
                        self._signal_strategy_name,
                        action,
                        market.question[:40],
                        _h1_trend,
                    )
                    continue
                if self.enforce_alt_1h_alignment:
                    # 2026-08-04 (operator GO): the alignment gate was DIAGNOSTIC-ONLY — it appended a
                    # reason and LET THE TRADE THROUGH ("a guard that only writes a comment"). LIVE PROOF
                    # (sess 175827 ops_pulse): all 6 lanes went SHORT into a BULLISH btc/market because the
                    # alts' native 15m/5m were bearish and the 1h (NEUTRAL) didn't block. When
                    # alt_1h_require_confirm is set, actually SKIP a native entry the alt's OWN 1h does not
                    # confirm: a short (BUY_NO) needs 1H BEARISH, a long (BUY_YES) needs 1H BULLISH — a
                    # NEUTRAL/opposing 1H blocks it. Alt-native (NOT btc). Hot-reloadable (self.config.get).
                    # Reversible (false). NOTE: this reduces frequency (neutral-1H entries are blocked).
                    # 2026-08-04 loosen: alt_1h_allow_neutral=true blocks ONLY an OPPOSING 1H
                    # (short w/ 1H BULLISH, long w/ 1H BEARISH); a NEUTRAL 1H passes. Keeps the
                    # anti-short-into-bull protection but un-starves neutral tape (was 115 skips/30min,
                    # 0 entries). Reversible (false = original require-confirm behavior).
                    _need_1h = "BEARISH" if action == "BUY_NO" else "BULLISH"
                    _oppose_1h = "BULLISH" if action == "BUY_NO" else "BEARISH"
                    _h1u = str(_h1_trend or "NEUTRAL").upper()
                    _blocked_1h = (
                        (_h1u == _oppose_1h)
                        if bool(self.config.get("alt_1h_allow_neutral", False))
                        else (_h1u != _need_1h)
                    )
                    if (
                        bool(self.config.get("alt_1h_require_confirm", False))
                        and _blocked_1h
                    ):
                        reason_parts.append(
                            f"alt_1h_unconfirmed_{action.lower()}_{str(_h1_trend or 'neutral').lower()}"
                        )
                        _bump_skip("alt_1h_unconfirmed")  # Codex: count it so starvation is measurable
                        logger.info(
                            "  %s SKIP %s on '%s' — own 1H=%s does not confirm (need %s)",
                            self._signal_strategy_name, action, market.question[:40],
                            _h1_trend, _need_1h,
                        )
                        continue
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
                _own_rsi, _own_macd = self._own_tf_rsi_macd(sol, _updown_tf if is_updown else "15m")
                _rsi_hard_block, _rsi_soft_delta, rsi_min_edge_add = self._resolve_rsi_gate(
                    action,
                    _own_rsi,
                    macd=_own_macd,
                    window=_updown_tf if is_updown else "15m",
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
                # 2026-07-13 C (operator GO): per-candidate feed-state breadcrumb —
                # joins entries to oracle basis/age for staleness analysis.
                try:
                    _cl_upd = getattr(sol, "chainlink_updated_at", None)
                    _cl_age = None
                    if _cl_upd is not None:
                        try:
                            _cl_age = (datetime.now(timezone.utc) - _cl_upd).total_seconds()
                        except TypeError:  # naive timestamp from the service — treat as UTC
                            # 2026-07-30 FEEDSTATE TZ FIX (Codex): was datetime.now() = LOCAL
                            # (Pacific UTC-7) minus a naive-UTC oracle ts, yielding a bogus
                            # ~-25170s (-7h = the TZ offset). Mirror _freshness_seconds: treat
                            # the naive ts as UTC. Diagnostic-only (the real gate already
                            # clamps via _freshness_seconds); this cleans the staleness log/calib.
                            _cl_age = (datetime.now(timezone.utc) - _cl_upd.replace(tzinfo=timezone.utc)).total_seconds()
                    logger.info(
                        "FEEDSTATE strat=%s mkt=%s tf=%s act=%s basis_bps=%s oracle_age_s=%s ref_spot=%s passed=%s",
                        self._signal_strategy_name, market.id,
                        _updown_tf if is_updown else "15m", action,
                        getattr(oracle_validation, "basis_bps", None),
                        (round(_cl_age) if _cl_age is not None else None),
                        bool(self.config.get("oracle_ref_use_exchange_spot", False)),
                        oracle_validation.passed,
                    )
                except Exception:
                    pass
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
                    _h1_resolved = str(getattr(mtt, "h1_trend", "") or "").upper()
                    # 2026-07-20 B3 (operator GO, Codex-reviewed): the histogram trigger below
                    # MISSES the case this damp exists for — a cleanly resolved BULLISH 1H with a
                    # flat/non-rising histogram leaves _h1_bear_ok True, so a SHORT into a bullish
                    # 1H gets NO damp at all. eth_macro was already upgraded to key on the resolved
                    # mtt.h1_trend (eth_macro.py:1621/1732); this ports that to the sol family.
                    # Parity with ETH: when the resolved 1H is BULLISH/BEARISH it decides the
                    # damp (active opposition only). NEUTRAL/unknown keeps the baseline
                    # histogram damp. Config-gated via self.config, which each subclass
                    # repoints at its own strategies.<asset> block (xrp_macro.py:59,
                    # hype:84, doge:81, bnb:107), so the flag is per-asset. Default OFF.
                    # NOTE: whether a given asset is enabled is a CONFIG decision — this code
                    # does not and cannot special-case any asset.
                    if bool(self.config.get("alt_h1_resolved_trend_damp", False)) and _h1_resolved in ("BULLISH", "BEARISH"):
                        # Resolved 1H has an actual direction -> it decides.
                        _h1_bull_ok = _h1_resolved != "BEARISH"
                        _h1_bear_ok = _h1_resolved != "BULLISH"
                    else:
                        # Flag off, OR 1H is NEUTRAL/unknown -> keep the baseline histogram
                        # damp. 2026-07-20 (Codex catch): letting NEUTRAL skip the damp
                        # entirely was an unrequested LOOSENING — old logic damped a LONG on a
                        # neutral 1H with a bearish histogram, and that must be preserved.
                        _h1_bull_ok = _macd_1h.histogram_rising or _macd_1h.histogram > 0
                        _h1_bear_ok = (not _macd_1h.histogram_rising) or _macd_1h.histogram < 0
                    if self.enforce_alt_1h_alignment:
                        if allowed_side == "LONG" and not _h1_bull_ok:
                            est_prob_up -= 0.04
                            reason_parts.append("h1_dampen_long_5m")
                            logger.info(
                                f"  {_alt_label} [5m] allow '{market.question[:40]}' — "
                                f"1H against LONG, est_prob dampened -0.04 "
                                f"(h1={_h1_resolved} hist={_macd_1h.histogram:.4f})"
                            )
                        if allowed_side == "SHORT" and not _h1_bear_ok:
                            est_prob_up += 0.04
                            reason_parts.append("h1_dampen_short_5m")
                            logger.info(
                                f"  {_alt_label} [5m] allow '{market.question[:40]}' — "
                                f"1H against SHORT, est_prob dampened +0.04 "
                                f"(h1={_h1_resolved} hist={_macd_1h.histogram:.4f})"
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
                        # 2026-06-07: HYPE-only (config-gated, default off) soft-penalty
                        # for 5m SHORT. Ghost: HYPE weak_5m_signal (all SHORT) n=981
                        # +6.5% EV / 56% win / median +0.08 — a real, if thin, short edge
                        # the hard reject was killing. Codex-reviewed: short-only, small,
                        # exits/price-bounds unchanged. Same penalty template as dip path.
                        _soft_short = (
                            action == "BUY_NO"
                            and bool(self.config.get("weak_5m_signal_soft_penalty_short", False))
                        )
                        if _is_dip_path or _soft_short:
                            _dip_penalty = 0.02  # est_prob shrink toward 0.5
                            if allowed_side == "LONG":
                                est_prob_up -= _dip_penalty
                            else:
                                est_prob_up += _dip_penalty
                            reason_parts.append(
                                f"weak_5m_penalty{'_dip' if _is_dip_path else '_short'}(m5_adj={m5_adj:+.2f})"
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
                                        macd_hist_5m=getattr(getattr(sol, "macd_5m", None), "histogram", None),
                                        macd_hist_15m=getattr(getattr(sol, "macd_15m", None), "histogram", None),
                                        macd_hist_1h=getattr(getattr(sol, "macd_1h", None), "histogram", None),
                                        rsi_5m=getattr(getattr(sol, "tf_5m", None), "rsi_14", None),
                                        rsi_15m=getattr(getattr(sol, "tf_15m", None), "rsi_14", None),
                                        rsi_1h=getattr(getattr(sol, "tf_1h", None), "rsi_14", None),
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
                        logger=logger, enabled=(not _fade_active) and self.config.get("fresh_cross_override", True),
                        # 2026-06-08: 5m market -> 5m RSI (own-window/faster-lead), not
                        # the 15m canonical. No-op today (flip is 1h-gated) but correct
                        # for when the flip is extended to the 5m window.
                        rsi_14=getattr(getattr(ta.sol, "tf_5m", None), "rsi_14", None), window="5m",
                        momentum_flip_enabled=(not _fade_active) and self.config.get("rsi_momentum_flip_1h", False),
                        macd_hist_5m=getattr(macd_5m, "histogram", None),
                        macd_flip_enabled=(not _fade_active) and self.config.get("macd_momentum_flip_5m15m", False),
                        macd_flip_long_to_short_enabled=(not _fade_active) and self.config.get("macd_momentum_flip_long_to_short", False),
                    )

                    # 2026-07-03 FROZEN-EST FIX (sol-family 5m): blend continuous window-delta
                    # P(up) into the step-built est. Opt-in via window_delta_est_weight_<tf>.
                    _wd_w = float(self.config.get(
                        f"window_delta_est_weight_{_updown_tf}",
                        self.config.get("window_delta_est_weight", 0.0),
                    ) or 0.0)
                    if _wd_w > 0.0:
                        try:
                            from src.analysis.window_delta import evaluate_window_delta as _ewd
                            _wde = _ewd(sol, _updown_tf, float(_eval_left or 0.0))
                            if _wde is not None and _wde[1] is not None:
                                est_prob_up = (1.0 - _wd_w) * est_prob_up + _wd_w * float(_wde[1])
                                reason_parts.append(f"wd_est_blend={float(_wde[1]):.3f}x{_wd_w:.2f}")
                        except Exception:
                            pass
                    est_prob_up = max(0.10, min(0.90, est_prob_up))
                    raw_est_prob = est_prob_up
                    # Window-delta confirmation — side is FINAL here (post-flip).
                    _wd_flip = None if _fade_active else self._window_delta_flip(
                        sol, _updown_tf, _eval_left, action,
                        primary_htf_bias=primary_htf_bias, alt_htf_bias=mtt.h1_trend,
                    )
                    if _wd_flip is not None:
                        action, allowed_side, direction, est_prob_up, _wd_prob = _wd_flip
                        raw_est_prob = est_prob_up
                        side_source = f"{side_source or ''}+window_delta_flip"
                        reason_parts.append(f"window_delta_flip->{action}({_wd_prob:.3f})")
                    # Re-apply per-window sit-out post-flip: window_delta_flip can turn a
                    # native long into a BUY_NO (or vice-versa), bypassing the pre-flip
                    # disable_buy_no_<tf> / disable_buy_yes_<tf> gate. (2026-06-16 fix.)
                    if is_updown:
                        _postflip_reason = self._post_flip_disabled_side(
                            action, _updown_tf, side_source
                        )
                        if _postflip_reason:
                            _bump_skip(_postflip_reason)
                            _log_skip_reject(
                                market=market, window=_updown_tf, side=allowed_side,
                                action=action, reason=_postflip_reason, yes_price=yes_price,
                                htf_bias=primary_htf_bias,
                                context={"side_source": side_source},
                            )
                            continue
                    # Low-ATR volatility gate — configured losing lanes only trade in
                    # low vol; mid/high-ATR is where they bleed (-13% EV). Side final.
                    _atr_block = self._low_atr_gate_blocks(sol, _updown_tf, action)
                    if _atr_block is not None:
                        _atr_pct, _atr_thr = _atr_block
                        _bump_skip("low_atr_gate_skip")
                        _log_skip_reject(
                            market=market, window=_updown_tf, side=allowed_side,
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
                    if (not _fade_active) and self._directional_flip_enabled():
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

                    # 5m BUY_NO inversion flip (forward-test 2026-06-11). The 5m
                    # short side is anti-selective for this lane (held-to-resolution
                    # WR ~30%); the cheap long is +EV on the same markets. The
                    # candidate already cleared every short-side gate above, so we
                    # redirect it to the long using the complement of the native
                    # est_prob (which was built to justify the short). The edge gate
                    # below then admits only the cheap longs. Opt-in per strategy via
                    # buy_no_5m_flip_to_yes (enabled for hype only).
                    if (
                        (not _fade_active)
                        and bool(self.config.get("buy_no_5m_flip_to_yes", False))
                        and action == "BUY_NO"
                    ):
                        estimated_prob = max(1.0 - float(estimated_prob), 0.50)
                        action = "BUY_YES"
                        direction = "UP"
                        allowed_side = "LONG"
                        side_source = f"{side_source or ''}+buy_no_5m_to_yes_flip"
                        reason_parts.append("buy_no_5m_to_yes_flip")

                    # 2026-07-30 WRONG-DIRECTION FIX (P0): re-gate the FINAL post-flip action. The gate above
                    # ran on the pre-flip side; flips (fresh-cross/window-delta/posterior/5m-to-yes) can turn a
                    # long into a BUY_NO oversold short the exhaustion gate never saw. Mirrors the post-flip
                    # _post_flip_disabled_side re-check.
                    _pf_rsi, _pf_macd = self._own_tf_rsi_macd(sol, _updown_tf if is_updown else "15m")
                    _pf_hard, _, rsi_min_edge_add = self._resolve_rsi_gate(
                        action,
                        _pf_rsi,
                        macd=_pf_macd,
                        window=_updown_tf if is_updown else "15m",
                    )
                    if _pf_hard:
                        _bump_skip("rsi_hard_blocked_postflip")
                        continue

                    _byn_floor_5m = self._alt_buy_yes_bullish_floor_bump(
                        window_size="5m", action=action, htf_bias=mtt.h1_trend,
                        yes_price=yes_price, rsi_14=sol.rsi_14,
                        raw_est_prob=raw_est_prob,
                    )
                    if _byn_floor_5m > 0:
                        estimated_prob = min(0.90, estimated_prob + _byn_floor_5m)
                        reason_parts.append(f"5m_buy_yes_floor=+{_byn_floor_5m:.2f}")

                    _adm = self._admission_prob(
                        estimated_prob,
                        window_size="5m",
                        action=action,
                    )
                    if action == "BUY_YES":
                        edge = _adm - yes_price
                    else:
                        edge = (1.0 - _adm) - (1.0 - yes_price)
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
                                    macd_hist_5m=getattr(getattr(sol, "macd_5m", None), "histogram", None),
                                    macd_hist_15m=getattr(getattr(sol, "macd_15m", None), "histogram", None),
                                    macd_hist_1h=getattr(getattr(sol, "macd_1h", None), "histogram", None),
                                    rsi_5m=getattr(getattr(sol, "tf_5m", None), "rsi_14", None),
                                    rsi_15m=getattr(getattr(sol, "tf_15m", None), "rsi_14", None),
                                    rsi_1h=getattr(getattr(sol, "tf_1h", None), "rsi_14", None),
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
                    _h1_resolved = str(getattr(mtt, "h1_trend", "") or "").upper()
                    # 2026-07-20 B3 (see the 5m site above for the full rationale): resolved
                    # mtt.h1_trend instead of the histogram sign, ETH parity, damp only on ACTIVE
                    # opposition. Config-gated, default OFF.
                    if bool(self.config.get("alt_h1_resolved_trend_damp", False)) and _h1_resolved in ("BULLISH", "BEARISH"):
                        # Resolved 1H has an actual direction -> it decides.
                        _h1_bull_ok = _h1_resolved != "BEARISH"
                        _h1_bear_ok = _h1_resolved != "BULLISH"
                    else:
                        # Flag off, OR 1H is NEUTRAL/unknown -> keep the baseline histogram
                        # damp. 2026-07-20 (Codex catch): letting NEUTRAL skip the damp
                        # entirely was an unrequested LOOSENING — old logic damped a LONG on a
                        # neutral 1H with a bearish histogram, and that must be preserved.
                        _h1_bull_ok = _macd_1h.histogram_rising or _macd_1h.histogram > 0
                        _h1_bear_ok = (not _macd_1h.histogram_rising) or _macd_1h.histogram < 0
                    if self.enforce_alt_1h_alignment:
                        if allowed_side == "LONG" and not _h1_bull_ok:
                            est_prob_up -= h1_dampen
                            reason_parts.append(f"h1_dampen_long_{window_label}")
                            logger.info(
                                f"  {_alt_label} [{window_label}] allow '{market.question[:40]}' — "
                                f"1H against LONG, est_prob dampened -{h1_dampen:.2f} "
                                f"(h1={_h1_resolved} hist={_macd_1h.histogram:.4f})"
                            )
                        if allowed_side == "SHORT" and not _h1_bear_ok:
                            est_prob_up += h1_dampen
                            reason_parts.append(f"h1_dampen_short_{window_label}")
                            logger.info(
                                f"  {_alt_label} [{window_label}] allow '{market.question[:40]}' — "
                                f"1H against SHORT, est_prob dampened +{h1_dampen:.2f} "
                                f"(h1={_h1_resolved} hist={_macd_1h.histogram:.4f})"
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

                    # 2026-07-03 FROZEN-EST FIX (sol 15m): blend the continuous
                    # window-delta P(up) — the tape's own read of this window —
                    # into the step-built est. Opt-in per window via
                    # window_delta_est_weight_<tf> (default 0 = unchanged).
                    _wd_w = float(self.config.get(
                        f"window_delta_est_weight_{window_label}",
                        self.config.get("window_delta_est_weight", 0.0),
                    ) or 0.0)
                    if _wd_w > 0.0:
                        try:
                            _wd_ml = 0.0
                            if market.end_date:
                                _wd_end = market.end_date
                                if _wd_end.tzinfo is None:
                                    _wd_end = _wd_end.replace(tzinfo=timezone.utc)
                                _wd_ml = max(0.0, (_wd_end - datetime.now(timezone.utc)).total_seconds() / 60.0)
                            _wde = evaluate_window_delta(sol, window_label, _wd_ml)
                            if _wde is not None and _wde[1] is not None:
                                est_prob_up = (1.0 - _wd_w) * est_prob_up + _wd_w * float(_wde[1])
                                reason_parts.append(f"wd_est_blend={float(_wde[1]):.3f}x{_wd_w:.2f}")
                        except Exception:
                            pass  # fail-open: est stays as built

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
                        logger=logger, enabled=(not _fade_active) and self.config.get("fresh_cross_override", True),
                        # 2026-06-08: window-aware faster-lead RSI (1h->15m, 15m->5m).
                        rsi_14=getattr(getattr(ta.sol, "tf_15m" if window_label == "1h" else "tf_5m", None), "rsi_14", None), window=window_label,
                        momentum_flip_enabled=(not _fade_active) and self.config.get("rsi_momentum_flip_1h", False),
                        macd_hist_5m=getattr(getattr(ta.sol, "macd_5m", None), "histogram", None),
                        macd_flip_enabled=(not _fade_active) and self.config.get("macd_momentum_flip_5m15m", False),
                        macd_flip_long_to_short_enabled=(not _fade_active) and self.config.get("macd_momentum_flip_long_to_short", False),
                    )

                    est_prob_up = max(0.10, min(0.90, est_prob_up))
                    raw_est_prob = est_prob_up
                    # Window-delta confirmation — side is FINAL here (post-flip).
                    _wd_flip = None if _fade_active else self._window_delta_flip(
                        sol, _updown_tf, _eval_left, action,
                        primary_htf_bias=primary_htf_bias, alt_htf_bias=mtt.h1_trend,
                    )
                    if _wd_flip is not None:
                        action, allowed_side, direction, est_prob_up, _wd_prob = _wd_flip
                        raw_est_prob = est_prob_up
                        side_source = f"{side_source or ''}+window_delta_flip"
                        reason_parts.append(f"window_delta_flip->{action}({_wd_prob:.3f})")
                    # Re-apply per-window sit-out post-flip: window_delta_flip can turn a
                    # native long into a BUY_NO (or vice-versa), bypassing the pre-flip
                    # disable_buy_no_<tf> / disable_buy_yes_<tf> gate. (2026-06-16 fix.)
                    if is_updown:
                        _postflip_reason = self._post_flip_disabled_side(
                            action, _updown_tf, side_source
                        )
                        if _postflip_reason:
                            _bump_skip(_postflip_reason)
                            _log_skip_reject(
                                market=market, window=_updown_tf, side=allowed_side,
                                action=action, reason=_postflip_reason, yes_price=yes_price,
                                htf_bias=primary_htf_bias,
                                context={"side_source": side_source},
                            )
                            continue
                    # Low-ATR volatility gate — configured losing lanes only trade in
                    # low vol; mid/high-ATR is where they bleed (-13% EV). Side final.
                    _atr_block = self._low_atr_gate_blocks(sol, _updown_tf, action)
                    if _atr_block is not None:
                        _atr_pct, _atr_thr = _atr_block
                        _bump_skip("low_atr_gate_skip")
                        _log_skip_reject(
                            market=market, window=_updown_tf, side=allowed_side,
                            action=action, reason="low_atr_gate_skip", yes_price=yes_price,
                            htf_bias=primary_htf_bias,
                            context={"atr_pct": round(_atr_pct, 6), "atr_threshold": _atr_thr},
                        )
                        continue
                    # 2026-07-30 WRONG-DIRECTION FIX (P0): re-gate the FINAL post-flip action. The gate above
                    # ran on the pre-flip side; flips (fresh-cross/window-delta/posterior/5m-to-yes) can turn a
                    # long into a BUY_NO oversold short the exhaustion gate never saw. Mirrors the post-flip
                    # _post_flip_disabled_side re-check.
                    _pf_rsi, _pf_macd = self._own_tf_rsi_macd(sol, _updown_tf)
                    _pf_hard, _, rsi_min_edge_add = self._resolve_rsi_gate(
                        action,
                        _pf_rsi,
                        macd=_pf_macd,
                        window=_updown_tf,
                    )
                    if _pf_hard:
                        _bump_skip("rsi_hard_blocked_postflip")
                        continue
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
                        window_size=window_label, action=action, htf_bias=mtt.h1_trend,
                        yes_price=yes_price, rsi_14=sol.rsi_14,
                        raw_est_prob=raw_est_prob,
                    )
                    if _byn_floor > 0:
                        estimated_prob = min(0.90, estimated_prob + _byn_floor)
                        reason_parts.append(f"{window_label}_buy_yes_floor=+{_byn_floor:.2f}")

                    _adm = self._admission_prob(
                        estimated_prob,
                        window_size=window_label,
                        action=action,
                    )
                    if action == "BUY_YES":
                        edge = _adm - yes_price
                    else:
                        edge = (1.0 - _adm) - (1.0 - yes_price)
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

                _own_rsi, _own_macd = self._own_tf_rsi_macd(sol, _updown_tf)
                _rsi_hard_block, _rsi_soft_delta, rsi_min_edge_add = self._resolve_rsi_gate(
                    action,
                    _own_rsi,
                    macd=_own_macd,
                    window=_updown_tf,
                )
                if _rsi_hard_block:
                    _bump_skip("rsi_hard_blocked")
                    logger.info(
                        f"  {self._signal_strategy_name} skip {action} on '{market.question[:40]}' — "
                        f"own-tf({_updown_tf}) RSI={float(_own_rsi):.1f} hit RSI/exhaustion gate"
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

                _adm = self._admission_prob(
                    estimated_prob,
                    window_size="15m",
                    action=action,
                )
                if action == "BUY_YES":
                    edge = _adm - yes_price
                else:
                    edge = (1.0 - _adm) - (1.0 - yes_price)
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
                _ai_updown_observe_only = bool(
                    self.config.get("ai_updown_observe_only", False)
                )
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
                    and not self._admit_marginal_quant_short(
                        edge, allowed_side, _timing_window_open,
                        window=(_updown_tf if is_updown else "15m"),
                    )
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
                    if False:  # 2026-07-19 AI-RESTORE (operator: complete revert INCLUDES the AI layer). The 07-15 observe-only skip is DISABLED so the alt-family AI call (else branch) always runs = evaluated + logged. Observe-only still means NON-GATING (every veto below stays observe_only-guarded). Latency bounded by max_ai_calls_per_scan, NOT by skipping the call.
                        # (2026-07-15 CYCLE-LAG skip — now dead code under if False): observe-only => AI verdict is logged-only
                        # and never gates (every veto below is observe_only-guarded, edge
                        # is not bumped, hold-cache is never written). Skip the ~10s
                        # blocking call (and the downstream shadow-pipeline await).
                        # `ai_calls += 1` below is preserved so the budget continue and
                        # the marginal admitted set stay byte-identical.
                        ai_decision = None
                    else:
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
                        if not _ai_updown_observe_only:
                            _bump_skip("ai_decision_timeout_marginal_threshold")
                            _log_ai_veto("ai_decision_timeout_marginal_threshold")
                            continue
                    ai_used = True
                    ai_analysis = (
                        ai_decision.direct_analysis if ai_decision is not None else None
                    )
                    # veto-only marginal pass: central layer already cleared this
                    # (no confident opposition) — admit on quant terms, skip the
                    # redundant local HOLD/supports/confidence/edge re-gate.
                    _mpass = (
                        ai_decision is not None
                        and ai_decision.reason in ("direct_ai_marginal_pass", "direct_ai_marginal_confirm")  # 2026-07-03: accept new fail-closed confirm reason
                    )
                    # Log reasoning so we can audit what the model is actually deciding
                    if ai_decision is not None and ai_analysis:
                        logger.info(
                            f"  {self._signal_strategy_name} AI decision [{ai_decision.action} "
                            f"conf={ai_decision.confidence:.2f} edge={float(ai_decision.edge or 0.0):.4f}] "
                            f"'{market.question[:45]}' | {ai_analysis.reasoning[:120]}"
                        )
                    if ai_decision is not None and not ai_decision.approved:
                        if not _ai_updown_observe_only:
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
                        if not _ai_updown_observe_only:
                            _bump_skip("ai_none_marginal_threshold")
                            _log_ai_veto("ai_none_marginal_threshold")
                            continue
                    if (
                        ai_decision is not None
                        and not _mpass
                        and ai_decision.action == "HOLD"
                    ):
                        if not _ai_updown_observe_only:
                            self._ai_hold_cache[market.id] = time.time()
                            _bump_skip("ai_hold_marginal_threshold")
                            _log_ai_veto("ai_hold_marginal_threshold")
                            logger.debug(f"{_brand}: AI says HOLD on '{market.question[:40]}...' — veto cached {self.ai_hold_veto_ttl_sec}s")
                            continue
                    if (
                        ai_decision is not None
                        and not _mpass
                        and not ai_recommendation_supports_action(
                        ai_decision.action, action
                        )
                    ):
                        if not _ai_updown_observe_only:
                            _bump_skip("ai_veto_marginal_threshold")
                            _log_ai_veto("ai_veto_marginal_threshold", ai_action=str(ai_decision.action))
                            logger.debug(
                                f"{_brand}: AI {ai_decision.action} conflicts with {action} "
                                f"on '{market.question[:40]}...'"
                            )
                            continue
                    if (
                        ai_decision is not None
                        and not _mpass
                        and ai_decision.confidence < self.ai_confidence_threshold
                    ):
                        if not _ai_updown_observe_only:
                            _bump_skip("ai_low_confidence_marginal_threshold")
                            _log_ai_veto("ai_low_confidence_marginal_threshold", ai_confidence=float(ai_decision.confidence))
                            logger.debug(
                                f"{_brand}: AI confidence {ai_decision.confidence:.2f} "
                                f"< {self.ai_confidence_threshold} marginal '{market.question[:40]}...'"
                            )
                            continue
                    ai_edge = float(ai_decision.edge or 0.0) if ai_decision is not None else 0.0
                    if ai_decision is not None and not _mpass and ai_edge <= 0:
                        if not _ai_updown_observe_only:
                            _bump_skip("ai_nonpositive_edge_marginal_threshold")
                            _log_ai_veto("ai_nonpositive_edge_marginal_threshold", ai_edge=ai_edge)
                            logger.debug(
                                f"{_brand}: non-positive ai_edge={ai_edge:.4f} marginal "
                                f"'{market.question[:40]}...'"
                            )
                            continue
                    if not _ai_updown_observe_only and ai_decision is not None:
                        edge = max(edge, ai_edge)
                        confidence = max(confidence, ai_decision.confidence)
                        reason_parts.append(f"ai_decision={ai_decision.source}")
                    if (
                        ai_decision is not None
                        and ai_analysis is not None
                        and
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
            # 2026-06-16: per-window BUY_YES entry-timing DEAD-ZONE. Some lanes are +EV at
            # the timing tails but -EV in a mid-window band — hype 15m: <5min +0.36 / >=15min
            # +0.02, but 5-12min -0.18 to -0.35 (those mid-window longs get stopped near
            # expiry). Skip BUY_YES when skip_lo <= mins_left < skip_hi. Opt-in (keys unset=off).
            _sz_lo = self.config.get(f"buy_yes_{_updown_tf}_skip_mins_lo")
            _sz_hi = self.config.get(f"buy_yes_{_updown_tf}_skip_mins_hi")
            if (
                is_updown and action == "BUY_YES"
                and _sz_lo is not None and _sz_hi is not None
                and float(_sz_lo) <= _eval_left
                and _mins_left < float(_sz_hi)
            ):
                _bump_skip(f"buy_yes_{_updown_tf}_timing_deadzone")
                _log_skip_reject(
                    market=market, window=_updown_tf, side=allowed_side, action=action,
                    reason=f"buy_yes_{_updown_tf}_timing_deadzone",
                    yes_price=yes_price, htf_bias=primary_htf_bias,
                    context={
                        "eval_mins_left": float(_eval_left),
                        "mins_left": float(_mins_left),
                    },
                )
                continue
            if is_updown and _no_signal_gate.lane_blocked(self._signal_strategy_name, _updown_tf, action):
                _bump_skip("no_signal_gate")
                _log_skip_reject(
                    market=market,
                    window=_updown_tf,
                    side=allowed_side,
                    action=action,
                    reason="no_signal_gate",
                    yes_price=yes_price,
                    est_prob_up=estimated_prob,
                    htf_bias=primary_htf_bias,
                    context={"gate": "no_signal_per_lane"},
                )
                continue
            if is_updown and (_eval_left < lane_policy.entry_window_min or _eval_left > lane_policy.entry_window_max):
                _bump_skip("lane_entry_window")
                log_window_reject(
                    self.full_config, market=market, strategy=self._signal_strategy_name,
                    window=_updown_tf, side=allowed_side, action=action,
                    side_source=locals().get("side_source"),
                    eval_mins_left=_eval_left,
                    entry_window_min=lane_policy.entry_window_min,
                    entry_window_max=lane_policy.entry_window_max,
                    yes_price=yes_price, est_prob=estimated_prob, edge=edge,
                )
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
                # 2026-07-04 operator freq unlock: per-tf override so the 1h raise
                # can be trimmed without touching 15m (unset = legacy value).
                _unconf_edge = self.config.get(
                    f"min_edge_when_ltf_unconfirmed_{_updown_tf}",
                    self.min_edge_15m_when_ltf_unconfirmed,
                )
                effective_min_edge = max(
                    effective_min_edge, float(_unconf_edge)
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

            # DYNAMIC ADMISSION (2026-07-26 operator GO): replace the static per-lane
            # floors with a tape-aware delta. LOOSEN (negative) a winning+green lane to
            # reclaim frequency a frozen floor would cost; TIGHTEN (positive) a net-losing
            # never-green lane. Self-correcting via the same signal that drives sizing.
            # Guarded + defaults to 0.0 (no effect) when the state file/admission is off.
            try:
                _tape_adm = get_tape_admission_delta(
                    self._signal_strategy_name,
                    _updown_tf if is_updown else "15m",
                    lane_side,
                )
                if _tape_adm:
                    _pre_adm = effective_min_edge
                    # never below a small hard floor; bounded delta already clamped upstream
                    effective_min_edge = max(0.0, effective_min_edge + _tape_adm)
                    reason_parts.append(f"tape_adm={_tape_adm:+.3f}")
                    logger.debug(
                        "  %s tape_admission %s|%s|%s min_edge %.4f -> %.4f (%+.3f)",
                        _brand, self._signal_strategy_name, _updown_tf, lane_side,
                        _pre_adm, effective_min_edge, _tape_adm,
                    )
            except Exception:
                pass

            # 2026-07-26 (#3 candidate-time TAPE FRESHNESS — operator-directed, Codex
            # root-cause). A GRADED edge+size penalty for entries where the immediate
            # tape has rolled over against the side (own-TF MACD decelerating/reversed)
            # or the move is RSI-exhausted. Reacts on THIS candidate (unlike the
            # close-based adapter, which lags the turn) and NEVER hard-blocks (unlike
            # tape_arbitration), so it cannot re-choke frequency. Covers 1h too (#4 =
            # the eth-1h-up stale-bias leak). size_mult carried to final_size below.
            _freshness_size_mult = 1.0
            try:
                _fresh_tf = _updown_tf if is_updown else "15m"
                _fresh_macd = (
                    sol.macd_5m if _fresh_tf == "5m"
                    else sol.macd_1h if _fresh_tf == "1h"
                    else sol.macd_15m
                )
                _fresh = compute_freshness_penalty(
                    action=action,
                    own_macd=_fresh_macd,
                    rsi=getattr(sol, "rsi_14", None),
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

            # 2026-07-31 Phase-1: pocket-RSI soft penalty now comes from the consolidated
            # _resolve_rsi_gate (rsi_min_edge_add), computed on the FINAL post-flip action
            # above. Apply it here to the edge bar. Weak low-RSI BUY_NO shorts must clear
            # effective_min_edge + add; strong-edge continuation still passes.
            _pkt_soft_applied = False
            if rsi_min_edge_add > 0.0:
                effective_min_edge = float(effective_min_edge) + rsi_min_edge_add
                _pkt_soft_applied = True  # block the AI-off marginal bypass below
                reason_parts.append(f"pocket_rsi_soft={rsi_min_edge_add:.3f}")

            # Updown marginal (parity with BTC): quant edge just below bar — AI confirms action + edge
            _ai_updown_observe_only = bool(
                self.config.get("ai_updown_observe_only", False)
            )
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
                and not self._admit_marginal_quant_short(
                    edge, allowed_side, _timing_window_open,
                    window=(_updown_tf if is_updown else "15m"),
                )
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
                if False:  # 2026-07-19 AI-RESTORE (operator: complete revert INCLUDES the AI layer). 07-15 observe-only skip DISABLED; else-branch AI call always runs (evaluated + logged; observe-only still non-gating).
                    # (2026-07-15 CYCLE-LAG skip — now dead code under if False): observe-only veto-only path => verdict can
                    # only veto, and every veto here is observe_only-guarded, so under
                    # observe-only the ~10s call is pure dead latency. Skip it;
                    # `ai_calls += 1` below preserved so admission is byte-identical.
                    ai_decision = None
                else:
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
                    if not _ai_updown_observe_only:
                        _bump_skip("ai_decision_timeout")
                        continue
                ai_used = True
                ai2 = ai_decision.direct_analysis if ai_decision is not None else None
                if ai_decision is not None and not ai_decision.approved:
                    if not _ai_updown_observe_only:
                        _bump_skip(f"ai_decision_{ai_decision.reason}")
                        logger.debug(
                            f"{_brand}: AI decision rejected updown marginal "
                            f"{ai_decision.reason} action={ai_decision.action} "
                            f"conf={ai_decision.confidence:.2f}"
                        )
                        continue
                ae = float(ai_decision.edge or 0.0) if ai_decision is not None else 0.0
                if ae > 0 and not _ai_updown_observe_only and ai_decision is not None:
                    edge = max(edge, ae)
                    confidence = max(confidence, ai_decision.confidence)
                    reason_parts.append(f"ai_decision={ai_decision.source}")
                if ai2 is not None and ai_decision is not None:
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
                elif not _ai_updown_observe_only:
                    _bump_skip("ai_nonpositive_edge_marginal_updown")
            elif (
                is_updown
                # 2026-07-28: AI is OBSERVE-ONLY on every alt (config ai_updown_observe_only),
                # so its timing window must NOT block a marginal alt entry — observing != enforcing.
                # This mirrors the open-window path above, where observe-only alts are always
                # admitted because the AI cannot veto. Only enforcing lanes keep this gate; bitcoin
                # (the sole enforcing updown lane) runs a separate code path and is unaffected. In
                # June the alt _DECISION_GATE_WINDOWS was empty so alt AI never fired at all — a
                # 2026-07-03 change turned it on for alts, which is what re-introduced this block.
                and not _ai_updown_observe_only
                and edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and (_updown_tf if is_updown else "15m") in self._DECISION_GATE_WINDOWS
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
            _admit_marginal_no_ai = False if _pkt_soft_applied else self._admit_marginal_quant_short(
                edge, allowed_side, _timing_window_open,
                window=(_updown_tf if is_updown else "15m"),
            )
            _is_rsi_fade = bool(side_source and "rsi_fade" in side_source)
            if _simple_band_long and action == "BUY_YES":
                # Band-admitted consensus lane: est_prob is unusable, so don't gate on
                # it — size on the configured flat edge instead (sizing policy, not a
                # fabricated probability). Tag for ghost/trade split.
                effective_min_edge = 0.0
                if edge < self._a1hsl_sizing_edge:
                    edge = self._a1hsl_sizing_edge
                reason_parts.append("alt_1h_simple_long")
            # 2026-08-10 RSI-FADE min-edge/conviction EXEMPTION (mirrors _simple_band_long +
            # flip_exempt_min_edge). The rsi_fade lane DELIBERATELY takes the anti-est_prob
            # side in the coinflip band (project_rsi_fade_edge_found_2026_08_10): est_prob is
            # anti-predictive there, so the model edge (est-yes) is ~0/NEGATIVE by
            # construction and lane_min_edge + the est_prob conviction floors reject ~100% of
            # faded candidates (measured: 0 faded entries ever cleared the gate; 74 fade
            # resolutions -> 0 trades). est_prob is unusable on the fade side, so don't gate
            # on it — size on a flat fade edge (sizing policy, not a fabricated probability).
            # Direction-agnostic (works if overbought-fade is later enabled). Fully reverts
            # with risk.rsi_fade.enabled:false (=> _is_rsi_fade always False => byte-identical).
            # The conviction-floor skips below also key off _is_rsi_fade. NOTE: sol_macro scan
            # loop only (sol/xrp/hype/doge/bnb) — eth duplicates this loop => separate port.
            if _is_rsi_fade:
                _rf_cfg = (self.full_config.get("risk", {}) or {}).get("rsi_fade", {}) or {}
                _rf_flat_edge = float(_rf_cfg.get("flat_edge", 0.05) or 0.05)
                effective_min_edge = 0.0
                if edge < _rf_flat_edge:
                    edge = _rf_flat_edge
                reason_parts.append(f"rsi_fade_exempt({_rf_flat_edge:.3f})")
            # 2026-06-18: INVERTED-EDGE / tape-flip admit (DEFAULT OFF, per-lane
            # flip_exempt_min_edge_<tf> or global flip_exempt_min_edge). window_delta_flip
            # candidates are tape-driven: est_prob = the window-delta P(up), so the model
            # edge (est-yes) lands ~0/negative by construction and lane_min_edge wrongly
            # rejects them — yet the flip subset is ghost-+EV across lanes (hype 1h +0.314,
            # xrp 1h +0.220, bnb 15m +0.167, sol 1h SHORT +0.094, doge 15m SHORT bull +0.334).
            # When enabled, admit on TAPE CONVICTION (already cleared window_delta_flip_margin)
            # and size on a flat flip edge (sizing policy, not a fabricated prob); size still
            # capped by the lane size_multiplier. Mirrors the _simple_band_long pattern. Keep
            # OFF until per-lane ghost-validated, then enable one lane at a time and watch.
            _is_flip = bool(side_source and "window_delta_flip" in side_source)
            if (
                is_updown and _is_flip and _timing_window_open
                and bool(self.config.get(
                    f"flip_exempt_min_edge_{_updown_tf}",
                    self.config.get("flip_exempt_min_edge", False),
                ))
            ):
                _flip_edge = float(self.config.get("flip_edge_credit", 0.05) or 0.05)
                effective_min_edge = min(effective_min_edge, _flip_edge)
                if edge < _flip_edge:
                    edge = _flip_edge
                reason_parts.append(f"flip_exempt_min_edge({_flip_edge:.3f})")
            # 2026-06-28 est_prob CONVICTION FLOOR (Hermes ghost: bnb est_prob is
            # genuinely predictive — est_up>=0.55 => 72.4% WR/+0.17EV on 15m, 66.1%/+0.12
            # on 1h LONG; noise on the other alts). The bot gated only on edge (est-yes),
            # so it took low-conviction coin-flips that bleed. Per-window opt-in via
            # {strategy}.by_tf.<tf>.min_est_prob_conviction; 0 = off (byte-identical for
            # every unset lane). Directional: BUY_YES needs est_up>=floor; BUY_NO needs
            # P(down)=1-est_up>=floor. Admission-only, evaluated BEFORE the min_edge gate.
            # 2026-07-02 Deploy2(U1): RAW conviction floor, updown BUY_YES only
            # (directional bidirectional floor below stays untouched; this one must
            # never gate BUY_NO — the deep-NO side is the proven winner).
            _yes_conv_floor = float(
                self._tf_cfg(_updown_tf, "min_est_prob_conviction_buy_yes", 0.0) or 0.0
            )
            if (
                is_updown
                and action == "BUY_YES"
                and _yes_conv_floor > 0.0
                and float(estimated_prob) < _yes_conv_floor
                and not _is_rsi_fade  # fade bets against est_prob => conviction floor N/A
            ):
                _bump_skip("buy_yes_conviction_floor")
                logger.info(
                    f"  {_brand} skip '{market.question[:40]}...' BUY_YES conviction "
                    f"{float(estimated_prob):.3f} < floor {_yes_conv_floor:.3f} (buy_yes_conviction_floor)"
                )
                continue
            _conv_floor = float(self._tf_cfg(_updown_tf, "min_est_prob_conviction", 0.0) or 0.0)
            if is_updown and _conv_floor > 0.0 and not _is_rsi_fade:  # fade bets against est_prob => floor N/A
                _conv = float(estimated_prob) if action == "BUY_YES" else (1.0 - float(estimated_prob))
                if _conv < _conv_floor:
                    _bump_skip("est_prob_conviction_floor")
                    log_rejected_candidate(
                        strategy=self._signal_strategy_name,
                        window=_updown_tf,
                        side=allowed_side,
                        action=action,
                        reason="est_prob_conviction_floor",
                        market=market,
                        yes_price=yes_price,
                        est_prob_up=estimated_prob,
                        htf_bias=primary_htf_bias,
                        stage="est_prob_conviction_floor",
                        context={
                            "conviction": round(float(_conv), 6),
                            "min_est_prob_conviction": round(float(_conv_floor), 6),
                            "estimated_prob": round(float(estimated_prob), 6),
                        },
                        policy_version="est_prob_conviction_floor_v1",
                    )
                    logger.info(
                        f"  {_brand} skip '{market.question[:40]}...' "
                        f"conviction={_conv:.3f} < floor={_conv_floor:.2f} ({_updown_tf})"
                    )
                    continue
            # 2026-07-29 FEE-AWARE GATE: `edge` is PRE-FEE (est_prob - price). Polymarket
            # charges taker fee ~rate*p*(1-p); Olympus $0. Subtract the venue fee hurdle so
            # admission is net-of-fee, AND drop the marginal rescue if the net edge no longer
            # clears the marginal floor (else fee-thin cheap-NO shorts sneak through it).
            # No-op when trading.fee_aware_edge.enabled=false (hurdle 0 -> _net_edge == edge).
            _fee_hurdle = fee_aware_edge_hurdle(self.config, yes_price)
            _net_edge = edge - _fee_hurdle
            if _admit_marginal_no_ai and _fee_hurdle > 0.0 and _net_edge < float(
                self.config.get("ai_updown_marginal_min_edge", 0.03)
            ):
                _admit_marginal_no_ai = False
            # EXPERIMENT (2026-08-03, operator-directed): drop the min_edge floor to test whether
            # it is an OLYMPUS PRE-FEE relic (edge is pre-fee; the uniform 0.09 bar was calibrated
            # at fee=$0) strangling frequency on every lane. Global flag, hot-reloadable, ONE-flip
            # reversible. Covers sol/xrp/hype/doge/bnb (they inherit this scan loop). 0.0 keeps only
            # the breakeven floor (net-of-fee edge must still be >= 0), NOT a full ungate.
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
                            asset_spot=sol.current_price,
                            btc_spot=corr.btc_price,
                            rsi_14=sol.rsi_14,
                            atr_14=sol.atr_14,
                            macd_hist_5m=getattr(getattr(sol, "macd_5m", None), "histogram", None),
                            macd_hist_15m=getattr(getattr(sol, "macd_15m", None), "histogram", None),
                            macd_hist_1h=getattr(getattr(sol, "macd_1h", None), "histogram", None),
                            rsi_5m=getattr(getattr(sol, "tf_5m", None), "rsi_14", None),
                            rsi_15m=getattr(getattr(sol, "tf_15m", None), "rsi_14", None),
                            rsi_1h=getattr(getattr(sol, "tf_1h", None), "rsi_14", None),
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
                    window_size=_updown_tf,
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
                    # Instrument the composite floor so it is ghost-validatable:
                    # without this, composite_score_below_floor only bumps a live
                    # ops counter and is never settled, so we can't tell if the
                    # 0.62 floor over-blocks +EV candidates. Log the score + floor
                    # as a threshold probe; the settler resolves the real outcome.
                    log_rejected_candidate(
                        strategy=self._signal_strategy_name,
                        window=_updown_tf if is_updown else "15m",
                        side=allowed_side,
                        action=action,
                        reason="composite_score_below_floor",
                        market=market,
                        yes_price=yes_price,
                        est_prob_up=estimated_prob,
                        htf_bias=primary_htf_bias,
                        stage="composite_score_below_floor",
                        gate_reason=composite.reason,
                        gate_stage="composite_floor",
                        convergence_score=composite.convergence_score,
                        context={
                            "composite_score": round(float(composite.score), 6),
                            "composite_floor": round(float(composite.floor), 6),
                            "composite_components": composite.components,
                            "edge": round(float(edge), 6),
                            "effective_min_edge": round(float(effective_min_edge), 6),
                            "raw_est_prob": round(float(raw_est_prob), 6),
                            "estimated_prob": round(float(estimated_prob), 6),
                            "confidence": round(float(confidence), 6),
                            "side_source": side_source,
                            **build_market_context(
                                asset_spot=sol.current_price,
                                btc_spot=corr.btc_price,
                                rsi_14=sol.rsi_14,
                                atr_14=sol.atr_14,
                                macd_hist_5m=getattr(getattr(sol, "macd_5m", None), "histogram", None),
                                macd_hist_15m=getattr(getattr(sol, "macd_15m", None), "histogram", None),
                                macd_hist_1h=getattr(getattr(sol, "macd_1h", None), "histogram", None),
                                rsi_5m=getattr(getattr(sol, "tf_5m", None), "rsi_14", None),
                                rsi_15m=getattr(getattr(sol, "tf_15m", None), "rsi_14", None),
                                rsi_1h=getattr(getattr(sol, "tf_1h", None), "rsi_14", None),
                            ),
                        },
                        probe_variants=build_threshold_probe_variants(
                            metric_name="composite_score",
                            observed_value=float(composite.score),
                            baseline_threshold=float(composite.floor),
                        ),
                        policy_version="composite_floor_v1",
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
                    # 2026-08-10 RSI-fade exemption: the fade lives in the CENTERED coinflip
                    # band by construction (est_prob unusable), already admitted on flat_edge
                    # above with effective_min_edge=0; min_edge_when_centered (e.g. xrp 0.12)
                    # would re-reject the exact candidates the fade targets. Skip for faded.
                    _center_min_edge = 0.0 if _is_rsi_fade else max(effective_min_edge, self.min_edge_when_centered)
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
                    _buy_no_min_no = float(self.config.get(f"buy_no_min_no_price_{_win}", self.config.get("buy_no_min_no_price", 0.20)))  # 2026-07-16 STAGED per-window port (mirror eth_macro:2768, restart-only): buy_no_min_no_price_5m caps 5m shorts only; 15m/1h fall back base 0.20
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
            # 2026-07-21 TRUE-KELLY conviction sizing (operator ORDER). The legacy
            # size_from_edge path clamped the sizing edge to max_edge_updown (0.09),
            # flattening EVERY high-conviction trade to one size ($15/$5 binary, winners
            # == losers). size_binary_position uses the real win-probability + price odds
            # so size scales with conviction: high-conviction runs to the lane cap,
            # marginal sits near the floor. win_prob reconstructed exactly from edge:
            # edge == win_prob_of_side - our_price (holds for BUY_YES and BUY_NO).
            # Flag-gated (default on) for instant revert. TypeError = kelly_sizer
            # version-skew guard (not hot-reloadable; full behavior lands at restart).
            _kf_kw = dict(window=_updown_tf) if is_updown else {}
            if bool(self.config.get("use_true_kelly_sizing", True)):
                _our_price = yes_price if action == "BUY_YES" else (1.0 - yes_price)
                _win_prob = min(0.99, max(0.01, float(_our_price) + float(edge)))
                # 2026-07-22 calibration-correction apply hook (flag-gated, default OFF).
                # Shrinks an over-confident lane's win_prob before sizing, matching the
                # shadow (scripts/calibration_correction.py) that earned the apply-gate:
                # SIZING-ONLY (never re-gates admission — edge already passed above).
                # Lane key == the entries.jsonl 'strategy' field (_signal_strategy_name)
                # stripped of _macro, same as the map's source. Fail-safe: delta=0 on any miss.
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
            # 2026-08-06 (Codex bundle re-review): the ACTIVE alt scan path. Under flat sizing + the per-lane
            # CEILING model, the legacy static per-lane size multiplier is NEUTRALIZED so the flat base flows
            # FULL to the adaptive sizer (its per-lane ceiling + realized climb is the single size authority).
            # The old 0.3x shrink here made the new $40/$28 alt-short ceilings unreachable ($15*0.3*2.5=$11.25).
            # Mirror of the guard in the sol helper (~1972) and bitcoin (~4895). Reverts with flat_sizing:false.
            _flat_sizing = bool((self.config.get("trading", {}) or {}).get("flat_sizing_enabled", False))
            if lane_policy.size_multiplier > 0 and not _flat_sizing:
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
            # de-size a stale/exhausted entry rather than block it. Applied last so it
            # binds under lane floors/caps; a heavily-penalized size may fall below the
            # 0.5 floor and skip, which is the intended outcome for a fully-stale entry.
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

            # 2026-07-28 QUANT-AGREEMENT admission gate (opt-in, sol/bnb). Entries whose
            # final side DISAGREES with quant_side (the est_prob-implied direction) are a
            # verified realized leak: sol+bnb non-flip disagree n103 34%WR -$100.90 vs
            # agree n765 48% +$416.53 (all sessions). Veto the override, EXCEPT paths that
            # deliberately ignore est_prob: window_delta_flip (tape-driven edge) and
            # simple_band (est_prob unusable, sized on flat edge). quant_side=None (raw
            # est_prob exactly 0.5 placeholder) is NOT vetoed. Config: require_quant_side_
            # agreement (per-strategy). Skip reason: quant_side_disagree.
            if bool(self.config.get("require_quant_side_agreement", False)):
                _qs = side_from_est_prob_up(raw_est_prob)
                _chosen = "LONG" if action == "BUY_YES" else "SHORT"
                _ss = str(signal_side_source or "")
                # 2026-08-10 Codex-fix: exempt rsi_fade — it DELIBERATELY takes the anti-predictive
                # side (measured 70% WR at RSI<40), which by definition disagrees with quant est_prob;
                # without this exemption require_quant_side_agreement would veto the exact edge.
                _exempt = ("window_delta_flip" in _ss) or ("simple_band" in _ss) or ("rsi_fade" in _ss)
                if _qs is not None and _chosen != _qs and not _exempt:
                    _bump_skip("quant_side_disagree")
                    _log_skip_reject(
                        market=market,
                        window=_updown_tf if is_updown else "15m",
                        side=allowed_side,
                        action=action,
                        reason="quant_side_disagree",
                        yes_price=yes_price,
                        htf_bias=primary_htf_bias,
                        context={"side_source": signal_side_source,
                                 "quant_side": _qs, "chosen": _chosen},
                    )
                    continue

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
            # 2026-07-19 MAIN-LANE VETO (operator, per-lane allow-list): skip a main-lane
            # trouble-lane trade only if the AI CONFIDENTLY opposes it. ALLOW-LIST only =>
            # lanes not listed (all +869 winners/engines) are never touched. FAIL-OPEN:
            # any error/HOLD/agree/low-conf appends the trade normally.
            _mlv_ok = True
            try:
                _mlv_agent = getattr(self, "ai_agent", None)
                _mlv_cfg = (getattr(_mlv_agent, "main_lane_veto_cfg", {}) or {}) if _mlv_agent is not None else {}
                if _mlv_agent is not None and bool(_mlv_cfg.get("enabled", False)):
                    _mlv_win = _updown_tf if is_updown else "15m"
                    _mlv_side = "up" if action == "BUY_YES" else "down"
                    _mlv_key = "%s|%s|%s" % (self._signal_strategy_name, _mlv_win, _mlv_side)
                    if _mlv_key in set(_mlv_cfg.get("lanes", []) or []):
                        _mlv_ok, _mlv_meta = await _mlv_agent.evaluate_main_lane_veto(
                            market_question=market.question,
                            market_description=getattr(market, "description", "") or "",
                            current_yes_price=float(yes_price),
                            market_id=str(market.id),
                            strategy_hint=self._signal_strategy_name,
                            lane_id=_mlv_key,
                            quant_action=action,
                        )
                        if not _mlv_ok:
                            self._mlv_skip_count = getattr(self, "_mlv_skip_count", 0) + 1
                            logger.info("  MAIN-LANE VETO skip %s mkt=%s %s", _mlv_key, market.id, _mlv_meta)
            except Exception as _mlv_e:
                logger.warning("main_lane_veto wiring error (FAIL-OPEN, trade proceeds): %s", _mlv_e)
                _mlv_ok = True
            if _mlv_ok:
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
            "asset_regime": _asset_regime.get_state(
                getattr(self.sol_service, "alt_symbol", None)
            ),
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
        # 2026-06-16: belt-and-suspenders lane-disable filter. The in-loop disable hooks
        # run before late side-flips / the 1h-neutral routing (side_source *_1h_native /
        # *_1h_neutral) finalize `action`, so a disabled lane (e.g. doge 1h BUY_NO) can
        # slip through and trade. This final pass on emitted signals catches ALL paths.
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
        _before = len(signals)
        signals = [s for s in signals if not _lane_disabled(s)]
        if len(signals) != _before:
            logger.info(
                "%s lane-disable filter dropped %d emitted signal(s) (late-flip/native bypass)",
                self._signal_strategy_name, _before - len(signals),
            )
        # 2026-06-18: per-pulse fan-out dedup. One (window, side) signal was being
        # applied to EVERY concurrently-open same-window market in a single scan —
        # e.g. hype 15m fired 3 IDENTICAL entries (edge=0.145, rsi=69) on the
        # 4:30/4:45/5:00 markets, and again on 1h — tying up 3x correlated capital on
        # one bet AND tripping "Max concurrent positions reached" (starving distinct
        # entries). Collapse to ONE market per (window_size, action): the
        # nearest-resolving (active bucket) by end_date. Opt-out, default on.
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
            signals = deduped
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
            signals = _oc_kept
        # ── FAVORITE-LONGSHOT lane (08-07) ──────────────────────────────────
        # Separate structural pass appended AFTER the normal alt scan. Buys the
        # favorite side (our-side price >= floor) regardless of est_prob/edge —
        # favorites are structurally underpriced. Covers all alts via subclasses.
        # Fail-safe: never crashes the scan; deduped vs already-emitted market_ids.
        try:
            _fav_existing_ids = {getattr(s, "market_id", None) for s in signals}
            _fav_signals = self._favorite_lane_signals(sol_markets, bankroll)
            for _fs in _fav_signals:
                if getattr(_fs, "market_id", None) in _fav_existing_ids:
                    continue
                signals.append(_fs)
                _fav_existing_ids.add(getattr(_fs, "market_id", None))
        except Exception as _fav_e:
            logger.warning(
                "%s favorite_lane pass error (skipped, scan unaffected): %s",
                getattr(self, "_signal_strategy_name", "sol_macro"), _fav_e,
            )
        return signals

    def _favorite_lane_signals(self, markets: List[Market], bankroll: float) -> List["SolMacroSignal"]:
        """FAVORITE-LONGSHOT structural lane (side-agnostic 'buy the favorite').

        For each alt updown market, bet the side priced >= floor. Bypasses the
        est_prob/edge/price-band machinery entirely: est_prob is ~coinflip so a
        0.90 favorite computes NEGATIVE edge and is killed by the edge gate — but
        favorite-longshot bias makes the structural bet +EV with no direction
        prediction. Covers sol/xrp/hype/bnb/doge via subclasses. PAPER only.
        Fully fail-safe: any error → [].
        """
        try:
            cfg = self.full_config.get("favorite_lane", {}) or {}
            # 2026-08-10 HOT-RELOADABLE KILL (operator: revert to +869 geometry = NO favorite lane;
            # win-11%/lose-90% payoff-trap, b=0.13). risk.* IS hot-reloadable so this flips the
            # favorite lane off WITHOUT a restart. Default True = no behavior change when unset.
            if not bool(self.full_config.get("risk", {}).get("favorite_lane_enabled", True)):
                return []
            if not bool(cfg.get("enabled", False)):
                return []
            floor = float(cfg.get("floor", 0.85))
            # price_max: skip DEEP favorites. A 0.96 favorite pays only ~4% on a win but
            # loses ~96% of stake when it gaps to $0 at settlement (worst risk/reward) — the
            # xrp -$70.37 (entry 0.96) loss. The 0.85-0.92 band pays enough per win (~9-18%)
            # to survive its own loss ratio. 0 / >=1.0 => no cap (disabled).
            price_max = float(cfg.get("price_max", 1.0) or 1.0)
            # 2026-08-10 PER-WINDOW FLOOR (operator "improve if data offers a filter, else cut").
            # 1h favorites 0.85-0.89 = 50% WR net -$65 (an hour to reverse => coin-flip); 0.90-0.94 =
            # 72.7% WR net +$3.88 (clears 68% breakeven). 15m favorites 87% WR at 0.85 (untouched).
            # Raise ONLY 1h's floor via window_floors: {1h: 0.90}. Absent => uses `floor` (identical).
            _window_floors = cfg.get("window_floors", {}) or {}
            size_usd = float(cfg.get("size_usd", 8.0))
            windows = set(str(w) for w in (cfg.get("windows", ["15m", "1h"]) or []))
            min_mins_left = float(cfg.get("min_mins_left", 3.0))
            now = datetime.now(timezone.utc)
            _spot_key = self._alt_asset_code()
            out: List[SolMacroSignal] = []
            for market in markets:
                try:
                    # Alt updown only, freshly-priced (reuse the scan's freshness guard).
                    if not (self._is_solana_market(market) and self._is_updown_market(market)):
                        continue
                    if not is_tradably_priced(market):
                        continue
                    tf = updown_timeframe_label(resolved_updown_window_minutes(market))
                    if tf not in windows:
                        continue
                    yes_price = market.yes_price
                    if yes_price is None:
                        continue
                    yes_price = float(yes_price)
                    fav_price = max(yes_price, 1.0 - yes_price)
                    # 2026-08-10 apply the PER-WINDOW floor (was checking base `floor`, ignoring
                    # window_floors — the 1h 0.90 floor never fired; parity with bitcoin.py:5384).
                    _eff_floor = float(_window_floors.get(tf, floor))
                    if fav_price < _eff_floor:
                        continue
                    if fav_price > price_max:
                        continue  # deep favorite: pennies-per-win vs ~full-stake settlement gap
                    if not market.end_date:
                        continue
                    end_utc = (
                        market.end_date.replace(tzinfo=timezone.utc)
                        if market.end_date.tzinfo is None else market.end_date
                    )
                    mins_left = (end_utc - now).total_seconds() / 60.0
                    if mins_left < min_mins_left:
                        continue
                    fav_action = "BUY_YES" if yes_price >= 0.5 else "BUY_NO"
                    direction = "UP" if fav_action == "BUY_YES" else "DOWN"
                    order_price = yes_price if fav_action == "BUY_YES" else (1.0 - yes_price)
                    # 2026-08-08 HONOR THE AI DIRECTION DRIVER (operator: "is AI driving? why not
                    # fixing this"). The favorite lane is side-agnostic and had been IGNORING the AI
                    # override entirely — so when the driver said an asset was FLAT/sit-out, the
                    # favorite lane bought its favorite anyway (the sol whipsaw: AI said sol=FLAT,
                    # favorite bought BOTH YES and NO, lost both). Gate the favorite on the SAME
                    # override the direction trades use: FLAT/none => sit out this asset; LONG =>
                    # only UP favorites; SHORT => only DOWN favorites. Config-gated; on any read the
                    # seam is fail-safe (returns None only for a genuine FLAT/stale, else the side).
                    if bool(cfg.get("respect_ai_direction", False)):
                        _ai_side = self._apply_direction_override(None, tf)  # 'LONG'/'SHORT'/None
                        if _ai_side is None:
                            logger.info(
                                "  [favorite-lane] SIT-OUT %s tf=%s: AI driver FLAT/sit-out this asset",
                                fav_action, tf)
                            continue
                        _want = "BUY_YES" if _ai_side == "LONG" else ("BUY_NO" if _ai_side == "SHORT" else None)
                        if _want is not None and fav_action != _want:
                            logger.info(
                                "  [favorite-lane] SIT-OUT %s tf=%s: AI driver says %s (favorite side disagrees)",
                                fav_action, tf, _ai_side)
                            continue
                    # 2026-08-08 REALIZED TAPE DEFERENCE (build-it-right; NOT a tape-blind gate).
                    # Sit out this favorite side WHILE the realized adapter says the side is
                    # net-losing / never-green in the CURRENT tape (get_tape_admission_delta > 0
                    # = tighten). Self-flips: when the tape turns, the per-(asset,window,side)
                    # delta flips sign and admission flips with it — no hardcoded direction, no
                    # per-regime threshold. Choppy tape => BOTH sides losing => both sit out
                    # (kills the sol YES-then-NO whipsaw). Recovery => delta drops => auto re-admit.
                    # Requires lane_tape_adapter.admission_mode: live; 0/absent delta => no effect.
                    _sit = float(cfg.get("tape_sit_out_delta", 0.0) or 0.0)
                    if _sit > 0.0:
                        _tape_delta = float(
                            get_tape_admission_delta(self._signal_strategy_name, tf, direction) or 0.0)
                        if _tape_delta >= _sit:
                            logger.info(
                                "  [favorite-lane] SIT-OUT %s tf=%s side=%s: realized tape delta "
                                "%.3f >= %.3f (side losing in current tape; self-flips on recovery)",
                                fav_action, tf, direction, _tape_delta, _sit,
                            )
                            continue
                    # 2026-08-10 FAVORITE-SCOPED REALIZED-NET SIT-OUT (operator GO). The
                    # tape_sit_out_delta above is fed by lane_tape_adapter, whose key MIXES
                    # favorite + band closes and whose MFE/green signal cannot see the favorite
                    # PAYOFF-TRAP (high-WR, negative-NET): it read sol|15m|up as "loosen" while
                    # that lane bled -$99. This reads the FAVORITE-ONLY rolling avg realized net
                    # per (asset,window,side) (favorite_net_tracker, built from the settled
                    # journal) and sits the side out while its recent favorites bleed. Self-flips:
                    # the rolling window rolls losers off / a recovered lane climbs back above the
                    # floor and re-admits — no per-window/side hardcode, not tape-blind. Fail-OPEN:
                    # None (too few samples / no state file) => admit. Disabled when key unset.
                    _fn_floor = cfg.get("fav_net_sit_out_avg", None)
                    if _fn_floor is not None:
                        _fav_net = get_favorite_net(self._signal_strategy_name, tf, direction)
                        if _fav_net is not None and _fav_net <= float(_fn_floor):
                            logger.info(
                                "  [favorite-lane] SIT-OUT %s tf=%s side=%s: favorite avg net "
                                "%.2f <= %.2f (lane bleeding; self-flips on recovery)",
                                fav_action, tf, direction, _fav_net, float(_fn_floor),
                            )
                            continue
                    # 2026-08-09 #1 SIT-OUT TREND-ALIGNED FAVORITES (operator GO, shadow-first) — sol-family
                    # parity with bitcoin._favorite_lane_signals. Trend-aligned favorites BLEED (-$365);
                    # FLAT-tape + against-trend WIN. Gate: tape trending AND favorite agrees => sit out
                    # (live) / log-only (shadow). Fail-safe. Mode: off (default) | shadow | live.
                    _sta_mode = str(cfg.get("sit_out_trend_aligned_mode", "off") or "off").lower()
                    if _sta_mode in ("shadow", "live"):
                        try:
                            _tstate = _latest_tape_state(self._signal_strategy_name) or {}
                            _tape_dir = str(_tstate.get("direction") or "FLAT").upper()
                            if _tape_dir in ("UP", "DOWN") and direction == _tape_dir:
                                logger.info(
                                    "  [favorite-lane] TREND-ALIGNED %s tf=%s dir=%s tape=%s (%s)",
                                    fav_action, tf, direction, _tape_dir,
                                    "SIT-OUT" if _sta_mode == "live" else "shadow-would-sit-out")
                                if _sta_mode == "live":
                                    continue
                        except Exception:
                            pass
                    # 2026-08-09 SIZE-TAPER by entry price (#2, operator GO) — sol-family parity with
                    # bitcoin._favorite_lane_signals. Thin-payoff high-price favorites (0.88-0.93 band:
                    # high WR but net-negative, payoff can't cover losers) get downsized; 0.85-0.88 stays
                    # full. taper=clamp((1-fav_price)/(1-floor), min_frac, 1.0). Off => byte-identical.
                    _fav_size = size_usd
                    if bool(cfg.get("size_taper_enabled", False)):
                        _min_frac = min(1.0, max(0.0, float(cfg.get("size_taper_min_frac", 0.4) or 0.4)))
                        _t_start = float(cfg.get("size_taper_start", 0.88) or 0.88)  # full up to here; taper above
                        _taper = (price_max - fav_price) / max(1e-6, price_max - _t_start)
                        _fav_size = size_usd * max(_min_frac, min(1.0, _taper))
                    out.append(SolMacroSignal(
                        market_id=market.id,
                        market_question=market.question,
                        action=fav_action,
                        price=order_price,
                        size=round(_fav_size, 2),
                        confidence=round(fav_price, 4),
                        edge=round(fav_price - 0.5, 4),
                        token_id_yes=market.token_id_yes,
                        token_id_no=market.token_id_no,
                        condition_id=getattr(market, "condition_id", None),
                        market_slug=getattr(market, "slug", None),
                        outcome_label_yes=getattr(market, "outcome_label_yes", None),
                        outcome_label_no=getattr(market, "outcome_label_no", None),
                        end_date=market.end_date,
                        direction=direction,
                        reason="favorite_lane",
                        strategy_name=self._signal_strategy_name,
                        alt_asset_code=_spot_key,
                        window_size=tf,
                        hour_utc=now.hour,
                        est_prob=round(fav_price, 4),
                        side_source="favorite_lane",
                    ))
                    logger.info(
                        "  [favorite-lane] %s '%s' tf=%s fav_price=%.3f size=$%.2f (%.1fm left)",
                        fav_action, market.question[:40], tf, fav_price, _fav_size, mins_left,
                    )
                except Exception as _fe:
                    logger.debug("favorite_lane per-market skip: %s", _fe)
                    continue
            return out
        except Exception as _e:
            logger.warning("favorite_lane_signals error (returning []): %s", _e)
            return []


def _get_weekend_penalty() -> float:
    """Return weekend penalty multiplier (1.0=normal, lower=tighter max size).

    Reduces position size during weekend / low-liquidity periods when
    HYPE-style manipulation (a4385 CEX pump) is most likely to occur.
    Kept in sync with ``exposure_manager._get_weekend_penalty``.
    """
    return 1.0  # 2026-07-11 re-applied on winning-artifact restore: weekend penalty data-DISABLED 05:26 (weekend -0.043/t vs weekday -0.227/t); operator-disputed, LIVE state preserved
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
