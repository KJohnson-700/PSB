"""
Math Utilities Module
Position sizing, Kelly criterion, EV calculations
"""

import logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PositionSize:
    """Position sizing recommendation"""

    amount: float
    edge: float
    kelly_fraction: float
    reasoning: str


class PositionSizer:
    """Calculates optimal position sizes using Kelly Criterion"""

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        max_position_pct: float = 0.05,
        min_position: float = 1.0,
        max_position: float = 200.0,
    ):
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct
        self.min_position = min_position
        self.max_position = max_position

    def kelly_criterion(
        self, win_probability: float, odds: float, fraction: float = None
    ) -> float:
        """
        Calculate Kelly Criterion for position sizing

        Formula: f = (bp - q) / b
        where:
            b = odds received (decimal odds - 1)
            p = probability of winning
            q = probability of losing (1 - p)

        Args:
            win_probability: Probability of winning (0.0 - 1.0)
            odds: Decimal odds (e.g., 2.0 for even money)
            fraction: Kelly fraction to use (default: self.kelly_fraction)

        Returns:
            Fraction of bankroll to bet
        """
        if fraction is None:
            fraction = self.kelly_fraction

        p = win_probability
        q = 1 - p
        b = odds - 1

        if b <= 0:
            return 0.0

        # Kelly formula
        kelly = (b * p - q) / b

        # Apply fractional Kelly
        kelly = kelly * fraction

        # Ensure non-negative (no betting when negative edge)
        return max(0.0, kelly)

    def calculate_position_size(
        self,
        bankroll: float,
        win_probability: float,
        current_price: float,
        ai_estimated_prob: float,
        max_exposure_pct: float = None,
    ) -> PositionSize:
        """
        Calculate optimal position size

        Args:
            bankroll: Total available capital
            win_probability: AI estimated probability of winning
            current_price: Current market price
            ai_estimated_prob: AI's estimated true probability
            max_exposure_pct: Maximum exposure as % of bankroll

        Returns:
            PositionSize object with amount and reasoning
        """
        if max_exposure_pct is None:
            max_exposure_pct = self.max_position_pct

        # Calculate edge
        edge = ai_estimated_prob - current_price

        # If negative edge, don't trade
        if edge <= 0:
            return PositionSize(
                amount=0.0,
                edge=edge,
                kelly_fraction=0.0,
                reasoning="Negative edge - no trade",
            )

        # Calculate odds (profit if you win)
        # If price is 0.60, you risk $0.40 to win $0.40
        odds = 1.0 / current_price

        # Calculate Kelly size
        kelly_size = self.kelly_criterion(
            win_probability=win_probability, odds=odds, fraction=self.kelly_fraction
        )

        # Convert to dollar amount
        dollar_amount = kelly_size * bankroll

        # Apply constraints
        max_dollars = bankroll * max_exposure_pct
        dollar_amount = min(dollar_amount, max_dollars)
        dollar_amount = max(dollar_amount, self.min_position)
        dollar_amount = min(dollar_amount, self.max_position)

        # Calculate actual Kelly fraction used
        actual_fraction = dollar_amount / bankroll if bankroll > 0 else 0

        return PositionSize(
            amount=round(dollar_amount, 2),
            edge=edge,
            kelly_fraction=actual_fraction,
            reasoning=f"Edge: {edge:.2%}, Kelly: {self.kelly_fraction:.2f}",
        )

    def calculate_kelly_bet(
        self,
        bankroll: float,
        edge: float,
        kelly_fraction: float = None,
    ) -> float:
        """
        Calculate bet size for binary market using edge.
        Simplified Kelly: bet = edge * fraction * bankroll (capped).
        """
        if kelly_fraction is None:
            kelly_fraction = self.kelly_fraction
        if edge <= 0:
            return 0.0
        size = edge * kelly_fraction * bankroll
        size = max(self.min_position, min(size, self.max_position))
        size = min(size, bankroll * self.max_position_pct)
        return round(size, 2)

    def calculate_binary_kelly_bet(
        self,
        bankroll: float,
        win_probability: float,
        contract_price: float,
        kelly_fraction: float = None,
    ) -> float:
        """Calculate Kelly bet size for a binary contract using actual payout odds.

        For a contract priced at ``c`` that pays ``1`` on success, net odds are:
        ``b = (1 - c) / c``.
        """
        if kelly_fraction is None:
            kelly_fraction = self.kelly_fraction

        p = max(0.0, min(1.0, float(win_probability)))
        c = max(0.01, min(0.99, float(contract_price)))
        b = (1.0 - c) / c
        if b <= 0:
            return 0.0

        kelly_frac = self.kelly_criterion(
            win_probability=p,
            odds=(1.0 / c),
            fraction=kelly_fraction,
        )
        if kelly_frac <= 0:
            return 0.0

        size = kelly_frac * bankroll
        size = max(self.min_position, min(size, self.max_position))
        size = min(size, bankroll * self.max_position_pct)
        return round(size, 2)

    def calculate_conservative_size(
        self, bankroll: float, confidence: float, edge: float
    ) -> float:
        """
        Calculates position size using a more conservative Kelly Criterion fraction.
        This is useful for long-term strategies where capital can be locked for a while.
        """
        # Use a fraction (e.g., 1/4th) of the standard Kelly fraction for conservative sizing
        conservative_fraction = self.kelly_fraction * 0.25

        # Calculate ideal position size using the conservative fraction
        ideal_size = bankroll * conservative_fraction * edge * confidence

        # Ensure the size is within the bot's global min/max position limits
        return max(self.min_position, min(self.max_position, ideal_size))

    def expected_value(
        self, win_probability: float, odds: float, bet_size: float
    ) -> float:
        """
        Calculate expected value of a bet

        Formula: EV = (win_probability * profit) - (loss_probability * loss)
        """
        profit = (odds - 1) * bet_size
        loss = bet_size

        return (win_probability * profit) - ((1 - win_probability) * loss)

    def roi(self, actual_outcome: bool, bet_size: float, current_price: float) -> float:
        """Calculate ROI if trade resolves"""
        if not actual_outcome:
            return -100.0  # Lost the bet

        # Profit when winning
        profit = (1.0 / current_price - 1) * bet_size
        return (profit / bet_size) * 100

    def risk_of_ruin(
        self, win_probability: float, kelly_fraction: float, num_bets: int
    ) -> float:
        """
        Calculate approximate risk of ruin

        Approximation formula for Kelly betting
        """
        import math

        if (
            kelly_fraction <= 0
            or kelly_fraction >= 1
            or win_probability <= 0
            or win_probability >= 1
        ):
            return 1.0

        # Calculate edge
        b = 1.0  # Even odds for simplicity
        p = win_probability
        q = 1 - p

        # Expected growth rate
        try:
            growth = p * math.log(1 + kelly_fraction * b) + q * math.log(
                1 - kelly_fraction
            )
        except ValueError:
            return 1.0

        # Variance
        variance = p * q * b**2

        # Risk of ruin approximation
        if growth <= 0:
            return 1.0

        ruin = math.exp(-2 * growth * num_bets / variance)

        return min(ruin, 1.0)


