# Bitcoin up/down (`bitcoin`)

BTC **Up or Down** markets (15m / 5m) with hierarchical HTF/LTF gates, optional LLM assist, and entry timing windows.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 209 | `data/backtest/reports/backtest_crypto_BTC_15m_20260502_202236.json` |
| Win rate | 50.7% | same |
| Net PnL | -$12.60 | same |
| Paper closes (`test_20260504_034719`) | 48 | [`docs/session_reports/session_parse_test_20260504_034719.json`](docs/session_reports/session_parse_test_20260504_034719.json) |
| Paper WR (same session) | 54.2% | same |
| Paper net PnL (same session) | -$21.42 | same |
| Paper BUY_NO (same session) | 10 trades, 30.0% WR, -$16.23 | same |
| Paper counter-trend `btc_4h_hist_declining` (same session) | 9 trades, 22.2% WR, -$17.03 | same |

## Change Log

### 2026-05-06 — BTC 5m counter-trend guardrail restore

- **What changed:** Restored the documented BTC-only guardrails in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml): `disable_buy_no_counter_trend: true` and `min_edge_buy_no: 0.11` (from live drifted values `false` / `0.08`).
- **Why:** Current paper session `test_20260505_225241` showed BTC 5m underperformance concentrated in one path: **5** closed trades, **-$14.80** net, **0% WR**, all **BUY_NO**, all **updown_time_stop**, all tagged `counter_trend=btc_4h_hist_declining`. That is exactly the path the prior BTC review said to suppress.
- **Hypothesis:** Re-blocking bullish-HTF counter-trend `BUY_NO` and restoring the stronger BUY_NO edge floor should stop this single failing admission path without changing BTC exit logic or valid bearish BTC short setups.
- **Expected outcome:** Next BTC 5m sample should show no new bullish-HTF counter-trend `BUY_NO` entries; if BTC still loses materially after that, phase 2 should evaluate BTC-specific stop tuning rather than reopening this path.
- **Actual outcome:** `pending` (need post-restore BTC sample).
- **Status:** `pending`

### 2026-05-03 — `min_4h_hist_magnitude` middle ground (20 → 35)

- **What changed:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.bitcoin.min_4h_hist_magnitude`: **20 → 35** (between macro **`btc_min_4h_hist_magnitude` 20** and prior legacy **50**).
- **Why:** Live priority is firing consistency vs starving BTC lane; full alignment at 20 was acceptable for ops parity but operator prefers less aggressive conviction cut than macros — middle value preserves most of the NEUTRAL relief vs **50** without matching macro threshold exactly.
- **Hypothesis:** Fewer `"below conviction threshold … downgrading to NEUTRAL"` gaps than **50**; slightly more chop filtering than **20**.
- **Expected outcome:** Logs/journal show BTC HTF passing Layer 1 more often than hist=50 regime; judge on live/paper skips and **`bitcoin`** signal counts — not backtest sheet on this knob alone.
- **Actual outcome:** `pending` (≥15 BTC closes post-deploy or clear ops telemetry — do not estimate).
- **Status:** `pending`

### 2026-05-02 — `min_4h_hist_magnitude` aligned to macro BTC HTF (50 → 20)

- **What changed:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.bitcoin.min_4h_hist_magnitude`: **50 → 20**, matching `btc_min_4h_hist_magnitude` used by SOL/ETH/HYPE/XRP for the same BTC 4H MACD conviction gate (`BitcoinStrategy._get_higher_tf_bias` vs `SolMacroStrategy._get_btc_htf_bias`).
- **Why:** 50 aggressively downgraded HTF to NEUTRAL vs macros still seeing BULL/BEAR; ops starvation while Layer 1 timeframe unchanged.
- **Hypothesis:** Fewer NEUTRAL churn cycles on BTC lane while downstream gates (anti-LTF, edge, windows, bands) still filter chop.
- **Expected outcome:** BTC HTF labels closer to macro dashboard narrative; trade count may rise vs hist=50 regime.
- **Actual outcome:** Backtest BTC 15m **2026-01-20 → 2026-04-20** `data/backtest/reports/backtest_crypto_BTC_15m_20260502_202236.json`: **209** trades, **50.72%** WR, **-$12.60** net PnL, expectancy **-$0.060**/trade — vs prior snapshot `data/backtest/reports/backtest_crypto_BTC_15m_20260429_154935.json`: **174** trades, **52.3%** WR, **+$21.45** (different code/config era; in-sample comparison only). Live/paper post-deploy: **`pending`** (≥15 closes).
- **Status:** `pending`

