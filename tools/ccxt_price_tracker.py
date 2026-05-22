#!/usr/bin/env python3
"""
ccxt_price_tracker.py — Volatility regime tracker for PSB ghost calibration.

Pulls real-time prices for all 7 PSB assets via CoinGecko REST API (no key needed),
calculates rolling 1h volatility, logs market regime (hot/flat/warm) to
data/calibration/volatility_regime.jsonl.

Cross-reference with rejected_candidates_settled.jsonl to label each ghost
rejection with the market regime active at its timestamp.

Usage:
  python3 ccxt_price_tracker.py          # run once
  python3 ccxt_price_tracker.py --daemon  # run continuously every 5min
"""

import json
import math
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests

# Config
REPO_ROOT = Path(__file__).resolve().parent.parent
REGIME_LOG = REPO_ROOT / "data" / "calibration" / "volatility_regime.jsonl"
HISTORY_FILE = REPO_ROOT / "data" / "calibration" / "price_history.jsonl"

COIN_IDS = [
    "bitcoin", "ethereum", "solana", "ripple",
    "dogecoin", "hyperliquid", "binancecoin",
]

# Volatility regime thresholds (std dev of 1h log returns)
FLAT_THRESHOLD = 0.005   # < 0.5%  = flat
HOT_THRESHOLD  = 0.020   # > 2.0%  = hot
# Between = warm

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


def fetch_prices():
    """Fetch USD prices + 24h change for all assets in one CoinGecko call."""
    params = {
        "ids": ",".join(COIN_IDS),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    }
    try:
        r = requests.get(COINGECKO_URL, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[ERROR] CoinGecko API failed: {e}")
        return {}


def load_history():
    """Load price history from disk."""
    if not HISTORY_FILE.exists():
        return []
    records = []
    with open(HISTORY_FILE) as f:
        for line in f:
            try:
                records.append(json.loads(line.strip()))
            except:
                continue
    return records


def save_record(record):
    """Append price record + prune history to last 2h."""
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    # Prune
    records = load_history()
    cutoff = time.time() - 7200
    recent = [r for r in records if r.get("ts", 0) > cutoff]
    with open(HISTORY_FILE, "w") as f:
        for r in recent:
            f.write(json.dumps(r) + "\n")


def calc_volatility(asset_history):
    """Std dev of log returns over the price history window."""
    prices = [
        h["price"] for h in sorted(asset_history, key=lambda x: x.get("ts", 0))
        if h.get("price") and h["price"] > 0
    ]
    if len(prices) < 3:
        return None
    returns = [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices))]
    if not returns:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance)


def determine_regime(volatilities):
    """Aggregate per-asset volatilities into one regime label."""
    valid = [v for v in volatilities.values() if v is not None]
    if not valid:
        return "unknown"
    avg = sum(valid) / len(valid)
    if avg < FLAT_THRESHOLD:
        return "flat"
    elif avg > HOT_THRESHOLD:
        return "hot"
    return "warm"


def run_tracker(daemon=False, interval=300):
    while True:
        ts_iso = datetime.now(timezone.utc).isoformat()
        ts_unix = time.time()
        print(f"\n[{ts_iso}] Fetching prices...")

        data = fetch_prices()
        if not data:
            print("  [ERROR] No data from CoinGecko")
            if daemon:
                time.sleep(interval)
                continue
            break

        prices = {}
        for coin_id in COIN_IDS:
            if coin_id not in data:
                print(f"  [WARN] {coin_id} not in response")
                continue
            d = data[coin_id]
            prices[coin_id] = {
                "price": d.get("usd"),
                "change_24h": d.get("usd_24h_change", 0),
            }
            # Save to history
            save_record({
                "ts": ts_unix,
                "asset": coin_id,
                "price": d.get("usd"),
            })

        # Calculate per-asset 1h volatility
        history = load_history()
        volatilities = {}
        for coin_id in prices:
            asset_hist = [h for h in history if h.get("asset") == coin_id]
            vol = calc_volatility(asset_hist)
            volatilities[coin_id] = vol

        regime = determine_regime(volatilities)

        regime_record = {
            "ts": ts_iso,
            "regime": regime,
            "volatilities": {k: round(v, 6) if v is not None else None for k, v in volatilities.items()},
            "prices": {k: v["price"] for k, v in prices.items()},
            "change_24h": {k: round(v["change_24h"], 4) for k, v in prices.items()},
        }

        REGIME_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(REGIME_LOG, "a") as f:
            f.write(json.dumps(regime_record) + "\n")

        vol_str = ", ".join(
            f"{k}={v:.4f}" if v is not None else f"{k}=N/A"
            for k, v in volatilities.items()
        )
        print(f"  Regime: {regime} | Vols: {vol_str}")
        print(f"  Logged -> {REGIME_LOG}")

        if not daemon:
            break

        print(f"  Sleeping {interval}s...")
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    run_tracker(daemon=args.daemon, interval=args.interval)
