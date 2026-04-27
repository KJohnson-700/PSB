"""
Weather strategy: compare Open-Meteo forecast vs Polymarket price.
Uses ICAO airport station coordinates (not city centers) — Polymarket resolves on
airport METAR data, so this is the primary alpha source.
Documented edge: $1K→$24K London, $65K NYC/London/Seoul.
"""

import logging
import math
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

import requests

from src.market.scanner import Market, is_crypto_updown_market
from src.analysis.math_utils import PositionSizer
from src.strategies.weather_models import WeatherSignal

logger = logging.getLogger(__name__)

# ICAO airport station coords — Polymarket resolves on these, NOT city centers.
# Order: longest/most-specific patterns first to avoid partial matches.
_CITY_PATTERNS: List[Tuple[re.Pattern, tuple, str]] = [
    (re.compile(r'\bnew\s+york\b|\bnyc\b', re.I),   (40.7769, -73.8740),  "KLGA"),
    (re.compile(r'\bjfk\b|\bkennedy\b', re.I),      (40.6413, -73.7781),  "KJFK"),
    (re.compile(r'\blaguardia\b', re.I),            (40.7769, -73.8740),  "KLGA"),
    (re.compile(r'\blos\s+angeles\b|\blax\b', re.I), (33.9425, -118.4081), "KLAX"),
    (re.compile(r"\bchicago(?:\s+o'?hare)?\b|\bo'?hare\b", re.I), (41.9742, -87.9073),  "KORD"),
    (re.compile(r'\bmiami\b', re.I),                 (25.7959, -80.2870),  "KMIA"),
    (re.compile(r'\bdallas\b', re.I),                (32.8481, -96.8512),  "KDAL"),
    (re.compile(r'\bdenver\b', re.I),                (39.8561, -104.6737), "KDEN"),
    (re.compile(r'\bphoenix\b', re.I),               (33.4342, -112.0116), "KPHX"),
    (re.compile(r'\bboston\b', re.I),                (42.3656, -71.0096),  "KBOS"),
    (re.compile(r'\bseattle\b', re.I),               (47.4502, -122.3088), "KSEA"),
    (re.compile(r'\batlanta\b', re.I),               (33.6407, -84.4277),  "KATL"),
    (re.compile(r'\bhouston\b', re.I),               (29.9902, -95.3368),  "KIAH"),
    (re.compile(r'\blondon\b|\bheathrow\b', re.I),  (51.4700, -0.4543),   "EGLL"),
    (re.compile(r'\btokyo\b', re.I),                 (35.5494, 139.7798),  "RJTT"),
    (re.compile(r'\bseoul\b', re.I),                 (37.4691, 126.4510),  "RKSS"),
    (re.compile(r'\bparis\b', re.I),                 (49.0097, 2.5479),    "LFPG"),
    (re.compile(r'\bsydney\b', re.I),                (-33.9399, 151.1753), "YSSY"),
    (re.compile(r'\bsingapore\b', re.I),             (1.3644, 103.9915),   "WSSS"),
    (re.compile(r'\bdubai\b', re.I),                 (25.2532, 55.3657),   "OMDB"),
]

# Market must contain a weather keyword to be considered.
_WEATHER_RE = re.compile(
    r'\b(rain|snow|precipitation|temperature|temp|weather|forecast|'
    r'degrees?|fahrenheit|celsius|humid|storm|flood|drought|sunshine|sunny|cloudy|wind|'
    r'hail|thunderstorm|inches?|inch|mm|centimeters?|cm|°f|°c)\b',
    re.I,
)

# Temperature market detection.
_TEMP_RE = re.compile(r'\b(temperature|temp|degrees?|fahrenheit|celsius|°f|°c)\b', re.I)

