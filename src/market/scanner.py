"""
Market Scanner Module
Fetches market data from Polymarket Gamma REST API (bulk /markets and slug/event helpers).
Historical note: GraphQL primary was removed; Gamma is the live list source.
"""
import asyncio
import inspect
import json
import logging
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from threading import Lock
from threading import local as thread_local
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import aiohttp
import requests
from dataclasses import dataclass, field

from src.utils.http_retry import requests_get_with_retries

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

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
    # Parsed candle duration for crypto Up/Down event markets. Current Gamma
    # hourly markets use the same question shape as old short-window markets, so
    # strategy buckets must not infer 5m/15m from asset name alone.
    window_minutes: Optional[int] = None
    
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
    r"(?:(?:bitcoin|btc)|(?:solana|sol)|(?:ethereum|eth|ether)|(?:ripple|xrp)|(?:hyperliquid|hype)|(?:dogecoin|doge)|(?:bnb|binance\s+coin))\s+up\s+or\s+down",
    re.IGNORECASE,
)
# Slug prefix from Gamma event slugs (5m / 15m updown).
_CRYPTO_UPDOWN_SLUG_RE = re.compile(
    r"(?:btc|sol|eth|xrp|hype|doge|bnb)-updown-(?:5m|15m|30m)-", re.IGNORECASE
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


def _coerce_json_list(raw: Any) -> List[Any]:
    """Accept Gamma list-like fields serialized as JSON strings or arrays."""
    if isinstance(raw, list):
        return raw
    if raw is None:
        return []
    if isinstance(raw, str):
        payload = raw.strip()
        if not payload:
            return []
        try:
            parsed = json.loads(payload)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _parse_updown_window_minutes_from_text(text: str) -> Optional[int]:
    """Return the explicit Up/Down candle length from a market title/range."""
    time_match = _UPDOWN_TIME_RANGE_RE.search(text or "")
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

    start_total = _to_24h(start_hour, start_period) * 60 + start_minute
    end_total = _to_24h(end_hour, end_period) * 60 + end_minute
    diff = end_total - start_total
    if diff <= 0:
        diff += 24 * 60
    return diff


def _dedupe_markets_by_id(markets: List[Market]) -> List[Market]:
    seen: set[str] = set()
    out: List[Market] = []
    for m in markets:
        mid = (m.id or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(m)
    return out


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
            "dogecoin",
            "doge",
            "bnb",
            "binance coin",
        )
    ):
        return True
    git = (market.group_item_title or "").lower()
    return "up or down" in git and any(
        tok in git
        for tok in ("bitcoin", "btc", "solana", "sol", "ethereum", "eth", "xrp", "hyperliquid", "hype", "doge", "dogecoin", "bnb", "binance coin")
    )


def resolved_updown_window_minutes(market: Market) -> int:
    """Best-effort candle length for routing (5 / 15 / 60). Prefer Gamma ``window_minutes``."""
    wm = getattr(market, "window_minutes", None)
    if wm is not None:
        try:
            wmi = int(wm)
            if wmi > 0:
                return wmi
        except (TypeError, ValueError):
            pass
    blob = f"{market.question or ''} {market.group_item_title or ''}"
    parsed = _parse_updown_window_minutes_from_text(blob)
    if parsed is not None:
        return int(parsed)
    q = (market.question or "").lower()
    slug_l = (market.slug or "").lower()
    if "updown-5m-" in slug_l or "updown-5-" in slug_l:
        return 5
    if "updown-30m-" in slug_l:
        return 30  # historic journal/backfill rows only — product discontinued
    # Hourly slug shape: e.g. bitcoin-up-or-down-may-17-2026-1am-et
    if _UPDOWN_SLUG_DATE_RE.search(slug_l):
        return 60
    if "5m" in q or "5-min" in q:
        return 5
    if "30m" in q or "30-min" in q:
        return 30
    return 15


def updown_timeframe_label(window_minutes: int) -> str:
    """Bucket for strategy entry logic: 5m, 15m, or 1h.

    The legacy 30m crypto product was discontinued; hourly (~60min) is the live
    long-cycle bucket. Anything above 22min routes to 1h.
    """
    if window_minutes <= 6:
        return "5m"
    if window_minutes <= 22:
        return "15m"
    return "1h"


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


def _infer_updown_window_minutes(
    *, slug: str, question: str, group_item_title: str, end_date: Optional[datetime]
) -> Optional[int]:
    """Infer event-market duration without mistaking hourly markets for 5m/15m."""
    text = f"{question or ''} {group_item_title or ''}"
    explicit = _parse_updown_window_minutes_from_text(text)
    if explicit is not None:
        return explicit

    slug_l = (slug or "").lower()
    if "updown-5m" in slug_l:
        return 5
    if "updown-15m" in slug_l:
        return 15
    if "updown-30m" in slug_l:
        return 30

    # Current Gamma human slugs are hourly, e.g.
    # bitcoin-up-or-down-april-29-2026-9pm-et, with one market ending at 10PM ET.
    # The date pattern in the slug is sufficient to identify these as 60-min markets;
    # we don't need end_date to be successfully parsed from the question text.
    if _UPDOWN_SLUG_DATE_RE.search(slug_l):
        return 60
    return None


