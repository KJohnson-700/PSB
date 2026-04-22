"""
Nothing Ever Happens Strategy

This strategy is based on the observation that in prediction markets, traders tend to
overestimate the probability of dramatic, long-shot events. This strategy identifies
long-term markets where the 'Yes' outcome has a low, but likely still inflated,
probability, and bets against it by selling 'Yes' shares (or buying 'No').

The profit is realized as the time value of the 'Yes' share decays towards zero
as the resolution date approaches and the unlikely event does not occur.
"""
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any

from pydantic import BaseModel, Field

from src.market.scanner import Market
from src.analysis.ai_agent import AIAgent
from src.analysis.math_utils import PositionSizer


class NEHSignal(BaseModel):
    """
    Represents a signal to bet against a dramatic, unlikely event.
    """
    market_id: str = Field(..., description="The unique identifier for the market.")
    market_question: str = Field(..., description="The question the market is resolving.")
    action: str = Field(..., description="The recommended action, e.g., 'SELL_YES'.")
    price: float = Field(..., description="The price at which to execute the trade (e.g., the current 'Yes' price to sell at).")
    size: float = Field(..., description="The suggested position size in shares.")
    edge: float = Field(default=0.0, description="Edge estimate: (1 - price), probability of decay to 0.")
    confidence: float = Field(..., description="The strategy's confidence in this signal.")
    token_id_yes: str = Field(..., description="The token ID for the 'Yes' outcome.")
    token_id_no: str = Field(..., description="The token ID for the 'No' outcome.")
    end_date: datetime = Field(..., description="The resolution date of the market.")


class NothingEverHappensStrategy:
    """
    Implements the 'Nothing Ever Happens' trading strategy.
    """
    def __init__(self, config: Dict[str, Any], ai_agent: AIAgent, position_sizer: PositionSizer):
        self.config = config.get('strategies', {}).get('neh', {})
        self.ai_agent = ai_agent
        self.position_sizer = position_sizer
        
        # Strategy-specific parameters
        self.enabled = self.config.get('enabled', False)
        self.min_days_to_resolution = self.config.get('min_days_to_resolution', 30)
        self.max_yes_price = self.config.get('max_yes_price', 0.08)
        self.min_liquidity = self.config.get('min_liquidity', 10000)
        self.max_positions_per_tournament = self.config.get('max_positions_per_tournament', 2)
        self.max_positions_neh = self.config.get('max_positions_neh', 25)

    async def scan_and_analyze(self, markets: List[Market], bankroll: float,
                               current_neh_count: int = 0) -> List[NEHSignal]:
        """
        Scans a list of markets and generates NEH signals if conditions are met.

        Args:
            current_neh_count: Number of already-open NEH positions (from position tracker).
                               This is the REAL count, not the local signal list length.
        """
        if not self.enabled:
            return []

        signals = []

        # NEH total position cap — check ACTUAL open positions, not local signal list.
        # BUG FIX: was checking len(signals) which is always 0 at this point.
        # Live data: NEH accumulated 37 positions vs 25 cap because of this bug.
        if current_neh_count >= self.max_positions_neh:
            return signals

        # Tournament bracket tracker — cap positions per bracket to avoid correlated concentration.
        # e.g. holding 17 NHL Cup positions means one team advancing tanks all of them together.
        _TOURNAMENT_PATTERNS = [
            (r'NHL Stanley Cup', 'nhl_cup'),
            (r'NBA Finals', 'nba_finals'),
            (r'NBA.*Conference Final', 'nba_conf'),
            (r'FIFA World Cup', 'fifa_wc'),
            (r'Champions League', 'ucl'),
            (r'La Liga', 'la_liga'),
            (r'Premier League', 'epl'),
            # Politics — 26/37 open positions were correlated 2028 politics with no bracket cap
            (r'Democratic.*(?:nomination|presidential)', 'dem_2028'),
            (r'Republican.*(?:nomination|presidential)', 'gop_2028'),
            (r'Presidential Election', 'pres_election'),
        ]
        tournament_counts: Dict[str, int] = {}

        # Define the time threshold for a 'long-term' market
        long_term_threshold = datetime.now() + timedelta(days=self.min_days_to_resolution)

        # How many more NEH positions we can open this cycle
        _remaining_slots = self.max_positions_neh - current_neh_count

        for market in markets:
            # Hard cap: stop generating signals once we've filled all remaining slots
            if len(signals) >= _remaining_slots:
                break

            # --- Filtering Criteria for NEH Strategy ---
            # 1. Market resolves in the distant future (skip if no end_date)
            if market.end_date is None or market.end_date < long_term_threshold:
                continue

            # 2. 'Yes' price is low, indicating an unlikely event
            if market.yes_price > self.max_yes_price:
                continue

            # 3. Sufficient liquidity (skip check when liquidity is unknown/zero in backtest)
            if market.liquidity > 0 and market.liquidity < self.min_liquidity:
                continue

            # If all criteria are met, we have a potential signal
            # The confidence is derived from how far in the future the market resolves.
            # A simple model: confidence increases the further out the date is.
            days_out = (market.end_date - datetime.now()).days
            confidence = min(1.0, (days_out / 365.0) * 0.8) # Cap confidence, normalize by a year

            # ── Tournament concentration cap ──
            # Cap positions per bracket to avoid correlated blowups (e.g. 17 NHL Cup positions
            # all moving against when a team advances). Limit to max_positions_per_tournament.
            _bracket = None
            for pattern, bracket_key in _TOURNAMENT_PATTERNS:
                if re.search(pattern, market.question, re.IGNORECASE):
                    _bracket = bracket_key
                    break
            if _bracket:
                current_count = tournament_counts.get(_bracket, 0)
                if current_count >= self.max_positions_per_tournament:
                    continue  # Skip: already at cap for this tournament
                tournament_counts[_bracket] = current_count + 1

            # This strategy is about selling the overpriced 'Yes' shares
            action = "SELL_YES"
            price = market.yes_price
            # Edge is the expected decay from current price toward zero.
            # Using (1 - price) was wrong — that gives ~0.99 for a 1% market,
            # causing Kelly to oversize massively (e.g. $31 to earn $0.34).
            # Correct: edge is proportional to how far the price can fall,
            # capped at 5% since decay from ~1% to 0% is a small absolute gain.
            edge = round(min(price * 2, 0.05), 4)

            # Position size is determined by the bankroll and confidence
            # For NEH, we might use a more conservative sizing model
            size = self.position_sizer.calculate_conservative_size(
                bankroll=bankroll,
                confidence=confidence,
                edge=edge
            )

            if size > 0:
                signal = NEHSignal(
                    market_id=market.id,
                    market_question=market.question,
                    action=action,
                    price=price,
                    size=size,
                    edge=edge,
                    confidence=confidence,
                    token_id_yes=market.token_id_yes,
                    token_id_no=market.token_id_no,
                    end_date=market.end_date
                )
                signals.append(signal)

        return signals

