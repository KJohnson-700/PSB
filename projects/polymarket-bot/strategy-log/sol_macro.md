# SOL macro (`sol_macro`)

SOL **Up or Down** vs BTC correlation/lag; macro + LTF + optional LLM; entry timing windows on up/down markets.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 20 | Paper `test_20260504_034719` — [`docs/session_reports/session_parse_test_20260504_034719.json`](docs/session_reports/session_parse_test_20260504_034719.json) |
| Win rate | 75.0% | same |
| Net PnL | +$12.20 | same |
| BUY_YES / BUY_NO (session) | 18 / +$14.90 vs 2 / -$2.70 | same |

## Change Log

### 2026-06-05 — Remove BTC leakage from SOL-family trade reasons and AI contexts

- **What changed:** In [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), removed BTC labels/values from alt trade reason strings, scan diagnostics, and AI decision context (`BTC_HTF`, `btc=$`, `corr=`, `diag_btc*`, `btc_spike_boost`, `lag_boost`). Also removed the residual 5m confidence component based on BTC correlation; confidence is now alt-native MACD/RSI/timing only.
- **Why:** BTC was already disabled as a trade gate, but its diagnostic strings still leaked into SOL-family explanations and one confidence component still referenced BTC correlation.
- **Hypothesis:** SOL/SOL-family decisions and explanations read as alt-native only; no BTC-looking tokens should appear in new alt signal reasons after rollout.
- **Expected outcome:** Post-restart SOL/SOL-family entries/skips no longer include BTC labels in `signal_reason` or marginal AI contexts.
- **Actual outcome:** `pending` — requires restart and at least 15 closed affected trades after rollout.
- **Status:** `pending`

### 2026-06-05 — Enforce alt-native rule: BTC no longer affects SOL trade decisions

- **What changed:** In [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), disabled BTC-derived trade controls for SOL-family alt paths via `_btc_trade_inputs_enabled() == False`. BTC context remains logged, but no longer changes SOL admission, edge, confidence, min-edge, centered-price gating, low/degraded-correlation handling, macro-leg blocks, BTC 1h regime multipliers, or sizing. Updated SOL tests so even legacy opt-in BTC-regime / low-correlation config cannot make BTC decide a SOL entry.
- **Why:** Operator reiterated the standing invariant: BTC must not decide alt trades. Current session `test_20260604_234611` showed SOL 5m BUY_YES entries carrying BTC context (`BTC_HTF`, BTC regime/correlation/catalyst diagnostics), and the code still retained BTC-derived boosts/gates despite comments saying diagnostic-only.
- **Hypothesis:** SOL entries and skips become strictly SOL-native plus oracle/price/risk controls; BTC fields remain available for post-hoc context but cannot manufacture, block, or resize trades.
- **Expected outcome:** Future SOL skip stats should stop showing BTC-derived hard reasons such as `degraded_correlation`, `low_corr_suppressed`, `macro_leg_blocks_long`, or `centered_price_no_catalyst`; signal reasons may still include `diag_*btc*` tokens.
- **Actual outcome:** `pending` — requires restart and at least 15 closed SOL trades after rollout.
- **Status:** `pending`

### 2026-06-01 — Decisive AI prompt (kill the HOLD default)

