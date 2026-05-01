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
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, Field

from src.market.scanner import Market
from src.analysis.ai_agent import AIAgent
from src.analysis.btc_price_service import BTCPriceService, TechnicalAnalysis
from src.analysis.math_utils import PositionSizer
from src.analysis.sol_btc_service import SOLBTCService, SOLTechnicalAnalysis, BTCSOLCorrelation
from src.analysis.kelly_sizer import KellySizer
from src.execution.exposure_manager import ExposureManager, MarketConditions, ExposureTier
from src.strategies.strategy_config import resolve_enabled_flag
from src.strategies.strategy_ai_context import (
    ai_recommendation_supports_action,
    format_market_metadata,
)

logger = logging.getLogger(__name__)


class SolMacroSignal(BaseModel):
    """Represents a signal on a Solana price market."""
    market_id: str = Field(..., description="Market identifier")
    market_question: str = Field(..., description="The market question")
    action: str = Field(..., description="BUY_YES or SELL_YES")
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
    window_size: Optional[str] = Field(None, description="Market window: 5m or 15m")
    hour_utc: Optional[int] = Field(None, description="UTC hour at entry time")
    est_prob: Optional[float] = Field(None, description="Estimated prob of YES at entry (key diagnostic)")
    rsi: Optional[float] = Field(None, description="SOL RSI-14 at entry")
    corr_1h: Optional[float] = Field(None, description="BTC-SOL 1H correlation at entry")
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


