"""Check all backtest results."""
import json, os

reports_dir = "data/backtest/reports"

print("=== SIMPLE BACKTESTS (with bankroll changes) ===")
for f in sorted(os.listdir(reports_dir)):
    if not f.endswith('.json'):
        continue
    path = os.path.join(reports_dir, f)
    with open(path) as fh:
        d = json.load(fh)

    init = d.get('initial_bankroll', 0)
    final = d.get('final_bankroll', 0)
    period = d.get('period', '')
    strat = d.get('strategy', '')
    trades = d.get('total_trades', 0)

    if init and final and abs(final - init) > 0.01:
        pnl = final - init
        ret = pnl / init * 100
        print(f"  {strat:12s} | {period:30s} | ${init:>8,.0f} -> ${final:>12,.2f} | {ret:>+8.1f}% | trades={trades} | {f}")

print()
print("=== RIGOROUS BACKTESTS (per-strategy metrics) ===")
for f in sorted(os.listdir(reports_dir)):
    if not f.startswith('backtest_rigorous') or not f.endswith('.json'):
        continue
    path = os.path.join(reports_dir, f)
    with open(path) as fh:
        d = json.load(fh)

    per_strat = d.get('per_strategy_metrics', {})
    if per_strat:
        print(f"\n  {f}:")
        for strat, m in per_strat.items():
            sharpe = m.get('sharpe_annual', 0)
            dd = m.get('max_drawdown_pct', 0)
            pnl = m.get('total_pnl', 0)
            trades = m.get('total_trades', 0)
            wr = m.get('win_rate_pct', 0)
            print(f"    {strat:12s} | Sharpe={sharpe:>+7.3f} | MaxDD={dd:>+7.2f}% | PnL=${pnl:>+8.2f} | trades={trades} | WR={wr:.0f}%")
