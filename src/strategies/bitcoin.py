"""
Bitcoin Up/Down Strategy v4 — Hierarchical Trend Filter

RULE HIERARCHY (strict — no exceptions):
═══════════════════════════════════════

LAYER 1: HIGHER TIMEFRAME TREND (4H) — THE LAW
  Determined by: Trend Sabre trend direction + price vs Sabre MA + 4H MACD above/below zero
  ► If 4H is BULLISH → ONLY allow LONG signals (BUY_YES on UP markets, SELL_YES on DOWN markets)
  ► If 4H is BEARISH → ONLY allow SHORT signals (SELL_YES on UP markets, BUY_YES on DOWN markets)
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
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, Field

from src.market.scanner import Market
from src.analysis.ai_agent import AIAgent
from src.analysis.math_utils import PositionSizer
from src.analysis.btc_price_service import BTCPriceService, TechnicalAnalysis
from src.analysis.kelly_sizer import KellySizer
from src.execution.exposure_manager import ExposureManager, MarketConditions, ExposureTier
from src.strategies.strategy_config import resolve_enabled_flag
from src.strategies.strategy_ai_context import (
    ai_recommendation_supports_action,
    format_market_metadata,
)

logger = logging.getLogger(__name__)


class BitcoinSignal(BaseModel):
    """Represents a signal on a Bitcoin price market."""
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
    direction: str = Field(..., description="UP or DOWN — what this market is betting on")
    btc_threshold: Optional[float] = Field(None, description="BTC price threshold from question")
    btc_current: Optional[float] = Field(None, description="Current BTC price at signal time")
    ai_used: bool = Field(default=False, description="Whether AI was consulted")
    reason: str = Field(default="", description="Why this signal was generated")
    # Coach features — logged to journal extra dict for pattern analysis
    htf_bias: Optional[str] = Field(None, description="HTF bias at entry: BULLISH/BEARISH/NEUTRAL")
    window_size: Optional[str] = Field(None, description="Market window: 5m or 15m")
    hour_utc: Optional[int] = Field(None, description="UTC hour at entry time")
    est_prob: Optional[float] = Field(None, description="Estimated prob of YES at entry (key diagnostic)")
    rsi: Optional[float] = Field(None, description="BTC RSI-14 at entry")


# Patterns to detect Bitcoin price markets
BTC_PATTERNS = [
    re.compile(r'\bbitcoin\b', re.IGNORECASE),
    re.compile(r'\bbtc\b', re.IGNORECASE),
]
# Detect 15-minute or 5-minute "Up or Down" markets (pattern matches both)
UPDOWN_PATTERN = re.compile(r'(?:bitcoin|btc)\s+up\s+or\s+down', re.IGNORECASE)


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
                 kelly_sizer=None, exposure_manager: ExposureManager = None):
        self.full_config = config
        self.config = config.get('strategies', {}).get('bitcoin', {})
        self.ai_agent = ai_agent
        self.position_sizer = position_sizer
        self.kelly_sizer = kelly_sizer or KellySizer(config)
        self.btc_service = BTCPriceService()
        self.exposure_manager = exposure_manager or ExposureManager(config)
        self._signal_strategy_name = "bitcoin"
        self.dead_zone_skip_callback = None
        if self.exposure_manager:
            self.exposure_manager._on_pause_ai_callback = self._ai_kill_switch_analysis

        self.enabled = resolve_enabled_flag(
            "bitcoin",
            self.config,
            logger=logger,
        )
        self.min_liquidity = self.config.get('min_liquidity', 10000)
        self.min_edge = self.config.get('min_edge', 0.08)
        self.min_edge_5m = self.config.get('min_edge_5m', self.min_edge)  # 5m-specific edge threshold
        self.ai_confidence_threshold = self.config.get('ai_confidence_threshold', 0.60)
        self.max_ai_calls_per_scan = int(self.config.get("max_ai_calls_per_scan", 8))
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
        self.min_edge_5m_ai_override = self.config.get("min_edge_5m_ai_override", 0.10)

        # Observability snapshot populated each scan (used by ops pulse / dashboard status).
        self.last_scan_stats: Dict[str, Any] = {}

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

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────

    def _is_bitcoin_market(self, market: Market) -> bool:
        text = f"{market.question} {market.description}".lower()
        return any(p.search(text) for p in BTC_PATTERNS)

    def _is_updown_market(self, market: Market) -> bool:
        """Check if this is a Bitcoin Up or Down market (matches both 15m and 5m)."""
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

        # The main loop scans every ~5m. Expand slightly so scans/latency don't
        # repeatedly miss a narrow valid window by seconds.
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

        # Prediction window bonus
        if mom.m15_in_prediction_window:
            bonus += 0.03
            reasons.append("15m predict window")
        if mom.m5_in_prediction_window:
            bonus += 0.02
            reasons.append("5m predict window")

        return bonus, reasons

    # ──────────────────────────────────────────────────────────────
    # LAYER 4: Edge Calculation
    # ──────────────────────────────────────────────────────────────

    def _estimate_probability(
        self,
        btc_price: float,
        threshold: float,
        direction: str,
        ta: TechnicalAnalysis,
        days_to_resolution: int,
        ltf_strength: float,
        timing_bonus: float,
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

        # 8. Time decay — more time = more uncertainty
        if days_to_resolution > 0:
            time_factor = min(1.0, days_to_resolution / 60.0)
            base_prob = base_prob * (1 - time_factor * 0.3) + 0.50 * (time_factor * 0.3)

        final = base_prob + ltf_adj + timing_bonus + tension_adj + rsi_adj + sr_adj + vp_adj
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

        # ══════════════════════════════════════════════════════════
        # LAYER 1: Determine higher TF bias — this gates everything
        # ══════════════════════════════════════════════════════════
        htf_bias = self._get_higher_tf_bias(ta)

        logger.info(
            f"BTC ${btc_price:,.0f} | HTF BIAS: {htf_bias} | "
            f"Sabre={'BULL' if sabre.trend==1 else 'BEAR'} MA=${sabre.ma_value:,.0f} "
            f"Trail=${sabre.trail_value:,.0f} tension={sabre.tension:+.1f} | "
            f"4H MACD hist={macd_4h.histogram:+.0f} {'above' if macd_4h.above_zero else 'below'}0 "
            f"{macd_4h.crossover} | "
            f"15m MACD hist={macd_15m.histogram:+.2f} {macd_15m.crossover} | "
            f"RSI={ta.rsi_14:.0f} | "
            f"Mom 15m={mom.m15_direction}({mom.m15_move_pct:+.3f}%) "
            f"5m={mom.m5_direction}({mom.m5_move_pct:+.3f}%)"
        )

        # BUG FIX: has_updown must be assigned before the NEUTRAL check that reads it
        has_updown = any(self._is_updown_market(m) for m in btc_markets)

        if htf_bias == "NEUTRAL":
            if not has_updown:
                logger.info("Bitcoin strategy: HTF bias NEUTRAL — sitting out this cycle")
                return []
            # NEUTRAL + updown markets: lean on Sabre direction (most responsive to price action).
            # Full BULLISH/BEARISH bias not established — tighter edge (0.09) enforced per-market.
            allowed_side = "LONG" if sabre.trend == 1 else "SHORT"
            logger.info(
                f"Bitcoin: HTF NEUTRAL + updown markets → lean={allowed_side} "
                f"(Sabre={'BULL' if sabre.trend==1 else 'BEAR'}) — tighter edge required"
            )
        else:
            # Determine allowed trading side based on HTF bias
            # BULLISH HTF → only LONG (buy the dip, ride the trend up)
            # BEARISH HTF → only SHORT (sell the rip, ride the trend down)
            allowed_side = "LONG" if htf_bias == "BULLISH" else "SHORT"

        # ══════════════════════════════════════════════════════════
        # LAYER 2: Check 15m MACD confirmation
        # ══════════════════════════════════════════════════════════
        ltf_confirmed, ltf_strength, ltf_reasons = self._check_lower_tf_confirmation(ta, allowed_side)

        # ANTI-LTF GATE: Backtest (90 days, 1904 → 1119 trades) shows:
        #   LTF confirmed   (strength >= 0.35) → 49.5% WR  ← BAD, MACD fires after the move peaks
        #   LTF unconfirmed (strength < 0.35)  → 54.9% WR  ← GOOD, early momentum phase
        # Trading the early-momentum window (before 15m MACD catches up) captures the
        # trend continuation phase. Once confirmed, the window is at exhaustion risk.
        if ltf_confirmed:
            self.last_scan_stats = {
                "enabled": True,
                "signals": 0,
                "ai_calls": 0,
                "ai_assists": 0,
                "ai_vetos": 0,
                "ai_holds": 0,
                "htf_bias": htf_bias,
                "allowed_side": allowed_side,
                "ltf_strength": round(float(ltf_strength), 4),
                "top_skip_reasons": {"ltf_confirmed_late_entry": 1},
                "gate_distributions": {},
            }
            logger.info(
                f"Bitcoin strategy: LTF confirmed = late-entry risk (MACD already crossed), "
                f"skipping. strength={ltf_strength:.2f}"
            )
            return []

        logger.info(f"  Anti-LTF gate passed: {allowed_side} — early momentum, strength={ltf_strength:.2f} (unconfirmed)")

        # ══════════════════════════════════════════════════════════
        # LAYER 3: Check timing
        # ══════════════════════════════════════════════════════════
        timing_bonus, timing_reasons = self._check_timing(ta, allowed_side)
        if timing_reasons:
            logger.info(f"  Timing: bonus={timing_bonus:+.3f} [{', '.join(timing_reasons)}]")

        # ══════════════════════════════════════════════════════════
        # LAYER 4: Evaluate each market
        # ══════════════════════════════════════════════════════════
        signals = []
        ai_calls = 0
        ai_assists = 0
        ai_vetos = 0
        ai_holds = 0
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

        for market in btc_markets:
            if market.liquidity > 0 and market.liquidity < self.min_liquidity:
                continue

            yes_price = market.yes_price
            is_updown = self._is_updown_market(market)
            is_5m = self._is_5m_market(market) if is_updown else False
            ai_used = False
            threshold = None
            direction = "UP"  # default; overridden below
            reason_parts = [f"HTF={htf_bias}", f"side={allowed_side}"]
            dead_zone_would_block = False
            dead_zone_hour = None

            # ── UP/DOWN MARKETS (15m or 5m) ──
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
                if is_5m:
                    # 5m candles: enter near candle open. Optionally auto-align to scan cadence.
                    _win_min, _win_max = self._resolve_entry_window_bounds(
                        is_5m=True,
                        default_min=2.5,
                        default_max=4.5,
                    )
                else:
                    # 15m candles: enter near candle open. Optionally auto-align to scan cadence.
                    _win_min, _win_max = self._resolve_entry_window_bounds(
                        is_5m=False,
                        default_min=12.5,
                        default_max=14.5,
                    )
                _sample("mins_left", _mins_left)
                if _mins_left < _win_min or _mins_left > _win_max:
                    _bump_skip("outside_entry_window")
                    logger.debug(
                        f"  BTC skip '{market.question[:40]}' — "
                        f"{_mins_left:.1f}m left, need {_win_min}–{_win_max}m window"
                    )
                    continue
                _ai_window_open = self._within_ai_decision_window(
                    mins_left=_mins_left,
                    is_5m=is_5m,
                )

                # Skip markets where price has already moved far from 50/50
                # (means the window is mid-resolution and market has "decided")
                _sample("entry_price", yes_price)
                if yes_price < 0.20 or yes_price > 0.80:
                    _bump_skip("price_too_far_from_50_50")
                    logger.debug(
                        f"  BTC skip '{market.question[:40]}' — price {yes_price:.2f} "
                        f"too far from 50/50, window likely in progress"
                    )
                    continue

                # In updown markets: LONG → BUY_YES (bet on UP), SHORT → SELL_YES (bet on DOWN)
                if allowed_side == "LONG":
                    action = "BUY_YES"
                    direction = "UP"
                else:
                    action = "SELL_YES"
                    direction = "DOWN"

                # ── BUY_YES kill switch ──
                # Live data: BUY_YES = 6 trades, 33% WR, -$4.93.
                # All profit comes from SELL_YES (78 trades, 51% WR, +$8.15).
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
                    est_prob_up = 0.50

                    # HTF bias — same as 15m, the 4H trend still matters for 5m bets
                    htf_boost = 0.0
                    if sabre.trend == 1 and macd_4h.above_zero:
                        htf_boost = 0.04  # Strong bull (slightly tighter than 15m)
                    elif sabre.trend == 1 or macd_4h.above_zero:
                        htf_boost = 0.02  # Partial bull
                    elif sabre.trend == -1 and not macd_4h.above_zero:
                        htf_boost = -0.04  # Strong bear
                    elif sabre.trend == -1 or not macd_4h.above_zero:
                        htf_boost = -0.02  # Partial bear
                    est_prob_up += htf_boost

                    # 4H/1H HISTOGRAM GATE (matches backtest engine)
                    # Primary: 4H histogram must be building in trade direction.
                    # Fallback: if 4H is decelerating but 1H is building, allow entry
                    # (catches local momentum recovery within larger trend structure).
                    macd_1h = ta.macd_1h
                    if allowed_side == "LONG" and not macd_4h.histogram_rising:
                        if not macd_1h.histogram_rising:
                            _bump_skip("hist_gate_5m_long_reject")
                            logger.info(
                                f"  BTC [5m] skip '{market.question[:40]}' — "
                                f"4H falling, 1H also falling — no momentum building for LONG"
                            )
                            continue
                        logger.info(
                            f"  BTC [5m] 1H gate pass '{market.question[:40]}' — "
                            f"4H falling but 1H rising — local momentum recovery"
                        )
                    if allowed_side == "SHORT" and macd_4h.histogram_rising:
                        if macd_1h.histogram_rising:
                            _bump_skip("hist_gate_5m_short_reject")
                            logger.info(
                                f"  BTC [5m] skip '{market.question[:40]}' — "
                                f"4H rising, 1H also rising — no momentum building for SHORT"
                            )
                            continue
                        logger.info(
                            f"  BTC [5m] 1H gate pass '{market.question[:40]}' — "
                            f"4H rising but 1H falling — local momentum recovery SHORT"
                        )

                    # 5m momentum direction — the primary LTF signal for 5m markets
                    # mom.m5_direction: SPIKE_UP, DRIFT_UP, LEAN_UP, NONE, LEAN_DOWN, DRIFT_DOWN, SPIKE_DOWN
                    m5_dir = mom.m5_direction
                    m5_adj = 0.0
                    m5_reasons = []
                    if allowed_side == "LONG":
                        if m5_dir == "SPIKE_UP":
                            m5_adj = 0.06
                            m5_reasons.append(f"5m SPIKE_UP ({mom.m5_move_pct:+.3f}%)")
                        elif m5_dir == "DRIFT_UP":
                            m5_adj = 0.04
                            m5_reasons.append(f"5m DRIFT_UP ({mom.m5_move_pct:+.3f}%)")
                        elif m5_dir == "LEAN_UP":
                            m5_adj = 0.01  # Weak nudge — don't rely on it alone
                            m5_reasons.append(f"5m LEAN_UP ({mom.m5_move_pct:+.3f}%)")
                        elif m5_dir in ("SPIKE_DOWN", "DRIFT_DOWN"):
                            m5_adj = -0.04  # 5m moving against us — penalty
                            m5_reasons.append(f"5m against ({m5_dir})")
                        elif m5_dir == "LEAN_DOWN":
                            m5_adj = -0.01  # Weak opposing nudge
                            m5_reasons.append(f"5m LEAN_DOWN ({mom.m5_move_pct:+.3f}%)")
                    else:  # SHORT
                        if m5_dir == "SPIKE_DOWN":
                            m5_adj = 0.06
                            m5_reasons.append(f"5m SPIKE_DOWN ({mom.m5_move_pct:+.3f}%)")
                        elif m5_dir == "DRIFT_DOWN":
                            m5_adj = 0.04
                            m5_reasons.append(f"5m DRIFT_DOWN ({mom.m5_move_pct:+.3f}%)")
                        elif m5_dir == "LEAN_DOWN":
                            m5_adj = 0.01
                            m5_reasons.append(f"5m LEAN_DOWN ({mom.m5_move_pct:+.3f}%)")
                        elif m5_dir in ("SPIKE_UP", "DRIFT_UP"):
                            m5_adj = -0.04
                            m5_reasons.append(f"5m against ({m5_dir})")
                        elif m5_dir == "LEAN_UP":
                            m5_adj = -0.01
                            m5_reasons.append(f"5m LEAN_UP against ({mom.m5_move_pct:+.3f}%)")

                    if allowed_side == "LONG":
                        est_prob_up += m5_adj
                    else:
                        est_prob_up -= m5_adj

                    # 5m prediction window bonus
                    if mom.m5_in_prediction_window:
                        if allowed_side == "LONG":
                            est_prob_up += 0.02
                        else:
                            est_prob_up -= 0.02
                        m5_reasons.append("5m predict window")

                    # RSI adjustments — expanded from 80/20 to 65/35 (lighter weight for noisy 5m)
                    if ta.rsi_14 > 80:
                        est_prob_up -= 0.02
                    elif ta.rsi_14 > 65:
                        est_prob_up -= 0.01
                    elif ta.rsi_14 < 20:
                        est_prob_up += 0.02
                    elif ta.rsi_14 < 35:
                        est_prob_up += 0.01

                    # NOTE: 4H histogram hard gate is applied above (continue on mismatch).
                    # If we reach here, 4H histogram is already aligned — no extra soft boost needed.

                    est_prob_up = max(0.10, min(0.90, est_prob_up))

                    if action == "BUY_YES":
                        edge = est_prob_up - yes_price
                    else:
                        edge = (1.0 - est_prob_up) - (1.0 - yes_price)
                    # Confidence: HTF boost weight doubled (macro direction matters more for
                    # 5m timing) + m5 momentum strength.  Floor at 0.45 so weak signals
                    # don't produce unrealistically low confidence values.
                    confidence = max(0.45, min(0.85, 0.50 + abs(htf_boost) * 2.5 + abs(m5_adj) * 1.5))

                    reason_parts.extend([
                        "[5m]",
                        "UPDOWN_5m",
                        f"btc=${btc_price:,.0f}",
                        f"est_up={est_prob_up:.3f}",
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
                        f"est_up={est_prob_up:.3f} edge={edge:.4f}"
                    )

                else:
                    # ── FIFTEEN-MINUTE UP/DOWN MARKET PATH ──
                    # Estimate probability from technical analysis
                    # Base: 0.50 (coin flip) + adjustments from HTF, LTF and timing
                    est_prob_up = 0.50

                    # HTF bias — requires ALL 3 votes (Sabre + price vs MA + 4H MACD).
                    # 2/3 votes produce near-random win rates that can't cover slippage.
                    htf_boost = 0.0
                    _price_above_ma = btc_price > sabre.ma_value
                    if sabre.trend == 1 and _price_above_ma and macd_4h.above_zero:
                        htf_boost = 0.08  # All 3 votes bullish — strong signal
                    elif sabre.trend == -1 and not _price_above_ma and not macd_4h.above_zero:
                        htf_boost = -0.08  # All 3 votes bearish — strong signal
                    elif sabre.trend == 1 and macd_4h.above_zero:
                        htf_boost = 0.03  # 2/3 bull (price below MA) — weak
                    elif sabre.trend == -1 and not macd_4h.above_zero:
                        htf_boost = -0.03  # 2/3 bear (price above MA) — weak
                    # else: mixed votes → no directional boost (htf_boost stays 0.0)
                    est_prob_up += htf_boost

                    # 4H/1H HISTOGRAM GATE (matches backtest engine)
                    # BTC 15m: without gate 50.7% WR; with gate 53.4% WR → improved further with anti-LTF.
                    # Primary: 4H histogram must be building in trade direction.
                    # Fallback: if 4H is decelerating but 1H is building, allow entry
                    # (catches local momentum recovery within larger trend structure).
                    macd_1h = ta.macd_1h
                    if allowed_side == "LONG" and not macd_4h.histogram_rising:
                        if not macd_1h.histogram_rising:
                            _bump_skip("hist_gate_15m_long_reject")
                            logger.info(
                                f"  BTC [15m] skip '{market.question[:40]}' — "
                                f"4H falling, 1H also falling — no momentum building for LONG"
                            )
                            continue
                        logger.info(
                            f"  BTC [15m] 1H gate pass '{market.question[:40]}' — "
                            f"4H falling but 1H rising — local momentum recovery"
                        )
                    if allowed_side == "SHORT" and macd_4h.histogram_rising:
                        if macd_1h.histogram_rising:
                            _bump_skip("hist_gate_15m_short_reject")
                            logger.info(
                                f"  BTC [15m] skip '{market.question[:40]}' — "
                                f"4H rising, 1H also rising — no momentum building for SHORT"
                            )
                            continue
                        logger.info(
                            f"  BTC [15m] 1H gate pass '{market.question[:40]}' — "
                            f"4H rising but 1H falling — local momentum recovery SHORT"
                        )

                    # LTF confirmation adds conviction
                    ltf_adj = ltf_strength * 0.20
                    est_prob_up += ltf_adj if allowed_side == "LONG" else -ltf_adj

                    # Timing/momentum adds more
                    if allowed_side == "LONG":
                        est_prob_up += timing_bonus
                    else:
                        est_prob_up -= timing_bonus

                    # RSI adjustments — expanded from 80/20 to 65/35 range.
                    # Live data: RSI 65-70 during SHORT had zero weight but is a real overbought signal.
                    # Very extreme (>80/<20) gets full -0.03/+0.03; mid-extreme (65-80/20-35) gets -0.02/+0.02
                    if ta.rsi_14 > 80:
                        est_prob_up -= 0.03  # Very overbought — strong support for SHORT
                    elif ta.rsi_14 > 65:
                        est_prob_up -= 0.02  # Overbought zone — modest support for SHORT
                    elif ta.rsi_14 < 20:
                        est_prob_up += 0.03  # Very oversold — strong support for LONG
                    elif ta.rsi_14 < 35:
                        est_prob_up += 0.02  # Oversold zone — modest support for LONG

                    # Sabre tension — lowered threshold from 4.0 to 2.0 to match threshold-market path.
                    # 72.9% of signals have tension_abs > 2.0; the 4.0 threshold almost never fired.
                    # Price stretched >2 ATR from the MA is meaningful mean-reversion risk.
                    if ta.trend_sabre.tension_abs > 2.0:
                        if allowed_side == "LONG":
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
                            est_prob_up -= 0.01  # At POC, likely to chop
                        reason_parts.append(f"VP_POC=${vp.poc_price:,.0f}")

                    est_prob_up = max(0.10, min(0.90, est_prob_up))

                    if action == "BUY_YES":
                        edge = est_prob_up - yes_price
                    else:
                        edge = (1.0 - est_prob_up) - (1.0 - yes_price)

                    confidence = min(0.85, 0.50 + ltf_strength * 0.20 + abs(timing_bonus))

                    reason_parts.extend([
                        "UPDOWN_15m",
                        f"btc=${btc_price:,.0f}",
                        f"est_up={est_prob_up:.3f}",
                        f"mkt_yes={yes_price:.3f}",
                        f"4H_MACD={'+'if macd_4h.above_zero else '-'}{abs(macd_4h.histogram):.0f}",
                        f"15m_MACD={'+' if macd_15m.macd_line > macd_15m.signal_line else '-'}{abs(macd_15m.histogram):.1f}",
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
                        action = "SELL_YES"
                else:
                    if direction == "UP":
                        action = "SELL_YES"
                    else:
                        action = "BUY_YES"

                if threshold:
                    distance_pct = abs(btc_price - threshold) / threshold
                    estimated_prob = self._estimate_probability(
                        btc_price, threshold, direction, ta,
                        days_to_resolution, ltf_strength, timing_bonus,
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
                    if edge < self.min_edge and edge > 0.03:
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
                        ai_analysis = await self.ai_agent.analyze_market(
                            market_question=market.question,
                            market_description=ai_context,
                            current_yes_price=yes_price,
                            market_id=market.id,
                            strategy_hint="bitcoin",
                        )
                        ai_calls += 1
                        ai_used = True
                        if not ai_analysis:
                            logger.critical(
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
                        ai_edge = (
                            ai_analysis.estimated_probability - yes_price
                            if action == "BUY_YES"
                            else yes_price - ai_analysis.estimated_probability
                        )
                        edge = max(edge, ai_edge)
                        confidence = ai_analysis.confidence_score
                        reason_parts.append("ai_confirm")

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
                    ai_analysis = await self.ai_agent.analyze_market(
                        market_question=market.question,
                        market_description=ai_context,
                        current_yes_price=yes_price,
                        market_id=market.id,
                        strategy_hint="bitcoin",
                    )
                    ai_calls += 1
                    ai_used = True
                    if not ai_analysis:
                        logger.critical(
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
                    edge = abs(ai_analysis.estimated_probability - yes_price) - 0.02
                    confidence = ai_analysis.confidence_score
                    reason_parts.append(f"ai_only btc=${btc_price:,.0f}")

            # ── Final filters ──
            effective_min_edge = self.min_edge_5m if is_5m else self.min_edge
            # NEUTRAL HTF: no confirmed bias — demand stronger edge for updown leans.
            # Applies to both 5m and 15m: the 5m path has zero backtest coverage under NEUTRAL
            # (all 1735 trades in Apr-2026 BTC 5m backtest were BULLISH/BEARISH only).
            if htf_bias == "NEUTRAL" and is_updown:
                effective_min_edge = max(effective_min_edge, 0.09)

            # ── AI-hold soft veto ────────────────────────────────────────────
            # If AI said HOLD on this market within the last ai_hold_veto_ttl_sec,
            # the 5m quant path must clear the higher min_edge_5m_ai_override
            # threshold. Closes the gap where AI is skeptical but quant fires anyway.
            _hold_ts = self._ai_hold_cache.get(market.id, 0)
            _hold_age = time.time() - _hold_ts
            if _hold_age < self.ai_hold_veto_ttl_sec:
                if edge < self.min_edge_5m_ai_override:
                    _bump_skip("ai_hold_veto_active")
                    logger.info(
                        f"  BTC ai-hold veto '{market.question[:45]}' "
                        f"— edge={edge:.4f} < override={self.min_edge_5m_ai_override:.4f} "
                        f"(AI said HOLD {_hold_age:.0f}s ago)"
                    )
                    continue

            # Updown AI assist: when quant edge is close but below threshold, let AI break ties.
            # This keeps strict HTF gating while allowing context-aware confirmation on borderline setups.
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
                _window = "5m" if is_5m else "15m"
                ai_context = (
                    f"{market.description}\n\n"
                    f"=== BTC UPDOWN CONTEXT ({_window}) ===\n"
                    f"BTC Price: ${btc_price:,.2f}\n"
                    f"Market YES Price: {yes_price:.3f}\n"
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
                ai_analysis = await self.ai_agent.analyze_market(
                    market_question=market.question,
                    market_description=ai_context,
                    current_yes_price=yes_price,
                    market_id=market.id,
                    strategy_hint="bitcoin",
                )
                ai_calls += 1
                ai_used = True

                if not ai_analysis:
                    logger.critical(
                        "BTC: AI returned None after provider call for market %s — "
                        "LLM chain failed or response invalid (marginal updown path)",
                        market.id,
                    )
                    continue
                if ai_analysis.recommendation == "HOLD":
                    ai_holds += 1
                    _bump_skip("ai_hold_marginal_updown")
                    # Cache this HOLD — blocks the 5m quant path for ai_hold_veto_ttl_sec
                    self._ai_hold_cache[market.id] = time.time()
                    logger.info(
                        f"  BTC AI skip '{market.question[:45]}' — HOLD on marginal edge={edge:.4f} "
                        f"(veto cached {self.ai_hold_veto_ttl_sec}s)"
                    )
                    continue
                # Log AI reasoning for every decision so we can audit what the model is thinking
                logger.info(
                    f"  BTC AI [{ai_analysis.recommendation} conf={ai_analysis.confidence_score:.2f} "
                    f"p={ai_analysis.estimated_probability:.3f}] '{market.question[:45]}' "
                    f"| {ai_analysis.reasoning[:120]}"
                )
                if not ai_recommendation_supports_action(ai_analysis.recommendation, action):
                    ai_vetos += 1
                    _bump_skip("ai_veto_marginal_updown")
                    logger.info(
                        f"  BTC AI veto '{market.question[:45]}' — rec={ai_analysis.recommendation} "
                        f"conflicts with action={action}"
                    )
                    continue
                if ai_analysis.confidence_score < self.ai_confidence_threshold:
                    _bump_skip("ai_low_confidence_marginal_updown")
                    logger.info(
                        f"  BTC AI skip '{market.question[:45]}' — confidence "
                        f"{ai_analysis.confidence_score:.2f} < {self.ai_confidence_threshold:.2f}"
                    )
                    continue

                ai_prob_yes = float(ai_analysis.estimated_probability)
                ai_edge = (
                    ai_prob_yes - yes_price
                    if action == "BUY_YES"
                    else yes_price - ai_prob_yes
                )
                if ai_edge <= 0:
                    _bump_skip("ai_nonpositive_edge_marginal_updown")
                    logger.info(
                        f"  BTC AI skip '{market.question[:45]}' — non-positive ai_edge={ai_edge:.4f}"
                    )
                    continue

                edge = max(edge, ai_edge)
                confidence = max(confidence, ai_analysis.confidence_score)
                ai_assists += 1
                reason_parts.append("ai_updown_confirm")
            elif (
                is_updown
                and edge < effective_min_edge
                and edge >= self.config.get("ai_updown_marginal_min_edge", 0.03)
                and self.config.get("use_ai", True)
                and self.config.get("use_ai_updown", True)
                and not _ai_window_open
            ):
                logger.debug(
                    f"  BTC AI window closed for marginal updown '{market.question[:40]}...' "
                    f"({_mins_left:.1f}m left)"
                )

            try:
                _sample("est_prob_up", est_prob_up)
            except NameError:
                pass
            _sample("edge", edge)
            if edge < effective_min_edge:
                _bump_skip("edge_below_min")
                _mkt_type = "updown_5m" if is_5m else (
                    "updown_15m_neutral" if (htf_bias == "NEUTRAL" and is_updown) else
                    ("updown_15m" if is_updown else "threshold")
                )
                logger.info(
                    f"  BTC skip '{market.question[:45]}' {action} "
                    f"edge={edge:.4f} < min={effective_min_edge} | {_mkt_type}"
                )
                continue

            # ── Edge cap for updown markets ──
            # Live data: edge >0.12 on 15m/5m updown = 27% WR. The probability model
            # inflates edge when BTC is far from the 15m threshold — a large computed
            # edge means BTC has ALREADY moved, not that it WILL move. Cap it.
            if is_updown:
                _max_edge_updown = self.config.get("max_edge_updown", 0.12)
                if edge > _max_edge_updown:
                    _bump_skip("edge_above_cap")
                    logger.info(
                        f"  BTC skip '{market.question[:45]}' {action} "
                        f"edge={edge:.4f} > max={_max_edge_updown} updown cap (inflated signal)"
                    )
                    continue

                # Updown-specific entry price band — symmetric around 0.50.
                # self.entry_price_min/max are for directional threshold markets (0.10-0.90).
                # Updown markets need a tighter band to avoid betting against strong consensus.
                #   BUY_YES at yes_price < 0.46: market is already bearish → no momentum edge
                #   SELL_YES at yes_price > 0.54: market is already bullish → no momentum edge
                _up_min = self.config.get("entry_price_min_updown", 0.46)
                _up_max = self.config.get("entry_price_max_updown", 0.54)
                if yes_price < _up_min or yes_price > _up_max:
                    _bump_skip("entry_price_out_of_range_updown")
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
                self._signal_strategy_name, bankroll, edge
            )
            if raw_size <= 0:
                _bump_skip("kelly_nonpositive")
                continue

            # Apply dynamic exposure scaling
            size = self.exposure_manager.scale_size(raw_size)
            if size <= 0:
                _bump_skip("scaled_size_nonpositive")
                continue
            reason_parts.append(f"exp={exp_tier.value}(x{exp_multiplier:.1f})")

            order_price = (yes_price - 0.01) if action == "BUY_YES" else (yes_price + 0.01)
            order_price = max(0.01, min(0.99, order_price))

            reason = " | ".join(reason_parts)

            # Reconstruct est_prob from edge + yes_price for journal logging.
            # BUY_YES:  edge = est_prob - yes_price  → est_prob = edge + yes_price
            # SELL_YES: edge = yes_price - est_prob  → est_prob = yes_price - edge
            _signal_est_prob = round(
                (edge + yes_price) if action == "BUY_YES" else (yes_price - edge),
                4,
            )

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
                window_size="5m" if is_5m else "15m",
                hour_utc=datetime.now(timezone.utc).hour,
                est_prob=_signal_est_prob,
                rsi=round(ta.rsi_14, 1),
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
            "htf_bias": htf_bias,
            "allowed_side": allowed_side,
            "ltf_strength": round(float(ltf_strength), 4),
            "ai_calls": ai_calls,
            "ai_assists": ai_assists,
            "ai_vetos": ai_vetos,
            "ai_holds": ai_holds,
            "top_skip_reasons": {k: v for k, v in top_skip_pairs},
            "gate_distributions": gate_distributions,
        }

        return signals
