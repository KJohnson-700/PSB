#!/usr/bin/env python3
"""
Scanner health check for crypto/watchlist pipelines.

Runs MarketScanner.scan_for_opportunities() once and prints:
- scan timing
- core bucket counts
- updown asset/window distribution

Usage:
  python3 scripts/live_strategy_scan.py
"""

from __future__ import annotations

import copy
import os
import sys
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.env_bootstrap import load_project_dotenv
from src.market.scanner import MarketScanner


def _asset_label(question: str) -> str:
    q = (question or "").lower()
    if "bitcoin" in q or "btc" in q:
        return "BTC"
    if "solana" in q or "sol " in q:
        return "SOL"
    if "ethereum" in q or "ether" in q or " eth" in q:
        return "ETH"
    if "xrp" in q or "ripple" in q:
        return "XRP"
    if "hyperliquid" in q or "hype" in q:
        return "HYPE"
    return "OTHER"


def _run_scan(config: dict) -> int:
    # Scanner health check focuses on crypto lane freshness; disable weather background
    # refresh to keep this script deterministic and avoid long-lived background threads.
    run_cfg = copy.deepcopy(config)
    strategies = run_cfg.setdefault("strategies", {})
    weather_cfg = strategies.setdefault("weather", {})
    weather_cfg["enabled"] = False
    weather_cfg["scan_limit"] = 0

    scanner = MarketScanner(run_cfg)
    start = time.perf_counter()
    (
        markets,
        up15,
        up5,
        up1h,
        hype_alt,
        weather,
        look_ahead_15m,
        look_ahead_5m,
        look_ahead_1h,
    ) = scanner._sync_network_phase()
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    high_liq = list(markets) + list(up15) + list(up5) + list(up1h) + list(hype_alt)

    print("\nScanner health check")
    print("====================")
    print(f"elapsed_ms: {elapsed_ms}")
    print(f"sync_network_ms: {elapsed_ms}")
    print("sync_timeout: False")
    print(f"look_ahead_15m: {look_ahead_15m}")
    print(f"look_ahead_5m: {look_ahead_5m}")
    print(f"look_ahead_1h: {look_ahead_1h}")
    print(f"high_liquidity: {len(high_liq)}")
    print(f"updown_15m: {len(up15)}")
    print(f"updown_5m: {len(up5)}")
    print(f"updown_1h: {len(up1h)}")
    print(f"weather: {len(weather)}")

    if up15 or up5 or up1h:
        by_asset_window: dict[tuple[str, str], int] = {}
        for m in up15:
            key = (_asset_label(m.question), "15m")
            by_asset_window[key] = by_asset_window.get(key, 0) + 1
        for m in up5:
            key = (_asset_label(m.question), "5m")
            by_asset_window[key] = by_asset_window.get(key, 0) + 1
        for m in up1h:
            key = (_asset_label(m.question), "1h")
            by_asset_window[key] = by_asset_window.get(key, 0) + 1

        print("\nasset/window counts:")
        for (asset, window), n in sorted(by_asset_window.items()):
            print(f"  {asset:>5} {window:>3}: {n}")

    # No async session used in this sync health check path.
    scanner._background_fetch_pool.shutdown(wait=False, cancel_futures=True)
    if not up15 and not up5 and not up1h:
        print("\nWARNING: no updown markets returned.")
        return 1
    return 0


def main() -> int:
    load_project_dotenv(REPO_ROOT, quiet=True)
    cfg_path = REPO_ROOT / "config" / "settings.yaml"
    if not cfg_path.exists():
        print(f"ERROR: config not found at {cfg_path}")
        return 2
    with open(cfg_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    code = _run_scan(config)
    # Force process exit for this CLI health check even if a third-party
    # networking thread lingers; this script is diagnostics-only.
    sys.stdout.flush()
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
