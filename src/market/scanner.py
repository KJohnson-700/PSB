"""
Market Scanner Module
Fetches market data from Polymarket GraphQL API (primary) or Gamma REST API (fallback).
"""
import asyncio
import json
import logging
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from threading import Lock
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import aiohttp
import requests
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

_WEATHER_MARKET_HINT_RE = re.compile(
    r"\b(rain|snow|precipitation|temperature|temp|weather|forecast|degrees?|"
    r"fahrenheit|celsius|humid|storm|flood|drought|sunshine|sunny|cloudy|wind|hail|thunderstorm)\b",
    re.IGNORECASE,
)
_WEATHER_TEMP_SLUG_RE = re.compile(
    r"^highest-temperature-in-([a-z0-9-]+)-on-([a-z]{3})-(\d{1,2})-(\d{4})$",
    re.IGNORECASE,
)
_WEATHER_TEMP_TEXT_RE = re.compile(
    r"\bhighest\s+temperature\s+in\s+[a-z0-9 .'-]+\s+on\s+"
    r"(?:[a-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,)?\s+\d{4}|[a-z]{3}\.?\s+\d{1,2}(?:,)?\s+\d{4})\b",
    re.IGNORECASE,
)
_WEATHER_PRECIP_SLUG_RE = re.compile(
    r"(?:\bprecipitation\b|\b(?:rain|snow)\b.*\b(?:mm|cm|inch|inches)\b|"
    r"\b(?:mm|cm|inch|inches)\b.*\b(?:rain|snow|precipitation)\b)",
    re.IGNORECASE,
)
_WEATHER_PRECIP_TEXT_RE = re.compile(
    r"\b(?:rain|snow|precipitation)\b.*\b(?:mm|cm|inch|inches)\b|"
    r"\b(?:mm|cm|inch|inches)\b.*\b(?:rain|snow|precipitation)\b",
    re.IGNORECASE,
)
_WEATHER_TITLE_HINT_RE = re.compile(
    r"\b(highest\s+temperature|precipitation|rain|snow)\b|"
    r"\b(?:mm|inch|inches|cm|°f|°c)\b",
    re.IGNORECASE,
)


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
_UPDOWN_SLUG_DATE_RE = re.compile(
    r"up-or-down-([a-z]+)-(\d{1,2})-(\d{4})-", re.IGNORECASE
)
_UPDOWN_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*(AM|PM)?\s*[–-]\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)


def is_crypto_updown_market(market: Market) -> bool:
    """True for crypto up/down candle markets (bitcoin/sol_macro/eth_macro), not price-threshold markets."""
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


def _parse_updown_market_end_from_text(
    *, slug: str, question: str, group_item_title: str
) -> Optional[datetime]:
    """Parse the individual up/down candle end time from Gamma text.

    Gamma event markets can expose an event-level ``endDate``. For hourly grouped
    up/down events that is too late for 5m/15m entries, so prefer the market's
    own time range when present.
    """
    slug_match = _UPDOWN_SLUG_DATE_RE.search(slug or "")
    if not slug_match:
        return None
    month_name, day_s, year_s = slug_match.groups()
    try:
        month = datetime.strptime(month_name[:3], "%b").month
        day = int(day_s)
        year = int(year_s)
    except (TypeError, ValueError):
        return None

    text = f"{question or ''} {group_item_title or ''}"
    time_match = _UPDOWN_TIME_RANGE_RE.search(text)
    if not time_match:
        return None
    h1, m1, p1, h2, m2, p2 = time_match.groups()
    start_period = (p1 or p2 or "").upper()
    end_period = (p2 or p1 or "").upper()
    try:
        start_hour = int(h1)
        start_minute = int(m1)
        end_hour = int(h2)
        end_minute = int(m2)
    except (TypeError, ValueError):
        return None

    def _to_24h(hour: int, period: str) -> int:
        if period == "PM" and hour != 12:
            return hour + 12
        if period == "AM" and hour == 12:
            return 0
        return hour

    start_et = datetime(
        year,
        month,
        day,
        _to_24h(start_hour, start_period),
        start_minute,
        tzinfo=_ET,
    )
    end_et = datetime(
        year,
        month,
        day,
        _to_24h(end_hour, end_period),
        end_minute,
        tzinfo=_ET,
    )
    if end_et <= start_et:
        end_et += timedelta(days=1)
    return end_et.astimezone(timezone.utc)


