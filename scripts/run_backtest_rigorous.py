#!/usr/bin/env python3
"""
Rigorous backtest: multiple periods, train/test split, stress scenarios, category-based markets.

Uses config/backtest_rigorous.yaml and config/backtest_markets.yaml (real slugs by category).
Produces aggregate and per-period reports for solid strategy evaluation.

Usage:
  python scripts/run_backtest_rigorous.py
  python scripts/run_backtest_rigorous.py --plan config/backtest_rigorous.yaml --strategies fade arbitrage
  python scripts/run_backtest_rigorous.py --no-train-test --no-stress   # periods only, baseline
  python scripts/run_backtest_rigorous.py --categories both_elections both_macro   # only these categories
  python scripts/run_backtest_rigorous.py --validate 20   # check 20 slugs per strategy have data for first period, then exit
  python scripts/run_backtest_rigorous.py --quick --save-report   # cap at 25 slugs per strategy for faster stress runs
  python scripts/run_backtest_rigorous.py --save-report --status-file data/backtest/reports/RUN_STATUS.md   # live status bar in file for other pane
  python scripts/run_backtest_rigorous.py --verbose   # progress bar + extra logging (loader/strategy)
"""

import argparse
import asyncio
import json
import logging
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Max concurrent API requests when loading market data (avoid rate limits)
DATA_LOAD_WORKERS = 6

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.backtest.data_loader import PolymarketLoader
from src.backtest.jon_becker_loader import JonBeckerDataLoader
from src.backtest.engine import BacktestEngine
from src.backtest.market_list_loader import (
    get_slugs_for_strategy,
    load_backtest_markets_yaml,
)
from src.env_bootstrap import load_project_dotenv

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_rigorous_plan(plan_path: Path) -> dict:
    with open(plan_path) as f:
        return yaml.safe_load(f)


def format_status_bar(current: int, total: int, width: int = 24) -> str:
    """Return a text status bar like [########----------------] 8/24 33% (ASCII for Windows console)"""
    if total <= 0:
        return "[????????????????????????] 0/0"
    pct = (current / total) * 100
    filled = int(width * current / total) if total else 0
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {current}/{total} {pct:.0f}%"


def write_run_status(
    status_path: Optional[Path],
    current: int,
    total: int,
    strategy: str,
    period_label: str,
    stress_name: str,
) -> None:
    """Write status bar and current run info to a file for the other pane."""
    if not status_path:
        return
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        with open(status_path, "w") as f:
            f.write("# Backtest run status\n\n")
            f.write("```\n")
            f.write(format_status_bar(current, total) + "\n")
            f.write("```\n\n")
            f.write(f"**Current:** {strategy} · {period_label} · {stress_name}\n\n")
            f.write(f"*Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")
    except Exception:
        pass


# Jon-Becker dataset loader (lazy init, optional)
_jb_loader: Optional[JonBeckerDataLoader] = None
_jb_loader_init = False


def _get_jb_loader() -> Optional[JonBeckerDataLoader]:
    """Get Jon-Becker loader if dataset is present. None otherwise."""
    global _jb_loader, _jb_loader_init
    if not _jb_loader_init:
        _jb_loader_init = True
        # Check common locations for the dataset
        for path in ["data", "../prediction-market-analysis/data", "data/jon-becker"]:
            jb = JonBeckerDataLoader(path)
            if jb.is_available():
                _jb_loader = jb
                logger.info(f"Jon-Becker dataset found at: {Path(path).resolve()}")
                break
        if _jb_loader is None:
            logger.debug("Jon-Becker dataset not found (optional)")
    return _jb_loader


def _load_one_slug(
    slug: str, start_date: str, end_date: str
) -> Tuple[str, Optional[Any]]:
    """Load one slug's market data. Tries CLOB API first, falls back to Jon-Becker."""
    loader = PolymarketLoader()
    data = loader.load_market_data(slug, start_date, end_date, "1h")

    # Fallback to Jon-Becker dataset if CLOB returned empty (resolved market data issue)
    if data is None or data.empty:
        jb = _get_jb_loader()
        if jb is not None:
            data = jb.load_market_data(slug, start_date, end_date, "1h")
            if data is not None and not data.empty:
                logger.debug(f"Jon-Becker fallback succeeded for {slug}")

    return (slug, data)


