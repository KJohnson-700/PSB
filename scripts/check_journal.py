"""Quick journal check script."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.execution.trade_journal import TradeJournal

j = TradeJournal()
closed = j.get_closed_trades()
print(f"Closed trades: {len(closed)}")
for c in closed:
    q = c.get("market_question", "")[:55]
    pnl = c.get("pnl", 0)
    strat = c.get("strategy", "")
    reason = c.get("exit_reason", "")
    print(f"  {strat} | {q} | PnL=${pnl:+.2f} | {reason}")

print(f"\nRealized PnL: ${j.realized_pnl:+.2f}")
print(f"Open positions: {len(j.get_open_positions())}")

crypto = [
    p for p in j.get_open_positions()
    if p.get("strategy") in ("bitcoin", "sol_macro", "eth_macro")
]
print(f"Open crypto: {len(crypto)}")
for p in crypto:
    q = p.get("market_question", "")[:55]
    entry = p.get("entry_price", 0)
    pnl = p.get("pnl", 0)
    strat = p.get("strategy", "")
    print(f"  {strat} | {q} | entry={entry:.3f} | pnl=${pnl:+.2f}")
