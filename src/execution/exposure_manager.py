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
from typing import Dict, Any, List, Optional, Callable, Tuple

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
        # Per-asset tier overrides (2026-07-11): e.g. exposure.per_asset.btc lets ONE
        # lane's tier cap/multiplier differ (btc was pinned MINIMAL x0.2/$8 while alts
        # ran MODERATE $15.60 — wins too small to matter). Per-lane rule: other assets
        # untouched unless they get their own block.
        self._apply_per_asset_overrides(exposure_config)

        # --- Loss-streak lane pause config ---
        self.loss_kill_switch_enabled = exposure_config.get('loss_kill_switch_enabled', True)
        # The loss-streak lane pause is a LIVE-trading safety. In paper it is inert
        # by default (paper sessions are for calibration/data — they must keep
        # trading the losing lanes, not pause them). Opt in via this flag (tests do).
        self.loss_kill_apply_in_paper = bool(
            exposure_config.get('loss_kill_apply_in_paper', False)
        )
        # 2026-07-26 operator GO — DIRECTIONAL BREAKER, per-lane allowlist. When non-empty,
        # the 3-consecutive-loss pause enforces ONLY on these lanes ("asset|window|side",
        # lowercased); every other lane is exempt. The directional_breaker_shadow proved a
        # BLANKET breaker LOSES money (false-cuts winners, net -$47..-$88) but is a clear
        # per-lane winner on specific lanes (xrp|5m|down +$25.37, xrp|15m|down +$8.58).
        # Empty/unset = legacy all-lanes behavior (unchanged).
        self.loss_kill_lane_allowlist = self._parse_lane_allowlist(
            exposure_config.get('loss_kill_lane_allowlist')
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
        # TODO(go-live): this state is in-memory only — there is a `to_dict` but no
        # `from_dict`/load, so every restart resets to tier=FULL with zero loss
        # streak (overnight 2026-06-17 "Bug #19": bot re-took a known -EV BNB pattern
        # at full size right after a restart). Intentional during smoke/paper testing
        # (restart == fresh $500 session). Before LIVE, add persist-on-update +
        # load-on-start (consecutive_losses, _portfolio_pnl, _recent_trades) so the
        # loss-streak brake survives a crash-restart within the same trading intent.
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
        # --- Per-lane loss-streak state (2026-07-25 operator GO) ---
        # The loss kill switch is PER-LANE, not per-asset: 3 consecutive losses on
        # e.g. doge 5m|down pauses ONLY that lane, not doge 5m|up / 15m / 1h. Keyed by
        # "window|side" (side normalized to up/down). The asset-level _paused/_pause_*
        # scalars above are now only driven by MANUAL pause; loss-streak pausing lives
        # entirely in _lane_state and is gated per-candidate via lane_paused().
        self._lane_state: Dict[str, Dict[str, Any]] = {}

    def _apply_per_asset_overrides(self, exposure_config: Dict[str, Any]) -> None:
        """Apply exposure.per_asset.<lane_name.lower()> overrides to this manager.

        Supported keys: full_size / moderate_size / minimal_size (tier USD caps) and
        full_multiplier / moderate_multiplier / minimal_multiplier (tier multipliers).
        Missing keys leave the shared defaults untouched. Never raises.
        """
        try:
            pa = (exposure_config or {}).get("per_asset") or {}
            mine = pa.get(str(getattr(self, "lane_name", "") or "").lower()) or {}
            if not mine:
                return
            _size_keys = {
                "full_size": ExposureTier.FULL,
                "moderate_size": ExposureTier.MODERATE,
                "minimal_size": ExposureTier.MINIMAL,
            }
            _mult_keys = {
                "full_multiplier": ExposureTier.FULL,
                "moderate_multiplier": ExposureTier.MODERATE,
                "minimal_multiplier": ExposureTier.MINIMAL,
            }
            for k, tier in _size_keys.items():
                if k in mine:
                    self.tier_sizing[tier] = float(mine[k])
            for k, tier in _mult_keys.items():
                if k in mine:
                    self.tier_multipliers[tier] = float(mine[k])
            logger.info(
                "Exposure per-asset overrides applied for %s: %s",
                getattr(self, "lane_name", "?"), dict(mine),
            )
        except Exception:
            logger.warning("per-asset exposure override failed (ignored)", exc_info=True)

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
        self._apply_per_asset_overrides(exposure_config)
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
        if "loss_kill_lane_allowlist" in exposure_config:
            self.loss_kill_lane_allowlist = self._parse_lane_allowlist(
                exposure_config.get("loss_kill_lane_allowlist")
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

        # Loss streak penalty — gated by loss_kill_active so it mirrors the lane
        # pause: inert in paper (calibration/data) unless loss_kill_apply_in_paper.
        # Without this gate, paper losses fill at full size but post-loss recoveries
        # get throttled to MINIMAL, so wins can never offset the earlier full-size
        # losses — an asymmetric ratchet that structurally bleeds the paper session.
        if self._consecutive_losses >= 2 and self.loss_kill_active:
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
        side: str = "",
    ):
        """Record a completed trade result. Triggers the PER-LANE loss pause if needed.

        ``side`` (BUY_YES/BUY_NO/YES/NO/up/down) is normalized to up/down and combined
        with ``window_size`` into the lane key, so the loss streak and pause are scoped
        to the exact lane (asset+window+side), not the whole asset.
        """
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

        key = self._lane_key(window_size, side)
        st = self._lane_st(key)

        # Track consecutive losses PER LANE
        if pnl < 0:
            st["consecutive_losses"] += 1
            st["streak_loss_abs_total"] += abs(float(pnl))
            self._streak_loss_abs_total += abs(float(pnl))
            logger.info(
                f"Exposure[{self.lane_name}/{key}]: Loss recorded ({pnl:+.2f}), "
                f"lane streak={st['consecutive_losses']}"
            )
            if (
                self.loss_kill_active
                and self._lane_breaker_enabled(key)
                and st["consecutive_losses"] >= self.max_consecutive_losses
            ):
                self._trigger_pause_lane(
                    key, st, f"{st['consecutive_losses']} consecutive losses",
                    window_size=str(window_size or ""), side=side,
                )
            elif not self.loss_kill_active and st["consecutive_losses"] >= self.max_consecutive_losses:
                _why = "disabled" if not self.loss_kill_switch_enabled else "paper session (live-only)"
                logger.info(
                    f"Exposure[{self.lane_name}/{key}]: Loss-streak lane pause inert "
                    f"({_why}) — would pause at {st['consecutive_losses']} losses"
                )
        else:
            if st["consecutive_losses"] > 0:
                logger.info(f"Exposure[{self.lane_name}/{key}]: Win recorded ({pnl:+.2f}), resetting lane streak")
            st["consecutive_losses"] = 0
            st["streak_loss_abs_total"] = 0.0
        # Compat aggregate for tier penalty / dashboard: worst lane streak.
        self._consecutive_losses = max(
            (s["consecutive_losses"] for s in self._lane_state.values()), default=0
        )

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
    # Per-lane loss-streak kill switch (2026-07-25 operator GO)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _norm_side(side: Any) -> str:
        """Normalize any side/action vocab to the lane 'up'/'down'."""
        s = str(side or "").strip().lower()
        if s in ("buy_yes", "yes", "up", "long"):
            return "up"
        if s in ("buy_no", "no", "down", "short"):
            return "down"
        return s  # unknown → keep raw so it still isolates by whatever was passed

    def _lane_key(self, window: Any, side: Any) -> str:
        return f"{str(window or '').strip().lower()}|{self._norm_side(side)}"

    def _parse_lane_allowlist(self, raw: Any) -> set:
        """Normalize allowlist entries to 'asset|window|up-or-down' so config can be
        written with any side vocab (BUY_NO/short/down) or stray spaces and still match
        the runtime lane id. Codex 2026-07-26."""
        out: set = set()
        for x in (raw or []):
            s = str(x or "").strip().lower()
            if not s:
                continue
            parts = [p.strip() for p in s.split("|")]
            if len(parts) == 3:
                parts[2] = self._norm_side(parts[2])
            out.add("|".join(parts))
        return out

    def _lane_breaker_enabled(self, key: str) -> bool:
        """True if the loss-streak breaker enforces on this lane. Empty allowlist =
        every lane (legacy). Otherwise only 'asset|window|side' lanes in the allowlist
        (e.g. 'xrp|5m|down'). Keeps the breaker per-lane, not blanket."""
        if not self.loss_kill_lane_allowlist:
            return True
        return f"{str(self.lane_name or '').strip().lower()}|{key}" in self.loss_kill_lane_allowlist

    def _lane_st(self, key: str) -> Dict[str, Any]:
        st = self._lane_state.get(key)
        if st is None:
            st = {
                "consecutive_losses": 0,
                "paused": False,
                "pause_reason": "",
                "pause_start": None,
                "cycles_since_pause": 0,
                "streak_loss_abs_total": 0.0,
                "recovery_anchor_pnl": 0.0,
                "recovery_target": 0.0,
                "last_trigger": None,
            }
            self._lane_state[key] = st
        return st

    def _trigger_pause_lane(
        self, key: str, st: Dict[str, Any], reason: str, *,
        window_size: str = "", side: str = "",
    ) -> None:
        """Activate the loss-streak pause for ONE lane."""
        st["paused"] = True
        st["pause_reason"] = reason
        st["pause_start"] = datetime.now()
        st["cycles_since_pause"] = 0
        st["recovery_anchor_pnl"] = self._portfolio_pnl
        st["recovery_target"] = (
            st["streak_loss_abs_total"] * self.loss_pause_recovery_multiple
            if self.loss_pause_recovery_multiple > 0
            else 0.0
        )
        lane_label = f"{self.lane_name}|{key}"
        st["last_trigger"] = {
            "lane": str(self.lane_name or "UNKNOWN"),
            "lane_key": key,
            "window_size": str(window_size or "").strip(),
            "side": self._norm_side(side),
            "reason": reason,
            "timestamp": st["pause_start"].isoformat(),
            "streak_loss_abs_total": round(st["streak_loss_abs_total"], 4),
            "recovery_target": round(st["recovery_target"], 4),
            "recovery_anchor_pnl": round(st["recovery_anchor_pnl"], 4),
        }
        # Mirror to the asset-level diagnostic slot so the dashboard/ops_pulse still
        # surface the most-recent kill (now lane-scoped).
        self._last_loss_kill_trigger = st["last_trigger"]
        if self.is_paper:
            logger.warning(f"LOSS-STREAK LANE PAUSE [{lane_label}]: {reason} — pausing {self.pause_cycles} cycles")
        else:
            mode_desc = "auto-resume" if self.resume_mode == PauseResumeMode.AUTO else "manual resume"
            logger.warning(f"LOSS-STREAK LANE PAUSE [{lane_label}]: {reason} — paused until {mode_desc}")

        if self._notifications is not None:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(
                        self._notifications.notify_kill_lane(
                            lane_label, reason, st["consecutive_losses"]
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
                        self._on_pause_ai_callback(reason, st["consecutive_losses"])
                    )
            except Exception:
                pass

    def _unpause_lane(self, key: str, st: Dict[str, Any], reason: str) -> None:
        """Deactivate the loss-streak pause for ONE lane."""
        st["paused"] = False
        st["pause_reason"] = ""
        st["cycles_since_pause"] = 0
        st["consecutive_losses"] = 0
        st["streak_loss_abs_total"] = 0.0
        st["recovery_anchor_pnl"] = self._portfolio_pnl
        st["recovery_target"] = 0.0
        st["last_trigger"] = None
        logger.info(f"EXPOSURE LANE RESUMED [{self.lane_name}|{key}]: {reason}")

    def _should_resume_lane(self, st: Dict[str, Any], conditions: MarketConditions) -> bool:
        """Per-lane mirror of _should_resume (reads the lane's recovery anchor/target)."""
        base_ok = (
            conditions.volume_ratio >= self.low_volume_ratio
            and conditions.trend_strength >= 0.3
            and conditions.volatility >= self.low_vol_threshold
        )
        if not base_ok:
            return False
        if self.require_green_window_for_resume and not self._is_green_window(conditions):
            return False
        if st["recovery_target"] > 0:
            recovered = max(0.0, self._portfolio_pnl - st["recovery_anchor_pnl"])
            if recovered < st["recovery_target"]:
                return False
        return True

    def lane_paused(self, window: Any, side: Any, conditions: Optional[MarketConditions] = None) -> Tuple[bool, str]:
        """PER-LANE loss-kill gate — call per-candidate in the strategy scan loop.

        Returns (paused, reason). Applies the same cooldown / auto-resume logic as the
        old asset-wide pause, but scoped to one lane. Inert when loss_kill_active is
        False (disabled or paper without loss_kill_apply_in_paper). Manual/global pause
        is handled separately by get_exposure().
        """
        if not self.loss_kill_active:
            return False, ""
        key = self._lane_key(window, side)
        if not self._lane_breaker_enabled(key):
            return False, ""
        st = self._lane_state.get(key)
        if not st or not st["paused"]:
            return False, ""
        cond = conditions if conditions is not None else self._last_conditions
        if self.is_paper or self.resume_mode == PauseResumeMode.AUTO:
            st["cycles_since_pause"] += 1
            if st["cycles_since_pause"] >= self.pause_cycles:
                if st["cycles_since_pause"] >= self.max_pause_cycles and st["recovery_target"] <= 0:
                    self._unpause_lane(key, st, "Max pause cycles reached")
                    return False, ""
                if cond is not None and self._should_resume_lane(st, cond):
                    self._unpause_lane(key, st, "Conditions improved after pause")
                    return False, ""
                return True, f"lane_paused({st['pause_reason']}) [cycle {st['cycles_since_pause']}/{self.max_pause_cycles}]"
            return True, f"lane_paused({st['pause_reason']}) cooling [cycle {st['cycles_since_pause']}/{self.pause_cycles}]"
        # Manual resume mode (live): stay paused until manual_resume().
        return True, f"lane_paused({st['pause_reason']}) — manual resume required"

    def lane_pause_snapshot(self) -> Dict[str, Any]:
        """Diagnostic: currently-paused lanes (for dashboard/ops_pulse)."""
        return {
            k: {"reason": v["pause_reason"], "streak": v["consecutive_losses"]}
            for k, v in self._lane_state.items() if v.get("paused")
        }

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
        self._lane_state.clear()
        logger.info("Exposure: MANUAL RESUME — all clear (all lanes)")

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
        self._lane_state.clear()

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
        """Get exposure status for dashboard/logging.

        Loss-kill pausing is now PER-LANE; the asset-level fields here AGGREGATE the
        per-lane state so the dashboard/tests keep a coherent view: ``paused`` is true
        if ANY lane is loss-paused (or manual), and the recovery/reason fields report
        the representative (most-recently-triggered) paused lane.
        """
        paused_lanes = {k: v for k, v in self._lane_state.items() if v.get("paused")}
        _rep = None
        if paused_lanes:
            _rep_key = (self._last_loss_kill_trigger or {}).get("lane_key")
            _rep = paused_lanes.get(_rep_key) or next(iter(paused_lanes.values()))
        return {
            'tier': self._current_tier.value,
            'multiplier': self.tier_multipliers.get(self._current_tier, 0),
            'max_size': self.tier_sizing.get(self._current_tier, 0),
            'paused': bool(paused_lanes) or self._manual_pause,
            'pause_reason': (_rep["pause_reason"] if _rep else ('manual' if self._manual_pause else '')),
            'consecutive_losses': self._consecutive_losses,
            'cycles_since_pause': (_rep["cycles_since_pause"] if _rep else 0),
            'recent_trades': len(self._recent_trades),
            'recent_pnl': sum(t.pnl for t in self._recent_trades[-10:]),
            'conditions': {
                'volatility': self._last_conditions.volatility if self._last_conditions else 0,
                'volume_ratio': self._last_conditions.volume_ratio if self._last_conditions else 0,
                'trend_strength': self._last_conditions.trend_strength if self._last_conditions else 0,
            } if self._last_conditions else {},
            'last_loss_kill_trigger': dict(self._last_loss_kill_trigger) if self._last_loss_kill_trigger else None,
            'pause_recovery_target': (_rep["recovery_target"] if _rep else self._pause_recovery_target),
            'pause_recovery_anchor_pnl': (_rep["recovery_anchor_pnl"] if _rep else self._pause_recovery_anchor_pnl),
            'portfolio_pnl': self._portfolio_pnl,
            'paused_lanes': self.lane_pause_snapshot(),
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
    # 2026-07-11 DATA-DISABLED (operator C): live era-split (>=06-18) shows WEEKEND
    # avg -$0.043/trade (40% WR, n=1199) vs WEEKDAY -$0.227 (38%, n=2252) — the
    # penalty was cutting size on the bot's BETTER days. Manipulation thesis (a4385)
    # not supported by realized PnL. Restore by reverting to 0.65/0.85 returns.
    return 1.0