def _market_window_minutes(market: Market) -> int:
    """Estimate candle window size (in minutes) from question time range.

    Looks for patterns like "2:15AM-2:20AM" or "2:15AM–2:30AM".
    Returns 5 for 5-minute windows, 15 for 15-minute windows (default).
    """
    m = re.search(r'(\d+):(\d+)(AM|PM)[–\-](\d+):(\d+)(AM|PM)', market.question, re.IGNORECASE)
    if m:
        h1, m1, p1, h2, m2, p2 = m.groups()
        h1, m1, h2, m2 = int(h1), int(m1), int(h2), int(m2)
        if p1.upper() == 'PM' and h1 != 12:
            h1 += 12
        if p1.upper() == 'AM' and h1 == 12:
            h1 = 0
        if p2.upper() == 'PM' and h2 != 12:
            h2 += 12
        if p2.upper() == 'AM' and h2 == 12:
            h2 = 0
        start_min = h1 * 60 + m1
        end_min = h2 * 60 + m2
        diff = end_min - start_min
        if diff < 0:
            diff += 1440  # midnight wrap
        return diff
    # Fall back to keyword detection
    q = market.question.lower()
    if '5m' in q or '5-min' in q:
        return 5
    return 15  # default: assume 15m

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
                 kelly_sizer=None, exposure_manager: ExposureManager = None):
        self.full_config = config
        self.config = config.get('strategies', {}).get('sol_macro', {})
        self.enabled = resolve_enabled_flag(
            "sol_macro",
            self.config,
            logger=logger,
        )
        self.ai_agent = ai_agent
        self.position_sizer = position_sizer
        self.kelly_sizer = kelly_sizer or KellySizer(config)
        self.btc_service = BTCPriceService()
        self.exposure_manager = exposure_manager or ExposureManager(config)
        if self.exposure_manager:
            self.exposure_manager._on_pause_ai_callback = self._ai_kill_switch_analysis
        self._signal_strategy_name = "sol_macro"
        self.dead_zone_skip_callback = None
        self._apply_strategy_config(rebuild_service=True)

        # AI-hold soft veto: cache market IDs where AI recently said HOLD so the
        # strong-signal path cannot bypass that decision within the TTL window.
        self._ai_hold_cache: Dict[str, float] = {}
        self.ai_hold_veto_ttl_sec = self.config.get("ai_hold_veto_ttl_sec", 300)
        self.min_edge_5m_ai_override = self.config.get("min_edge_5m_ai_override", 0.10)

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

    def _apply_strategy_config(self, *, rebuild_service: bool = False) -> None:
        # Thresholds from config first — before any other init work — so
        # scan_and_analyze always sees instance values from YAML, not class fallbacks.
        self.min_liquidity = self.config.get("min_liquidity", 1000)
        self.min_edge = self.config.get("min_edge", 0.09)
        self.min_edge_5m = self.config.get("min_edge_5m", self.min_edge)
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
        self.low_corr_threshold_1h = float(
            self.config.get("low_corr_threshold_1h", 0.50)
        )
        self.low_corr_damping = float(self.config.get("low_corr_damping", 0.70))
        self.low_corr_suppresses_entries = bool(
            self.config.get("low_corr_suppresses_entries", False)
        )
        if rebuild_service or not hasattr(self, "sol_service"):
            self.sol_service = self._build_alt_service()

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

    def _btc_alt_corr_log_label(self) -> str:
        return f"BTC-{self._alt_log_label()} corr"

    @staticmethod
    def _signal_lag_magnitude(corr) -> Optional[float]:
        """Alt-aware lag % for journaling: opportunity mag when flagged, else signed alt lag vs BTC."""
        if corr.lag_opportunity:
            return round(float(corr.opportunity_magnitude), 4)
        if corr.btc_spike_detected and abs(float(corr.sol_lag_pct)) >= 1e-6:
            return round(float(corr.sol_lag_pct), 4)
        return None

    def _is_solana_market(self, market: Market) -> bool:
        text = f"{market.question} {market.description}".lower()
        # Make sure it's SOL and NOT just "resolve" or other words containing "sol"
        has_sol = any(p.search(text) for p in SOL_PATTERNS)
        # Exclude BTC-only markets
        is_btc_only = 'bitcoin' in text and 'solana' not in text
        return has_sol and not is_btc_only

    def _is_updown_market(self, market: Market) -> bool:
        """Check if this is a Solana Up or Down market (matches both 15m and 5m)."""
        return bool(UPDOWN_PATTERN.search(market.question))

    def _is_5m_market(self, market: Market) -> bool:
        """Check if this is a 5-minute candle Up or Down market (≤6 min window)."""
        return _market_window_minutes(market) <= 6

    def _resolve_entry_window_bounds(self, *, is_5m: bool, default_min: float, default_max: float) -> tuple[float, float]:
        """Return entry window bounds, optionally widened to align with scan cadence."""
        win_min = float(self.config.get("entry_window_5m_min" if is_5m else "entry_window_15m_min", default_min))
        win_max = float(self.config.get("entry_window_5m_max" if is_5m else "entry_window_15m_max", default_max))
        if win_min > win_max:
            win_min, win_max = win_max, win_min

        if not self.config.get("entry_window_auto_align", False):
            return win_min, win_max

        scan_interval_sec = float(self.config.get("entry_window_align_scan_interval_sec", 300))
        default_expand = 1.0 if is_5m else 1.5
        max_expand_min = float(self.config.get("entry_window_auto_align_max_expand_min", default_expand))
        jitter_sec = float(self.config.get("entry_window_auto_align_jitter_sec", 15))
        expansion_min = min(scan_interval_sec / 120.0, max_expand_min) + max(0.0, jitter_sec) / 60.0

        market_window_min = 5.0 if is_5m else 15.0
        aligned_min = max(0.0, win_min - expansion_min)
        aligned_max = min(market_window_min, win_max + expansion_min)
        if aligned_max <= aligned_min:
            return win_min, win_max
        return aligned_min, aligned_max

    def _resolve_ai_decision_window_bounds(self, *, is_5m: bool) -> tuple[float, float]:
        """Return the preferred AI-decision timing window in minutes remaining."""
        default_min = 1.5 if is_5m else 8.0
        default_max = 2.5 if is_5m else 13.0
        win_min = float(self.config.get("ai_entry_window_5m_min" if is_5m else "ai_entry_window_15m_min", default_min))
        win_max = float(self.config.get("ai_entry_window_5m_max" if is_5m else "ai_entry_window_15m_max", default_max))
        if win_min > win_max:
            win_min, win_max = win_max, win_min
        return win_min, win_max

    def _within_ai_decision_window(self, *, mins_left: float, is_5m: bool) -> bool:
        win_min, win_max = self._resolve_ai_decision_window_bounds(is_5m=is_5m)
        return win_min <= mins_left <= win_max

    def _rsi_blocks_entry(self, action: str, rsi: float) -> bool:
        """Optional config-scoped RSI hard gate for extreme one-sided entries."""
        buy_ceiling = self.config.get("rsi_buy_block_above")
        if action == "BUY_YES" and buy_ceiling is not None and rsi >= float(buy_ceiling):
            return True

        sell_floor = self.config.get("rsi_sell_block_below")
        if action == "SELL_YES" and sell_floor is not None and rsi <= float(sell_floor):
            return True

        return False

    def _oracle_basis_blocks_entry(self, oracle_basis_bps: Optional[float]) -> bool:
        """Optional hard gate when spot diverges too far from the oracle reference."""
        max_basis_bps = self.config.get("oracle_max_basis_bps")
        if max_basis_bps is None or oracle_basis_bps is None:
            return False
        return abs(float(oracle_basis_bps)) > float(max_basis_bps)

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
        """Determine 1H macro trend for SOL. This gates everything.

        Uses:
        1. 1H EMA alignment (9 > 21 > 50 = bullish, reverse = bearish)
        2. 1H RSI zone (>55 = bull bias, <45 = bear bias)
        3. Multi-TF alignment score

        Returns: "BULLISH", "BEARISH", or "NEUTRAL"
        """
        mtt = ta.multi_tf
        sol = ta.sol

        bull_votes = 0
        bear_votes = 0

        # Vote 1: 1H trend from multi-TF analysis
        if mtt.h1_trend == "BULLISH":
            bull_votes += 1
        elif mtt.h1_trend == "BEARISH":
            bear_votes += 1

        # Vote 2: EMA alignment on 15m (proxy for sustained direction)
        if sol.ema_9 > sol.ema_21 > sol.ema_50:
            bull_votes += 1
        elif sol.ema_9 < sol.ema_21 < sol.ema_50:
            bear_votes += 1

        # Vote 3: RSI zone
        if sol.rsi_14 > 55:
            bull_votes += 1
        elif sol.rsi_14 < 45:
            bear_votes += 1

        if bull_votes >= 2:
            return "BULLISH"
        elif bear_votes >= 2:
            return "BEARISH"
        return "NEUTRAL"

    def _get_btc_htf_bias(self, ta: TechnicalAnalysis) -> str:
        """Use BTC 4H structure as the primary macro gate for alt strategies."""
        sabre = ta.trend_sabre
        macd_4h = ta.macd_4h
        price = ta.current_price

        bull_votes = 0
        bear_votes = 0

        if sabre.trend == 1:
            bull_votes += 1
        elif sabre.trend == -1:
            bear_votes += 1

        if price > sabre.ma_value:
            bull_votes += 1
        elif price < sabre.ma_value:
            bear_votes += 1

        early_bull = macd_4h.crossover == "BULLISH_CROSS" and macd_4h.histogram_rising
        early_bear = macd_4h.crossover == "BEARISH_CROSS" and not macd_4h.histogram_rising
        recovery = not macd_4h.above_zero and macd_4h.histogram > 0
        if early_bear:
            bear_votes += 1
        elif macd_4h.above_zero or early_bull or recovery:
            bull_votes += 1
        else:
            bear_votes += 1

        if bull_votes >= 2:
            bias = "BULLISH"
        elif bear_votes >= 2:
            bias = "BEARISH"
        else:
            return "NEUTRAL"

        min_hist = float(self.config.get("btc_min_4h_hist_magnitude", 20.0))
        if abs(macd_4h.histogram) < min_hist:
            logger.info(
                "BTC HTF: %s by vote but 4H MACD hist=%+.1f below conviction threshold (%s) "
                "— downgrading to NEUTRAL",
                bias,
                macd_4h.histogram,
                min_hist,
            )
            return "NEUTRAL"

        return bias

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

    def _strong_enough_5m_signal(self, m5_adj: float) -> bool:
        """Optional guard for weak 5m-only entries.

        Some assets perform poorly when the 5m path is allowed to enter on the
        weakest MACD state (`macd_line > signal_line`, worth only +0.02). When
        configured, require at least the configured positive 5m adjustment.
        """
        return m5_adj >= self.min_positive_m5_adj_5m

    def _low_corr_blocks_entry(self, corr: BTCSOLCorrelation) -> bool:
        """Optional hard gate for assets whose BTC-lag thesis breaks when decoupled."""
        return (
            self.low_corr_suppresses_entries
            and corr.correlation_1h < self.low_corr_threshold_1h
        )

    # ──────────────────────────────────────────────────────────────
    # LAYER 2: 15m Trend Confirmation
    # ──────────────────────────────────────────────────────────────

    def _check_15m_confirmation(self, ta: SOLTechnicalAnalysis, allowed_side: str) -> tuple:
        """Check if 15m MACD confirms the allowed direction.

        Returns: (confirmed: bool, strength: float, reasons: list)
        """
        macd_15m = ta.sol.macd_15m
        reasons = []
        strength = 0.0

        if allowed_side == "LONG":
            if macd_15m.crossover == "BULLISH_CROSS":
                strength += 0.40
                reasons.append("15m MACD bull cross")
            if macd_15m.histogram_rising:
                if macd_15m.prev_histogram < 0 and macd_15m.histogram > 0:
                    strength += 0.35
                    reasons.append("15m hist red-to-green")
                elif macd_15m.histogram > macd_15m.prev_histogram:
                    strength += 0.15
                    reasons.append("15m hist rising")
            if macd_15m.macd_line > macd_15m.signal_line:
                strength += 0.10
                reasons.append("15m MACD above signal")
        else:  # SHORT
            if macd_15m.crossover == "BEARISH_CROSS":
                strength += 0.40
                reasons.append("15m MACD bear cross")
            if not macd_15m.histogram_rising:
                if macd_15m.prev_histogram > 0 and macd_15m.histogram < 0:
                    strength += 0.35
                    reasons.append("15m hist green-to-red")
                elif macd_15m.histogram < macd_15m.prev_histogram:
                    strength += 0.15
                    reasons.append("15m hist falling")
            if macd_15m.macd_line < macd_15m.signal_line:
                strength += 0.10
                reasons.append("15m MACD below signal")

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

        # BTC-SOL lag — secondary confirmer (reduced weight)
        # Live data: lag=None = 63% WR, lag=value = 50% WR.
        # Lag arrives after market partially prices in the move.
        # Keep as small nudge for threshold markets only; updown markets
        # apply their own lag adjustment in the scan loop.
        lag_adj = 0.0
        corr = ta.correlation
        if corr.lag_opportunity:
            if (direction == "UP" and corr.opportunity_direction == "LONG") or \
               (direction == "DOWN" and corr.opportunity_direction == "SHORT"):
                lag_adj = min(0.04, abs(corr.opportunity_magnitude) * 0.25)
            else:
                lag_adj = -0.02

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
        if btc_ta:
            btc_htf_bias = self._get_btc_htf_bias(btc_ta)
            logger.info(f"BTC HTF: {btc_htf_bias} | BTC ${btc_ta.current_price:,.0f}")
        else:
            btc_htf_bias = None
            logger.warning("BTC HTF unavailable — falling back to alt-only analysis")

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

        # ═══════════════════════════════════════════════
        # LAYER 1: Macro trend (1H)
        # ═══════════════════════════════════════════════
        macro_trend = self._get_macro_trend(ta)
        primary_htf_bias = btc_htf_bias or macro_trend

        logger.info(
            f"{_alt_label} ${sol_price:,.2f} | BTC_HTF: {primary_htf_bias} | ALT_HTF: {macro_trend} | "
            f"1H={mtt.h1_trend} 15m={mtt.m15_trend} 5m={mtt.m5_trend} | "
            f"15m MACD hist={sol.macd_15m.histogram:+.3f} {sol.macd_15m.crossover} | "
            f"RSI={sol.rsi_14:.0f} | "
            f"{_corr_lbl}={corr.correlation_1h:.2f} lag_opp={corr.lag_opportunity} "
            f"lag_dir={corr.opportunity_direction} lag_mag={corr.opportunity_magnitude:+.2f}% | "
            f"BTC spike={corr.btc_spike_detected} ({corr.btc_move_5m_pct:+.2f}%)"
        )

        # Check for updown markets
        has_updown = any(self._is_updown_market(m) for m in sol_markets)

        _is_neutral_macro = primary_htf_bias == "NEUTRAL"

        if _is_neutral_macro:
            if not has_updown:
                logger.info(f"{_brand} strategy: BTC+ALT HTF neutral — sitting out")
                return []
            # NEUTRAL macro with updown markets: use LTF as primary signal.
            # Live data: lag=None trades 63% WR outperform lag=value 50% WR.
            # Allow entry when LTF is confirmed; lag is a SECONDARY boost only.
            # Track these trades separately via NEUTRAL_MACRO tag in reason_parts.
            if corr.btc_spike_detected:
                # BTC spike but alt hasn't moved → trade the catch-up direction
                allowed_side = "LONG" if corr.btc_move_5m_pct > 0 else "SHORT"
                logger.info(
                    f"{_brand}: Macro NEUTRAL, BTC spike detected ({corr.btc_move_5m_pct:+.2f}%). "
                    f"Trading {_alt_label} catch-up: {allowed_side}"
                )
            elif corr.lag_opportunity:
                _min_lag_mag = self.config.get("min_lag_magnitude_pct", 0.30)
                _lag_mag = abs(corr.opportunity_magnitude)
                if _lag_mag >= _min_lag_mag:
                    allowed_side = corr.opportunity_direction
                    logger.info(
                        f"{_brand}: Macro NEUTRAL, strong lag ({_lag_mag:.2f}%) — "
                        f"using lag direction: {allowed_side}"
                    )
                else:
                    # Weak lag during NEUTRAL — allow but use alt's own 1H bias as direction
                    allowed_side = "LONG" if corr.sol_trend == "BULLISH" else "SHORT" if corr.sol_trend == "BEARISH" else None
                    if allowed_side is None:
                        logger.info(f"{_brand}: Macro NEUTRAL, weak lag, no {_alt_label} bias — sitting out")
                        return []
                    logger.info(
                        f"{_brand}: Macro NEUTRAL, weak lag — using {_alt_label} 1H bias: {allowed_side}"
                    )
            else:
                # No lag, no spike — alt-only direction is weak in chop; optional hard skip.
                if self.neutral_macro_require_spike_or_lag:
                    logger.info(
                        f"{_brand}: Macro NEUTRAL, no BTC spike/lag — sitting out "
                        f"(neutral_macro_require_spike_or_lag)"
                    )
                    return []
                # No lag, no spike — use alt's own 1H trend as direction
                allowed_side = "LONG" if corr.sol_trend == "BULLISH" else "SHORT" if corr.sol_trend == "BEARISH" else None
                if allowed_side is None:
                    logger.info(f"{_brand}: Macro NEUTRAL, no lag, no {_alt_label} bias — sitting out")
                    return []
                logger.info(f"{_brand}: Macro NEUTRAL, no lag — using {_alt_label} 1H bias: {allowed_side}")
        else:
            # BULLISH or BEARISH macro — BTC 4H is primary, alt HTF is secondary
            allowed_side = "LONG" if primary_htf_bias == "BULLISH" else "SHORT"

            # MTF alignment note: fully aligned = trend has been running.
            # Live data shows lag=None trades (63% WR) outperform lag=value (50% WR) —
            # do NOT require lag to enter. Macro + LTF is the real edge.
            # Just log alignment status; entry price filter (0.46-0.49) is the gatekeeper.
            if has_updown and ta.multi_tf.aligned:
                logger.info(
                    f"{_brand}: MTF fully aligned — entry price filter will gate quality "
                    f"(lag is secondary; macro+LTF is primary signal)"
                )

            # ── Lag as SECONDARY confirmer (not entry gate) ──
            # Live data: lag=None macro trades = 63% WR; lag=value = 50% WR.
            # The lag signal arrives AFTER the market has partially priced in the move.
            # Macro + LTF is the primary signal; lag adds a small probability boost only.
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
        # LAYER 2: 15m confirmation
        # ═══════════════════════════════════════════════
        ltf_confirmed, ltf_strength, ltf_reasons = self._check_15m_confirmation(ta, allowed_side)

        # ANTI-LTF GATE: Backtest (90 days, 2180 → 1208 trades) shows:
        #   LTF confirmed   (strength >= 0.35) → 51.9% WR  ← BAD, MACD fires after move peaks
        #   LTF unconfirmed (strength < 0.35)  → 65.0% WR  ← EXCELLENT, early momentum phase
        # NOTE: Previous live session (session_20260320_to_25) showed unconfirmed = 47% WR.
        # That was WITHOUT the 4H histogram_rising gate. With the gate active, unconfirmed
        # selects for early-momentum windows within established 1H trends = 65% WR.
        if ltf_confirmed:
            logger.info(
                f"{_brand}: LTF confirmed = late-entry risk (MACD crossed = exhaustion risk), "
                f"skipping. strength={ltf_strength:.2f}"
            )
            return []

        logger.info(f"  Anti-LTF gate passed: {allowed_side} — early momentum, strength={ltf_strength:.2f}")

        # ═══════════════════════════════════════════════
        # LAYER 3: 5m entry timing + lag detection
        # ═══════════════════════════════════════════════
        timing_bonus, timing_reasons = self._check_entry_timing(ta, allowed_side)
        if timing_reasons:
            logger.info(f"  Timing: bonus={timing_bonus:+.3f} [{', '.join(timing_reasons)}]")

        # ═══════════════════════════════════════════════
        # LAYER 4: Evaluate each market
        # ═══════════════════════════════════════════════
        signals = []
        ai_calls = 0
        skip_reasons: Dict[str, int] = {}
        gate_samples: Dict[str, list] = {}

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

        # Sample LTF strength (cycle-level, applies to all markets that reach the loop)
        _sample("ltf_strength", ltf_strength)

        for market in sol_markets:
            if market.liquidity > 0 and market.liquidity < self.min_liquidity:
                _bump_skip("liquidity")
                continue

            yes_price = market.yes_price
            is_updown = self._is_updown_market(market)
            is_5m = self._is_5m_market(market) if is_updown else False
            ai_used = False
            reason_parts = [
                f"BTC_HTF={primary_htf_bias}",
                f"ALT_HTF={macro_trend}",
                f"side={allowed_side}",
            ]
            dead_zone_would_block = False
            dead_zone_hour = None
            if _is_neutral_macro:
                reason_parts.append("NEUTRAL_MACRO")

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
                if is_5m:
                    _win_min, _win_max = self._resolve_entry_window_bounds(
                        is_5m=True,
                        default_min=2.75,
                        default_max=3.75,
                    )
                else:
                    _win_min, _win_max = self._resolve_entry_window_bounds(
                        is_5m=False,
                        default_min=13.0,
                        default_max=14.33,
                    )
                _sample("mins_left", _mins_left)
                if _mins_left < _win_min or _mins_left > _win_max:
                    _bump_skip("outside_entry_window")
                    logger.debug(
                        f"  {_brand} skip '{market.question[:40]}' — "
                        f"{_mins_left:.1f}m left, need {_win_min}–{_win_max}m window"
                    )
                    continue
                _ai_window_open = self._within_ai_decision_window(
                    mins_left=_mins_left,
                    is_5m=is_5m,
                )

                # ── BTC minimum dollar move before entering ──
                # Require BTC to have moved a minimum $ amount to confirm directional momentum.
                # Bypass for low-correlation assets — if BTC is not driving this asset,
                # requiring BTC movement incorrectly suppresses valid alt-independent signals.
                _btc_corr = corr.correlation_1h
                _low_corr_btc_bypass = float(self.config.get("btc_min_move_low_corr_threshold", 0.30))
                _btc_price = corr.btc_price or 0.0
                _btc_move_5m_dollars = abs(corr.btc_move_5m_pct / 100.0 * _btc_price)
                _btc_move_15m_dollars = abs(corr.btc_move_15m_pct / 100.0 * _btc_price)
                if is_5m:
                    _btc_min_move = self.config.get("btc_min_move_dollars_5m", 37.0)
                    _btc_move = _btc_move_5m_dollars
                else:
                    _btc_min_move = self.config.get("btc_min_move_dollars_15m", 70.0)
                    _btc_move = max(_btc_move_5m_dollars, _btc_move_15m_dollars)
                if _btc_price > 0 and _btc_move < _btc_min_move:
                    if _btc_corr < _low_corr_btc_bypass:
                        logger.debug(
                            f"  {_brand} btc_min_move bypassed (corr={_btc_corr:.2f} < {_low_corr_btc_bypass}) "
                            f"BTC moved ${_btc_move:.0f} < min ${_btc_min_move:.0f}"
                        )
                    else:
                        _bump_skip("btc_min_move_dollars")
                        logger.debug(
                            f"  {_brand} skip '{market.question[:40]}' — "
                            f"BTC moved ${_btc_move:.0f} < min ${_btc_min_move:.0f}"
                        )
                        continue

                # Skip windows where price has already drifted far from 50/50
                _sample("entry_price", yes_price)
                if yes_price < 0.20 or yes_price > 0.80:
                    _bump_skip("price_too_far_from_even")
                    logger.debug(
                        f"  {_brand} skip '{market.question[:40]}' — price {yes_price:.2f} "
                        f"too far from 50/50, window in progress"
                    )
                    continue

                # YES = "Up", NO = "Down"
                if allowed_side == "LONG":
                    action = "BUY_YES"
                    direction = "UP"
                else:
                    action = "SELL_YES"
                    direction = "DOWN"

                # ── Adaptive direction gate ──
                # Instead of manual disable_sell_yes / disable_buy_yes, use the asset's
                # own 1H trend to suppress counter-trend trades. This replaces the static
                # config flags with a dynamic check:
                #   - 1H trend BULLISH  → suppress SELL_YES (don't short in an uptrend)
                #   - 1H trend BEARISH  → suppress BUY_YES  (don't long in a downtrend)
                #   - 1H trend NEUTRAL  → allow both sides
                # The mtt (MultiTimeframeTrend) object is already fetched once per cycle.
                _h1_trend = mtt.h1_trend  # "BULLISH", "BEARISH", or "NEUTRAL"
                if action == "SELL_YES" and _h1_trend == "BULLISH":
                    _bump_skip("sell_yes_suppressed_bullish_1h")
                    logger.info(
                        f"  {self._signal_strategy_name} skip SELL_YES on '{market.question[:40]}' — "
                        f"1H trend BULLISH, suppressing counter-trend short"
                    )
                    continue
                if action == "BUY_YES" and _h1_trend == "BEARISH":
                    _bump_skip("buy_yes_suppressed_bearish_1h")
                    logger.info(
                        f"  {self._signal_strategy_name} skip BUY_YES on '{market.question[:40]}' — "
                        f"1H trend BEARISH, suppressing counter-trend long"
                    )
                    continue
                if self._rsi_blocks_entry(action, sol.rsi_14):
                    _bump_skip("rsi_extreme_block")
                    logger.info(
                        f"  {self._signal_strategy_name} skip {action} on '{market.question[:40]}' — "
                        f"RSI={sol.rsi_14:.1f} hit configured hard gate"
                    )
                    continue
                if self._oracle_basis_blocks_entry(sol.oracle_basis_bps):
                    _bump_skip("oracle_basis_block")
                    logger.info(
                        f"  {self._signal_strategy_name} skip {action} on '{market.question[:40]}' — "
                        f"oracle basis {sol.oracle_basis_bps:+.1f}bps exceeds cap"
                    )
                    continue

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

                    # 1H HISTOGRAM GATE (matches backtest engine htf_key="1h" for SOL)
                    # Relaxed from strict "histogram_rising" to "histogram in trade direction
                    # OR rising". Original gate required acceleration — too strict, blocked
                    # entries for hours during valid trending conditions where histogram was
                    # positive but decelerating (e.g. hist=+0.10, prev=+0.16).
                    _macd_1h = sol.macd_1h
                    _h1_bull_ok = _macd_1h.histogram_rising or _macd_1h.histogram > 0
                    _h1_bear_ok = (not _macd_1h.histogram_rising) or _macd_1h.histogram < 0
                    if allowed_side == "LONG" and not _h1_bull_ok:
                        _bump_skip("histogram_1h_blocks_long_5m")
                        logger.info(
                            f"  {_alt_label} [5m] skip '{market.question[:40]}' — "
                            f"1H histogram negative and falling (hist={_macd_1h.histogram:.4f})"
                        )
                        continue
                    if allowed_side == "SHORT" and not _h1_bear_ok:
                        _bump_skip("histogram_1h_blocks_short_5m")
                        logger.info(
                            f"  {_alt_label} [5m] skip '{market.question[:40]}' — "
                            f"1H histogram positive and rising (hist={_macd_1h.histogram:.4f})"
                        )
                        continue

                    # BTC catalyst gate: require spike or lag in 5m markets to avoid flat-market guesses
                    _require_catalyst_5m = bool(self.config.get("require_btc_catalyst_5m", False))
                    if _require_catalyst_5m and not corr.lag_opportunity and not corr.btc_spike_detected:
                        _bump_skip("no_btc_catalyst_5m")
                        logger.info(
                            f"  {_alt_label} [5m] skip '{market.question[:40]}' — "
                            f"no BTC catalyst (spike={corr.btc_spike_detected} lag={corr.lag_opportunity})"
                        )
                        continue

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
                    if not self._strong_enough_5m_signal(m5_adj):
                        _bump_skip("weak_5m_signal")
                        logger.info(
                            f"  {_alt_label} [5m] skip '{market.question[:40]}' — "
                            f"5m signal too weak (m5_adj={m5_adj:+.2f}, min={self.min_positive_m5_adj_5m:.2f})"
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

                    # Correlation confidence — log for diagnostics.
                    # Light damping on low corr: primary edge is macro+LTF, not correlation.
                    # Previous: 5m used 0.55 cutoff / 0.5 damping (halved edge); 15m used 0.50.
                    # Unified: both use 0.50 cutoff, 0.7 damping (30% reduction, not 50%).
                    if corr.correlation_1h > 0.85:
                        reason_parts.append(f"high_corr({corr.correlation_1h:.2f})")
                    elif self._low_corr_blocks_entry(corr):
                        _bump_skip("low_corr_suppressed")
                        logger.info(
                            f"  {_alt_label} [5m] skip '{market.question[:40]}' — "
                            f"1H corr {corr.correlation_1h:.2f} below hard floor "
                            f"{self.low_corr_threshold_1h:.2f}"
                        )
                        continue
                    elif corr.correlation_1h < self.low_corr_threshold_1h:
                        est_prob_up = 0.50 + (est_prob_up - 0.50) * self.low_corr_damping
                        reason_parts.append(f"low_corr_5m({corr.correlation_1h:.2f})")

                    est_prob_up = max(0.10, min(0.90, est_prob_up))

                    if action == "BUY_YES":
                        edge = est_prob_up - yes_price
                    else:
                        edge = (1.0 - est_prob_up) - (1.0 - yes_price)
                    # Confidence: 5m MACD momentum is PRIMARY for 5m markets; lag removed
                    lag_conf_5m = 0.0
                    confidence = max(0.50, min(0.85, 0.50 + abs(m5_adj) * 2.5 + lag_conf_5m + abs(timing_bonus) * 0.3))

                    reason_parts.extend([
                        "[5m]",
                        "UPDOWN_5m",
                        f"{_spot_key}=${sol_price:,.2f}",
                        f"btc=${corr.btc_price:,.0f}" if corr.btc_price else "",
                        f"est_up={est_prob_up:.3f}",
                        f"mkt_yes={yes_price:.3f}",
                        f"5m_MACD={'+' if macd_5m.macd_line > macd_5m.signal_line else '-'}{abs(macd_5m.histogram):.3f}",
                        f"corr={corr.correlation_1h:.2f}",
                        f"RSI={sol.rsi_14:.0f}",
                    ])
                    reason_parts.extend(m5_reasons)

                    logger.debug(
                        f"  [5m] {_alt_label} updown '{market.question[:45]}' "
                        f"macro={macro_trend} m5_adj={m5_adj:+.2f} "
                        f"est_up={est_prob_up:.3f} edge={edge:.4f}"
                    )

                    estimated_prob = est_prob_up

                else:
                    # ── FIFTEEN-MINUTE UP/DOWN MARKET PATH ──
                    # PRIMARY signal: macro trend + LTF confirmation (live data evidence)
                    # SECONDARY signal: lag / spike (small probability booster only)
                    est_prob_up = 0.50

                    # Macro trend — PRIMARY driver (increased from 0.05 since it's now the gate)
                    est_prob_up = self._apply_primary_htf_bias(
                        est_prob_up, primary_htf_bias, 0.07
                    )

                    # 1H HISTOGRAM GATE (matches backtest engine htf_key="1h" for SOL)
                    # SOL 15m: without gate ~51% WR; with gate ~59.3% WR.
                    # Relaxed: allow when histogram is in trade direction (positive for
                    # LONG) even if decelerating, not just when accelerating. Blocks only
                    # when histogram is actively against the trade direction.
                    _macd_1h = sol.macd_1h
                    _h1_bull_ok = _macd_1h.histogram_rising or _macd_1h.histogram > 0
                    _h1_bear_ok = (not _macd_1h.histogram_rising) or _macd_1h.histogram < 0
                    if allowed_side == "LONG" and not _h1_bull_ok:
                        _bump_skip("histogram_1h_blocks_long_15m")
                        logger.info(
                            f"  {_alt_label} [15m] skip '{market.question[:40]}' — "
                            f"1H histogram negative and falling (hist={_macd_1h.histogram:.4f})"
                        )
                        continue
                    if allowed_side == "SHORT" and not _h1_bear_ok:
                        _bump_skip("histogram_1h_blocks_short_15m")
                        logger.info(
                            f"  {_alt_label} [15m] skip '{market.question[:40]}' — "
                            f"1H histogram positive and rising (hist={_macd_1h.histogram:.4f})"
                        )
                        continue

                    # LTF confirmation — PRIMARY probability driver (increased from 0.18)
                    ltf_adj = ltf_strength * 0.22
                    est_prob_up += ltf_adj if allowed_side == "LONG" else -ltf_adj

                    # Timing / 5m momentum
                    if allowed_side == "LONG":
                        est_prob_up += timing_bonus
                    else:
                        est_prob_up -= timing_bonus

                    # RSI extremes
                    if sol.rsi_14 > 75:
                        est_prob_up -= 0.03
                    elif sol.rsi_14 < 25:
                        est_prob_up += 0.03

                    # Correlation confidence — unified with 5m path.
                    # Light damping: primary edge is macro+LTF, not correlation.
                    if corr.correlation_1h > 0.85:
                        reason_parts.append(f"high_corr({corr.correlation_1h:.2f})")
                    elif self._low_corr_blocks_entry(corr):
                        _bump_skip("low_corr_suppressed")
                        logger.info(
                            f"  {_alt_label} [15m] skip '{market.question[:40]}' — "
                            f"1H corr {corr.correlation_1h:.2f} below hard floor "
                            f"{self.low_corr_threshold_1h:.2f}"
                        )
                        continue
                    elif corr.correlation_1h < self.low_corr_threshold_1h:
                        est_prob_up = 0.50 + (est_prob_up - 0.50) * self.low_corr_damping
                        reason_parts.append(f"low_corr({corr.correlation_1h:.2f})")

                    est_prob_up = max(0.10, min(0.90, est_prob_up))

                    if action == "BUY_YES":
                        edge = est_prob_up - yes_price
                    else:
                        edge = (1.0 - est_prob_up) - (1.0 - yes_price)
                    # Confidence driven by LTF strength (primary); lag signal removed
                    confidence = min(0.85, 0.50 + ltf_strength * 0.22 + abs(timing_bonus) * 0.5)

                    reason_parts.extend([
                        "UPDOWN_15m",
                        f"{_spot_key}=${sol_price:,.2f}",
                        f"btc=${corr.btc_price:,.0f}" if corr.btc_price else "",
                        f"est_up={est_prob_up:.3f}",
                        f"mkt_yes={yes_price:.3f}",
                        f"corr={corr.correlation_1h:.2f}",
                        f"RSI={sol.rsi_14:.0f}",
                    ])
                    reason_parts.extend(ltf_reasons)
                    if timing_reasons:
                        reason_parts.extend(timing_reasons)

                    estimated_prob = est_prob_up

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
                    action = "BUY_YES" if direction == "UP" else "SELL_YES"
                else:
                    action = "SELL_YES" if direction == "UP" else "BUY_YES"

                if self._rsi_blocks_entry(action, sol.rsi_14):
                    _bump_skip("threshold_rsi_block")
                    logger.info(
                        f"  {self._signal_strategy_name} skip {action} on '{market.question[:40]}' — "
                        f"RSI={sol.rsi_14:.1f} hit configured hard gate"
                    )
                    continue
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

                if action == "BUY_YES":
                    edge = estimated_prob - yes_price
                else:
                    edge = (1.0 - estimated_prob) - (1.0 - yes_price)
                reason_parts.extend([
                    f"{_spot_key}=${sol_price:,.2f}",
                    f"btc=${corr.btc_price:,.0f}" if corr.btc_price else "",
                    f"target=${threshold:,.2f}",
                    f"dist={distance_pct:.1%}",
                    f"est_prob={estimated_prob:.2f}",
                    f"mkt_yes={yes_price:.2f}",
                    f"corr={corr.correlation_1h:.2f}",
                    f"macro_leg={corr.opportunity_magnitude:+.2f}%" if corr.lag_opportunity else "",
                ])
                reason_parts.extend(ltf_reasons)
                if timing_reasons:
                    reason_parts.extend(timing_reasons)

                confidence = min(0.85, 0.50 + ltf_strength * 0.20 + timing_bonus + distance_pct * 0.5)

                # AI-hold soft veto: block any entry (marginal or strong) if AI said HOLD
                # on this market within the veto TTL.
                _hold_ts = self._ai_hold_cache.get(market.id, 0)
                _hold_age = time.time() - _hold_ts
                if _hold_age < self.ai_hold_veto_ttl_sec:
                    if edge < self.min_edge_5m_ai_override:
                        logger.info(
                            f"  {self._signal_strategy_name} ai-hold veto '{market.question[:45]}' — "
                            f"edge={edge:.4f} < override={self.min_edge_5m_ai_override:.4f} "
                            f"(AI said HOLD {_hold_age:.0f}s ago)"
                        )
                        continue

                # AI tiebreaker for marginal edge (skipped when AI offline or use_ai false)
                if edge < self.min_edge and edge > 0.03:
                    if not self.config.get("use_ai", True):
                        _bump_skip("ai_disabled_marginal_threshold")
                        logger.debug(
                            f"{_brand}: use_ai=false — skipping marginal trade "
                            f"'{market.question[:40]}...' edge={edge:.4f}"
                        )
                        continue
                    if not self.ai_agent.is_available():
                        _bump_skip("ai_offline_marginal_threshold")
                        logger.debug(
                            f"{_brand}: AI offline — skipping marginal trade "
                            f"'{market.question[:40]}...' edge={edge:.4f}"
                        )
                        continue
                    if ai_calls >= self.max_ai_calls_per_scan:
                        _bump_skip("ai_call_limit_marginal_threshold")
                        logger.debug(
                            f"{_brand}: max AI calls per scan ({self.max_ai_calls_per_scan}) — "
                            f"skipping marginal '{market.question[:40]}...'"
                        )
                        continue
                    ai_context = (
                        f"{market.description}\n\n"
                        f"=== LIVE {_alt_label} DATA ===\n"
                        f"{_alt_label} Price: ${sol_price:,.2f} | Threshold: ${threshold:,.2f} ({direction})\n"
                        f"{_alt_label} Oracle: {sol.chainlink_network or 'n/a'} "
                        f"{f'${sol.chainlink_price:,.2f}' if sol.chainlink_price is not None else 'n/a'} | "
                        f"basis={f'{sol.oracle_basis_bps:+.1f}bps' if sol.oracle_basis_bps is not None else 'n/a'}\n"
                        f"Distance: {distance_pct:.1%} | Days left: {days_to_resolution}\n\n"
                    ) + (
                        f"=== BTC-{_alt_label} CORRELATION ===\n"
                        f"BTC: ${corr.btc_price:,.2f} | Correlation: {corr.correlation_1h:.2f}\n"
                        f"BTC spike: {corr.btc_spike_detected} ({corr.btc_move_5m_pct:+.2f}%)\n"
                        f"{_alt_label} macro leg: {corr.lag_opportunity} dir={corr.opportunity_direction} mag={corr.opportunity_magnitude:+.2f}%\n\n"
                        f"=== MACRO (1H) — {macro_trend} ===\n"
                        f"BTC 4H bias: {primary_htf_bias}\n"
                        f"EMA: 9=${sol.ema_9:,.2f} 21=${sol.ema_21:,.2f} 50=${sol.ema_50:,.2f}\n"
                        f"RSI: {sol.rsi_14:.1f}\n\n"
                        f"=== 15m CONFIRMATION ===\n"
                        f"15m MACD: hist={sol.macd_15m.histogram:+.3f} {sol.macd_15m.crossover}\n\n"
                        f"Allowed side: {allowed_side}\n"
                        f"Quant edge={edge:.4f} min_edge={(self.min_edge_5m if is_5m else self.min_edge):.4f}\n"
                        f"Should we take this {action} trade, or HOLD?\n"
                        f"\n=== MARKET ===\n{format_market_metadata(market)}"
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
                    # Log reasoning so we can audit what the model is actually deciding
                    if ai_analysis:
                        logger.info(
                            f"  {self._signal_strategy_name} AI [{ai_analysis.recommendation} "
                            f"conf={ai_analysis.confidence_score:.2f} p={ai_analysis.estimated_probability:.3f}] "
                            f"'{market.question[:45]}' | {ai_analysis.reasoning[:120]}"
                        )
                    if not ai_analysis:
                        _bump_skip("ai_none_marginal_threshold")
                        logger.critical(
                            "%s: AI returned None after provider call for market %s (%s)",
                            _brand,
                            market.id,
                            self._signal_strategy_name,
                        )
                        continue
                    if ai_analysis.recommendation == "HOLD":
                        self._ai_hold_cache[market.id] = time.time()
                        _bump_skip("ai_hold_marginal_threshold")
                        logger.debug(f"{_brand}: AI says HOLD on '{market.question[:40]}...' — veto cached {self.ai_hold_veto_ttl_sec}s")
                        continue
                    if not ai_recommendation_supports_action(
                        ai_analysis.recommendation, action
                    ):
                        _bump_skip("ai_veto_marginal_threshold")
                        logger.debug(
                            f"{_brand}: AI {ai_analysis.recommendation} conflicts with {action} "
                            f"on '{market.question[:40]}...'"
                        )
                        continue
                    if ai_analysis.confidence_score < self.ai_confidence_threshold:
                        _bump_skip("ai_low_confidence_marginal_threshold")
                        logger.debug(
                            f"{_brand}: AI confidence {ai_analysis.confidence_score:.2f} "
                            f"< {self.ai_confidence_threshold} marginal '{market.question[:40]}...'"
                        )
                        continue
                    ai_prob_yes = float(ai_analysis.estimated_probability)
                    ai_edge = (
                        ai_prob_yes - yes_price
                        if action == "BUY_YES"
                        else yes_price - ai_prob_yes
                    )
                    if ai_edge <= 0:
                        _bump_skip("ai_nonpositive_edge_marginal_threshold")
                        logger.debug(
                            f"{_brand}: non-positive ai_edge={ai_edge:.4f} marginal "
                            f"'{market.question[:40]}...'"
                        )
                        continue
                    edge = max(edge, ai_edge)
                    confidence = max(confidence, ai_analysis.confidence_score)
                    reason_parts.append("ai_marginal_confirm")

            # ── Final filters (both paths) ──
            effective_min_edge = self.min_edge_5m if is_5m else self.min_edge
            # No 15m LTF confirmation: require stronger edge for 15m updown (proceeding on macro only)
            if ltf_strength == 0.0 and is_updown and not is_5m:
                effective_min_edge = max(
                    effective_min_edge, self.min_edge_15m_when_ltf_unconfirmed
                )

            # Updown marginal (parity with BTC): quant edge just below bar — AI confirms action + edge
            if (
                is_updown
                and edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and _ai_window_open
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and self.ai_agent.is_available()
                and ai_calls < self.max_ai_calls_per_scan
            ):
                _win = "5m" if is_5m else "15m"
                ai_context2 = (
                    f"{market.description}\n\n"
                    f"=== {_alt_label} UPDOWN CONTEXT ({_win}) ===\n"
                    f"{_alt_label}: ${sol_price:,.2f} | YES={yes_price:.3f} | action={action} | allowed={allowed_side}\n"
                    f"Oracle={sol.chainlink_network or 'n/a'} "
                    f"{f'${sol.chainlink_price:,.2f}' if sol.chainlink_price is not None else 'n/a'} "
                    f"basis={f'{sol.oracle_basis_bps:+.1f}bps' if sol.oracle_basis_bps is not None else 'n/a'}\n"
                    f"BTC_HTF={primary_htf_bias} | ALT_HTF={macro_trend} | "
                    f"Quant edge={edge:.4f} required>={effective_min_edge:.4f}\n"
                    f"BTC ${corr.btc_price:,.2f} corr1h={corr.correlation_1h:.3f} "
                    f"macro_opp={corr.lag_opportunity} mag={corr.opportunity_magnitude:+.2f}%\n"
                    f"15m MACD hist={sol.macd_15m.histogram:+.3f} {sol.macd_15m.crossover}\n"
                    f"LTF_strength={ltf_strength:.2f}\n\n"
                    f"=== MARKET ===\n{format_market_metadata(market)}\n\n"
                    "Answer with BUY_YES, BUY_NO, or HOLD."
                )
                ai2 = await self.ai_agent.analyze_market(
                    market_question=market.question,
                    market_description=ai_context2,
                    current_yes_price=yes_price,
                    market_id=market.id,
                    strategy_hint=self._signal_strategy_name,
                )
                ai_calls += 1
                ai_used = True
                if not ai2:
                    _bump_skip("ai_none_marginal_updown")
                    logger.critical(
                        "%s: AI returned None after provider call for market %s (updown marginal, %s)",
                        _brand,
                        market.id,
                        self._signal_strategy_name,
                    )
                elif ai2.recommendation == "HOLD":
                    _bump_skip("ai_hold_marginal_updown")
                    logger.debug(f"{_brand}: AI HOLD updown marginal '{market.question[:40]}...'")
                elif not ai_recommendation_supports_action(ai2.recommendation, action):
                    _bump_skip("ai_veto_marginal_updown")
                    logger.debug(
                        f"{_brand}: AI veto updown marginal {ai2.recommendation} vs {action}"
                    )
                elif ai2.confidence_score < self.ai_confidence_threshold:
                    _bump_skip("ai_low_confidence_marginal_updown")
                    logger.debug(f"{_brand}: AI low conf updown marginal")
                else:
                    ap = float(ai2.estimated_probability)
                    ae = ap - yes_price if action == "BUY_YES" else yes_price - ap
                    if ae > 0:
                        edge = max(edge, ae)
                        confidence = max(confidence, ai2.confidence_score)
                        reason_parts.append("ai_updown_confirm")
                    else:
                        _bump_skip("ai_nonpositive_edge_marginal_updown")
            elif (
                is_updown
                and edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and not _ai_window_open
            ):
                _bump_skip("ai_window_closed_marginal_updown")
                logger.debug(
                    f"{_brand}: AI window closed for marginal updown '{market.question[:40]}...' "
                    f"({_mins_left:.1f}m left)"
                )

            try:
                _sample("est_prob_up", est_prob_up)
            except NameError:
                pass
            _sample("edge", edge)
            if edge < effective_min_edge:
                _bump_skip("edge_below_min")
                _mkt_type = "5m" if is_5m else (
                    "15m_unconf" if (is_updown and ltf_strength == 0.0) else
                    ("15m" if is_updown else "threshold")
                )
                logger.info(
                    f"  {_brand} skip '{market.question[:40]}...' edge={edge:.4f} < min={effective_min_edge} ({_mkt_type})"
                )
                continue

            # ── Entry price filter for updown markets ──
            # Only trade when yes_price is within [entry_price_min, entry_price_max].
            # This band prevents entering when the market has already moved:
            #   - BUY_YES at yes_price > max: market too bullish, lag already priced in
            #   - BUY_YES at yes_price < min: market too bearish, going long against consensus
            #   - SELL_YES at yes_price < min: market too bearish, lag already priced in
            #   - SELL_YES at yes_price > max: market too bullish, going short against consensus
            #
            # Live data (2026-04-24 session, 29 trades):
            #   market YES in [0.46, 0.54] → 72% WR  (sweet spot)
            #   market YES < 0.46 or > 0.54 → ~30% WR (market consensus fighting signal)
            if is_updown:
                _yp_low = self.entry_price_min
                _yp_high = self.entry_price_max
                if yes_price < _yp_low or yes_price > _yp_high:
                    _bump_skip("entry_price_band_updown")
                    logger.info(
                        f"  {self._signal_strategy_name} skip '{market.question[:40]}...' "
                        f"yes_price={yes_price:.3f} outside [{_yp_low:.3f}, {_yp_high:.3f}] "
                        f"(market already moved, signal has no edge)"
                    )
                    continue

            # ── Edge cap for updown markets ──
            # Live data: SOL updown edge >0.09 = 22% WR. Large edges mean SOL has ALREADY
            # moved in the lag window — the catch-up opportunity is gone, not starting.
            if is_updown:
                _max_edge_updown = self.config.get("max_edge_updown", 0.09)
                if edge > _max_edge_updown:
                    _bump_skip("edge_above_cap")
                    logger.info(
                        f"  {_brand} skip '{market.question[:40]}...' edge={edge:.4f} "
                        f"> max={_max_edge_updown} updown cap (catch-up already priced in)"
                    )
                    continue

            # Position sizing
            if not self.kelly_sizer:
                _bump_skip("kelly_unavailable")
                logger.error("%s strategy: KellySizer unavailable — skipping entry sizing", _brand)
                continue
            raw_size = self.kelly_sizer.size_from_edge(
                self._signal_strategy_name, bankroll, edge
            )
            final_size = self.exposure_manager.scale_size(raw_size)
            if final_size < 0.5:
                _bump_skip("size_too_small")
                continue
            reason_parts.append(f"exp={exp_tier.value}(x{exp_multiplier:.1f})")

            reason_str = " | ".join(r for r in reason_parts if r)

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
                window_size="5m" if is_5m else "15m",
                hour_utc=datetime.now(timezone.utc).hour,
                est_prob=round(estimated_prob, 4),
                rsi=round(sol.rsi_14, 1),
                corr_1h=round(corr.correlation_1h, 4),
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
                        "window_size": "5m" if is_5m else "15m",
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

        if signals:
            logger.info(f"{_brand} strategy: {len(signals)} signals generated")
        elif sol_markets:
            top_reason = max(skip_reasons, key=skip_reasons.get) if skip_reasons else "no_eligible_markets"
            logger.info(
                f"{_brand} strategy: 0 signals from {len(sol_markets)} markets "
                f"(BTC_HTF={primary_htf_bias} ALT_HTF={macro_trend} top_skip={top_reason})"
            )
        gate_distributions = {k: _summarize(v) for k, v in gate_samples.items()}
        if gate_samples:
            logger.info(f"  [gate-dist] {gate_distributions}")
        self.last_scan_stats = {
            "enabled": True,
            "signals": len(signals),
            "markets_considered": len(sol_markets),
            "btc_htf_bias": primary_htf_bias,
            "alt_htf_bias": macro_trend,
            "top_skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: kv[1], reverse=True)[:8]),
            "gate_distributions": gate_distributions,
        }
        return signals


def _get_weekend_penalty() -> float:
    """Return weekend penalty multiplier (1.0=normal, 0.0=full penalty).

    Reduces position size during weekend / low-liquidity periods when
    HYPE-style manipulation (a4385 CEX pump) is most likely to occur.
    """
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()  # 0=Mon … 5=Sat, 6=Sun
    utc_hour = now_utc.hour

    # Weekend (Sat/Sun full UTC days)
    if weekday >= 5:  # Saturday = 5, Sunday = 6
        return 0.50

    # Friday 20:00 UTC through Saturday 08:00 UTC — elevated manipulation risk
    if weekday == 4 and utc_hour >= 20:
        return 0.70

    return 1.0
