## Hermes BTC 5m Research Mapping — PSB Code Audit

Date: 2026-06-11
Scope: map Hermes' 2026-06-10 BTC 5m Up/Down research notes against PSB's current local code. This is an audit only; no strategy or config changes were made.

### Bottom Line

Hermes' proposed direction is plausible and mostly aligned with PSB's own recent instrumentation: **Window Delta** should be treated as the clean candidate for a dedicated BTC 5m snipe lane. PSB is not currently running that lean strategy. The current BTC 5m path is still a hybrid of HTF bias, 4H/1H histogram gates, 5m candle momentum labels, RSI blocks, calibration bumps, lane policy, and Kelly sizing.

The immediate next implementation should not replace `bitcoin` wholesale. Add a separate **BTC 5m Window Delta Snipe shadow lane** that uses CLOB executable prices and writes neutral rows first. Promote to paper execution only after the shadow/ghost evidence clears the lane-specific bar.

### Claim Mapping

| Hermes claim | PSB current state | Assessment |
|---|---|---|
| **Window Delta is the primary signal.** | `src/analysis/window_delta.py` already defines time-aware `window_delta_pct` and `window_delta_prob`, but `src/strategies/bitcoin.py` uses it for BTC reject context only. BTC 5m entry decisions still come from `compute_btc_5m_quant(...)`, HTF, MACD, RSI, and 5m momentum. | Directionally aligned, not implemented as primary BTC 5m logic. |
| **Snipe late with a polling loop around T-10s.** | BTC 5m config allows `entry_window_min: 0.5`, `entry_window_max: 5.5`, with `entry_window_latency_buffer_sec: 12`. This is a broad window, not T-10s polling. | Not implemented. Current loop cadence and scanner lookahead may miss true T-10s behavior. |
| **TP 15-20%, SL 30%.** | Global and per-lane exits are configurable, but exit tuning is outside ghost coverage. `updown_exit_shared.py` and `live_testing.py` handle TP/SL logic. | Needs live/journal counterfactual validation, not ghost-only. |
| **Hard max entry price around $0.70.** | BTC 5m lane policy is tighter: `entry_price_min/max` around `0.45-0.55`, plus separate guards such as `buy_no_min_no_price` and bull-regime BUY_NO caps. | Current config blocks many higher-priced momentum snipes. Do not widen blindly; test only under strong delta. |
| **Maker entry / post-only.** | `main.py` calls `clob_client.place_order(... post_only=True ...)`; `clob_client.py` maps that to `OrderType.POST_ONLY`. | Implemented for live/paper order submission. |
| **Use BUY side for selected outcome.** | `BUY_YES` buys YES token; `BUY_NO` buys NO token via `_resolve_execution_intent`. Order outcome is logged as YES/NO. | Conceptually implemented. |
| **CLOB ask/bid must drive paper/live, not Gamma fantasy prices.** | Up/down event parsing starts from Gamma `outcomePrices`, then scanner hydration uses CLOB `/midpoint`, not best executable ask. Execution posts at `signal.price`, and BTC sets `order_price = yes_price - 0.01` or NO equivalent. | Material risk. A snipe lane should snapshot CLOB book and compute edge from executable ask / post-only limit behavior. |
| **MagicLink accounts may need `signature_type=1`.** | `CLOBClient.set_credentials` constructs `PyClobClient(host, chain_id, key, creds)` with no explicit `signature_type` or `funder`; repo has no config/env path for this. | Needs account-type verification before live orders if using MagicLink/proxy wallet. |
| **Backtester must use CLOB asks.** | Repo policy says old backtest engines are not source of truth. Validation source is settled ghost log / Ghost Lab. | Reframe: do not fix old backtest first; add CLOB-priced shadow rows and settle them. |

### Current BTC 5m Path

Relevant files:

- `src/strategies/bitcoin.py`
- `src/strategies/btc_updown_5m.py`
- `src/analysis/window_delta.py`
- `src/market/scanner.py`
- `src/execution/clob_client.py`
- `config/settings.yaml`

Current BTC 5m flow:

1. Scanner fetches 5m up/down markets from Gamma event slugs.
2. Scanner parses Gamma `outcomePrices`, then `update_market_prices()` hydrates with CLOB `/midpoint`.
3. `bitcoin.py` routes 5m markets through `compute_btc_5m_quant(...)`.
4. `compute_btc_5m_quant(...)` starts from 0.50 and adjusts with HTF boost, histogram gate, RSI block, 5m candle direction, and prediction-window bonus.
5. Final filters apply lane entry window, min edge, entry price band, Kelly/exposure sizing, and risk checks.
6. Execution submits a post-only limit order at `signal.price`.

