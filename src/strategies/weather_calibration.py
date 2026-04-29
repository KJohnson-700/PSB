"""Persistent per-city weather forecast calibration."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

CALIBRATION_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "weather_calibration.json"
)


class WeatherCalibrationStore:
    """Stores resolved weather forecast observations keyed by city+horizon."""

    def __init__(
        self,
        path: Optional[Path] = None,
        min_observations: int = 30,
    ) -> None:
        self.path = path or CALIBRATION_PATH
        self.min_observations = max(1, int(min_observations))
        self._data = self._load()

    @staticmethod
    def _bucket_key(city: str, horizon_days: int) -> str:
        return f"{city}|{int(horizon_days)}"

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"buckets": {}}
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f) or {}
            if not isinstance(data, dict):
                return {"buckets": {}}
            data.setdefault("buckets", {})
            return data
        except (OSError, ValueError, TypeError) as e:
            logger.warning("Weather calibration load failed: %s", e)
            return {"buckets": {}}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
        except OSError as e:
            logger.warning("Weather calibration save failed: %s", e)

    def get_bias(self, city: str, horizon_days: int) -> Tuple[float, int]:
        bucket = self._data.get("buckets", {}).get(self._bucket_key(city, horizon_days), {})
        count = int(bucket.get("count", 0) or 0)
        bias = float(bucket.get("bias", 0.0) or 0.0)
        return bias, count

    def apply_correction(self, raw_forecast: float, city: str, horizon_days: int) -> Tuple[float, float, int]:
        bias, count = self.get_bias(city, horizon_days)
        if count < self.min_observations:
            return raw_forecast, 0.0, count
        corrected = min(0.99, max(0.01, raw_forecast - bias))
        return corrected, bias, count

    def record_observation(
        self,
        *,
        city: str,
        horizon_days: int,
        raw_forecast_prob: float,
        actual_outcome: float,
        gap_used: float,
    ) -> None:
        bucket_key = self._bucket_key(city, horizon_days)
        buckets = self._data.setdefault("buckets", {})
        bucket = buckets.setdefault(
            bucket_key,
            {
                "city": city,
                "horizon_days": int(horizon_days),
                "count": 0,
                "bias": 0.0,
                "observations": [],
            },
        )
        observations = bucket.setdefault("observations", [])
        observations.append(
            {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "raw_forecast_prob": round(float(raw_forecast_prob), 6),
                "actual_outcome": round(float(actual_outcome), 6),
                "gap_used": round(float(gap_used), 6),
            }
        )
        count = len(observations)
        bias = sum(
            float(o.get("raw_forecast_prob", 0.0)) - float(o.get("actual_outcome", 0.0))
            for o in observations
        ) / count
        bucket["count"] = count
        bucket["bias"] = round(bias, 6)
        self._save()

