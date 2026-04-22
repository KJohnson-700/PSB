"""
Weather strategy: compare NOAA/Open-Meteo forecast vs Polymarket price.
If gap > 15¢, enter on the forecast side. Documented: $1K→$24K London, $65K NYC/London/Seoul.
"""

import logging
import re
from datetime import datetime
from typing import Dict, List, Any, Optional

from src.market.scanner import Market
from src.analysis.math_utils import PositionSizer
from src.strategies.weather_models import WeatherSignal

logger = logging.getLogger(__name__)

# City name -> (lat, lon) for Open-Meteo
CITY_COORDS: Dict[str, tuple] = {
    "new york": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "london": (51.5074, -0.1278),
    "seoul": (37.5665, 126.9780),
    "los angeles": (34.0522, -118.2437),
    "la": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "miami": (25.7617, -80.1918),
    "paris": (48.8566, 2.3522),
    "tokyo": (35.6762, 139.6503),
}


class WeatherStrategy:
    """Trade when forecast probability diverges from market price by >= gap_min."""

    def __init__(self, config: Dict[str, Any], position_sizer: PositionSizer, kelly_sizer=None):
        self.config = config.get("strategies", {}).get("weather", {})
        self.position_sizer = position_sizer
        self.kelly_sizer = kelly_sizer
        self.gap_min = self.config.get("gap_min", 0.15)
        self.enabled = self.config.get("enabled", False)
        self.kelly_fraction = self.config.get("kelly_fraction", 0.25)
        self.max_position_pct = self.config.get("max_exposure_per_trade", 0.05)
        self._signal_strategy_name = "weather"
        self._backtest_proxy_forecast: Optional[float] = None

    def _parse_market_location(
        self, question: str, description: str
    ) -> Optional[tuple]:
        """Extract (lat, lon) from market question/description. Returns None if not parseable."""
        text = f"{question} {description}".lower()
        for city, coords in CITY_COORDS.items():
            if city in text:
                return coords
        return None

    def _fetch_forecast(
        self, lat: float, lon: float, target_date: str
    ) -> Optional[float]:
        """Fetch Open-Meteo forecast probability for precipitation. Returns YES prob or None."""
        try:
            import requests

            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": lat,
                "longitude": lon,
                "daily": "precipitation_probability_max",
                "timezone": "UTC",
                "start_date": target_date[:10],
                "end_date": target_date[:10],
            }
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            daily = data.get("daily", {})
            probs = daily.get("precipitation_probability_max", [])
            if probs:
                return min(0.99, max(0.01, probs[0] / 100.0))
        except Exception as e:
            logger.debug("Open-Meteo fetch failed: %s", e)
        return None

    async def scan_and_analyze(
        self, markets: List[Market], bankroll: float
    ) -> List[WeatherSignal]:
        """Scan weather markets; return signals when forecast vs price gap >= gap_min."""
        if not self.enabled:
            return []

        signals = []
        for market in markets:
            loc = self._parse_market_location(market.question, market.description or "")
            if not loc:
                continue

            # Use proxy forecast if set (backtest mode), otherwise fetch live
            if self._backtest_proxy_forecast is not None:
                forecast_prob = self._backtest_proxy_forecast
            else:
                # Use end_date or "today" for forecast target
                target = getattr(market.end_date, "strftime", None)
                target_date = (
                    target("%Y-%m-%d")
                    if target and market.end_date
                    else datetime.utcnow().strftime("%Y-%m-%d")
                )
                forecast_prob = self._fetch_forecast(loc[0], loc[1], target_date)
            if forecast_prob is None:
                continue

            market_price = market.yes_price
            gap = abs(forecast_prob - market_price)
            if gap < self.gap_min:
                continue

            # Bet on forecast side
            if forecast_prob > market_price:
                action = "BUY_YES"
                price = market.yes_price
            else:
                action = "BUY_NO"
                price = market.no_price

            edge = gap - 0.02  # fee buffer
            size = self.kelly_sizer.size_from_edge(
                self._signal_strategy_name, bankroll, edge
            ) if self.kelly_sizer else self.position_sizer.calculate_kelly_bet(
                bankroll, edge, self.kelly_fraction
            )
            if size <= 0:
                continue

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

        return signals