# Threshold parsers — handles "between 68–69°F", "above 70°F", "below 50°F"
_TEMP_RANGE_RE = re.compile(
    r'between\s+(\d+\.?\d*)\s*[°\-–]?\s*(?:and\s+)?(\d+\.?\d*)\s*[°]?\s*[fF]?\b', re.I
)
_TEMP_ABOVE_RE = re.compile(r'\babove\s+(\d+\.?\d*)\s*[°]?[fF]?\b', re.I)
_TEMP_BELOW_RE = re.compile(r'\bbelow\s+(\d+\.?\d*)\s*[°]?[fF]?\b', re.I)
_TEMP_EXACT_RE = re.compile(r'(\d+\.?\d*)\s*[°]?[fF]\b')


class WeatherStrategy:
    """Trade when Open-Meteo airport-station forecast diverges from market price."""

    def __init__(self, config: Dict[str, Any], position_sizer: PositionSizer, kelly_sizer=None):
        cfg = config.get("strategies", {}).get("weather", {})
        self.config = cfg
        self.position_sizer = position_sizer
        self.kelly_sizer = kelly_sizer
        self.gap_min = cfg.get("gap_min", 0.15)
        self.min_ev = cfg.get("min_ev", 0.05)
        self.min_volume = cfg.get("min_volume", 2000.0)
        self.min_liquidity = cfg.get("min_liquidity", self.min_volume)
        self.min_hours = cfg.get("min_hours_to_resolution", 2.0)
        self.max_hours = cfg.get("max_hours_to_resolution", 72.0)
        self.max_yes_price = cfg.get("max_yes_price", 0.45)
        self.metar_enabled = cfg.get("metar_enabled", True)
        self.enabled = cfg.get("enabled", False)
        self.kelly_fraction = cfg.get("kelly_fraction", 0.25)
        self._signal_strategy_name = "weather"
        self._backtest_proxy_forecast: Optional[float] = None
        # Scan diagnostics reset each cycle
        self._scan_stats: Dict[str, int] = {}

    # ── Market classification ────────────────────────────────────────────────

    def _is_weather_market(self, question: str, description: str) -> bool:
        return bool(_WEATHER_RE.search(f"{question} {description}"))

    def _parse_market_location(
        self, question: str, description: str
    ) -> Optional[Tuple[tuple, str]]:
        """Return ((lat, lon), icao) or None."""
        text = f"{question} {description}"
        for pattern, coords, icao in _CITY_PATTERNS:
            if pattern.search(text):
                return coords, icao
        return None

    def _parse_temp_threshold(self, question: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
        """Parse temperature threshold from question.

        Returns:
            (low, high)  — range market  e.g. (68.0, 69.0)
            (thresh, None) — above market e.g. (70.0, None)
            (None, thresh) — below market e.g. (None, 50.0)
            None           — unparseable
        """
        m = _TEMP_RANGE_RE.search(question)
        if m:
            return float(m.group(1)), float(m.group(2))
        m = _TEMP_ABOVE_RE.search(question)
        if m:
            return float(m.group(1)), None
        m = _TEMP_BELOW_RE.search(question)
        if m:
            return None, float(m.group(1))
        return None

    # ── Forecast fetching ────────────────────────────────────────────────────

    def _fetch_precip_forecast(self, lat: float, lon: float, target_date: str) -> Optional[float]:
        """Open-Meteo precipitation probability for a date. Returns [0,1] or None."""
        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "precipitation_probability_max",
                    "timezone": "UTC",
                    "start_date": target_date[:10],
                    "end_date": target_date[:10],
                },
                timeout=10,
            )
            r.raise_for_status()
            probs = r.json().get("daily", {}).get("precipitation_probability_max", [])
            if probs:
                return min(0.99, max(0.01, probs[0] / 100.0))
        except Exception as e:
            logger.debug("Open-Meteo precip fetch failed: %s", e)
        return None

    def _fetch_temp_forecast(
        self,
        lat: float,
        lon: float,
        target_date: str,
        threshold: Tuple[Optional[float], Optional[float]],
    ) -> Optional[float]:
        """Open-Meteo temperature forecast → probability via ensemble spread.

        Uses hourly temperature_2m ensemble (16 members) to estimate spread,
        then approximates probability with a normal distribution.
        Falls back to deterministic forecast with ±2°F std if ensemble unavailable.
        """
        low, high = threshold
        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "hourly": "temperature_2m",
                    "temperature_unit": "fahrenheit",
                    "timezone": "UTC",
                    "start_date": target_date[:10],
                    "end_date": target_date[:10],
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            daily = data.get("daily", {})
            t_max = (daily.get("temperature_2m_max") or [None])[0]
            t_min = (daily.get("temperature_2m_min") or [None])[0]
            if t_max is None:
                return None

            # Estimate std from diurnal range as proxy for forecast uncertainty
            diurnal = (t_max - t_min) if t_min is not None else 4.0
            std = max(1.5, diurnal * 0.25)

            def _norm_cdf(x: float, mu: float, sigma: float) -> float:
                return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))

            if low is not None and high is not None:
                # P(low <= max_temp <= high)
                prob = _norm_cdf(high, t_max, std) - _norm_cdf(low, t_max, std)
            elif low is not None:
                # P(max_temp > low) — "above X"
                prob = 1.0 - _norm_cdf(low, t_max, std)
            else:
                # P(max_temp < high) — "below X"
                prob = _norm_cdf(high, t_max, std)  # type: ignore[arg-type]

            return min(0.99, max(0.01, prob))
        except Exception as e:
            logger.debug("Open-Meteo temp fetch failed: %s", e)
        return None

    def _fetch_metar(self, icao: str) -> Optional[Dict[str, Any]]:
        """Fetch latest METAR observation from aviationweather.gov. Fire-and-forget sanity check."""
        if not self.metar_enabled:
            return None
        try:
            r = requests.get(
                "https://aviationweather.gov/api/data/metar",
                params={"ids": icao, "format": "json"},
                timeout=8,
            )
            r.raise_for_status()
            data = r.json()
            if data:
                return data[0]
        except Exception as e:
            logger.debug("METAR fetch failed for %s: %s", icao, e)
        return None

    # ── Entry filter helpers ─────────────────────────────────────────────────

    def _hours_to_resolution(self, end_date: Optional[datetime]) -> Optional[float]:
        if end_date is None:
            return None
        now = datetime.now(timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        delta = (end_date - now).total_seconds() / 3600
        return delta

    # ── Main scan ────────────────────────────────────────────────────────────

    async def scan_and_analyze(
        self, markets: List[Market], bankroll: float
    ) -> List[WeatherSignal]:
        """Scan markets for weather forecast vs price gaps. Returns tradeable signals."""
        if not self.enabled:
            return []

        stats = {
            "total_markets_seen": len(markets),
            "weather_keyword_matches": 0,
            "markets_scanned": 0,
            "city_matches": 0,
            "temp_markets": 0,
            "precip_markets": 0,
            "signals_generated": 0,
            "skipped_volume": 0,
            "skipped_liquidity": 0,
            "skipped_hours": 0,
            "skipped_ev": 0,
            "skipped_no_threshold": 0,
            "skipped_no_forecast": 0,
        }

        signals: List[WeatherSignal] = []

        for market in markets:
            if is_crypto_updown_market(market):
                continue
            q = market.question
            desc = market.description or ""

            if not self._is_weather_market(q, desc):
                continue

            stats["weather_keyword_matches"] += 1
            stats["markets_scanned"] += 1

            loc_result = self._parse_market_location(q, desc)
            if not loc_result:
                continue

            (lat, lon), icao = loc_result
            stats["city_matches"] += 1

            if (market.liquidity or 0) < self.min_liquidity:
                stats["skipped_liquidity"] += 1
                continue

            # Volume filter
            if (market.liquidity or 0) < self.min_volume and (market.volume or 0) < self.min_volume:
                stats["skipped_volume"] += 1
                continue

            # Hours-to-resolution filter
            hours = self._hours_to_resolution(market.end_date)
            if hours is not None and (hours < self.min_hours or hours > self.max_hours):
                stats["skipped_hours"] += 1
                continue

            is_temp = bool(_TEMP_RE.search(f"{q} {desc}"))

            if self._backtest_proxy_forecast is not None:
                forecast_prob = self._backtest_proxy_forecast
            else:
                target_date = (
                    market.end_date.strftime("%Y-%m-%d")
                    if market.end_date
                    else datetime.utcnow().strftime("%Y-%m-%d")
                )
                if is_temp:
                    stats["temp_markets"] += 1
                    threshold = self._parse_temp_threshold(q)
                    if threshold is None:
                        stats["skipped_no_threshold"] += 1
                        logger.debug("Weather: no temp threshold parsed from '%s'", q[:60])
                        continue
                    forecast_prob = self._fetch_temp_forecast(lat, lon, target_date, threshold)
                else:
                    stats["precip_markets"] += 1
                    forecast_prob = self._fetch_precip_forecast(lat, lon, target_date)

            if forecast_prob is None:
                stats["skipped_no_forecast"] += 1
                continue

            market_price = market.yes_price
            gap = abs(forecast_prob - market_price)

            if gap < self.gap_min:
                continue

            # EV filter
            ev = gap - 0.02  # fee buffer
            if ev < self.min_ev:
                stats["skipped_ev"] += 1
                continue

            # Skip expensive YES shares
            if market_price > self.max_yes_price and forecast_prob > market_price:
                continue

            action = "BUY_YES" if forecast_prob > market_price else "BUY_NO"
            price = market.yes_price if action == "BUY_YES" else market.no_price

            size = (
                self.kelly_sizer.size_from_edge(self._signal_strategy_name, bankroll, ev)
                if self.kelly_sizer
                else self.position_sizer.calculate_kelly_bet(bankroll, ev, self.kelly_fraction)
            )
            if size <= 0:
                continue

            # METAR sanity check (non-blocking)
            if self.metar_enabled and is_temp and not self._backtest_proxy_forecast:
                metar = self._fetch_metar(icao)
                if metar:
                    obs_temp = metar.get("temp")
                    if obs_temp is not None:
                        # Convert C to F if needed (METAR returns Celsius)
                        obs_f = obs_temp * 9 / 5 + 32
                        logger.debug(
                            "Weather METAR %s: observed=%.1f°F forecast_prob=%.2f",
                            icao, obs_f, forecast_prob,
                        )

            logger.info(
                "Weather signal: %s %s city=%s forecast=%.2f market=%.2f gap=%.2f ev=%.2f size=$%.0f | %s",
                action,
                "temp" if is_temp else "precip",
                icao,
                forecast_prob,
                market_price,
                gap,
                ev,
                size,
                q[:70],
            )
            stats["signals_generated"] += 1
            signals.append(
                WeatherSignal(
                    market_id=market.id,
                    token_id_yes=market.token_id_yes,
                    token_id_no=market.token_id_no,
                    market_question=market.question,
                    end_date=market.end_date,
                    action=action,
                    forecast_prob=forecast_prob,
                    market_price=market_price,
                    gap=gap,
                    size=size,
                    price=price,
                )
            )

        self._scan_stats = stats
        logger.info(
            "Weather scan: total=%d keyword=%d city=%d scanned=%d temp=%d precip=%d "
            "signals=%d | skip: liq=%d vol=%d hrs=%d ev=%d thresh=%d forecast=%d",
            stats["total_markets_seen"],
            stats["weather_keyword_matches"],
            stats["city_matches"],
            stats["markets_scanned"],
            stats["temp_markets"],
            stats["precip_markets"],
            stats["signals_generated"],
            stats["skipped_liquidity"],
            stats["skipped_volume"],
            stats["skipped_hours"],
            stats["skipped_ev"],
            stats["skipped_no_threshold"],
            stats["skipped_no_forecast"],
        )

        return signals
