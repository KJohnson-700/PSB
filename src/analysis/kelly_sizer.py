"""
Kelly Sizing Module
Per-asset Kelly with streak-adjusted auto-correlation sizing.
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, Optional

from src.execution.performance_feedback import get_drift_kelly_mult

logger = logging.getLogger(__name__)


@dataclass
class AssetKellyConfig:
    """Kelly sizing parameters per asset."""
    base_kelly_fraction: float
    streak_multiplier_max: float
    streak_threshold: int
    min_kelly_fraction: float


def detect_window_from_question(question: str) -> str:
    """Infer 5m vs 15m window from market question time range.

    Examples:
        "Solana Up or Down - April 21, 1:30AM-1:35AM ET" → "5m"
        "Bitcoin Up or Down - April 21, 1:30AM-1:45AM ET" → "15m"
    """
    m = re.search(r'(\d+):(\d+(?:AM|PM)[–\-]\d+:\d+(?:AM|PM))', question, re.IGNORECASE)
    if not m:
        return "15m"
    time_range = m.group(1)
    try:
        start_str, end_str = time_range.split('–') if '–' in time_range else time_range.split('-')
        start_minutes = _time_to_minutes(start_str.strip())
        end_minutes = _time_to_minutes(end_str.strip())
        delta = abs(end_minutes - start_minutes)
        if delta <= 6:
            return "5m"
        if delta >= 45:
            return "1h"
        if delta >= 23:
            return "30m"  # legacy: 30m product is discontinued but historic rows persist
        return "15m"
    except Exception:
        return "15m"


def _time_to_minutes(t: str) -> int:
    """Convert '1:30AM' or '01:30' to minutes since midnight."""
    t = t.strip()
    m = re.match(r'(\d{1,2}):(\d{2})(AM|PM)?', t, re.IGNORECASE)
    if not m:
        return 0
    h, mn = int(m.group(1)), int(m.group(2))
    is_pm = m.group(3) and m.group(3).upper() == 'PM'
    if is_pm and h != 12:
        h += 12
    elif not is_pm and h == 12:
        h = 0
    return h * 60 + mn


class KellySizer:
    """
    Per-asset Kelly position sizing with streak-based auto-correlation adjustment.

    Tracks outcomes per (strategy, window) for display purposes.
    Sizing decisions use combined strategy streak across all windows.
    """

    def __init__(self, config: Dict):
        trading_cfg = config.get("trading", {})
        strategies_cfg = config.get("strategies", {})

        self._defaults = {
            "bitcoin":        AssetKellyConfig(base_kelly_fraction=0.15, streak_multiplier_max=1.5, streak_threshold=3, min_kelly_fraction=0.08),
            "sol_macro":        AssetKellyConfig(base_kelly_fraction=0.15, streak_multiplier_max=1.4, streak_threshold=3, min_kelly_fraction=0.08),
            "eth_macro":        AssetKellyConfig(base_kelly_fraction=0.12, streak_multiplier_max=1.4, streak_threshold=3, min_kelly_fraction=0.06),
            "hype_macro":       AssetKellyConfig(base_kelly_fraction=0.08, streak_multiplier_max=1.3, streak_threshold=4, min_kelly_fraction=0.04),
            "xrp_macro":        AssetKellyConfig(base_kelly_fraction=0.10, streak_multiplier_max=1.5, streak_threshold=3, min_kelly_fraction=0.05),
            "doge_macro":       AssetKellyConfig(base_kelly_fraction=0.10, streak_multiplier_max=1.4, streak_threshold=3, min_kelly_fraction=0.05),
            "bnb_macro":        AssetKellyConfig(base_kelly_fraction=0.10, streak_multiplier_max=1.4, streak_threshold=3, min_kelly_fraction=0.05),
        }

        for strat, cfg in self._defaults.items():
            strat_cfg = strategies_cfg.get(strat, {})
            if "kelly_fraction" in strat_cfg:
                cfg.base_kelly_fraction = float(strat_cfg["kelly_fraction"])

        global_frac = float(trading_cfg.get("kelly_fraction", 0.25))
        self._global_kelly_fraction = global_frac
        self._min_position = float(trading_cfg.get("default_position_size", 1.0) or 1.0)
        self._max_position = float(trading_cfg.get("max_position_size", 0.0) or 0.0)
        self._max_position_pct = float(trading_cfg.get("max_exposure_per_trade", 0.05) or 0.05)

        self._recent_outcomes: Dict[str, list] = {s: [] for s in self._defaults}
        self._recent_outcomes_by_window: Dict[tuple, list] = {}
        self._root_config = config

    def reload_from_config(self, config: Dict) -> None:
        """Refresh config-derived Kelly fractions without clearing streak state."""
        trading_cfg = config.get("trading", {}) or {}
        strategies_cfg = config.get("strategies", {}) or {}
        for strat, cfg in self._defaults.items():
            strat_cfg = strategies_cfg.get(strat, {}) or {}
            if "kelly_fraction" in strat_cfg:
                cfg.base_kelly_fraction = float(strat_cfg["kelly_fraction"])
        self._global_kelly_fraction = float(
            trading_cfg.get("kelly_fraction", self._global_kelly_fraction)
        )
        self._min_position = float(
            trading_cfg.get("default_position_size", self._min_position) or self._min_position
        )
        self._max_position = float(
            trading_cfg.get("max_position_size", self._max_position) or self._max_position
        )
        self._max_position_pct = float(
            trading_cfg.get("max_exposure_per_trade", self._max_position_pct)
            or self._max_position_pct
        )
        self._root_config = config

    def _window_key(self, strategy: str, window: str) -> tuple:
        return (strategy, window)

    def record_outcome(
        self, strategy: str, outcome: bool, window: Optional[str] = None
    ) -> None:
        """Record trade outcome for streak tracking. outcome=True = win.

        Args:
            strategy: strategy key (bitcoin, sol_macro, etc.)
            outcome: True = win, False = loss
            window: "5m" or "15m". Auto-detected from market_question if not provided.
        """
        if strategy not in self._recent_outcomes:
            self._recent_outcomes[strategy] = []
        self._recent_outcomes[strategy].append(outcome)
        if len(self._recent_outcomes[strategy]) > 20:
            self._recent_outcomes[strategy].pop(0)

        if window is not None:
            wk = self._window_key(strategy, window)
            if wk not in self._recent_outcomes_by_window:
                self._recent_outcomes_by_window[wk] = []
            self._recent_outcomes_by_window[wk].append(outcome)
            if len(self._recent_outcomes_by_window[wk]) > 20:
                self._recent_outcomes_by_window[wk].pop(0)

    def get_current_streak(self, strategy: str, window: Optional[str] = None) -> int:
        """Return current consecutive win streak.

        If window is None: combined streak across all windows (for sizing).
        If window is set: streak for that specific window (for display).
        """
        if window is not None:
            outcomes = self._recent_outcomes_by_window.get(
                self._window_key(strategy, window), []
            )
        else:
            outcomes = self._recent_outcomes.get(strategy, [])
        if not outcomes or not outcomes[-1]:
            return 0
        streak = 0
        for o in reversed(outcomes):
            if o:
                streak += 1
            else:
                break
        return streak

    def get_window_stats(
        self, strategy: str, window: str
    ) -> Dict:
        """Return {streak, wins, losses, wr} for a specific strategy+window."""
        outcomes = self._recent_outcomes_by_window.get(
            self._window_key(strategy, window), []
        )
        wins = sum(1 for o in outcomes if o)
        losses = len(outcomes) - wins
        wr = (wins / len(outcomes) * 100) if outcomes else 0.0
        streak = self.get_current_streak(strategy, window)
        return {
            "streak": streak,
            "wins": wins,
            "losses": losses,
            "wr": round(wr, 1),
            "trades": len(outcomes),
        }

    def get_all_window_stats(self) -> Dict[str, Dict[str, Dict]]:
        """Return per-strategy, per-window stats for dashboard rendering.

        Returns: {
            "bitcoin": {
                "5m": {"streak": 2, "wins": 5, "losses": 2, "wr": 71.4, "trades": 7},
                "15m": {"streak": 0, "wins": 3, "losses": 4, "wr": 42.9, "trades": 7},
            },
            ...
        }
        """
        result = {}
        for strat in self._defaults:
            result[strat] = {}
            for win in ("5m", "15m", "30m", "1h"):
                result[strat][win] = self.get_window_stats(strat, win)
        return result

    def get_streak_multiplier(self, strategy: str, window: Optional[str] = None) -> float:
        """Return streak multiplier for sizing (1.0 = no adjustment).

        2026-07-14 K2' (operator GO; reference = MrFadiAi spec, vault research
        20260608): symmetric gradual streak — 1.0 + 0.1*win_streak −
        0.2*loss_streak, clamped [0.5, per-asset max]. Replaces the win-only
        step function (no boost until threshold, then instant max) that also
        pooled ALL windows so a 5m win streak inflated 15m/1h sizing.
        Window-scoped when the caller passes its window."""
        cfg = self._defaults.get(strategy)
        if not cfg:
            return 1.0
        if window is not None:
            outcomes = self._recent_outcomes_by_window.get(
                self._window_key(strategy, window), []
            )
        else:
            outcomes = self._recent_outcomes.get(strategy, [])
        streak_w = 0
        streak_l = 0
        for o in reversed(outcomes):
            if o:
                if streak_l:
                    break
                streak_w += 1
            else:
                if streak_w:
                    break
                streak_l += 1
        mult = 1.0 + 0.1 * streak_w - 0.2 * streak_l
        upper = min(1.5, float(cfg.streak_multiplier_max or 1.5))
        return max(0.5, min(mult, upper))

    def get_kelly_fraction(
        self, strategy: str, streak_multiplier: float = None, window: str = None
    ) -> float:
        """Return effective Kelly fraction after streak adjustment."""
        cfg = self._defaults.get(strategy)
        if not cfg:
            return self._global_kelly_fraction
        frac = cfg.base_kelly_fraction
        if streak_multiplier is None:
            streak_multiplier = self.get_streak_multiplier(strategy, window)
        frac = frac * streak_multiplier
        frac = frac * get_drift_kelly_mult(strategy, self._root_config)
        return max(cfg.min_kelly_fraction, min(frac, 1.0))

    def size_from_edge(
        self,
        strategy: str,
        bankroll: float,
        edge: float,
        streak_multiplier: float = None,
        window: str = None,
    ) -> float:
        """
        Calculate Kelly size from edge using streak-adjusted Kelly fraction.
        """
        if edge <= 0:
            return 0.0

        frac = self.get_kelly_fraction(strategy, streak_multiplier, window)

        base_size = edge * frac * bankroll

        cap = bankroll * self._max_position_pct
        if self._max_position > 0:
            cap = min(cap, self._max_position)
        if cap <= 0:
            return 0.0
        size = min(max(base_size, self._min_position), cap)

        return round(size, 2)

    def size_binary_position(
        self,
        strategy: str,
        bankroll: float,
        win_probability: float,
        contract_price: float,
        streak_multiplier: float = None,
        window: str = None,
    ) -> float:
        """Kelly size for binary contracts using true payout odds from price.

        2026-07-21: window param added for parity with size_from_edge (window-scoped
        streak fraction). This is the conviction-proportional sizing path — size scales
        with the real win-probability edge AND the price odds, so high-conviction trades
        run up to the cap and marginal ones sit near the floor (vs the linear
        size_from_edge which was flattened by the max_edge_updown clamp).
        """
        p = max(0.0, min(1.0, float(win_probability)))
        c = max(0.01, min(0.99, float(contract_price)))
        b = (1.0 - c) / c
        if b <= 0:
            return 0.0

        frac = self.get_kelly_fraction(strategy, streak_multiplier, window)
        full_kelly = ((b * p) - (1.0 - p)) / b
        if full_kelly <= 0:
            return 0.0

        wager_fraction = full_kelly * frac
        base_size = bankroll * wager_fraction
        cap = bankroll * self._max_position_pct
        if self._max_position > 0:
            cap = min(cap, self._max_position)
        if cap <= 0:
            return 0.0
        size = min(max(base_size, self._min_position), cap)
        return round(size, 2)

    def get_asset_config(self, strategy: str) -> Optional[AssetKellyConfig]:
        """Return Kelly config for a strategy, or None."""
        return self._defaults.get(strategy)


_DEFAULT_KELLY_SIZER: Optional[KellySizer] = None


def get_kelly_sizer(config: Dict) -> KellySizer:
    global _DEFAULT_KELLY_SIZER
    if _DEFAULT_KELLY_SIZER is None:
        _DEFAULT_KELLY_SIZER = KellySizer(config)
    else:
        _DEFAULT_KELLY_SIZER.reload_from_config(config)
    return _DEFAULT_KELLY_SIZER