- **What changed:** Rewrote the shared decision/analysis system prompt ([`ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py) `SYSTEM_PROMPT`): removed the "be conservative — markets overestimate" inaction bias, reframed HOLD as requiring a *specific, evidence-based* reason (never a default for uncertainty), told it to commit to a direction on any real lean, and clarified `confidence_score` = strength of the directional evidence. Bumped `prompt_version` → `lane-feedback-v2-decisive` so the settler can split pre/post verdicts.
- **Why:** Diagnosis showed the AI returned **HOLD on 77%** of responses and approved only **3%** — the prompt itself was steering the model toward inaction on near-coin-flip 15m/1h markets, which the veto-only marginal change alone does not fix.
- **Hypothesis:** A decisive prompt cuts spurious HOLDs and surfaces more directional calls (and more *confident-opposition* vetoes), making the gate a real tiebreaker rather than a near-total veto.
- **Expected outcome:** HOLD share drops well below 77%; more BUY_YES/BUY_NO verdicts; gate approval rate rises off 3%.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only; needs bot restart. Watch HOLD% / approval% in `decision_layer.jsonl` under prompt_version `lane-feedback-v2-decisive`.

### 2026-06-01 — Marginal lane → AI veto-only (unblock 15m/1h)

- **What changed:** The marginal-lane AI gate flipped from fail-closed to **veto-only**: the AI can now only REJECT a below-threshold candidate with a *confident, directly-opposing* directional call (conf ≥ `decision_layer.min_confidence`). HOLD / SKIP / low-confidence / agreement fall back to the quant trade. Central change in [`evaluate_trade_decision`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py) (new `veto_only` param) threaded through the marginal call site(s); guarded the redundant local re-checks; opt-out `decision_layer.marginal_veto_only`.
- **Why:** Over a 5.5h window (`decision_layer.jsonl`) the gate approved only **3%** of AI-evaluated candidates — the model returned **HOLD 77%** of the time (conservative system prompt + a 0.60 confidence bar that near-coin-flip 15m/1h markets can't honestly clear), and HOLD was a hard veto. SOL approved only 1/19 AI-evaluated candidates in the window.
- **Hypothesis:** Restoring marginal admission (blocked only on confident AI opposition) reopens 15m/1h frequency without losing the AI's ability to stop a conviction-wrong trade.
- **Expected outcome:** SOL 15m/1h marginal entries resume; AI still vetoes confident-opposite cases.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only (AI-gate behavior is not ghost-validatable); needs bot restart to load.

### 2026-06-01 — Shared BUY_YES soft-repair hook

- **What changed:** Added a shared BUY_YES lane-repair hook to the SOL-family macro path. The hook can only apply configured probability haircuts and min-edge adders; it cannot disable lanes or allowlist families. SOL has no active rule in this change.
- **Why:** Operator rejected disabling losers as a WR shortcut. The shared path needs a safer repair surface for XRP/HYPE and future SOL-family BUY_YES overconfidence fixes.
- **Hypothesis:** Future SOL-family BUY_YES tuning can target false-positive causes without removing whole lanes from evidence collection.
- **Expected outcome:** No SOL behavior change unless a SOL-specific `buy_yes_lane_repair` rule is added later.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-06-01 — Revert BUY_YES WR-mode side/window cleanup

- **What changed:** Reverted the same-day SOL BUY_YES lane disables. SOL `5m up`, `15m up`, and `1h up` return to their prior entry-policy behavior.
- **Why:** Operator rejected lane disabling as a lazy WR fix. The next correction needs to diagnose why BUY_YES false positives pass edge/price/regime gates.
- **Hypothesis:** Restoring admission preserves live/ghost evidence while future work changes causal signal quality rather than removing losing cohorts wholesale.
- **Expected outcome:** SOL BUY_YES no longer stops because of this WR-mode cleanup; next review should isolate calibration, BTC-secondary context, oracle basis, and entry-family effects.
- **Actual outcome:** `pending`
- **Status:** `reverted ❌`

### 2026-06-01 — BUY_YES WR-mode side/window cleanup

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), disabled SOL `5m up` and `1h up` entry-policy lanes while leaving `15m up` enabled.
- **Why:** Past-3-day BUY_YES review showed SOL `5m` at `14` trades / `42.9%` WR, below the operator's `55%` minimum. SOL `15m` was small but above target at `6` trades / `66.7%` WR.
- **Hypothesis:** SOL BUY_YES sample shifts toward the better recent 15m cell while 5m/1h upside pauses until ghosts/live journals justify re-enabling.
- **Expected outcome:** SOL `5m up` / `1h up` BUY_YES entries should stop; review SOL 15m BUY_YES after at least 15 closed post-change trades.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-31 — Suppress anti-predictive 5m-native BUY_NO shorts (all alts)

- **What changed:** New opt-in `disable_buy_no_5m_native` (set `true` for all 6 alts). In [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) `_resolve_alt_bias_for_tf` consumer, 5m up/down `BUY_NO` candidates are sat out when the flag is set, routed through `_log_skip_reject` (reason `buy_no_5m_native_suppressed`) so the counterfactual keeps settling in the ghost log. ETH inherits this via `ETHMacroStrategy(SolMacroStrategy)` — single edit covers all alts. Commit `5d8cbc0`.
- **Why:** Held-to-resolution (`data/calibration/trades_settled.jsonl`): 5m-native shorts are anti-predictive — eth 11.8%, xrp 16.7%, doge 27.8%, sol 33.3% WR vs the SAME alt's 15m-native at 50-65%. Ghost-reject correlation showed the MACD veto on 5m shorts is a coin-flip (47.7% resolve YES, n=2483), i.e. the 5m short *signal* is inverted — so suppression beats soft-scoring (IQL/MACD soft-scoring parked).
- **Hypothesis:** Removing the inverted 5m short cell lifts alt BUY_NO held-WR toward 50% without touching longs or 15m shorts; throughput loss is bounded to a -EV cell.
- **Expected outcome:** `buy_no_5m_native_suppressed` appears in `rejected_candidates.jsonl`; alt BUY_NO held-WR rises; 5m BUY_YES and 15m BUY_NO unchanged.
- **Actual outcome:** `pending` (needs bot restart + ~15 closed post-change trades)
- **Status:** `pending`

### 2026-05-31 — SOL 1h starvation window repair

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), widened SOL 1h entry windows from `60.0` to `360.0` minutes in both `by_tf.1h` and `entry_policy.window_side_overrides.1h` for `up` and `down`.
- **Why:** The May 31 morning audit showed 1h alt starvation from `lane_entry_window`: the feed was mostly surfacing 1h candidates `61-298` minutes out, while SOL still capped 1h admission at `60.0`. May 30 settled ghosts showed SOL 1h rejected candidates at `59.4%` WR / `+12.9%` ROI (`n=962`), including `sol_macro|1h|LONG|lane_entry_window` at `68.9%` WR / `+37.9%` ROI (`n=61`).
- **Hypothesis:** SOL 1h should resume collecting paper fills instead of starving on future-listed markets; downstream edge, price, oracle, and risk gates still decide final admission.
- **Expected outcome:** SOL 1h `lane_entry_window` rejects should fall and 1h trade count should become nonzero in comparable sessions.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-28 — Local SOL short-lane guards after 126-trade session review

- **What changed:** In [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), added SOL-only local guards that block `sol_5m_vs_slower` `BUY_NO` entries when SOL 1H is bullish, and block `sol_15m_native` `BUY_NO` entries when BTC 1H regime is `BULL` and the YES side is already expensive (`sol_15m_buy_no_max_yes_price_bull_1h`, default `0.48`). Added focused regression coverage in [tests/test_sol_macro.py](/Users/mainfolder/Documents/psb-main%201/tests/test_sol_macro.py).
- **Why:** The 126-trade session review showed SOL was not a broad loser, but its repeat damage clustered in those two short-side subpaths while `sol_5m_native` remained the healthy lane.
- **Hypothesis:** SOL should keep its native 5m path active while cutting the specific short-side branches that were leaking expectancy in bullish higher-timeframe context.
- **Expected outcome:** Future SOL skips should include `sol_vs_slower_short_against_h1` and `sol_15m_bull_regime_expensive_short`; `sol_5m_native` throughput should remain intact.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-28 — `sell_5m_low_corr` hard skip downgraded to diagnostic context

- **What changed:** In [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), `sell_5m_low_corr` no longer hard-skips 5m `BUY_NO` candidates. Low alt-vs-BTC correlation is now appended as `diag_sell_5m_low_corr(...)` in the signal reason while later alt-native signal, edge, price, liquidity, and risk gates still decide admission.
- **Why:** Calibration review showed the rejected pool near coin-flip-to-positive, and the hard skip let BTC correlation decide SOL admission despite the current alt-native rule.
- **Hypothesis:** SOL 5m downside throughput should improve without bypassing the actual edge and risk gates.
- **Expected outcome:** Future SOL diagnostics should stop reporting `sell_5m_low_corr` as a hard skip; impact should be reviewed after >=15 closed post-change SOL trades.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-26 — Timeframe-scoped entry control config

- **What changed:** Moved SOL entry-control thresholds and windows from legacy flat timeframe keys (`min_edge_5m`, `entry_window_*`) into canonical `defaults` / `by_tf` config, and routed shared macro entry policy reads through the timeframe resolver.
- **Why:** Static config audit showed the same 5m/15m values duplicated in flat keys and lane policy overrides, making it unclear which tuning surface was authoritative.
- **Hypothesis:** SOL 5m/15m/1h tuning changes should stay scoped to their `by_tf` cell with no cross-timeframe bleed.
- **Expected outcome:** Startup logs should show SOL `by_tf` overrides; focused tests should preserve the same effective min-edge/window values.
- **Actual outcome:** `pending` (config migration only; need >=15 closed SOL trades before performance judgment).
- **Status:** `pending`

### 2026-05-26 — Resolver metadata parity for shared macro signals

- **What changed:** Added BTC-compatible resolver metadata to the shared macro signal path: `conflict_type`, `resolver_path`, `htf_side`, `quant_side`, and `momentum_side`, with journal and position persistence.
- **Why:** SOL-family entries already had HTF and oracle metadata, but direction-resolution details were not first-class like BTC.
- **Hypothesis:** Future ghost/trade reviews can separate HTF-aligned, quant-disagree, and momentum-disagree entries without changing entry behavior.
- **Expected outcome:** New SOL entries should include resolver metadata in journal extras and `entry_signal`.
- **Actual outcome:** `pending` (need post-change entries to verify field coverage).
- **Status:** `pending`

### 2026-05-26 — Hold up/down winners to resolution

- **What changed:** Enabled `trading.exit_rules.updown_hold_winners_to_resolution` and suppressed up/down `take_profit` exits while that flag is true.
- **Why:** SOL still showed exit+selection damage after separating sizing. This targets premature winner clipping directly.
- **Hypothesis:** Correct SOL trades should realize closer to binary-resolution payoff when held through settlement.
- **Expected outcome:** Fewer `take_profit` exits, more `RESOLVED:* (real)` exits, and higher avg-win dollars.
- **Actual outcome:** `pending` (need >=15 closed SOL trades after restart).
- **Status:** `pending`

### 2026-05-26 — Missed SOL 15m IQL restore before restart

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), restored `sol_macro.iql_15m_enabled: true` with the existing `iql_15m_hist_floor: 0.06`.
- **Why:** A second pass found SOL still carried the May 25 exploration setting (`iql_15m_enabled: false`) even though the May 22 baseline and earlier rollback intent had the 15m IQL guard active. This was a missed setting in the pre-restart rollback.
- **Hypothesis:** SOL 15m volume should drop when the 15m MACD structure is not strong enough, reducing the SOL volume-up/loss problem while leaving the BUY_YES recovery tweak intact for non-15m paths.
- **Expected outcome:** Next paper run should show `iql_15m_reject` ghosts/skips for weak SOL 15m candidates and lower SOL 15m standard-lane churn.
- **Actual outcome:** `pending` (need ≥15 closed SOL trades after restart on this config).
- **Status:** `pending`

### 2026-05-26 — BUY_YES recovery tweak and missing SOL settings

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), changed SOL `alt_momentum_confirm.buy_yes` from all windows to `15m` only while keeping `buy_no` confirmation on `5m`, `15m`, and `1h`. Added explicit SOL `min_liquidity_buy_no`, `entry_price_max_15m_yes_side`, `oracle_max_basis_bps_15m_buy_yes`, and `oracle_basis_relax_max_bps_15m_buy_yes`. In [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), side/window-specific oracle basis settings are now read by the live validator.
- **Why:** The prior rollback risked overcorrecting into BUY_YES starvation. SOL also lacked settings that the shared strategy code supports and other alt lanes were already carrying.
- **Hypothesis:** SOL BUY_YES can reappear on cleaner 5m/1h setups while 15m BUY_YES remains confirmed; BUY_NO remains protected from unconfirmed bearish-default floods.
- **Expected outcome:** Next paper run should show nonzero SOL BUY_YES candidates/fills without reopening unconfirmed BUY_NO volume.
- **Actual outcome:** `pending` (need ≥15 closed SOL trades after restart on this config).
- **Status:** `pending`

### 2026-05-26 — Pre-restart rollback of post-May-22 momentum guard regression

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), restored `sol_macro.alt_momentum_confirm` to block `BUY_YES` and `BUY_NO` on `5m`, `15m`, and `1h` unless SOL-native MACD confirms the trade direction.
- **Why:** Post-restart accepted trades became overwhelmingly `BUY_NO`, with `bearish_dip_default` swallowing most fills. Review against baseline `62486e6` showed the May 22 default-on momentum guards had been replaced by empty allowlists, so the later “restore baseline” did not actually restore the protective admission behavior.
- **Hypothesis:** SOL default-side entries, especially bearish-standard 5m/15m lanes, should drop unless SOL tape confirms the side; `buy_*_no_alt_momentum_confirm` should reappear as an intentional skip reason.
- **Expected outcome:** Next paper run should show fewer SOL `bearish_dip_default` fills and lower SOL drawdown contribution, while still allowing confirmed LONG/SHORT entries.
- **Actual outcome:** `pending` (need ≥15 closed SOL trades after restart on this config).
- **Status:** `pending`

### 2026-05-25 — Exploration sizing instead of SOL suppression

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), SOL 5m/15m up/down entry-policy overrides now include `size_multiplier: 0.3`, while `alt_momentum_confirm` remains open and `iql_15m_enabled: false` so SOL continues collecting live calibration samples.
- **Why:** The goal is not to starve SOL. The weak current session showed where the damage concentrated, but SOL still needs enough paper/ghost data to learn which prediction families can be converted into winners.
- **Hypothesis:** SOL trade count remains meaningful, but standard-lane drawdowns are capped while calibration and lane attribution accumulate enough evidence to improve the prediction logic.
- **Expected outcome:** Next session should show SOL entries continuing, with smaller per-trade notional on 5m/15m exploration lanes and enough closed samples to compare `standard` vs `spike` families.
- **Actual outcome:** `pending` (need ≥15 closed SOL trades after restart on this config).
- **Status:** `pending`

### 2026-05-25 — Restore SOL protective gates after weak current session

- **Superseded:** Same-day operator feedback clarified the goal is calibration/exploration, not starvation. See the entry above; SOL gates were re-opened with `0.3x` exploratory sizing.
- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), restored `sol_macro.alt_momentum_confirm` to `buy_yes: [15m]` and `buy_no: [1h]`, restored `sol_macro.iql_15m_enabled: true`, and restored shared `updown_composite.default_min_score: 0.62`.
- **Why:** Current paper session `test_20260525_051023` underperformed the May 22 baseline on trade quality and concentrated losses in SOL standard down lanes. SOL total was `3/16`, `-$44.31`; `sol_macro|5m|down|bearish__bearish__bull|standard` was `1/9`, `-$37.57`.
- **Hypothesis:** Restoring the protective gates should reduce repeat SOL standard-down entries, especially 5m bearish/BTC-bull setups that the May 22 baseline already flagged as structurally weak.
- **Expected outcome:** Next session should show fewer SOL 5m standard-down entries and lower SOL drawdown contribution; skip diagnostics should show restored `buy_no_no_alt_momentum_confirm` / `iql_15m_reject` when applicable.
- **Actual outcome:** `pending` (need ≥15 closed SOL trades after restart on this config).
- **Status:** `pending`

### 2026-05-25 — Cap live lane-calibration alpha at identity

- **What changed:** In [src/analysis/lane_calibration.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_calibration.py), `ALPHA_CLAMP_HI` changed from `2.50` to `1.00`. Raw `alpha_ewma` telemetry can still exceed `1.0`, but live calibration can no longer amplify SOL-family probabilities away from 50/50; sub-1 shrinkage remains active.
- **Why:** Session attribution showed `alpha_used > 1.0` was damaging the overall session and alt lanes. This removes calibration amplification from the shared macro path while preserving shrinkage for overpredicted lanes.
- **Hypothesis:** SOL-family drawdown should improve by preventing high-alpha cohorts from receiving larger calibrated edge.
- **Expected outcome:** Next live/non-shadow session should show no effective `alpha_used > 1.0` entries and lower loss concentration in high-alpha macro buckets.
- **Actual outcome:** `pending` (need ≥15 closed SOL-family trades after this change).
- **Status:** `pending`

### 2026-05-21 — SOL-family high-edge cap converted to sizing clamp; oracle basis caps widened

- **What changed:** In [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), the shared SOL-family up/down `max_edge_updown` no longer rejects entries with `edge_above_cap`; it now clamps only the Kelly sizing input and leaves the trade admissible. In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), oracle basis caps were widened for the shared macro family lanes that were repeatedly starved by basis blocks: `sol_macro.oracle_max_basis_bps 10 -> 25` with `oracle_basis_relax_max_bps 15 -> 30`, `xrp_macro 15 -> 20` with relax `15 -> 25`, `doge_macro 10 -> 18` with relax `15 -> 22`, and `bnb_macro 10 -> 18` with relax `15 -> 22`. Backtest parity was updated in [src/backtest/updown_engine.py](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py) so capped-edge trades still enter in simulation.
- **Why:** Recent ghost review showed `edge_above_cap` and tight oracle-basis vetoes were suppressing many of the best-looking SHORT opportunities across SOL/XRP/DOGE/BNB. The evidence fit a risk-control-in-the-wrong-layer problem: cap the size, not the admission, and treat moderate basis dislocation as tradable unless it is truly extreme.
- **Hypothesis:** SOL-family lanes should stop starving on moderate basis spreads and high-edge prints, producing more live candidates while keeping size discipline through Kelly clamping and existing exposure controls.
- **Expected outcome:** `edge_above_cap` should disappear as a skip reason for SOL-family up/down lanes; `oracle_basis_block` frequency should fall materially for SOL/XRP/DOGE/BNB; next review should compare whether added fills improve throughput without degrading expectancy.
- **Actual outcome:** `pending` (need ≥15 closed trades after this change across the affected lanes before judging).
- **Status:** `pending`

### 2026-05-12 — Live↔backtest 1H histogram gate parity (drift fix)

- **What changed:** The backtest `_edge_15m_sol` 1H histogram gate was a hard reject — `if not macd_1h.histogram_rising: return 0.0, 0.0`. Live `sol_macro.py` 15m updown has a **relaxed** gate that blocks ONLY when the 1H histogram is actively against the trade direction (negative-and-falling for LONG). A positive-but-decelerating 1H histogram passed in live but was rejected in backtest. Both now share `alt_1h_hist_gate` in `strategies._core` ([src/strategies/_core/alt_gates.py:34](src/strategies/_core/alt_gates.py:34)).

  Also part of the same refactor pass: shared primitives for `apply_primary_htf_bias`, `sol_rsi_extremes_adj`, `passes_15m_iql`, and `sol_m5_macd_adj` — sol_macro and updown_engine now call the same code for these.

- **Why:** Hand-copied alt-strategy logic between `updown_engine.py` and `sol_macro.py` had drifted; the strict backtest 1H gate was producing artificially low SOL/alt 15m trade counts vs live. Backtest WR was a poor proxy for live behavior on alts as a result.

- **Hypothesis:** SOL/alt backtests will produce more 15m trades during positive-but-decelerating 1H regimes, more accurately reflecting what live actually does. No live behavior change (live already used the relaxed gate).

- **Expected outcome:** SOL 15m backtest trade counts increase modestly; WR shouldn't shift dramatically since the entries that now fire were always entries live considered. Numbers should now move together when comparing backtest to live for the same date range.

- **Actual outcome:** `pending` (need ≥15 closed SOL trades to compare backtest vs live cohort after running fresh backtests).

- **Status:** `pending`

- **Verification:** 314 tests pass; explicit drift-fix test in `tests/test_strategy_core_alt_gates.py::test_long_gate_passes_when_positive_but_decelerating` asserts the relaxed semantics. Commits: `b990f1b`, `c6b84a0`. Merged to main as `7b7f503`.

### 2026-05-09 — Oracle-first + composite score gate for SOL up/down

- **What changed:** `sol_macro` up/down candidates now require fresh Chainlink oracle validation (`require_oracle_for_updown=true`, `oracle_max_age_sec=180`, `oracle_max_basis_bps=10.0`) and pass the shared deterministic composite score before AI/sizing.
- **Why:** SOL-family entries should not reach Kelly sizing solely because edge clears a threshold when oracle basis/freshness or micro/timing quality is weak.
- **Hypothesis:** Weak SOL up/down candidates shift from low-quality entries to explicit `oracle_*` or `composite_score_below_floor` skips.
- **Expected outcome:** SOL skip telemetry includes oracle freshness/basis and composite components; remaining entries have cleaner pre-entry validation.
- **Actual outcome:** `pending` (need ≥15 closed SOL trades after this change).
- **Status:** `pending`

### 2026-05-07 — Full intervention: tighten SOL 5m to require a real BTC catalyst

- **What changed:**
  - [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml)
  - `strategies.sol_macro.require_btc_catalyst_5m: false -> true`
- **Why:** In the active failure run `test_20260507_035930`, every closed SOL trade was a `5m` `BUY_YES` long. The lane was only slightly positive on 7 trades (`+$1.00`), but that came from 3 take profits against 4 losers, and the broader SOL evidence remains structurally bad: latest `SOL 5m` backtest `933` trades / `-486.225`, latest `SOL 15m` backtest `462` trades / `-403.2`.
- **Hypothesis:** Requiring an explicit BTC catalyst on SOL `5m` should suppress the low-conviction continuation longs that currently drift into `updown_time_stop` or real resolution losses, while leaving stronger spike/lag-driven entries available for observation.
- **Expected outcome:** SOL `5m` fire rate drops materially; average loss on the remaining SOL entries improves; if SOL still works at all, it should do so on catalyst-backed setups rather than routine bullish continuation guesses.
- **Actual outcome:** `pending` (current live process has not been restarted onto this config yet).
- **Status:** `pending`

### 2026-05-06 — SOL 15m window widen + 5m edge tighten + IQL floor relax (commit `d6da79c`)

- **What changed:**
  - `entry_window_15m_min`: 2.0 → 1.0 ; `entry_window_15m_max`: 16.0 → 18.0
  - `min_edge_5m`: 0.07 → 0.085 (and `backtest.min_edge_sol_5m` mirror to 0.085)
  - `iql_15m_hist_floor`: 0.15 → 0.10
- **Why:** Live diagnostic showed 15m silent for 8h+ with `outside_entry_window` as dominant skip (11/cycle). Behind that, SOL 15m hits IQL hist floor — 0.15 is calibrated for higher-priced assets and disproportionately strict for SOL at ~$80. Meanwhile 5m was firing actively but bleeding (-$3 avg loss vs +$2 avg win — fragile math).
- **Hypothesis:** Wider window admits the 15m candidates that were missing the 14-min slot due to scan cadence. Lower IQL floor unblocks SOL-scale MACD hist signals. Tighter `min_edge_5m` culls the marginal 5m entries most exposed to resolution variance.
- **Expected outcome:** SOL 15m fire rate ≥2 entries per 12h window. SOL 5m fire rate drops ~15-25%; PnL/WR trends up.
- **Actual outcome:** `pending` (need ≥15 closed trades post-change).
- **Status:** `pending`

### 2026-05-02 — Shared entry-window auto-align cap fix + latency-adjusted mins_left

- **What changed:** [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py): `_resolve_entry_window_bounds()` auto-align no longer clamps the expanded upper bound to fixed 15m / 6m candle widths (YAML `entry_window_*_max` above those widths was ineffective). Optional safety: `entry_window_hard_cap_mins_left` when nonzero. Up/down timing compares minutes remaining minus `entry_window_latency_buffer_sec` before `_within_ai_decision_window`. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.sol_macro`: `entry_window_15m_max` **45.0**, `entry_window_15m_min` **2.0**, `entry_window_latency_buffer_sec` **12**, `ai_entry_window_15m_min` **2.0** (sibling macro blocks updated similarly).
- **Why:** Same failure mode as BTC: listings and cadence meant real `mins_left` often fell outside an effective band capped at 15 minutes regardless of YAML; scan/strategy latency consumes seconds against narrow comparisons.
- **Hypothesis:** SOL macro reaches BTC-move, edge, histogram, correlation, and price-band gates more often without weakening those economics.
- **Expected outcome:** `outside_entry_window` should lose dominance in SOL ops skip mix when markets exist.
- **Actual outcome:** `pending` (minimum ~15 closed SOL macro trades after deploy — use `/api/journal/summary` or equivalent).
- **Status:** `pending`

### 2026-04-30 — Keep 15m anti-LTF threshold at 0.50 and align backtest parity

- **What changed:** Kept live `_check_15m_confirmation` threshold at `0.50` and updated [src/backtest/updown_engine.py](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py) to use the same SOL-family anti-LTF confirmation threshold instead of stale `0.25`.
- **Why:** Focused cached SOL 15m comparison for **2026-01-20 → 2026-04-20** showed threshold `0.50` performed better than `0.35`: `0.50` = 1120 trades, 48.93% WR, `-$284.03`; `0.35` = 1093 trades, 48.49% WR, `-$356.55`. Both were negative, but `0.50` was less bad and better aligned with the live late-entry thesis.
- **Hypothesis:** Backtest/live parity should improve, and the higher anti-LTF threshold should avoid over-classifying mild MACD agreement as "late confirmed" while still catching composite late entries.
- **Expected outcome:** SOL-family backtests now model the live `0.50` anti-LTF gate; do not revert to `0.35` without a broader out-of-sample improvement.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed SOL macro trades after deploy).
- **Status:** `pending`

### 2026-04-30 — Lag staleness persistence and explicit up/down price band

- **What changed:** [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) now persists lag-opportunity detection time on the service instead of the per-call result object, expiring stale lag after 5 minutes unless BTC prints a materially larger same-direction impulse. [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) now uses `entry_price_min` and `entry_price_max` directly for up/down entry bands instead of deriving the lower bound from `1 - entry_price_max`.
- **Why:** The previous staleness check was dead code because each result timestamp was set immediately before checking age. The price-band path only worked by coincidence while min/max were symmetric around 0.50.
- **Hypothesis:** SOL macro should stop repeatedly acting on old BTC impulses while preserving fresh spike opportunities; future asymmetric entry-band tuning should behave as configured.
- **Expected outcome:** Stale lag opportunities are suppressed after 300s without a fresh BTC impulse; default `0.46–0.54` behavior remains unchanged.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed SOL macro trades after deploy).
- **Status:** `pending`

