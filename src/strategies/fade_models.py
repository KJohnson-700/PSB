from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Literal

class FadeSignal(BaseModel):
    """
    Represents a signal to bet against extreme market consensus.
    """
    market_id: str = Field(..., description="The unique identifier for the market.")
    token_id_yes: str = Field(..., description="The token ID for the YES outcome.")
    token_id_no: str = Field(..., description="The token ID for the NO outcome.")
    market_question: str = Field(..., description="The question of the prediction market.")
    end_date: Optional[datetime] = Field(None, description="The market's resolution date.")
    action: Literal["BUY_YES", "SELL_YES"] = Field(..., description="The action to take (betting against the consensus).")
    consensus_price: float = Field(..., gt=0, lt=1, description="The extreme price indicating market consensus.")
    ai_confidence: float = Field(..., ge=0, le=1, description="The AI's confidence that the consensus is wrong.")
    implied_probability_gap: float = Field(..., ge=0, le=1, description="The gap between market price and AI's true probability.")
    size: float = Field(..., gt=0, description="The size of the trade in USDC.")
    price: float = Field(..., gt=0, lt=1, description="The price at which to place the limit order.")