Key mismatch: `window_delta_prob` is available but not the primary BTC 5m decision input.

### Pricing Risk

The largest practical issue in Hermes' notes is valid for PSB: **decision price and fill price are not yet guaranteed to be the same economic object**.

Current scanner/execution behavior:

- Gamma event parser reads `outcomePrices`.
- Generic hydration uses CLOB `/midpoint`.
- BTC strategy computes edge from `market.yes_price`.
- BTC strategy posts `order_price = yes_price - 0.01` for `BUY_YES`, or `(1.0 - yes_price) - 0.01` for `BUY_NO`.
- CLOB order-book snapshots exist in `CLOBClient.fetch_order_book_snapshot(...)`, but the BTC 5m decision path does not use best ask/bid for entry economics.

For a late snipe lane, edge should be computed from:

- `buy_yes_paid = best_yes_ask` for taker simulation, or selected post-only limit if maker simulation.
- `buy_no_paid = best_no_ask` for BUY_NO, not `1.0 - yes_mid`.
- fillability / queue risk explicitly recorded if using post-only below ask.

### Validation Plan

Do not use the removed/broken backtest engines for this. Use shadow and ghost-style forward evidence.

Recommended validation sequence:

1. Add BTC to the neutral `window_delta_shadow.jsonl` writer currently used by alt macro paths.
2. For every BTC 5m candidate, log:
   - `strategy=bitcoin`
   - `window=5m`
   - `market_id`
   - `action_by_window_delta`
   - `wd_pct`
   - `wd_prob`
   - `mins_left`
   - `yes_mid`
   - `yes_best_bid`
   - `yes_best_ask`
   - `no_best_bid`
   - `no_best_ask`
   - `post_only_limit_price`
   - `would_cross`
   - `spread`
   - `source_price_kind`
3. Extend `scripts/window_delta_shadow_settle.py` or add a BTC-specific mode that calculates EV from executable side price, not only `yes_price`.
4. Require a minimum settled sample before paper execution. Suggested initial gate: n >= 50 for BTC 5m, positive EV/$ in the chosen edge bucket, and no severe degradation after fees/spread assumptions.
5. Only after shadow passes, add a config-disabled paper lane such as `strategies.bitcoin.window_delta_snipe_5m.enabled: false`, then enable for paper with tight sizing.

### Implementation Action Items

1. **CLOB price snapshot helper**
   - Add a helper that returns best bid/ask for both YES and NO token IDs.
   - Use it before any BTC 5m snipe decision.
   - Fail closed if either selected-side ask is missing or stale.

2. **BTC 5m shadow logger**
   - Mirror `sol_macro._shadow_log_window_delta(...)` for BTC.
   - Include CLOB executable prices, not just midpoint/Gamma price.

3. **Snipe decision module**
   - Keep separate from `compute_btc_5m_quant(...)`.
   - Input: current BTC, window open BTC, mins left, CLOB book, config thresholds.
   - Output: `HOLD | BUY_YES | BUY_NO`, paid price, limit price, edge, reason.

4. **Timing loop**
   - Current scan cadence is not enough for a true T-10s snipe unless the bot happens to scan at the right moment.
   - Add a narrow per-market late-window watcher only after shadow data says the signal is worth it.

5. **CLOB account preflight**
   - Verify whether the operator uses private-key EOA or MagicLink/proxy wallet.
   - If MagicLink/proxy, add config/env support for `signature_type` and `funder` if required by installed `py-clob-client>=0.30.0`.

### Kanban

| Status | Item | Owner |
|---|---|---|
| To Do | Add BTC 5m CLOB book snapshot utility | Codex/Claude |
| To Do | Add BTC rows to `window_delta_shadow.jsonl` | Codex/Claude |
| To Do | Extend shadow settle script to use executable ask/post-only assumptions | Codex/Claude |
| To Do | Verify CLOB account type and `signature_type` requirement | Operator + agent |
| Later | Build config-disabled BTC 5m window-delta snipe lane | Codex/Claude |
| Later | Add late-window watcher/polling loop after shadow evidence passes | Codex/Claude |

### Metadata/Summary

Tags: #PSB #BTC5m #WindowDelta #Polymarket #CLOB #GhostLab
Related Concepts: [[Window Delta]], [[CLOB Execution]], [[Ghost Lab]], [[BTC 5m Strategy]], [[Post-Only Orders]]

Summary: Hermes' BTC 5m research maps well to a new PSB shadow lane, but not to the current live BTC 5m implementation. The main blocker is price-source integrity: a late snipe lane must decide and settle against CLOB executable prices, not Gamma outcome prices or midpoint approximations.
