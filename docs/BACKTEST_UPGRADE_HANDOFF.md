# Backtest Upgrade — Handoff for Senior Engineer

This doc describes the rigorous backtest upgrade and where to extend it.

## Status (Implemented)

- **Multi-period** — Multiple non-overlapping periods from plan; per-period and aggregate results.
- **Train/test** — Out-of-sample split: aggregate PnL by train vs test period labels; optional via `--no-train-test`.
- **Walk-forward** — Config in plan (`walk_forward.enabled`); when enabled, per-period test PnL and aggregate printed and included in JSON report.
- **Stress scenarios** — Named scenarios (e.g. `high_slippage`, `with_fees`) with slippage/fee overrides; optional `--no-stress` runs baseline only and still produces full report.
- **Sharpe, max drawdown, per-period stats** — Computed for baseline; printed in aggregate section; stored in `per_strategy_metrics` in JSON report (`sharpe_annual`, `max_drawdown_pct`, `per_period_returns_pct`).
- **Category-specific runs** — CLI flag `--categories` accepts a list of category keys (e.g. `both_elections`, `both_macro`, `arbitrage_elections_president`); when provided, only those categories are used via `get_slugs_for_strategy(..., categories=...)`; when not provided, behavior unchanged (all categories meeting `min_markets_per_category`).
- **2024-focused market list** — `build_backtest_market_list.py --end-year 2024` (or `--end-date-min` / `--end-date-max`) keeps only events that ended in that year/range, so backtests over 2024 periods use slugs that have CLOB history in that window.
- **Pre-run validation** — `run_backtest_rigorous.py --validate N` checks N slugs per strategy for the first period’s date range and `min_bars`; prints pass/fail and suggests rebuilding with `--end-year 2024` if many fail.
- **Quick runs** — `run_backtest_rigorous.py --quick` caps at 25 slugs per strategy so stress runs complete in reasonable time; use without `--quick` for full slug set.
- **Preload + shared cache** — All period data and resolution outcomes are loaded once in parallel (6 workers) before the 32 backtest runs; runs use in-memory caches only (no API calls during runs). See *Performance* below.

## Performance: preload and shared cache

**Bottleneck (fixed):** Previously, each of the 32 runs (2 strategies × 4 periods × 4 stress scenarios) loaded market data and resolution per slug independently, so the same data was fetched up to 32× and API calls dominated runtime.

**Fix:** The script now (1) builds shared `data_cache` and `resolution_cache` dicts, (2) preloads all period data in parallel via `_fill_period_cache()` and `ThreadPoolExecutor` (6 workers), one batch per period, (3) prefills resolution outcomes for all slugs in parallel, then (4) runs the 32 backtest runs with no `load_market_data` or `get_resolution_outcome` calls—only cache lookups. Implemented in `scripts/run_backtest_rigorous.py` (shared caches, preload loop, and passing caches into `run_one_period()`).

**Expected runtime (approximate):**
- **Quick** (25 slugs per strategy): ~3 minutes total (preload + 32 runs).
- **60 slugs per strategy:** ~10–15 minutes.
- **Full** (150 slugs per strategy): ~30–45 minutes.

## Accuracy and productivity

- **Train/test** — Use it. Tune on train periods, judge on test; avoid overfitting to one stretch of history.
- **Multiple periods** — Covers different regimes; one quarter can be fluky; aggregate Sharpe/drawdown across periods.
- **Stress scenarios** — If baseline looks good but high_slippage or with_fees kills PnL, the edge is fragile; fix sizing or execution before live.
- **Validate slugs first** — `--validate N` avoids wasting a long run on slugs with no CLOB history for the date range; rebuild with `--end-year` if many fail.
- **Quick then full** — Run `--quick --save-report` to confirm pipeline and get a report in ~3 min; then run full (or `--max-slugs 60`) when you need production metrics.

## What Was Added

### 1. Config: `config/backtest_rigorous.yaml`
- **periods**: Multiple non-overlapping date ranges (e.g. 2024 Q1–Q4) for regime coverage.
- **train_test**: Out-of-sample split: `train_period_labels` vs `test_period_labels`. Tune on train, report test.
- **stress_scenarios**: Named scenarios (e.g. `high_slippage`, `with_fees`) with `slippage_mult` and `fee_bps` to test robustness.
- **market_list_path**: Points to `config/backtest_markets.yaml` (real slugs by category).
- **min_markets_per_category**: Only use categories with ≥ N slugs for solid sample size.

### 2. Engine: `src/backtest/engine.py`
- **Optional kwargs**: `slippage_mult=1.0`, `fee_bps_override=None`. Used for stress tests without editing `settings.yaml`.
- **Spread path**: When `use_spread_slippage` is True, slippage is `(spread/2) * _slippage_mult` so stress scales both BPS and spread-based slippage.

### 3. Market list loader: `src/backtest/market_list_loader.py`
- **get_slugs_for_strategy(plan_path, strategy, categories=None, min_markets_per_category=30, max_slugs_per_strategy=None)**  
  Returns slugs from `backtest_markets.yaml` for a given strategy (arbitrage/fade/both), optionally filtered by category and min count.

