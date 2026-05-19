"""
BNB Macro Strategy.

BNB is its own alt leg: BNB spot/HTF/oracle data drives primary direction.
BTC is secondary context only. This reuses the shared alt-macro base exactly
like XRP/HYPE so rejected-candidate logging and calibration stay comparable.
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

BNB_PATTERNS = [
    re.compile(r"\bbnb\b", re.IGNORECASE),
    re.compile(r"\bbinance\s+coin\b", re.IGNORECASE),
]
BNB_UPDOWN_PATTERN = re.compile(
    r"(?:bnb|binance\s+coin)\s+up\s+or\s+down", re.IGNORECASE
)
BNB_UPDOWN_SLUG_PREFIXES = (
    "bnb-updown-",
    "bnb-up-or-down-",
    "binance-coin-up-or-down-",
)
NON_BNB_ASSET_TERMS = (
    "bitcoin",
    "btc",
    "solana",
    "ethereum",
    "ether",
    "xrp",
    "ripple",
    "hyperliquid",
    "hype",
    "doge",
    "dogecoin",
)


class BNBMacroStrategy(SolMacroStrategy):
    """BNB macro strategy with BNB-first direction and BNBUSDT data."""

    def _build_alt_service(self) -> SOLBTCService:
        return SOLBTCService(
            alt_symbol="BNBUSDT",
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
        ai_broker=None,
    ):
        super().__init__(
            config,
            ai_agent,
            position_sizer,
            kelly_sizer,
            exposure_manager,
            ai_broker=ai_broker,
        )
        self.config = config.get("strategies", {}).get("bnb_macro", {})
        self.enabled = resolve_enabled_flag("bnb_macro", self.config, logger=logger)
        self._apply_strategy_config(rebuild_service=True)
        self._signal_strategy_name = "bnb_macro"

    def _alt_asset_code(self) -> str:
        return "bnb"

    def _is_solana_market(self, market: Market) -> bool:
        slug = (getattr(market, "slug", None) or "").lower()
        text = (
            f"{market.question} "
            f"{getattr(market, 'description', '') or ''} "
            f"{getattr(market, 'group_item_title', '') or ''} "
            f"{slug}"
        ).lower()
        has_bnb = any(p.search(text) for p in BNB_PATTERNS) or slug.startswith(
            BNB_UPDOWN_SLUG_PREFIXES
        )
        if not has_bnb:
            return False
        if any(term in text for term in NON_BNB_ASSET_TERMS):
            primary = (
                f"{market.question} "
                f"{getattr(market, 'group_item_title', '') or ''} "
                f"{slug}"
            ).lower()
            if not any(p.search(primary) for p in BNB_PATTERNS) and not slug.startswith(
                BNB_UPDOWN_SLUG_PREFIXES
            ):
                return False
        return True

    def _is_updown_market(self, market: Market) -> bool:
        slug = (getattr(market, "slug", None) or "").lower()
        if slug.startswith(BNB_UPDOWN_SLUG_PREFIXES):
            return True
        text = f"{market.question} {getattr(market, 'group_item_title', '') or ''}"
        return bool(BNB_UPDOWN_PATTERN.search(text))
