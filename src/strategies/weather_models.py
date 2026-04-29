"""Models for weather strategy signals."""
from typing import Any, Dict, Optional
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Literal


class WeatherSignal(BaseModel):
    """Signal from weather forecast vs market price comparison."""
    market_id: str = Field(..., description="Market identifier.")
    token_id_yes: str = Field(..., description="YES token ID.")
    token_id_no: str = Field(..., description="NO token ID.")
    market_question: str = Field(..., description="Market question.")
    end_date: Optional[datetime] = Field(None, description="Resolution date.")
    action: Literal["BUY_YES", "BUY_NO"] = Field(..., description="Side to buy.")
    subtype: Literal["temp", "precip"] = Field(..., description="Weather market subtype.")
    forecast_prob: float = Field(..., ge=0, le=1, description="Forecast probability for YES.")
    market_price: float = Field(..., ge=0, le=1, description="Current market YES price.")
    gap: float = Field(..., ge=0, description="|forecast_prob - market_price|.")
    size: float = Field(..., gt=0, description="Trade size in USDC.")
    price: float = Field(..., gt=0, lt=1, description="Limit order price.")
    city: Optional[str] = Field(None, description="Canonical city key for calibration.")
    horizon_days: Optional[int] = Field(None, description="Forecast horizon bucket used for calibration.")
    raw_forecast_prob: Optional[float] = Field(None, ge=0, le=1, description="Uncorrected forecast probability.")
    calibration_bias: float = Field(0.0, description="Applied city+horizon calibration bias.")
    calibration_count: int = Field(0, description="Observation count behind the applied calibration bias.")
    ai_ensemble: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional weather AI ensemble decision payload for borderline markets.",
    )
