#!/usr/bin/env python3
"""Weather strategy runner for current Polymarket weather markets.

Uses Gamma-only market discovery by default. Historical price/metric enrichment from
PolymarketData is optional when ``POLYMARKETDATA_API_KEY`` is present.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis.kelly_sizer import KellySizer
from src.analysis.math_utils import PositionSizer
from src.backtest.data_loader import PolymarketDataLoader, PolymarketLoader
from src.market.scanner import Market, MarketScanner
from src.strategies.weather import WeatherStrategy

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

API_KEY = os.getenv("POLYMARKETDATA_API_KEY")
PM_API = PolymarketDataLoader(api_key=API_KEY) if API_KEY else None
GAMMA = PolymarketLoader()
REPORT_DIR = Path("data/backtest/reports")
LOOKBACK_HOURS = 72
BACKTEST_CITY_COORDS: Dict[str, Tuple[Tuple[float, float], str]] = {
    "new-york": ((40.7769, -73.8740), "KLGA"),
    "nyc": ((40.7769, -73.8740), "KLGA"),
    "london": ((51.4700, -0.4543), "EGLL"),
    "tokyo": ((35.5494, 139.7798), "RJTT"),
    "seoul": ((37.4691, 126.4510), "RKSS"),
    "paris": ((49.0097, 2.5479), "LFPB"),
    "dubai": ((25.2532, 55.3657), "OMDB"),
    "singapore": ((1.3644, 103.9915), "WSSS"),
    "sydney": ((-33.9399, 151.1753), "YSSY"),
    "manila": ((14.5086, 121.0198), "RPLL"),
    "karachi": ((24.9065, 67.1608), "OPKC"),
    "dhaka": ((23.8433, 90.3978), "VGHS"),
    "mumbai": ((19.0896, 72.8656), "VABB"),
    "delhi": ((28.5562, 77.1000), "VIDP"),
    "bangkok": ((13.6900, 100.7501), "VTBS"),
    "jakarta": ((-6.1256, 106.6559), "WIII"),
    "hong-kong": ((22.3080, 113.9185), "VHHH"),
    "shanghai": ((31.1443, 121.8083), "ZSPD"),
    "beijing": ((40.0799, 116.6031), "ZBAA"),
}


def load_config() -> Dict[str, Any]:
    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def parse_dt(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_weather_rows(
    *,
    cities: Optional[List[str]] = None,
    include_closed: bool = False,
    limit: int = 400,
) -> List[Dict[str, Any]]:
    scanner = MarketScanner(
        {
            "polymarket": {"min_liquidity": 0},
            "strategies": {
                "weather": {
                    "enabled": True,
                    "min_liquidity": 0,
                    "min_volume": 0,
                }
            },
        }
    )
    markets = scanner.fetch_weather_markets(cities=cities, limit=limit)
    if not markets:
        return []
    now_utc = datetime.now(timezone.utc)
    results: List[Dict[str, Any]] = []
    for market in markets:
        status = "closed" if (market.end_date and market.end_date <= now_utc.replace(tzinfo=None)) else "open"
        if status == "closed" and not include_closed:
            continue
        slug = str(market.slug or "").strip().lower()
        city = str(market.group_item_title or "").strip().lower()
        if not city:
            city = slug.replace("highest-temperature-in-", "").split("-on-", 1)[0]
        end_dt = market.end_date
        if end_dt and end_dt.tzinfo is not None:
            end_dt = end_dt.astimezone(timezone.utc)
        if status == "closed" and end_dt and (now_utc - end_dt).days > 7:
            continue
        results.append(
            {
                "id": market.id,
                "marketId": market.id,
                "slug": slug,
                "question": market.question,
                "title": market.question,
                "description": market.description,
                "volume": market.volume,
                "liquidity": market.liquidity,
                "yes_price": market.yes_price,
                "no_price": market.no_price,
                "spread": market.spread,
                "end_dt": end_dt,
                "status": status,
                "city": city,
                "groupItemTitle": market.group_item_title,
                "tokens": [market.token_id_no, market.token_id_yes],
            }
        )

    results.sort(
        key=lambda row: float((row.get("volume") or 0) + (row.get("liquidity") or 0)),
        reverse=True,
    )
    return results


def build_market(
    row: Dict[str, Any],
    yes_price: float,
    spread: float,
    liquidity: Optional[float] = None,
) -> Market:
    tokens = row.get("tokens", []) or []
    end_dt = row.get("end_dt")
    end_naive = end_dt.replace(tzinfo=None) if end_dt else None
    return Market(
        id=str(row.get("marketId") or row.get("id") or row.get("slug")),
        question=str(row.get("question") or row.get("title") or row.get("slug")),
        description=str(row.get("description") or row.get("slug") or "")[:300],
        volume=float(row.get("volume", 0) or 0),
        liquidity=float(liquidity if liquidity is not None else row.get("liquidity", 0) or 0),
        yes_price=float(yes_price),
        no_price=float(max(0.0, min(1.0, 1.0 - yes_price))),
        spread=float(spread),
        end_date=end_naive,
        token_id_yes=str(tokens[1]) if len(tokens) > 1 else "",
        token_id_no=str(tokens[0]) if len(tokens) > 0 else "",
        group_item_title=str(row.get("city") or ""),
        slug=str(row.get("slug") or ""),
    )


def fetch_price_series(slug: str, *, lookback_hours: int = LOOKBACK_HOURS) -> pd.DataFrame:
    """Fetch price series for a market.
    
    PolymarketData returns nested format: {"No": [{"t","p"},...], "Yes": [{"t","p"},...]}
    We extract YES prices (index 1) and flatten to {"t": ..., "price": ..., "spread": ..., "liquidity": ...}
    """
    if PM_API is None:
        return pd.DataFrame()

    end_ts = datetime.now(timezone.utc)
    start_ts = end_ts - timedelta(hours=lookback_hours)
    prices = PM_API.fetch_prices(
        slug,
        start_ts=start_ts.isoformat(),
        end_ts=end_ts.isoformat(),
        resolution="1h",
    )
    time.sleep(1.2)  # Rate limit avoidance
    metrics = PM_API.fetch_metrics(
        slug,
        start_ts=start_ts.isoformat(),
        end_ts=end_ts.isoformat(),
        resolution="1h",
    )
    if prices.empty:
        return pd.DataFrame()

    df = prices.copy()

    # PolymarketData v2 returns nested {"No": [...], "Yes": [...]} with t,p per entry
    # Extract YES prices (token index 1) if present, otherwise flat format
    if "Yes" in df.columns and "No" in df.columns:
        # Nested format — extract YES series
        yes_data = df["Yes"].iloc[0] if not df["Yes"].empty else []
        if isinstance(yes_data, list) and yes_data:
            rows = [{"t": d["t"], "price": d["p"]} for d in yes_data]
            df = pd.DataFrame(rows)
        else:
            return pd.DataFrame()
    elif "t" in df.columns and "p" in df.columns:
        # Flat format
        df = df.rename(columns={"p": "price"})
    elif "t" in df.columns:
        for candidate in ("price", "yes_price", "value"):
            if candidate in df.columns:
                df = df.rename(columns={candidate: "price"})
                break
        if "price" not in df.columns:
            return pd.DataFrame()
    else:
        return pd.DataFrame()

    if "t" not in df.columns or "price" not in df.columns:
        return pd.DataFrame()

    df["t"] = pd.to_datetime(df["t"], utc=True)
    df = df.sort_values("t").drop_duplicates("t")

    if metrics.empty:
        df["spread"] = 0.02
        df["liquidity"] = pd.NA
        return df

    metrics = metrics.copy()
    if "t" not in metrics.columns:
        metrics["spread"] = 0.02
        metrics["liquidity"] = pd.NA
    else:
        metrics["t"] = pd.to_datetime(metrics["t"], utc=True)
        metrics = metrics.sort_values("t")
        if "spread" not in metrics.columns:
            for candidate in ("bid_ask_spread", "spread_pct"):
                if candidate in metrics.columns:
                    metrics = metrics.rename(columns={candidate: "spread"})
                    break
        if "spread" not in metrics.columns:
            metrics["spread"] = 0.02
        if "liquidity" not in metrics.columns:
            metrics["liquidity"] = pd.NA

    merged = pd.merge_asof(
        df,
        metrics[["t", "spread", "liquidity"]],
        on="t",
        direction="nearest",
        tolerance=pd.Timedelta("2h"),
    )
    merged["spread"] = merged["spread"].fillna(0.02)
    return merged


def snapshot_price_series(row: Dict[str, Any]) -> pd.DataFrame:
    yes_price = float(row.get("yes_price", 0.5) or 0.5)
    spread = float(row.get("spread", 0.02) or 0.02)
    liquidity = row.get("liquidity")
    return pd.DataFrame(
        [
            {
                "t": datetime.now(timezone.utc),
                "price": yes_price,
                "spread": spread,
                "liquidity": liquidity,
            }
        ]
    )


def init_strategy(config: Dict[str, Any]) -> WeatherStrategy:
    trading_cfg = config.get("trading", {}) or {}
    position_sizer = PositionSizer(
        kelly_fraction=trading_cfg.get("kelly_fraction", 0.25),
        max_position_pct=trading_cfg.get("max_exposure_per_trade", 0.05),
        min_position=trading_cfg.get("default_position_size", 10),
        max_position=trading_cfg.get("max_position_size", 15),
    )
    strategy = WeatherStrategy(config, position_sizer, KellySizer(config))
    strategy.enabled = True
    strategy.metar_enabled = False
    orig_parse_market_location = strategy._parse_market_location

    def _parse_market_location_with_fallback(question: str, description: str):
        found = orig_parse_market_location(question, description)
        if found is not None:
            return found
        text = f"{question} {description}".lower().replace("_", "-")
        for city_key, coords in BACKTEST_CITY_COORDS.items():
            city_text = city_key.replace("-", " ")
            if city_key in text or city_text in text:
                return coords
        return None

    strategy._parse_market_location = _parse_market_location_with_fallback
    return strategy


def calculate_pnl(
    *,
    size: float,
    action: str,
    entry_yes: float,
    current_yes: float,
    outcome_yes: Optional[bool],
) -> Tuple[float, bool]:
    if action == "BUY_YES":
        entry_price = max(entry_yes, 0.01)
        exit_price = 1.0 if outcome_yes is True else 0.0 if outcome_yes is False else current_yes
    else:
        entry_price = max(1.0 - entry_yes, 0.01)
        exit_price = 0.0 if outcome_yes is True else 1.0 if outcome_yes is False else (1.0 - current_yes)
    shares = size / entry_price
    pnl = shares * exit_price - size
    return float(pnl), pnl > 0


def run_market_backtest(
    *,
    strategy: WeatherStrategy,
    row: Dict[str, Any],
    price_df: pd.DataFrame,
    bankroll: float,
) -> Optional[Dict[str, Any]]:
    if price_df.empty:
        return None

    # This script is intended to evaluate currently open weather markets using
    # the latest market price plus the latest forecast, not replay the same
    # current forecast across stale historical snapshots.
    snapshot = price_df.iloc[-1]
    liquidity = snapshot.get("liquidity")
    market = build_market(
        row,
        yes_price=float(snapshot["price"]),
        spread=float(snapshot.get("spread", 0.02) or 0.02),
        liquidity=float(liquidity) if pd.notna(liquidity) else None,
    )
    import asyncio
    try:
        signals = asyncio.run(strategy.scan_and_analyze([market], bankroll))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            signals = loop.run_until_complete(strategy.scan_and_analyze([market], bankroll))
        finally:
            loop.close()
    scan_stats = dict(getattr(strategy, "_scan_stats", {}) or {})
    signal = signals[0] if signals else None

    if not signal:
        return {"signal": None, "scan_stats": scan_stats}

    current_yes = float(price_df.iloc[-1]["price"])
    outcome_yes = None
    if str(row.get("status")) == "closed":
        outcome_yes = GAMMA.get_resolution_outcome(str(row.get("slug")))
    pnl, won = calculate_pnl(
        size=float(signal.size),
        action=str(signal.action),
        entry_yes=float(signal.market_price),
        current_yes=current_yes,
        outcome_yes=outcome_yes,
    )
    return {
        "signal": signal,
        "current_yes": current_yes,
        "outcome_yes": outcome_yes,
        "pnl": round(pnl, 4),
        "won": won if outcome_yes is not None else None,
        "scan_stats": scan_stats,
    }


def run_backtest(
    rows: List[Dict[str, Any]],
    *,
    config: Dict[str, Any],
    bankroll: float,
    quick: bool,
) -> Dict[str, Any]:
    selected_rows = rows[:25] if quick else rows
    strategy = init_strategy(config)
    results: Dict[str, Any] = {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "strategy": "weather",
        "markets_total": len(rows),
        "markets_scanned": len(selected_rows),
        "markets_with_data": 0,
        "signals_generated": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_pnl": 0.0,
        "avg_pnl_per_trade": 0.0,
        "by_city": {},
        "signal_details": [],
        "errors": [],
    }

    for idx, row in enumerate(selected_rows, start=1):
        slug = str(row.get("slug"))
        logger.info("[%d/%d] processing %s", idx, len(selected_rows), slug)
        try:
            price_df = fetch_price_series(slug)
            if price_df.empty:
                price_df = snapshot_price_series(row)
            results["markets_with_data"] += 1
            market_result = run_market_backtest(
                strategy=strategy,
                row=row,
                price_df=price_df,
                bankroll=bankroll,
            )
            if not market_result or not market_result.get("signal"):
                continue

            signal = market_result["signal"]
            city = str(row.get("city"))
            pnl = float(market_result["pnl"])
            outcome_yes = market_result["outcome_yes"]
            realized = outcome_yes is not None

            if realized:
                if pnl > 0:
                    results["wins"] += 1
                elif pnl < 0:
                    results["losses"] += 1
                results["realized_pnl"] += pnl
            else:
                results["unrealized_pnl"] += pnl

            results["signals_generated"] += 1
            city_stats = results["by_city"].setdefault(
                city,
                {"signals": 0, "wins": 0, "losses": 0, "pnl": 0.0},
            )
            city_stats["signals"] += 1
            city_stats["pnl"] += pnl
            if realized:
                if pnl > 0:
                    city_stats["wins"] += 1
                elif pnl < 0:
                    city_stats["losses"] += 1

            results["signal_details"].append(
                {
                    "slug": slug,
                    "city": city,
                    "status": row.get("status"),
                    "action": signal.action,
                    "forecast_prob": round(float(signal.forecast_prob), 4),
                    "entry_price_yes": round(float(signal.market_price), 4),
                    "current_price_yes": round(float(market_result["current_yes"]), 4),
                    "gap": round(float(signal.gap), 4),
                    "size": round(float(signal.size), 4),
                    "pnl": round(pnl, 4),
                    "resolved": realized,
                    "won": market_result["won"],
                    "question": signal.market_question,
                }
            )
        except Exception as exc:
            logger.exception("Weather backtest failed for %s", slug)
            results["errors"].append(f"{slug}: {exc}")

    resolved_trades = results["wins"] + results["losses"]
    results["win_rate"] = round(results["wins"] / resolved_trades, 4) if resolved_trades else 0.0
    results["realized_pnl"] = round(results["realized_pnl"], 4)
    results["unrealized_pnl"] = round(results["unrealized_pnl"], 4)
    results["total_pnl"] = round(results["realized_pnl"] + results["unrealized_pnl"], 4)
    results["avg_pnl_per_trade"] = round(
        results["total_pnl"] / results["signals_generated"], 4
    ) if results["signals_generated"] else 0.0

    for city_stats in results["by_city"].values():
        resolved_city = city_stats["wins"] + city_stats["losses"]
        city_stats["pnl"] = round(city_stats["pnl"], 4)
        city_stats["win_rate"] = round(city_stats["wins"] / resolved_city, 4) if resolved_city else 0.0

    return results


def save_report(results: Dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"weather_backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the weather strategy backtest on live markets")
    parser.add_argument("--quick", action="store_true", help="Limit to first 25 markets")
    parser.add_argument("--save-report", action="store_true", help="Save JSON report")
    parser.add_argument("--cities", nargs="+", help="Optional city filter")
    parser.add_argument("--closed", action="store_true", help="Include recently closed markets")
    parser.add_argument("--bankroll", type=float, default=10000.0, help="Sizing bankroll")
    args = parser.parse_args()

    rows = fetch_weather_rows(cities=args.cities, include_closed=args.closed)
    if not rows:
        logger.error("No live weather markets found")
        return 1

    logger.info("Found %d live weather markets", len(rows))
    results = run_backtest(
        rows,
        config=load_config(),
        bankroll=args.bankroll,
        quick=args.quick,
    )

    print("\n============================================================")
    print("WEATHER BACKTEST RESULTS")
    print("============================================================")
    print(f"Markets total:         {results['markets_total']}")
    print(f"Markets scanned:       {results['markets_scanned']}")
    print(f"Markets with data:     {results['markets_with_data']}")
    print(f"Signals generated:     {results['signals_generated']}")
    print(f"Wins / Losses:         {results['wins']} / {results['losses']}")
    print(f"Win rate:              {results['win_rate']:.2%}")
    print(f"Realized PnL:          ${results['realized_pnl']:.2f}")
    print(f"Unrealized PnL:        ${results['unrealized_pnl']:.2f}")
    print(f"Total PnL:             ${results['total_pnl']:.2f}")
    print(f"Avg PnL / trade:       ${results['avg_pnl_per_trade']:.2f}")
    print(f"Cities covered:        {', '.join(sorted(results['by_city'].keys()))}")
    if results["errors"]:
        print(f"Errors:                {len(results['errors'])}")

    if args.save_report:
        report_path = save_report(results)
        print(f"Report saved to:       {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
