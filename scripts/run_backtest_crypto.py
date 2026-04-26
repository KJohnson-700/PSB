#!/usr/bin/env python3
"""
Run a Bitcoin, SOL, or ETH Up/Down strategy backtest against real Binance OHLCV data.

Downloads historical candles from Binance (free, no key), caches them to
data/backtest/ohlcv/ as Parquet, replays the EXACT same indicator math and
signal logic that runs live, then prints a detailed results report.

Usage
-----
  # BTC 15m — full in-sample run (no validation, prints a warning)
  python scripts/run_backtest_crypto.py --symbol BTC --window 15

  # BTC 15m with train/test split — RECOMMENDED
  # Train on Jan–Mar, test on Apr (never tune on the test window)
  python scripts/run_backtest_crypto.py --symbol BTC --window 15 \\
      --start 2026-01-01 --end 2026-04-24 --test-start 2026-04-01

  # SOL 15m with walk-forward split
  python scripts/run_backtest_crypto.py --symbol SOL --window 15 \\
      --start 2026-01-01 --end 2026-04-24 --test-start 2026-04-01

  # Force fresh download (ignore cache)
  python scripts/run_backtest_crypto.py --symbol BTC --window 15 --no-cache

What it tests
-------------
  Layer 1: 4H HTF bias  (Sabre + price vs MA + MACD above/below zero)
  Layer 2: 15m / 5m LTF MACD confirmation
  Layer 3: Entry timing (candle momentum set to NEUTRAL -- avoids look-ahead)
  Layer 4: Edge estimation vs 0.50 assumed YES price

What it does NOT model
----------------------
  * Early-candle momentum bonuses (would require intra-window 1m look-ahead)
  * Real LLM calls — never used; this script is quant-only (live bot uses AIAgent only when ai.live_inferencing is true)
  * Actual Polymarket YES prices (assumes 0.50; real prices are close to this)
  * Live liquidity filter (applied in live strategy, not here)
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Force UTF-8 output on Windows (avoids UnicodeEncodeError for ->, Y, ..., etc.)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.backtest.ohlcv_loader  import OHLCVLoader
from src.backtest.updown_engine import UpdownBacktestEngine, UpdownBacktestResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_RICH_AVAILABLE = False
try:
    from rich.console import Console
    from rich.table   import Table
    from rich.panel   import Panel
    from rich.text    import Text
    _RICH_AVAILABLE = True
except ImportError:
    pass


def load_config() -> dict:
    p = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(p) as f:
        return yaml.safe_load(f)


def _default_dates() -> tuple:
    """Return (start, end) defaulting to last 90 days."""
    end   = datetime.utcnow()
    start = end - timedelta(days=90)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _print_plain(result: UpdownBacktestResult, data_size: dict,
                 label: str = "") -> None:
    """Plain-text output (no Rich dependency)."""
    sep = "=" * 60
    header = f"CRYPTO UPDOWN BACKTEST -- {result.symbol} {result.window_size}m"
    if label:
        header += f"  [{label}]"
    print(f"\n{sep}")
    print(f"  {header}")
    print(sep)
    print(f"  Period         : {result.start_date}  ->  {result.end_date}")
    print(f"  Initial bankroll : ${result.initial_bankroll:,.2f}")
    print(f"  Final bankroll   : ${result.final_bankroll:,.2f}")
    print(f"  Net PnL          : ${result.net_pnl:+,.2f}  ({result.total_return_pct:+.1f}%)")
    print(sep)
    if result.windows_scanned:
        print(f"  Windows scanned  : {result.windows_scanned:,}")
    print(f"  Trades entered   : {result.windows_entered:,}")
    print(f"  Wins / Losses    : {result.wins} / {result.losses}")
    print(f"  Win rate         : {result.win_rate:.1%}")
    print(f"  Avg edge         : {result.avg_edge:.3f}")
    print(f"  Expectancy       : ${result.expectancy:+.3f} per trade")
    print(f"  Slippage paid    : ${result.slippage_total:,.2f}")
    print(sep)
    if data_size:
        print(f"\n  OHLCV bars used:")
        for iv, n in data_size.items():
            print(f"    {iv:>4}  ->  {n:,} bars")

    if result.trades:
        print(f"\n  Last 10 trades:")
        for t in result.trades[-10:]:
            pnl_str = f"${t.pnl:+.2f}"
            print(
                f"    {str(t.window_open)[:16]}  "
                f"{t.action:<10}  "
                f"HTF={t.htf_bias:<8}  "
                f"LTF={'Y' if t.ltf_confirmed else 'N'}({t.ltf_strength:.2f})  "
                f"edge={t.edge:.3f}  "
                f"${t.size:.0f}  "
                f"{t.outcome}  {pnl_str}"
            )
    print()


def _print_rich(result: UpdownBacktestResult, data_size: dict,
                label: str = "") -> None:
    """Rich-formatted output."""
    console = Console()

    # Summary panel
    colour  = "green" if result.net_pnl >= 0 else "red"
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan", min_width=22)
    summary.add_column()

    def row(lbl, value):
        summary.add_row(lbl, str(value))

    title_suffix = f"  [{label}]" if label else ""
    row("Symbol / Window",    f"{result.symbol}  {result.window_size}m Up/Down")
    row("Period",             f"{result.start_date}  ->  {result.end_date}")
    row("Initial bankroll",   f"${result.initial_bankroll:,.2f}")
    row("Final bankroll",     f"${result.final_bankroll:,.2f}")
    row("Net PnL",            f"[{colour}]${result.net_pnl:+,.2f}  ({result.total_return_pct:+.1f}%)[/]")
    if result.windows_scanned:
        row("Windows scanned", f"{result.windows_scanned:,}")
    row("Trades entered",     f"{result.windows_entered:,}")
    row("Win / Loss",         f"{result.wins} / {result.losses}")
    row("Win rate",           f"{result.win_rate:.1%}")
    row("Avg edge",           f"{result.avg_edge:.3f}")
    row("Expectancy",         f"${result.expectancy:+.3f} per trade")
    row("Slippage paid",      f"${result.slippage_total:,.2f}")

    panel_title = f"[bold]Crypto Updown Backtest -- {result.symbol} {result.window_size}m{title_suffix}"
    console.print(Panel(summary, title=panel_title))

    if data_size:
        data_tbl = Table(title="OHLCV data used", show_header=True)
        data_tbl.add_column("Interval", style="cyan")
        data_tbl.add_column("Bars",     justify="right")
        for iv, n in data_size.items():
            data_tbl.add_row(iv, f"{n:,}")
        console.print(data_tbl)

    # Trades table
    if result.trades:
        tbl = Table(title="Last 20 trades", show_header=True)
        tbl.add_column("Window open",    style="dim",   min_width=16)
        tbl.add_column("Action",                        min_width=10)
        tbl.add_column("HTF",                           min_width=8)
        tbl.add_column("LTF",           justify="center")
        tbl.add_column("Edge",          justify="right")
        tbl.add_column("Size",          justify="right")
        tbl.add_column("Outcome",       justify="center")
        tbl.add_column("PnL",           justify="right")

        for t in result.trades[-20:]:
            outcome_str = "[green]WIN[/]"  if t.outcome == "WIN" else "[red]LOSS[/]"
            pnl_colour  = "green" if t.pnl >= 0 else "red"
            tbl.add_row(
                str(t.window_open)[:16],
                t.action,
                t.htf_bias,
                f"{'Y' if t.ltf_confirmed else 'N'} {t.ltf_strength:.2f}",
                f"{t.edge:.3f}",
                f"${t.size:.0f}",
                outcome_str,
                f"[{pnl_colour}]${t.pnl:+.2f}[/]",
            )
        console.print(tbl)


def _result_to_dict(result: UpdownBacktestResult) -> dict:
    """Serialise one UpdownBacktestResult to a JSON-compatible dict."""
    return {
        "start_date":       result.start_date,
        "end_date":         result.end_date,
        "initial_bankroll": result.initial_bankroll,
        "final_bankroll":   result.final_bankroll,
        "net_pnl":          round(result.net_pnl, 4),
        "total_return_pct": round(result.total_return_pct, 2),
        "windows_scanned":  result.windows_scanned,
        "windows_entered":  result.windows_entered,
        "wins":             result.wins,
        "losses":           result.losses,
        "win_rate":         round(result.win_rate, 4),
        "avg_edge":         round(result.avg_edge, 4),
        "expectancy":       round(result.expectancy, 4),
        "slippage_total":   round(result.slippage_total, 4),
        "trades": [
            {
                "window_open":   str(t.window_open)[:19],
                "window_close":  str(t.window_close)[:19],
                "action":        t.action,
                "htf_bias":      t.htf_bias,
                "ltf_confirmed": t.ltf_confirmed,
                "ltf_strength":  round(t.ltf_strength, 3),
                "edge":          round(t.edge, 4),
                "confidence":    round(t.confidence, 4),
                "size":          t.size,
                "fill_price":    round(t.fill_price, 4),
                "outcome":       t.outcome,
                "pnl":           round(t.pnl, 4),
                "asset_open":    round(t.asset_open, 2),
                "asset_close":   round(t.asset_close, 2),
            }
            for t in result.trades
        ],
    }


def save_report(
    result: UpdownBacktestResult,
    data_size: dict,
    test_result: "UpdownBacktestResult | None" = None,
) -> Path:
    """Save a JSON report.  If test_result is provided the file contains separate
    'train' and 'test' sections; the top-level summary fields reflect the full run.
    """
    report_dir = Path(__file__).resolve().parent.parent / "data" / "backtest" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"backtest_crypto_{result.symbol}_{result.window_size}m_{ts}.json"
    path = report_dir / name

    _strategy_map = {"BTC": "bitcoin", "ETH": "eth_lag", "XRP": "xrp_lag",
                     "HYPE": "hype_lag", "SOL": "sol_lag"}
    _strategy_key = _strategy_map.get(result.symbol, "sol_lag")

    payload: dict = {
        # Dashboard-compatibility fields (updateBacktest() reads these from top level)
        "strategy":         f"{_strategy_key}_{result.window_size}m",
        "trades_count":     result.wins + result.losses,
        "report_type":      "crypto_updown",
        "symbol":           result.symbol,
        "window_minutes":   result.window_size,
        "data_bars":        data_size,
    }

    if test_result is not None:
        # Walk-forward split: surface the TEST metrics at top level for dashboard
        # (the held-out window is the only one that counts for evaluation)
        payload.update({
            "split_mode":       True,
            "test_start_date":  test_result.start_date,
            # Top-level metrics come from the test window
            "start_date":       test_result.start_date,
            "end_date":         test_result.end_date,
            "initial_bankroll": test_result.initial_bankroll,
            "final_bankroll":   test_result.final_bankroll,
            "net_pnl":          round(test_result.net_pnl, 4),
            "total_return_pct": round(test_result.total_return_pct, 2),
            "windows_scanned":  test_result.windows_scanned,
            "windows_entered":  test_result.windows_entered,
            "wins":             test_result.wins,
            "losses":           test_result.losses,
            "win_rate":         round(test_result.win_rate, 4),
            "avg_edge":         round(test_result.avg_edge, 4),
            "expectancy":       round(test_result.expectancy, 4),
            "slippage_total":   round(test_result.slippage_total, 4),
            "trades":           _result_to_dict(test_result)["trades"],
            # Full detail in sub-sections
            "train":            _result_to_dict(_get_train(result, test_result)),
            "test":             _result_to_dict(test_result),
        })
    else:
        payload.update(_result_to_dict(result))

    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


def _get_train(full: UpdownBacktestResult,
               test: UpdownBacktestResult) -> UpdownBacktestResult:
    """Extract the train portion by removing test trades from the full result."""
    import pandas as pd
    test_ts = pd.Timestamp(test.start_date).tz_localize("UTC")
    train_trades = [t for t in full.trades if t.window_open < test_ts]
    wins   = sum(1 for t in train_trades if t.outcome == "WIN")
    losses = sum(1 for t in train_trades if t.outcome == "LOSS")
    pnl    = sum(t.pnl  for t in train_trades)
    slip   = sum(t.slip for t in train_trades)
    return UpdownBacktestResult(
        symbol=full.symbol,
        window_size=full.window_size,
        start_date=full.start_date,
        end_date=test.start_date,
        initial_bankroll=full.initial_bankroll,
        final_bankroll=full.initial_bankroll + pnl,
        trades=train_trades,
        windows_scanned=0,
        windows_entered=len(train_trades),
        wins=wins,
        losses=losses,
        slippage_total=round(slip, 4),
    )


def main() -> int:
    default_start, default_end = _default_dates()

    parser = argparse.ArgumentParser(
        description="Crypto updown backtest (BTC / SOL / ETH, 15m or 5m)"
    )
    parser.add_argument(
        "--symbol", choices=["BTC", "SOL", "ETH", "HYPE", "XRP"], default="BTC",
        help="Asset to backtest (default: BTC)"
    )
    parser.add_argument(
        "--window", type=int, choices=[5, 15], default=15,
        help="Window size in minutes (default: 15)"
    )
    parser.add_argument("--start",      default=default_start,
                        help=f"Start date YYYY-MM-DD (default: {default_start})")
    parser.add_argument("--end",        default=default_end,
                        help=f"End date   YYYY-MM-DD (default: {default_end})")
    parser.add_argument("--test-start", default=None, metavar="DATE",
                        help="Split date YYYY-MM-DD: data before this date is TRAIN "
                             "(tune here), data from this date is TEST (validate only, "
                             "never tune on it).  Omitting this flag runs fully in-sample "
                             "— results cannot be used to validate parameters.")
    parser.add_argument("--bankroll", type=float, default=500.0,
                        help="Initial paper bankroll (default: $500)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Force fresh download of OHLCV data")
    parser.add_argument("--no-ui",    action="store_true",
                        help="Plain text output (no Rich)")
    parser.add_argument("--no-save-report", action="store_true",
                        help="Skip saving JSON report (reports are saved by default)")
    args = parser.parse_args()

    config = load_config()

    # -- 0. Warn loudly when no test split is requested --------------------
    if args.test_start is None:
        _IN_SAMPLE_WARNING = (
            "\n" + "!" * 60 + "\n"
            "  IN-SAMPLE RUN — no --test-start provided.\n"
            "  Results cannot validate parameters: the backtest period\n"
            "  overlaps the data used to tune thresholds.\n"
            "  Use --test-start YYYY-MM-DD to get a held-out test window.\n"
            "  Example: --start 2026-01-01 --test-start 2026-04-01\n"
            + "!" * 60
        )
        print(_IN_SAMPLE_WARNING)

    # -- 1. Download / load OHLCV ------------------------------------------
    print(f"\nLoading {args.symbol} OHLCV data ({args.start} -> {args.end}) ...")
    loader    = OHLCVLoader(no_cache=args.no_cache)
    data      = loader.load_all(args.symbol, args.start, args.end)
    data_size = {iv: len(df) for iv, df in data.items()}

    # Sanity check
    primary_iv = "4h" if args.symbol == "BTC" else "1h"
    if data.get(primary_iv, None) is None or len(data[primary_iv]) < 50:
        logger.error(
            f"Not enough {args.symbol} {primary_iv} data "
            f"({len(data.get(primary_iv, []))} bars). "
            f"Check internet connection or try a wider date range."
        )
        return 1

    total_bars = sum(data_size.values())
    print(f"  Total bars loaded: {total_bars:,}  "
          f"({' | '.join(f'{iv}:{n:,}' for iv, n in data_size.items())})\n")

    # -- 2. Run backtest (single pass over full range) ---------------------
    engine = UpdownBacktestEngine(config=config, initial_bankroll=args.bankroll)
    print(f"Running {args.symbol} {args.window}m backtest ...")
    result = engine.run(
        data=data,
        start_date=args.start,
        end_date=args.end,
        window_minutes=args.window,
        symbol=args.symbol,
    )

    # -- 3. Split and print results ----------------------------------------
    use_rich     = _RICH_AVAILABLE and not args.no_ui
    test_result  = None

    if args.test_start:
        train_result, test_result = result.split(args.test_start)
        print(f"\n  Split: TRAIN {args.start} → {args.test_start}  |  "
              f"TEST {args.test_start} → {args.end}\n")
        if use_rich:
            _print_rich(train_result, {}, label="TRAIN — in-sample, tune here")
            _print_rich(test_result,  {}, label="TEST  — out-of-sample, validate only")
        else:
            _print_plain(train_result, {}, label="TRAIN — in-sample, tune here")
            _print_plain(test_result,  {}, label="TEST  — out-of-sample, validate only")
    else:
        if use_rich:
            _print_rich(result, data_size)
        else:
            _print_plain(result, data_size)

    if result.windows_entered == 0:
        print(
            "No trades entered.  This usually means there was insufficient warmup data "
            "or the strategy never found edges above the configured minimum.  "
            "Try a longer date range (--start further back)."
        )

    # -- 4. Save report (always, unless --no-save-report) ------------------
    if not args.no_save_report:
        rpath = save_report(result, data_size, test_result=test_result)
        print(f"\nReport saved: {rpath}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