class MarketScanner:
    """Scans Polymarket for trading opportunities.

    Primary data source: Gamma REST API (https://gamma-api.polymarket.com)
    Note: graphql.polymarket.com/matic is permanently offline — removed.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._reload_config_fields()
        self.session: Optional[aiohttp.ClientSession] = None
        self._background_fetch_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="scanner-bg"
        )
        self._slow_fetch_lock = Lock()
        self._weather_refresh_future: Optional[Future[List[Market]]] = None
        self._weather_cache: List[Market] = []
        self._weather_cache_updated_at: Optional[datetime] = None

    def _reload_config_fields(self) -> None:
        """Refresh derived thresholds from the shared config dict."""
        _pm = self.config.get("polymarket", {}) or {}
        _tr = self.config.get("trading", {}) or {}
        _wx = (self.config.get("strategies", {}) or {}).get("weather", {}) or {}
        self.min_liquidity = _pm.get("min_liquidity", 10000)
        self.weather_min_liquidity = float(
            _wx.get("min_liquidity", _wx.get("min_volume", self.min_liquidity))
        )
        self.weather_scan_limit = max(20, int(_wx.get("scan_limit", 120)))
        self._cycle_interval_sec = float(_tr.get("cycle_interval_sec", 120))
        configured_timeout = float(_pm.get("scanner_sync_timeout_sec", 120))
        # Never let a sync timeout outrun the trading cadence. Leave a small gap so
        # one slow cycle does not overlap the next and blind the bot indefinitely.
        hard_cap_timeout = max(20.0, self._cycle_interval_sec - 15.0)
        self._scanner_sync_timeout = min(configured_timeout, hard_cap_timeout)
        if configured_timeout > self._scanner_sync_timeout:
            logger.warning(
                "Scanner timeout capped from %.1fs to %.1fs to stay below cycle interval %.1fs",
                configured_timeout,
                self._scanner_sync_timeout,
                self._cycle_interval_sec,
            )

    def reload_from_config(self, config: Dict[str, Any]) -> None:
        """Apply updated config to the live scanner without replacing caches/pool."""
        self.config = config
        self._reload_config_fields()

    def _market_liquidity_threshold(self, question: str, description: str = "") -> float:
        text = f"{question or ''} {description or ''}"
        if _WEATHER_MARKET_HINT_RE.search(text):
            return self.weather_min_liquidity
        return self.min_liquidity

    @staticmethod
    def _is_dedicated_weather_candidate(market: Market) -> bool:
        slug = (market.slug or "").lower()
        title_text = f"{market.question} {market.group_item_title}".lower()
        if _WEATHER_TEMP_SLUG_RE.match(slug):
            return True
        if _WEATHER_TEMP_TEXT_RE.search(title_text):
            return True
        if _WEATHER_PRECIP_SLUG_RE.search(slug):
            return True
        if _WEATHER_PRECIP_TEXT_RE.search(title_text):
            return True
        return bool(_WEATHER_TITLE_HINT_RE.search(f"{slug} {title_text}"))

    def _should_fetch_hype_alt_markets(self) -> bool:
        """HYPE alt slug fetch is slow; default follows strategies.hype_macro.enabled.

        Set polymarket.fetch_hype_alt_markets to true/false to override.
        """
        pm = self.config.get("polymarket") or {}
        if "fetch_hype_alt_markets" in pm:
            return bool(pm.get("fetch_hype_alt_markets"))
        return bool(
            (self.config.get("strategies") or {}).get("hype_macro", {}).get("enabled", False)
        )

    def _should_fetch_weather_markets(self) -> bool:
        pm = self.config.get("polymarket") or {}
        if "fetch_weather_markets" in pm:
            return bool(pm.get("fetch_weather_markets"))
        return bool(
            (self.config.get("strategies") or {}).get("weather", {}).get("enabled", False)
        )

    def _sync_network_phase(self) -> Tuple[
        List[Market], List[Market], List[Market], List[Market], List[Market], int, int
    ]:
        """Blocking HTTP: Gamma list + 15m/5m updown + optional HYPE alt. Runs in a thread.

        Fetches run in parallel via ThreadPoolExecutor so the longest one (HYPE alt)
        doesn't add to the wall-clock time of the other fetches.
        """
        look_ahead_15m, look_ahead_5m = self._resolve_updown_lookahead()
        fetch_hype = self._should_fetch_hype_alt_markets()
        fetch_weather = self._should_fetch_weather_markets()
        weather_snapshot = self._get_weather_market_snapshot() if fetch_weather else []

        tasks = {
            "gamma": lambda: self._fetch_markets_gamma(limit=200),
            "updown": lambda: self.fetch_updown_markets(look_ahead=look_ahead_15m),
            "updown_5m": lambda: self.fetch_updown_5m_markets(look_ahead=look_ahead_5m),
        }
        if fetch_hype:
            tasks["hype_alt"] = lambda: self.fetch_hype_alt_updown_markets(limit=100)

        results: Dict[str, Any] = {}
        pool = ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="scanner")
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        try:
            for future in as_completed(futures, timeout=self._scanner_sync_timeout):
                name = futures[future]
                try:
                    results[name] = future.result() or []
                except Exception as e:
                    logger.error(f"{name} fetch error: {e}")
                    results[name] = []
        except FuturesTimeoutError:
            unfinished = [name for future, name in futures.items() if not future.done()]
            logger.warning(
                "Scanner: partial sync timeout after %.1fs; returning completed sources only. unfinished=%s",
                self._scanner_sync_timeout,
                unfinished,
            )
        finally:
            for future, name in futures.items():
                if future.done():
                    continue
                future.cancel()
                results.setdefault(name, [])
            pool.shutdown(wait=False, cancel_futures=True)

        return (
            results.get("gamma", []),
            results.get("updown", []),
            results.get("updown_5m", []),
            results.get("hype_alt", []),
            weather_snapshot,
            look_ahead_15m,
            look_ahead_5m,
        )

    def _get_weather_market_snapshot(self) -> List[Market]:
        """Return the last completed weather snapshot and keep refresh running in background.

        Weather discovery can outrun the scanner sync deadline. We therefore avoid putting it
        on the critical path each cycle and instead refresh it opportunistically.
        """
        self._harvest_weather_refresh_result()

        with self._slow_fetch_lock:
            future = self._weather_refresh_future
            if future is None:
                self._weather_refresh_future = self._background_fetch_pool.submit(
                    self._run_weather_refresh
                )
            cached = list(self._weather_cache)

        return cached

    def _run_weather_refresh(self) -> List[Market]:
        markets = self.fetch_weather_markets(limit=self.weather_scan_limit) or []
        logger.info(
            "Scanner: weather background refresh fetched %d dedicated markets",
            len(markets),
        )
        return markets

    def _harvest_weather_refresh_result(self) -> None:
        with self._slow_fetch_lock:
            future = self._weather_refresh_future

        if future is None or not future.done():
            return

        try:
            markets = future.result() or []
        except Exception as e:
            logger.error("weather background refresh error: %s", e)
            markets = []

        with self._slow_fetch_lock:
            self._weather_cache = list(markets)
            self._weather_cache_updated_at = datetime.now(timezone.utc)
            self._weather_refresh_future = None

    def _empty_scan_result(self, sync_timeout: bool = False) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "look_ahead_15m": 0,
            "look_ahead_5m": 0,
            "updown_15m_count": 0,
            "updown_5m_count": 0,
            "updown_hype_alt_count": 0,
            "weather_market_count": 0,
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
            "weather": [],
            "scanner_meta": meta,
        }

    def _resolve_updown_lookahead(self) -> tuple[int, int]:
        """Resolve scanner look-ahead from enabled strategy configs.

        Returns:
            (lookahead_15m, lookahead_5m)
        """
        strategies = self.config.get("strategies", {}) or {}
        keys = ["bitcoin", "sol_macro", "eth_macro", "hype_macro", "xrp_macro"]

        enabled_cfgs = []
        for key in keys:
            cfg = strategies.get(key, {}) or {}
            if bool(cfg.get("enabled", False)):
                enabled_cfgs.append(cfg)

        cfg_pool = enabled_cfgs if enabled_cfgs else [strategies.get(k, {}) or {} for k in keys]

        look_15m = 8
        look_5m = 8
        for cfg in cfg_pool:
            look_15m = max(look_15m, int(cfg.get("look_ahead_15m", 8)))
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
                threshold = self._market_liquidity_threshold(
                    market.question, market.description
                )
                if market.liquidity >= threshold or market.volume >= threshold:
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
            if market.spread <= 0:
                # Mid prices only reveal convergence, not true order-book spread.
                market.spread = max(0.0, 1.0 - (yes_price + no_price))
        
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
                        threshold = self._market_liquidity_threshold(
                            gm.get("question", ""),
                            gm.get("description", "") or "",
                        )
                        if vol < threshold and liq < threshold:
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
                                end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone(timezone.utc)
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

    _HUMAN_UPDOWN_PREFIXES = (
        "bitcoin",
        "solana",
        "ethereum",
        "xrp",
        "hyperliquid",
        "hype",
    )

    @staticmethod
    def _build_human_updown_event_slug(asset_prefix: str, when_utc: datetime) -> str:
        """Build Gamma's human-readable event slug in America/New_York time.

        Example:
            bitcoin-up-or-down-april-27-2026-3pm-et
        """
        if when_utc.tzinfo is None:
            when_utc = when_utc.replace(tzinfo=timezone.utc)
        when_et = when_utc.astimezone(_ET)
        month = when_et.strftime("%B").lower()
        day = when_et.day
        year = when_et.year
        hour_12 = when_et.hour % 12 or 12
        ampm = "am" if when_et.hour < 12 else "pm"
        return f"{asset_prefix}-up-or-down-{month}-{day}-{year}-{hour_12}{ampm}-et"

    @classmethod
    def _iter_named_event_slugs(
        cls,
        *,
        prefixes: Tuple[str, ...],
        step_minutes: int,
        look_ahead: int,
    ) -> List[str]:
        now = datetime.now(timezone.utc)
        slugs: List[str] = []
        seen: set[str] = set()
        for offset in range(0, look_ahead + 1):
            window_time = now + timedelta(minutes=offset * step_minutes)
            for asset_prefix in prefixes:
                slug = cls._build_human_updown_event_slug(asset_prefix, window_time)
                if slug in seen:
                    continue
                seen.add(slug)
                slugs.append(slug)
        return slugs

    @classmethod
    def _iter_updown_event_slugs(cls, *, step_minutes: int, look_ahead: int) -> List[str]:
        return cls._iter_named_event_slugs(
            prefixes=cls._HUMAN_UPDOWN_PREFIXES,
            step_minutes=step_minutes,
            look_ahead=look_ahead,
        )

    @staticmethod
    def _parse_gamma_event_market(gm: Dict[str, Any], slug: str) -> Optional[Market]:
        try:
            outcomes = json.loads(gm.get("outcomePrices", "[]"))
            yes_price = float(outcomes[0]) if outcomes else 0.5
            no_price = float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price
            if yes_price <= 0.01 or yes_price >= 0.99:
                return None

            tokens = json.loads(gm.get("clobTokenIds", "[]"))
            vol = float(gm.get("volume", 0) or 0)
            liq = float(gm.get("liquidity", 0) or 0)
            question = gm.get("question", "")
            group_item_title = gm.get("groupItemTitle", "")
            end_str = gm.get("endDate") or gm.get("end_date_iso")
            end_date = _parse_updown_market_end_from_text(
                slug=slug,
                question=question,
                group_item_title=group_item_title,
            )
            if end_str:
                try:
                    end_date = end_date or datetime.fromisoformat(
                        end_str.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                except (ValueError, TypeError):
                    pass

            return Market(
                id=gm.get("id", ""),
                question=question,
                description=(gm.get("description", "") or "")[:300],
                volume=vol,
                liquidity=liq,
                yes_price=yes_price,
                no_price=no_price,
                spread=abs(yes_price - no_price),
                end_date=end_date,
                token_id_yes=tokens[0] if tokens else "",
                token_id_no=tokens[1] if len(tokens) > 1 else "",
                group_item_title=group_item_title,
                slug=slug,
            )
        except Exception:
            return None

    def _fetch_event_slug_markets(
        self,
        slugs: List[str],
        *,
        timeout_sec: float,
        limit: Optional[int] = None,
    ) -> List[Market]:
        markets: List[Market] = []
        seen_ids: set[str] = set()
        for slug in slugs:
            if limit is not None and len(markets) >= limit:
                break
            try:
                resp = requests.get(
                    f"{self.GAMMA_API_BASE}/events",
                    params={"slug": slug},
                    timeout=timeout_sec,
                )
                if resp.status_code != 200:
                    continue
                events = resp.json()
                if not events:
                    continue
                event = events[0]
                for gm in event.get("markets", []):
                    parsed = self._parse_gamma_event_market(gm, slug)
                    if parsed is None or parsed.id in seen_ids:
                        continue
                    seen_ids.add(parsed.id)
                    markets.append(parsed)
                    if limit is not None and len(markets) >= limit:
                        break
            except Exception as e:
                logger.debug(f"Failed to fetch updown slug {slug}: {e}")
                continue
        return markets

    def fetch_updown_markets(self, look_ahead: int = 8) -> List[Market]:
        """Fetch current + upcoming 15-minute crypto Up/Down markets.

        Args:
            look_ahead: number of future 15-min windows to fetch (default 4 = 1 hour)

        Returns:
            List of Market objects for tradeable updown windows.
        """
        markets = self._fetch_event_slug_markets(
            self._iter_updown_event_slugs(step_minutes=15, look_ahead=look_ahead),
            timeout_sec=8,
        )

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
        """Fetch current + upcoming 5-minute crypto Up/Down markets.

        Args:
            look_ahead: number of future 5-min windows to fetch (default 8 = 40 minutes)

        Returns:
            List of Market objects for tradeable 5m updown windows.
        """
        markets = self._fetch_event_slug_markets(
            self._iter_updown_event_slugs(step_minutes=5, look_ahead=look_ahead),
            timeout_sec=8,
        )

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
        """Fetch HYPE alias slugs directly without crawling the full event set.

        This is a bounded fallback path for named `hyperliquid` / `hype` event slugs.
        """
        look_ahead_15m, look_ahead_5m = self._resolve_updown_lookahead()
        slugs = self._iter_named_event_slugs(
            prefixes=("hyperliquid", "hype"),
            step_minutes=15,
            look_ahead=max(1, min(look_ahead_15m, 4)),
        )
        slugs.extend(
            self._iter_named_event_slugs(
                prefixes=("hyperliquid", "hype"),
                step_minutes=5,
                look_ahead=max(1, min(look_ahead_5m, 8)),
            )
        )
        markets = self._fetch_event_slug_markets(slugs, timeout_sec=4, limit=limit)
        if markets:
            logger.info(f"Fetched {len(markets)} Hyperliquid/HYPE alt up/down markets")
        return markets

    def fetch_weather_markets(
        self,
        cities: Optional[List[str]] = None,
        limit: int = 600,
    ) -> List[Market]:
        """Fetch open weather markets from Gamma only, no paid API dependency."""
        city_filter = {city.lower() for city in (cities or [])}
        markets: List[Market] = []
        seen_market_ids: set[str] = set()
        offset = 0
        raw_candidates = 0
        try:
            while len(markets) < limit:
                params = {
                    "limit": min(100, limit - len(markets)),
                    "offset": offset,
                    "active": "true",
                    "closed": "false",
                }
                resp = requests.get(
                    f"{self.GAMMA_API_BASE}/markets",
                    params=params,
                    timeout=8,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                parsed_batch = self._parse_markets(batch)
                for market in parsed_batch:
                    if not self._is_dedicated_weather_candidate(market):
                        continue
                    raw_candidates += 1
                    text = f"{market.slug} {market.question} {market.description} {market.group_item_title}".lower()
                    if city_filter and not any(city in text for city in city_filter):
                        continue
                    if market.id in seen_market_ids:
                        continue
                    seen_market_ids.add(market.id)
                    markets.append(market)
                offset += len(batch)
                if len(batch) < params["limit"]:
                    break
            if markets:
                sample = [m.question[:100] for m in markets[:3]]
                logger.info(
                    "Fetched %d dedicated weather markets from Gamma (raw_candidates=%d, sample=%s)",
                    len(markets),
                    raw_candidates,
                    sample,
                )
            else:
                logger.info("Fetched 0 dedicated weather markets from Gamma")
            return markets
        except Exception as e:
            logger.error(f"Weather market fetch error: {e}")
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
                weather,
                look_ahead_15m,
                look_ahead_5m,
            ) = await asyncio.wait_for(
                asyncio.to_thread(self._sync_network_phase),
                timeout=self._scanner_sync_timeout + 2.0,
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
        if weather:
            weather = await self.update_market_prices(weather)

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
            known_updown_ids = {
                m.id for m in opportunities.get("updown", [])
            } | {
                m.id for m in opportunities.get("updown_5m", [])
            }
            hype_alt = [m for m in hype_alt if m.id not in known_updown_ids]

        if hype_alt:
            opportunities["high_liquidity"].extend(hype_alt)
            opportunities["updown_hype_alt"] = hype_alt
        else:
            opportunities["updown_hype_alt"] = []

        if weather:
            opportunities["high_liquidity"].extend(weather)
            opportunities["weather"] = weather
        else:
            opportunities["weather"] = []

        opportunities["scanner_meta"] = {
            "look_ahead_15m": look_ahead_15m,
            "look_ahead_5m": look_ahead_5m,
            "updown_15m_count": len(opportunities.get("updown", [])),
            "updown_5m_count": len(opportunities.get("updown_5m", [])),
            "updown_hype_alt_count": len(opportunities.get("updown_hype_alt", [])),
            "weather_market_count": len(opportunities.get("weather", [])),
        }

        logger.info(
            "Scanner: bulk Gamma feed has %d generic consensus YES markets "
            "(not crypto up/down strategy buckets)",
            len(opportunities["consensus_yes"]),
        )
        logger.info(
            "Scanner: bulk Gamma feed has %d generic consensus NO markets "
            "(not crypto up/down strategy buckets)",
            len(opportunities["consensus_no"]),
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
        self._background_fetch_pool.shutdown(wait=False, cancel_futures=True)
