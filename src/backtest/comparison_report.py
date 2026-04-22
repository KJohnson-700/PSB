"""
Backtest vs Paper/Live Trade Comparison Report.

Compares backtest results against live/paper trade journal data to identify
gaps between simulation and reality. Key metrics:
- Win rate: backtest vs live
- Avg edge: backtest predicted vs live realized
- Slippage: modeled vs actual
- PnL: backtest vs live per strategy
- Trade count: how many signals were generated vs how many were actually taken

Usage:
    from src.backtest.comparison_report import generate_comparison_report
    report = generate_comparison_report(
        backtest_dir="data/backtest/reports",
        journal_path="data/journal/trade_journal.jsonl"
    )
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


@dataclass
class StrategyComparison:
    """Side-by-side comparison for one strategy."""

    strategy: str
    # Backtest metrics
    bt_win_rate: float = 0.0
    bt_avg_edge: float = 0.0
    bt_avg_slippage_bps: float = 0.0
    bt_net_pnl: float = 0.0
    bt_trades: int = 0
    bt_return_pct: float = 0.0
    # Live/paper metrics
    live_win_rate: float = 0.0
    live_avg_edge: float = 0.0
    live_avg_slippage_bps: float = 0.0
    live_net_pnl: float = 0.0
    live_trades: int = 0
    live_return_pct: float = 0.0
    # Gaps
    win_rate_gap: float = 0.0
    edge_gap: float = 0.0
    slippage_gap: float = 0.0
    pnl_gap: float = 0.0


@dataclass
class ComparisonReport:
    """Full comparison report."""

    generated_at: str
    strategies: List[StrategyComparison] = field(default_factory=list)
    summary: str = ""


def _load_backtest_results(backtest_dir: Path) -> Dict[str, Dict]:
    """Load latest backtest result per strategy from report JSONs."""
    results: Dict[str, Dict] = {}
    if not backtest_dir.exists():
        return results

    for f in sorted(
        backtest_dir.glob("backtest_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            with open(f) as fp:
                data = json.load(fp)
            strategy = data.get("strategy", "unknown")
            if strategy not in results:
                results[strategy] = data
        except Exception:
            pass
    return results


def _load_journal_trades(journal_path: Path) -> List[Dict]:
    """Load trades from trade journal JSONL."""
    trades = []
    if not journal_path.exists():
        return trades
    try:
        with open(journal_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "trade":
                        trades.append(entry)
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        logger.warning(f"Failed to load journal: {e}")
    return trades


def _compute_live_metrics(trades: List[Dict], strategy: str) -> Dict[str, float]:
    """Compute live metrics for a specific strategy."""
    strat_trades = [
        t for t in trades if t.get("strategy", "").lower() == strategy.lower()
    ]
    if not strat_trades:
        return {
            "win_rate": 0,
            "avg_edge": 0,
            "avg_slippage_bps": 0,
            "net_pnl": 0,
            "trades": 0,
            "return_pct": 0,
        }

    wins = 0
    total_edge = 0
    total_slippage = 0
    total_pnl = 0
    n = len(strat_trades)

    for t in strat_trades:
        pnl = t.get("pnl", 0)
        if pnl > 0:
            wins += 1
        total_pnl += pnl
        total_edge += t.get("edge", 0)
        total_slippage += t.get("slippage_bps", 0)

    return {
        "win_rate": wins / n if n else 0,
        "avg_edge": total_edge / n if n else 0,
        "avg_slippage_bps": total_slippage / n if n else 0,
        "net_pnl": total_pnl,
        "trades": n,
        "return_pct": 0,  # Can't compute without bankroll context from journal
    }


def generate_comparison_report(
    backtest_dir: str = "data/backtest/reports",
    journal_path: str = "data/journal/trade_journal.jsonl",
) -> ComparisonReport:
    """Generate a backtest-vs-live comparison report.

    Args:
        backtest_dir: Directory containing backtest report JSONs
        journal_path: Path to trade journal JSONL file

    Returns:
        ComparisonReport with per-strategy comparisons
    """
    bt_results = _load_backtest_results(Path(backtest_dir))
    live_trades = _load_journal_trades(Path(journal_path))

    report = ComparisonReport(generated_at=datetime.now().isoformat())

    for strategy, bt_data in bt_results.items():
        # Extract backtest metrics
        bt_trades_list = bt_data.get("trades", [])
        bt_wins = sum(1 for t in bt_trades_list if t.get("pnl", 0) > 0)
        bt_n = len(bt_trades_list)
        bt_avg_edge = (
            sum(t.get("edge", 0) for t in bt_trades_list) / bt_n if bt_n else 0
        )
        bt_avg_slip = (
            sum(t.get("slippage_cost", 0) for t in bt_trades_list) / bt_n if bt_n else 0
        )

        # Extract live metrics
        live_metrics = _compute_live_metrics(live_trades, strategy)

        sc = StrategyComparison(
            strategy=strategy,
            bt_win_rate=bt_wins / bt_n if bt_n else 0,
            bt_avg_edge=bt_avg_edge,
            bt_avg_slippage_bps=bt_avg_slip * 10000,  # convert to bps
            bt_net_pnl=bt_data.get("gross_pnl", 0),
            bt_trades=bt_n,
            bt_return_pct=bt_data.get("total_return_pct", 0),
            live_win_rate=live_metrics["win_rate"],
            live_avg_edge=live_metrics["avg_edge"],
            live_avg_slippage_bps=live_metrics["avg_slippage_bps"],
            live_net_pnl=live_metrics["net_pnl"],
            live_trades=live_metrics["trades"],
            live_return_pct=live_metrics["return_pct"],
        )

        # Compute gaps
        sc.win_rate_gap = sc.live_win_rate - sc.bt_win_rate
        sc.edge_gap = sc.live_avg_edge - sc.bt_avg_edge
        sc.slippage_gap = sc.live_avg_slippage_bps - sc.bt_avg_slippage_bps
        sc.pnl_gap = sc.live_net_pnl - sc.bt_net_pnl

        report.strategies.append(sc)

    # Build summary
    lines = []
    for sc in report.strategies:
        status = "OK" if sc.win_rate_gap > -0.1 else "DIVERGING"
        lines.append(
            f"[{status}] {sc.strategy}: "
            f"BT win={sc.bt_win_rate:.0%} Live={sc.live_win_rate:.0%} (gap={sc.win_rate_gap:+.0%}) | "
            f"BT PnL=${sc.bt_net_pnl:+.0f} Live=${sc.live_net_pnl:+.0f} (gap=${sc.pnl_gap:+.0f})"
        )
    report.summary = "\n".join(lines) if lines else "No data to compare."

    logger.info(f"Comparison report generated: {len(report.strategies)} strategies")
    return report


def print_comparison_report(report: ComparisonReport):
    """Print a formatted comparison report to console."""
    print("\n" + "=" * 80)
    print("BACKTEST vs LIVE COMPARISON REPORT")
    print(f"Generated: {report.generated_at}")
    print("=" * 80)

    for sc in report.strategies:
        status = "OK" if sc.win_rate_gap > -0.1 else "WARNING"
        print(f"\n--- {sc.strategy.upper()} [{status}] ---")
        print(
            f"  Win Rate:    BT={sc.bt_win_rate:.0%}  Live={sc.live_win_rate:.0%}  Gap={sc.win_rate_gap:+.0%}"
        )
        print(
            f"  Avg Edge:    BT={sc.bt_avg_edge:.4f}  Live={sc.live_avg_edge:.4f}  Gap={sc.edge_gap:+.4f}"
        )
        print(
            f"  Slippage:    BT={sc.bt_avg_slippage_bps:.0f}bps  Live={sc.live_avg_slippage_bps:.0f}bps  Gap={sc.slippage_gap:+.0f}bps"
        )
        print(
            f"  Net PnL:     BT=${sc.bt_net_pnl:+.2f}  Live=${sc.live_net_pnl:+.2f}  Gap=${sc.pnl_gap:+.2f}"
        )
        print(f"  Trade Count: BT={sc.bt_trades}  Live={sc.live_trades}")

    if not report.strategies:
        print("\nNo data available for comparison.")
        print("Ensure backtest reports exist in data/backtest/reports/")
        print("and trade journal exists in data/journal/trade_journal.jsonl")

    print("=" * 80)
