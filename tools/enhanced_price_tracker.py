#!/usr/bin/env python3
"""
enhanced_price_tracker.py — Combines price volatility + Polymarket odds regime.

Pulls:
  1. CoinGecko prices for all 7 assets → price volatility regime (flat/warm/hot)
  2. Gamma API Polymarket up/down prices for all 7 assets on 5m + 15m
     → Polymarket odds regime (clustered/signal/deadzone)

Logs both to data/calibration/market_regime.jsonl
Ghost settlement/report tooling joins these snapshots onto rejected candidates by
timestamp.

Usage:
  python3 enhanced_price_tracker.py          # run once
  python3 enhanced_price_tracker.py --daemon # run every 15min
"""

import json
import math
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config ───────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
REGIME_LOG = REPO_ROOT / "data" / "calibration" / "market_regime.jsonl"
HISTORY_FILE = REPO_ROOT / "data" / "calibration" / "price_history.jsonl"

# CoinGecko asset IDs
COIN_IDS = [
    "bitcoin", "ethereum", "solana", "ripple",
    "dogecoin", "hyperliquid", "binancecoin",
]
COIN_NAME_MAP = {
    "bitcoin": "bitcoin", "ethereum": "ethereum",
    "solana": "solana", "ripple": "ripple",
    "dogecoin": "dogecoin", "hyperliquid": "hyperliquid",
    "binancecoin": "binancecoin",
}

# Gamma API for Polymarket short-window up/down markets
GAMMA_BASE = "https://gamma-api.polymarket.com"

# Price volatility thresholds (std dev of 1h log returns)
FLAT_THRESHOLD = 0.005   # < 0.5% = flat
HOT_THRESHOLD  = 0.020   # > 2.0% = hot

# Polymarket deadzone: all assets clustering near 50% with low spread
POLYMARKET_DEADZONE_SPREAD = 0.04   # max 4pp spread across assets = cluster
POLYMARKET_DEADZONE_CENTER = 0.50   # center at 50%
POLYMARKET_SIGNAL_SPREAD = 0.15     # > 15pp spread = signal active

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
POLYMARKET_CACHE_TTL = 30  # seconds — Polymarket list view can serve stale data


