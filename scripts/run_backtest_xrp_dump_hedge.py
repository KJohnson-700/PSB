#!/usr/bin/env python3
"""
Simulated backtest for XRP dump-and-hedge (no Polymarket history required).

Uses Binance XRPUSDT 1m candles to drive a crude synthetic YES/NO mid path,
then replays XRPDumpHedgeStrategy.scan_and_analyze on rolling snapshots.

Does not call LLMs. For live testing, enable strategies.xrp_dump_hedge in
config/settings.yaml when Gamma exposes xrp-updown-15m-* slugs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.analysis.math_utils import PositionSizer
from src.backtest.ohlcv_loader import OHLCVLoader
from src.market.scanner import Market
from src.strategies.xrp_dump_hedge import XRPDumpHedgeStrategy


def _load_config() -> dict:
    p = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(p) as f:
        return yaml.safe_load(f)


def _synthetic_yes_no(close: float, close_prev: float, base: float) -> tuple:
    """Map spot move to synthetic mids (crude stress toy, not a price oracle)."""
    if close_prev <= 0:
        return base, 1.0 - base
    r = (close - close_prev) / close_prev
    yes = max(0.05, min(0.95, base - r * 3.0))
    skew = 0.02
    no = max(0.05, min(0.95, 1.0 - yes + skew))
    return yes, no


def _make_market(yes: float, no: float, minute_i: int) -> Market:
    _ = minute_i
    q = "XRP Up or Down 2:15AM–2:30AM ET"
    end = datetime.now(timezone.utc) + timedelta(minutes=10)
    return Market(
        id="sim-xrp-updown",
        question=q,
        description="synthetic",
        volume=1e6,
        liquidity=50_000,
        yes_price=yes,
        no_price=no,
        spread=0.02,
        end_date=end,
        token_id_yes="sim_yes",
        token_id_no="sim_no",
        group_item_title="",
    )


def _save_xrp_sim_report(
    *,
    leg1: int,
    leg2: int,
    bars: int,
    step: int,
    start: str,
    end: str,
    bankroll: float,
) -> Path:
    report_dir = Path(__file__).resolve().parent.parent / "data" / "backtest" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    name = f"backtest_xrp_dump_hedge_sim_{ts}.json"
    path = report_dir / name
    payload = {
        "strategy": "xrp_dump_hedge",
        "report_type": "xrp_dump_hedge_sim",
        "symbol": "XRP",
        "window_minutes": 15,
        "start_date": start,
        "end_date": end,
        "initial_bankroll": bankroll,
        "leg1_signals": leg1,
        "leg2_signals": leg2,
        "bars_scanned": bars,
        "step_minutes": step,
        "run_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  Saved report: {path}")
    return path


async def run(args: argparse.Namespace) -> None:
    cfg = _load_config()
    loader = OHLCVLoader(no_cache=args.no_cache)

    # Kraken OHLCV lookback: 1m ~12h, 5m ~2.5d, 15m ~7.5d, 1h ~30d.
    # Use 1h by default for maximum lookback (30 days); fall back to lower resolution.
    # Override with args.resolution if provided. The dump-and-hedge logic works at any
    # granularity — coarser bars capture larger per-bar moves → more dump events.
    df = resolution = None
    for iv, label in [("1h", "1h"), ("15m", "15m"), ("5m", "5m"), ("1m", "1m")]:
        candidate = loader.load("XRPUSDT", iv, args.start, args.end)
        if not candidate.empty and len(candidate) >= 100:
            df = candidate
            resolution = label
            break
    if df is None or df.empty:
        print("No XRP data available for the requested date range — check network.")
        return
    print(f"Loaded {len(df)} XRP {resolution} bars ({args.start} -> {args.end})")

    pos = PositionSizer(
        kelly_fraction=cfg.get("trading", {}).get("kelly_fraction", 0.25),
        max_position_pct=cfg.get("trading", {}).get("max_exposure_per_trade", 0.05),
        min_position=cfg.get("trading", {}).get("default_position_size", 10),
        max_position=cfg.get("trading", {}).get("max_position_size", 15),
    )
    strat = XRPDumpHedgeStrategy(cfg, pos)
    strat.enabled = True
    strat._btc_z_override = -3.0
    strat.use_btc_z_gate = True

    leg1 = leg2 = 0
    base = 0.52
    prev = float(df["close"].iloc[0])
    step = max(1, int(args.step_minutes))
    for i in range(1, len(df), step):
        row = df.iloc[i]
        c = float(row["close"])
        yes, no = _synthetic_yes_no(c, prev, base)
        prev = c
        m = _make_market(yes, no, i)
        sigs = await strat.scan_and_analyze([m], float(args.bankroll))
        for s in sigs:
            if s.leg == "1":
                leg1 += 1
            elif s.leg == "2":
                leg2 += 1

    print("XRP dump-hedge simulation (synthetic book)")
    print(f"  Bars: {len(df)}  step: {step}m")
    print(f"  Leg1 (dump BUY_YES) signals: {leg1}")
    print(f"  Leg2 (hedge BUY_NO) signals: {leg2}")
    if not args.no_save_report:
        _save_xrp_sim_report(
            leg1=leg1,
            leg2=leg2,
            bars=len(df),
            step=step,
            start=args.start,
            end=args.end,
            bankroll=float(args.bankroll),
        )


def main():
    from datetime import datetime, timedelta, timezone as _tz
    _today = datetime.now(_tz.utc)
    _default_end = _today.strftime("%Y-%m-%d")
    _default_start = (_today - timedelta(days=30)).strftime("%Y-%m-%d")

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=_default_start)
    ap.add_argument("--end", default=_default_end)
    ap.add_argument("--bankroll", type=float, default=10_000)
    ap.add_argument("--step-minutes", type=int, default=1)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-save-report", action="store_true", help="Skip writing JSON under data/backtest/reports/")
    ap.add_argument("--window", type=int, default=15, help="unused; reserved for CLI parity")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
