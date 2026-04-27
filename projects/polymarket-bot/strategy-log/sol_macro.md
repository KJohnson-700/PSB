# SOL macro (`sol_macro`)

SOL **Up or Down** vs BTC correlation/lag; macro + LTF + optional LLM; entry timing windows on up/down markets.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | - | `/api/journal/summary` |
| Win rate | - | same |
| Net PnL | - | same |

## Change Log

### 2026-04-26 — Optional RSI hard gates (shared `SolMacroStrategy` implementation)

- **What changed:** `src/strategies/sol_macro.py` now applies optional `rsi_buy_block_above` / `rsi_sell_block_below` via `_rsi_blocks_entry` before entries; strategies opt in per YAML. ETH enables `rsi_buy_block_above: 80.0` (see `eth_macro.md`); SOL/HYPE/XRP unchanged until configured.
- **Why:** Single implementation path for all macro up/down classes that inherit `SolMacroStrategy`.
- **Hypothesis:** No behavior change for strategies that omit the new keys; see `eth_macro.md` for the ETH exhaustion-entry hypothesis.
- **Expected outcome:** Config-driven RSI ceilings/floors without forking per-asset entry loops.
- **Actual outcome:** `pending` (ETH-specific outcomes tracked under `eth_macro.md`).
- **Status:** `pending`

### 2026-04-21 — UTC blocklist scope-back to Tier A + re-audit cadence

- **What changed:** `strategies.sol_macro.blocked_utc_hours_updown` narrowed from `[1, 6, 9, 18, 22, 23]` to **`[1, 6, 23]`** in `config/settings.yaml`. H9 / H18 / H22 removed from the block (downgraded to "watch").
- **Why:** Evidence audit (see `.cursor/plans/block-list-evidence-audit_f364fc11.plan.md`) found that H1 / H6 / H23 are backed by a **621-trade** backtest with WRs in the 38–42% range and -$28 to -$33 per trade — above the `MIN_TRADES=5` / `BAD_WR_THRESHOLD=0.46` / `BAD_EV_THRESHOLD=-$2` bar. H9 was added on **n=7** from paper slice `reset_20260416` (~29% WR, -$25) with explicit reasoning "parity with bitcoin/eth_macro" rather than standalone evidence. H18 / H22 were cited as "14% WR, ~-$16.80" with no explicit sample size — a 14% WR implies n≈7 which is below the discipline `scripts/hourly_heatmap.py` enforces in `--suggest`. The config author already wrote "do not block H17 without more data" (line 188) for the same reason; this change applies that same principle consistently to H9 / H18 / H22.
- **Hypothesis:** Removing weakly-supported blocks lets SOL macro trade more hours, accelerating the per-hour sample accumulation needed for statistically meaningful re-validation.
- **Expected outcome:** ~3 more eligible hours/day for SOL macro up/down; within ~2 weeks of live trading the hourly heatmap should have per-hour samples that cross the `MIN_TRADES=5` threshold for the previously-watched hours.
- **Actual outcome:** `pending`.
- **Re-audit cadence:** Weekly `python scripts/hourly_heatmap.py --days 14 --suggest`; re-promote a watched hour to Tier A only on **≥15 trades AND WR < 0.46 AND avg PnL < -$2**.
- **Status:** `pending`

### 2026-04-21 — Correctness: `scan_and_analyze` on class, `enabled`, weekend helper, `_bump_skip`

- **What changed:** (1) `async def scan_and_analyze` is a method of **`SolMacroStrategy`** (was accidentally nested inside `_get_weekend_penalty`, so **`ETHMacroStrategy` / `HYPEMacroStrategy` had no method**). (2) **`_get_weekend_penalty()`** is again a **module-level** function; **`conditions_from_ta`** still calls it. (3) **`self.enabled`** set in **`SolMacroStrategy.__init__`** (`enabled` from `strategies.sol_macro`, default **True**). (4) Local **`skip_reasons` + `_bump_skip`** added before the market loop (parity with **`bitcoin.py`**). (5) Minor f-string parenthesis fix in AI context line for `min_edge`.
- **Why:** Bot could not start or crypto legs logged **`AttributeError`** / **`NameError`**; Railway/local **`_crypto_cycle`** depended on **`scan_and_analyze`** for SOL/ETH/HYPE.
- **Hypothesis:** Restoring structure + `enabled` + skip helper restores live behavior without changing strategy economics.
- **Expected outcome:** No missing-method errors; SOL macro scans run; ETH/HYPE inherit behavior.
- **Actual outcome:** `pending` for ≥15 closed trades post-deploy; **engineering:** `pytest` / `py_compile` clean for `sol_macro` in session.
- **Status:** `pending` (live PnL validation)

### 2026-04-18 — 5m min_edge parity + H09 UTC block + comment hygiene

- **What changed:** `strategies.sol_macro.min_edge_5m` **0.12 → 0.09** (match `eth_macro`); `backtest.min_edge_sol_5m` **0.09**; `blocked_utc_hours_updown` added **H9** (list now `[1, 6, 9, 18, 22, 23]`); `entry_price_min` comment updated (removed stale 0.47–0.49 “100% WR” claim).
- **Why:** Live mix was ~15m-dominated (~57% WR, heavy ~−$10 tails) while 5m barely traded at 0.12; H09 showed weak SOL exits in paper slice and is already blocked for BTC/eth.
- **Hypothesis:** More 5m participation at the same threshold as ETH improves horizon balance without starving edge; H9 removal cuts a recurring loss pocket.
- **Expected outcome:** Higher 5m trade count; stable or improved SOL net PnL; fewer H09 losers.
- **Actual outcome:** `pending` (need ≥15 closed `sol_macro` trades after deploy).
- **Status:** `pending`

### 2026-04-11 — Entry window auto-alignment (scan cadence)

- **What changed:** `SolMacroStrategy._resolve_entry_window_bounds()` — same mechanism as BTC: optional widening of `entry_window_*` when `entry_window_auto_align: true`, driven by `entry_window_align_scan_interval_sec`, `entry_window_auto_align_max_expand_min`, `entry_window_auto_align_jitter_sec`. Implemented in `src/strategies/sol_macro.py`; flags under `strategies.sol_macro` in `config/settings.yaml`.
- **Why:** 5m main-loop cadence + Railway latency made tight minute remaining windows easy to miss; operators saw healthy cycles but persistent timing skips.
- **Hypothesis:** Bounded expansion preserves early-candle intent while improving hit rate vs `outside_entry_window`.
- **Expected outcome:** More consistent eligibility checks inside the intended early window; downstream gates (BTC $ move, price band, macro/LTF) unchanged.
- **Actual outcome:** `pending` (≥15 closed trades post-deploy or comparable ops `top_skip_reasons` shift).
- **Status:** `pending`

## Review sessions

_(none yet)_

## Lessons learned

_(none yet — add only after data)_
