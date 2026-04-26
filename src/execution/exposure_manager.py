"""
Dynamic Exposure Manager

Scales trade sizing and frequency based on:
1. Market conditions (volatility + volume)
2. Recent performance (loss streaks)
3. Trend clarity (from technical analysis)

Exposure Tiers:
  FULL     → max sizing (config `exposure.full_size`, default ~$15) — high vol + clear trend
  MODERATE → reduced sizing (`moderate_size`, default ~$13) — moderate conditions
  MINIMAL  → reduced sizing (`minimal_size`, default ~$10) — sideways/low volume
  PAUSED   → kill switch active — 3+ consecutive losses or flat conditions

Kill Switch:
  - 3 consecutive losses → pause for N cycles (test) or until manual restart (live)
  - Flat/sideways market with no volume → pause until conditions improve
  - Two resume modes for live: auto-resume when conditions return, or manual only

The manager is queried BEFORE every trade to get the current size multiplier.
"""
import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger(__name__)


class ExposureTier(Enum):
    FULL = "FULL"
    MODERATE = "MODERATE"
    MINIMAL = "MINIMAL"
    PAUSED = "PAUSED"


class PauseResumeMode(Enum):
    AUTO = "auto"       # Resume automatically when conditions improve
    MANUAL = "manual"   # Wait for user to restart the bot


@dataclass
class TradeResult:
    """Record of a completed trade for streak tracking."""
    timestamp: datetime
    pnl: float
    strategy: str
    market_id: str


@dataclass
class MarketConditions:
    """Snapshot of current market conditions for exposure decisions."""
    volatility: float = 0.0      # ATR as % of price (e.g., 0.02 = 2%)
    volume_ratio: float = 1.0    # Current volume vs average (>1 = above avg)
    trend_strength: float = 0.0  # 0-1, from technical analysis
    trend_direction: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    weekend_penalty: float = 1.0  # 1.0 = normal, 0.0 = full penalty (weekend/low-liquidity)