### 4. Rigorous runner: `scripts/run_backtest_rigorous.py`
- Loads `backtest_rigorous.yaml` and `backtest_markets.yaml`.
- For each **strategy**, **period**, and **stress scenario**: loads data for slugs, runs `BacktestEngine` with overrides, collects results.
- **Train vs test**: Aggregates results by train/test period labels and prints separate PnL.
- **Output**: Prints per-run and aggregate summary; optional `--save-report` writes `data/backtest/reports/backtest_rigorous_<ts>.json`.

### 5. Existing script: `scripts/run_backtest_multi.py`
- **--market-list**: If set, loads slugs from `config/backtest_markets.yaml` for the chosen strategy (min 30 per category) and uses them as `slugs_override` (no Gamma discovery). Single-period, quick runs.

## Usage

```bash
# Rigorous: multi-period + train/test + stress (from plan)
python scripts/run_backtest_rigorous.py --save-report

# Rigorous: periods only, no train/test, no stress (baseline only, still produces report)
python scripts/run_backtest_rigorous.py --no-train-test --no-stress

# Rigorous: run only specific categories from config/backtest_markets.yaml
python scripts/run_backtest_rigorous.py --categories both_elections both_macro
python scripts/run_backtest_rigorous.py --categories arbitrage_elections_president --save-report

# Rigorous: validate that slugs have data for first period (then exit; suggests --end-year if many fail)
python scripts/run_backtest_rigorous.py --validate 20

# Rigorous: quick run (25 slugs per strategy) with stress and report
python scripts/run_backtest_rigorous.py --quick --save-report

# Rigorous: live status bar in another file (open RUN_STATUS.md in second pane)
python scripts/run_backtest_rigorous.py --quick --save-report --status-file data/backtest/reports/RUN_STATUS.md

# Rebuild market list for 2024 backtests (only events that ended in 2024)
python scripts/build_backtest_market_list.py --end-year 2024 --max-events 8000

# Existing multi-run with category-based slugs (single period)
python scripts/run_backtest_multi.py --strategy fade --start 2024-10-01 --end 2024-11-30 --market-list --target 50
```

## Better test data (2024 backtests)

If arbitrage (or fade) has very few markets with data in 2024:

1. **Rebuild the market list** so it includes events that ended in 2024:  
   `python scripts/build_backtest_market_list.py --end-year 2024 --max-events 8000`  
   Then re-run the rigorous backtest.

2. **Validate before a full run** to see how many slugs have data:  
   `python scripts/run_backtest_rigorous.py --validate 20`  
   If most fail, adjust the date range in the plan or rebuild with `--end-year` (or `--end-date-min` / `--end-date-max`).

3. **Faster stress runs** without changing the plan:  
   `python scripts/run_backtest_rigorous.py --quick --save-report`  
   Uses 25 slugs per strategy; drop `--quick` for the full 150 when you need full coverage.

## Safety and run mode (ProbablyProfit-inspired)

- **Paper vs live** — Run with `--paper` (default) or `--live --confirm-live`; the latter prompts for typing `YES` before enabling live trading. Dry-run is set from CLI and overrides config.
- **Kill switch** — If `data/KILL_SWITCH` exists, the main loop skips placing new trades. Use `python scripts/emergency_stop.py` to create it and `python scripts/resume_trading.py` to remove it (or `python -m src.main --emergency-stop` / `--resume-trading`).
- **Preflight** — Run `python scripts/preflight.py` (optionally `--check-clob`) before starting the bot to verify API keys, config, and optionally CLOB connectivity.
- **AI consensus** — In `config/settings.yaml`, set `ai.consensus_enabled: true` and `ai.consensus_min_agree: 2` so the bot only acts when at least two providers agree on BUY_YES or BUY_NO.

Strategy prompt templates and a future prompt-driven strategy are documented in `docs/STRATEGY_PROMPTS.md`.

## Where to Extend (Senior Engineer)

1. **New periods / regimes**  
   Edit `config/backtest_rigorous.yaml` → `periods` and, if using train/test, `train_test.train_period_labels` / `test_period_labels`.

2. **New stress scenarios**  
   Add entries under `stress_scenarios` in `backtest_rigorous.yaml` (e.g. `reduced_liquidity` with a different multiplier or future engine support).

3. **Category-specific runs**  
   Implemented. Use `--categories both_elections both_macro` (or other keys from `backtest_markets.yaml`). Passes `categories` into `get_slugs_for_strategy()`; when not provided, all categories meeting `min_markets_per_category` are used.

4. **Walk-forward / rolling train-test**  
   Implement a new mode that, for each period, trains on previous periods and tests on the current one; aggregate test PnL across windows. Plan could add `walk_forward: true` and a `train_lookback_periods` count.

5. **Sharpe, drawdown, per-period stats**  
   In the rigorous script, compute and store in the JSON report: Sharpe ratio, max drawdown, per-period returns. Optionally read from `BacktestResult` / trade list if the engine exposes them.

6. **Liquidity / fill stress**  
   If the loader or engine supports liquidity or book depth, add a stress scenario that simulates worse fills (e.g. scale size by 0.5 or use a stricter spread threshold).

All new behavior should keep using **real data only** (slugs from `build_backtest_market_list.py` output) and the existing `BacktestEngine` + `PolymarketLoader` so results stay comparable.