### 2026-05-02 — NEUTRAL HTF + ambiguous 4H MACD: momentum then Sabre (no hard cycle skip)

- **What changed:** [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py): Under HTF **NEUTRAL** with up/down markets, when 4H MACD is ambiguous (not clearly rising above zero nor falling below), the strategy now sets `allowed_side` from **15m** impulse (`SPIKE_*` / `DRIFT_*`) when present, otherwise **Sabre** bull/bear lean — replacing the prior **`return []`** path gated by **`neutral_updown_skip_ambiguous_4h`**. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml): removed **`neutral_updown_skip_ambiguous_4h`**; comment notes the retired toggle.
- **Why:** Ops showed BTC up/down could idle whole cycles while SOL-style macros still traded; the ambiguous-4H hard skip amplified **`outside_entry_window`**, **`buy_yes_disabled`**, and window fixes still left NEUTRAL chop as a zero-signal lane.
- **Hypothesis:** Short-horizon tape lean plus Sabre fallback restores parity with “always evaluate when markets exist” without dropping HTF/LTF, edge, Kelly, or entry-window gates downstream.
- **Expected outcome:** Fewer silent BTC cycles on NEUTRAL + flat-transitioning 4H MACD; skip telemetry should shift away from implicit early exits before layer-2 gates.
- **Actual outcome:** `pending` (minimum ~15 closed BTC trades after deploy; journal or `/api/journal/summary` — do not estimate).
- **Status:** `pending`

### 2026-05-02 — Entry-window auto-align cap fix, latency-adjusted mins_left, gate telemetry, BUY_YES re-enabled

