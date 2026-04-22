"""
Settle archived paper positions — Check resolved markets and close positions in archived sessions.

When you reset to a new session, the ~70 open positions from the old session are archived.
Those markets may have resolved on Polymarket. This script fetches resolution status and
logs exits for any resolved positions, updating the archived session files.

Usage:
    python scripts/settle_archived_positions.py

Run periodically or once after reset to settle the old batch.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ARCHIVE_BASE = PROJECT_ROOT / "data" / "paper_trades_archive"
GAMMA_API = "https://gamma-api.polymarket.com"


def fetch_resolution(market_id: str) -> dict | None:
    """Fetch resolution status from Polymarket. Returns {outcome_won: YES|NO} or None."""
    try:
        resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("closed", False):
            return None
        resolution = data.get("resolution")
        if resolution:
            return {"outcome_won": resolution.upper() if isinstance(resolution, str) else None}
        outcome_prices = data.get("outcomePrices", "")
        if outcome_prices:
            prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
            if len(prices) >= 2:
                yes_price = float(prices[0])
                if yes_price >= 0.99:
                    return {"outcome_won": "YES"}
                if yes_price <= 0.01:
                    return {"outcome_won": "NO"}
        return None
    except Exception as e:
        print(f"  Error fetching {market_id}: {e}")
        return None


def settle_position(pos: dict, exit_price: float, reason: str) -> dict:
    """Calculate PnL for a settled position."""
    side = pos.get("side", "BUY")
    entry_price = pos.get("entry_price", 0)
    size = pos.get("size", 0)
    if side == "BUY":
        pnl = (exit_price - entry_price) * size
    else:
        pnl = (entry_price - exit_price) * size
    return {
        "timestamp": datetime.now().isoformat(),
        "event": "EXIT",
        "trade_id": pos.get("trade_id", ""),
        "market_id": pos.get("market_id", ""),
        "market_question": pos.get("market_question", ""),
        "strategy": pos.get("strategy", ""),
        "action": pos.get("action", ""),
        "side": side,
        "outcome": pos.get("outcome", ""),
        "size": size,
        "entry_price": entry_price,
        "current_price": exit_price,
        "pnl": round(pnl, 4),
        "bankroll": 0,
        "reason": reason,
    }


def process_session(session_dir: Path) -> int:
    """Settle resolved positions in one archived session. Returns count settled."""
    positions_file = session_dir / "positions.json"
    entries_file = session_dir / "entries.jsonl"
    if not positions_file.exists():
        return 0

    with open(positions_file, encoding="utf-8") as f:
        positions = json.load(f)

    if not positions:
        return 0

    open_positions = list(positions.values()) if isinstance(positions, dict) else positions
    market_ids = list(set(p.get("market_id", "") for p in open_positions if p.get("market_id")))
    if not market_ids:
        return 0

    resolved = {}
    for mid in market_ids:
        r = fetch_resolution(mid)
        if r and r.get("outcome_won"):
            resolved[mid] = r

    if not resolved:
        return 0

    settled_count = 0
    for pos in open_positions:
        mid = pos.get("market_id", "")
        if mid not in resolved:
            continue

        outcome_won = resolved[mid]["outcome_won"]
        action = pos.get("action", "")
        if action == "BUY_YES":
            exit_price = 1.0 if outcome_won == "YES" else 0.0
        elif action == "SELL_YES":
            exit_price = 1.0 if outcome_won == "YES" else 0.0
        elif action == "BUY_NO":
            exit_price = 1.0 if outcome_won == "NO" else 0.0
        else:
            continue

        reason = f"RESOLVED:{outcome_won} (archived batch)"
        exit_entry = settle_position(pos, exit_price, reason)

        with open(entries_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(exit_entry, default=str) + "\n")

        trade_id = pos.get("trade_id", "")
        if trade_id and trade_id in positions:
            del positions[trade_id]
            settled_count += 1
            pnl = exit_entry["pnl"]
            print(f"  Settled: {pos.get('strategy','')} {pos.get('market_question','')[:40]} PnL=${pnl:+.2f}")

    if settled_count:
        with open(positions_file, "w", encoding="utf-8") as f:
            json.dump(positions, f, indent=2, default=str)
        summary_file = session_dir / "summary.json"
        if summary_file.exists():
            try:
                with open(summary_file, encoding="utf-8") as f:
                    summary = json.load(f)
                summary["open_positions"] = len(positions)
                with open(summary_file, "w", encoding="utf-8") as f:
                    json.dump(summary, f, indent=2)
            except Exception:
                pass

    return settled_count


def main():
    if not ARCHIVE_BASE.exists():
        print("No paper_trades_archive found.")
        return 0

    total = 0
    for archive_ts in sorted(ARCHIVE_BASE.iterdir(), reverse=True):
        if not archive_ts.is_dir():
            continue
        for session_dir in archive_ts.iterdir():
            if not session_dir.is_dir():
                continue
            pos_file = session_dir / "positions.json"
            if not pos_file.exists():
                continue
            with open(pos_file, encoding="utf-8") as f:
                cnt = len(json.load(f))
            if cnt == 0:
                continue
            print(f"\nSession {session_dir.name}: {cnt} open positions")
            n = process_session(session_dir)
            total += n
            if n:
                print(f"  -> Settled {n} positions")

    print(f"\nTotal settled: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
