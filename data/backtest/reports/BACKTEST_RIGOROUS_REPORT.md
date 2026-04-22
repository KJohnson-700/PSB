# Rigorous Backtest Report — Fade & Arbitrage

**Report generated:** 2026-03-14  
**Source:** `data/backtest/reports/backtest_rigorous_20260313_231736.json`  
**Config:** `config/backtest_rigorous.yaml` (4 periods 2024 Q1–Q4, train/test, baseline stress in this run)

---

## 1. Strategy Tested | Markets Used | Backtest Period

| Strategy   | Markets (with data) | Backtest Period | Train Periods | Test Periods |
|-----------|----------------------|------------------|---------------|-------------|
| **Fade**      | 39 (Q1), 93 (Q2), 101 (Q3), 65 (Q4) | 2024-01-01 → 2024-11-30 | 2024 Q1, Q2 | 2024 Q3, Q4 |
| **Arbitrage** | 1 (Q1–Q4)            | 2024-01-01 → 2024-11-30 | 2024 Q1, Q2 | 2024 Q3, Q4 |

**Note:** Arbitrage uses the same 150 slugs from `config/backtest_markets.yaml` as fade, but after loading CLOB data in each period’s date range only **one** market has ≥50 bars: `which-party-will-win-the-2024-united-states-presidential-election`. All other arbitrage slugs are from older resolved events (e.g. 2020–2022) and have no or insufficient 2024 price history.

---

## 2. Performance Metrics

### Fade (baseline)

| Metric        | Value    |
|---------------|----------|
| **Total PnL** | **-$150.00** (sum across 4 periods) |
| **ROI**       | ~ -1.9% per period (avg of 4 period returns) |
| **Win rate**  | N/A (aggregate; many markets lose small amounts) |
| **Sharpe (annual)** | **-30.47** |
| **Max drawdown**    | **-7.31%** |

**Per-period (baseline):**

| Period   | Markets | Net PnL    | Return % | Trades |
|----------|---------|------------|----------|--------|
| 2024 Q1  | 39      | -$40.75    | -2.04%   | 583    |
| 2024 Q2  | 93      | -$7.22     | -0.36%   | 1,312  |
| 2024 Q3  | 101     | -$40.19    | -2.01%   | 1,823  |
| 2024 Q4  | 65      | -$61.83    | -3.09%   | 1,495  |

**Train vs test (baseline):**

- **Train PnL (Q1+Q2):** -$47.97  
- **Test PnL (Q3+Q4):** -$102.02  
- Test is worse than train; no out-of-sample edge.

### Arbitrage (baseline)

| Metric        | Value    |
|---------------|----------|
| **Total PnL** | **-$6.06** (almost entirely from Q4) |
| **ROI**       | 0% (Q1–Q3), -0.30% (Q4) |
| **Sharpe (annual)** | **-9.17** |
| **Max drawdown**    | **-0.30%** |

**Per-period (baseline):**

| Period   | Markets | Net PnL   | Return % | Trades |
|----------|---------|-----------|----------|--------|
| 2024 Q1  | 1       | $0.00     | 0%       | 0      |
| 2024 Q2  | 1       | $0.00     | 0%       | 0      |
| 2024 Q3  | 1       | $0.00     | 0%       | 0      |
| 2024 Q4  | 1       | -$6.06    | -0.30%   | 25     |

**Train vs test:** Train $0, Test -$6.06. Not interpretable (single market, almost no trades in train).

---

## 3. Execution Metrics

### Fade

- **Avg slippage / execution cost:** Reported as `execution_cost_total` per period (e.g. Q1 ~$0.40, Q4 ~$12.71); total ~$22.48 across 4 periods.
- **Fill rate:** All requested trades executed (blocked_count 0).
- **Avg trade duration:** Not computed in this report.

### Arbitrage

- **Execution cost total:** ~$0.06 (Q4 only).
- **Fill rate:** N/A (only 25 trades in one period).
- **Avg trade duration:** N/A.

---

## 4. Stress-Scenario Impact

The run used for this report contained **only the baseline** scenario (`stress_scenarios: ["baseline"]`).  

**Not in this run:** `high_slippage`, `very_high_slippage`, `with_fees`.

To get stress impact:

- Run:  
  `python scripts/run_backtest_rigorous.py --save-report`  
  (with full stress in `config/backtest_rigorous.yaml`)  
- Or a quicker run with fewer slugs, e.g. temporarily set `max_slugs_per_strategy_per_period: 25` in `config/backtest_rigorous.yaml`, then run the same command.

**Expected:** Under higher slippage and with_fees, fade’s negative PnL would worsen; arbitrage cannot be meaningfully evaluated until it has more markets with data.