### 2026-04-29 — Phase 3 timezone-safe threshold days

- **What changed:** Changed traditional threshold-market `days_to_resolution` arithmetic in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) to coerce naive market end dates to UTC and compare against `datetime.now(timezone.utc)`.
- **Why:** Phase 3 scan found local naive `datetime.now()` leaking into UTC market-end arithmetic, which can shift day counts around local day boundaries.
- **Hypothesis:** SOL-family threshold-market probability estimates should be stable across host timezone boundaries; short-window up/down markets should be unchanged.
- **Expected outcome:** No behavior change for 5m/15m up/down flow; threshold markets avoid off-by-timezone day buckets.
- **Actual outcome:** `pending` (need ≥15 closed SOL-family threshold-market trades if this path becomes active).
- **Status:** `pending`

### 2026-04-29 — Phase 2 dead-gate and edge cleanup

- **What changed:** Removed redundant BTC HTF suppress branches in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) because `action` is already derived from `primary_htf_bias`; retained the real 1H trend suppress gates. Removed no-op positive-edge normalization in the shared SOL-family edge paths.
- **Why:** The BTC HTF suppress counters could not fire by construction and polluted the skip-funnel vocabulary. The edge normalization line was a no-op and made it look like adverse edge might be transformed when it was not.
- **Hypothesis:** SOL-family telemetry should be cleaner with no loss of actual risk gating, because the remaining 1H trend suppress gates are the operative counter-trend filter.
- **Expected outcome:** `btc_bullish_suppress_short` / `btc_bearish_suppress_long` disappear from expected skip counters; real suppressions continue under `sell_yes_suppressed_bullish_1h` / `buy_yes_suppressed_bearish_1h`.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed SOL-family trades after deploy).
- **Status:** `pending`

