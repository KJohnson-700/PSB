"""
HYPE Macro Strategy — BTC-to-HYPE correlation lag trading.

Uses SolMacroStrategy architecture and gates, but swaps in Hyperliquid HYPE
candle data via HyperliquidHypeService.
"""
import re
from typing import Any, Dict, List

from src.analysis.ai_agent import AIAgent
from src.analysis.hyperliquid_hype_service import HyperliquidHypeService
from src.analysis.math_utils import PositionSizer
from src.execution.exposure_manager import ExposureManager
from src.market.scanner import Market
from src.strategies.sol_macro import SolMacroSignal, SolMacroStrategy
from src.strategies.strategy_config import resolve_enabled_flag

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
    """HYPE macro strategy — same layered architecture as SOL macro."""

    def _build_alt_service(self) -> HyperliquidHypeService:
        return HyperliquidHypeService(
            alt_symbol="HYPEUSDT",
            dynamic_beta_min=self.dynamic_beta_min,
            dynamic_beta_max=self.dynamic_beta_max,
            dynamic_beta_extreme_max=self.dynamic_beta_extreme_max,
            btc_spike_floor_pct_5m=self.btc_spike_floor_pct_5m,
            btc_spike_floor_pct_15m=self.btc_spike_floor_pct_15m,
            lag_signal_min_pct=self.lag_signal_min_pct,
        )

    def __init__(
        self,
        config: Dict[str, Any],
        ai_agent: AIAgent,
        position_sizer: PositionSizer,
        kelly_sizer=None,
        exposure_manager: ExposureManager = None,
    ):
        super().__init__(config, ai_agent, position_sizer, kelly_sizer, exposure_manager)
        self.config = config.get("strategies", {}).get("hype_macro", {})
        self.enabled = resolve_enabled_flag(
            "hype_macro",
            self.config,
            logger=logger,
        )
        self._apply_strategy_config(rebuild_service=True)
        self._signal_strategy_name = "hype_macro"

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
        hard_min_edge = max(0.05, float(self.config.get("hard_min_edge", 0.05)))
        filtered: List[SolMacroSignal] = []
        rejected = 0

        for signal in signals:
            if float(signal.edge or 0.0) < hard_min_edge:
                rejected += 1
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
            top["hard_min_edge"] = int(top.get("hard_min_edge", 0)) + rejected
            stats["top_skip_reasons"] = top
            stats["signals"] = len(filtered)
            self.last_scan_stats = stats

        return filtered