class ExposureManager:
    """Dynamically scales trade exposure based on conditions and performance."""

    def __init__(
        self,
        config: Dict[str, Any],
        is_paper: bool = True,
        notifications: Any = None,
        lane_name: str = "UNKNOWN",
    ):
        exposure_config = config.get('exposure', {})

        self.is_paper = is_paper
        self._notifications = notifications
        self.lane_name = lane_name

        self.is_paper = is_paper

        # --- Sizing per tier (USD caps after multiplier + optional floor) ---
        self.tier_sizing = {
            ExposureTier.FULL: exposure_config.get('full_size', 15.0),
            ExposureTier.MODERATE: exposure_config.get('moderate_size', 13.0),
            ExposureTier.MINIMAL: exposure_config.get('minimal_size', 10.0),
            ExposureTier.PAUSED: 0.0,
        }
        # Floor after tier multiplier so MODERATE/MINIMAL do not shrink Kelly to $1–3 trades.
        self.min_trade_usd = float(exposure_config.get('min_trade_usd', 0.0) or 0.0)

        # --- Tier multipliers (applied to Kelly/position sizer output) ---
        self.tier_multipliers = {
            ExposureTier.FULL: 1.0,
            ExposureTier.MODERATE: 0.6,
            ExposureTier.MINIMAL: 0.2,
            ExposureTier.PAUSED: 0.0,
        }

        # --- Kill switch config ---
        self.loss_kill_switch_enabled = exposure_config.get('loss_kill_switch_enabled', True)
        self.max_consecutive_losses = exposure_config.get('max_consecutive_losses', 3)
        self.pause_cycles = exposure_config.get('pause_cycles', 2)  # Test mode: pause N cycles
        self.resume_mode = PauseResumeMode(
            exposure_config.get('live_resume_mode', 'auto')
        )

        # --- Volatility thresholds ---
        # ATR as % of price
        self.high_vol_threshold = exposure_config.get('high_vol_pct', 0.015)   # 1.5%
        self.low_vol_threshold = exposure_config.get('low_vol_pct', 0.005)     # 0.5%
        # Volume ratio (current vs 20-bar avg)
        self.high_volume_ratio = exposure_config.get('high_volume_ratio', 1.3)
        self.low_volume_ratio = exposure_config.get('low_volume_ratio', 0.7)

        # --- State ---
        self._recent_trades: List[TradeResult] = []
        self._consecutive_losses: int = 0
        self._paused: bool = False
        self._pause_reason: str = ""
        self._pause_start: Optional[datetime] = None
        self._cycles_since_pause: int = 0
        self._manual_pause: bool = False  # User explicitly paused
        self._current_tier: ExposureTier = ExposureTier.FULL
        self._last_conditions: Optional[MarketConditions] = None
        self._on_pause_ai_callback: Optional[Callable] = None

    def reload_from_config(self, exposure_config: Dict[str, Any]) -> None:
        """Refresh sizing, kill-switch, and condition thresholds from YAML/dashboard.

        Does not reset streaks, auto-pause state, or manual pause — only parameters.
        """
        if not exposure_config:
            return
        self.tier_sizing = {
            ExposureTier.FULL: exposure_config.get("full_size", 15.0),
            ExposureTier.MODERATE: exposure_config.get("moderate_size", 13.0),
            ExposureTier.MINIMAL: exposure_config.get("minimal_size", 10.0),
            ExposureTier.PAUSED: 0.0,
        }
        self.min_trade_usd = float(exposure_config.get("min_trade_usd", 0.0) or 0.0)
        self.loss_kill_switch_enabled = exposure_config.get(
            "loss_kill_switch_enabled", True
        )
        self.max_consecutive_losses = exposure_config.get("max_consecutive_losses", 3)
        self.pause_cycles = exposure_config.get("pause_cycles", 2)
        self.resume_mode = PauseResumeMode(
            exposure_config.get("live_resume_mode", "auto")
        )
        self.high_vol_threshold = exposure_config.get("high_vol_pct", 0.015)
        self.low_vol_threshold = exposure_config.get("low_vol_pct", 0.005)
        self.high_volume_ratio = exposure_config.get("high_volume_ratio", 1.3)
        self.low_volume_ratio = exposure_config.get("low_volume_ratio", 0.7)

    # ──────────────────────────────────────────────────────────────
    # Core: Get current exposure tier and size
    # ──────────────────────────────────────────────────────────────

    def get_exposure(self, conditions: MarketConditions) -> tuple:
        """Get current exposure tier, multiplier, and max trade size.

        Called at the START of each trading cycle before any trades.

        Returns: (tier: ExposureTier, multiplier: float, max_size: float, reason: str)
        """
        self._last_conditions = conditions

        # --- Check kill switch first ---
        if self._manual_pause:
            return ExposureTier.PAUSED, 0.0, 0.0, "Manual pause — restart bot to resume"

        if self._paused:
            if self.is_paper or self.resume_mode == PauseResumeMode.AUTO:
                # Auto-resume: check if conditions improved
                self._cycles_since_pause += 1
                if self._cycles_since_pause >= self.pause_cycles:
                    # Check if conditions have improved
                    if self._should_resume(conditions):
                        self._unpause("Conditions improved after pause")
                    else:
                        return (
                            ExposureTier.PAUSED, 0.0, 0.0,
                            f"Paused ({self._pause_reason}) — waiting for conditions "
                            f"[cycle {self._cycles_since_pause}/{self.pause_cycles}+]"
                        )
                else:
                    return (
                        ExposureTier.PAUSED, 0.0, 0.0,
                        f"Paused ({self._pause_reason}) — cooling off "
                        f"[cycle {self._cycles_since_pause}/{self.pause_cycles}]"
                    )
            else:
                # Manual resume mode (live)
                return (
                    ExposureTier.PAUSED, 0.0, 0.0,
                    f"Paused ({self._pause_reason}) — restart bot to resume"
                )

        # --- Determine tier from conditions ---
        tier = self._calculate_tier(conditions)
        self._current_tier = tier
        multiplier = self.tier_multipliers[tier]
        max_size = self.tier_sizing[tier]

        reason = self._build_reason(tier, conditions)

        # --- T1-5: Weekend / low-liquidity size reduction ---
        # Reduces max position size during weekend or thin market conditions
        # where manipulation risk (e.g., a4385-style CEX pump) is elevated.
        effective_weekend_penalty = getattr(conditions, 'weekend_penalty', 1.0)
        if effective_weekend_penalty < 1.0:
            max_size *= effective_weekend_penalty
            reason += f" weekend_penalty={effective_weekend_penalty:.1f}"
            logger.info(
                f"Exposure: weekend/low-liquidity penalty applied "
                f"({effective_weekend_penalty:.1f}x → max ${max_size:.2f})"
            )

        logger.info(
            f"Exposure: {tier.value} (x{multiplier:.1f}, max ${max_size:.2f}) | "
            f"vol={conditions.volatility:.3f} vol_ratio={conditions.volume_ratio:.2f} "
            f"trend={conditions.trend_direction}({conditions.trend_strength:.2f}) | "
            f"streak={self._consecutive_losses} losses | {reason}"
        )

        return tier, multiplier, max_size, reason

    def _calculate_tier(self, c: MarketConditions) -> ExposureTier:
        """Determine exposure tier from market conditions."""
        score = 0

        # Volatility scoring
        if c.volatility >= self.high_vol_threshold:
            score += 2  # High vol = opportunity
        elif c.volatility >= self.low_vol_threshold:
            score += 1  # Moderate
        # else: low vol = 0

        # Volume scoring
        if c.volume_ratio >= self.high_volume_ratio:
            score += 2  # High participation
        elif c.volume_ratio >= self.low_volume_ratio:
            score += 1  # Normal
        # else: low volume = 0

        # Trend clarity
        if c.trend_strength >= 0.6:
            score += 2  # Clear trend
        elif c.trend_strength >= 0.3:
            score += 1  # Some trend
        # else: no trend = 0

        # Loss streak penalty
        if self._consecutive_losses >= 2:
            score -= 2  # Approaching kill switch, reduce

        # Tier assignment
        if score >= 5:
            return ExposureTier.FULL
        elif score >= 3:
            return ExposureTier.MODERATE
        elif score >= 1:
            return ExposureTier.MINIMAL
        else:
            return ExposureTier.MINIMAL  # Never auto-pause from conditions alone

    def _should_resume(self, conditions: MarketConditions) -> bool:
        """Check if conditions are good enough to resume after pause."""
        # Need at least moderate conditions to resume
        return (
            conditions.volume_ratio >= self.low_volume_ratio
            and conditions.trend_strength >= 0.3
            and conditions.volatility >= self.low_vol_threshold
        )

    def _build_reason(self, tier: ExposureTier, c: MarketConditions) -> str:
        parts = []
        if c.volatility >= self.high_vol_threshold:
            parts.append("high_vol")
        elif c.volatility < self.low_vol_threshold:
            parts.append("low_vol")
        if c.volume_ratio >= self.high_volume_ratio:
            parts.append("high_participation")
        elif c.volume_ratio < self.low_volume_ratio:
            parts.append("low_participation")
        if c.trend_strength >= 0.6:
            parts.append(f"clear_{c.trend_direction.lower()}")
        elif c.trend_strength < 0.3:
            parts.append("no_trend")
        if self._consecutive_losses > 0:
            parts.append(f"{self._consecutive_losses}_losses")
        return " ".join(parts) if parts else "normal"

    # ──────────────────────────────────────────────────────────────
    # Trade Result Tracking
    # ──────────────────────────────────────────────────────────────

    def record_trade(self, pnl: float, strategy: str = "", market_id: str = ""):
        """Record a completed trade result. Triggers kill switch if needed."""
        result = TradeResult(
            timestamp=datetime.now(),
            pnl=pnl,
            strategy=strategy,
            market_id=market_id,
        )
        self._recent_trades.append(result)

        # Keep last 50 trades
        if len(self._recent_trades) > 50:
            self._recent_trades = self._recent_trades[-50:]

        # Track consecutive losses
        if pnl < 0:
            self._consecutive_losses += 1
            logger.info(f"Exposure: Loss recorded ({pnl:+.2f}), streak={self._consecutive_losses}")

            if self.loss_kill_switch_enabled and self._consecutive_losses >= self.max_consecutive_losses:
                self._trigger_pause(
                    f"{self._consecutive_losses} consecutive losses"
                )
            elif not self.loss_kill_switch_enabled and self._consecutive_losses >= self.max_consecutive_losses:
                logger.info(f"Exposure: Kill switch disabled (testing) — would pause at {self._consecutive_losses} losses")
        else:
            if self._consecutive_losses > 0:
                logger.info(f"Exposure: Win recorded ({pnl:+.2f}), resetting loss streak")
            self._consecutive_losses = 0

    def _trigger_pause(self, reason: str):
        """Activate the kill switch."""
        self._paused = True
        self._pause_reason = reason
        self._pause_start = datetime.now()
        self._cycles_since_pause = 0

        if self.is_paper:
            logger.warning(
                f"KILL SWITCH: {reason} — pausing for {self.pause_cycles} cycles"
            )
        else:
            mode_desc = "auto-resume" if self.resume_mode == PauseResumeMode.AUTO else "manual restart"
            logger.warning(
                f"KILL SWITCH: {reason} — paused until {mode_desc}"
            )

        if self._notifications is not None:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(
                        self._notifications.notify_kill_lane(
                            self.lane_name, reason, self._consecutive_losses
                        )
                    )
            except Exception:
                pass

        if self._on_pause_ai_callback is not None:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(
                        self._on_pause_ai_callback(reason, self._consecutive_losses)
                    )
            except Exception:
                pass

    def _unpause(self, reason: str):
        """Deactivate the kill switch."""
        self._paused = False
        self._pause_reason = ""
        self._cycles_since_pause = 0
        self._consecutive_losses = 0  # Reset on unpause
        logger.info(f"EXPOSURE RESUMED: {reason}")

    # ──────────────────────────────────────────────────────────────
    # Manual Controls
    # ──────────────────────────────────────────────────────────────

    def manual_pause(self):
        """User-triggered pause."""
        self._manual_pause = True
        logger.warning("Exposure: MANUAL PAUSE activated")

    def manual_resume(self):
        """User-triggered resume."""
        self._manual_pause = False
        self._paused = False
        self._pause_reason = ""
        self._cycles_since_pause = 0
        self._consecutive_losses = 0
        logger.info("Exposure: MANUAL RESUME — all clear")

    def reset_for_new_paper_session(self):
        """Clear streaks, pauses, and recent trade memory after a dashboard paper reset."""
        self._recent_trades.clear()
        self._consecutive_losses = 0
        self._paused = False
        self._pause_reason = ""
        self._pause_start = None
        self._cycles_since_pause = 0
        self._manual_pause = False
        self._current_tier = ExposureTier.FULL
        self._last_conditions = None

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────

    @property
    def consecutive_losses(self) -> int:
        """Current consecutive loss streak (read-only)."""
        return self._consecutive_losses

    def scale_size(self, raw_size: float) -> float:
        """Apply exposure multiplier to a raw position size.

        Call this after Kelly/position sizer gives a raw size.
        Enforces ``min_trade_usd`` after multiply so tier multipliers do not produce $1–3 trades.
        """
        multiplier = self.tier_multipliers.get(self._current_tier, 1.0)
        max_size = self.tier_sizing.get(self._current_tier, raw_size)
        if max_size <= 0:
            return 0.0
        scaled = raw_size * multiplier
        if self.min_trade_usd > 0:
            scaled = max(scaled, self.min_trade_usd)
        return min(scaled, max_size)

    def get_status(self) -> Dict[str, Any]:
        """Get exposure status for dashboard/logging."""
        return {
            'tier': self._current_tier.value,
            'multiplier': self.tier_multipliers.get(self._current_tier, 0),
            'max_size': self.tier_sizing.get(self._current_tier, 0),
            'paused': self._paused or self._manual_pause,
            'pause_reason': self._pause_reason if self._paused else ('manual' if self._manual_pause else ''),
            'consecutive_losses': self._consecutive_losses,
            'cycles_since_pause': self._cycles_since_pause,
            'recent_trades': len(self._recent_trades),
            'recent_pnl': sum(t.pnl for t in self._recent_trades[-10:]),
            'conditions': {
                'volatility': self._last_conditions.volatility if self._last_conditions else 0,
                'volume_ratio': self._last_conditions.volume_ratio if self._last_conditions else 0,
                'trend_strength': self._last_conditions.trend_strength if self._last_conditions else 0,
            } if self._last_conditions else {},
        }

    @staticmethod
    def conditions_from_ta(ta) -> MarketConditions:
        """Build MarketConditions from a TechnicalAnalysis object."""
        # Volatility: ATR / price as percentage
        volatility = 0.0
        if ta.trend_sabre.atr > 0 and ta.current_price > 0:
            volatility = ta.trend_sabre.atr / ta.current_price

        # Volume ratio: compare recent volume to average
        # (This would ideally use actual volume data, but we can estimate
        # from the candle momentum strength as a proxy)
        volume_ratio = 1.0
        mom = ta.candle_momentum
        if mom.momentum_strength > 0.6:
            volume_ratio = 1.5  # Strong momentum = high participation
        elif mom.momentum_strength > 0.3:
            volume_ratio = 1.1
        elif mom.momentum_strength < 0.1:
            volume_ratio = 0.6  # No momentum = low participation

        return MarketConditions(
            volatility=volatility,
            volume_ratio=volume_ratio,
            trend_strength=ta.trend_strength,
            trend_direction=ta.trend_direction,
            weekend_penalty=_get_weekend_penalty(),
        )


def _get_weekend_penalty() -> float:
    """Return weekend penalty multiplier (1.0=normal, 0.0=full penalty).

    Reduces position size during weekend / low-liquidity periods when
    HYPE-style manipulation (a4385 CEX pump) is most likely to occur.
    """
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()  # 0=Mon … 5=Sat, 6=Sun
    utc_hour = now_utc.hour

    if weekday >= 5:
        return 0.50

    if weekday == 4 and utc_hour >= 20:
        return 0.70

    return 1.0
