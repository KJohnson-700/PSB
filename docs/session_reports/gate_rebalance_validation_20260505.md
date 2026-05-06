# Gate rebalance validation — 2026-05-05

## Scope (rebalance signal suppression)

- **RSI:** Soft penalty + optional hard gate (`rsi_hard_gate_enabled`, `rsi_soft_penalty_*`) on `sol_macro` / `eth_macro` (and config parity for `hype_macro` / `xrp_macro`). Rollout escape hatch: set `rsi_hard_gate_enabled: true` to restore binary blocks.
- **15m LTF stack:** `min_edge_15m_when_ltf_unconfirmed` eased **0.11 → 0.10** on macro lanes (per `config/settings.yaml`); **bitcoin** entry bands and `min_edge_buy_no` adjusted as in config.
- **Gate-stack audit:** RSI hard block replaced by default-soft path reduces starvation where RSI and edge gates stacked; LTF-unconfirmed floor relaxed slightly so 15m can still trade when other gates align. Combined interactions should be watched via skip telemetry (`rsi_soft_penalty_applied`, `edge_after_penalty_below_threshold`, existing counters).

## Baseline attribution artifacts

- `docs/session_reports/attribution_since_gate_rebalance_baseline_old_034719.{md,json}`
- `docs/session_reports/attribution_since_gate_rebalance_baseline_post150648.{md,json}`

## Backtests (local, held-out test split)

Window chosen so **local oracle JSONL** fully covers dates (cache starts ~2026-04-08 for SOL/ETH feeds). Split: **TRAIN** 2026-04-09→2026-04-15, **TEST** 2026-04-15→2026-04-20.

| Run | TRAIN (PnL / WR / trades) | TEST (PnL / WR / trades) | Report JSON |
|-----|---------------------------|--------------------------|-------------|
| SOL 15m | -$15.75, 40.0%, 10 | -$14.93, 45.8%, 24 | `data/backtest/reports/backtest_crypto_SOL_15m_20260504_234851.json` |
| ETH 15m | $0, n/a, 0 | -$7.57, 0%, 1 | `data/backtest/reports/backtest_crypto_ETH_15m_20260504_234902.json` |

Short-window ETH had **no train trades** (gates / tape); treat metrics as **sanity / smoke** only, not a full strategy sign-off.

## Infra fix: oracle cache “stall” on backtest

`OracleHistoryLoader._cache_covers` required `last >= end_ts` with `end_ts` = end-of-day **23:59:59**. The last Chainlink tick in cache was often a few **seconds** earlier, so the loader assumed cache miss and walked rounds on RPC (~`max_rounds`), making runs look hung. **Fix:** allow **2-minute** slack on the end bound (`_ORACLE_CACHE_COVER_END_SLACK` in `src/backtest/oracle_loader.py`). Regression: `test_cache_covers_accepts_last_tick_slightly_before_end_of_day`.

## Tests

- Full suite: `320+` tests green includingweather/slug expectations updated in `tests/test_strategies.py`, `tests/test_hype_integration.py`; oracle test in `tests/test_backtest_oracle_replay.py`.

## Rollout guardrails

1. Ship with **`rsi_hard_gate_enabled: false`** and soft penalties on; if live shows RSI regime pollution, flip **hard** on that lane only.
2. Re-run `scripts/run_backtest_crypto.py` with **`--test-start`** after any further threshold edits; align `--start` with oracle file coverage or extend `data/backtest/oracle/*.jsonl`.
3. Monitor Discord/journal only for execution outcomes; use dashboard / skip keys for gate diagnostics.
