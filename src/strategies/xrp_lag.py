"""
XRP Lag Strategy — BTC-to-XRP correlation lag trading.

Inherits SOLLagStrategy's full layered architecture.
Overrides: alt-coin service symbol, market filter patterns, config key, journal strategy name.
"""
import re
from typing import Any, Dict

from src.analysis.ai_agent import AIAgent
from src.analysis.math_utils import PositionSizer
from src.analysis.sol_btc_service import SOLBTCService
from src.execution.exposure_manager import ExposureManager
from src.market.scanner import Market
from src.strategies.sol_lag import SOLLagStrategy

XRP_PATTERNS = [
    re.compile(r"\bxrp\b", re.IGNORECASE),
    re.compile(r"\bripple\b", re.IGNORECASE),
]
XRP_UPDOWN_PATTERN = re.compile(
    r"(?:xrp|ripple)\s+up\s+or\s+down", re.IGNORECASE
)


class XRPLagStrategy(SOLLagStrategy):
    """XRP Lag strategy — same structure as SOL lag, XRPUSDT as the alt leg."""

    def __init__(
        self,
        config: Dict[str, Any],
        ai_agent: AIAgent,
        position_sizer: PositionSizer,
        kelly_sizer=None,
        exposure_manager: ExposureManager = None,
    ):
        super().__init__(config, ai_agent, position_sizer, kelly_sizer, exposure_manager)
        self.config = config.get("strategies", {}).get("xrp_lag", {})
        self.enabled = self.config.get("enabled", False)
        self.sol_service = SOLBTCService(alt_symbol="XRPUSDT")
        self.min_liquidity = self.config.get("min_liquidity", 5000)
        self.min_edge = self.config.get("min_edge", 0.08)
        self.min_edge_5m = self.config.get("min_edge_5m", self.min_edge)
        self.ai_confidence_threshold = self.config.get("ai_confidence_threshold", 0.60)
        self.max_ai_calls_per_scan = int(self.config.get("max_ai_calls_per_scan", 8))
        self.kelly_fraction = self.config.get("kelly_fraction", 0.15)
        self.entry_price_min = self.config.get("entry_price_min", 0.15)
        self.entry_price_max = self.config.get("entry_price_max", 0.85)
        self._signal_strategy_name = "xrp_lag"

    def _is_solana_market(self, market: Market) -> bool:
        """Detect XRP (not BTC-only) prediction markets."""
        text = f"{market.question} {market.description}".lower()
        has_xrp = any(p.search(text) for p in XRP_PATTERNS)
        is_btc_only = "bitcoin" in text and not has_xrp
        return has_xrp and not is_btc_only

    def _is_updown_market(self, market: Market) -> bool:
        """Detect XRP Up or Down markets (15m / 5m)."""
        return bool(XRP_UPDOWN_PATTERN.search(market.question))