### 2026-04-29 — Fixed-cycle scheduler + wider shared macro timing windows

- **What changed:** [src/main.py](/Users/mainfolder/Documents/psb-main%201/src/main.py) now keeps the unified loop on a fixed cadence by subtracting cycle runtime before sleeping. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) widened SOL-family up/down timing windows for SOL/HYPE to 15m `8.0–15.0` and 5m `0.75–5.0` minutes remaining; XRP received explicit matching window config instead of inheriting narrower defaults.
- **Why:** Live ops showed SOL/HYPE/XRP markets arriving but most cycles dominated by `outside_entry_window`; XRP also lacked explicit entry-window config while inheriting the shared `SolMacroStrategy` defaults.
- **Hypothesis:** The shared macro lanes should start reaching the real signal gates more often while preserving edge, price-band, BTC-move, AI, exposure, and sizing checks.
- **Expected outcome:** `outside_entry_window` should stop being the default blocker for SOL/HYPE/XRP. Remaining skips should expose real economics such as `btc_min_move_dollars`, `edge_below_min`, liquidity, price band, or BTC/ALT confirmation gates.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed trades per affected lane after deploy).
- **Status:** `pending`

### 2026-04-28 — Full skip accounting for shared SOL-family market loop

- **What changed:** Added explicit `_bump_skip()` calls to the shared `SolMacroStrategy` market loop for liquidity, entry timing, BTC minimum move, price band, 5m/15m histogram gates, threshold-market guards, AI marginal outcomes, final edge/cap filters, and size-too-small exits. Added `tests/test_sol_macro_skip_accounting.py` as a regression guard.
- **Why:** Ops pulse showed SOL/HYPE/XRP considering markets but reporting `top_skip_reasons={}` because several `continue` paths skipped without incrementing diagnostics.
- **Hypothesis:** SOL/HYPE/XRP cycles with zero signals will now report the real dominant blocker instead of falling back to an empty stats dict.
- **Expected outcome:** Next live run should show populated `top_skip_reasons` for SOL/HYPE/XRP when they pass markets but do not emit signals; strategy economics unchanged.
- **Actual outcome:** `pending` (need live ops pulse after restart).
- **Status:** `pending`