---

## 5. Risk Analysis

- **Capital at risk:** Bankroll $2,000 per run; allocated evenly across markets with data.
- **Market concentration:** Fade is diversified (39–101 markets per period). Arbitrage is effectively a single-market backtest.
- **Sensitivity:** Fade is sensitive to execution (high cost in Q4); stress tests would show sensitivity to slippage and fees. Arbitrage sensitivity is unknown due to lack of data.

---

## 6. Key Insights

**Fade**

- **Strengths:** Many markets with data; execution path is realistic (no blocked trades).
- **Weaknesses:** Negative PnL in every period; train and test both lose; very negative Sharpe; drawdown ~7.3%. Suggests no statistical edge in this setup.
- **Failure modes:** Strategy loses under baseline; would likely worsen with high_slippage and with_fees.

**Arbitrage**

- **Strengths:** Single market that traded (Q4) shows small loss, so engine runs.
- **Weaknesses:** Only one market has CLOB history in 2024; 0 trades in Q1–Q3. Backtest is not representative of a multi-market arbitrage strategy.
- **Failure modes:** Current market list is dominated by events that resolved before or outside 2024; date filter leaves almost no arbitrage universe.

---

## 7. Recommendation

| Strategy   | Recommendation | Reason |
|-----------|-----------------|--------|
| **Fade**      | **Refine**      | No edge in backtest; consistent losses and poor Sharpe. Revisit entry/exit rules, sizing, or universe before any deploy. |
| **Arbitrage** | **Reject (test setup)** | Backtest is invalid: only one market with data. Fix data/universe (see below) and re-run before judging the strategy. |

---

## 8. Issues Found

1. **Arbitrage: effectively zero markets in 2024**  
   - **Symptom:** Only 1 market with ≥50 bars in each period; 0 trades in Q1–Q3.  
   - **Cause:** `config/backtest_markets.yaml` arbitrage slugs are mostly from Gamma “closed events” (2020–2022). CLOB/price API returns no or insufficient data for those slugs in 2024 windows.

2. **Stress scenarios not in saved run**  
   - **Symptom:** JSON report has `"stress_scenarios": ["baseline"]`.  
   - **Cause:** The run that produced the report was either with `--no-stress` or an older config that did not include all stress scenarios.

3. **Full run runtime**  
   - **Symptom:** Full run (150 slugs × 4 periods × 2 strategies × 4 stress) can take a long time and was backgrounded.  
   - **Cause:** Many API calls and backtest steps per slug.

---

## 9. Suggested Fixes for Better Test Data

- **Arbitrage universe for 2024**
  - **Option A:** Rebuild arbitrage slugs so they are **active or resolved in 2024**. Run:
    - `python scripts/build_backtest_market_list.py ...` with parameters that restrict to events with end_date in 2024 or that have CLOB history in 2024 (if the script supports it). Then point `config/backtest_rigorous.yaml` at the updated `config/backtest_markets.yaml`.
  - **Option B:** In `config/backtest_rigorous.yaml`, add or use a **separate date range** for arbitrage (e.g. 2022–2023) where the current slug list has more CLOB coverage, and run arbitrage-only backtests over that range. Requires either a second plan or strategy-specific period config.
  - **Option C:** Add a **validation step** before the full backtest: for a sample of slugs (e.g. 20) per strategy, call `loader.load_market_data(slug, period_start, period_end, "1h")` and require at least N markets with ≥ min_bars. If arbitrage fails, skip or warn and suggest refreshing the market list or adjusting dates.

- **Stress scenarios**
  - Ensure `config/backtest_rigorous.yaml` has all desired stress_scenarios (baseline, high_slippage, very_high_slippage, with_fees) and run **without** `--no-stress` so the saved JSON includes them. For faster iteration, temporarily set `max_slugs_per_strategy_per_period: 20` or `25`, run `python scripts/run_backtest_rigorous.py --save-report`, then restore 150 for production-style runs.

- **Categories**
  - Use `--categories` to focus on categories that have 2024 data (e.g. if build_backtest_market_list.py tags by year or category). Example:  
    `python scripts/run_backtest_rigorous.py --categories fade_political_election fade_breaking_political --save-report`

- **Min markets per category**
  - If arbitrage categories have few slugs with 2024 data, temporarily lower `min_markets_per_category` in the plan so more categories are included, or add arbitrage-specific categories that are built from 2024-active events only.

**Summary:** Current setup is adequate for **fade** (enough markets and periods to conclude “no edge; refine”). For **arbitrage**, the setup is inadequate until the market list or date ranges are aligned with CLOB data availability; apply the fixes above and re-run before deploy/refine/reject.