def _get_resolution_one(slug: str) -> Tuple[str, Optional[bool]]:
    """Fetch resolution outcome for one slug (for thread pool)."""
    loader = PolymarketLoader()
    return (slug, loader.get_resolution_outcome(slug))


def _get_end_date_one(slug: str) -> Tuple[str, Optional[Any]]:
    """Fetch market end_date for one slug (for thread pool)."""
    loader = PolymarketLoader()
    return (slug, loader.get_market_end_date(slug))


def _fill_period_cache(
    cache: Dict[Tuple[str, str, str], Any],
    slugs: List[str],
    start_date: str,
    end_date: str,
    min_bars: int,
    workers: int = DATA_LOAD_WORKERS,
) -> List[Tuple[str, Any]]:
    """Load missing (slug, start_date, end_date) into cache in parallel; return list of (slug, df) with >= min_bars."""
    missing = [s for s in slugs if (s, start_date, end_date) not in cache]
    if missing:
        with ThreadPoolExecutor(max_workers=min(workers, len(missing))) as ex:
            futures = {
                ex.submit(_load_one_slug, s, start_date, end_date): s for s in missing
            }
            for fut in as_completed(futures):
                slug, data = fut.result()
                cache[(slug, start_date, end_date)] = data
    market_data: List[Tuple[str, Any]] = []
    for slug in slugs:
        data = cache.get((slug, start_date, end_date))
        if data is not None and len(data) >= min_bars:
            market_data.append((slug, data))
    return market_data


def run_one_period(
    strategy: str,
    start_date: str,
    end_date: str,
    slugs: List[str],
    config: dict,
    plan: dict,
    stress_name: Optional[str] = None,
    slippage_mult: float = 1.0,
    fee_bps_override: Optional[int] = None,
    resolution_cache: Optional[Dict[str, Optional[bool]]] = None,
    data_cache: Optional[Dict[Tuple[str, str, str], Any]] = None,
    end_date_cache: Optional[Dict[str, Optional[Any]]] = None,
) -> dict:
    """Run backtest for one strategy over one period with given slugs. Optional stress overrides.
    resolution_cache: optional slug -> outcome to avoid repeated API calls.
    data_cache: shared (slug, start_date, end_date) -> df to avoid re-fetching; filled on miss in parallel."""
    loader = PolymarketLoader()
    bt = plan.get("backtest", {})
    bankroll = bt.get("bankroll", 2000)
    min_bars = bt.get("min_bars", 50)
    max_bars = bt.get("max_bars", 200)
    max_trades_per_market = bt.get("max_trades_per_market", 25)

    if data_cache is not None:
        market_data = _fill_period_cache(
            data_cache, slugs, start_date, end_date, min_bars
        )
    else:
        market_data = []
        for slug in slugs:
            data = loader.load_market_data(slug, start_date, end_date, "1h")
            # Fallback to Jon-Becker if CLOB returned empty
            if data is None or data.empty:
                jb = _get_jb_loader()
                if jb is not None:
                    data = jb.load_market_data(slug, start_date, end_date, "1h")
            if data is not None and len(data) >= min_bars:
                market_data.append((slug, data))

    if not market_data:
        return {
            "strategy": strategy,
            "start_date": start_date,
            "end_date": end_date,
            "period_label": None,
            "stress": stress_name,
            "num_markets": 0,
            "error": f"No data for {len(slugs)} slugs in {start_date}–{end_date}",
        }

    n = len(market_data)
    alloc = bankroll / n
    total_final = 0.0
    total_trades = 0
    total_blocked = 0
    total_exec_cost = 0.0
    results = []
    markets_traded = 0

    for slug, data in market_data:
        if max_bars is not None and len(data) > max_bars:
            data = data.tail(max_bars)
        engine = BacktestEngine(
            config=config,
            strategy_name=strategy,
            initial_bankroll=alloc,
            slippage_mult=slippage_mult,
            fee_bps_override=fee_bps_override,
        )
        if resolution_cache is not None:
            if slug not in resolution_cache:
                resolution_cache[slug] = loader.get_resolution_outcome(slug)
            resolution_outcome = resolution_cache[slug]
        else:
            resolution_outcome = loader.get_resolution_outcome(slug)

        # Get real end_date for NEH and other strategies that need it
        market_end_date = None
        if end_date_cache is not None:
            if slug not in end_date_cache:
                end_date_cache[slug] = loader.get_market_end_date(slug)
            market_end_date = end_date_cache[slug]
        else:
            market_end_date = loader.get_market_end_date(slug)

        result = asyncio.run(
            engine.run(
                data,
                slug=slug,
                on_progress=None,
                resolution_outcome=resolution_outcome,
                max_trades_per_market=max_trades_per_market,
                end_date=market_end_date,
            )
        )
        total_final += result.final_bankroll
        total_trades += result.num_trades
        total_blocked += result.blocked_trade_count
        total_exec_cost += result.execution_cost_total
        net = result.final_bankroll - alloc
        results.append(
            {
                "slug": slug,
                "final": result.final_bankroll,
                "trades": result.num_trades,
                "net": net,
            }
        )
        if result.num_trades > 0:
            markets_traded += 1

    invested = alloc * n
    net_total = total_final - invested
    return_pct = 100 * net_total / invested if invested else 0
    return {
        "strategy": strategy,
        "start_date": start_date,
        "end_date": end_date,
        "period_label": None,
        "stress": stress_name,
        "num_markets": n,
        "markets_traded": markets_traded,
        "initial_bankroll": bankroll,
        "final_bankroll": total_final,
        "net_pnl": net_total,
        "return_pct": return_pct,
        "total_trades": total_trades,
        "blocked_count": total_blocked,
        "execution_cost_total": total_exec_cost,
        "markets": results,
    }


