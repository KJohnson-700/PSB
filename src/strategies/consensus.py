"""
Consensus Strategy Module
Strategy 2: Alert on consensus-betting opportunities
"""
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from datetime import datetime

from ..market.scanner import Market, is_crypto_updown_market

logger = logging.getLogger(__name__)


@dataclass
class ConsensusAlert:
    """Alert for consensus betting opportunity"""
    market_id: str
    market_question: str
    consensus_side: str  # "YES" or "NO"
    current_price: float
    volume: float
    liquidity: float
    opposite_liquidity: float
    hours_to_expiration: float
    risk_assessment: str
    recommended_action: str
    timestamp: datetime


class ConsensusStrategy:
    """
    Strategy 2: Consensus Alerting
    
    Logic:
    1. Monitor markets for consensus (>85% on one side)
    2. Check liquidity on opposite side (can we actually get out?)
    3. Check time to expiration
    4. Alert user for manual approval
    
    This strategy does NOT auto-trade - it alerts for human decision
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config.get('strategies', {}).get('consensus', {})
        self.enabled = bool(self.config.get('enabled', False))

        # Configuration
        self.threshold = self.config.get('threshold', 0.85)
        self.min_opposite_liquidity = self.config.get('min_opposite_liquidity', 500)
        self.expiration_window_hours = self.config.get('expiration_window_hours', 48)
        
        # State
        self.alerted_markets: set = set()
        
    def scan_for_consensus(
        self,
        markets: List[Market]
    ) -> List[ConsensusAlert]:
        """
        Scan markets for consensus opportunities
        
        Args:
            markets: List of markets to scan
        
        Returns:
            List of consensus alerts (for human approval)
        """
        if not self.enabled:
            return []

        alerts = []

        for market in markets:
            # Crypto 15m/5m up-down windows: lopsided prices are usually resolution flow, not
            # generic "contrarian" setups. bitcoin / sol_lag / eth_lag own these markets.
            if is_crypto_updown_market(market):
                continue
            # Skip if already alerted
            if market.id in self.alerted_markets:
                continue

            # Skip if not near expiration
            hours = market.hours_to_expiration
            if hours and hours > self.expiration_window_hours:
                continue
            
            # Check for consensus
            alert = self._check_consensus(market)
            
            if alert:
                alerts.append(alert)
                self.alerted_markets.add(market.id)
        
        return alerts
    
    def _check_consensus(self, market: Market) -> Optional[ConsensusAlert]:
        """Check if market is at consensus level"""
        
        # Check YES consensus
        if market.yes_price >= self.threshold:
            # Calculate opposite liquidity (NO side)
            # Approximate using volume and price
            no_liquidity = market.liquidity * (1 - market.yes_price) * 2
            
            if no_liquidity < self.min_opposite_liquidity:
                logger.debug(f"Market {market.id} has YES consensus but low NO liquidity")
                return None
            
            risk = self._assess_risk(
                consensus_price=market.yes_price,
                hours_remaining=market.hours_to_expiration or 0
            )
            
            return ConsensusAlert(
                market_id=market.id,
                market_question=market.question,
                consensus_side="YES",
                current_price=market.yes_price,
                volume=market.volume,
                liquidity=market.liquidity,
                opposite_liquidity=no_liquidity,
                hours_to_expiration=market.hours_to_expiration or 0,
                risk_assessment=risk,
                recommended_action="Consider NO (crowded YES)",
                timestamp=datetime.now()
            )
        
        # Check NO consensus (yes_price < 0.15)
        if market.yes_price <= (1 - self.threshold):
            # Calculate opposite liquidity (YES side)
            yes_liquidity = market.liquidity * market.yes_price * 2
            
            if yes_liquidity < self.min_opposite_liquidity:
                logger.debug(f"Market {market.id} has NO consensus but low YES liquidity")
                return None
            
            risk = self._assess_risk(
                consensus_price=1 - market.yes_price,
                hours_remaining=market.hours_to_expiration or 0
            )
            
            return ConsensusAlert(
                market_id=market.id,
                market_question=market.question,
                consensus_side="NO",
                current_price=market.yes_price,
                volume=market.volume,
                liquidity=market.liquidity,
                opposite_liquidity=yes_liquidity,
                hours_to_expiration=market.hours_to_expiration or 0,
                risk_assessment=risk,
                recommended_action="Consider YES (crowded NO)",
                timestamp=datetime.now()
            )
        
        return None
    
    def _assess_risk(self, consensus_price: float, hours_remaining: float) -> str:
        """Assess risk level of the consensus bet"""
        
        # Very high consensus (>95%) near expiration = HIGH RISK
        if consensus_price >= 0.95 and hours_remaining < 12:
            return "HIGH - Market likely correct, time to exit limited"
        
        # High consensus (>90%) with decent time = MODERATE
        if consensus_price >= 0.90 and hours_remaining > 24:
            return "MODERATE - Potential value but risky"
        
        # High consensus with plenty of time = LOWER RISK
        if consensus_price >= 0.85 and hours_remaining > 48:
            return "LOW-MODERATE - Possible fade vs one-sided odds"
        
        return "MODERATE - One-sided market; review manually"
    
    def get_alert_summary(self, alert: ConsensusAlert) -> str:
        """Generate formatted alert summary for notification"""
        
        emoji = "🔴" if alert.risk_assessment.startswith("HIGH") else "🟡" if alert.risk_assessment.startswith("MODERATE") else "🟢"
        
        summary = f"""
{emoji} Manual review — lopsided market

📌 Market: {alert.market_question[:80]}...

📊 Current Odds: {alert.consensus_side} @ {alert.current_price:.0%}
📈 Volume: ${alert.volume:,.0f}
💧 Opposite Liquidity: ${alert.opposite_liquidity:,.0f}
⏰ Time Remaining: {alert.hours_to_expiration:.1f} hours

⚠️ Assessment: {alert.risk_assessment}

🎯 Suggested: {alert.recommended_action}

🔗 https://polymarket.com/market/{alert.market_id}
"""
        return summary
    
    def should_user_trade(
        self,
        alert: ConsensusAlert,
        user_bankroll: float,
        max_position_pct: float = 0.05
    ) -> float:
        """
        Calculate suggested position size for the user
        
        Returns suggested amount or 0 if shouldn't trade
        """
        # Don't recommend if high risk
        if alert.risk_assessment.startswith("HIGH"):
            return 0.0
        
        # Calculate position size based on risk
        if alert.risk_assessment.startswith("LOW"):
            # Lower risk - can size up
            size_pct = max_position_pct
        else:
            # Moderate risk - size down
            size_pct = max_position_pct * 0.5
        
        amount = user_bankroll * size_pct
        
        # Ensure minimum
        if amount < 10:
            return 0.0
            
        return round(amount, 2)
    
    def reset_alerts(self):
        """Reset alerted markets to allow re-alerting"""
        self.alerted_markets.clear()
        logger.info("Reset consensus alerts counter")
