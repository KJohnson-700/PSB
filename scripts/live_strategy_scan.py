#!/usr/bin/env python3
"""
Live Strategy Scanner — pulls real Polymarket data and runs all strategies against it.
No trades executed. Logs what WOULD have been traded and why.

Usage:
  python scripts/live_strategy_scan.py
  python scripts/live_strategy_scan.py --strategy fade --limit 50
  python scripts/live_strategy_scan.py --strategy all --min-volume 50000
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
import yaml

from src.backtest.backtest_ai import BacktestAIAgent
from src.analysis.math_utils import PositionSizer
from src.strategies.fade import FadeStrategy
from src.strategies.arbitrage import ArbitrageStrategy
from src.strategies.neh import NothingEverHappensStrategy
from src.market.scanner import Market

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def load_config():
    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def _print_alt_btc_analysis_banner(alt_label: str, binance_symbol: str) -> None:
    """Print one block: alt spot vs BTC correlation/lag (same math as live strategies)."""
    from src.analysis.sol_btc_service import SOLBTCService

    print(f"\n\nALT/BTC SERVICE SIGNAL STATE ({binance_symbol} leg)")
    print("-" * 70)
    try:
        svc = SOLBTCService(alt_symbol=binance_symbol)
        ta = svc.get_full_analysis()
        if ta:
            corr = ta.correlation
            mtt = ta.multi_tf
            alt = ta.sol
            print(
                f"  {alt_label}: ${alt.current_price:.2f}  Trend={mtt.overall_direction}  "
                f"1H={mtt.h1_trend}  15m={mtt.m15_trend}  5m={mtt.m5_trend}"
            )
            print(
                f"  BTC: ${corr.btc_price:,.0f}  5m={corr.btc_move_5m_pct:+.2f}%  "
                f"15m={corr.btc_move_15m_pct:+.2f}%  spike={corr.btc_spike_detected}"
            )
            print(
                f"  Lag opp: {corr.lag_opportunity}  dir={corr.opportunity_direction}  "
                f"mag={corr.opportunity_magnitude:+.2f}%  corr1h={corr.correlation_1h:.3f}"
            )
        else:
            print("  Could not fetch analysis — Binance may be rate-limiting")
    except Exception as e:
        print(f"  Error: {e}")


def scan_updown_markets(config):
    """Print live BTC / SOL / ETH updown markets and SOL/BTC + ETH/BTC signal state."""
    from src.market.scanner import MarketScanner

    print("\n" + "=" * 70)
    print("UPDOWN MARKET STATUS (BTC / SOL / ETH)")
    print("=" * 70)

    scanner = MarketScanner(config)

    markets_15m = scanner.fetch_updown_markets(look_ahead=4)
    markets_5m = scanner.fetch_updown_5m_markets(look_ahead=8)
    all_updown = markets_15m + markets_5m

    print(f"\nFetched {len(markets_15m)} 15m + {len(markets_5m)} 5m updown markets")

    if not all_updown:
        print("  No updown markets in current window — slugs may not exist yet.")
    else:
        for keyword, label in [("bitcoin", "BTC"), ("solana", "SOL"), ("ethereum", "ETH")]:
            asset_mkts = [m for m in all_updown if keyword in m.question.lower()]
            if not asset_mkts:
                print(f"\n  {label}: No markets found")
                continue
            print(f"\n  {label} ({len(asset_mkts)} markets):")
            for m in asset_mkts:
                print(f"    {m.question[:65]}")
                print(
                    f"      YES={m.yes_price:.3f}  NO={m.no_price:.3f}  liq=${m.liquidity:,.0f}"
                )

    _print_alt_btc_analysis_banner("SOL", "SOLUSDT")
    _print_alt_btc_analysis_banner("ETH", "ETHUSDT")


def fetch_active_markets(limit=100, min_volume=10000):
    """Fetch active markets from Gamma API sorted by volume."""
    markets = []
    offset = 0
    while len(markets) < limit:
        params = {
            "limit": min(limit - len(markets), 100),
            "offset": offset,
            "active": "true",
            "closed": "false",
        }
        try:
            resp = requests.get(f"{GAMMA_API}/markets", params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for m in batch:
                vol = float(m.get("volume", 0) or 0)
                if vol >= min_volume:
                    markets.append(m)
            offset += len(batch)
            if len(batch) < params["limit"]:
                break
        except Exception as e:
            print(f"  API error: {e}")
            break
    return markets


def gamma_to_market(gm):
    """Convert Gamma API market dict to our Market dataclass."""
    try:
        outcomes = json.loads(gm.get("outcomePrices", "[]"))
        yes_price = float(outcomes[0]) if outcomes else 0.5
        no_price = float(outcomes[1]) if len(outcomes) > 1 else 1.0 - yes_price
    except (json.JSONDecodeError, IndexError, TypeError):
        yes_price = 0.5
        no_price = 0.5

    try:
        tokens = json.loads(gm.get("clobTokenIds", "[]"))
        token_yes = tokens[0] if tokens else ""
        token_no = tokens[1] if len(tokens) > 1 else ""
    except (json.JSONDecodeError, IndexError, TypeError):
        token_yes = ""
        token_no = ""

    end_str = gm.get("endDate") or gm.get("end_date_iso")
    end_date = None
    if end_str:
        try:
            end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            pass

    spread_val = float(gm.get("spread", 0.02) or 0.02)
    volume = float(gm.get("volume", 0) or 0)
    liquidity = float(gm.get("liquidity", 0) or 0)

    return Market(
        id=gm.get("id", ""),
        question=gm.get("question", ""),
        description=gm.get("description", "")[:200],
        volume=volume,
        liquidity=liquidity,
        yes_price=yes_price,
        no_price=no_price,
        spread=spread_val,
        end_date=end_date,
        token_id_yes=token_yes,
        token_id_no=token_no,
        group_item_title=gm.get("groupItemTitle", ""),
    )


def format_signal(sig, strategy_name):
    """Format a signal for display."""
    parts = [
        f"  [{strategy_name.upper():5s}]",
        f"{sig.action:10s}",
        f"@ {sig.price:.3f}",
        f"size=${sig.size:.0f}",
    ]
    if hasattr(sig, "edge"):
        parts.append(f"edge={sig.edge:.2f}")
    if hasattr(sig, "implied_probability_gap"):
        parts.append(f"IPG={sig.implied_probability_gap:.2f}")
    if hasattr(sig, "confidence"):
        parts.append(f"conf={sig.confidence:.2f}")
    return " | ".join(parts)


async def scan_markets(config, strategy_names, markets, bankroll):
    """Run strategies against real market data."""
    ai = BacktestAIAgent(config)
    sizer = PositionSizer(
        kelly_fraction=config["trading"]["kelly_fraction"],
        max_position_pct=config["trading"]["max_exposure_per_trade"],
    )

    strategies = {}
    if "fade" in strategy_names or "all" in strategy_names:
        strategies["fade"] = FadeStrategy(config, ai, sizer)
    if "arbitrage" in strategy_names or "all" in strategy_names:
        strategies["arbitrage"] = ArbitrageStrategy(config, ai, sizer)
    if "neh" in strategy_names or "all" in strategy_names:
        strategies["neh"] = NothingEverHappensStrategy(config, ai, sizer)

    all_signals = []
    for name, strategy in strategies.items():
        # Reset between strategies so all markets are scanned fresh
        if hasattr(strategy, "reset_processed"):
            strategy.reset_processed()
        signals = await strategy.scan_and_analyze(markets, bankroll)
        for sig in signals:
            all_signals.append((name, sig))

    return all_signals


def main():
    parser = argparse.ArgumentParser(description="Live Polymarket strategy scanner")
    parser.add_argument("--strategy", default="all", help="fade, arbitrage, neh, or all")
    parser.add_argument("--limit", type=int, default=100, help="Max markets to scan")
    parser.add_argument("--min-volume", type=float, default=10000, help="Min volume filter")
    parser.add_argument("--bankroll", type=float, default=10000, help="Simulated bankroll")
    parser.add_argument("--save", action="store_true", help="Save results to JSON")
    args = parser.parse_args()

    config = load_config()
    strategy_names = [args.strategy] if args.strategy != "all" else ["all"]

    print("=" * 70)
    print("LIVE POLYMARKET STRATEGY SCANNER")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Strategies: {args.strategy}")
    print(f"Bankroll: ${args.bankroll:,.0f}")
    print("=" * 70)

    # Fetch real markets
    print(f"\nFetching active markets (min volume ${args.min_volume:,.0f})...")
    raw_markets = fetch_active_markets(limit=args.limit, min_volume=args.min_volume)
    print(f"  Found {len(raw_markets)} qualifying markets")

    if not raw_markets:
        print("No markets found. Try lowering --min-volume.")
        return

    # Convert to Market objects
    markets = []
    for gm in raw_markets:
        try:
            m = gamma_to_market(gm)
            if 0.01 < m.yes_price < 0.99:
                markets.append(m)
        except Exception:
            continue
    print(f"  {len(markets)} markets with valid prices")

    # Show market distribution
    zones = {"consensus_yes": 0, "consensus_no": 0, "mid_range": 0, "low_yes": 0, "low_no": 0}
    for m in markets:
        if m.yes_price >= 0.80:
            zones["consensus_yes"] += 1
        elif m.yes_price <= 0.20:
            zones["consensus_no"] += 1
        elif 0.20 < m.yes_price <= 0.40:
            zones["low_yes"] += 1
        elif 0.60 <= m.yes_price < 0.80:
            zones["low_no"] += 1
        else:
            zones["mid_range"] += 1

    print(f"\n  Price distribution:")
    print(f"    YES consensus (>0.80):  {zones['consensus_yes']:3d}  <- Fade zone")
    print(f"    NO consensus  (<0.20):  {zones['consensus_no']:3d}  <- Fade zone")
    print(f"    Low YES (0.20-0.40):    {zones['low_yes']:3d}  <- Arb zone")
    print(f"    Low NO  (0.60-0.80):    {zones['low_no']:3d}  <- Arb zone")
    print(f"    Mid range (0.40-0.60):  {zones['mid_range']:3d}  <- No signal zone")

    scan_updown_markets(config)

    # Run strategies
    print(f"\nRunning strategies against live data...")
    signals = asyncio.run(scan_markets(config, strategy_names, markets, args.bankroll))

    if not signals:
        print("\n  NO SIGNALS generated. Strategies filtered everything out.")
        print("  This is normal -- it means no markets meet the entry criteria right now.")
    else:
        print(f"\n  {len(signals)} SIGNALS generated:")
        print("-" * 70)

        # Group by market
        by_market = {}
        for name, sig in signals:
            mq = getattr(sig, "market_question", "unknown")
            mid = getattr(sig, "market_id", "unknown")
            key = f"{mid[:12]}... {mq[:60]}"
            if key not in by_market:
                by_market[key] = []
            by_market[key].append((name, sig))

        for market_label, sigs in by_market.items():
            print(f"\n  {market_label}")
            for name, sig in sigs:
                print(f"    {format_signal(sig, name)}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    fade_count = sum(1 for n, _ in signals if n == "fade")
    arb_count = sum(1 for n, _ in signals if n == "arbitrage")
    neh_count = sum(1 for n, _ in signals if n == "neh")
    total_size = sum(s.size for _, s in signals)

    print(f"  Fade signals:      {fade_count}")
    print(f"  Arbitrage signals: {arb_count}")
    print(f"  NEH signals:       {neh_count}")
    print(f"  Total signals:     {len(signals)}")
    print(f"  Total exposure:    ${total_size:,.0f}")
    print(f"  Markets scanned:   {len(markets)}")

    if args.save:
        report_dir = Path(__file__).resolve().parent.parent / "data" / "live_scans"
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = report_dir / f"scan_{ts}.json"
        report = {
            "timestamp": datetime.now().isoformat(),
            "markets_scanned": len(markets),
            "signals": [
                {
                    "strategy": name,
                    "market_id": getattr(sig, "market_id", ""),
                    "market_question": getattr(sig, "market_question", ""),
                    "action": sig.action,
                    "price": sig.price,
                    "size": sig.size,
                    "edge": getattr(sig, "edge", getattr(sig, "implied_probability_gap", 0)),
                    "confidence": getattr(sig, "confidence", 0),
                }
                for name, sig in signals
            ],
            "distribution": zones,
        }
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  Report saved: {report_path}")


if __name__ == "__main__":
    main()
