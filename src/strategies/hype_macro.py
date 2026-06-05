"""
HYPE Macro Strategy.

HYPE is its own alt leg: HYPE spot/HTF/oracle data drives primary direction.
BTC is secondary context only. The class still reuses the shared alt-macro base
until that base is renamed/split, so keep HYPE-specific overrides explicit here.
"""
from __future__ import annotations  # PEP 604 `X | None` annotations on Python 3.9

import re
from typing import Any, Dict, List

from src.analysis.ai_agent import AIAgent
from src.analysis.hyperliquid_hype_service import (
    HyperliquidHypeService,
    hyperliquid_kwargs_from_config,
)
from src.analysis.math_utils import PositionSizer
from src.execution.exposure_manager import ExposureManager
from src.market.scanner import Market
from src.strategies.sol_macro import SolMacroSignal, SolMacroStrategy
from src.strategies.strategy_config import resolve_enabled_flag
from src.execution.performance_feedback import get_drift_min_edge_mult

import logging

logger = logging.getLogger(__name__)

HYPE_PATTERNS = [
    re.compile(r"\bhyperliquid\b", re.IGNORECASE),
    re.compile(r"\bhype\b(?=.*\b(?:price|token|coin|usd|usdt|up\s+or\s+down)\b)", re.IGNORECASE),
]
HYPE_UPDOWN_PATTERN = re.compile(
    r"(?:hyperliquid|hype)\s+up\s+or\s+down", re.IGNORECASE
)
HYPE_UPDOWN_SLUG_PREFIXES = (
    "hype-updown-",
    "hype-up-or-down-",
    "hyperliquid-up-or-down-",
)
NON_HYPE_ASSET_TERMS = ("bitcoin", "btc", "solana", "ethereum", "ether", "xrp", "ripple")


class HYPEMacroStrategy(SolMacroStrategy):
    """HYPE macro strategy with HYPE-first direction and Hyperliquid data."""

    def _hype_signal_guard_reason(self, signal: SolMacroSignal) -> str | None:
        if signal.action != "BUY_NO":
            return None
        side_source = str(signal.side_source or "")
        if "neutral_fallback" not in side_source:
            return None

        if signal.window_size == "5m":
            return "hype_5m_neutral_fallback_short_disabled"

        return None

    def _build_alt_service(self) -> HyperliquidHypeService:
        """Build the HYPE data/oracle service; never use SOL spot for this lane."""
        hk = hyperliquid_kwargs_from_config(self._hyperliquid_cfg)
        return HyperliquidHypeService(
            alt_symbol="HYPEUSDT",
            dynamic_beta_min=self.dynamic_beta_min,
            dynamic_beta_max=self.dynamic_beta_max,
            dynamic_beta_extreme_max=self.dynamic_beta_extreme_max,
            btc_spike_floor_pct_5m=self.btc_spike_floor_pct_5m,
            btc_spike_floor_pct_15m=self.btc_spike_floor_pct_15m,
            lag_signal_min_pct=self.lag_signal_min_pct,
            **hk,
        )

    def __init__(
        self,
        config: Dict[str, Any],
        ai_agent: AIAgent,
        position_sizer: PositionSizer,
        kelly_sizer=None,
        exposure_manager: ExposureManager = None,
        ai_broker=None,
    ):
        self._hyperliquid_cfg = dict(config.get("hyperliquid") or {})
        super().__init__(config, ai_agent, position_sizer, kelly_sizer, exposure_manager, ai_broker=ai_broker)
        self.config = config.get("strategies", {}).get("hype_macro", {})
        self.enabled = resolve_enabled_flag(
            "hype_macro",
            self.config,
            logger=logger,
        )
        self._apply_strategy_config(rebuild_service=True)
        self._signal_strategy_name = "hype_macro"

    def _alt_asset_code(self) -> str:
        """Hard-code HYPE identity so shared-base naming cannot leak SOL labels."""
        return "hype"

    def _is_solana_market(self, market: Market) -> bool:
        """Detect HYPE/Hyperliquid prediction markets."""
        text = (
            f"{market.question} {market.description} "
            f"{market.group_item_title} {market.slug}"
        ).lower()
        slug = (market.slug or "").lower()
        has_hype = (
            slug.startswith(HYPE_UPDOWN_SLUG_PREFIXES)
            or any(p.search(text) for p in HYPE_PATTERNS)
        )
        if not has_hype:
            return False
        if any(term in text for term in NON_HYPE_ASSET_TERMS):
            primary = f"{market.question} {market.group_item_title} {market.slug}".lower()
            if not any(p.search(primary) for p in HYPE_PATTERNS) and not slug.startswith(HYPE_UPDOWN_SLUG_PREFIXES):
                return False
        return True

    def _is_updown_market(self, market: Market) -> bool:
        """Detect HYPE Up or Down markets (15m / 5m windows)."""
        slug = (market.slug or "").lower()
        if slug.startswith(HYPE_UPDOWN_SLUG_PREFIXES):
            return True
        text = f"{market.question} {market.group_item_title}"
        return bool(HYPE_UPDOWN_PATTERN.search(text))

    async def scan_and_analyze(self, markets: List[Market], bankroll: float) -> List[SolMacroSignal]:
        """Run base scan, then enforce a hard floor for HYPE edge.

        Fix 2: never allow low/zero-edge HYPE entries through execution path,
        regardless of whether AI branch was used.
        """
        signals = await super().scan_and_analyze(markets, bankroll)
        base_hard = max(0.0, float(self.config.get("hard_min_edge", 0.0) or 0.0))
        filtered: List[SolMacroSignal] = []
        rejected = 0
        guard_rejected = 0
        edge_rejected = 0

        for signal in signals:
            guard_reason = self._hype_signal_guard_reason(signal)
            if guard_reason:
                rejected += 1
                guard_rejected += 1
                logger.info(
                    "HYPE local guard skip '%s...' reason=%s side_source=%s",
                    signal.market_question[:45],
                    guard_reason,
                    signal.side_source,
                )
                continue
            hard_min_edge = base_hard
            if (
                self._btc_trade_inputs_enabled()
                and self._btc_1h_regime_gates.get("enabled", False)
                and signal.btc_1h_regime
            ):
                hard_min_edge *= self._regime_min_edge_mult(signal.btc_1h_regime)
            hard_min_edge *= get_drift_min_edge_mult("hype_macro", self.full_config)
            if float(signal.edge or 0.0) < hard_min_edge:
                rejected += 1
                edge_rejected += 1
                logger.info(
                    "HYPE hard-edge skip '%s...' edge=%.4f < %.4f (ai_used=%s)",
                    signal.market_question[:45],
                    float(signal.edge or 0.0),
                    hard_min_edge,
                    signal.ai_used,
                )
                continue
            filtered.append(signal)

        if rejected:
            stats = dict(getattr(self, "last_scan_stats", {}) or {})
            top = dict(stats.get("top_skip_reasons", {}) or {})
            if edge_rejects := int(edge_rejected):
                top["hard_min_edge"] = int(top.get("hard_min_edge", 0)) + edge_rejects
            if guard_rejects := int(guard_rejected):
                top["local_hype_guard"] = int(top.get("local_hype_guard", 0)) + guard_rejects
            stats["top_skip_reasons"] = top
            stats["signals"] = len(filtered)
            self.last_scan_stats = stats

        return filtered
