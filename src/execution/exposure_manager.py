"""
Dynamic Exposure Manager

Scales trade sizing and frequency based on:
1. Market conditions (volatility + volume)
2. Recent performance (loss streaks)
3. Trend clarity (from technical analysis)

Exposure Tiers:
  FULL     → preserves Kelly target sizing, capped by config `exposure.full_size`
  MODERATE → reduced sizing via tier multiplier, capped by `moderate_size`
  MINIMAL  → heavily reduced sizing via tier multiplier, capped by `minimal_size`
  PAUSED   → lane pause active — 3+ consecutive losses or flat conditions

Loss-Streak Lane Pause:
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
    window_size: str = ""


@dataclass
class MarketConditions:
    """Snapshot of current market conditions for exposure decisions."""
    volatility: float = 0.0      # ATR as % of price (e.g., 0.02 = 2%)
    volume_ratio: float = 1.0    # Current volume vs average (>1 = above avg)
    trend_strength: float = 0.0  # 0-1, from technical analysis
    trend_direction: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    weekend_penalty: float = 1.0  # 1.0 = normal, 0.0 = full penalty (weekend/low-liquidity)
    green_window: Optional[bool] = None


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
        # Legacy base floor in USD. Used only when explicit per-tier floors are absent.
        self.min_trade_usd = float(exposure_config.get('min_trade_usd', 0.0) or 0.0)

        # --- Tier multipliers (applied to Kelly/position sizer output) ---
        self.tier_multipliers = {
            ExposureTier.FULL: 1.0,
            ExposureTier.MODERATE: 0.6,
            ExposureTier.MINIMAL: 0.2,
            ExposureTier.PAUSED: 0.0,
        }
        self.tier_floors = self._resolve_tier_floors(exposure_config)

        # --- Loss-streak lane pause config ---
        self.loss_kill_switch_enabled = exposure_config.get('loss_kill_switch_enabled', True)
        # The loss-streak lane pause is a LIVE-trading safety. In paper it is inert
        # by default (paper sessions are for calibration/data — they must keep
        # trading the losing lanes, not pause them). Opt in via this flag (tests do).
        self.loss_kill_apply_in_paper = bool(
            exposure_config.get('loss_kill_apply_in_paper', False)
        )
        self.max_consecutive_losses = exposure_config.get('max_consecutive_losses', 3)
        self.pause_cycles = exposure_config.get('pause_cycles', 2)  # Test mode: pause N cycles
        self.max_pause_cycles = int(
            exposure_config.get("max_pause_cycles", max(self.pause_cycles * 2, self.pause_cycles + 1))
        )
        self.loss_pause_recovery_multiple = float(
            exposure_config.get("loss_pause_recovery_multiple", 0.0) or 0.0
        )
        self.require_green_window_for_resume = bool(
            exposure_config.get("require_green_window_for_resume", False)
        )
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
        self._last_loss_kill_trigger: Optional[Dict[str, Any]] = None
        self._streak_loss_abs_total: float = 0.0
        self._portfolio_pnl: float = 0.0
        self._pause_recovery_anchor_pnl: float = 0.0
        self._pause_recovery_target: float = 0.0
        self._latest_green_window: Optional[bool] = None

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
        self.tier_floors = self._resolve_tier_floors(exposure_config)
        # Preserve the explicitly-configured value on partial reloads. A sizing-only
        # (or any key-missing) update must NOT silently re-enable the loss-streak
        # kill switch — that was flipping config `false` back to `true` at runtime
        # and lighting the dashboard LOSS KILL badge. Only override when the key is
        # actually present in the incoming dict.
        if "loss_kill_switch_enabled" in exposure_config:
            self.loss_kill_switch_enabled = bool(
                exposure_config["loss_kill_switch_enabled"]
            )
        if "loss_kill_apply_in_paper" in exposure_config:
            self.loss_kill_apply_in_paper = bool(
                exposure_config["loss_kill_apply_in_paper"]
            )
        self.max_consecutive_losses = exposure_config.get("max_consecutive_losses", 3)
        self.pause_cycles = exposure_config.get("pause_cycles", 2)
        self.max_pause_cycles = int(
            exposure_config.get("max_pause_cycles", max(self.pause_cycles * 2, self.pause_cycles + 1))
        )
        self.loss_pause_recovery_multiple = float(
            exposure_config.get("loss_pause_recovery_multiple", 0.0) or 0.0
        )
        self.require_green_window_for_resume = bool(
            exposure_config.get("require_green_window_for_resume", False)
        )
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

        # --- Check lane pause first ---
        if self._manual_pause:
            return ExposureTier.PAUSED, 0.0, 0.0, "Manual pause — restart bot to resume"

        if self._paused:
            if self.is_paper or self.resume_mode == PauseResumeMode.AUTO:
                # Auto-resume: check if conditions improved
                self._cycles_since_pause += 1
                if self._cycles_since_pause >= self.pause_cycles:
                    if (
                        self._cycles_since_pause >= self.max_pause_cycles
                        and self._pause_recovery_target <= 0
                    ):
                        logger.warning(
                            "OPS_JSON exposure_auto_resume lane=%s reason=%r cycles=%s max_pause_cycles=%s",
                            self.lane_name,
                            self._pause_reason,
                            self._cycles_since_pause,
                            self.max_pause_cycles,
                        )
                        self._unpause("Max pause cycles reached")
                    # Check if conditions have improved
                    elif self._should_resume(conditions):
                        self._unpause("Conditions improved after pause")
                    else:
                        waiting_for = self._resume_waiting_for(conditions)
                        logger.info(
                            "OPS_JSON exposure_paused lane=%s reason=%r cycles=%s pause_cycles=%s max_pause_cycles=%s waiting_for=%s",
                            self.lane_name,
                            self._pause_reason,
                            self._cycles_since_pause,
                            self.pause_cycles,
                            self.max_pause_cycles,
                            waiting_for,
                        )
                        return (
                            ExposureTier.PAUSED, 0.0, 0.0,
                            f"Paused ({self._pause_reason}) — waiting for conditions "
                            f"[cycle {self._cycles_since_pause}/{self.max_pause_cycles}; waiting_for={waiting_for}]"
                        )
                else:
                    return (
                        ExposureTier.PAUSED, 0.0, 0.0,
                        f"Paused ({self._pause_reason}) — cooling off "
                        f"[cycle {self._cycles_since_pause}/{self.pause_cycles}]"
                    )
            else:
                # Manual resume mode
                return (
                    ExposureTier.PAUSED, 0.0, 0.0,
                    f"Paused ({self._pause_reason}) — manual resume required"
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
            score -= 2  # Approaching lane pause threshold, reduce

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
        base_ok = (
            conditions.volume_ratio >= self.low_volume_ratio
            and conditions.trend_strength >= 0.3
            and conditions.volatility >= self.low_vol_threshold
        )
        if not base_ok:
            return False
        if self.require_green_window_for_resume and not self._is_green_window(conditions):
            return False
        if self._pause_recovery_target > 0:
            recovered = max(0.0, self._portfolio_pnl - self._pause_recovery_anchor_pnl)
            if recovered < self._pause_recovery_target:
                return False
        return True

    def _resume_waiting_for(self, conditions: MarketConditions) -> str:
        missing = []
        if conditions.volume_ratio < self.low_volume_ratio:
            missing.append("volume")
        if conditions.trend_strength < 0.3:
            missing.append("trend_strength")
        if conditions.volatility < self.low_vol_threshold:
            missing.append("volatility")
        if self.require_green_window_for_resume and not self._is_green_window(conditions):
            missing.append("green_window")
        if self._pause_recovery_target > 0:
            recovered = max(0.0, self._portfolio_pnl - self._pause_recovery_anchor_pnl)
            if recovered < self._pause_recovery_target:
                missing.append(
                    f"recovery_pnl({recovered:.2f}/{self._pause_recovery_target:.2f})"
                )
        return ",".join(missing) if missing else "none"

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

    @property
    def loss_kill_active(self) -> bool:
        """Effective state of the loss-streak lane pause.

        Off if disabled; off in paper sessions unless ``loss_kill_apply_in_paper``
        is set. This is the value that should gate pause behavior and drive the
        dashboard LOSS KILL badge, so paper sessions never show/trigger it.
        """
        if not self.loss_kill_switch_enabled:
            return False
        if self.is_paper and not self.loss_kill_apply_in_paper:
            return False
        return True

    # ──────────────────────────────────────────────────────────────
    # Trade Result Tracking
    # ──────────────────────────────────────────────────────────────

    def record_trade(
        self,
        pnl: float,
        strategy: str = "",
        market_id: str = "",
        window_size: str = "",
    ):
        """Record a completed trade result. Triggers lane pause if needed."""
        result = TradeResult(
            timestamp=datetime.now(),
            pnl=pnl,
            strategy=strategy,
            market_id=market_id,
            window_size=str(window_size or ""),
        )
        self._recent_trades.append(result)

        # Keep last 50 trades
        if len(self._recent_trades) > 50:
            self._recent_trades = self._recent_trades[-50:]

        # Track consecutive losses
        if pnl < 0:
            self._consecutive_losses += 1
            self._streak_loss_abs_total += abs(float(pnl))
            logger.info(f"Exposure: Loss recorded ({pnl:+.2f}), streak={self._consecutive_losses}")

            if self.loss_kill_active and self._consecutive_losses >= self.max_consecutive_losses:
                self._trigger_pause(
                    f"{self._consecutive_losses} consecutive losses",
                    window_size=str(window_size or ""),
                )
            elif not self.loss_kill_active and self._consecutive_losses >= self.max_consecutive_losses:
                _why = "disabled" if not self.loss_kill_switch_enabled else "paper session (live-only)"
                logger.info(f"Exposure: Loss-streak lane pause inert ({_why}) — would pause at {self._consecutive_losses} losses")
        else:
            if self._consecutive_losses > 0:
                logger.info(f"Exposure: Win recorded ({pnl:+.2f}), resetting loss streak")
            self._consecutive_losses = 0
            self._streak_loss_abs_total = 0.0

    def _trigger_pause(self, reason: str, *, window_size: str = ""):
        """Activate the loss-streak lane pause."""
        self._paused = True
        self._pause_reason = reason
        self._pause_start = datetime.now()
        self._cycles_since_pause = 0
        self._pause_recovery_anchor_pnl = self._portfolio_pnl
        self._pause_recovery_target = (
            self._streak_loss_abs_total * self.loss_pause_recovery_multiple
            if self.loss_pause_recovery_multiple > 0
            else 0.0
        )
        window_norm = str(window_size or "").strip()
        self._last_loss_kill_trigger = {
            "lane": str(self.lane_name or "UNKNOWN"),
            "window_size": window_norm,
            "reason": reason,
            "timestamp": self._pause_start.isoformat(),
            "streak_loss_abs_total": round(self._streak_loss_abs_total, 4),
            "pause_recovery_target": round(self._pause_recovery_target, 4),
            "pause_recovery_anchor_pnl": round(self._pause_recovery_anchor_pnl, 4),
        }

        if self.is_paper:
            logger.warning(
                f"LOSS-STREAK LANE PAUSE: {reason} — pausing for {self.pause_cycles} cycles"
            )
        else:
            mode_desc = "auto-resume" if self.resume_mode == PauseResumeMode.AUTO else "manual resume"
            logger.warning(
                f"LOSS-STREAK LANE PAUSE: {reason} — paused until {mode_desc}"
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
        """Deactivate the loss-streak lane pause."""
        self._paused = False
        self._pause_reason = ""
        self._cycles_since_pause = 0
        self._consecutive_losses = 0  # Reset on unpause
        self._streak_loss_abs_total = 0.0
        self._pause_recovery_anchor_pnl = self._portfolio_pnl
        self._pause_recovery_target = 0.0
        self._last_loss_kill_trigger = None
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
        self._streak_loss_abs_total = 0.0
        self._pause_recovery_anchor_pnl = self._portfolio_pnl
        self._pause_recovery_target = 0.0
        self._last_loss_kill_trigger = None
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
        self._streak_loss_abs_total = 0.0
        self._pause_recovery_anchor_pnl = self._portfolio_pnl
        self._pause_recovery_target = 0.0
        self._latest_green_window = None
        self._last_loss_kill_trigger = None

    def update_portfolio_pnl(self, pnl: float) -> None:
        """Update realized PnL used for recovery gating and operator diagnostics."""
        self._portfolio_pnl = float(pnl)

    def update_resume_window(
        self,
        *,
        green_window: Optional[bool] = None,
    ) -> None:
        """Push latest market-regime context into the pause/resume gate."""
        if green_window is not None:
            self._latest_green_window = bool(green_window)

    def _is_green_window(self, conditions: MarketConditions) -> bool:
        if conditions.green_window is not None:
            return bool(conditions.green_window)
        if self._latest_green_window is not None:
            return bool(self._latest_green_window)
        return self._should_resume_baseline(conditions)

    def _should_resume_baseline(self, conditions: MarketConditions) -> bool:
        return (
            conditions.volume_ratio >= self.low_volume_ratio
            and conditions.trend_strength >= 0.3
            and conditions.volatility >= self.low_vol_threshold
        )

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
        Enforces an explicit tier floor when configured; otherwise falls back to
        the legacy ``min_trade_usd * current_tier_multiplier`` floor.
        """
        multiplier = self.tier_multipliers.get(self._current_tier, 1.0)
        max_size = self.tier_sizing.get(self._current_tier, raw_size)
        if max_size <= 0:
            return 0.0
        scaled = raw_size * multiplier
        tier_floor = self.tier_floors.get(self._current_tier, 0.0)
        if tier_floor > 0:
            scaled = max(scaled, tier_floor)
        return min(scaled, max_size)

    def _resolve_tier_floors(self, exposure_config: Dict[str, Any]) -> Dict[ExposureTier, float]:
        legacy_base = float(exposure_config.get("min_trade_usd", 0.0) or 0.0)

        def floor_for(key: str, tier: ExposureTier) -> float:
            if key in exposure_config:
                return float(exposure_config.get(key) or 0.0)
            return legacy_base * self.tier_multipliers[tier]

        return {
            ExposureTier.FULL: floor_for("full_min_trade_usd", ExposureTier.FULL),
            ExposureTier.MODERATE: floor_for("moderate_min_trade_usd", ExposureTier.MODERATE),
            ExposureTier.MINIMAL: floor_for("minimal_min_trade_usd", ExposureTier.MINIMAL),
            ExposureTier.PAUSED: 0.0,
        }

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
            'last_loss_kill_trigger': dict(self._last_loss_kill_trigger) if self._last_loss_kill_trigger else None,
            'pause_recovery_target': self._pause_recovery_target,
            'pause_recovery_anchor_pnl': self._pause_recovery_anchor_pnl,
            'portfolio_pnl': self._portfolio_pnl,
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
    """Return weekend penalty multiplier (1.0=normal, lower=tighter max size).

    Reduces position size during weekend / low-liquidity periods when
    HYPE-style manipulation (a4385 CEX pump) is most likely to occur.
    Fri 20:00+ UTC and Sat/Sun use partial penalties (0.85 / 0.65), not full halving.
    """
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()  # 0=Mon … 5=Sat, 6=Sun
    utc_hour = now_utc.hour

    if weekday >= 5:
        return 0.65

    if weekday == 4 and utc_hour >= 20:
        return 0.85

    return 1.0