class MarketScanner:
    """Scans Polymarket for trading opportunities.

    Primary data source: Gamma REST API (https://gamma-api.polymarket.com)
    Note: graphql.polymarket.com/matic is permanently offline — removed.
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._reload_config_fields()
        self.session: Optional[aiohttp.ClientSession] = None
        self._sync_driver_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="scanner-driver"
        )
        self._sync_fetch_pool = ThreadPoolExecutor(
            max_workers=5, thread_name_prefix="scanner-sync"
        )
        self._slug_fetch_pool = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="scanner-slug"
        )
        self._background_fetch_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="scanner-bg"
        )
        self._slow_fetch_lock = Lock()
        self._slug_cache_lock = Lock()
        self._cycle_empty_event_slugs: set[str] = set()
        self._slug_fetch_stats: Dict[str, Dict[str, int]] = {}
        self._gamma_thread_state = thread_local()
        self._gamma_sessions_lock = Lock()
        self._gamma_sessions: set[requests.Session] = set()
        self._active_sync_phase: Optional[asyncio.Future] = None
        self._scan_call_count = 0
        self._updown_1h_cache: List[Market] = []
        self._updown_1h_cache_updated_at: Optional[datetime] = None

    def _reload_config_fields(self) -> None:
        """Refresh derived thresholds from the shared config dict."""
        _pm = self.config.get("polymarket", {}) or {}
        _tr = self.config.get("trading", {}) or {}
        self.min_liquidity = _pm.get("min_liquidity", 10000)
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
        self._gamma_http_max_retries = max(1, int(_pm.get("gamma_http_max_retries", 3)))
        self._gamma_http_retry_backoff_base_sec = max(
            0.05, float(_pm.get("gamma_http_retry_backoff_base_sec", 0.5))
        )

    def reload_from_config(self, config: Dict[str, Any]) -> None:
        """Apply updated config to the live scanner without replacing caches/pool."""
        self.config = config
        self._reload_config_fields()

    def _gamma_get(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 15,
    ) -> requests.Response:
        p = path if path.startswith("/") else f"/{path}"
        return requests_get_with_retries(
            f"{self.GAMMA_API_BASE}{p}",
            params=params or {},
            timeout=timeout,
            max_retries=self._gamma_http_max_retries,
            backoff_base=self._gamma_http_retry_backoff_base_sec,
            log_name="scanner.gamma",
            session=self._get_gamma_requests_session(),
        )

    def _get_gamma_requests_session(self) -> requests.Session:
        """Reuse a bounded requests session per worker thread for Gamma fetches."""
        session = getattr(self._gamma_thread_state, "gamma_session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=32,
                pool_maxsize=32,
                max_retries=0,
                pool_block=True,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._gamma_thread_state.gamma_session = session
            with self._gamma_sessions_lock:
                self._gamma_sessions.add(session)
        return session

    def _close_gamma_requests_sessions(self) -> None:
        with self._gamma_sessions_lock:
            sessions = list(self._gamma_sessions)
            self._gamma_sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                logger.debug("Failed to close Gamma requests session", exc_info=True)

    def _market_liquidity_threshold(self, question: str, description: str = "") -> float:
        return self.min_liquidity

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

    def _resolve_hourly_crypto_scan_every_n_cycles(self) -> int:
        trading_cfg = (self.config.get("trading", {}) or {})
        raw = trading_cfg.get("crypto_hourly_scan_every_n_cycles", 3)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 3

    def _should_refresh_updown_1h(self, scan_call_count: int) -> bool:
        every_n = self._resolve_hourly_crypto_scan_every_n_cycles()
        call_n = max(1, int(scan_call_count or 1))
        return ((call_n - 1) % every_n) == 0

    def _invoke_sync_network_phase(self, refresh_updown_1h: bool) -> Tuple[
        List[Market],
        List[Market],
        List[Market],
        List[Market],
        List[Market],
        List[Market],
        int,
        int,
        int,
    ]:
        sync_fn = self._sync_network_phase
        try:
            params = inspect.signature(sync_fn).parameters
        except (TypeError, ValueError):
            params = {}
        if not params:
            return sync_fn()
        return sync_fn(refresh_updown_1h)

    def _sync_network_phase(self, refresh_updown_1h: bool = True) -> Tuple[
        List[Market],
        List[Market],
        List[Market],
        List[Market],
        List[Market],
        int,
        int,
        int,
    ]:
        """Blocking HTTP: Gamma list + 15m/5m/30m updown + optional HYPE alt. Runs in a thread.

        Fetches run in parallel via ThreadPoolExecutor so the longest one (HYPE alt)
        doesn't add to the wall-clock time of the other fetches.
        """
        with self._slug_cache_lock:
            self._cycle_empty_event_slugs = set()
            self._slug_fetch_stats = {}
        look_ahead_15m, look_ahead_5m, look_ahead_1h = self._resolve_updown_lookahead()
        fetch_hype = self._should_fetch_hype_alt_markets()

        tasks = {
            "gamma": lambda: self._fetch_markets_gamma(limit=200),
            "updown": lambda: self.fetch_updown_markets(look_ahead=look_ahead_15m),
            "updown_5m": lambda: self.fetch_updown_5m_markets(look_ahead=look_ahead_5m),
        }
        if refresh_updown_1h:
            tasks["updown_1h"] = lambda: self.fetch_updown_1h_markets(look_ahead=look_ahead_1h)
        if fetch_hype:
            tasks["hype_alt"] = lambda: self.fetch_hype_alt_updown_markets(limit=100)

        results: Dict[str, Any] = {}
        failed_fetches: set[str] = set()
        completed_fetches: set[str] = set()
        futures = {self._sync_fetch_pool.submit(fn): name for name, fn in tasks.items()}
        try:
            for future in as_completed(futures, timeout=self._scanner_sync_timeout):
                name = futures[future]
                completed_fetches.add(name)
                try:
                    results[name] = future.result() or []
                except Exception as e:
                    logger.error(f"{name} fetch error: {e}")
                    failed_fetches.add(name)
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

        # fetch_updown_markets historically returned (15m, ~30m carry) — the 30m carry path
        # is dead (Polymarket discontinued the 30m crypto product family). Accept either
        # the legacy tuple form or a bare list, ignoring any second element.
        up_pair = results.get("updown", [])
        if isinstance(up_pair, tuple) and up_pair:
            updown_15m = up_pair[0] if isinstance(up_pair[0], list) else []
        else:
            updown_15m = up_pair if isinstance(up_pair, list) else []

        updown_1h_markets: List[Market]
        if refresh_updown_1h:
            hourly_failed = (
                "updown_1h" in failed_fetches or "updown_1h" not in completed_fetches
            )
            if hourly_failed and self._updown_1h_cache:
                logger.warning(
                    "Scanner: reusing cached hourly updown markets after live 1h fetch failure/timeout"
                )
                updown_1h_markets = list(self._updown_1h_cache)
            else:
                updown_1h_markets = results.get("updown_1h", []) or []
                self._updown_1h_cache = list(updown_1h_markets)
                self._updown_1h_cache_updated_at = datetime.now(timezone.utc)
        else:
            updown_1h_markets = list(self._updown_1h_cache)
            if updown_1h_markets:
                logger.info(
                    "Scanner: reusing cached hourly updown markets (skip cadence active)"
                )

        return (
            results.get("gamma", []),
            updown_15m,
            results.get("updown_5m", []),
            updown_1h_markets,
            results.get("hype_alt", []),
            look_ahead_15m,
            look_ahead_5m,
            look_ahead_1h,
        )

    def _empty_scan_result(self, sync_timeout: bool = False) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "look_ahead_15m": 0,
            "look_ahead_5m": 0,
            "look_ahead_1h": 0,
            "updown_15m_count": 0,
            "updown_5m_count": 0,
            "updown_1h_count": 0,
            "updown_hype_alt_count": 0,
            "slug_fetch_stats": {},
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
            "updown_1h": [],
            "updown_hype_alt": [],
            "scanner_meta": meta,
        }

    def _resolve_updown_lookahead(self) -> tuple[int, int, int]:
        """Resolve scanner look-ahead from enabled strategy configs.

        Returns:
            (lookahead_15m, lookahead_5m, lookahead_1h)
        """
        strategies = self.config.get("strategies", {}) or {}
        keys = [
            "bitcoin",
            "sol_macro",
            "eth_macro",
            "hype_macro",
            "xrp_macro",
            "doge_macro",
            "bnb_macro",
        ]

        enabled_cfgs = []
        for key in keys:
            cfg = strategies.get(key, {}) or {}
            if bool(cfg.get("enabled", False)):
                enabled_cfgs.append(cfg)

        cfg_pool = enabled_cfgs if enabled_cfgs else [strategies.get(k, {}) or {} for k in keys]

        # Seed low so max() across strategy configs reflects requested lookahead (not a floor of 8).
        look_15m = 1
        look_5m = 1
        look_1h = 1
        for cfg in cfg_pool:
            look_15m = max(look_15m, int(cfg.get("look_ahead_15m", 8)))
            look_5m = max(look_5m, int(cfg.get("look_ahead_5m", 3)))
            # Accept legacy look_ahead_30m as alias for the renamed lane.
            look_1h = max(
                look_1h,
                int(cfg.get("look_ahead_1h", cfg.get("look_ahead_30m", 4))),
            )

        look_15m = max(1, min(96, look_15m))
        look_5m = max(1, min(288, look_5m))
        look_1h = max(1, min(48, look_1h))
        return look_15m, look_5m, look_1h
        
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session.

        Cap connection pool to prevent socket exhaustion under bursty fetches
        (e.g. fetch_prices fan-out across many token_ids). Default aiohttp limit
        is 100 with no per-host cap, which can exhaust ephemeral ports on
        long-running processes and cause 5-10s tail stalls.
        """
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=50, limit_per_host=20, ttl_dns_cache=300
            )
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self.session = aiohttp.ClientSession(
                connector=connector, timeout=timeout
            )
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
                        else:
                            return token_id, None
                except asyncio.TimeoutError:
                    return token_id, "timeout"
                except Exception as e:
                    return token_id, f"err:{type(e).__name__}"
                return token_id, None

        try:
            results = await asyncio.gather(*[_get_mid(tid) for tid in token_ids])
            prices: Dict[str, float] = {}
            timeouts = 0
            errors = 0
            misses = 0
            for tid, val in results:
                if isinstance(val, float):
                    prices[tid] = val
                elif val == "timeout":
                    timeouts += 1
                elif isinstance(val, str) and val.startswith("err:"):
                    errors += 1
                else:
                    misses += 1
            if timeouts or errors or misses:
                logger.debug(
                    "Scanner.fetch_prices: %d/%d ok (timeouts=%d errors=%d misses=%d)",
                    len(prices), len(token_ids), timeouts, errors, misses,
                )
            return prices
        except Exception as e:
            logger.error(f"Error fetching prices: {e}")
            return {}
    
    def _parse_markets(self, markets_data: List[Dict]) -> List[Market]:
        """Parse raw market data into Market objects"""
        markets = []
        
        for m in markets_data:
            try:
                # Extract token IDs (YES and NO)
                clob_token_ids = _coerce_json_list(m.get('clobTokenIds', []))
                if len(clob_token_ids) < 2:
                    continue
                    
                token_id_yes = clob_token_ids[0]
                token_id_no = clob_token_ids[1]
                
                # Parse end date
                end_date = None
                if m.get('endDate'):
                    try:
                        end_date = datetime.fromisoformat(m['endDate'].replace('Z', '+00:00'))
                    except (ValueError, TypeError, AttributeError) as e:
                        logger.debug(
                            "Scanner: failed to parse endDate=%r for market %s: %s",
                            m.get('endDate'), m.get('id', '?'), e,
                        )
                question = m.get('question', '')
                group_item_title = m.get('groupItemTitle', '')
                slug = str(m.get("slug") or "")
                window_minutes = _infer_updown_window_minutes(
                    slug=slug,
                    question=question,
                    group_item_title=group_item_title,
                    end_date=end_date,
                ) if _CRYPTO_ASSET_UPDOWN_PATTERN.search(question or "") else None
                
                market = Market(
                    id=m['id'],
                    question=question,
                    description=m.get('description', ''),
                    volume=float(m.get('volume', 0)),
                    liquidity=float(m.get('liquidity', 0)),
                    yes_price=0.5,  # Will be updated with real prices
                    no_price=0.5,   # Will be updated with real prices
                    spread=0.0,      # Will be calculated
                    end_date=end_date,
                    token_id_yes=token_id_yes,
                    token_id_no=token_id_no,
                    group_item_title=group_item_title,
                    slug=slug,
                    window_minutes=window_minutes,
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

    def _set_slug_fetch_stats(
        self, key: str, *, attempted: int, hit_slugs: int, empty_slugs: int
    ) -> None:
        with self._slug_cache_lock:
            self._slug_fetch_stats[key] = {
                "attempted_slugs": int(max(0, attempted)),
                "hit_slugs": int(max(0, hit_slugs)),
                "empty_slug_responses": int(max(0, empty_slugs)),
            }

    def _get_slug_fetch_stats_snapshot(self) -> Dict[str, Dict[str, int]]:
        with self._slug_cache_lock:
            return {key: dict(values) for key, values in self._slug_fetch_stats.items()}
    
    def _fetch_markets_gamma(self, limit: int = 100) -> List[Market]:
        """Fetch active markets from Gamma REST ``GET /markets`` (paginated)."""
        markets = []
        offset = 0
        try:
            while len(markets) < limit:
                params = {"limit": min(limit - len(markets), 100), "offset": offset,
                          "active": "true", "closed": "false"}
                resp = self._gamma_get("/markets", params=params, timeout=15)
                try:
                    resp.raise_for_status()
                    batch = resp.json()
                finally:
                    resp.close()
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
                        outcomes = _coerce_json_list(gm.get("outcomePrices", "[]"))
                        yes_price = float(outcomes[0]) if outcomes else 0.5
                        no_price = float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price
                        tokens = _coerce_json_list(gm.get("clobTokenIds", "[]"))
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
                        question = gm.get("question", "")
                        group_item_title = gm.get("groupItemTitle", "")
                        slug = str(gm.get("slug") or "")
                        window_minutes = _infer_updown_window_minutes(
                            slug=slug,
                            question=question,
                            group_item_title=group_item_title,
                            end_date=end_date,
                        ) if _CRYPTO_ASSET_UPDOWN_PATTERN.search(question or "") else None
                        m = Market(
                            id=gm.get("id", ""), question=question,
                            description=(gm.get("description", "") or "")[:200],
                            volume=vol, liquidity=liq,
                            yes_price=yes_price, no_price=no_price, spread=spread_val,
                            end_date=end_date, token_id_yes=token_yes, token_id_no=token_no,
                            group_item_title=group_item_title,
                            slug=slug,
                            window_minutes=window_minutes,
                        )
                        if 0.01 < m.yes_price < 0.99:
                            markets.append(m)
                    except Exception:
                        continue
                offset += len(batch)
                if len(batch) < params["limit"]:
                    break
        except requests.exceptions.HTTPError as e:
            snippet = ""
            if e.response is not None:
                try:
                    snippet = (e.response.text or "")[:400]
                except Exception:
                    snippet = ""
            logger.error(
                "Gamma /markets HTTP error status=%s: %s body=%r",
                getattr(e.response, "status_code", None),
                e,
                snippet,
                exc_info=True,
            )
        except requests.RequestException as e:
            logger.error("Gamma /markets request error: %s", e, exc_info=True)
        except Exception as e:
            logger.error("Gamma /markets unexpected error: %s", e, exc_info=True)
        logger.info(f"Gamma API fetched {len(markets)} markets")
        return markets

    # ──────────────────────────────────────────────────────────────
    # Short-window Up/Down market fetcher
    # ──────────────────────────────────────────────────────────────
    GAMMA_API_BASE = "https://gamma-api.polymarket.com"

    _TIMESTAMP_UPDOWN_PREFIXES = (
        "btc",
        "sol",
        "eth",
        "xrp",
        "hype",
        "doge",
        "bnb",
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
        now_ts = int(datetime.now(timezone.utc).timestamp())
        step_seconds = step_minutes * 60
        floor_ts = (now_ts // step_seconds) * step_seconds
        label = f"{step_minutes}m"
        slugs: List[str] = []
        for offset in range(0, look_ahead + 1):
            window_ts = floor_ts + offset * step_seconds
            for asset_prefix in cls._TIMESTAMP_UPDOWN_PREFIXES:
                slugs.append(f"{asset_prefix}-updown-{label}-{window_ts}")
        return slugs

    # Asset prefixes Polymarket lists under the hourly Up/Down product
    # (https://polymarket.com/crypto/hourly). Prefixes are not fully consistent
    # with the short-window slug families: HYPE uses ``hype-`` (not
    # ``hyperliquid-``), while DOGE uses ``dogecoin-`` (not ``doge-``).
    _HOURLY_UPDOWN_ASSETS: Tuple[str, ...] = (
        "bitcoin",
        "ethereum",
        "solana",
        "xrp",
        "hype",
        "dogecoin",
        "bnb",
    )

    @classmethod
    def _iter_updown_1h_human_slugs(
        cls, *, look_ahead: int, now_utc: Optional[datetime] = None
    ) -> List[str]:
        """Hourly Up/Down slugs in the live Polymarket shape.

        Example: ``bitcoin-up-or-down-may-17-2026-1am-et`` — single start-hour token (ET),
        year included. One market per asset per hour, starting at the current ET hour.
        """
        ref = now_utc or datetime.now(timezone.utc)
        now_et = ref.astimezone(_ET).replace(minute=0, second=0, microsecond=0)
        out: List[str] = []
        seen: set[str] = set()
        for offset in range(0, max(0, int(look_ahead)) + 1):
            slot = now_et + timedelta(hours=offset)
            for asset_w in cls._HOURLY_UPDOWN_ASSETS:
                slug = cls._build_human_updown_event_slug(asset_w, slot.astimezone(timezone.utc))
                if slug not in seen:
                    seen.add(slug)
                    out.append(slug)
        return out

    @staticmethod
    def _parse_gamma_event_market(gm: Dict[str, Any], slug: str) -> Optional[Market]:
        try:
            outcomes = _coerce_json_list(gm.get("outcomePrices", "[]"))
            yes_price = float(outcomes[0]) if outcomes else 0.5
            no_price = float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price
            if yes_price <= 0.01 or yes_price >= 0.99:
                return None

            tokens = _coerce_json_list(gm.get("clobTokenIds", "[]"))
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
            window_minutes = _infer_updown_window_minutes(
                slug=slug,
                question=question,
                group_item_title=group_item_title,
                end_date=end_date,
            )

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
                window_minutes=window_minutes,
            )
        except Exception:
            return None

    def _fetch_event_slug_markets(
        self,
        slugs: List[str],
        *,
        timeout_sec: float,
        limit: Optional[int] = None,
        stats_key: Optional[str] = None,
    ) -> List[Market]:
        def _fetch_one(slug: str) -> List[Market]:
            with self._slug_cache_lock:
                if slug in self._cycle_empty_event_slugs:
                    return []
            parsed_markets: List[Market] = []
            try:
                market_resp = self._gamma_get(
                    "/markets", params={"slug": slug}, timeout=timeout_sec
                )
                try:
                    if market_resp.status_code == 200:
                        for gm in market_resp.json() or []:
                            parsed = self._parse_gamma_event_market(gm, slug)
                            if parsed is not None:
                                parsed_markets.append(parsed)
                        # Non-empty /markets: done. Empty /markets on *-updown-* slugs must still
                        # try /events — Gamma often nests those markets under the event only.
                        if parsed_markets:
                            return parsed_markets
                finally:
                    market_resp.close()

                resp = self._gamma_get(
                    "/events", params={"slug": slug}, timeout=timeout_sec
                )
                try:
                    if resp.status_code == 200:
                        events = resp.json() or []
                        if events:
                            event = events[0]
                            for gm in event.get("markets", []):
                                parsed = self._parse_gamma_event_market(gm, slug)
                                if parsed is not None:
                                    parsed_markets.append(parsed)
                finally:
                    resp.close()
            except Exception as e:
                logger.debug(f"Failed to fetch updown slug {slug}: {e}")
            if not parsed_markets:
                with self._slug_cache_lock:
                    self._cycle_empty_event_slugs.add(slug)
            return parsed_markets

        markets: List[Market] = []
        seen_ids: set[str] = set()
        hit_slugs = 0
        empty_slugs = 0
        future_to_slug = {self._slug_fetch_pool.submit(_fetch_one, slug): slug for slug in slugs}
        try:
            for future in as_completed(future_to_slug):
                parsed_batch = future.result()
                if parsed_batch:
                    hit_slugs += 1
                else:
                    empty_slugs += 1
                for parsed in parsed_batch:
                    if parsed is None or parsed.id in seen_ids:
                        continue
                    seen_ids.add(parsed.id)
                    markets.append(parsed)
                    if limit is not None and len(markets) >= limit:
                        break
                if limit is not None and len(markets) >= limit:
                    break
        finally:
            for future in future_to_slug:
                if not future.done():
                    future.cancel()
        if stats_key:
            self._set_slug_fetch_stats(
                stats_key,
                attempted=len(slugs),
                hit_slugs=hit_slugs,
                empty_slugs=empty_slugs,
            )
        markets.sort(key=lambda m: (m.end_date or datetime.max.replace(tzinfo=timezone.utc), m.slug, m.id))
        return markets

    def fetch_updown_markets(self, look_ahead: int = 8) -> List[Market]:
        """Fetch crypto Up/Down markets from the ``*-updown-15m-{unix}`` slug family."""
        raw = self._fetch_event_slug_markets(
            self._iter_updown_event_slugs(step_minutes=15, look_ahead=look_ahead),
            timeout_sec=8,
            stats_key="updown_15m",
        )
        fifteen: List[Market] = []
        skipped_other: List[Market] = []
        for m in raw:
            wm = m.window_minutes
            if wm is None:
                if "updown-15m" in (m.slug or "").lower():
                    fifteen.append(m)
                else:
                    skipped_other.append(m)
                continue
            if 10 <= wm <= 20:
                fifteen.append(m)
            else:
                skipped_other.append(m)
        if skipped_other:
            durs = sorted(
                {int(x.window_minutes) for x in skipped_other if x.window_minutes is not None}
            )
            logger.info(
                "Skipped %d updown rows from 15m slug batch (window_minutes not in 10-20 band; durations=%s)",
                len(skipped_other),
                durs,
            )

        if fifteen:
            def _is_eth_mkt(m: Market) -> bool:
                q = m.question.lower()
                return "ethereum" in q or "ether" in q or bool(re.search(r"\beth\b", q))

            def _is_hype_mkt(m: Market) -> bool:
                q = m.question.lower()
                return "hyperliquid" in q or bool(re.search(r"\bhype\b", q))

            logger.info(
                f"Fetched {len(fifteen)} 15m updown markets "
                f"(BTC: {sum(1 for m in fifteen if 'bitcoin' in m.question.lower())}, "
                f"SOL: {sum(1 for m in fifteen if 'solana' in m.question.lower())}, "
                f"ETH: {sum(1 for m in fifteen if _is_eth_mkt(m))}, "
                f"XRP: {sum(1 for m in fifteen if 'xrp' in m.question.lower() or 'ripple' in m.question.lower())}, "
                f"HYPE: {sum(1 for m in fifteen if _is_hype_mkt(m))})"
            )
        return fifteen

    def fetch_updown_5m_markets(self, look_ahead: int = 3) -> List[Market]:
        """Fetch current + upcoming 5-minute crypto Up/Down markets.

        Args:
            look_ahead: number of future 5-min windows to fetch (default 3 ≈ 15 minutes ahead;
                keep small vs window length so momentum features target this cycle, not the next)

        Returns:
            List of Market objects for tradeable 5m updown windows.
        """
        markets = self._fetch_event_slug_markets(
            self._iter_updown_event_slugs(step_minutes=5, look_ahead=look_ahead),
            timeout_sec=8,
            stats_key="updown_5m",
        )
        rejected = [m for m in markets if m.window_minutes and m.window_minutes > 6]
        markets = [m for m in markets if m.window_minutes and m.window_minutes <= 6]
        if rejected:
            logger.info(
                "Skipped %d non-5m updown markets from 5m bucket (durations=%s)",
                len(rejected),
                sorted({m.window_minutes for m in rejected}),
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

    def fetch_updown_1h_markets(self, look_ahead: int = 4) -> List[Market]:
        """Fetch current + upcoming 1-hour crypto Up/Down markets.

        Slug shape: ``{asset}-up-or-down-{month}-{day}-{year}-{H}{am|pm}-et``
        (e.g. ``bitcoin-up-or-down-may-17-2026-1am-et``).

        Polymarket discontinued the legacy ``*-updown-30m-{unix}`` family entirely; the
        hourly product is the live replacement. The active hourly scanner universe is
        defined by ``_HOURLY_UPDOWN_ASSETS``.
        """
        slugs = self._iter_updown_1h_human_slugs(look_ahead=look_ahead)
        markets = self._fetch_event_slug_markets(
            slugs,
            timeout_sec=8,
            stats_key="updown_1h",
        )
        # Hourly markets have endDate-startDate spanning the trade-open window (~48h),
        # so band-filter against window_minutes — _infer_updown_window_minutes returns 60
        # for any market whose slug matches the ``up-or-down-<month>-<day>-<year>`` pattern.
        lo, hi = 55, 65
        rejected = [m for m in markets if m.window_minutes and not (lo <= m.window_minutes <= hi)]
        markets = [m for m in markets if m.window_minutes and lo <= m.window_minutes <= hi]
        if rejected:
            logger.info(
                "Skipped %d non-1h updown markets from hourly bucket (durations=%s)",
                len(rejected),
                sorted({m.window_minutes for m in rejected}),
            )

        if markets:
            def _is_eth_mkt_1h(m: Market) -> bool:
                q = m.question.lower()
                return "ethereum" in q or "ether" in q or bool(re.search(r"\beth\b", q))

            def _is_hype_mkt_1h(m: Market) -> bool:
                q = m.question.lower()
                return "hyperliquid" in q or bool(re.search(r"\bhype\b", q))

            logger.info(
                f"Fetched {len(markets)} 1h updown markets "
                f"(BTC: {sum(1 for m in markets if 'bitcoin' in m.question.lower())}, "
                f"SOL: {sum(1 for m in markets if 'solana' in m.question.lower())}, "
                f"ETH: {sum(1 for m in markets if _is_eth_mkt_1h(m))}, "
                f"XRP: {sum(1 for m in markets if 'xrp' in m.question.lower() or 'ripple' in m.question.lower())}, "
                f"HYPE: {sum(1 for m in markets if _is_hype_mkt_1h(m))})"
            )
        return markets

    def fetch_hype_alt_updown_markets(self, limit: int = 100) -> List[Market]:
        """Fetch HYPE alias slugs directly without crawling the full event set.

        This is a bounded fallback path for named `hyperliquid` / `hype` event slugs.
        """
        look_ahead_15m, look_ahead_5m, _look_ahead_1h = self._resolve_updown_lookahead()
        # HYPE has no hourly crypto Up/Down product on Polymarket, so the hourly slug
        # batch is skipped here. Keep 5m and 15m alt-slug families.
        slugs = self._iter_named_event_slugs(
            prefixes=("hyperliquid", "hype"),
            step_minutes=15,
            look_ahead=max(1, min(look_ahead_15m, 8)),
        )
        slugs.extend(
            self._iter_named_event_slugs(
                prefixes=("hyperliquid", "hype"),
                step_minutes=5,
                look_ahead=max(1, min(look_ahead_5m, 24)),
            )
        )
        markets = self._fetch_event_slug_markets(
            slugs,
            timeout_sec=4,
            limit=limit,
            stats_key="updown_hype_alt",
        )
        if markets:
            logger.info(f"Fetched {len(markets)} Hyperliquid/HYPE alt up/down markets")
        return markets

    async def scan_for_opportunities(self) -> Dict[str, Any]:
        """Scan for different types of opportunities.

        Sync HTTP (Gamma + updown + optional HYPE alt) runs in a worker thread with a
        timeout so the asyncio event loop is not blocked for minutes on slow APIs.
        Price hydration for gamma, 15m / 5m / 1h updown batches runs in
        parallel via asyncio.gather; HYPE alt hydrates after dedupe against those IDs.
        """
        t_scan_start = time.perf_counter()
        logger.info("Scanner: sync network phase (thread) starting")
        active_phase = self._active_sync_phase
        if active_phase is not None and not active_phase.done():
            logger.warning(
                "Scanner: previous sync phase still running; skipping new network phase to avoid overlap"
            )
            result = self._empty_scan_result(sync_timeout=True)
            result["scanner_meta"]["sync_phase_overlap_skipped"] = True
            return result
        try:
            loop = asyncio.get_running_loop()
            self._scan_call_count += 1
            refresh_updown_1h = self._should_refresh_updown_1h(self._scan_call_count)
            sync_phase = loop.run_in_executor(
                self._sync_driver_pool,
                lambda: self._invoke_sync_network_phase(refresh_updown_1h),
            )
            self._active_sync_phase = sync_phase
            (
                markets,
                updown,
                updown_5m,
                updown_1h,
                hype_alt,
                look_ahead_15m,
                look_ahead_5m,
                look_ahead_1h,
            ) = await asyncio.wait_for(
                asyncio.shield(sync_phase),
                timeout=self._scanner_sync_timeout + 2.0,
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((time.perf_counter() - t_scan_start) * 1000)
            logger.error(
                "Scanner: sync network phase timed out after %dms (limit=%.1fs) — empty scan",
                elapsed_ms,
                self._scanner_sync_timeout,
            )
            result = self._empty_scan_result(sync_timeout=True)
            result["scanner_meta"]["sync_phase_elapsed_ms"] = elapsed_ms
            return result
        finally:
            if self._active_sync_phase is not None and self._active_sync_phase.done():
                self._active_sync_phase = None

        sync_ms = int((time.perf_counter() - t_scan_start) * 1000)
        logger.info("Scanner: sync network phase finished in %dms", sync_ms)

        async def _hydrate(ms: List[Market]) -> List[Market]:
            return await self.update_market_prices(ms) if ms else []

        markets, updown, updown_5m, updown_1h = await asyncio.gather(
            _hydrate(markets),
            _hydrate(updown),
            _hydrate(updown_5m),
            _hydrate(updown_1h),
        )

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

        if updown_1h:
            opportunities["high_liquidity"].extend(updown_1h)
            opportunities["updown_1h"] = updown_1h
        else:
            opportunities["updown_1h"] = []

        if hype_alt:
            known_updown_ids = (
                {m.id for m in opportunities.get("updown", [])}
                | {m.id for m in opportunities.get("updown_5m", [])}
                | {m.id for m in opportunities.get("updown_1h", [])}
            )
            hype_alt = [m for m in hype_alt if m.id not in known_updown_ids]

        if hype_alt:
            hype_alt = await self.update_market_prices(hype_alt)
            opportunities["high_liquidity"].extend(hype_alt)
            opportunities["updown_hype_alt"] = hype_alt
        else:
            opportunities["updown_hype_alt"] = []

        opportunities["scanner_meta"] = {
            "look_ahead_15m": look_ahead_15m,
            "look_ahead_5m": look_ahead_5m,
            "look_ahead_1h": look_ahead_1h,
            "updown_1h_source": "live" if refresh_updown_1h else "cache",
            "sync_phase_elapsed_ms": sync_ms,
            "updown_15m_count": len(opportunities.get("updown", [])),
            "updown_5m_count": len(opportunities.get("updown_5m", [])),
            "updown_1h_count": len(opportunities.get("updown_1h", [])),
            "updown_hype_alt_count": len(opportunities.get("updown_hype_alt", [])),
            "slug_fetch_stats": self._get_slug_fetch_stats_snapshot(),
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
        self._close_gamma_requests_sessions()
        self._sync_driver_pool.shutdown(wait=False, cancel_futures=True)
        self._sync_fetch_pool.shutdown(wait=False, cancel_futures=True)
        self._slug_fetch_pool.shutdown(wait=False, cancel_futures=True)
        self._background_fetch_pool.shutdown(wait=False, cancel_futures=True)