def _period_order(periods_cfg: List[dict]) -> List[str]:
    """Return period labels in the same order as plan's periods list."""
    return [p.get("label") or f"{p['start']}_{p['end']}" for p in periods_cfg]


def validate_slugs(
    strategies: List[str],
    slugs_by_strategy: Dict[str, List[str]],
    first_period_start: str,
    first_period_end: str,
    min_bars: int,
    sample_size: int,
) -> bool:
    """
    Check that a sample of slugs per strategy have >= min_bars in the first period.
    Returns True if all strategies have at least one slug with data; else False.
    Prints pass/fail counts and suggests --end-year rebuild if many fail.
    """
    loader = PolymarketLoader()
    all_ok = True
    n_strategies = len([s for s in strategies if slugs_by_strategy.get(s)])
    n_strategies = max(n_strategies, 1)
    idx = 0
    for strategy in strategies:
        slugs = slugs_by_strategy.get(strategy, [])
        if not slugs:
            print(f"  [{strategy}] No slugs to validate.")
            all_ok = False
            continue
        idx += 1
        bar = format_status_bar(idx, n_strategies)
        print(f"  {bar}  Validating {strategy}...")
        sample = slugs[:sample_size]
        passed = 0
        for slug in sample:
            data = loader.load_market_data(
                slug, first_period_start, first_period_end, "1h"
            )
            if data is not None and len(data) >= min_bars:
                passed += 1
        print(
            f"      [{strategy}] {passed}/{len(sample)} slugs have >= {min_bars} bars in {first_period_start}–{first_period_end}"
        )
        if passed == 0:
            all_ok = False
    if not all_ok:
        print("\n  Suggestion: Rebuild market list for this date range, e.g.:")
        print(
            "    python scripts/build_backtest_market_list.py --end-year 2024 --max-events 8000"
        )
        print("  Then re-run the backtest.")
    return all_ok