def calculate_spread_profit(
    yes_price: float, no_price: float, yes_size: float, no_size: float
) -> float:
    """
    Calculate profit from spread trading

    In spread trading, you buy both YES and NO and profit from the spread
    """
    # Total cost
    total_cost = (yes_price * yes_size) + (no_price * no_size)

    # You always win exactly one side
    winning_side = max(yes_price, no_price)
    profit = (winning_side * max(yes_size, no_size)) - total_cost

    return profit


def calculate_arbitrage_profit(
    yes_price: float, no_price: float, stake: float
) -> Optional[float]:
    """
    Check for arbitrage opportunity

    If yes_price + no_price < 1, there's arbitrage opportunity
    """
    total = yes_price + no_price

    if total >= 1.0:
        return None

    # Profit is the difference
    profit = (1.0 - total) * stake

    return profit


def calculate_implied_odds(price: float) -> float:
    """Convert price to implied odds"""
    if price <= 0:
        return 0.0
    return 1.0 / price


def calculate_decimal_odds(price: float) -> float:
    """Convert price to decimal odds"""
    return 1.0 / price


def calculate_us_odds(price: float) -> float:
    """Convert price to US odds format"""
    if price <= 0 or price >= 1:
        return 0.0
    if price >= 0.5:
        return (price / (1 - price)) * 100
    else:
        return -((1 - price) / price) * 100
