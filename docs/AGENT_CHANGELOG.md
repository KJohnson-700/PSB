# Agent changelog (backfill)

**Purpose:** Record **what shipped** when work was done in **Claude Code, Codex, Cursor**, or similar **without** a matching entry in the Obsidian strategy log or a written operator handoff. **Git remains the source of truth**; this file is a readable index.

**Strategy tuning and hypothesis tracking** still belong in `projects/polymarket-bot/strategy-log/` per `AGENTS.md`. This doc covers **codebase / infra / dashboard** provenance only.

**Canonical repo for this bot:** `https://github.com/KJohnson-700/PSB` (see `AGENTS.md` — do not confuse with other GitHub projects).

---

## 2026-05-17 — Alts: 4H MACD histogram-slope override (additive, default-off)

Ported BTC's BUY_NO-specific counter-trend mechanic (`bitcoin.py:1233-1243` — `disable_buy_no_counter_trend` gate firing on bullish HTF + 4H MACD histogram declining) onto the alt four-path resolver as an **additive** firing path. Symmetric mirror on the LONG side for the BEAR→LONG exception. Uses **alt-native** 4H MACD (per the "alts are not decided by BTC" rule), not BTC's.

**`src/analysis/sol_btc_service.py`:** Added `macd_4h` field to `SOLAnalysis`, fetched native 4H klines for the alt symbol in `calc_sol_indicators()`, computed via the inherited `calc_macd()` helper.

**`src/strategies/sol_macro.py`:** Added two new config flags — `buy_no_4h_hist_override_enabled` and `buy_yes_4h_hist_override_enabled` (both default `false`). Extended `_bearish_dip_ltf_ok` and `_bullish_rally_ltf_ok` with an OR-combined 4H-hist firing path: when the flag is on, alt 4H hist declining (BUY_NO) or rising (BUY_YES) fires the override even if the existing 5m/15m + RSI + BTC-5m gate doesn't confirm. `resolver_active` outer gate updated to include the new flags.

**`src/strategies/eth_macro.py`:** Mirrored `resolver_active` gate; helper overrides inherited from `SolMacroStrategy`.

