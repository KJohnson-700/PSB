#!/usr/bin/env python3
"""
Run ``run_backtest_crypto.py`` for multiple symbols with **identical** date/window
arguments so each job processes the same calendar span (comparable bar counts).

Use ``--parallel`` to overlap runs in separate processes so **wall-clock** time is
closer to the slowest single symbol instead of the sum of all (at the cost of CPU
and memory). Sequential mode is the default (predictable load).

Examples
--------
  .venv/bin/python scripts/run_crypto_backtest_bundle.py \\
      --start 2026-01-20 --end 2026-04-20 --window 15 --test-start 2026-04-01

  # Faster wall-clock (5 processes); ensure enough RAM / RPC headroom for oracle.
  .venv/bin/python scripts/run_crypto_backtest_bundle.py \\
      --start 2026-01-20 --end 2026-04-20 --window 15 --parallel 5
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _python() -> str:
    root = _repo_root()
    v = root / ".venv" / "bin" / "python"
    if v.is_file():
        return str(v)
    return sys.executable


def _build_cmd(symbol: str, cfg: dict) -> list[str]:
    script = _repo_root() / "scripts" / "run_backtest_crypto.py"
    cmd = [
        _python(),
        str(script),
        "--symbol",
        symbol,
        "--window",
        str(cfg["window"]),
        "--start",
        cfg["start"],
        "--end",
        cfg["end"],
    ]
    if cfg.get("test_start"):
        cmd += ["--test-start", cfg["test_start"]]
    if cfg.get("no_cache"):
        cmd.append("--no-cache")
    if cfg.get("skip_oracle"):
        cmd.append("--skip-oracle")
    if cfg.get("oracle_fetch"):
        cmd.append("--oracle-fetch")
    if cfg.get("no_save_report"):
        cmd.append("--no-save-report")
    if cfg.get("no_ui"):
        cmd.append("--no-ui")
    if cfg.get("polymarket_marks"):
        cmd.append("--polymarket-marks")
    if cfg.get("progress_interval") is not None:
        cmd += ["--progress-interval", str(cfg["progress_interval"])]
    if cfg.get("max_seconds"):
        cmd += ["--max-seconds", str(cfg["max_seconds"])]
    return cmd


def _run_subprocess(symbol: str, cfg: dict) -> tuple[str, int, float]:
    cmd = _build_cmd(symbol, cfg)
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(_repo_root()))
    return symbol, proc.returncode, time.perf_counter() - t0


def main() -> int:
    default_start = "2026-01-20"
    default_end = "2026-04-20"
    p = argparse.ArgumentParser(
        description="Run crypto up/down backtests for several symbols with the same window."
    )
    p.add_argument(
        "--symbols",
        default="BTC,SOL,ETH,XRP,HYPE",
        help="Comma-separated symbols (default: BTC,SOL,ETH,XRP,HYPE)",
    )
    p.add_argument("--start", default=default_start)
    p.add_argument("--end", default=default_end)
    p.add_argument("--window", type=int, choices=[5, 15, 30], default=15)
    p.add_argument("--test-start", default=None, metavar="DATE")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--skip-oracle", action="store_true")
    p.add_argument(
        "--oracle-fetch",
        action="store_true",
        help="Allow slow Chainlink RPC backfill in each child (default children use oracle cache only).",
    )
    p.add_argument("--no-save-report", action="store_true")
    p.add_argument("--no-ui", action="store_true")
    p.add_argument("--progress-interval", type=int, default=1000)
    p.add_argument(
        "--max-seconds",
        type=float,
        default=0.0,
        help="Forward a per-symbol replay time cap; partial reports still save.",
    )
    p.add_argument(
        "--parallel",
        type=int,
        default=1,
        metavar="N",
        help="Run up to N symbols at once (default: 1 = sequential)",
    )
    p.add_argument(
        "--polymarket-marks",
        action="store_true",
        help="Forward --polymarket-marks to each run_backtest_crypto subprocess.",
    )
    ns = p.parse_args()
    symbols = [s.strip().upper() for s in ns.symbols.split(",") if s.strip()]
    if not symbols:
        print("No symbols after parsing --symbols", file=sys.stderr)
        return 1

    cfg = {
        "window": ns.window,
        "start": ns.start,
        "end": ns.end,
        "test_start": ns.test_start,
        "no_cache": ns.no_cache,
        "skip_oracle": ns.skip_oracle,
        "oracle_fetch": ns.oracle_fetch,
        "no_save_report": ns.no_save_report,
        "no_ui": ns.no_ui,
        "polymarket_marks": ns.polymarket_marks,
        "progress_interval": ns.progress_interval,
        "max_seconds": ns.max_seconds,
    }

    wall0 = time.perf_counter()
    results: list[tuple[str, int, float]] = []

    if ns.parallel <= 1:
        for sym in symbols:
            results.append(_run_subprocess(sym, cfg))
    else:
        workers = min(ns.parallel, len(symbols))
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_run_subprocess, sym, cfg): sym for sym in symbols}
            for fut in as_completed(futures):
                results.append(fut.result())

    results.sort(key=lambda x: symbols.index(x[0]) if x[0] in symbols else 99)
    wall = time.perf_counter() - wall0

    print("\n--- bundle summary ---")
    for sym, code, elapsed in results:
        status = "ok" if code == 0 else f"exit {code}"
        print(f"  {sym:4}  {elapsed:7.1f}s  {status}")
    print(f"  wall-clock (bundle): {wall:.1f}s")
    print("----------------------\n")

    return 0 if all(c == 0 for _, c, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
