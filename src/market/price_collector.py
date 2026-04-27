import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any

from src.market.scanner import Market

logger = logging.getLogger(__name__)


class PriceCollector:
    """Append live market snapshots to JSONL for later backtesting."""

    def __init__(self, config: Dict[str, Any]):
        cfg = ((config.get("data_collection") or {}).get("weather") or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.interval_sec = int(cfg.get("interval_sec", 300))
        rel_path = cfg.get("path", "data/market_prices/weather_prices.jsonl")
        self.path = Path(rel_path)
        if not self.path.is_absolute():
            self.path = Path(__file__).resolve().parent.parent.parent / rel_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_write_ts = 0.0

    def maybe_collect(self, markets: List[Market]) -> None:
        if not self.enabled or not markets:
            return
        now = time.time()
        if now - self._last_write_ts < self.interval_sec:
            return
        self._last_write_ts = now
        ts = datetime.now(timezone.utc).isoformat()
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                for market in markets:
                    payload = {
                        "ts": ts,
                        "market_id": market.id,
                        "slug": market.slug,
                        "question": market.question,
                        "description": market.description,
                        "group_item_title": market.group_item_title,
                        "yes_price": market.yes_price,
                        "no_price": market.no_price,
                        "spread": market.spread,
                        "volume": market.volume,
                        "liquidity": market.liquidity,
                        "end_date": market.end_date.isoformat() if market.end_date else None,
                        "token_id_yes": market.token_id_yes,
                        "token_id_no": market.token_id_no,
                    }
                    fh.write(json.dumps(payload) + "\n")
            logger.info("PriceCollector: wrote %d weather market snapshots", len(markets))
        except Exception as e:
            logger.error("PriceCollector write failed: %s", e)