**`src/backtest/updown_engine.py`:** Replay-path mirror via new `_alt_macd_4h_from_1h(df_1h)` helper (resamples alt 1H to 4H since the replay engine doesn't fetch native 4H for alts). Extended `_buy_no_ltf_override_replay`, `_bullish_rally_ltf_override_replay`, `_resolve_side_with_ltf_overrides_replay` and both call sites to thread `df_1h` and respect the new flags.

**`config/settings.yaml`:** Added the two flags to all four macro blocks (`sol_macro`, `eth_macro`, `hype_macro`, `xrp_macro`) defaulting to `false` — opt-in per asset after backtest evidence.

**`tests/test_sol_macro.py`:** 6 new tests covering: flag-off path (does not fire), flag-on with declining/rising 4H hist (fires via 4H path), flag-on with wrong-direction slope (does not fire), end-to-end resolver BULL→SHORT and BEAR→LONG via 4H path.

**Verification:** `pytest tests/test_sol_macro.py tests/test_eth_macro.py tests/test_sol_macro_skip_accounting.py tests/test_updown_backtest_parity.py` → 157 passed (141 prior + 6 new + 10 from existing additions). Empirical SOL 15m backtest comparison blocked by a pre-existing `float(timedelta).total_seconds()` bug at `updown_engine.py:1188` (present on HEAD, unrelated to this change — flagged for separate fix).

**Why default-off:** No tightening; per "don't tweak winners," ship dark and enable per-asset only after evidence the additional firing path is net-positive. Plan: `~/.claude/plans/in-your-opinion-should-parsed-snowflake.md`.

---

## 2026-05-17 — Dashboard: BUY_YES vs BUY_NO session compare (Performance tab)

**`src/dashboard/server.py`:** `GET /api/journal/action_breakdown` — session closed trades split by `BUY_YES` / `BUY_NO` (flat-aware WR, net/avg PnL, `slipping` when both sides have 3+ trades).

**`src/dashboard/index.html`:** Replaced unused Strategy Performance signal metric boxes with **per-strategy lanes** (BTC/SOL/ETH/HYPE/XRP/WX), each showing Buy YES vs Buy NO WR, W/L, and net PnL; session slip banner when both sides have 3+ closes. `dashboard_ui_rev`: `2026-05-17-action-perf-strategy-lanes`.

**Tests:** `tests/test_dashboard_action_breakdown.py`; bundle guard for `action-perf-grid` and no `BTC Signals` in `#strategy-boxes`.

---

## 2026-05-17 — Handoff: BUY_YES recovery + dual-direction rally logic (plan only, not implemented)

**`docs/HANDOFF_BUY_YES_RALLY_MERGE.md`:** Operator handoff for next agent — targeted fixes (no blind revert to pre–May 9), restore BUY_YES path quality from May 3–8 paper baseline, contain `buy_no_ltf` clash, add symmetric LTF momentum on **both** LONG and SHORT paths in bull and bear. Plan: `.cursor/plans/buy_yes_rally_merge_4b6efcaa.plan.md`.

**Status:** documentation only; implementation pending explicit execute.

---

## 2026-05-17 — Backtest/live sync: replay threshold source, report assumptions, paper-vs-backtest diff tool

**`src/backtest/updown_engine.py`:** Removed another backtest/live drift seam by making replay min-edge thresholds prefer the live strategy keys (`strategies.*.min_edge`, `min_edge_5m`) unless an explicit `backtest.min_edge_*` override is set. Replay results now also carry structured `replay_assumptions` so reports state where the threshold came from, whether entry prices came from empirical live fills vs fallback sampling, whether Polymarket 1m marks were enabled, and which execution details are still not modeled in replay.

**`scripts/run_backtest_crypto.py`:** Crypto report JSON now persists `replay_assumptions` at both top level and split-test level so dashboard / audit consumers can see the exact replay contract instead of assuming it.

**`scripts/compare_paper_to_backtest.py`:** Added a lightweight operator audit tool that compares one paper journal session slice against one backtest report for the same `strategy` / `window_size`, surfacing drift in trade count, WR, PnL, expectancy, action mix, exit reasons, and entry-price cohorts. This is an aggregate parity check, not a full candidate-universe replay.

**Tests:** Extended `tests/test_crypto_backtest_eth.py` for replay-assumption persistence and live-key min-edge resolution; added `tests/test_compare_paper_to_backtest.py` for split-report loading and journal filtering.

**Verification target:** `.venv/bin/python -m pytest tests/test_crypto_backtest_eth.py tests/test_compare_paper_to_backtest.py tests/test_updown_backtest_parity.py -q`.

---

## 2026-05-17 — BTC 5m live/backtest single source of truth (`btc_updown_5m`)

**`src/strategies/btc_updown_5m.py`:** Shared quant module (hist gate, HTF boost, m5 scoring, prediction window, RSI gates, edge vs **live `yes_price`**). **`bitcoin.py`** 5m path and **`UpdownBacktestEngine._edge_5m_btc`** both call **`compute_btc_5m_quant`** — not a backtest revert.

**`src/backtest/updown_engine.py`:** BTC 5m keeps **in-band eval YES** (`1c39eda`); m5 direction stays first-90s of candle (live); prediction-window bonus uses **candle age at `eval_minutes_left`**.

**`tests/test_updown_backtest_parity.py`:** Asserts engine edge == shared module; eval-age prediction window; eval vs open YES.

**Verification:** `.venv/bin/python -m pytest tests/test_updown_backtest_parity.py tests/test_bitcoin.py -q`.

---

## 2026-05-17 — Symmetric LTF momentum gates for BUY_YES and BUY_NO (handoff `HANDOFF_BUY_YES_RALLY_MERGE.md`)

**`src/strategies/sol_macro.py`, `src/strategies/eth_macro.py`, `src/backtest/updown_engine.py`, `config/settings.yaml`, `tests/test_sol_macro.py`, `tests/test_eth_macro.py`:** Added the four-path side resolver described in the handoff. Both LONG and SHORT admissions now require their own short-window momentum confirmation in both BULL and BEAR regimes, rather than only the SHORT side requiring it in BULL. Fixes the post-May-9 regression where `buy_no_ltf_override` could flip a BULL macro to SHORT and clash with otherwise-valid BUY_YES rally tape.

**Resolver paths** (in `_resolve_allowed_side_with_ltf_overrides`):
- `bullish_rally_default` — BULL + bullish 15m+5m + RSI ≥ 55 + BTC 5m ≥ floor
- `bullish_rally_exception` — BEAR + same bullish confirmation
- `bearish_dip_default` — BEAR + bearish 15m+5m + RSI ≤ 45 + BTC 5m ≤ cap
- `bearish_dip_exception` — BULL + same bearish confirmation **only when bullish rally does not also confirm** (clash rule)
- Skip with `ltf_resolver_skip` when neither momentum confirms in either regime.

**Scope:** sol_macro implements the canonical resolver; eth_macro ports the wiring into its duplicated scan loop; xrp_macro and hype_macro inherit cleanly via `SolMacroStrategy` and `super().scan_and_analyze()`. updown_engine replay mirrors the resolver for backtest/live parity.

**Config:** Added `buy_yes_ltf_override_{enabled,rsi_min,min_btc_5m_pct}` to all four macro yaml blocks (sol/eth/hype/xrp), enabled by default. Existing `buy_no_ltf_override_*` keys are unchanged.

**Tests:** `tests/test_sol_macro.py` +8 cases covering all four resolver paths, clash rule, and the bullish helper; `tests/test_eth_macro.py` +2 cases for the ETH duplicated-scan wiring. `_buy_no_ltf_override` kept as a back-compat alias for the new `_bearish_dip_ltf_ok` helper.

**Verification:** `pytest tests/test_sol_macro.py tests/test_eth_macro.py tests/test_sol_macro_skip_accounting.py tests/test_updown_backtest_parity.py` → 141 passed. SOL 15m backtest 2026-01-20 → 2026-04-20 with the replay resolver active tightened trade count 63 → 36 (WR 27% → 28%) — symmetric gate filters ambiguous tape on both sides as designed.

**Out of scope:** `bitcoin.py` rally logic (handoff carve-out); paper-vs-backtest comparison (separate skill).

**Commits:** `0c91999` (sol_macro), `ca4ec48` (eth_macro), `5e4f018` (updown_engine).

---

## 2026-05-16 — Naming cleanup for rejected-candidate tracking + lane calibration rollout note

**`src/analysis/ghost_calibration.py`, `src/analysis/rejected_candidate_log.py`, `src/main.py`, `src/ops_pulse.py`:** Renamed user-facing/runtime wording from ambiguous **ghost calibration / ghost mode** language to **rejected-candidate tracker** wherever the feature is describing blocked-trade logging and settlement. `OPS_JSON` now exposes a new `rejected_candidate_tracker` block while preserving `ghost_calibration` as a backward-compatible alias for existing consumers.

**`config/settings.yaml`:** Clarified `lane_calibration.shadow_mode` as **observation-only** mode in comments. Current behavior: posteriors update on close, but entry probabilities remain unchanged until `shadow_mode` is turned off.

**`docs/AGENT_CHANGELOG.md`:** Corrected the stale note that said lane-calibration entry wiring was not shipped. The `LaneCalibrator.calibrate(...)` entry call sites already exist in `bitcoin`, `eth_macro`, and `sol_macro` (which also covers XRP/HYPE via inheritance).

**Operational recommendation:** Do **not** flip lane calibration live globally yet. The code path is ready, but current per-lane posterior samples are still uneven and several effective alphas are clamp-driven or derived from thin cohorts, so a global `shadow_mode: false` change would be statistically premature without a lane-level minimum-sample gate or a narrower staged rollout.

**Verification:** `.venv/bin/python -m pytest tests/test_ghost_calibration.py tests/test_ops_pulse.py tests/test_lane_calibration.py tests/test_bitcoin.py tests/test_sol_macro.py -q` passed (`129 passed`).

---

## 2026-05-16 — Backtest vs live sync: 30m entry timing, skip_counts, drift keys

**`src/backtest/updown_engine.py`:** Replay now reads entry-eval delay from `strategies.*` (not orphaned root keys), evaluates `mins_left` inside the lane entry band (not only at window open), and uses YES mid at eval time. Impossible bands (e.g. min > window length) fall back to open eval so skips count as `outside_entry_window`. Fixes BTC 30m backtests that showed **0 / 4368** entries while live paper traded inside the 25–29m band.

**`scripts/run_backtest_crypto.py`, `src/dashboard/index.html`:** Persist `skip_counts` and `strategy_base` on crypto reports; dashboard shows dominant skip reasons when `windows_entered === 0`.

**`src/execution/backtest_expectations.py`, `src/execution/live_testing.py`, `src/analysis/journal_learning.py`:** Normalize `bitcoin_30m` report keys to live journal `bitcoin` + `window_size` for drift and learning comparisons.

**Tests:** `tests/test_backtest_expectations.py` (new); extended `tests/test_updown_backtest_parity.py` (30m lane band regression).

**Verification:** `.venv/bin/python -m pytest tests/test_updown_backtest_parity.py tests/test_backtest_expectations.py tests/test_performance_feedback.py -q`; BTC 30m `2026-01-20`–`2026-04-20` → **85 trades** (was 0).

---

## 2026-05-16 — Rejected-candidate tracker loop closed in runtime + ops visibility

**`src/analysis/ghost_calibration.py`:** Added a dedicated rejected-candidate settlement/summary module. It auto-settles `data/calibration/rejected_candidates.jsonl` against Gamma market outcomes into `data/calibration/rejected_candidates_settled.jsonl`, keeps the flow idempotent via stable `ghost_id`, and builds a compact status payload (`total_rejected`, `total_settled`, `unresolved`, win/loss counts, top reason/action buckets) for runtime observability.

**`src/main.py`:** `PolyBot` now refreshes rejected-candidate tracker state at startup and once per trading cycle after resolution work. That closes the operational loop that previously existed only as an offline script: rejected candidates are now automatically revisited and the latest summary is cached on the bot instance instead of relying on a manual `tools/settle_rejected_candidates.py` run.

**`src/ops_pulse.py`:** `OPS_JSON` / `/api/ops/summary` snapshots now include a rejected-candidate tracker block so operators can verify whether blocked-trade outcomes are actually being ingested, how much is still unresolved, and whether the settled cohort is favorable or not.

**Tests:** Added `tests/test_ghost_calibration.py` for settlement idempotence / summary behavior and extended `tests/test_ops_pulse.py` to assert the new ops payload surface.

**Verification:** `.venv/bin/python -m pytest tests/test_ghost_calibration.py tests/test_ops_pulse.py -q` passed, and `.venv/bin/python -m py_compile src/analysis/ghost_calibration.py src/main.py src/ops_pulse.py` passed before ship.

**Not committed (runtime data):** `data/entry_prices/updown_fills.jsonl`, `data/calibration/`, `data/lane_state_audit.jsonl`.

---

## 2026-05-16 — Lane-specific entry policy for BTC/SOL/ETH/HYPE/XRP macro lanes

**`src/analysis/lane_entry_policy.py`:** Added a shared lane-entry resolver parallel to the existing exit resolver. Entry policy now resolves by **`strategy + window + side`** with precedence **global defaults → strategy entry policy → strategy `window_side_overrides`**, and returns typed fields for `enabled`, `min_edge`, `hard_min_edge`, `ai_override_min_edge`, `entry_price_min`, `entry_price_max`, `entry_window_min`, `entry_window_max`, and `size_multiplier`.

**`src/strategies/bitcoin.py`, `src/strategies/sol_macro.py`, `src/strategies/eth_macro.py`:** Replaced scattered strategy-specific entry threshold reads with the shared lane policy path. Entry admission now uses the resolved lane for both paper and live decisions, with explicit skip reasons for `lane_disabled`, `lane_min_edge`, `lane_entry_window`, `lane_price_band`, and `lane_size_too_small`. `eth_macro`, `hype_macro`, and `xrp_macro` inherit the shared SOL-family lane policy flow.

**`src/main.py`:** Journal/open-position extras now persist the resolved `entry_policy` metadata used at admission so lane-level post-trade audits can see the exact thresholds and multipliers applied. This file also retains the same config-application path for live strategy instances; no separate paper-only admission logic was introduced.

**`config/settings.yaml`:** Added explicit `entry_policy` blocks for **`bitcoin`**, **`sol_macro`**, **`eth_macro`**, **`hype_macro`**, and **`xrp_macro`**, including `window_side_overrides` for `5m` / `15m` / `30m` where those lanes trade. Legacy strategy-level keys remain readable as compatibility fallback, but new lane-policy keys take precedence.

**Tests:** Added `tests/test_lane_entry_policy.py` for resolver precedence and fallback coverage, updated execution-driver and SOL skip-accounting coverage for lane skip reasons / journal metadata, and kept existing live-config / exit-policy regression coverage green.

**Verification:** `.venv/bin/python -m pytest tests/test_lane_entry_policy.py tests/test_strategy_execution_drivers.py tests/test_live_config_apply.py tests/test_updown_exit_shared.py tests/test_eth_macro.py tests/test_sol_macro.py tests/test_updown_backtest_parity.py tests/test_sol_macro_skip_accounting.py -q` and `.venv/bin/python -m pytest tests/test_bitcoin.py tests/test_bitcoin_scenarios.py tests/test_strategy_enabled_defaults.py -q` both passed before ship.

**Not committed (runtime data):** `data/entry_prices/updown_fills.jsonl`, `data/calibration/`, `data/lane_state_audit.jsonl`.

---

## 2026-05-15 — Lane calibration (shadow), calibration log, BTC HTF vote diagnostics

**`src/analysis/calibration_log.py`, `src/analysis/lane_calibration.py`, `tools/calibration_report.py`:** Phase 0 append-only calibration trade log (`data/calibration/trades.jsonl`) and Phase 6 per-lane EWMA/Beta posteriors (`data/calibration/lane_posteriors.json`). **`lane_calibration.shadow_mode: true`** in config — posteriors update on close but **`calibrate()`** does not change live entries while observation-only mode stays on.

**`src/main.py`:** Builds **`LaneCalibrator`** at startup; on each live exit appends calibration rows and records posterior snapshots; resolution settlements also call **`kelly_sizer.record_outcome`** with detected window.

**`src/strategies/sol_macro.py`, `src/strategies/eth_macro.py`:** **`_get_btc_htf_bias_details()`** exposes sabre / price-vs-MA / MACD votes, raw vs final bias, and 4H histogram conviction in logs, entry reasons, and indicator snapshots.

**`config/settings.yaml`:** **`lane_calibration`** block (`enabled`, `shadow_mode`).

**Tests:** `test_calibration_log.py`, `test_lane_calibration.py`, `test_dashboard_updown_breakdown.py`, dashboard bundle overlay tests, live config Kelly resolution test.

**Not committed (runtime data):** `data/entry_prices/updown_fills.jsonl`, `data/lane_state_audit.jsonl`.

**Next step — decide whether to flip observation-only mode live.** The entry-side `LaneCalibrator.calibrate(...)` wiring now exists in `bitcoin`, `eth_macro`, and `sol_macro` (which also covers XRP/HYPE via inheritance). The remaining decision is operational: use paper-session evidence from `data/calibration/lane_posteriors.json` and `python tools/calibration_report.py` to decide whether `lane_calibration.shadow_mode: false` is justified.

---

## 2026-05-15 — Dashboard: neon strategy palette + BTC live chart saturation

**`src/dashboard/index.html`:** Replaced pastel strategy hues with operator neon palette on **`window.STRATS`** (single source for pills, journal, scan rows, metric boxes, canvas charts): **bitcoin** `#1f8bff`, **sol_lag / sol_macro** `#b14dff`, **eth_lag / eth_macro** `#00e5ff`, **hype_lag / hype_macro** `#ff6a00`, **xrp_lag / xrp_macro** `#ffe000`. **BTC trade overlay** (`_BTC_TRADE_OVERLAY_PALETTE`) now uses the same saturated fills with stronger glow alphas (0.78) instead of washed `#ffca5a` / `#8fb0ff` pastels. **MACD histogram** weak bars use **`#00897b` / `#c62828`** instead of **`#b2dfdb` / `#ffcdd2`**. Up/down symbol color fallback routes through **`stratHex`** macro keys.

**`src/dashboard/server.py`:** **`dashboard_ui_rev`** → **`2026-05-15-strategy-neon-chart-palette`**.

**Vault:** `Hermes Second Brain/projects/psb/notes/2026-05-15-strategy-neon-chart-palette.md`.

---

## 2026-05-14 — Up/down exits: lane + window overrides for ETH/SOL/HYPE/XRP bearish lanes

**`src/execution/updown_exit_shared.py`:** Added lane-aware/window-aware up/down exit resolution with explicit precedence: strategy+window+lane → strategy+lane → global lane → existing globals. Shared helpers now classify `up` vs `down` exposure (`BUY_NO` and legacy short-YES map to `down`) and infer `5m` / `15m` / `30m` from stored `window_size` or exact entry runway for legacy open positions.

**`src/execution/live_testing.py`:** Live exit checks now resolve adverse `% stop`, late-window cents stop, high-entry tightening, and in-profit tightening through the shared lane/window resolver while preserving exit order (`take_profit` → `updown_stop_loss` → `updown_time_stop` → expiry/time-limit).

**`src/backtest/updown_engine.py`:** Backtest live-exit proxy now uses the same lane/window-aware resolver as live so ETH/SOL bearish replay stays aligned with production semantics.

**`src/execution/clob_client.py`, `src/main.py`, `src/execution/trade_journal.py`:** `Position` now persists `window_size`; entry execution writes it onto live positions and journal open-position state, and restart sync restores it for exit evaluation.

**`config/settings.yaml`:** Added initial ETH/SOL/HYPE/XRP `window_lane_overrides` for bearish (`down`) `5m` and `15m` up/down exits. ETH keeps its existing base per-strategy override and adds tighter bearish thresholds; SOL/HYPE/XRP add bearish window-specific overrides without changing bullish behavior. BTC was intentionally left on shared defaults because the shipped lane/window plumbing does not change BTC behavior unless explicit overrides are added.

**Tests:** Added shared exit precedence/lane tests, live ETH/SOL bearish stop/time-stop regressions, journal `window_size` persistence coverage, backtest parity coverage for `BUY_NO` down-lane overrides, and dashboard config round-trip coverage for nested `window_lane_overrides`.

**Deferred until more test data:** No lane-specific `take_profit_pct`, no BTC bearish override block yet, no side-specific exit ordering changes, and no lane-specific global defaults beyond per-strategy tuning. Those remain candidates only if the next lane-level paper data still shows `updown_stop_loss` / `updown_time_stop` concentration after this pass.

---

## 2026-05-14 — BTC admission loosen: wider windows + lower BUY_NO extra edge floor

**`config/settings.yaml`:** BTC-only admission loosening after local paper session `test_20260514_000030` showed `cumulative_signal_counts.bitcoin = 0` with skip mix dominated by `outside_entry_window`, plus a few `price_too_far_from_50_50` and short histogram rejects. Widened `entry_window_15m_max` **16.0 → 19.0**, `entry_window_5m_max` **5.0 → 5.5**, `entry_timing_window_15m_max` **15.0 → 18.0**, `entry_timing_window_5m_max` **5.0 → 5.5**, increased `entry_window_auto_align_max_expand_min` **3.0 → 4.0**, and lowered BTC `min_edge_buy_no` **0.11 → 0.09**.

**Why this scope only:** The current BTC starvation evidence was admission-side, not exit-side. No BTC exit overrides were added here, and no short histogram gate code was changed yet.

**Deferred until more test data:** If BTC still fails to participate after the widened windows, the next candidate tweak is code-side relaxation of the bearish histogram rejects (`hist_gate_15m_short_reject`, `hist_gate_5m_short_reject`) or a configurable bypass, not more exit changes.

---

## 2026-05-13 — Dashboard: backtest HUD heat ramp, marquee brand colors, stronger card/button glow

**`src/dashboard/index.html`:** Backtest HUD uses multi-accent frame (red / purple / cyan / green) instead of cyan-only; live run title uses gradient clip; **% display** `clamp(3.25rem, 12vw, 5.5rem)` with determinate **red→orange→green** via `_btHeatFromPct` + `_btApplyDeterminateHeat` on the number, fill bar, and track border; indeterminate bar uses full-spectrum gradient with **slide + hue-shift** animations; spawn/wait uses `bt-hud-pct-wait` cycle. Cards, metric boxes, hero tiles, and buttons get **stronger default + hover glow** (purple/cyan/green mix). Backtest tab notice uses purple/green tint.

**`src/dashboard/server.py`:** `dashboard_ui_rev` → `2026-05-13-bt-hud-heat-glow-v2`.

---

## 2026-05-13 — Crypto up/down backtest: edge vs market YES (parity with live)

**`src/backtest/updown_engine.py`:** Per-window Polymarket YES series (or OHLCV proxy) is resolved once as `yes_mid_market` and passed into `_edge_15m`, `_edge_15m_sol`, `_edge_15m_eth_follow`, `_edge_5m` / `_edge_5m_btc` / `_edge_5m_sol`, and `_edge_5m_eth_follow_from_df`. Edge is `est_prob_up - yes_price` for LONG (BUY_YES) and `yes_price - est_prob_up` for SHORT (BUY_NO), matching live `bitcoin` / `sol_macro` / `eth_macro` semantics so `max_edge_updown` / `edge_above_cap` and downstream sizing align with observed YES mids. Added `_yes_mid_at_window_open`; reuse loaded `pm_yes` for exit replay (no second fetch).

**Verification:** `.venv/bin/python -m pytest tests/test_updown_backtest_parity.py tests/test_backtest_oracle_replay.py -q` → **34 passed**.

---

## 2026-05-13 — Dashboard: 1.5× type ramp (fullscreen + Commands)

**`src/dashboard/index.html`:** Fullscreen `h2`/`h3` at **27px / 30px** (1.5× over compact 18px / 20px). `.cmd-ref h3` **27px**; fullscreen hero + metric clamps and `#view-commands .cmd-ref-lead` use **1.5vw** fluid step (replacing ~2.2–1.9vw).

**`src/dashboard/server.py`:** `dashboard_ui_rev` → `2026-05-13-dashboard-type-ramp-1p5x`.

**Verification:** `.venv/bin/python -m pytest tests/test_dashboard_bundle.py::test_dashboard_index_serves_and_health_has_ui_rev -q` → **passed**.

---

## 2026-05-13 — Dashboard: Commands tab type scale correction

**`src/dashboard/index.html`:** Reverted blind 2× on Commands hero `h2`; added `.cmd-ref-lead` with `clamp(1.05rem,2.2vw,1.38rem)` and set `#view-commands h3` to compact `11px` so the reference doc tab stays dense. **`src/dashboard/server.py`:** `dashboard_ui_rev` bump.

## 2026-05-13 — Dashboard: 2× font size for cyan card titles (h2/h3)

**`src/dashboard/index.html`:** Doubled `.sh`/`h2`, `h3`, cfg/commands overrides, fullscreen overrides, collapsible `[+]` affordance, and inline Commands / Session AI review headings. Slightly tightened letter-spacing on the smallest uppercase titles so doubled size stays readable.

## 2026-05-13 — Active Positions marquees: no DOM reset on unchanged data, pixel shift, deck glow

**`src/dashboard/index.html`:** Ops metric deck + ops digest ticker skip rebuilding when status text unchanged; `translate3d` + per-element CSS vars for loop distance and duration; debounced `resize` remeasure; metric deck chips get slightly stronger cyan/white box-shadow.

## 2026-05-14 — Backtest tab: mission HUD + server-side crypto progress parse

**`src/dashboard/backtest_progress_parse.py`:** Parses last `progress x/y windows (z%)` line from captured subprocess stdout.

**`src/dashboard/server.py`:** `GET /api/backtest/status` job objects include `progress_pct`, `progress_current`, `progress_total`; bumped `dashboard_ui_rev`.

**`src/dashboard/index.html`:** `#bt-hud` panel (tokens, scan, determinate/indeterminate bar); `updateBacktestHud` / `Standby` / `Starting`; wired into `pollBacktestJobUntilDone`, `startCryptoBacktest` (fixed missing `{ symbol, window }` request body), and `resumePendingBacktestTracking`.

**`tests/test_backtest_progress_parse.py`**, **`tests/test_dashboard_bundle.py`:** Parser golden cases; bundle asserts `id="bt-hud"` on `/`.

**Verification:** `.venv/bin/python -m pytest tests/test_backtest_progress_parse.py tests/test_dashboard_bundle.py -q` → **27 passed**.

## 2026-05-13 — Terminal PolyBot startup/shutdown ASCII banners

**`src/terminal_banners.py`:** New helpers `framed_lines`, `resolve_dashboard_display_url`, `print_startup_banner`, `print_shutdown_banner` (ASCII box, dashboard URL aligned with `start_dashboard` rules).

**`src/main.py`:** Prints startup banner after `set_api_keys`; records `bot._terminal_shutdown_sig` in SIGINT/SIGTERM handler and after dashboard-only interrupt; prints shutdown banner on stderr after `bot.shutdown()` in the main `finally` and on the dashboard-only return path.

**`start.py`:** Removed duplicate pre-`main()` box so the canonical banner prints once from `main()`.

**`tests/test_terminal_banners.py`:** Width and URL resolution regressions.

**Verification:** `.venv/bin/python -m pytest tests/test_terminal_banners.py tests/test_dashboard_bundle.py -q` → **30 passed**.

## 2026-05-13 — Dashboard: ORACLE shutdown splash for Stop, richer start splash, Commands shutdown blurb

**`src/dashboard/index.html`:** `DashPrivacy` gains `mode-shutdown` (amber lane, `bootShutdownLog`, dismiss button + Escape), optional `show(mode, { banner, detail })`, longer `bootLog` for load paths, larger loading/shutdown title + phase detail line. Command Center **Stop** / **Start Live Test** show the overlay when motion is allowed; tab transitions pass explicit banner/detail. Commands tab: larger **Start commands** heading + intro paragraph; new **Shutdown, cancel, and stop** card (no duplicate CLI rows).

**`src/dashboard/server.py`:** Bumped `dashboard_ui_rev`.

**Verification:** `.venv/bin/python -m pytest tests/test_dashboard_bundle.py -q` → **24 passed**.

## 2026-05-13 — Alt config test: remove BTC 1H regime scaling on non-BTC lanes, restore HYPE 1H alignment

**`config/settings.yaml`:** Disabled `btc_1h_regime_gates.enabled` for `sol_macro`, `eth_macro`, `hype_macro`, and `xrp_macro` so BTC 1H `RANGE/BEAR` no longer tighten `min_edge` or reduce size on those non-BTC lanes during the next dashboard validation pass.

**`config/settings.yaml`:** Restored `hype_macro.enforce_alt_1h_alignment: true` so HYPE follows the same bearish-1H `BUY_YES` suppression posture as the other alt lanes while leaving the existing diagnostic `BUY_NO` path intact.

**Verification:** Config change only. No backtests run in-agent; operator will run dashboard backtests after this change.

## 2026-05-13 — ETH live simplification: restore shared alt-first gating posture

**`config/settings.yaml`:** Tightened `eth_macro` back toward the shared alt-macro posture: `neutral_macro_require_spike_or_lag: true`, `enforce_alt_1h_alignment: true`, `direction_source: btc` (alt-first / no per-market side override), disabled the permissive BTC-follow bypasses (`btc_follow_stf_bypass_if_1h_ok`, `btc_follow_15m_allow_macd_grind`, `btc_follow_stf_bypass_when_macro_agrees`, `btc_follow_1h_allow_floor_without_rising`), and re-enabled `btc_1h_regime_gates` so ETH min-edge/size tighten in BTC RANGE/BEAR regimes like the other alt lanes.

**`src/strategies/eth_macro.py`:** Updated class defaults to match the stricter live posture so missing YAML keys no longer silently fall back to permissive ETH behavior (`direction_source` now defaults to `btc`; `btc_follow_1h_allow_floor_without_rising` now defaults to `false`).

**Verification:** `.venv/bin/python -m pytest tests/test_eth_macro.py tests/test_updown_backtest_parity.py -q` → **51 passed**.

## 2026-05-13 — Alt macro replay parity: neutral fallback tree, ETH fallback, HYPE hard-edge

**`src/backtest/updown_engine.py`:** Replay now resolves alt direction with live-style fallback branches instead of `NEUTRAL -> skip` for every non-BTC window. SOL/XRP/HYPE replay can now use BTC-spike, lag, and alt-1H fallback branches when macro HTF is neutral; ETH replay now uses alt-1H-primary direction with BTC-spike / lag / BTC-HTF fallback behavior closer to `eth_macro`, plus replay-side BUY_NO override support and a corrected SOL-family HTF vote that reads 1H candles directly instead of relying only on `ta.ema_*`.

**`src/backtest/updown_engine.py`:** HYPE replay now applies a post-edge hard floor compatible with live `hype_macro` behavior, including drift feedback and BTC 1H regime min-edge scaling when configured.

**`scripts/run_backtest_crypto.py`:** BTC OHLCV is now loaded for all alt macro backtests (`ETH`, `SOL`, `XRP`, `HYPE`) so replay can actually exercise BTC-secondary parity branches instead of starving them of context unless a 5m correlation gate was enabled.

**`tests/test_updown_backtest_parity.py`:** Adds regressions for SOL neutral BTC-spike fallback, SOL BUY_NO override parity, ETH BTC-HTF fallback when ETH 1H is neutral, and HYPE hard-min-edge enforcement.

## 2026-05-13 — Crypto backtest/live parity gates + Backtest-tab output tail

**`src/backtest/updown_engine.py`:** Backtest replay now mirrors live entry gating more closely: live-style `KellySizer` sizing before exposure clamps, structured `skip_counts` on `UpdownBacktestResult`, entry-window and timing-window filters during replay, `max_edge_updown` cap support, and ETH no longer hard-requires `btc_data` when `btc_follow_1h_required: false`.

**`tests/test_updown_backtest_parity.py`:** Adds focused regressions for ETH BTC-follow gating, `outside_entry_window` skip accounting, and `edge_above_cap` skip accounting.

**`src/dashboard/index.html`:** Backtest tab now includes a dedicated **Backtest output tail** panel wired to the existing `/api/backtest/status` polling, so subprocess stdout is visible on the tab while jobs run and after completion.

**`tests/test_dashboard_bundle.py`:** Guards the new Backtest-tab output tail DOM and polling hook.

## 2026-05-13 — Dashboard: fix inline JS brace/IIFE closure; CI parses scripts with node --check

**`src/dashboard/index.html`:** Restores missing `});` / IIFE terminator so `startCryptoBacktest` and boot paths parse as valid JavaScript.

**`tests/test_dashboard_bundle.py`:** Adds `test_dashboard_inline_scripts_parse_cleanly` — runs `node --check` on each inline `<script>` body.

## 2026-05-13 — Crypto backtest reliability: progress, cache-only oracle, faster alt replay

**`src/backtest/updown_engine.py`:** Normalizes OHLCV frames once with `_open_time_ns` so per-window history slices use `searchsorted`/`iloc` instead of boolean-copy scans; skips expensive Trend Sabre reconstruction for alt-family replays where BTC/SOL/ETH/XRP/HYPE paths do not consume it; adds replay `on_progress`, `progress_interval`, `max_seconds`, `total_windows`, `run_complete`, and `elapsed_seconds` metadata.

**`scripts/run_backtest_crypto.py`:** Adds heartbeat output (`--progress-interval`, default **1000**) and bounded partial runs via `--max-seconds`; default oracle mode is now **cache-only** so missing/stale Chainlink cache cannot hang a normal backtest. Use `--oracle-fetch` explicitly for slow RPC backfill; `--skip-oracle` still disables oracle replay entirely.

**`src/backtest/oracle_loader.py`:** `load_history(..., allow_fetch=False)` returns cached slices only and logs coverage misses instead of entering RPC fetch.

**`scripts/run_crypto_backtest_bundle.py`:** Forwards `--progress-interval`, `--max-seconds`, and `--oracle-fetch` to child runs.

**Verification:** ETH 15m `2026-01-20..2026-04-20` default cache-oracle run completed **8,736/8,736** windows in **54.2s** with progress; SOL 5m short-window smoke completed **1,728/1,728** windows in **2.8s**.

## 2026-05-13 — eth_macro BUY_NO audit fixes: symmetric macro-leg block + annotation/test cleanup

**`src/strategies/eth_macro.py`:** `block_counter_macro_leg_updown` now matches the ETH config comment for both sides: LONG blocks when `macro_leg < updown_macro_leg_min_for_long`; SHORT / `BUY_NO` blocks when `macro_leg > updown_macro_leg_max_for_short`. SHORT blocks emit `macro_leg_blocks_short` through normal skip and BUY_NO skip telemetry.

**`config/settings.yaml`:** Added `eth_macro.updown_macro_leg_max_for_short: 0.0`; clarified neutral ETH fallback and disabled per-market 1H alignment comments.

**`src/strategies/sol_macro.py`:** `_buy_no_ltf_override` type hint widened from `SOLTechnicalAnalysis` to `Any` because ETH/HYPE/XRP reuse the shared alt-TA shape.

**Tests:** `tests/test_eth_macro.py` covers SHORT-positive macro-leg blocking and LONG-negative behavior; `tests/test_strategy_execution_drivers.py` covers BUY_NO post-entry annotation receiving true YES mid.

## 2026-05-13 — Up/down backtest parity: Polymarket-mark integration coverage

**`tests/test_updown_backtest_parity.py`:** Added integration checks that ``UpdownBacktestEngine._settle_updown_with_live_exit_proxy`` prefers supplied Polymarket YES marks over the synthetic underlying proxy and that sparse minute marks still drive exit decisions via ``Series.asof`` near expiry.

## 2026-05-13 — Polymarket marks tests: fetch + parquet cache behavior

**`tests/test_updown_polymarket_slug.py`:** Covers `_yes_series_from_prices_df` (dedupe, sort, clip, empty/malformed), no API key (no fetch), cache hit (skips fetch), sparse cache → fetch, sparse fetch → no file, successful fetch → parquet write, corrupt cache → fetch, fetch exception → None. Mocks `PolymarketDataLoader` on `updown_polymarket_marks` module.

## 2026-05-13 — eth_macro: ETH-primary gates + optional BTC full analysis

**`config/settings.yaml`:** `eth_macro.neutral_macro_require_spike_or_lag: false`; `eth_macro.btc_1h_regime_gates.enabled: false`.

**`src/strategies/eth_macro.py`:** Require only ETH `get_full_analysis` to start a scan; if BTC full analysis is missing, use neutral BTC HTF placeholder, skip BTC 15m/5m impulse hard-gates (ETH leg + correlation path), and avoid regime sizing/min_edge without `btc_ta`. NEUTRAL+BTC HTF fallback uses explicit `BULLISH`/`BEARISH` only (not `!= NEUTRAL`).

**`tests/test_eth_macro.py`:** `test_eth_scan_eth_only_when_btc_full_analysis_unavailable`.

## 2026-05-13 — HYPE OHLCV: Binance USDM first, Hyperliquid fallback; dash report refresh retries

**`src/backtest/ohlcv_loader.py`:** For HYPE, try **Binance futures** ``HYPEUSDT`` klines (``fapi``) before Hyperliquid; same cache keys under ``data/backtest/ohlcv/HYPE_*.parquet``.

**`src/dashboard/index.html`:** ``loadBacktestReports(retries, gapMs)`` — after crypto backtest job completes (including resume), **5** polls at **700ms** gap plus ``fetchAll()`` when the Backtest tab is active so new ``backtest_*.json`` files show on cards.

**`src/analysis/sol_btc_service.py`:** Comment on HYPE oracle — Arbitrum Chainlink only in this repo; Pyth not integrated.

## 2026-05-13 — Dashboard crypto backtest: ALL bundle + Polymarket marks from settings

**`src/dashboard/server.py`:** ``POST /api/backtest/start`` accepts ``symbol: ALL`` (runs ``scripts/run_crypto_backtest_bundle.py``); optional ``start``, ``end``, ``parallel`` in JSON; when ``backtest.polymarket_marks.enabled`` in ``config/settings.yaml``, appends ``--polymarket-marks`` to single-symbol and bundle commands.

**`scripts/run_crypto_backtest_bundle.py`:** ``--polymarket-marks`` forwarded to each child ``run_backtest_crypto.py``.

**`src/dashboard/index.html`:** Crypto backtest dropdown adds **ALL {w}m (bundle)** rows; client sends ``symbol: ALL``.

**`tests/test_dashboard_bundle.py`:** HTML guard + bundle start smoke test.

## 2026-05-13 — Polymarket YES 1m marks for crypto up/down backtest (option A)

**`src/backtest/updown_polymarket_marks.py`:** Unix slug parity with scanner (``{asset}-updown-{5m|15m|30m}-{UTC_aligned_epoch}``); PolymarketData 1m YES fetch + parquet cache under ``data/backtest/polymarket_marks/``; requires ``POLYMARKETDATA_API_KEY``.

**`src/backtest/updown_engine.py`:** Per-window load; ``_settle_updown_with_live_exit_proxy`` uses ``Series.asof`` on PM YES when present, else underlying proxy.

**`config/settings.yaml`:** ``backtest.polymarket_marks`` (default ``enabled: false``).

**`scripts/run_backtest_crypto.py`:** ``--polymarket-marks`` flips config flag.

**`.gitignore`:** ignore cached ``*.parquet`` for those marks.

**`tests/test_updown_polymarket_slug.py`:** Slug alignment + disabled loader.

## 2026-05-13 — Shared crypto up/down exit config + backtest proxy aligned to live rules

**`src/execution/updown_exit_shared.py`:** Single parser for ``trading.exit_rules`` up/down fields (including ``updown_exit_window_max_fraction`` in overrides), ``CRYPTO_UPDOWN_STRATEGIES``, and pure helpers: scaled exit window, high-entry cents stop, in-profit % stop tighten, adverse time-stop branches (incl. short YES). Docstring states CLOB vs synthetic-mark limitation.

**`src/execution/live_testing.py`:** ``PositionExitManager`` delegates to shared parser/helpers (behavior unchanged for existing tests).

**`src/backtest/updown_engine.py`:** Loads same globals; ``_settle_updown_with_live_exit_proxy`` applies high-entry cents, in-profit % stop, scaled near-expiry window, and ``adverse_for_updown_cents_time_stop`` (long YES / long NO). Module docstring updated.

**`tests/test_updown_exit_shared.py`:** Coverage for parser and helpers.

## 2026-05-13 — Dashboard: Active Positions section starts collapsed

**`index.html`:** **`positionsCollapsed`** default **true**; **`positions-body-wrap`** **`display:none`** and chevron **▶** on load. **`server.py`:** **`dashboard_ui_rev`:** **`2026-05-13-positions-section-default-collapsed`**.

## 2026-05-13 — Polymarket CLOB WebSocket URL + subscribe wire-up

**`src/market/websocket.py`:** Connect to **`wss://ws-subscriptions-clob.polymarket.com/ws/market`** (bare **`/ws`** returns **404**). Initial payload **`{"type":"market","assets_ids":[...]}`**; add/remove uses **`operation` `subscribe` / `unsubscribe`**. Optional override **`trading.clob_ws.wss_url`**. On connect, clear in-memory subscription sets so reconnect resyncs. **`_clob_ws_cfg` / `_asset_ids_key`** helper.

## 2026-05-13 — Dashboard: Active Positions detail default closed

**`index.html`:** **`dashRenderPositionsMasterDetail`** — no longer auto-selects the first open row on load or when positions arrive; detail pane stays on **“Select a row…”** until the operator clicks a row. If the selected id is not in the current filtered list, selection clears instead of jumping to the first row.

**`server.py`:** **`dashboard_ui_rev`:** **`2026-05-13-positions-detail-default-closed`**.

## 2026-05-13 — May 11 exit audit: journal vs YAML (`updown_stop_loss` vs `updown_time_stop`)

**`docs/session_reports/may11_2026_exit_reason_reconciliation.md`:** Reconciles informal audit language with code — **`exit_reason: updown_stop_loss`** maps to **`updown_stop_loss_pct`** (% adverse), **not** **`updown_stop_cents`**. **`updown_time_stop`** maps to late-window cents + **`updown_exit_window_mins`**. Includes **`test_20260511_*`** paper slice (**7** EXIT rows: **4** pct-stop, **3** TP, **0** time-stop in-repo folders).

**`scripts/slice_paper_exits_by_session_prefix.py`:** CLI to aggregate EXIT rows by session dir prefix (exit_reason × window).

**`config/settings.yaml`:** `trading.exit_rules` comments — journal mapping pointer to the doc above.

**`src/analysis/underperformance_audit.py`:** Overall + per-strategy **`recent_buy_yes_stop_loss_loss`** / **`_share_of_negative_pnl`**; hypothesis evidence splits **time_stop $** vs **pct_stop $**; Markdown summary line for % stop share; fix-candidate text names both cohorts.

## 2026-05-13 — Up/down oracle relax, BUY_NO skip counts, entry timing window rename

**`src/analysis/updown_composite_score.py`:** **`validate_oracle_reference`** — **`stale_basis_relax_max_bps`** (pass when feed `updatedAt` is older than **`max_age_sec`** but \|basis\| within relax cap); **`allow_exchange_when_oracle_missing`** (pass when both Chainlink fields are absent but exchange spot exists). **`src/strategies/sol_macro.py`:** wires new flags from strategy config; on oracle fail **`BUY_NO`** now **`_emit_buy_no_skip`** so diagnostics match **`skip`** logs. Renamed **`_within_ai_decision_window`** → **`_within_entry_timing_window`** (config **`entry_timing_window_*`**, legacy **`ai_entry_window_*`** still read). **`src/strategies/bitcoin.py`**, **`src/strategies/eth_macro.py`:** same timing-window rename. **`config/settings.yaml`:** global **`ai_entry_window_` → `entry_timing_window_`**; **`hype_macro`** **`oracle_stale_basis_relax_max_bps: 45`**; XRP **`entry_price_max_15m_yes_side`** (code also reads legacy **`entry_price_max_15m_buy_yes`**). **Tests:** **`tests/test_updown_composite_score.py`**, **`tests/test_sol_macro.py`**.

## 2026-05-13 — Entry windows (SOL/HYPE), HYPE AI 15m max, 30m human compact slugs

**`config/settings.yaml`:** **`strategies.sol_macro.entry_window_15m_max`** **18 → 23**; **`strategies.hype_macro.entry_window_15m_max`** **16 → 27**; **`strategies.hype_macro.entry_timing_window_15m_max`** **15 → 25** (marginal up/down tie-break timing vs widened quant entry band). **`polymarket.fetch_updown_30m_human_compact_slugs`** (default **true**) — second slug family for 30m Gamma discovery.

**`scanner.py`:** **`_compact_updown_range_time_et`**, **`_iter_updown_30m_human_compact_slugs`** — ET half-hour slugs like **`bitcoin-up-or-down-may-13-530am-600am-et`** merged with **`{asset}-updown-30m-{unix}`** in **`fetch_updown_30m_markets`** (deduped); post-fetch duration band **20–52** minutes. **`fetch_updown_markets`** returns **`(15m_bucket, ~30m_carry)`** — rows from the **15m slug** responses with **`window_minutes` 26–34** (title-inferred half-hour) are no longer dropped; **`_sync_network_phase`** merges carry into **`updown_30m`** when **`polymarket.updown_30m_merge_from_15m_slug_batch`** is true (default). **`_dedupe_markets_by_id`**. Slugs with **`updown-15m`** and **`window_minutes` None** → **15m bucket**. **Tests:** **`tests/test_scanner_crypto_enhancements.py`**.

**RCA note:** Gamma **`btc-updown-30m-{unix}`** often **`[]`** while **`btc-updown-15m-{unix}`** resolves; compact human slugs match the **`hyperliquid-up-or-down-…-415am-430am-et`** family. **`updown_30m_count`** may stay **0** until Polymarket lists matching events. HYPE skip drivers vary by pulse (**`oracle_stale`** / **`liquidity`** vs **`outside_entry_window`**); rank from multi-cycle **`scan_skip_digest`**, not one ops snapshot. **Bulk Gamma caveat:** see **`docs/GAMMA_UPDOWN_30M_DISCOVERY.md`** (matching both substrings **`updown`** and **`30`** on a slug hits **`5m`** epoch false positives, not **`30m`** markets).

## 2026-05-12 — Dashboard: CLOB order book (WebSocket cache + REST fallback)

**`index.html`:** **`#positions-orderbook-wrap`** in Active Positions **detail** — polls **`GET /api/orderbook`** every **2s** for the held outcome token; shows **websocket** vs **rest** source and top bid/ask rows.

**`server.py`:** **`GET /api/orderbook?token_id=`** — prefers non-empty **WS** snapshot; else **public REST** via **`clob_client.fetch_order_book_snapshot`** (level-0 `get_order_book`). **`dashboard_ui_rev`:** **`2026-05-12-orderbook-ws-api`**.

**`websocket.py`:** Subscribe uses **`trading.clob_ws.asset_ids_json_key`** (default **`assets_ids`**); **`_merge_orders`** bid/ask sort fix; **`snapshot_order_book_json`**.

**`main.py`:** Background **`ws_client.listen()`** + subscription sync for **open-position** YES/NO tokens (`trading.clob_ws`, channel default **`market`**).

**`config/settings.yaml`:** **`trading.clob_ws`**.

## 2026-05-12 — HYPE OHLCV: HL UTC timestamps, 1m bisect pagination, 5m→1m fallback

**`hyperliquid_hype_service.py`:** **`utc=True`** on parsed candle times — fixes range merge/filter **`datetime64` vs `Timestamp`** failures that zeroed 5m/15m fetches. **Smaller 1m chunks**, **`_hl_bisect_fetch_chunk`** on empty snapshots. **`ohlcv_loader.py`:** if HL **1m** missing but **5m** exists — **resample forward-fill 5m→1m**; tz-safe localize. **`run_backtest_crypto.py`:** exit if **HYPE `1m` &lt; 50 bars** with HL retention hint.

## 2026-05-12 — Planned follow-up (superseded): Live dashboard CLOB order book / depth

**Update:** Initial **plan-only** note — **shipped** under **Dashboard: CLOB order book (WebSocket cache + REST fallback)** above.

## 2026-05-12 — Backtest: exit code in `/api/backtest/status`, UI failure line; HYPE min_edge YAML parity

**`server.py`:** **`exit_code`** on finished dashboard-spawned backtest jobs (scoped **`job_id`** + per-job list). **`index.html`:** **`pollBacktestJobUntilDone`** shows red failure when **`exit_code !== 0`** (e.g. HYPE OHLCV empty). **`config/settings.yaml`:** **`backtest.min_edge_hype_5m`** **0.07** to match **`strategies.hype_macro.min_edge_5m`** (was 0.09). **`dashboard_ui_rev`:** **`2026-05-12-bt-exit-hype-minedge`**.

## 2026-05-12 — Dashboard: ops digest ticker (status-only, under deck hint)

**`index.html`:** **`#ops-digest-ticker`** between deck filter hint and detail panel — builds one line from existing **`/api/status`** / merged deck fields (**regime**, open **positions** count, **side_selection** lanes, top **scan_skip_digest** skips, **session** PnL, kill / **can_trade** reason). **Marquee** when text overflows viewport; **`prefers-reduced-motion`** wraps with no animation. SSE merges **`scan_skip_digest`** when present and refreshes ticker. **`dashboard_ui_rev`:** **`2026-05-12-ops-digest-ticker`**.

## 2026-05-12 — Dashboard: Active Positions master list above deck + detail below

**`index.html`:** Compact **master list** directly under the **Active Positions** heading (above Side/Skips deck); **detail** pane unchanged in **`pos-unified-panel`** below deck + filter hint; row click restores selection → detail. **`dashboard_ui_rev`:** **`2026-05-12-positions-master-above-deck`**.

## 2026-05-12 — Positions: persist CLOB token ids + single-panel layout

**`clob_client.Position`:** optional **`token_id_yes`** / **`token_id_no`** (filled at entry from signals). **`trade_journal.log_entry`:** stores ids on **`open_positions`** for resume. **`main.py`:** bitcoin / SOL–ETH macro / weather execution passes tokens into **`Position`** and **`log_entry`**; journal sync restores them. **`server.py`:** status **`positions`** include **`token_id_yes`**, **`token_id_no`**, and derived **`clob_token_id`** (held leg); disk positions JSON same. Detail pane shows abbreviated **CLOB token** when present. *(Layout: see **Active Positions master list above deck** entry.)*

## 2026-05-12 — Dashboard: metric deck white chrome (deck-only CSS)

**`index.html`:** Ops metric deck chips use **transparent** fills, **white** metric text and **white** border/outline glow; **cyan** kept on **`.chip-k`** (Side / skip labels). **Filter** chips use **white** borders/hover instead of gold. Slightly tighter chip sizing; **`deckPulse`** uses a **white** ring (not cyan). **Tail** tile matches (transparent + soft white edge glow; **`.deck-tail-k`** cyan). **`pos-deck-filter-hint`** muted white (no yellow). **Unchanged:** global **`.g:hover` / `.card:hover`** glow. **`server.py` `dashboard_ui_rev`:** **`2026-05-12-metric-deck-white-chrome`**.

## 2026-05-12 — Discord exit-only: embed URL hardening, main exit payload, kill parity

**`AGENTS.md`:** Discord policy — **`notify_exit`** for closes when enabled; **`notify_trade`** stub; Polymarket link or explicit placeholder when **`market_id`** missing. **`notification_manager.py`:** **`_polymarket_market_url_for_exit`**, exit embed fields (**Market id**, **Trade id**, longer Market text), startup log line clarifies entry/fill disabled in code. **`main.py`:** **`notify_exit`** gains **`entry_price`** / **`trade_id_tail`** from position; global kill Discord loop includes **`xrp_dump_hedge`**. **`config/settings.yaml`:** comments for **`alert_on_trade`** / **`alert_on_exit`**. **`tests/test_notification_manager.py`:** URL helper + embed assertions. *(Vault `projects/polymarket-bot/changelog.md` not present in this workspace copy — infra note here.)*

## 2026-05-12 — Dashboard Live: master–detail positions + ops metric deck rail

**`index.html`:** Command Center **Active Positions** → master list + detail (Polymarket link, optional **end_date** countdown); row **enter** animation; list **exit** flash when a position drops; Side deck chips can **filter** the list. **Metric deck:** tabs Gates / Skips / Side, scroll-snap chips, edge fades, change **pulse**, trailing open + session PnL, **staleness** from **`ts`**, **`prefers-reduced-motion`**. **`server.py`:** **`serialize_position`** + disk positions pass **`end_date`**; **`/api/status`** adds **`ts`**; **`dashboard_ui_rev`:** **`2026-05-12-live-positions-master-detail-deck`**. **`test_dashboard_bundle`:** DOM markers + status **`ts`**.

## 2026-05-12 — Dashboard: alt-first crypto hero order + lag labels

**`index.html`:** SOL/ETH/HYPE/XRP **hero rows** put **alt Δ%** before **BTC Δ%**; lag tile labels **SOL–BTC lag**, **ETH–BTC lag**, **HYPE–BTC lag**, **XRP–BTC lag** (matches strategy: alt 1H primary, lag/BTC secondary). HYPE/XRP blurbs updated. **`server.py` `dashboard_ui_rev`:** **`2026-05-12-dashboard-alt-first-crypto-heroes`**.

## 2026-05-12 — Dashboard: crypto + equal-card rows stacked (single column)

**`index.html`:** **`crypto-grid`** and **`equal-card-grid.two`** use **`grid-template-columns: 1fr`** so BTC/SOL/ETH/HYPE/XRP cards stack vertically at full row width; larger gap and **`crypto-grid>.card`** padding. Fullscreen no longer forces a 2-column crypto row; hero tiles use readable min-heights and **`clamp`** font sizes. **`server.py` `dashboard_ui_rev`:** **`2026-05-12-dashboard-crypto-cards-stacked`**.

## 2026-05-12 — Dashboard: reduce Command Center flicker (ticker + badges + metrics)

**`index.html`:** **`dashRefreshCryptoLiveTickers`** removed from per-panel crypto status updates; **one** deferred pass at end of **`fetchAll`** when **`currentView === 'live'`** (double **`requestAnimationFrame`** after layout). **`updateMetrics`** skips rewriting **strategy boxes / table** when a stable numeric fingerprint is unchanged. **`ops-decision-gates`** skips **`innerHTML`** when digest HTML unchanged. **`server.py` `dashboard_ui_rev`:** **`2026-05-12-dashboard-flicker-ticker-coalesce`**.

## 2026-05-12 — Dashboard: crypto gate tiles + strategy metric grid (ticker + scroll)

**`index.html`:** Removed **`overflow:visible`** on **signal gate** cells (was letting columns grow past the grid, misshaping tiles and breaking **`dashApplySlowTickerIfNeeded`** width math). Gate wrappers are **`display:flex; flex-direction:column; min-width:0; overflow:hidden`**. **`[id$="-status-detail"]`** uses **`max-height:7em; overflow-y:auto`** so long gate copy scrolls vertically. **Strategy Performance** boxes use **`.strategy-metric-grid`** (`repeat(6, minmax(0,1fr))` + responsive 3/2 columns) instead of **`auto-fit` / `minmax(120px,1fr)`** for even tiles. **`dashRefreshCryptoLiveTickers`** runs tickers again after **`requestAnimationFrame`** so measurements happen post-layout. **`server.py` `dashboard_ui_rev`:** **`2026-05-12-crypto-cards-ticker-scroll-layout`**.

## 2026-05-12 — Hyperliquid HYPE `fetch_klines_range`: paginate long windows (fix empty 1m)

**`hyperliquid_hype_service.py`:** **`candleSnapshot`** was called once with the full **`[startTime, endTime]`** span (e.g. months of **1m**). Hyperliquid returns at most on the order of **~5000** candles per response; wide requests often came back **`[]`**, so backtests had **no HYPE 1m** and zero trades / crashes downstream. **`fetch_klines_range`** now **chunks** in **`_HL_MAX_CANDLES_PER_RANGE_REQUEST` (4000)** bars per request, advances from the last returned **`open_time`**, concatenates, dedupes, then applies the existing date filter. **`test_market_data_fallbacks`:** first chunk **`endTime`** assertion updated to match pagination.

## 2026-05-12 — `updown_engine`: skip settle when 1m frame missing `open_time`

**`updown_engine.py`:** Crypto updown replay assumed `data["1m"]` always had an **`open_time`** column. **HYPE** runs with no 1m bars (Hyperliquid range empty / partial cache) passed an empty frame and crashed with **`KeyError: 'open_time'`**. Before slicing for exit replay, **continue** the scan step when **`df_1m` is empty or lacks `open_time`** (same as “cannot settle”). Local **`run_backtest_crypto.py --skip-oracle`** matrix can finish; dashboard still lists saved **`backtest_crypto_*.json`** under **`data/backtest/reports/`**.

## 2026-05-12 — main: strategy refactors, updown/oracle/dashboard, pytest alignment (448 green)

**Bundle:** Removes **`src/strategies/_core/*`**, **`ai_call_log` / `ai_replay_agent`** and their tests; updates **`bitcoin`**, **`eth_macro`**, **`sol_macro`**, **`updown_engine`**, **`ai_agent`**, **`run_backtest_crypto`**, dashboard **markers / STRATS / decision digest**. **`tests`:** **`evaluate_trade_decision`** **`AsyncMock`** + **`AIDecision`** in BTC AI integration; **`test_dashboard_bundle`** matches decision chips (no **HYPE floor** chip), crypto hero CSS selector, **Session fills** on journal vs removed duplicate hero line; **`test_strategies`** weather size **25.0**. **`oracle_loader`:** warn when cache rows exist but requested window has no overlap (see below). **Not committed:** `data/entry_prices/updown_fills.jsonl` (local fill log lines).

## 2026-05-12 — Oracle loader: warn when JSONL exists but date window misses cache

**`oracle_loader.py`:** Local **`ethusdt_polygon_chainlink.jsonl`** (and similar) can span **April 2026** while backtests use **Jan–Feb 2026**, producing an **empty slice** and a misleading **“no oracle history fetched”** after failed RPC backfill. When the cache has rows but **none** in the requested `[start, end]`, log an explicit warning with **row count** and **cached timestamp span** so operators align **`--start` / `--end`** with the file or run from a network where Polygon RPC backfill succeeds.

## 2026-05-12 — BTC chart markers: darker on entry (in), lighter on exit (out)

**`index.html`:** Restored **`_shadeStratHex`** (hex darken / lighten). **`_tradeMarkersFromPoints`** uses **`stratHex`** then **`colorIn = shade(-0.36)`** for entry / **`colorOut = shade(+0.34)`** for exit; open trades keep the darker entry only. Win/loss remains shape + bar position + signed PnL text (no strategy-agnostic red). **`dashboard_ui_rev`:** **`2026-05-12-chart-markers-in-dark-out-light`**.

## 2026-05-12 — BTC chart trade markers: keep strategy color on losses

**`index.html`:** `_tradeMarkersFromPoints` painted **any** losing trade **`#b91c1c`**, so BTC markers looked red instead of **gold** and ETH red instead of **blue**. Markers now always use **`stratHex(strategy)`**; win vs loss stays **circle / square**, **belowBar / aboveBar**, and **signed PnL** in marker text. **`dashboard_ui_rev`:** **`2026-05-12-btc-chart-markers-strategy-hue-only`**.

## 2026-05-12 — Command Center strategy boxes + positions: use canonical `STRATS` / `stratHex`

**`index.html`:** The six **strategy metric boxes** (BTC/SOL/ETH/HYPE/XRP/Weather signal counts) still used **hardcoded colors** (`var(--green)` for bitcoin, `#fb923c` for eth_macro, etc.), so the same strategy looked **different** from journal pills, scan badges, and chart markers. **`sigBox`** now takes `(strat, label)` and sets the big number color with **`stratHex(strat)`**. **Active positions** strategy column uses **`stratPillHTML`** instead of a generic cyan badge. Backtest weather summary card uses **`stratPillHTML('weather')`**. Removed unused **`_hexToRgbTrade` / `_shadeHexTrade`** helpers after marker color simplification. **`dashboard_ui_rev`:** **`2026-05-12-strategy-metric-boxes-strathex`**.

## 2026-05-12 — Command Center: drop duplicate session fills line under Trades today

**`index.html`:** Removed **`hero-trades-sub`** (`fills this session: N`) under the hero trades figure — it repeated the same count as **`daily_trades`** for many operators and read as redundant noise. Session-scoped entry counts remain on **Paper Trade Journal** (**Session fills**). Tooltip now states UTC day vs session scope. **`dashboard_ui_rev`:** **`2026-05-12-command-center-no-duplicate-trades-line`**.

## 2026-05-12 — Scrub unused May 9 `15m_buy_yes` AI bridge

Removed **`hype_macro`** enforced lane **`15m_buy_yes`**, **`updown_composite.hype_15m_buy_yes_min_score`**, and digest/UI wiring (`size_multiplier_15m_buy_yes`, hype floor chip). **`sol_macro`** up/down scoring lane is always **`default`** for AI enforcement checks; **XRP** **`entry_price_max_15m_buy_yes`** (price cap) is unchanged. Tests updated for lane-agnostic composite floor. **`dashboard_ui_rev`:** **`2026-05-12-scrub-15m-buy-yes-bridge-ui`**.

## 2026-05-12 — Crypto updown replay: oracle basis uses 1m spot (not HTF close)

**`UpdownBacktestEngine`:** `oracle_max_basis_bps` compared Chainlink to **`ta.current_price`**, which is the last **4h/1h** close before the window—often very stale vs a fresh oracle tick, so ETH (and other) backtests with oracle replay could show **zero trades** while the same run with `oracle_history=None` still produced signals. Basis now prefers **last 1m close strictly before `window_open`**, falling back to `ta.current_price` if 1m is missing (**`src/backtest/updown_engine.py`**). **`tests/test_backtest_oracle_replay.py`:** align ETH patches with **`_get_sol_htf_bias`**, keep **`_ohlcv_1m`** open≠close so windows can settle.

## 2026-05-12 — Pytest and crypto CLI: explicit completion banners

**`tests/conftest.py`** — **`pytest_sessionfinish`** prints a **`PYTEST COMPLETE`** footer (pass vs non-zero exit). **`scripts/run_backtest_crypto.py`** prints **`CRYPTO BACKTEST COMPLETE (SYMBOL Xm)`** after a successful full run.

## 2026-05-12 — Dashboard Backtest: single Run Crypto BT button

Removed legacy **Run Backtest** stub (duplicate UX; **`startBacktest()`** only warned about rigorous mode). **`Run Crypto BT`** uses **`crypto-bt-run-btn`**. **`dashboard_ui_rev`:** **`2026-05-12-backtest-single-run-btn`**.

## 2026-05-12 — Dashboard Backtest: remove optional test-split date input

Backtest tab no longer shows the walk-forward **`test_start`** date picker; dashboard runs stay full-range in-sample unless **`--test-start`** is passed from CLI. **`dashboard_ui_rev`:** **`2026-05-12-backtest-no-test-date-ui`**.

## 2026-05-12 — Dashboard Backtest tab: mirrors live strategies from settings.yaml

**`GET /api/backtest/reports`** adds **`live_scope`** (crypto **`strategies.*.enabled`**, **`windows`** [5,15,30] aligned with live routing buckets, **`weather_enabled`**). **`index.html`** renders only enabled journal keys (**`bitcoin`**, **`*_macro`**), dropdown options built from that scope, **weather** summary only when **`strategies.weather.enabled`**. **XRP dump-hedge** sim removed from dashboard UI (not live); **`xrp_dump_hedge_sim`** JSON remains excluded from crypto card dedupe in **`server.py`**. Weather reports use **`backtest_weather_*.json`** under **`data/backtest/reports/`**. **`dashboard_ui_rev`:** **`2026-05-12-backtest-live-scope`**.

## 2026-05-12 — Live crypto macro direction rule: alt-first, BTC-secondary

Operator correction: **all non-BTC live strategies must choose direction from the asset’s own alt indicators first**; BTC is secondary context/fallback and must not override a usable alt HTF side. Patched **`sol_macro.py`** base behavior and **`eth_macro.py`** so `direction_source: hybrid` no longer lets BTC/market proxy choose over ETH 1H. Important cleanup note: **`hype_macro.py`** and **`xrp_macro.py` currently inherit `SolMacroStrategy` in code while swapping their own services/market filters**, so they are affected by base-class behavior even though they are not SOL strategies and should retain their own oracle/data assumptions. Follow-up audit: split/rename the shared alt macro base or verify each subclass override so no HYPE/XRP/ETH path accidentally inherits SOL-specific assumptions.

## 2026-05-11 — Dashboard: Performance Exit Reasons strip (undefined `$`)

**`updateExitReasons`** called `$('…')` but **`$`** was only defined inside **`updateLivePerf`**, so successful **`/api/journal/exit-reason-summary`** responses threw **`ReferenceError`**, **`fetchAll` caught it**, and the bar stayed blank. Added local **`const $ = (id) => document.getElementById(id)`** inside **`updateExitReasons`**. **`dashboard_ui_rev`:** `2026-05-11-exit-reasons-strip-fix`.

## 2026-05-14 — HYPE macro admission-gate loosening

**`config/settings.yaml`:** HYPE-only live-test participation patch. Lowered **`strategies.hype_macro.min_liquidity`** from **1000** to **500** after repeated **`liquidity`** skips on thin Hyperliquid books. Widened **`entry_window_15m_max`** from **27.0** to **32.0**, **`entry_window_5m_max`** from **4.5** to **5.5**, and **`entry_window_auto_align_max_expand_min`** from **3.0** to **4.0** to reduce dominant **`outside_entry_window`** starvation.

**`config/settings.yaml`:** Relaxed HYPE oracle friction by raising **`oracle_max_basis_bps`** from **12.0** to **18.0** and **`oracle_stale_basis_relax_max_bps`** from **45.0** to **75.0** after repeated **`oracle_stale`** / **`oracle_basis_block`** skips.

**`config/settings.yaml`:** Removed three HYPE admission blocks during local testing: **`neutral_macro_require_spike_or_lag: true -> false`**, **`enforce_alt_1h_alignment: true -> false`**, and **`require_btc_catalyst_5m: true -> false`**. The purpose is to stop the **short-side 1H histogram veto** and 5m catalyst gate from suppressing nearly all HYPE candidates while participation is close to zero.

## 2026-05-11 — Dashboard: Performance tab polls journal summary for Session Summary

**`fetchAll`** uses **`needJournalSummary = needJournal || needPerformance`** so **`GET /api/journal/summary`** runs on **Performance**, not only **Journal**. **`updateSessionSummary(j)`** always runs off that payload; **`updateJournal(j)`** still only when **Journal** and not archive-pinned. Fixes empty **Session Summary** card when staying on Performance. **`dashboard_ui_rev`:** `2026-05-11-performance-session-summary-poll`.

## 2026-05-11 — Dashboard: Journal session history + cleaner BTC header

- **Test Session History** moved **Performance → Journal** (after **Test runs**; same `/api/journal/sessions` + row click → `selectJournalSession`). **`loadSessionHistory()`** tied to Journal tab, journal poll, and refresh when Journal is active.
- **BTC card:** removed **`#btc-signal-badges`** mini gate summary (duplicate of **Signal Gates** below). Removed **`syncCryptoGateSummaryBar`**.
- **`dashboard_ui_rev`:** superseded by **Performance session-summary poll** bump (verify **`/health`**).

## 2026-05-11 — Dashboard: real 30m MACD & % moves on existing rows

**SOL/BTC service:** `SOLAnalysis.macd_30m`, `BTCSOLCorrelation` 30m %-moves (1m×31), 120×1m pulls; **BTC** `TechnicalAnalysis.macd_30m` + **`get_full_analysis`**; **Hyperliquid** `"30m"` interval; **backtest** `_build_ta` populates `macd_30m`; **API:** `/api/bitcoin/analysis` macd 1h/30m/15m fields; alt payload `macd_30m_*`, `btc_move_30m`, `sol_move_30m`, `{alt}_move_30m`. **Dashboard:** BTC hero + meta strip MACD 30m; alt meta **MACD 30m \| 15m \| 5m \| Corr \| ATR**; hero **5·15·30m** Δ% triples. `dashboard_ui_rev` bumped.

## 2026-05-11 — Commit + Codex review handoff

Git commit batches the **30m** chain (scanner, strategies, Kelly, tests) and **dashboard/API/analysis** work; **`data/entry_prices/updown_fills.jsonl`** excluded from that commit (operator-local). Second-pass brief for reasons, expected outcomes, checklist: **`docs/HANDOFF_CODEX_30M_REVIEW_2026-05-11.md`**.

## 2026-05-11 — Updown WR on Live (existing gates only)

`/api/journal/updown_breakdown` is fetched when **Live** or **Performance** is open so **existing** Signal Gate WR lines (30m / 15m / 5m) populate; no extra Journal tab cards. `dashboard_ui_rev` bumped.

## 2026-05-11 — Up/down 30m chain + dashboard session P&L

Scanner merge + strategies already carried **30m** Polymarket buckets; this pass completes **dashboard classification** (`*_updown_30m`), **Performance TARGET** rows, **Kelly `_window_stats` 30m**, **SSE `session_total_pnl`** mapped into hero **Session P&L** (prefers journal **`total_pnl`** over realized-only), **`main.py`** lookahead logging + **`window_minutes ≤ 45`** crypto exemption + **`_detect_window_from_question`** 30m deltas, **`live_strategy_scan.py`** 9-tuple unpack, and test updates (`test_hype_integration`, `test_sol_macro`, classify/Kelly).

## 2026-05-10 — Dashboard session fills consistency

Journal: **`get_summary()`** **`total_entries` / `total_exits`** now mirror **`open_positions`** + **`closed_trades`** (phantom-skipped trades no longer deflate session fill totals on the dashboard). **`log_entry`** infers **`entry_leg`** from action/side/outcome when not passed explicitly (fixes **`BUY_NO`** defaulting to YES phantom guard at exit). **`_build_closed_stats`** token-flip filter matches **`log_exit`** (**YES-leg** only).

## 2026-05-09 — Session bundle (May 9)

Work landed on `main` the same calendar day; individual commits below are the audit trail where separate agent sessions did not leave vault notes.

| Commit | Summary |
|--------|---------|
| `1e4d433` | Drift runtime feedback (`performance_feedback`, `_runtime_feedback`), shared backtest expectations loader, strategy min-edge + Kelly hooks, dashboard drift/status + `STRATS` aliases + trade markers (darker/lighter in/out), journal learning module + `run_learning_loop.py` + tests. |
| `30d9252` | Dashboard: Claude design handoff visuals + live tab restructure. |
| `04f95b9` | Up/down backtest parity, sizing, narrators, session artifacts. |
| `add4724` | Dashboard: crypto tiles, decision gates, fullscreen layout. |
| `65a2c09` | Hide AI reasoning in dashboard summaries. |
| `6a24c8f` | Surface decision gates in dashboard. |
| `6afdc9a` | Oracle and composite decision gates. |
| `35116dd` | Use AI decision layer in macro strategies. |
| `0f28b69` | Enforced AI decision layer. |
| `1281189` | Require AI for low-confidence BTC neutral entries. |
| `d2299f7` | Tighten BTC neutral 15m entries. |
| `b417971` | Shadow calibration and exit replay tooling. |
## 2026-05-15 — Unified loop overrun recovery

| Commit | Summary |
|--------|---------|
| `uncommitted` | Replaced zero-sleep overrun behavior in `src/main.py` with a bounded recovery pause (`trading.overrun_recovery_sleep_sec`, default 5s) so slow cycles do not hammer immediately and do not skip a full extra cadence slot. |
| `uncommitted` | Added per-strategy scan timing logs plus AI elapsed-ms logging, then bounded BTC AI calls to 15s and disabled BTC 5m AI assists by default so slow LLM calls do not dominate the unified 60s trading cycle. |

---

| `c46e4f4` | Buy-no status diagnostics. |

---

## 2026-05-14 — BTC HTF observability

| Commit | Summary |
|--------|---------|
| `uncommitted` | Added vote-level BTC 4H bias diagnostics (`sabre`, `price_vs_ma`, `macd` state, raw vs final bias, histogram conviction) to shared strategy helpers and threaded them into `eth_macro` entry reasons, indicator snapshots, and scan stats. |

---

## 2026-05-08 — AI / journal surface

| Commit | Summary |
|--------|---------|
| `acdbb3a` | Narrator output → journal + dashboard AI summary panel. |
| `b1c177a` | Auto-run narrators on bot startup against previous session. |
| `0e9d579` | Shadow pipeline expansion, post-trade annotation, narrators, pre-entry veto. |

---

## 2026-05-07 — Kelly, exits, dashboard wiring

| Commit | Summary |
|--------|---------|
| `e0718f5` | Docs: dashboard wiring sweep (stop-loss, exit-reason visibility). |
| `591266f` | Dashboard: stop-loss config, exit-reason summary, bankroll unavailable state. |
| `b0a89f8` | `updown_stop_loss_pct` (time-stop bleed). |
| `1e64a61` / `adc5157` / `7529062` / `1a7023d` | Kelly fraction calibration and cap fixes. |
| `cd66c96` | Config: alt-lane gate intervention. |
| `30061a3` | Lower `min_trade_usd` so Kelly sizing flows through. |

---

## How to extend

1. After a merge or push, append a **dated section** or add rows to the table.  
2. Prefer **one line per commit**; link to PRs if you use them.  
3. If you *did* update the strategy vault, cross-link the vault date/heading here (optional).
