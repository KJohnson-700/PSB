"""
XRP Macro Strategy.

XRP is its own alt leg: XRP spot/HTF/oracle data drives primary direction.
BTC is secondary context only. The class still reuses the shared alt-macro base
until that base is renamed/split, so keep XRP-specific overrides explicit here.
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
XRP_UPDOWN_SLUG_PREFIXES = ("xrp-updown-", "xrp-up-or-down-", "ripple-up-or-down-")
NON_XRP_ASSET_TERMS = ("bitcoin", "btc", "solana", "ethereum", "ether", "hyperliquid", "hype")


class XRPMacroStrategy(SolMacroStrategy):
    """XRP macro strategy with XRP-first direction and XRPUSDT data."""

    def _build_alt_service(self) -> SOLBTCService:
        """Build the XRP data/oracle service; never use SOL spot for this lane."""
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

    def _alt_asset_code(self) -> str:
        """Hard-code XRP identity so shared-base naming cannot leak SOL labels."""
        return "xrp"

    def _is_solana_market(self, market: Market) -> bool:
        """Detect XRP (not BTC-only) prediction markets."""
        _git = getattr(market, "group_item_title", "") or ""
        _slug = getattr(market, "slug", None) or ""
        _desc = getattr(market, "description", "") or ""
        text = (
            f"{market.question} {_desc} "
            f"{_git} {_slug}"
        ).lower()
        has_xrp = any(p.search(text) for p in XRP_PATTERNS) or _slug.lower().startswith(XRP_UPDOWN_SLUG_PREFIXES)
        if not has_xrp:
            return False
        if any(term in text for term in NON_XRP_ASSET_TERMS):
            primary = f"{market.question} {_git} {_slug}".lower()
            if not any(p.search(primary) for p in XRP_PATTERNS):
                return False
        return True

    def _is_updown_market(self, market: Market) -> bool:
        """Detect XRP Up or Down markets (15m / 5m)."""
        slug = (getattr(market, "slug", None) or "").lower()
        if slug.startswith(XRP_UPDOWN_SLUG_PREFIXES):
            return True
        text = f"{market.question} {getattr(market, 'group_item_title', '') or ''}"
        return bool(XRP_UPDOWN_PATTERN.search(text))