- **What changed:** [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py): `_resolve_entry_window_bounds()` auto-align no longer caps `aligned_max` at a fixed 15-minute candle width (YAML `entry_window_15m_max` above 15 was previously ineffective). Up/down entry-window checks use minutes remaining minus `entry_window_latency_buffer_sec`. Samples `ltf_strength` into per-cycle `gate_distributions`. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.bitcoin`: `entry_window_15m_max` **45.0**, `entry_window_15m_min` **2.0**, `entry_window_auto_align_max_expand_min` **2.0**, `entry_window_latency_buffer_sec` **12**, **`disable_buy_yes: false`**, `ai_entry_window_15m_min` **2.0** (aligned with quant timing notes).
- **Why:** Live ops showed BTC cycles dominated by `outside_entry_window` despite YAML “wide” windows, because auto-align applied `min(15, win_max + expansion)` and listings often appear with **minutes-to-resolution above 15** before the quoted candle. `disable_buy_yes` produced `buy_yes_disabled` skips on HTF-bullish LONG paths.
- **Hypothesis:** Honoring YAML max plus a small latency shave restores eligibility for early-listed contracts without bypassing edge, entry-price band, HTF/LTF, or Kelly gates; BUY_YES symmetry returns under bullish bias while poor historical BUY_YES performance remains journal-visible.
- **Expected outcome:** `outside_entry_window` should fall as the dominant BTC skip when markets exist; ops summaries should include `ltf_strength` in gate distributions; BUY_YES may emit signals when bias is bullish.
- **Actual outcome:** `pending` (minimum ~15 closed BTC trades after deploy; use `/api/journal/summary` or session journal export — do not estimate).
- **Status:** `pending`

### 2026-04-29 — Phase 6 backtest/live parity correction

- **What changed:** BTC live threshold/updown tension and the crypto up/down backtest now use signed, direction-aware ±0.02 tension math. The backtest also uses wider empirical/synthetic entry-price sampling, entry-price band filtering, additive cent-based slippage, sampled `entry_price`, live-style full-tier sizing approximation, and skips flat candles instead of counting them as NO.
- **Why:** Phase 6 found the BTC/SOL-family crypto backtest was materially optimistic versus live: narrower prices, cheaper slippage, different sizing, and tension math that did not match live.
- **Hypothesis:** Backtest PnL should become more conservative and more comparable to live paper sessions; trade count should drop where sampled prices fall outside live entry bands.
- **Expected outcome:** Reported BTC 15m results should no longer resemble the pre-parity +$90.70 run; slippage should increase and sizes should cluster near the configured live cap.
- **Actual outcome:** Pre-deploy cached BTC 15m backtest artifact `data/backtest/reports/backtest_crypto_BTC_15m_20260429_154935.json`: 174 trades, 52.3% WR, +$21.45 net PnL, +4.3% return, expectancy +$0.123/trade, $13.05 slippage. In-sample warning applies.
- **Status:** `pending`

### 2026-04-29 — Phase 3 threshold probability smoothing

- **What changed:** Smoothed `bitcoin._estimate_probability()` threshold distance math so probability moves continuously through 50% at the threshold, made Trend Sabre tension direction-aware, and changed threshold `days_to_resolution` arithmetic to timezone-aware UTC in [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py).
- **Why:** Phase 3 scan found a 10 percentage-point probability cliff for tiny threshold crosses, a direction-blind tension penalty that punished the mean-reversion beneficiary, and naive local-vs-UTC datetime math.
- **Hypothesis:** BTC threshold probability estimates should become less jumpy near the strike while stretched-price adjustments now help the side favored by snap-back instead of penalizing both sides.
- **Expected outcome:** Cleaner threshold-market edge estimates and fewer near-boundary false positives; 15m up/down behavior should remain broadly stable because it does not use the traditional threshold probability branch.
- **Actual outcome:** `pending` for live validation (need ≥15 closed BTC trades after deploy). Pre-deploy BTC 15m backtest artifact: 306 trades, 53.3% WR, +$90.70 net PnL, +18.1% return, expectancy +$0.296/trade; in-sample warning applies.
- **Status:** `pending`

### 2026-04-29 — Phase 2 edge and threshold hardening

- **What changed:** Removed no-op positive-edge normalization in [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py) so edge remains the direct probability-vs-market value, and raised BTC threshold parsing ceiling from `$10M` to `$1B` in strategy/dashboard extraction paths.
- **Why:** `edge = abs(edge) if edge > 0 else edge` did not change values and made the intent unclear. The `$10M` ceiling could silently misclassify future high-threshold BTC markets as "no threshold."
- **Hypothesis:** No signal economics should change from removing the no-op edge line; threshold markets above `$10M` should route through the intended threshold probability path instead of the fallback branch.
- **Expected outcome:** BTC diagnostics and future refactors should be less ambiguous; no material trade-count change unless a high-threshold BTC market appears.
- **Actual outcome:** `pending` (need ≥15 closed BTC trades after change; high-threshold market behavior only observable when such markets are live).
- **Status:** `pending`

### 2026-04-29 — Fixed-cycle scheduler + wider BTC up/down entry windows

- **What changed:** [src/main.py](/Users/mainfolder/Documents/psb-main%201/src/main.py) now runs the unified trading loop on a fixed cadence by subtracting cycle runtime from the sleep interval, instead of sleeping the full interval after each cycle. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) widened BTC up/down entry windows to 15m `8.0–15.0` and 5m `0.75–5.0` minutes remaining.
- **Why:** Live ops showed BTC markets arriving but repeatedly skipping as `outside_entry_window`. The old scheduler made a configured 120s cadence behave like cycle-runtime + 120s, causing phase drift across narrow windows.
- **Hypothesis:** Fixed cadence plus wider timing eligibility should let BTC evaluate valid up/down markets without loosening edge, price-band, Kelly, or risk gates.
- **Expected outcome:** BTC `outside_entry_window` should drop as the dominant skip reason; if signals still do not fire, remaining blockers should surface as `edge_below_min`, price band, AI window, or HTF/LTF gates.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed BTC trades after deploy).
- **Status:** `pending`

### 2026-04-27 — BTC AI timing window for marginal up/down calls

- **What changed:** Added an explicit AI decision window in [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py) for marginal up/down AI assists. The `strategies.bitcoin` block in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) now exposes `ai_entry_window_15m_min/max` and `ai_entry_window_5m_min/max`, interpreted as minutes remaining until resolution.
- **Why:** BTC already used AI for marginal up/down tie-breaks, but it could do so too early in the candle before enough directional information had formed.
- **Hypothesis:** Constraining those AI calls to the later decision band should improve the quality of AI-assisted marginal entries without changing the strong-edge or non-AI paths.
- **Expected outcome:** Fewer low-information BTC AI calls near candle open; AI assists, when they happen, should be based on more useful intra-window data.
- **Actual outcome:** `pending` (need ≥15 closed BTC trades after deploy).
- **Status:** `pending`

### 2026-04-26 — Pre-restart BTC up/down safety caps

- **What changed:** `strategies.bitcoin.max_edge_updown` tightened **0.15 → 0.12** and `strategies.bitcoin.disable_buy_yes` flipped **false → true** in `config/settings.yaml`.
- **Why:** April 26 audit found the live code already documents `edge >0.12` as an inflated/late-entry risk, while config still allowed entries up to 0.15. BTC BUY_YES also remained enabled despite prior 6-trade evidence at 33% WR / negative PnL.
- **Hypothesis:** Capping the high-edge bucket and pausing BUY_YES should prevent the two riskiest BTC up/down lanes from dragging the next paper session while the anti-LTF/high-edge mechanism is investigated.
- **Expected outcome:** Fewer late high-edge BTC entries, no BTC BUY_YES exposure, and cleaner out-of-sample evidence from SELL_YES-only BTC trades below the 0.12 cap.
- **Actual outcome:** `pending` (need ≥15 closed BTC trades after restart, plus separate review before re-enabling BUY_YES).
- **Status:** `pending`

### 2026-04-21 — UTC blocklist scope-back to Tier A + re-audit cadence

- **What changed:** `strategies.bitcoin.blocked_utc_hours_updown` narrowed from `[0, 1, 2, 3, 9, 15, 18, 22]` to **`[0, 1, 2]`** in `config/settings.yaml`. H3 / H9 / H15 / H18 / H22 removed from the block (downgraded to "watch"). This is the **first Change Log entry for this blocklist** — prior expansions predate AGENTS.md rule #4 and were not journaled here.
- **Why:** Evidence audit (see `.cursor/plans/block-list-evidence-audit_f364fc11.plan.md`) found that only the extreme backtest losers cleared the project's own `MIN_TRADES=5` / `BAD_WR_THRESHOLD=0.46` / `BAD_EV_THRESHOLD=-$2` bar with high confidence. Backtest was 540 trades / Mar 2026 ≈ 22 trades/hour. H1 (26.9% WR, -$61.68) and H2 (27.3% WR, -$50.28) are statistically robust; H3/H9/H15/H18 sit at 41–44% WR where the 95% CI at n≈22 overlaps 50%; H22 had no sample size cited. The wider list also contributed to zero-trade cycles (live BTC signals dropped at H2/H3 UTC on 2026-04-21).
- **Hypothesis:** A narrower Tier-A block retains the highest-EV protection while letting trades flow in hours that are "bad-leaning but not confirmed bad," so live data can earn the re-block.
- **Expected outcome:** More closed trades per day (~5 previously blocked hours unblocked); live heatmap (`scripts/hourly_heatmap.py`) accumulates enough per-hour samples within ~2 weeks to re-validate or re-promote Tier-B hours on evidence rather than backtest alone.
- **Actual outcome:** `pending` (need ≥15 closed trades in each previously-blocked hour post-deploy).
- **Re-audit cadence:** Weekly `python scripts/hourly_heatmap.py --days 14 --suggest` once live trades resume; re-promote a Tier-B hour to Tier A only if it meets **≥15 trades AND WR < 0.46 AND avg PnL < -$2** in the live window.
- **Status:** `pending`

### 2026-04-11 — Entry window auto-alignment (scan cadence)

- **What changed:** `BitcoinStrategy._resolve_entry_window_bounds()` widens `entry_window_*_min/max` slightly when `entry_window_auto_align: true`, using `entry_window_align_scan_interval_sec` (default 300), `entry_window_auto_align_max_expand_min`, and `entry_window_auto_align_jitter_sec`. Wired in `src/strategies/bitcoin.py`; flags added under `strategies.bitcoin` in `config/settings.yaml`.
- **Why:** Main loop scans every ~5m; narrow “minutes until resolution” bands caused repeated `outside_entry_window` skips even when edge/HTF/LTF would otherwise allow evaluation.
- **Hypothesis:** A small, bounded expansion keeps “early candle” intent while letting at least one scan per interval intersect the valid band (plus clock/request jitter).
- **Expected outcome:** Fewer spurious `outside_entry_window` skips; BTC up/down gets a fair shot at signal generation without materially trading late-window noise (still capped by existing price/edge gates).
- **Actual outcome:** `pending` (need ≥15 closed trades after deploy, or clear before/after ops skip-reason mix).
- **Status:** `pending`

## Review sessions

### 2026-05-04 — Paper `test_20260504_034719` (journal parse)

- **Headline:** BTC was the **only** negative strategy in this long local paper run; losses concentrated in **BUY_NO** and **counter-trend** (`counter_trend=btc_4h_hist_declining` in entry `signal_reason`).
- **Measured (parse of `entries.jsonl`, ENTRY+EXIT join):** `bitcoin` **48** closes, **54.2%** WR, **-$21.42**; `BUY_YES` 38 / **-$5.20**; `BUY_NO` 10 / **-$16.23** (30% WR); counter-trend tag **9** closes, **2** wins, **-$17.03**.
- **Session exit path (all strategies):** `take_profit` +$132.95 (75); **`updown_time_stop` -$105.12** (35) — time-stop bucket is the main structural bleed alongside resolution tails.
- **Pacific hour at entry (`hour_pt`, all strategies combined in this session — not BTC-only):** weakest combined buckets **8** (-$13.68, 14 trades) and **13** (-$11.00, 4); strongest **10** (+$26.98, 22). Use for narrative only; for BTC-only hours use a filtered script later.
- **Post-fix tracking:** `disable_buy_no_counter_trend: true`, `min_edge_buy_no: 0.11` (2026-05-04 commit) — **actual outcome `pending`** (≥15 BTC closes after change).
- **Artifacts:** [`docs/session_reports/session_parse_test_20260504_034719.json`](docs/session_reports/session_parse_test_20260504_034719.json); rolling heatmap (**Pacific exit hour**, 30d, `--suggest`) [`docs/session_reports/hourly_heatmap_20260504_exit_pt.txt`](docs/session_reports/hourly_heatmap_20260504_exit_pt.txt). **Tooling:** `scripts/hourly_heatmap.py` YAML block overlay applies only to `bitcoin` / `sol_macro` / `eth_macro`; **xrp_macro** / **hype_macro** rows in that file have no blocked-hour status from config.

### 2026-04-26 — Paper session observation: no new BTC closes

- **What we observed:** No new BTC up/down closed trades in this session window.
- **Interpretation:** Could be genuine BTC market scarcity in the scanned windows and/or current BTC gating (`disable_buy_yes`, edge/window filters) being tight for this regime.
- **Action:** Keep current guardrails, re-check after more session data accumulates (target ≥15 BTC closes) before changing BTC entry gates again.

## Lessons learned

_(none yet — add only after data)_
