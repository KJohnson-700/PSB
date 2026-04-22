"""
Market Scanner Module
Fetches market data from Polymarket GraphQL API (primary) or Gamma REST API (fallback).
"""
import asyncio
import json
import logging
import re
import time
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta, timezone
import aiohttp
import requests
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Market:
    """Represents a Polymarket market"""
    id: str
    question: str
    description: str
    volume: float
    liquidity: float
    yes_price: float
    no_price: float
    spread: float
    end_date: Optional[datetime]
    token_id_yes: str
    token_id_no: str
    group_item_title: str
    # Event slug when fetched via Gamma (e.g. eth-updown-15m-1712345678); empty for bulk feeds.
    slug: str = ""
    
    @property
    def is_binary(self) -> bool:
        """Check if market is binary"""
        return self.yes_price + self.no_price > 0.98
    
    @property
    def is_consensus_yes(self) -> bool:
        """Check if YES is at consensus level"""
        return self.yes_price >= 0.85
    
    @property
    def is_consensus_no(self) -> bool:
        """Check if NO is at consensus level"""
        return self.no_price >= 0.85  # no_price = 1 - yes_price
    
    @property
    def hours_to_expiration(self) -> Optional[float]:
        """Calculate hours until market expiration"""
        if not self.end_date:
            return None
        end = self.end_date
        if end.tzinfo is not None:
            now = datetime.now(timezone.utc)
            if end.tzinfo is not timezone.utc:
                end = end.astimezone(timezone.utc)
        else:
            now = datetime.now()
        delta = end - now
        return delta.total_seconds() / 3600


# Matches BTC / SOL / ETH short-candle "Up or Down" questions (15m / 5m windows).
_CRYPTO_ASSET_UPDOWN_PATTERN = re.compile(
    r"(?:(?:bitcoin|btc)|(?:solana|sol)|(?:ethereum|eth|ether)|(?:ripple|xrp)|(?:hyperliquid|hype))\s+up\s+or\s+down",
    re.IGNORECASE,
)
# Slug prefix from Gamma event slugs (5m / 15m updown).
_CRYPTO_UPDOWN_SLUG_RE = re.compile(
    r"(?:btc|sol|eth|xrp|hype)-updown-(?:5m|15m)-", re.IGNORECASE
)
_HYPE_ALT_UPDOWN_SLUG_RE = re.compile(
    r"(?:hyperliquid-up-or-down|hype-up-or-down)-", re.IGNORECASE
)


def is_crypto_updown_market(market: Market) -> bool:
    """True for crypto up/down candle markets (bitcoin/sol_lag/eth_lag), not price-threshold markets."""
    slug = (market.slug or "").strip()
    if slug and _CRYPTO_UPDOWN_SLUG_RE.search(slug):
        return True
    if slug and _HYPE_ALT_UPDOWN_SLUG_RE.search(slug):
        return True
    if _CRYPTO_ASSET_UPDOWN_PATTERN.search(market.question):
        return True
    q = (market.question or "").lower()
    if "up or down" in q and any(
        tok in q
        for tok in (
            "bitcoin",
            "btc",
            "solana",
            "sol ",
            "ethereum",
            "eth ",
            "ether",
            "xrp",
            "ripple",
            "hyperliquid",
            "hype",
        )
    ):
        return True
    git = (market.group_item_title or "").lower()
    return "up or down" in git and any(
        tok in git
        for tok in ("bitcoin", "btc", "solana", "sol", "ethereum", "eth", "xrp", "hyperliquid", "hype")
    )


