from __future__ import annotations

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
from typing import TYPE_CHECKING, Dict, List, Any, Optional, Tuple, Literal

import requests

from src.market.scanner import Market, is_crypto_updown_market
from src.analysis.math_utils import PositionSizer
from src.analysis.weather_ensemble import WeatherEnsembleRunner
from src.strategies.weather_calibration import WeatherCalibrationStore
from src.strategies.weather_models import WeatherSignal

if TYPE_CHECKING:
    from src.analysis.ai_agent import AIAgent

logger = logging.getLogger(__name__)


def _normalize_city_text(value: str) -> str:
    """Normalize market text for resilient city and airport matching."""
    value = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return re.sub(r"\s+", " ", value).strip()


_CITY_LOOKUP: Dict[str, Tuple[tuple, str]] = {
    "new-york": ((40.7769, -73.8740), "KLGA"),
    "nyc": ((40.7769, -73.8740), "KLGA"),
    "jfk": ((40.6413, -73.7781), "KJFK"),
    "laguardia": ((40.7769, -73.8740), "KLGA"),
    "los-angeles": ((33.9425, -118.4081), "KLAX"),
    "lax": ((33.9425, -118.4081), "KLAX"),
    "chicago": ((41.9742, -87.9073), "KORD"),
    "ohare": ((41.9742, -87.9073), "KORD"),
    "o-hare": ((41.9742, -87.9073), "KORD"),
    "miami": ((25.7959, -80.2870), "KMIA"),
    "dallas": ((32.8481, -96.8512), "KDAL"),
    "denver": ((39.8561, -104.6737), "KDEN"),
    "phoenix": ((33.4342, -112.0116), "KPHX"),
    "boston": ((42.3656, -71.0096), "KBOS"),
    "seattle": ((47.4502, -122.3088), "KSEA"),
    "atlanta": ((33.6407, -84.4277), "KATL"),
    "houston": ((29.9902, -95.3368), "KIAH"),
    "london": ((51.4700, -0.4543), "EGLL"),
    "heathrow": ((51.4700, -0.4543), "EGLL"),
    "tokyo": ((35.5494, 139.7798), "RJTT"),
    "seoul": ((37.4691, 126.4510), "RKSS"),
    "paris": ((49.0097, 2.5479), "LFPB"),
    "sydney": ((-33.9399, 151.1753), "YSSY"),
    "singapore": ((1.3644, 103.9915), "WSSS"),
    "dubai": ((25.2532, 55.3657), "OMDB"),
    "manila": ((14.5086, 121.0198), "RPLL"),
    "karachi": ((24.9065, 67.1608), "OPKC"),
    "dhaka": ((23.8433, 90.3978), "VGHS"),
    "mumbai": ((19.0896, 72.8656), "VABB"),
    "delhi": ((28.5562, 77.1000), "VIDP"),
    "bangkok": ((13.6900, 100.7501), "VTBS"),
    "jakarta": ((-6.1256, 106.6559), "WIII"),
    "hong-kong": ((22.3080, 113.9185), "VHHH"),
    "shanghai": ((31.1443, 121.8083), "ZSPD"),
    "beijing": ((40.0799, 116.6031), "ZBAA"),
}

_CITY_ALIASES: Dict[str, str] = {
    "nyc": "new-york",
    "laguardia": "new-york",
    "ohare": "chicago",
    "o-hare": "chicago",
    "heathrow": "london",
}
_ICAO_TO_CITY_KEY: Dict[str, str] = {}
for _city_key, (_coords, _icao) in _CITY_LOOKUP.items():
    _ICAO_TO_CITY_KEY.setdefault(_icao, _CITY_ALIASES.get(_city_key, _city_key))

_CITY_VARIANTS: Dict[str, str] = {}
for _city_key, (_coords, _icao) in _CITY_LOOKUP.items():
    _canonical = _CITY_ALIASES.get(_city_key, _city_key)
    for _variant in (
        _city_key,
        _city_key.replace("-", " "),
        _icao,
        _icao.lower(),
    ):
        _norm = _normalize_city_text(_variant)
        if _norm:
            _CITY_VARIANTS[_norm] = _canonical