### 2026-04-27 — Asset-specific Chainlink oracle coverage for macro lanes

- **What changed:** [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) now maps each macro asset to its own Chainlink reference feed: SOL/USD, ETH/USD, XRP/USD on Polygon and HYPE/USD on Arbitrum. The shared analysis object now exposes alt-leg oracle price, network, and basis in bps. [src/dashboard/server.py](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py) now returns those fields in the live analysis payload.
- **Why:** BTC already had an oracle reference, but the non-BTC crypto lanes did not. That meant ETH/SOL/XRP/HYPE could not reason about exchange-vs-oracle basis even though short-window Polymarket resolution depends on a reference source.
- **Hypothesis:** Uniform oracle coverage makes the shared macro stack safer and more diagnosable, even before every lane enables a hard basis veto.
- **Expected outcome:** ETH/SOL/XRP/HYPE all surface oracle reference data during live analysis; inherited strategies can opt into basis gating with config.
- **Actual outcome:** `pending` (need live verification after deploy).
- **Status:** `pending`

### 2026-04-27 — Shared AI decision window for macro up/down lanes

- **What changed:** Added a shared AI timing helper in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) so marginal up/down AI calls only fire inside configured `ai_entry_window_*` bands. Added explicit `ai_entry_window_15m_min/max`, `ai_entry_window_5m_min/max`, `ai_hold_veto_ttl_sec`, and `min_edge_5m_ai_override` to the SOL/HYPE/XRP-style config blocks in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml). Because HYPE and XRP inherit `SolMacroStrategy`, they pick up the same timing behavior automatically.
- **Why:** The shared macro family already had AI tie-breakers, but they were not time-scoped to the part of the candle where enough data has formed to make the model useful. This change keeps AI from firing too early in short-window up/down markets.
- **Hypothesis:** Restricting AI to the configured timing band should produce fewer low-information AI calls while preserving the existing quant-first trade flow.
- **Expected outcome:** SOL/HYPE/XRP marginal up/down AI assists happen later in the candle, with no behavior change for non-AI or clearly strong-edge trades.
- **Actual outcome:** `pending` (need live/post-change samples per inherited strategy).
- **Status:** `pending`

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

### 2026-05-07 — Active failure run read: SOL was not the first drag, but it still does not earn a pass

- **Session:** `test_20260507_035930`
- **Closed trades (`sol_macro`):** `7`
- **Net PnL:** `+$1.00`
- **Exit shape:** all `5m` `BUY_YES`; wins were `take_profit`, losses were `3x updown_time_stop` plus `1x RESOLVED:NO`
- **Read:** the small positive result is too thin to override the broader evidence. SOL remains a structurally weak lane, and the right intervention is to tighten `5m` admission rather than blame dead zone or declare recovery.

### 2026-05-04 — Paper `test_20260504_034719`

- **Headline:** Solid session slice: **20** closes, **75%** WR, **+$12.20** — aligns with “macro + LTF” lane behaving vs BTC grind.
- **Action mix:** **BUY_YES** dominated (18 trades, +$14.90); **BUY_NO** only **2** trades (-$2.70) — low n on shorts.
- **Rolling heatmap:** see [`docs/session_reports/hourly_heatmap_20260504_exit_pt.txt`](docs/session_reports/hourly_heatmap_20260504_exit_pt.txt) (SOL has YAML block overlay in script).

## Lessons learned

_(none yet — add only after data)_