def fetch_coingecko_prices():
    """Fetch USD prices + 24h change for all assets."""
    try:
        r = requests.get(COINGECKO_URL, params={
            "ids": ",".join(COIN_IDS),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        }, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[ERROR] CoinGecko failed: {e}")
        return {}


def fetch_polymarket_updown():
    """Fetch live Polymarket up/down prices for 5m and 15m windows.

    Uses the same slug construction logic as scanner.py:
      - 5m:  bitcoin-updown-5m-{unix_ts}
      - 15m: bitcoin-updown-15m-{unix_ts}

    Returns dict: {timeframe: {asset: {price, volume, slug}}}
    """
    results = {"5m": {}, "15m": {}}
    now_ts = int(datetime.now(timezone.utc).timestamp())

    for tf_min in [5, 15]:
        step_seconds = tf_min * 60
        floor_ts = (now_ts // step_seconds) * step_seconds

        for asset in ["bitcoin", "ethereum", "solana", "xrp", "dogecoin", "hype", "bnb"]:
            slug = f"{asset}-updown-{tf_min}m-{floor_ts}"
            try:
                r = requests.get(
                    f"{GAMMA_BASE}/markets",
                    params={"slug": slug, "limit": 1},
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0:
                        m = data[0]
                        outcomes = _coerce_json_list(m.get("outcomePrices", "[]"))
                        price = float(outcomes[0]) if outcomes else None
                        volume = float(m.get("volume", 0) or 0)
                        results[str(tf_min) + "m"][asset] = {
                            "price": price,
                            "volume": volume,
                            "slug": slug,
                        }
            except Exception as e:
                pass  # silently skip failed individual asset fetches

    return results


def _coerce_json_list(val):
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except:
            pass
    return []


def load_price_history():
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


def save_price_record(asset, price, ts_unix):
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps({"ts": ts_unix, "asset": asset, "price": price}) + "\n")
    # Prune to last 2h
    records = load_price_history()
    cutoff = time.time() - 7200
    recent = [r for r in records if r.get("ts", 0) > cutoff]
    with open(HISTORY_FILE, "w") as f:
        for r in recent:
            f.write(json.dumps(r) + "\n")


def calc_volatility(asset_history):
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


def price_regime(volatilities):
    valid = [v for v in volatilities.values() if v is not None]
    if not valid:
        return "unknown"
    avg = sum(valid) / len(valid)
    if avg < FLAT_THRESHOLD:
        return "flat"
    elif avg > HOT_THRESHOLD:
        return "hot"
    return "warm"


def polymarket_regime(tick_data):
    """Determine Polymarket regime from 5m + 15m tick data.

    deadzone:  all assets clustered at 49-51% (spread < 4pp)
    signal:    spread > 15pp indicating real directional bets
    flat:       everything between
    """
    all_prices = []

    for tf, assets in tick_data.items():
        for asset, data in assets.items():
            if data.get("price") is not None:
                all_prices.append(data["price"])

    if len(all_prices) < 3:
        return "unknown"

    mean_price = sum(all_prices) / len(all_prices)
    max_price = max(all_prices)
    min_price = min(all_prices)
    spread = max_price - min_price

    # Check clustering around 50%
    if spread < POLYMARKET_DEADZONE_SPREAD:
        # All assets clustered — deadzone
        return "deadzone"

    # Check genuine directional signal
    if spread > POLYMARKET_SIGNAL_SPREAD:
        return "signal"

    return "flat"


def combined_regime(price_reg, poly_reg):
    """Combine price + polymarket into one label."""
    if price_reg == "unknown" or poly_reg == "unknown":
        return "unknown"
    if price_reg == "flat" and poly_reg == "deadzone":
        return "deadzone_confirmed"
    if price_reg == "hot" or poly_reg == "signal":
        return "active"
    return "quiet"


def run_tracker(daemon=False, interval=900):
    while True:
        ts_iso = datetime.now(timezone.utc).isoformat()
        ts_unix = time.time()
        print(f"\n[{ts_iso}] Fetching market data...")

        # 1. CoinGecko prices
        cg_data = fetch_coingecko_prices()
        prices = {}
        for coin_id in COIN_IDS:
            if coin_id not in cg_data:
                continue
            d = cg_data[coin_id]
            price = d.get("usd")
            prices[coin_id] = {"price": price, "change_24h": d.get("usd_24h_change", 0)}
            if price:
                save_price_record(coin_id, price, ts_unix)

        # 2. Polymarket up/down prices
        poly_data = fetch_polymarket_updown()

        # 3. Calculate price volatilities
        history = load_price_history()
        volatilities = {}
        for coin_id in prices:
            asset_hist = [h for h in history if h.get("asset") == coin_id]
            vol = calc_volatility(asset_hist)
            volatilities[coin_id] = vol

        p_reg = price_regime(volatilities)
        pm_reg = polymarket_regime(poly_data)
        c_reg = combined_regime(p_reg, pm_reg)

        record = {
            "ts": ts_iso,
            "price_regime": p_reg,
            "polymarket_regime": pm_reg,
            "combined_regime": c_reg,
            "volatilities": {k: round(v, 6) if v is not None else None for k, v in volatilities.items()},
            "prices": {k: v["price"] for k, v in prices.items()},
            "polymarket_5m": {k: {"price": v["price"], "volume": v["volume"]} for k, v in poly_data.get("5m", {}).items()},
            "polymarket_15m": {k: {"price": v["price"], "volume": v["volume"]} for k, v in poly_data.get("15m", {}).items()},
        }

        REGIME_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(REGIME_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

        vol_str = ", ".join(
            f"{k}={v:.4f}" if v is not None else f"{k}=N/A"
            for k, v in volatilities.items()
        )
        pm_5m_str = ", ".join(
            f"{k}={v['price']:.2f}" if v.get('price') else f"{k}=?"
            for k, v in poly_data.get("5m", {}).items()
        )
        print(f"  Price regime: {p_reg} | Vols: {vol_str}")
        print(f"  Polymarket regime: {pm_reg} | 5m: {pm_5m_str}")
        print(f"  Combined: {c_reg}")
        print(f"  Logged -> {REGIME_LOG}")

        if not daemon:
            break

        print(f"  Sleeping {interval}s...")
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=int, default=900)
    args = parser.parse_args()
    run_tracker(daemon=args.daemon, interval=args.interval)
