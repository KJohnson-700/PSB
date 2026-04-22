"""
Data Models
For storing trade history and active positions
"""
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict


@dataclass
class TradeRecord:
    """Record of a completed trade"""
    trade_id: str
    market_id: str
    market_question: str
    outcome: str
    side: str
    size: float
    price: float
    pnl: float
    opened_at: str
    closed_at: Optional[str]
    status: str  # open, closed


class DataManager:
    """Manages persistent storage of trades and state"""
    
    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = Path(__file__).parent.parent / "data"
        
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        
        self.trades_file = self.data_dir / "trades.json"
        self.positions_file = self.data_dir / "active_positions.json"
        self.state_file = self.data_dir / "bot_state.json"
        
    def load_trades(self) -> List[Dict]:
        """Load trade history"""
        if self.trades_file.exists():
            with open(self.trades_file, 'r') as f:
                return json.load(f)
        return []
    
    def save_trade(self, trade: Dict):
        """Save a new trade"""
        trades = self.load_trades()
        trades.append(trade)
        with open(self.trades_file, 'w') as f:
            json.dump(trades, f, indent=2)
    
    def load_positions(self) -> Dict[str, Dict]:
        """Load active positions"""
        if self.positions_file.exists():
            with open(self.positions_file, 'r') as f:
                return json.load(f)
        return {}
    
    def save_position(self, position_id: str, position: Dict):
        """Save an active position"""
        positions = self.load_positions()
        positions[position_id] = position
        with open(self.positions_file, 'w') as f:
            json.dump(positions, f, indent=2)
    
    def remove_position(self, position_id: str):
        """Remove a closed position"""
        positions = self.load_positions()
        if position_id in positions:
            del positions[position_id]
            with open(self.positions_file, 'w') as f:
                json.dump(positions, f, indent=2)
    
    def load_state(self) -> Dict:
        """Load bot state"""
        if self.state_file.exists():
            with open(self.state_file, 'r') as f:
                return json.load(f)
        return {
            'bankroll': 1000.0,
            'daily_pnl': 0.0,
            'daily_trades': 0,
            'last_reset': datetime.now().isoformat()
        }
    
    def save_state(self, state: Dict):
        """Save bot state"""
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get trading statistics"""
        trades = self.load_trades()
        
        if not trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0.0,
                'total_pnl': 0.0,
                'avg_pnl': 0.0
            }
        
        winning = [t for t in trades if t.get('pnl', 0) > 0]
        losing = [t for t in trades if t.get('pnl', 0) < 0]
        
        return {
            'total_trades': len(trades),
            'winning_trades': len(winning),
            'losing_trades': len(losing),
            'win_rate': len(winning) / len(trades) if trades else 0,
            'total_pnl': sum(t.get('pnl', 0) for t in trades),
            'avg_pnl': sum(t.get('pnl', 0) for t in trades) / len(trades)
        }
