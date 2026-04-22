# Rigorous Backtest Report

**Report generated:** 2026-03-13  
**Source JSON:** `data/backtest/reports/backtest_rigorous_20260313_230106.json`  
**Config:** `config/backtest_rigorous.yaml` (market list: `config/backtest_markets.yaml`)

---

## Run configuration (this report)

- **Scope:** Reduced run for timely report: **15 slugs** per strategy, **baseline only** (no stress scenarios).  
  A full run with 150 slugs and all stress scenarios (baseline, high_slippage, very_high_slippage, with_fees) was started separately and may take 30+ minutes; when it completes, the latest `backtest_rigorous_<timestamp>.json` in this folder will contain stress results.
- **Train/test:** Enabled. Train: 2024 Q1, 2024 Q2. Test: 2024 Q3, 2024 Q4.
- **Periods:** 2024 Q1, Q2, Q3, Q4 (as in plan).
- **Stress scenarios in this run:** baseline only.

---

## Strategy tested

- **Fade**
- **Arbitrage**

---

## Markets used

- **Fade:** 15 slugs from `config/backtest_markets.yaml` (by_strategy.fade, min_markets_per_category=30). After filtering by `min_bars` and date range, **2–4 markets** had data per period (e.g. eric-adams-resigns-in-march, mike-johnson-out-as-speaker-before-may, roaring-kitty-charged-in-2024, mike-johnson-out-as-speaker-before-election, will-giuliani-appear-in-court-for-arizona-indictment).
- **Arbitrage:** 15 slugs from the same config. **No markets** had sufficient CLOB history in 2024 for any period → “No data for 15 slugs” in all four periods. The first 15 arbitrage slugs in the list are likely older events; a run with 150 slugs would include more recent markets that may have 2024 data.

---

## Backtest period

- **2024 Q1:** 2024-01-01 – 2024-03-31  
- **2024 Q2:** 2024-04-01 – 2024-06-30  
- **2024 Q3:** 2024-07-01 – 2024-09-30  
- **2024 Q4:** 2024-10-01 – 2024-11-30  

---

## Performance metrics

### Fade (baseline, 4 periods)

| Metric            | Value        |
|------------------|-------------|
| **Total PnL**    | **-$202.03** |
| **ROI**          | ~-10.1% (on allocated capital over periods) |
| **Win rate**     | N/A (not computed in report; all 4 period returns negative) |
| **Sharpe (ann.)**| **-48.41**  |
| **Max drawdown** | **-9.7%**   |

- **Train PnL (2024 Q1+Q2):** -$81.60  
- **Test PnL (2024 Q3+Q4):** -$120.43  
- **Per-period returns %:** Q1 -2.82%, Q2 -1.26%, Q3 -3.55%, Q4 -2.47%.

### Arbitrage (baseline)

- **No data** in any period for the 15 slugs used → no PnL, Sharpe, or max drawdown.  
- **Train PnL:** $0.00 (no results).  
- **Test PnL:** $0.00 (no results).

---

## Execution metrics

From the fade baseline results:

- **Total trades (all periods):** 175  
- **Blocked trades:** 0  
- **Execution cost total:** ~\$4.24 across all markets/periods  
- **Avg slippage / fill rate / avg trade duration:** Not broken out in this JSON (computed inside engine; could be added to report in future).

---

## Risk analysis

- **Capital at risk:** Bankroll $2,000 per period (split across markets with equal allocation per market).  
- **Market concentration:** Fade used 2–4 markets per period; high concentration in a small set of slugs.  
- **Sensitivity tests:** **Not run in this report** (baseline only). For **high_slippage**, **very_high_slippage**, and **with_fees**, run the full rigorous backtest and use the new JSON report.

---

## Stress scenario results (this run)

Only **baseline** was executed.  

- **high_slippage / very_high_slippage / with_fees:** Not available in `backtest_rigorous_20260313_230106.json`.  
- To get stress results: run  
  `python scripts/run_backtest_rigorous.py --save-report`  
  and use the latest `backtest_rigorous_<timestamp>.json` after it finishes.

---

## Key insights

**Fade (15-slug reduced run, baseline)**  
- **Weaknesses:** Negative PnL in every period; train and test both lose; very negative Sharpe; drawdown about 9.7%. Small sample (2–4 markets per period) and only 15 slugs, so results are indicative, not definitive.  
- **Strengths:** No data errors; execution path runs end-to-end; train/test split is consistent with plan.

**Arbitrage**  
- **Weaknesses:** No markets with sufficient data in 2024 for the first 15 slugs → strategy not evaluated.  
- **Next step:** Run with full 150 slugs (or a list of slugs known to have 2024 CLOB history) to get arbitrage baseline and stress metrics.

**Methodology**  
- Script fix applied: `--no-stress` now runs the **baseline** scenario so that quick runs still produce aggregate and train/test metrics.  
- Config was temporarily set to 15 slugs for this report and then restored to 150 for future full runs.

---

## Recommendation

| Strategy    | Recommendation | Reasons |
|------------|----------------|---------|
| **Fade**   | **Refine**     | (1) Consistent losses in both train and test and very negative Sharpe in this reduced run. (2) Need validation on a larger universe (150 slugs) and under stress (high_slippage, with_fees) before any deploy. |
| **Arbitrage** | **Refine**   | (1) No data for the 15 slugs in 2024; strategy was not evaluated. (2) Re-run with full market list (and/or slugs with 2024 history), then re-assess baseline and stress before considering deploy. |

**Overall:** Do **not deploy** either strategy to live trading based on this report. Run the full rigorous backtest (150 slugs, all stress scenarios), then re-run this report structure on the new JSON to get stress robustness and arbitrage results before any deploy decision.
