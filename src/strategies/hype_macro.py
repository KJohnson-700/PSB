"""
HYPE Macro Strategy — BTC-to-HYPE correlation lag trading.

Uses SolMacroStrategy architecture and gates, but swaps in Hyperliquid HYPE
candle data via HyperliquidHypeService.
"""
import re
from typing import Any, Dict

from src.analysis.ai_agent import AIAgent
from src.analysis.hyperliquid_hype_service import HyperliquidHypeService
from src.analysis.math_utils import PositionSizer
from src.execution.exposure_manager import ExposureManager
from src.market.scanner import Market
from src.strategies.sol_macro import SolMacroStrategy
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


class HYPEMacroStrategy(SolMacroStrategy):
    """HYPE macro strategy — same layered architecture as SOL macro."""

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
        self.sol_service = HyperliquidHypeService(alt_symbol="HYPEUSDT")
        self.min_liquidity = self.config.get("min_liquidity", 100)
        self.min_edge = self.config.get("min_edge", 0.09)
        self.min_edge_5m = self.config.get("min_edge_5m", self.min_edge)
        self.ai_confidence_threshold = self.config.get("ai_confidence_threshold", 0.60)
        self.max_ai_calls_per_scan = int(self.config.get("max_ai_calls_per_scan", 8))
        self.kelly_fraction = self.config.get("kelly_fraction", 0.15)
        self.entry_price_min = self.config.get("entry_price_min", 0.46)
        self.entry_price_max = self.config.get("entry_price_max", 0.54)
        self.ai_hold_veto_ttl_sec = self.config.get("ai_hold_veto_ttl_sec", 300)
        self.min_edge_5m_ai_override = self.config.get("min_edge_5m_ai_override", 0.10)
        self._signal_strategy_name = "hype_macro"

    def _is_solana_market(self, market: Market) -> bool:
        """Detect HYPE/Hyperliquid prediction markets."""
        text = f"{market.question} {market.description}".lower()
        slug = (market.slug or "").lower()
        has_hype = (
            slug.startswith(("hype-updown-", "hype-up-or-down-", "hyperliquid-up-or-down-"))
            or any(p.search(text) for p in HYPE_PATTERNS)
        )
        is_btc_only = "bitcoin" in text and not has_hype
        return has_hype and not is_btc_only

    def _is_updown_market(self, market: Market) -> bool:
        """Detect HYPE Up or Down markets (15m / 5m windows)."""
        return bool(HYPE_UPDOWN_PATTERN.search(market.question))
