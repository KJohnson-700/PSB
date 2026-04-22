"""Debug resolution tracker - check why crypto positions aren't settling."""
import sys, os, requests, json
from datetime import datetime
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.execution.trade_journal import TradeJournal

GAMMA_API = "https://gamma-api.polymarket.com"

j = TradeJournal()
open_positions = j.get_open_positions()

# Filter crypto positions
crypto = [
    p for p in open_positions
    if p.get("strategy") in ("bitcoin", "sol_lag", "eth_lag")
]
print(f"=== CRYPTO OPEN POSITIONS ({len(crypto)}) ===")
print(f"Current local time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print()

for p in crypto:
    mid = p.get("market_id", "")
    q = p.get("market_question", "")[:65]
    strat = p.get("strategy", "")
    entry = p.get("entry_price", 0)

    print(f"--- {strat}: {q}")
    print(f"    Market ID: {mid}")
    print(f"    Entry: {entry:.3f}")

    # Check Gamma API
    try:
        resp = requests.get(f"{GAMMA_API}/markets/{mid}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            closed = data.get("closed", False)
            resolution = data.get("resolution", None)
            end_date = data.get("endDate", "unknown")
            outcome_prices = data.get("outcomePrices", "")
            active = data.get("active", "?")
            accepting = data.get("acceptingOrders", "?")

            print(f"    API Status: closed={closed}, resolution={resolution}, active={active}")
            print(f"    End date: {end_date}")
            print(f"    Outcome prices: {outcome_prices}")
            print(f"    Accepting orders: {accepting}")

            if outcome_prices:
                try:
                    prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                    yes_price = float(prices[0])
                    print(f"    YES price: {yes_price} {'(RESOLVED YES)' if yes_price >= 0.99 else '(RESOLVED NO)' if yes_price <= 0.01 else '(NOT RESOLVED)'}")
                except:
                    pass
        else:
            print(f"    API Error: HTTP {resp.status_code}")
    except Exception as e:
        print(f"    API Error: {e}")
    print()

# Also check how many total resolution checks happened
print("=== RESOLUTION TRACKER CACHE ===")
# Check if any markets got cached as resolved
closed_trades = j.get_closed_trades()
print(f"Already settled: {len(closed_trades)}")
for c in closed_trades:
    print(f"  {c.get('strategy')} | {c.get('market_question','')[:55]} -> {c.get('exit_reason','')}")
