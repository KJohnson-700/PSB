# Backtest results (40% run — 60 slugs per strategy)

**Report:** `backtest_rigorous_20260314_011424.json`  
**Run:** 2024-03-14 01:14:24 | 4 periods (Q1–Q4), train/test, 4 stress scenarios

---

## Strategy: Fade

| Period   | Markets | Trades | Net PnL   | Return % |
|----------|---------|--------|-----------|----------|
| 2024 Q1  | 39      | 583    | -$40.75   | -2.04%   |
| 2024 Q2  | 93      | 1,312  | -$7.22    | -0.36%   |
| 2024 Q3  | 101     | 1,823  | -$40.19   | -2.01%   |
| 2024 Q4  | 65      | 1,495  | -$61.83   | -3.09%   |

**Baseline total (all periods):** **-$149.99**  
**Train (Q1+Q2) PnL:** -$47.97  
**Test (Q3+Q4) PnL:** -$102.02  

**Risk (baseline):**
- **Sharpe (annual):** -30.47  
- **Max drawdown:** -7.31%  
- **Per-period returns:** -2.04%, -0.36%, -2.01%, -3.09%

**Stress:** Losses worsen under high_slippage, very_high_slippage, and with_fees (e.g. Q4 baseline -$61.83 vs with_fees -$63.07).

---

## Strategy: Arbitrage

| Period   | Markets | Trades | Net PnL | Return % |
|----------|---------|--------|---------|----------|
| 2024 Q1  | 1       | 0      | $0      | 0%       |
| 2024 Q2  | 1       | 0      | $0      | 0%       |
| 2024 Q3  | 1       | 0      | $0      | 0%       |
| 2024 Q4  | 1       | 25     | -$6.06  | -0.30%   |

**Baseline total:** **-$6.06** (all from Q4; only one market had enough data)  
**Train PnL:** $0  
**Test PnL:** -$6.06  

**Risk (baseline):**
- **Sharpe (annual):** -9.17  
- **Max drawdown:** -0.30%  
- **Per-period returns:** 0%, 0%, 0%, -0.30%

**Note:** With the 2024 slug list, only one arbitrage market had ≥50 bars in every period (2024 presidential). So arbitrage is barely tested in this run.

---

## Summary

| Metric        | Fade      | Arbitrage   |
|---------------|-----------|-------------|
| Total PnL     | **-$150** | **-$6**     |
| Sharpe        | -30.47    | -9.17       |
| Max drawdown  | -7.31%    | -0.30%      |
| Markets used  | 39–101/period | 1/period |

**Recommendation**
- **Fade:** **Refine** — No edge in this backtest; losses in every period and very negative Sharpe. Revisit thresholds, sizing, or universe before considering deploy.
- **Arbitrage:** **Inconclusive** — Only one market with data; rebuild or expand 2024 slug set and re-run before judging.