class MarketScanner:
    """Scans Polymarket for trading opportunities.

    Primary data source: Gamma REST API (https://gamma-api.polymarket.com)
    Note: graphql.polymarket.com/matic is permanently offline — removed.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        _pm = config.get("polymarket", {}) or {}
        self.min_liquidity = _pm.get("min_liquidity", 10000)
        # Wall-clock cap for bundled Gamma + updown (+ optional HYPE alt) HTTP in a worker thread.
        self._scanner_sync_timeout = float(_pm.get("scanner_sync_timeout_sec", 120))
        self.session: Optional[aiohttp.ClientSession] = None

    def _should_fetch_hype_alt_markets(self) -> bool:
        """HYPE alt slug fetch is slow; default follows strategies.hype_lag.enabled.

        Set polymarket.fetch_hype_alt_markets to true/false to override.
        """
        pm = self.config.get("polymarket") or {}
        if "fetch_hype_alt_markets" in pm:
            return bool(pm.get("fetch_hype_alt_markets"))
        return bool(
            (self.config.get("strategies") or {}).get("hype_lag", {}).get("enabled", False)
        )

    def _sync_network_phase(self) -> Tuple[
        List[Market], List[Market], List[Market], List[Market], int, int
    ]:
        """Blocking HTTP: Gamma list + 15m/5m updown + optional HYPE alt. Runs in a thread."""
        look_ahead_15m, look_ahead_5m = self._resolve_updown_lookahead()
        markets = self._fetch_markets_gamma(limit=200)
        updown: List[Market] = []
        updown_5m: List[Market] = []
        hype_alt: List[Market] = []
        try:
            updown = self.fetch_updown_markets(look_ahead=look_ahead_15m) or []
        except Exception as e:
            logger.error(f"Updown market fetch error: {e}")
        try:
            updown_5m = self.fetch_updown_5m_markets(look_ahead=look_ahead_5m) or []
        except Exception as e:
            logger.error(f"5m updown market fetch error: {e}")
        if self._should_fetch_hype_alt_markets():
            try:
                hype_alt = self.fetch_hype_alt_updown_markets(limit=100) or []
            except Exception as e:
                logger.error(f"HYPE alt updown market fetch error: {e}")
        return markets, updown, updown_5m, hype_alt, look_ahead_15m, look_ahead_5m

    def _empty_scan_result(self, sync_timeout: bool = False) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "look_ahead_15m": 0,
            "look_ahead_5m": 0,
            "updown_15m_count": 0,
            "updown_5m_count": 0,
            "updown_hype_alt_count": 0,
        }
        if sync_timeout:
            meta["sync_phase_timeout"] = True
        return {
            "high_liquidity": [],
            "consensus_yes": [],
            "consensus_no": [],
            "low_spread": [],
            "near_expiration": [],
            "updown": [],
            "updown_5m": [],
            "updown_hype_alt": [],
            "scanner_meta": meta,
        }

    def _resolve_updown_lookahead(self) -> tuple[int, int]:
        """Resolve scanner look-ahead from enabled strategy configs.

        Returns:
            (lookahead_15m, lookahead_5m)
        """
        strategies = self.config.get("strategies", {}) or {}
        keys = ["bitcoin", "sol_lag", "eth_lag", "hype_lag", "xrp_dump_hedge"]

        enabled_cfgs = []
        for key in keys:
            cfg = strategies.get(key, {}) or {}
            if bool(cfg.get("enabled", False)):
                enabled_cfgs.append(cfg)

        cfg_pool = enabled_cfgs if enabled_cfgs else [strategies.get(k, {}) or {} for k in keys]

        look_15m = 4
        look_5m = 8
        for cfg in cfg_pool:
            look_15m = max(look_15m, int(cfg.get("look_ahead_15m", 4)))
            look_5m = max(look_5m, int(cfg.get("look_ahead_5m", 8)))

        look_15m = max(1, min(96, look_15m))
        look_5m = max(1, min(288, look_5m))
        return look_15m, look_5m
        
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def fetch_markets(self, limit: int = 100) -> List[Market]:
        """Fetch active markets from Polymarket via Gamma REST API."""
        return self._fetch_markets_gamma(limit=limit)

    _CLOB_API = "https://clob.polymarket.com"
    _PRICE_CONCURRENCY = 20  # max simultaneous CLOB midpoint requests

    async def fetch_prices(self, token_ids: List[str]) -> Dict[str, float]:
        """Fetch current mid prices for token IDs via CLOB API /midpoint."""
        if not token_ids:
            return {}

        session = await self._get_session()
        sem = asyncio.Semaphore(self._PRICE_CONCURRENCY)

        async def _get_mid(token_id: str):
            async with sem:
                try:
                    async with session.get(
                        f"{self._CLOB_API}/midpoint",
                        params={"token_id": token_id},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            mid = float(data.get("mid", 0) or 0)
                            if mid > 0:
                                return token_id, mid
                except Exception:
                    pass
                return token_id, None

        try:
            results = await asyncio.gather(*[_get_mid(tid) for tid in token_ids])
            return {tid: price for tid, price in results if price is not None}
        except Exception as e:
            logger.error(f"Error fetching prices: {e}")
            return {}
    
    def _parse_markets(self, markets_data: List[Dict]) -> List[Market]:
        """Parse raw market data into Market objects"""
        markets = []
        
        for m in markets_data:
            try:
                # Extract token IDs (YES and NO)
                clob_token_ids = m.get('clobTokenIds', [])
                if len(clob_token_ids) < 2:
                    continue
                    
                token_id_yes = clob_token_ids[0]
                token_id_no = clob_token_ids[1]
                
                # Parse end date
                end_date = None
                if m.get('endDate'):
                    try:
                        end_date = datetime.fromisoformat(m['endDate'].replace('Z', '+00:00'))
                    except:
                        pass
                
                market = Market(
                    id=m['id'],
                    question=m.get('question', ''),
                    description=m.get('description', ''),
                    volume=float(m.get('volume', 0)),
                    liquidity=float(m.get('liquidity', 0)),
                    yes_price=0.5,  # Will be updated with real prices
                    no_price=0.5,   # Will be updated with real prices
                    spread=0.0,      # Will be calculated
                    end_date=end_date,
                    token_id_yes=token_id_yes,
                    token_id_no=token_id_no,
                    group_item_title=m.get('groupItemTitle', ''),
                    slug=str(m.get("slug") or ""),
                )
                
                # Filter by liquidity
                if market.liquidity >= self.min_liquidity:
                    markets.append(market)
                    
            except Exception as e:
                logger.warning(f"Error parsing market: {e}")
                continue
        
        return markets
    
    async def update_market_prices(self, markets: List[Market]) -> List[Market]:
        """Update markets with current prices"""
        if not markets:
            return []
        
        # Collect all token IDs
        token_ids = []
        for market in markets:
            token_ids.extend([market.token_id_yes, market.token_id_no])
        
        # Fetch prices
        prices = await self.fetch_prices(token_ids)
        
        # Update markets
        for market in markets:
            yes_price = prices.get(market.token_id_yes, 0.5)
            no_price = prices.get(market.token_id_no, 0.5)
            
            market.yes_price = yes_price
            market.no_price = no_price
            market.spread = abs(yes_price - (1 - no_price))
        
        return markets
    
    def _fetch_markets_gamma(self, limit: int = 100) -> List[Market]:
        """Fallback: fetch markets from Gamma REST API when GraphQL is unavailable."""
        GAMMA_API = "https://gamma-api.polymarket.com"
        markets = []
        offset = 0
        try:
            while len(markets) < limit:
                params = {"limit": min(limit - len(markets), 100), "offset": offset,
                          "active": "true", "closed": "false"}
                resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=15)
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                for gm in batch:
                    try:
                        vol = float(gm.get("volume", 0) or 0)
                        liq = float(gm.get("liquidity", 0) or 0)
                        if vol < self.min_liquidity:
                            continue
                        outcomes = json.loads(gm.get("outcomePrices", "[]"))
                        yes_price = float(outcomes[0]) if outcomes else 0.5
                        no_price = float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price
                        tokens = json.loads(gm.get("clobTokenIds", "[]"))
                        token_yes = tokens[0] if tokens else ""
                        token_no = tokens[1] if len(tokens) > 1 else ""
                        end_str = gm.get("endDate") or gm.get("end_date_iso")
                        end_date = None
                        if end_str:
                            try:
                                end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00")).replace(tzinfo=None)
                            except (ValueError, TypeError):
                                pass
                        spread_val = float(gm.get("spread", 0.02) or 0.02)
                        m = Market(
                            id=gm.get("id", ""), question=gm.get("question", ""),
                            description=(gm.get("description", "") or "")[:200],
                            volume=vol, liquidity=liq,
                            yes_price=yes_price, no_price=no_price, spread=spread_val,
                            end_date=end_date, token_id_yes=token_yes, token_id_no=token_no,
                            group_item_title=gm.get("groupItemTitle", ""),
                            slug=str(gm.get("slug") or ""),
                        )
                        if 0.01 < m.yes_price < 0.99:
                            markets.append(m)
                    except Exception:
                        continue
                offset += len(batch)
                if len(batch) < params["limit"]:
                    break
        except Exception as e:
            logger.error(f"Gamma API fallback error: {e}")
        logger.info(f"Gamma API fetched {len(markets)} markets")
        return markets

    # ──────────────────────────────────────────────────────────────
    # 15-minute Up/Down market fetcher (BTC & SOL)
    # ──────────────────────────────────────────────────────────────
    GAMMA_API_BASE = "https://gamma-api.polymarket.com"

    def fetch_updown_markets(self, look_ahead: int = 4) -> List[Market]:
        """Fetch current + upcoming 15-minute BTC & SOL Up/Down markets.

        These markets live in a separate event slug system:
            btc-updown-15m-{unix_ts}
            sol-updown-15m-{unix_ts}

        Args:
            look_ahead: number of future 15-min windows to fetch (default 4 = 1 hour)

        Returns:
            List of Market objects for tradeable updown windows.
        """
        now = int(time.time())
        current_window = now - (now % 900)
        markets: List[Market] = []

        for offset in range(0, look_ahead + 1):
            ts = current_window + (offset * 900)
            for prefix in [
                "btc-updown-15m",
                "sol-updown-15m",
                "eth-updown-15m",
                "xrp-updown-15m",
                "hype-updown-15m",
            ]:
                slug = f"{prefix}-{ts}"
                try:
                    resp = requests.get(
                        f"{self.GAMMA_API_BASE}/events",
                        params={"slug": slug},
                        timeout=8,
                    )
                    if resp.status_code != 200:
                        continue
                    events = resp.json()
                    if not events:
                        continue
                    event = events[0]
                    for gm in event.get("markets", []):
                        try:
                            mid = gm.get("id", "")
                            question = gm.get("question", "")
                            desc = (gm.get("description", "") or "")[:300]
                            vol = float(gm.get("volume", 0) or 0)
                            liq = float(gm.get("liquidity", 0) or 0)
                            outcomes = json.loads(gm.get("outcomePrices", "[]"))
                            yes_price = float(outcomes[0]) if outcomes else 0.5
                            no_price = float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price
                            tokens = json.loads(gm.get("clobTokenIds", "[]"))
                            token_yes = tokens[0] if tokens else ""
                            token_no = tokens[1] if len(tokens) > 1 else ""
                            end_str = gm.get("endDate") or gm.get("end_date_iso")
                            end_date = None
                            if end_str:
                                try:
                                    end_date = datetime.fromisoformat(
                                        end_str.replace("Z", "+00:00")
                                    ).replace(tzinfo=None)
                                except (ValueError, TypeError):
                                    pass

                            # Skip already-resolved (prices at 0/1)
                            if yes_price <= 0.01 or yes_price >= 0.99:
                                continue

                            m = Market(
                                id=mid,
                                question=question,
                                description=desc,
                                volume=vol,
                                liquidity=liq,
                                yes_price=yes_price,
                                no_price=no_price,
                                spread=abs(yes_price - no_price),
                                end_date=end_date,
                                token_id_yes=token_yes,
                                token_id_no=token_no,
                                group_item_title=gm.get("groupItemTitle", ""),
                                slug=slug,
                            )
                            markets.append(m)
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug(f"Failed to fetch updown slug {slug}: {e}")
                    continue

        if markets:
            def _is_eth_mkt(m: Market) -> bool:
                q = m.question.lower()
                return "ethereum" in q or "ether" in q or bool(re.search(r"\beth\b", q))

            def _is_hype_mkt(m: Market) -> bool:
                q = m.question.lower()
                return "hyperliquid" in q or bool(re.search(r"\bhype\b", q))

            logger.info(
                f"Fetched {len(markets)} 15m updown markets "
                f"(BTC: {sum(1 for m in markets if 'bitcoin' in m.question.lower())}, "
                f"SOL: {sum(1 for m in markets if 'solana' in m.question.lower())}, "
                f"ETH: {sum(1 for m in markets if _is_eth_mkt(m))}, "
                f"XRP: {sum(1 for m in markets if 'xrp' in m.question.lower() or 'ripple' in m.question.lower())}, "
                f"HYPE: {sum(1 for m in markets if _is_hype_mkt(m))})"
            )
        return markets

    def fetch_updown_5m_markets(self, look_ahead: int = 8) -> List[Market]:
        """Fetch current + upcoming 5-minute BTC & SOL Up/Down markets.

        These markets live in a separate event slug system:
            btc-updown-5m-{unix_ts}
            sol-updown-5m-{unix_ts}

        Args:
            look_ahead: number of future 5-min windows to fetch (default 8 = 40 minutes)

        Returns:
            List of Market objects for tradeable 5m updown windows.
        """
        now = int(time.time())
        current_window = now - (now % 300)
        markets: List[Market] = []

        for offset in range(0, look_ahead + 1):
            ts = current_window + (offset * 300)
            for prefix in ["btc-updown-5m", "sol-updown-5m", "eth-updown-5m", "xrp-updown-5m", "hype-updown-5m"]:
                slug = f"{prefix}-{ts}"
                try:
                    resp = requests.get(
                        f"{self.GAMMA_API_BASE}/events",
                        params={"slug": slug},
                        timeout=8,
                    )
                    if resp.status_code != 200:
                        continue
                    events = resp.json()
                    if not events:
                        continue
                    event = events[0]
                    for gm in event.get("markets", []):
                        try:
                            mid = gm.get("id", "")
                            question = gm.get("question", "")
                            desc = (gm.get("description", "") or "")[:300]
                            vol = float(gm.get("volume", 0) or 0)
                            liq = float(gm.get("liquidity", 0) or 0)
                            outcomes = json.loads(gm.get("outcomePrices", "[]"))
                            yes_price = float(outcomes[0]) if outcomes else 0.5
                            no_price = float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price
                            tokens = json.loads(gm.get("clobTokenIds", "[]"))
                            token_yes = tokens[0] if tokens else ""
                            token_no = tokens[1] if len(tokens) > 1 else ""
                            end_str = gm.get("endDate") or gm.get("end_date_iso")
                            end_date = None
                            if end_str:
                                try:
                                    end_date = datetime.fromisoformat(
                                        end_str.replace("Z", "+00:00")
                                    ).replace(tzinfo=None)
                                except (ValueError, TypeError):
                                    pass

                            # Skip already-resolved (prices at 0/1)
                            if yes_price <= 0.01 or yes_price >= 0.99:
                                continue

                            m = Market(
                                id=mid,
                                question=question,
                                description=desc,
                                volume=vol,
                                liquidity=liq,
                                yes_price=yes_price,
                                no_price=no_price,
                                spread=abs(yes_price - no_price),
                                end_date=end_date,
                                token_id_yes=token_yes,
                                token_id_no=token_no,
                                group_item_title=gm.get("groupItemTitle", ""),
                                slug=slug,
                            )
                            markets.append(m)
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug(f"Failed to fetch 5m updown slug {slug}: {e}")
                    continue

        if markets:
            def _is_eth_mkt_5(m: Market) -> bool:
                q = m.question.lower()
                return "ethereum" in q or "ether" in q or bool(re.search(r"\beth\b", q))

            def _is_hype_mkt_5(m: Market) -> bool:
                q = m.question.lower()
                return "hyperliquid" in q or bool(re.search(r"\bhype\b", q))

            logger.info(
                f"Fetched {len(markets)} 5m updown markets "
                f"(BTC: {sum(1 for m in markets if 'bitcoin' in m.question.lower())}, "
                f"SOL: {sum(1 for m in markets if 'solana' in m.question.lower())}, "
                f"ETH: {sum(1 for m in markets if _is_eth_mkt_5(m))}, "
                f"XRP: {sum(1 for m in markets if 'xrp' in m.question.lower() or 'ripple' in m.question.lower())}, "
                f"HYPE: {sum(1 for m in markets if _is_hype_mkt_5(m))})"
            )
        return markets

    def fetch_hype_alt_updown_markets(self, limit: int = 100) -> List[Market]:
        """Fetch Hyperliquid/HYPE up-or-down markets from non timestamp slugs.

        Handles event slugs like:
          - hyperliquid-up-or-down-...
          - hype-up-or-down-...
        """
        markets: List[Market] = []
        offset = 0
        while len(markets) < limit:
            try:
                params = {
                    "limit": min(100, limit - len(markets)),
                    "offset": offset,
                    "active": "true",
                    "closed": "false",
                }
                resp = requests.get(f"{self.GAMMA_API_BASE}/events", params=params, timeout=10)
                if resp.status_code != 200:
                    break
                events = resp.json() or []
                if not events:
                    break
                for event in events:
                    slug = str(event.get("slug") or "")
                    if not _HYPE_ALT_UPDOWN_SLUG_RE.search(slug):
                        continue
                    for gm in event.get("markets", []):
                        try:
                            mid = gm.get("id", "")
                            question = gm.get("question", "")
                            if "up or down" not in question.lower():
                                continue
                            desc = (gm.get("description", "") or "")[:300]
                            vol = float(gm.get("volume", 0) or 0)
                            liq = float(gm.get("liquidity", 0) or 0)
                            outcomes = json.loads(gm.get("outcomePrices", "[]"))
                            yes_price = float(outcomes[0]) if outcomes else 0.5
                            no_price = float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price
                            tokens = json.loads(gm.get("clobTokenIds", "[]"))
                            token_yes = tokens[0] if tokens else ""
                            token_no = tokens[1] if len(tokens) > 1 else ""
                            end_str = gm.get("endDate") or gm.get("end_date_iso")
                            end_date = None
                            if end_str:
                                try:
                                    end_date = datetime.fromisoformat(
                                        end_str.replace("Z", "+00:00")
                                    ).replace(tzinfo=None)
                                except (ValueError, TypeError):
                                    pass
                            if yes_price <= 0.01 or yes_price >= 0.99:
                                continue
                            markets.append(
                                Market(
                                    id=mid,
                                    question=question,
                                    description=desc,
                                    volume=vol,
                                    liquidity=liq,
                                    yes_price=yes_price,
                                    no_price=no_price,
                                    spread=abs(yes_price - no_price),
                                    end_date=end_date,
                                    token_id_yes=token_yes,
                                    token_id_no=token_no,
                                    group_item_title=gm.get("groupItemTitle", ""),
                                    slug=slug,
                                )
                            )
                        except Exception:
                            continue
                offset += len(events)
                if len(events) < params["limit"]:
                    break
            except Exception as e:
                logger.debug(f"Failed to fetch alternate HYPE up/down events: {e}")
                break

        if markets:
            logger.info(f"Fetched {len(markets)} Hyperliquid/HYPE alt up/down markets")
        return markets

    async def scan_for_opportunities(self) -> Dict[str, Any]:
        """Scan for different types of opportunities.

        Sync HTTP (Gamma + updown + optional HYPE alt) runs in a worker thread with a
        timeout so the asyncio event loop is not blocked for minutes on slow APIs.
        """
        t_scan_start = time.perf_counter()
        logger.info("Scanner: sync network phase (thread) starting")
        try:
            (
                markets,
                updown,
                updown_5m,
                hype_alt,
                look_ahead_15m,
                look_ahead_5m,
            ) = await asyncio.wait_for(
                asyncio.to_thread(self._sync_network_phase),
                timeout=self._scanner_sync_timeout,
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((time.perf_counter() - t_scan_start) * 1000)
            logger.error(
                "Scanner: sync network phase timed out after %dms (limit=%.1fs) — empty scan",
                elapsed_ms,
                self._scanner_sync_timeout,
            )
            return self._empty_scan_result(sync_timeout=True)

        sync_ms = int((time.perf_counter() - t_scan_start) * 1000)
        logger.info("Scanner: sync network phase finished in %dms", sync_ms)

        if markets:
            markets = await self.update_market_prices(markets)

        opportunities: Dict[str, Any] = {
            "high_liquidity": [],
            "consensus_yes": [],
            "consensus_no": [],
            "low_spread": [],
            "near_expiration": [],
        }

        for market in markets:
            if market.liquidity >= self.min_liquidity or market.volume >= self.min_liquidity:
                opportunities["high_liquidity"].append(market)
            if market.is_consensus_yes:
                opportunities["consensus_yes"].append(market)
            if market.is_consensus_no:
                opportunities["consensus_no"].append(market)
            if market.spread < 0.03:
                opportunities["low_spread"].append(market)
            hours = market.hours_to_expiration
            if hours and hours < 48:
                opportunities["near_expiration"].append(market)

        if updown:
            opportunities["high_liquidity"].extend(updown)
            opportunities["updown"] = updown
        else:
            opportunities["updown"] = []

        if updown_5m:
            opportunities["high_liquidity"].extend(updown_5m)
            opportunities["updown_5m"] = updown_5m
        else:
            opportunities["updown_5m"] = []

        if hype_alt:
            opportunities["high_liquidity"].extend(hype_alt)
            opportunities["updown_hype_alt"] = hype_alt
        else:
            opportunities["updown_hype_alt"] = []

        opportunities["scanner_meta"] = {
            "look_ahead_15m": look_ahead_15m,
            "look_ahead_5m": look_ahead_5m,
            "updown_15m_count": len(opportunities.get("updown", [])),
            "updown_5m_count": len(opportunities.get("updown_5m", [])),
            "updown_hype_alt_count": len(opportunities.get("updown_hype_alt", [])),
        }

        logger.info(
            f"Found {len(opportunities['consensus_yes'])} consensus YES opportunities"
        )
        logger.info(
            f"Found {len(opportunities['consensus_no'])} consensus NO opportunities"
        )

        total_ms = int((time.perf_counter() - t_scan_start) * 1000)
        logger.info(
            "Scanner: scan_for_opportunities complete in %dms (includes price hydrate)",
            total_ms,
        )

        return opportunities
    
    async def close(self):
        """Close HTTP session"""
        if self.session and not self.session.closed:
            await self.session.close()