for _variant, _canonical in {
    "new york city": "new-york",
    "new york ny": "new-york",
    "new york new york": "new-york",
    "la guardia": "new-york",
    "la guardia airport": "new-york",
    "lga": "new-york",
    "jfk airport": "new-york",
    "john f kennedy": "new-york",
    "john f kennedy airport": "new-york",
    "boston ma": "boston",
    "boston logan": "boston",
    "boston logan airport": "boston",
    "logan": "boston",
    "logan airport": "boston",
    "bos": "boston",
    "chicago il": "chicago",
    "chicago o hare": "chicago",
    "chicago o hare airport": "chicago",
    "o hare": "chicago",
    "o hare airport": "chicago",
    "ord": "chicago",
}.items():
    _CITY_VARIANTS[_normalize_city_text(_variant)] = _canonical

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
    (re.compile(r'\bparis\b', re.I),                 (49.0097, 2.5479),    "LFPB"),
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
_PRECIP_RE = re.compile(r'\b(rain|snow|precipitation|storm|hail|thunderstorm)\b', re.I)


class WeatherStrategy:
    """Trade when Open-Meteo airport-station forecast diverges from market price."""

    def __init__(
        self,
        config: Dict[str, Any],
        position_sizer: PositionSizer,
        kelly_sizer=None,
        ai_agent: Optional[AIAgent] = None,
    ):
        cfg = config.get("strategies", {}).get("weather", {})
        self.config = cfg
        self.position_sizer = position_sizer
        self.kelly_sizer = kelly_sizer
        self.ai_agent = ai_agent
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
        self.metar_mismatch_threshold_c = float(
            cfg.get("metar_mismatch_threshold_c", 3.0)
        )
        self.metar_mismatch_threshold_c_per_day = float(
            cfg.get("metar_mismatch_threshold_c_per_day", 1.0)
        )
        self.ev_fee_buffer_pct = float(cfg.get("ev_fee_buffer_pct", 0.02))
        self.min_ev_horizon_multipliers = {
            1: float(cfg.get("min_ev_mult_t1", 1.2)),
            2: float(cfg.get("min_ev_mult_t2", 1.0)),
            3: float(cfg.get("min_ev_mult_t3", 0.8)),
        }
        self.use_weather_ai = bool(cfg.get("use_weather_ai", False))
        self.max_weather_ai_calls_per_scan = int(
            cfg.get("max_weather_ai_calls_per_scan", 3)
        )
        self.borderline_high = float(cfg.get("borderline_high", 0.10))
        self.calibration_store = WeatherCalibrationStore(
            min_observations=int(cfg.get("calibration_min_observations", 30))
        )
        self.weather_ensemble = WeatherEnsembleRunner(ai_agent, config)
        # Scan diagnostics reset each cycle
        self._scan_stats: Dict[str, int] = {}

    # ── Market classification ────────────────────────────────────────────────

    def _is_weather_market(self, question: str, description: str) -> bool:
        return bool(_WEATHER_RE.search(f"{question} {description}"))

    def _parse_market_location(
        self, question: str, description: str
    ) -> Optional[Tuple[tuple, str]]:
        """Return ((lat, lon), icao) or None."""
        details = self._parse_market_location_details(question, description)
        if details is None:
            return None
        _, coords, icao = details
        return coords, icao

    def _parse_market_location_details(
        self, question: str, description: str
    ) -> Optional[Tuple[str, tuple, str]]:
        """Return (city_key, (lat, lon), icao) or None."""
        text = f"{question} {description}"
        for pattern, coords, icao in _CITY_PATTERNS:
            if pattern.search(text):
                return _ICAO_TO_CITY_KEY.get(icao, icao.lower()), coords, icao

        for candidate in self._iter_location_candidates(question, description):
            match = self._match_city_variant(candidate)
            if match is None:
                continue
            canonical_city, coords, icao = match
            return canonical_city, coords, icao
        return None

    @staticmethod
    def _iter_location_candidates(question: str, description: str) -> List[str]:
        candidates = [question, description]
        combined = f"{question} {description}"

        temp_slug_match = re.search(
            r"highest-temperature-in-([a-z0-9-]+?)-on-",
            combined,
            re.IGNORECASE,
        )
        if temp_slug_match:
            candidates.append(temp_slug_match.group(1).replace("-", " "))

        precip_slug_match = re.search(
            r"will-([a-z0-9-]+?)-have-.*(?:precipitation|rain|snow)",
            combined,
            re.IGNORECASE,
        )
        if precip_slug_match:
            candidates.append(precip_slug_match.group(1).replace("-", " "))

        return [candidate for candidate in candidates if candidate]

    @staticmethod
    def _match_city_variant(candidate: str) -> Optional[Tuple[str, tuple, str]]:
        normalized = _normalize_city_text(candidate)
        if not normalized:
            return None

        padded = f" {normalized} "
        for variant in sorted(_CITY_VARIANTS, key=len, reverse=True):
            if f" {variant} " not in padded:
                continue
            canonical_city = _CITY_VARIANTS[variant]
            coords, icao = _CITY_LOOKUP[canonical_city]
            return canonical_city, coords, icao
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
    ) -> Optional[Any]:
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

            return {
                "prob": min(0.99, max(0.01, prob)),
                "forecast_temp_f": float(t_max),
                "std_f": float(std),
            }
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

    def _target_forecast_date(self, market: Market) -> tuple[Optional[str], Optional[str]]:
        """Choose T+1/T+2/T+3 based on time-to-resolution."""
        target_date, _, skip_reason = self._target_forecast_date_details(market)
        return target_date, skip_reason

    def _target_forecast_date_details(
        self, market: Market
    ) -> tuple[Optional[str], Optional[int], Optional[str]]:
        """Choose target date and horizon_days based on time-to-resolution."""
        if self._backtest_proxy_forecast is not None:
            return None, None, None

        hours = self._hours_to_resolution(market.end_date)
        if hours is not None:
            if hours < self.min_hours:
                return None, None, "skipped_below_min_hours"
            if hours > self.max_hours:
                return None, None, "skipped_above_max_hours"
            if hours < 24:
                horizon_days = 1
            elif hours < 72:
                horizon_days = 2
            else:
                horizon_days = 3
        else:
            horizon_days = 1

        max_horizon_days = max(1, int(self.config.get("forecast_horizon_days", 3)))
        if horizon_days > max_horizon_days:
            return None, horizon_days, "skipped_too_far_out"

        target_date = (datetime.now(timezone.utc) + timedelta(days=horizon_days)).strftime("%Y-%m-%d")
        return target_date, horizon_days, None

    @staticmethod
    def _clamp_prob(value: float) -> float:
        return min(0.99, max(0.01, value))

    def _classify_weather_subtype(
        self, question: str, description: str
    ) -> Optional[Literal["temp", "precip"]]:
        """Assign one stable subtype for metrics/journaling.

        Some markets can contain overlapping weather language. Prefer temperature
        when a temperature threshold is explicitly parseable; otherwise use
        precipitation when precip keywords are present.
        """
        blob = f"{question} {description}"
        is_temp = bool(_TEMP_RE.search(blob))
        is_precip = bool(_PRECIP_RE.search(blob))
        if is_temp and self._parse_temp_threshold(question) is not None:
            return "temp"
        if is_precip:
            return "precip"
        if is_temp:
            return "temp"
        return None

    def _fee_buffer(self, contract_price: float) -> float:
        """Use a price-scaled EV haircut instead of a flat cents buffer."""
        price = max(0.01, min(0.99, float(contract_price)))
        return price * self.ev_fee_buffer_pct

    def _required_min_ev(self, horizon_days: Optional[int]) -> float:
        day = max(1, min(3, int(horizon_days or 1)))
        mult = self.min_ev_horizon_multipliers.get(day, 1.0)
        return self.min_ev * mult

    def _metar_threshold_f(self, horizon_days: Optional[int]) -> float:
        day = max(1, int(horizon_days or 1))
        threshold_c = self.metar_mismatch_threshold_c + max(0, day - 1) * (
            self.metar_mismatch_threshold_c_per_day
        )
        return threshold_c * 9 / 5

    @staticmethod
    def _side_probability_and_price(
        forecast_yes_prob: float,
        market_yes_price: float,
    ) -> tuple[str, float, float]:
        if forecast_yes_prob > market_yes_price:
            return "BUY_YES", forecast_yes_prob, market_yes_price
        return "BUY_NO", 1.0 - forecast_yes_prob, 1.0 - market_yes_price

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
            "skipped_no_location": 0,
            "skipped_no_temp_keyword": 0,
            "skipped_too_far_out": 0,
            "skipped_below_min_hours": 0,
            "skipped_above_max_hours": 0,
            "skipped_extreme_consensus": 0,
            "skipped_below_liquidity": 0,
            "skipped_below_volume": 0,
            "skipped_ev": 0,
            "skipped_no_threshold": 0,
            "skipped_no_forecast": 0,
            "skipped_metar_mismatch": 0,
            "weather_ai_calls": 0,
            "weather_ai_applied": 0,
            "weather_ai_hold": 0,
            "sample_market_questions": [],
            "sample_rejected_questions": [],
        }

        signals: List[WeatherSignal] = []
        ai_calls = 0
        weather_markets = [market for market in markets if not is_crypto_updown_market(market)]
        weather_markets.sort(
            key=lambda market: float((market.volume or 0) + (market.liquidity or 0)),
            reverse=True,
        )

        for market in weather_markets:
            q = market.question
            desc = market.description or ""

            if not self._is_weather_market(q, desc):
                continue

            stats["weather_keyword_matches"] += 1
            stats["markets_scanned"] += 1
            if len(stats["sample_market_questions"]) < 5:
                stats["sample_market_questions"].append(q[:120])

            loc_result = self._parse_market_location_details(
                q,
                f"{desc} {market.slug or ''} {market.group_item_title or ''}",
            )
            if not loc_result:
                stats["skipped_no_location"] += 1
                if len(stats["sample_rejected_questions"]) < 5:
                    stats["sample_rejected_questions"].append(f"no_location: {q[:100]}")
                continue

            city_key, (lat, lon), icao = loc_result
            stats["city_matches"] += 1

            if market.yes_price <= 0.05 or market.yes_price >= 0.95:
                stats["skipped_extreme_consensus"] += 1
                continue

            if (market.liquidity or 0) < self.min_liquidity:
                stats["skipped_below_liquidity"] += 1
                continue

            # Volume filter
            if (market.liquidity or 0) < self.min_volume and (market.volume or 0) < self.min_volume:
                stats["skipped_below_volume"] += 1
                continue

            target_date, horizon_days, skip_reason = self._target_forecast_date_details(market)
            if skip_reason:
                stats[skip_reason] += 1
                continue

            subtype = self._classify_weather_subtype(q, desc)
            if subtype is None:
                stats["skipped_no_temp_keyword"] += 1
                if len(stats["sample_rejected_questions"]) < 5:
                    stats["sample_rejected_questions"].append(f"no_keyword: {q[:100]}")
                continue

            forecast_temp_f = None
            forecast_std_f = None
            if self._backtest_proxy_forecast is not None:
                raw_forecast_prob = self._backtest_proxy_forecast
            else:
                if subtype == "temp":
                    stats["temp_markets"] += 1
                    threshold = self._parse_temp_threshold(q)
                    if threshold is None:
                        stats["skipped_no_threshold"] += 1
                        logger.debug("Weather: no temp threshold parsed from '%s'", q[:60])
                        continue
                    temp_result = self._fetch_temp_forecast(lat, lon, target_date, threshold)
                    if isinstance(temp_result, dict):
                        raw_forecast_prob = temp_result.get("prob")
                        forecast_temp_f = temp_result.get("forecast_temp_f")
                        forecast_std_f = temp_result.get("std_f")
                    else:
                        raw_forecast_prob = temp_result
                else:
                    stats["precip_markets"] += 1
                    raw_forecast_prob = self._fetch_precip_forecast(
                        lat,
                        lon,
                        target_date or datetime.utcnow().strftime("%Y-%m-%d"),
                    )

            if raw_forecast_prob is None:
                stats["skipped_no_forecast"] += 1
                continue

            corrected_forecast_prob, calibration_bias, calibration_count = (
                self.calibration_store.apply_correction(
                    float(raw_forecast_prob),
                    city_key,
                    int(horizon_days or 1),
                )
            )

            market_price = market.yes_price
            metar_obs_f = None

            if (
                self.metar_enabled
                and subtype == "temp"
                and not self._backtest_proxy_forecast
                and forecast_temp_f is not None
            ):
                metar = self._fetch_metar(icao)
                if metar:
                    obs_temp = metar.get("temp")
                    if obs_temp is not None:
                        obs_f = float(obs_temp) * 9 / 5 + 32
                        metar_obs_f = obs_f
                        metar_gap_f = abs(obs_f - float(forecast_temp_f))
                        threshold_f = self._metar_threshold_f(horizon_days)
                        if metar_gap_f > threshold_f:
                            stats["skipped_metar_mismatch"] += 1
                            if len(stats["sample_rejected_questions"]) < 5:
                                stats["sample_rejected_questions"].append(
                                    f"metar_mismatch: {q[:100]}"
                                )
                            logger.info(
                                "Weather METAR mismatch skip: %s obs=%.1fF forecast=%.1fF gap=%.1fF thresh=%.1fF",
                                icao,
                                obs_f,
                                forecast_temp_f,
                                metar_gap_f,
                                threshold_f,
                            )
                            continue
                        logger.debug(
                            "Weather METAR %s: observed=%.1fF forecast=%.1fF prob=%.2f",
                            icao,
                            obs_f,
                            forecast_temp_f,
                            corrected_forecast_prob,
                        )

            effective_forecast_prob = corrected_forecast_prob
            ensemble_payload = None
            gap = abs(effective_forecast_prob - market_price)

            if gap < self.gap_min:
                continue

            in_borderline_zone = gap <= (self.gap_min + self.borderline_high)
            if (
                in_borderline_zone
                and self.use_weather_ai
                and self.ai_agent
                and self.ai_agent.is_available()
                and ai_calls < self.max_weather_ai_calls_per_scan
            ):
                hours_to_resolution = self._hours_to_resolution(market.end_date)
                ensemble = await self.weather_ensemble.run(
                    market_id=market.id,
                    question=q,
                    description=desc,
                    subtype=subtype,
                    city_key=city_key,
                    icao=icao,
                    horizon_days=int(horizon_days or 1),
                    horizon_hours=hours_to_resolution,
                    forecast_prob=corrected_forecast_prob,
                    raw_forecast_prob=float(raw_forecast_prob),
                    market_price=market_price,
                    calibration_bias=calibration_bias,
                    calibration_count=calibration_count,
                    forecast_temp_f=forecast_temp_f,
                    metar_obs_f=metar_obs_f,
                    ensemble_std_f=forecast_std_f,
                )
                ai_calls += 1
                stats["weather_ai_calls"] += 1
                if ensemble is not None:
                    ensemble_payload = ensemble.to_signal_payload()
                    if ensemble.recommendation == "HOLD":
                        stats["weather_ai_hold"] += 1
                        continue
                    effective_forecast_prob = ensemble.estimated_probability
                    stats["weather_ai_applied"] += 1
                    gap = abs(effective_forecast_prob - market_price)
                    if gap < self.gap_min:
                        continue

            action, win_probability, contract_price = self._side_probability_and_price(
                effective_forecast_prob,
                market_price,
            )

            fee_buffer = self._fee_buffer(contract_price)
            ev = gap - fee_buffer
            required_min_ev = self._required_min_ev(horizon_days)
            if ev < required_min_ev:
                stats["skipped_ev"] += 1
                continue

            # Skip expensive YES shares
            if market_price > self.max_yes_price and corrected_forecast_prob > market_price:
                continue

            price = contract_price

            size = (
                self.kelly_sizer.size_binary_position(
                    self._signal_strategy_name,
                    bankroll,
                    win_probability,
                    contract_price,
                )
                if self.kelly_sizer
                else self.position_sizer.calculate_binary_kelly_bet(
                    bankroll,
                    win_probability,
                    contract_price,
                    self.kelly_fraction,
                )
            )
            if size <= 0:
                continue

            logger.info(
                "Weather signal: %s %s city=%s forecast=%.2f market=%.2f gap=%.2f ev=%.2f fee=%.3f req_ev=%.2f size=$%.0f | %s",
                action,
                subtype,
                icao,
                effective_forecast_prob,
                market_price,
                gap,
                ev,
                fee_buffer,
                required_min_ev,
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
                    subtype=subtype,
                    forecast_prob=effective_forecast_prob,
                    market_price=market_price,
                    gap=gap,
                    size=size,
                    price=price,
                    city=city_key,
                    horizon_days=int(horizon_days or 1),
                    raw_forecast_prob=self._clamp_prob(float(raw_forecast_prob)),
                    calibration_bias=calibration_bias,
                    calibration_count=calibration_count,
                    ai_ensemble=ensemble_payload,
                )
            )

        self._scan_stats = stats
        logger.info(
            "Weather scan: total=%d keyword=%d city=%d scanned=%d temp=%d precip=%d "
            "signals=%d | skip: no_loc=%d no_temp=%d liq=%d vol=%d minh=%d maxh=%d far=%d "
            "extreme=%d ev=%d thresh=%d forecast=%d metar=%d ai_calls=%d ai_used=%d ai_hold=%d",
            stats["total_markets_seen"],
            stats["weather_keyword_matches"],
            stats["city_matches"],
            stats["markets_scanned"],
            stats["temp_markets"],
            stats["precip_markets"],
            stats["signals_generated"],
            stats["skipped_no_location"],
            stats["skipped_no_temp_keyword"],
            stats["skipped_below_liquidity"],
            stats["skipped_below_volume"],
            stats["skipped_below_min_hours"],
            stats["skipped_above_max_hours"],
            stats["skipped_too_far_out"],
            stats["skipped_extreme_consensus"],
            stats["skipped_ev"],
            stats["skipped_no_threshold"],
            stats["skipped_no_forecast"],
            stats["skipped_metar_mismatch"],
            stats["weather_ai_calls"],
            stats["weather_ai_applied"],
            stats["weather_ai_hold"],
        )
        if stats["sample_market_questions"]:
            logger.info("Weather scan sample markets: %s", stats["sample_market_questions"])
        if stats["sample_rejected_questions"]:
            logger.info("Weather scan sample rejects: %s", stats["sample_rejected_questions"])

        return signals