def _strategy_results_in_period_order(
    all_results: List[dict],
    strategy: str,
    baseline_stress_name: Optional[str],
    ordered_labels: List[str],
) -> List[dict]:
    """Return baseline results for one strategy sorted by period order (no errors)."""
    strategy_results = [
        r
        for r in all_results
        if r.get("strategy") == strategy
        and "error" not in r
        and r.get("stress") == baseline_stress_name
    ]
    label_to_index = {lb: i for i, lb in enumerate(ordered_labels)}
    strategy_results.sort(
        key=lambda r: label_to_index.get(r.get("period_label", ""), 999)
    )
    return strategy_results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rigorous backtest: multi-period, train/test, stress"
    )
    parser.add_argument(
        "--plan", default="config/backtest_rigorous.yaml", help="Rigorous plan YAML"
    )
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=["fade", "arbitrage"],
        help="Strategies to run (add --strategies weather to include; slow due to API calls)",
    )
    parser.add_argument(
        "--no-train-test",
        action="store_true",
        help="Run all periods together (no train/test split)",
    )
    parser.add_argument(
        "--no-stress", action="store_true", help="Skip stress scenarios (baseline only)"
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=None,
        help="Category keys to use (e.g. both_elections both_macro); default: all categories meeting min_markets_per_category",
    )
    parser.add_argument(
        "--strategy-categories-only",
        action="store_true",
        help="Run each strategy only on its dedicated categories (exclude 'both') for a purity test",
    )
    parser.add_argument(
        "--validate",
        type=int,
        default=None,
        metavar="N",
        help="Validate only: check N slugs per strategy have min_bars in first period, then exit (suggests --end-year rebuild if many fail)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Cap at 25 slugs per strategy for faster runs (overrides plan max_slugs_per_strategy_per_period)",
    )
    parser.add_argument(
        "--max-slugs",
        type=int,
        default=None,
        metavar="N",
        help="Cap slugs per strategy at N (e.g. 60 for 40%% of full 150)",
    )
    parser.add_argument(
        "--status-file",
        default=None,
        metavar="PATH",
        help="Write live status bar to this file (e.g. data/backtest/reports/RUN_STATUS.md) for other pane",
    )
    parser.add_argument(
        "--save-report", action="store_true", help="Write JSON report to report_dir"
    )
    parser.add_argument(
        "--live-markets",
        action="store_true",
        help="For weather, use live Gamma-discovered markets instead of static YAML market lists",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable extra logging (loader/strategy); default is progress bar only",
    )
    args = parser.parse_args()

    if not args.verbose:
        for _log in ("src.backtest", "src.strategies"):
            logging.getLogger(_log).setLevel(logging.WARNING)

    repo = Path(__file__).resolve().parent.parent
    plan_path = repo / args.plan
    if not plan_path.is_file():
        logger.error("Plan not found: %s", plan_path)
        return 1

    load_project_dotenv(repo, quiet=True)

    config = load_config()
    plan = load_rigorous_plan(plan_path)

    market_list_path = repo / plan.get(
        "market_list_path", "config/backtest_markets.yaml"
    )
    need_market_list = not (
        args.live_markets
        and len(args.strategies) == 1
        and args.strategies[0] == "weather"
    )
    if need_market_list and not market_list_path.is_file():
        logger.error("Market list not found: %s", market_list_path)
        return 1

    min_per_cat = plan.get("min_markets_per_category", 30)
    max_slugs = plan.get("max_slugs_per_strategy_per_period", 150)
    if args.max_slugs is not None:
        max_slugs = args.max_slugs
    if args.quick:
        max_slugs = 25
    periods_cfg = plan.get("periods", [])
    if not periods_cfg:
        logger.error("No periods defined in plan")
        return 1

    train_test = plan.get("train_test", {}) if not args.no_train_test else {}
    train_test_enabled = train_test.get("enabled", False) and not args.no_train_test
    train_labels = set(train_test.get("train_period_labels", []))
    test_labels = set(train_test.get("test_period_labels", []))

    walk_forward_cfg = plan.get("walk_forward", {})
    walk_forward_enabled = walk_forward_cfg.get("enabled", False)
    ordered_period_labels = _period_order(periods_cfg)

    stress_scenarios = plan.get("stress_scenarios", {})
    if args.no_stress:
        stress_scenarios = {"baseline": {"slippage_mult": 1.0, "fee_bps": None}}
    elif "baseline" not in stress_scenarios and stress_scenarios:
        stress_scenarios = {
            "baseline": {"slippage_mult": 1.0, "fee_bps": None},
            **stress_scenarios,
        }
    baseline_stress_name = (
        "baseline"
        if "baseline" in stress_scenarios
        else (list(stress_scenarios.keys())[0] if stress_scenarios else None)
    )

    # Resolve period labels to (start, end)
    period_by_label = {}
    for p in periods_cfg:
        label = p.get("label") or f"{p['start']}_{p['end']}"
        period_by_label[label] = (p["start"], p["end"], label)

    all_results: List[dict] = []
    train_aggregate: Dict[str, list] = {s: [] for s in args.strategies}
    test_aggregate: Dict[str, list] = {s: [] for s in args.strategies}

    print("\n" + "=" * 70)
    print("RIGOROUS BACKTEST — Multi-period, train/test, stress")
    print("=" * 70)
    print(f"Plan: {plan_path}")
    if need_market_list:
        print(f"Market list: {market_list_path} (min_markets_per_category={min_per_cat})")
    else:
        print("Market list: bypassed (weather live Gamma discovery)")
    if args.categories:
        print(f"Categories filter: {args.categories}")
    print(
        f"Periods: {len(periods_cfg)} | Train/test: {train_test_enabled} | Stress: {len(stress_scenarios)}"
    )
    if args.quick:
        print("Quick mode: max 25 slugs per strategy")
    print()

    if args.live_markets and len(args.strategies) == 1 and args.strategies[0] == "weather":
        try:
            from scripts.run_backtest_weather import (
                fetch_weather_rows,
                run_backtest as run_weather_backtest,
                save_report as save_weather_report,
            )
        except BaseException as exc:
            logger.error("Unable to load live weather backtest helpers: %s", exc)
            return 1
        print("Running weather against live Gamma-discovered markets")
        rows = fetch_weather_rows(include_closed=False)
        if not rows:
            logger.error("No live weather markets found via Gamma discovery")
            return 1
        results = run_weather_backtest(
            rows,
            config=config,
            bankroll=float(plan.get("backtest", {}).get("bankroll", 2000)),
            quick=bool(args.quick),
        )
        if args.save_report:
            report_path = save_weather_report(results)
            print(f"Weather report saved: {report_path}")
        print(
            f"Weather live run: scanned={results.get('markets_scanned', 0)} "
            f"signals={results.get('signals_generated', 0)} "
            f"total_pnl=${results.get('total_pnl', 0):.2f}"
        )
        return 0

    # Optional: validate only (check slug data for first period)
    if args.validate is not None:
        first_period = periods_cfg[0]
        first_start, first_end = first_period["start"], first_period["end"]
        min_bars = plan.get("backtest", {}).get("min_bars", 50)
        slugs_by_strategy = {}
        for strategy in args.strategies:
            slugs_by_strategy[strategy] = get_slugs_for_strategy(
                market_list_path,
                strategy=strategy,
                categories=args.categories,
                min_markets_per_category=min_per_cat,
                max_slugs_per_strategy=max_slugs,
            )
        print(
            f"Validation (first period {first_start}–{first_end}, min_bars={min_bars}):"
        )
        validate_slugs(
            args.strategies,
            slugs_by_strategy,
            first_start,
            first_end,
            min_bars,
            args.validate,
        )
        return 0

    # Pre-load slugs and compute total runs for status bar
    strategy_slugs: List[Tuple[str, List[str]]] = []
    # Load market data for category breakdown
    market_data_yaml = (
        load_backtest_markets_yaml(market_list_path)
        if market_list_path.is_file()
        else {}
    )

    for strategy in args.strategies:
        # --strategy-categories-only: exclude 'both' categories
        cat_filter = args.categories
        if args.strategy_categories_only and cat_filter is None:
            by_strat = (market_data_yaml.get("by_strategy") or {}).get(strategy) or {}
            cat_filter = [c for c in by_strat.keys() if not c.startswith("both_")]

        slugs = get_slugs_for_strategy(
            market_list_path,
            strategy=strategy,
            categories=cat_filter,
            min_markets_per_category=min_per_cat,
            max_slugs_per_strategy=max_slugs,
        )
        if not slugs:
            logger.warning(
                "No slugs for strategy %s (min_markets_per_category=%s)",
                strategy,
                min_per_cat,
            )
            continue
        strategy_slugs.append((strategy, slugs))
        # Category breakdown: which categories contributed slugs
        by_strat = market_data_yaml.get("by_strategy") or {}
        strat_cats = by_strat.get(strategy) or {}
        cat_counts = [
            (c, len(s or []))
            for c, s in strat_cats.items()
            if s and len(s) >= min_per_cat
        ]
        cat_str = ", ".join(f"{c}({n})" for c, n in cat_counts[:6])
        if len(cat_counts) > 6:
            cat_str += f" ... +{len(cat_counts) - 6} more"
        print(
            f"  [{strategy}] Using {len(slugs)} slugs from: {cat_str or 'all categories'}"
        )

    total_runs = (
        len(strategy_slugs) * len(ordered_period_labels) * len(stress_scenarios)
    )
    status_path = (repo / args.status_file) if args.status_file else None
    if status_path:
        print(f"Status file: {status_path} (open in another pane to watch progress)")

    # Shared caches: load each (slug, period) once; resolution per slug once. Cuts API calls by ~8x.
    data_cache: Dict[Tuple[str, str, str], Any] = {}
    resolution_cache: Dict[str, Optional[bool]] = {}
    end_date_cache: Dict[str, Optional[Any]] = {}

    # Preload all period data in parallel (one batch per period). Makes the 32 runs API-free.
    all_slugs = sorted(set(s for _, sl in strategy_slugs for s in sl))
    min_bars = plan.get("backtest", {}).get("min_bars", 50)
    print(
        f"Preloading data for {len(all_slugs)} slugs × {len(ordered_period_labels)} periods ({DATA_LOAD_WORKERS} workers)..."
    )
    for label in ordered_period_labels:
        start_date, end_date = period_by_label[label][0], period_by_label[label][1]
        market_data = _fill_period_cache(
            data_cache, all_slugs, start_date, end_date, min_bars
        )
        print(
            f"  {label}: {len(market_data)}/{len(all_slugs)} markets with >= {min_bars} bars"
        )
    # Prefill resolution outcome for all slugs (avoids 32×N resolution calls during runs)
    print(f"Preloading resolution outcomes for {len(all_slugs)} slugs...")
    with ThreadPoolExecutor(max_workers=min(DATA_LOAD_WORKERS, len(all_slugs))) as ex:
        futs = [ex.submit(_get_resolution_one, s) for s in all_slugs]
        for fut in as_completed(futs):
            slug, outcome = fut.result()
            resolution_cache[slug] = outcome
    # Prefill end_dates for all slugs (needed for NEH strategy)
    print(f"Preloading end dates for {len(all_slugs)} slugs...")
    with ThreadPoolExecutor(max_workers=min(DATA_LOAD_WORKERS, len(all_slugs))) as ex:
        futs = [ex.submit(_get_end_date_one, s) for s in all_slugs]
        for fut in as_completed(futs):
            slug, ed = fut.result()
            end_date_cache[slug] = ed
    print()

    run_index = 0
    for strategy, slugs in strategy_slugs:
        for label in ordered_period_labels:
            start_date, end_date = period_by_label[label][0], period_by_label[label][1]
            for stress_name, stress_cfg in stress_scenarios.items():
                slip_mult = float(stress_cfg.get("slippage_mult", 1.0))
                fee_bps = stress_cfg.get("fee_bps")
                fee_bps_int = int(fee_bps) if fee_bps is not None else None

                report = run_one_period(
                    strategy=strategy,
                    start_date=start_date,
                    end_date=end_date,
                    slugs=slugs,
                    config=config,
                    plan=plan,
                    stress_name=stress_name,
                    slippage_mult=slip_mult,
                    fee_bps_override=fee_bps_int,
                    resolution_cache=resolution_cache,
                    data_cache=data_cache,
                    end_date_cache=end_date_cache,
                )
                report["period_label"] = label
                all_results.append(report)

                run_index += 1
                bar = format_status_bar(run_index, total_runs)
                print(f"  {bar}  {strategy} {label} {stress_name}")
                write_run_status(
                    status_path, run_index, total_runs, strategy, label, stress_name
                )

                if "error" in report:
                    print(f"      {report['error']}")
                else:
                    is_test = train_test_enabled and label in test_labels
                    bucket = test_aggregate if is_test else train_aggregate
                    if stress_name == baseline_stress_name:
                        bucket[strategy].append(report)
                    pnl = report.get("net_pnl", 0)
                    ret = report.get("return_pct", 0)
                    print(
                        f"      {report['num_markets']} markets | ${pnl:+.2f} ({ret:+.1f}%) | {report.get('total_trades', 0)} trades"
                    )

    if status_path and total_runs > 0:
        write_run_status(status_path, total_runs, total_runs, "—", "—", "done")

    print("\n" + "=" * 70)
    print("AGGREGATE BY STRATEGY (baseline, all periods)")
    print("=" * 70)
    per_strategy_metrics: Dict[str, dict] = {}
    for strategy in args.strategies:
        strategy_results = _strategy_results_in_period_order(
            all_results, strategy, baseline_stress_name, ordered_period_labels
        )
        if not strategy_results:
            continue
        total_pnl = sum(r.get("net_pnl", 0) for r in strategy_results)
        total_trades = sum(r.get("total_trades", 0) for r in strategy_results)
        n_periods = len(strategy_results)
        line = f"  {strategy:12} | {n_periods} period(s) | Net PnL ${total_pnl:+.2f} | {total_trades} trades"

        # Per-period returns (decimal) for Sharpe and max drawdown (baseline only)
        returns_decimal = [r.get("return_pct", 0) / 100.0 for r in strategy_results]
        per_period_returns_pct = [r.get("return_pct", 0) for r in strategy_results]
        if returns_decimal:
            mean_ret = sum(returns_decimal) / len(returns_decimal)
            variance = sum((x - mean_ret) ** 2 for x in returns_decimal) / len(
                returns_decimal
            )
            std_ret = math.sqrt(variance) if variance > 0 else 0.0
            # Annualize using number of periods (e.g. 4 quarters -> sqrt(4))
            n_per = len(returns_decimal)
            sharpe_annual = (
                (mean_ret / std_ret * math.sqrt(n_per)) if std_ret > 0 else 0.0
            )
            cum = 1.0
            cummax = 1.0
            max_dd = 0.0
            for r in returns_decimal:
                cum *= 1.0 + r
                cummax = max(cummax, cum)
                dd = (cum - cummax) / cummax if cummax > 0 else 0.0
                max_dd = min(max_dd, dd)
            max_drawdown_pct = 100.0 * max_dd
            per_strategy_metrics[strategy] = {
                "sharpe_annual": round(sharpe_annual, 4),
                "max_drawdown_pct": round(max_drawdown_pct, 4),
                "per_period_returns_pct": per_period_returns_pct,
            }
            line += f" | Sharpe: {sharpe_annual:.2f} | MaxDD: {max_drawdown_pct:.1f}%"
        print(line)

    if walk_forward_enabled:
        print("\n" + "=" * 70)
        print("Walk-forward test PnL by period")
        print("=" * 70)
        wf_by_period: List[dict] = []
        wf_aggregate: Dict[str, float] = {s: 0.0 for s in args.strategies}
        for label in ordered_period_labels:
            row: Dict[str, Any] = {"period_label": label, "strategies": {}}
            for strategy in args.strategies:
                report = next(
                    (
                        r
                        for r in all_results
                        if r.get("strategy") == strategy
                        and r.get("period_label") == label
                        and "error" not in r
                        and r.get("stress") == baseline_stress_name
                    ),
                    None,
                )
                pnl = report.get("net_pnl", 0) if report else 0
                row["strategies"][strategy] = pnl
                wf_aggregate[strategy] += pnl
            wf_by_period.append(row)
            strat_pnls = " | ".join(
                f"{s}: ${row['strategies'][s]:+.2f}" for s in args.strategies
            )
            print(f"  {label}: {strat_pnls}")
        print(
            "  Aggregate: "
            + " | ".join(f"{s}: ${wf_aggregate[s]:+.2f}" for s in args.strategies)
        )
        walk_forward_test_results: Dict[str, Any] = {
            "by_period": wf_by_period,
            "aggregate": wf_aggregate,
        }
    else:
        walk_forward_test_results = None

    if train_test_enabled and (train_labels or test_labels):
        print("\n" + "=" * 70)
        print("TRAIN vs TEST (out-of-sample)")
        print("=" * 70)
        for strategy in args.strategies:
            train_reports = train_aggregate.get(strategy, [])
            test_reports = test_aggregate.get(strategy, [])
            train_pnl = sum(r.get("net_pnl", 0) for r in train_reports)
            test_pnl = sum(r.get("net_pnl", 0) for r in test_reports)
            print(
                f"  {strategy:12} | Train PnL ${train_pnl:+.2f} | Test PnL ${test_pnl:+.2f}"
            )

    if args.save_report and all_results:
        report_dir = repo / plan.get("output", {}).get(
            "report_dir", "data/backtest/reports"
        )
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = report_dir / f"backtest_rigorous_{ts}.json"
        payload = {
            "plan": str(plan_path),
            "run_at": ts,
            "train_test_enabled": train_test_enabled,
            "stress_scenarios": list(stress_scenarios.keys()),
            "results": all_results,
            "train_aggregate": {
                k: [r.get("net_pnl") for r in v] for k, v in train_aggregate.items()
            },
            "test_aggregate": {
                k: [r.get("net_pnl") for r in v] for k, v in test_aggregate.items()
            },
            "per_strategy_metrics": per_strategy_metrics,
        }
        if walk_forward_test_results is not None:
            payload["walk_forward_test_results"] = walk_forward_test_results
        with open(report_path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nReport saved: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
