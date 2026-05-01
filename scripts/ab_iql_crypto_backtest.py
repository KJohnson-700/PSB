#!/usr/bin/env python3
"""One-shot A/B: baseline vs current settings for IQL + 5m SELL gates.

Baseline (harness-only): turns IQL off and relaxes XRP/HYPE sell_corr so the
diff isolates those gates. Current = config/settings.yaml as loaded.

Uses cached OHLCV when present. For oracle/history parity use the same flags as
run_backtest_crypto; omit --skip-oracle if you need Chainlink replay (requires
web3 + deps from requirements.txt).

Example:

  python scripts/ab_iql_crypto_backtest.py --start 2026-01-20 --end 2026-04-29 --skip-oracle
  python scripts/ab_iql_crypto_backtest.py ... --skip-oracle --symbols SOL
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yaml

from typing import Any, Dict, Optional

from src.backtest.ohlcv_loader import OHLCVLoader
from src.backtest.oracle_loader import OracleHistoryLoader
from src.backtest.updown_engine import UpdownBacktestEngine


STRAT_KEYS = {"SOL": "sol_macro", "ETH": "eth_macro", "XRP": "xrp_macro", "HYPE": "hype_macro"}
DEFAULT_SYMBOLS = ["SOL", "ETH", "XRP", "HYPE"]


def baseline_cfg(full: dict) -> dict:
    cfg = copy.deepcopy(full)
    for key in ("sol_macro", "eth_macro", "xrp_macro", "hype_macro"):
        blk = cfg.get("strategies", {}).setdefault(key, {})
        blk["iql_15m_enabled"] = False
    for key in ("xrp_macro", "hype_macro"):
        blk = cfg["strategies"].setdefault(key, {})
        blk["sell_5m_min_corr"] = -1.0
        blk.pop("min_positive_m5_adj_5m_sell", None)
    return cfg


def _btc_for(sym: str, cfg: dict, btc_full: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return BTC OHLC bundle or None matching run_backtest_crypto policy."""
    if btc_full is None:
        return None
    if sym == "ETH":
        return btc_full
    sk = STRAT_KEYS.get(sym)
    if not sk:
        return None
    if float(cfg.get("strategies", {}).get(sk, {}).get("sell_5m_min_corr", -1.0)) >= 0:
        return btc_full
    return None


def preload_symbol_bundle(
    sym: str,
    *,
    bef: dict,
    cur: dict,
    loader: OHLCVLoader,
    start: str,
    end: str,
    skip_oracle: bool,
):
    alt = loader.load_all(sym, start, end)

    need_btc = sym == "ETH"
    if sym in STRAT_KEYS:
        sk = STRAT_KEYS[sym]
        for cfg in (bef, cur):
            if float(cfg.get("strategies", {}).get(sk, {}).get("sell_5m_min_corr", -1.0)) >= 0:
                need_btc = True
                break

    btc_full = loader.load_all("BTC", start, end) if need_btc else None

    oracle_hist = None
    if not skip_oracle and sym in {"ETH", "SOL", "XRP", "HYPE"}:
        oracle_hist = OracleHistoryLoader().load_history(sym, start, end)

    return alt, btc_full, oracle_hist


def run_preloaded(
    sym: str,
    window: int,
    cfg: dict,
    *,
    alt: dict,
    btc_full: Optional[Dict[str, Any]],
    oracle_hist,
    bankroll: float,
    start: str,
    end: str,
):
    eng = UpdownBacktestEngine(config=cfg, initial_bankroll=bankroll)
    return eng.run(
        data=alt,
        start_date=start,
        end_date=end,
        window_minutes=window,
        symbol=sym,
        btc_data=_btc_for(sym, cfg, btc_full),
        oracle_history=oracle_hist,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Baseline vs current IQL / sell 5m backtest A/B.")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--bankroll", type=float, default=500.0)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help=f"Comma list (default: {','.join(DEFAULT_SYMBOLS)})",
    )
    p.add_argument(
        "--skip-oracle",
        action="store_true",
        help="Do not load oracle history (faster; disables basis filter).",
    )
    args = p.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with settings_path.open() as f:
        current = yaml.safe_load(f)

    bef = baseline_cfg(current)
    current_deep = copy.deepcopy(current)

    ora_note = "off" if args.skip_oracle else "on"
    print(
        f"A/B crypto updown ({args.start} → {args.end})  "
        f"cache={'off' if args.no_cache else 'on'}  oracle={ora_note}\n"
    )
    hdr = (
        f"{'sym':<5} {'win':>3} {'label':<36} {'trades':>6} {'WR%':>7} "
        f"{'PnL$':>10} {'E/tr$':>8} {'ora_sk':>6}"
    )
    print(hdr)
    print("-" * len(hdr))

    loader = OHLCVLoader(no_cache=args.no_cache)

    for sym in symbols:
        alt, btc_full, ora = preload_symbol_bundle(
            sym, bef=bef, cur=current_deep, loader=loader,
            start=args.start, end=args.end, skip_oracle=args.skip_oracle,
        )
        for window in (15, 5):
            for label, cfg in (
                ("baseline (no IQL, no sell corr)", bef),
                ("current settings.yaml", current_deep),
            ):
                r = run_preloaded(
                    sym,
                    window,
                    copy.deepcopy(cfg),
                    alt=alt,
                    btc_full=btc_full,
                    oracle_hist=ora,
                    bankroll=args.bankroll,
                    start=args.start,
                    end=args.end,
                )
                wr_pct = r.win_rate * 100
                print(
                    f"{sym:<5} {window:>3} {label:<36} {r.windows_entered:>6}"
                    f" {wr_pct:>7.1f} {r.net_pnl:>10.2f} {r.expectancy:>8.4f}"
                    f" {r.oracle_basis_skips:>6}"
                )
        print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
