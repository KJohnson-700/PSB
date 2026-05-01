"""
XRP Macro Strategy — BTC-to-XRP correlation lag trading.

Inherits SolMacroStrategy's full layered architecture.
Overrides: alt-coin service symbol, market filter patterns, config key, journal strategy name.
"""
import re
from typing import Any, Dict

from src.analysis.ai_agent import AIAgent
from src.analysis.math_utils import PositionSizer
from src.analysis.sol_btc_service import SOLBTCService
from src.execution.exposure_manager import ExposureManager
from src.market.scanner import Market
from src.strategies.sol_macro import SolMacroStrategy
from src.strategies.strategy_config import resolve_enabled_flag

import logging

logger = logging.getLogger(__name__)

XRP_PATTERNS = [
    re.compile(r"\bxrp\b", re.IGNORECASE),
    re.compile(r"\bripple\b", re.IGNORECASE),
]
XRP_UPDOWN_PATTERN = re.compile(
    r"(?:xrp|ripple)\s+up\s+or\s+down", re.IGNORECASE
)


class XRPMacroStrategy(SolMacroStrategy):
    """XRP Macro strategy — same structure as SOL macro, XRPUSDT as the alt leg."""

    def _build_alt_service(self) -> SOLBTCService:
        return SOLBTCService(
            alt_symbol="XRPUSDT",
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
        self.config = config.get("strategies", {}).get("xrp_macro", {})
        self.enabled = resolve_enabled_flag(
            "xrp_macro",
            self.config,
            logger=logger,
        )
        self._apply_strategy_config(rebuild_service=True)
        self._signal_strategy_name = "xrp_macro"

    def _is_solana_market(self, market: Market) -> bool:
        """Detect XRP (not BTC-only) prediction markets."""
        text = f"{market.question} {market.description}".lower()
        has_xrp = any(p.search(text) for p in XRP_PATTERNS)
        is_btc_only = "bitcoin" in text and not has_xrp
        return has_xrp and not is_btc_only

    def _is_updown_market(self, market: Market) -> bool:
        """Detect XRP Up or Down markets (15m / 5m)."""
        return bool(XRP_UPDOWN_PATTERN.search(market.question))
