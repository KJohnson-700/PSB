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

### 2026-07-31 — Revert BTC paper sizing to linear Kelly

- **What changed:** Set `strategies.bitcoin.use_true_kelly_sizing: false` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), forcing BTC entries back through `KellySizer.size_from_edge` instead of `size_binary_position`.
- **Why:** Paper session `test_20260731_042952` showed BTC 15m BUY_NO at 0/3, -$19.31, with all three losers being oversold shorts (`RSI 22.4`, `22.5`, `28.3`). The Phase-1 `estimated_prob` path has not proven reliable enough for BTC true binary Kelly sizing.
- **Hypothesis:** BTC 15m BUY_NO losses from miscalibrated oversold-short estimates shrink back toward the prior flat/linear notional range while preserving trade admission for forward evidence.
- **Expected outcome:** BTC entries no longer scale up to the binary Kelly cap from `estimated_prob`; next BTC paper fills should show smaller notional on comparable edge/price setups.
- **Actual outcome:** `pending` — requires at least 15 closed BTC trades after the rollback.
- **Status:** `pending`

### 2026-07-13 — Let BTC 1h UP floor survive MINIMAL sizing brake

- **What changed:** In [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), split per-lane sizing overrides into lift → floor → hard cap. `PAUSED` still blocks all overrides; `MINIMAL` skips lift but can honor `lane_min_notional_ignores_minimal_*`. Config added `bitcoin.lane_min_notional_1h_up: 20` and `bitcoin.lane_min_notional_ignores_minimal_1h_up: true`.
- **Why:** Operator observed known-winner BTC 1h expiry wins being shrunk to ~$5 fills during the loss-streak MINIMAL tier, paying only $3.50/$4.29 instead of the intended ~$14-$17 at a $20 floor.
- **Hypothesis:** BTC 1h UP entries keep known-winner sizing through global MINIMAL without violating the `PAUSED` kill switch.
- **Expected outcome:** BTC 1h UP fills use at least $20 when the per-lane opt-in flag is present, while `lane_max_notional_1h_up: 25` remains a hard cap after lift/floor ordering.
- **Actual outcome:** `pending` — requires restart and at least 15 closed affected BTC 1h UP trades after rollout.
- **Status:** `pending`

### 2026-06-06 — Zero BTC BUY_YES bullish-floor bumps

- **What changed:** `bitcoin.btc_5m_buy_yes_bullish_floor_bump: 0.20 → 0.0` and `bitcoin.btc_15m_buy_yes_bullish_floor_bump: 0.26 → 0.0`.
- **Why:** Live taken-trade evidence showed BTC long bumps fabricating edge under bullish bias; operator rule now keeps only the two proven long-bump winners, HYPE 5m and BNB 5m.
- **Hypothesis:** BTC BUY_YES entries stop relying on straight probability inflation from the bullish floor and require real raw edge from the signal path.
- **Expected outcome:** BTC 5m/15m BUY_YES frequency drops where edge was created only by the bump; accepted longs should have cleaner stated-edge provenance.
- **Actual outcome:** `pending` — requires bot restart and at least 15 closed affected BTC trades after rollout.
- **Status:** `pending`

### 2026-06-06 — Enable BTC 5m bias/quant-disagreement — HIGH-GAP slice only — REVERTED (EV-negative: gap≥.15 avg realized_pct −8.6%/−2.4%; see changelog EV-driven revert)

- **What changed:** `_maybe_bias_quant_disagree_override` ([bitcoin.py](src/strategies/bitcoin.py)) no longer hard-excludes 5m. New BTC config: `bias_quant_disagree_allow_5m: true`, `bias_quant_disagree_5m_min_gap: 0.15`. On 5m the override now admits a disagreement candidate (with the existing `bias_quant_disagree_size_multiplier: 0.33` haircut) **only when `|yes_price − raw_est| ≥ 0.15`**; smaller gaps still hard-reject. Two new `__init__` attrs; new test `test_bitcoin_bias_quant_disagree_override_admits_5m_high_gap_when_enabled`; existing `keeps_5m_strict` still passes (default-off + small-gap).
- **Why:** The 5m exclusion was set on a 2026-05-27 prior that found 5m disagreement **realized-negative** — but that was the *whole* cohort. Fresh ghost (`rejected_candidates_settled`, n=558) splits sharply: `|yes−est|` gap **≥0.15 → 76.6% would-win (n=197)**, 0.10–0.15 → 62%, but the **<0.10 middle → ~40%** (the loser cohort the prior caught). Same split by price (yes≥0.60 → 77%, mid 0.45–0.60 → ~41%). So the lane isn't bad — only the low-conviction middle is. Admitting just the large-gap slice captures the winners and keeps excluding the 40% middle.
- **Hypothesis:** High-gap 5m disagreement admits (small size) add positive-EV BTC 5m flow without re-introducing the realized-negative middle that motivated the original exclusion.
- **Expected outcome:** BTC 5m `lane_min_edge_bias_quant_disagree` skips drop for gap≥0.15; new `bias_quant_override_gap=` reason appears on admitted 5m fills; realized PnL on these should track the ~77% would-win (watch closely — scan-level ≠ realized under stops).
- **Trade-off note:** This is the one lever where scan-ghost and the prior realized finding conflicted, so it's gated tight (gap≥0.15, 0.33× size, opt-in flag). Forward-test only; needs **restart**. Revert = set `bias_quant_disagree_allow_5m: false`. Tests: `test_bitcoin.py` 52 passed.
- **Actual outcome:** `pending`
- **Status:** `pending` — code+config+test, not committed, needs restart. Verify post-restart: admitted 5m disagreement fills are net-positive; if the realized WR underperforms the 77% scan number, raise `5m_min_gap` or flip the flag off.

### 2026-06-06 — Un-starve BTC 5m/1h: loosen 3 winner-blocking gates (ghost-validated) — REVERTED (inert + EV-negative; see changelog EV-driven revert)

- **What changed (config only, `config/settings.yaml`, BTC block):** (1) `bull_regime_buy_no_min_mins_left` 2.0→0.5 and `_15m` 4.0→1.0 — the `bull_regime_late_short` gate. (2) `bias_quant_disagree_aligned_max_gap_non5m` 0.3→0.45 — admits more 15m/1h bias/quant-disagreement candidates via the existing size-haircut override. (3) BTC 1h `min_edge` (up+down) 0.09→0.06 in `entry_policy.window_side_overrides.1h`. 15m and 5m min_edge untouched.
- **Why:** BTC stopped firing like the +$257 baseline (which traded 5m/15m/1h, 92% SHORT, in a *falling* tape). Tape reversed to rising (BULL), so BTC correctly flipped long (c711ad0 fresh-cross, working as intended) — but 5m/1h went silent. **Ghost (`rejected_candidates_settled`, since 2026-06-01) shows the gates eating the flow are blocking winners:** `bull_regime_late_short` rejects settle **82%** (5m, n=354) / **80%** (1h); `lane_min_edge_bias_quant_disagree` settle **56%** (5m, n=558) / **86%** (1h, n=72); plain `lane_min_edge` settle **58%** (5m) / **92%** (1h, n=91). The bull-regime late-short time gate and the high 1h min-edge bar were the biggest 1h winner-blockers. `bull_regime_expensive_short` left ON (ghost 28% — correctly blocks losers).
- **Hypothesis:** Loosening these three (all LOOSENING, no new gates) restores BTC 5m short flow + 1h frequency without forcing wrong-way shorts (c711ad0 not reverted — that would short the rising tape).
- **Expected outcome:** BTC 1h trades resume; `bull_regime_late_short` and 1h `lane_min_edge*` skip counts drop sharply; BTC window mix returns toward baseline (5m/15m/1h) instead of 15m-only.
- **NOT done (flagged):** BTC **5m** bias/quant-disagreement stays hard-rejected (`_maybe_bias_quant_disagree_override` excludes 5m in code, [bitcoin.py:544](src/strategies/bitcoin.py:544)). Ghost is only 56% and a documented prior (AGENT_CHANGELOG 2026-05-27) found 5m disagreement **realized-negative** — enabling it needs a code change + realized-PnL check ghosts can't give. Left for user decision.
- **Trade-off note:** Forward-test only (admission changes ARE ghost-validatable, but realized PnL under live stops/sizing isn't); needs bot **restart**. BTC-only knobs, no alt blast radius. Tests: `test_bitcoin.py` 51 passed.
- **Actual outcome:** `pending`
- **Status:** `pending` — config-only, not committed, needs restart. Verify post-restart: BTC 1h entries appear; `bull_regime_late_short`/`lane_min_edge_bias_quant_disagree` skip counts fall.

### 2026-06-06 — 15m BUY_NO hold-to-resolution — PROPOSED then REVERTED (same day)

- **REVERTED same day, never ran.** User chose to test the all-trade-hold (stop_pct: 0) behavior on the **1h lane only** first before expanding. The 15m override was removed; BTC 15m BUY_NO is back on the global stop (0.20, with TP). Re-apply only after the 1h forward-test reads positive. The analysis below stands as the case for re-applying later.
- **What was changed (then reverted):** `exit_rules.updown_overrides.bitcoin.window_lane_overrides.15m.down`: `updown_hold_winners_to_resolution: true` + `updown_stop_loss_pct: 0.0` (same shape as the 1h.down fix). BTC 15m BUY_YES and all other lanes untouched.
- **Why:** Settled-exit audit (`data/calibration/trades_settled.jsonl`, n=71 BUY_NO, split by `exit_reason`) — biggest sample in the dataset: `updown_stop_loss` leg (n=35) realized **−$117.23** vs held **−$30.16** (gap **+$87**, 13/35 recover to a win); `take_profit` leg (n=36) realized +$190.64 vs held +$268.18 (gap +$78). Full-hold counterfactual = **+$238 vs +$73 realized**. The % stop is firing at the collapse and the price then recovers (consistent with the once-per-cycle stop firing deep). Same mechanism as the 1h fix.
- **Hypothesis:** Disabling the % stop + relaxing the TP recovers most of the +$164 lane gap; the late-window cents/time stop remains as the loser floor.
- **Expected outcome:** BTC 15m BUY_NO `updown_stop_loss` exits drop sharply; lane realized PnL moves toward the held-counterfactual.
- **Trade-off note:** No early % backstop — adverse 15m moves ride to the late-window stop / binary zero (max loss = stake). Forward-test only (exit change, not ghost-validatable); needs bot **restart**. Part of the per-lane SL/TP calibration pass (15m). Other 15m lanes left as-is: eth/hype BUY_NO/BUY_YES holding *hurts* (negative gap — keep tight); bnb 15m BUY_NO already holds winners (2026-06-04); rest n<20.
- **Actual outcome:** n/a — reverted before any run.
- **Status:** `REVERTED` (same day) — 1h-only test first; re-apply 15m if 1h forward-test is positive.

### 2026-06-06 — 1h BUY_NO: hold to resolution (disable the % stop) — REVERTED (regime mismatch: recovery audit was falling tape, now rising; see changelog EV-driven revert)

- **What changed:** Added `exit_rules.updown_overrides.bitcoin.window_lane_overrides.1h.down` in `config/settings.yaml`: `updown_hold_winners_to_resolution: true` + `updown_stop_loss_pct: 0.0`. Disables the early take-profit *and* the percentage stop for BTC 1h BUY_NO only; the late-window cents/time stop (`updown_stop_cents` / `updown_exit_window_mins`) stays as the loser floor near expiry. BTC 1h BUY_YES and all other windows untouched.
- **Why:** Settled-exit audit (`data/calibration/trades_settled.jsonl`, n=22 BUY_NO, split by `exit_reason`): the `updown_stop_loss` leg (n=10) realized **−$23.47** but the same trades held to resolution net **+$2.02** — **4 of 10 stop-outs recover to a WIN by resolution**. The TP leg (n=12) realized +$53.94 vs held +$62.81. The global 20% stop (no BTC 1h override existed) was cutting trades a 1h market had time to recover. Triggered by a live BTC 1h BUY_NO stop-out (−$4.03 via `updown_stop_loss`, ~4 min after entry) on 2026-06-06.
- **Mechanism note:** `updown_hold_winners_to_resolution` alone only disables the TP ([live_testing.py:305](src/execution/live_testing.py:305)); the stop is a separate gate, so `updown_stop_loss_pct: 0.0` is required to let the stopped-out losers recover. Verified via `resolve_updown_exit_params_for_position` → bitcoin/NO/1h returns hold=True, stop=0.0.
- **Hypothesis:** Removing the % stop on this lane recovers ~+$25 (the stop-leg gap) and the TP relax recovers ~+$9, roughly doubling lane PnL (+$30 → +$65 over a comparable sample), with WR unchanged (held_wr == realized_wr == 54.5%).
- **Expected outcome:** BTC 1h BUY_NO `updown_stop_loss` exits disappear from `trades_settled`; positions ride to resolution (or the late-window cents stop); lane realized PnL rises toward the held-counterfactual.
- **Trade-off note:** No early % backstop on this lane — a hard adverse 1h move now rides to the late-window stop / binary zero (max loss = stake, bounded). Forward-test only (exit change, not ghost-validatable); needs bot **restart** to load. Part of the per-lane SL/TP calibration pass (1h first).
- **Actual outcome:** `pending`
- **Status:** `pending` — config-only, not committed, needs restart. Watch `trades_settled.jsonl` BTC 1h BUY_NO exit reasons + realized vs held.

### 2026-06-01 — Decisive AI prompt (kill the HOLD default)

- **What changed:** Rewrote the shared decision/analysis system prompt ([`ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py) `SYSTEM_PROMPT`): removed the "be conservative — markets overestimate" inaction bias, reframed HOLD as requiring a *specific, evidence-based* reason (never a default for uncertainty), told it to commit to a direction on any real lean, and clarified `confidence_score` = strength of the directional evidence. Bumped `prompt_version` → `lane-feedback-v2-decisive` so the settler can split pre/post verdicts.
- **Why:** Diagnosis showed the AI returned **HOLD on 77%** of responses and approved only **3%** — the prompt itself was steering the model toward inaction on near-coin-flip 15m/1h markets, which the veto-only marginal change alone does not fix.
- **Hypothesis:** A decisive prompt cuts spurious HOLDs and surfaces more directional calls (and more *confident-opposition* vetoes), making the gate a real tiebreaker rather than a near-total veto.
- **Expected outcome:** HOLD share drops well below 77%; more BUY_YES/BUY_NO verdicts; gate approval rate rises off 3%.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only; needs bot restart. Watch HOLD% / approval% in `decision_layer.jsonl` under prompt_version `lane-feedback-v2-decisive`.

### 2026-06-01 — Marginal lane → AI veto-only (unblock 15m/1h)

- **What changed:** Also fixed BTC's AI decision timeout: it was silently falling back to the legacy **15s** (`ai_call_timeout_sec`) while MiniMax answers in ~22s, so *every* BTC decision-layer call timed out — added `ai_decision_timeout_sec: 40` to the BTC config block. The marginal-lane AI gate flipped from fail-closed to **veto-only**: the AI can now only REJECT a below-threshold candidate with a *confident, directly-opposing* directional call (conf ≥ `decision_layer.min_confidence`). HOLD / SKIP / low-confidence / agreement fall back to the quant trade. Central change in [`evaluate_trade_decision`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py) (new `veto_only` param) threaded through the marginal call site(s); guarded the redundant local re-checks; opt-out `decision_layer.marginal_veto_only`.
- **Why:** Over a 5.5h window (`decision_layer.jsonl`) the gate approved only **3%** of AI-evaluated candidates — the model returned **HOLD 77%** of the time (conservative system prompt + a 0.60 confidence bar that near-coin-flip 15m/1h markets can't honestly clear), and HOLD was a hard veto. BTC's gate had effectively never run (100% timeout at 15s); after the timeout fix the marginal lane becomes live and veto-only.
- **Hypothesis:** Restoring marginal admission (blocked only on confident AI opposition) reopens 15m/1h frequency without losing the AI's ability to stop a conviction-wrong trade.
- **Expected outcome:** BTC 15m/1h marginal entries resume; AI still vetoes confident-opposite cases.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only (AI-gate behavior is not ghost-validatable); needs bot restart to load.

### 2026-06-01 — BUY_YES attribution hook, BTC report-only

- **What changed:** Wired BTC through the shared BUY_YES lane-repair helper, but added no active BTC repair rule. The past-3-day BTC `15m htf_bullish_side_long` cohort stays report-only.
- **Why:** Attribution showed BTC `15m htf_bullish_side_long` had low WR but positive PnL and average raw probability below average YES price, so it did not match the overconfidence failure found in ETH/XRP/HYPE.
- **Hypothesis:** BTC should not be penalized until a causal false-positive pattern is identified; the hook exists only for future soft repairs.
- **Expected outcome:** No BTC behavior change from this entry.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-06-01 — Revert BUY_YES WR-mode family allowlist

- **What changed:** Reverted the same-day BTC `buy_yes_wr_mode` code/config clamp. BTC BUY_YES is no longer blocked by a family allowlist just to lift reported WR.
- **Why:** Operator rejected the approach as over-tightening and not a real correction. The failure to hit the `55%` minimum / `62%` goal needs feature, calibration, and entry-quality repair, not scoreboard pruning.
- **Hypothesis:** Restoring admission keeps BUY_YES sample collection intact while the next review attributes false positives by probability construction, entry family, price, oracle basis, and regime context.
- **Expected outcome:** No new `buy_yes_wr_mode_family_block` rejects; BTC BUY_YES resumes prior gating behavior.
- **Actual outcome:** `pending`
- **Status:** `reverted ❌`

### 2026-06-01 — BUY_YES WR-mode family allowlist

- **What changed:** Added `buy_yes_wr_mode` to BTC and [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), allowing BTC BUY_YES only for the `15m` `spike` family; `5m` and `1h` BUY_YES are blocked by the WR-mode family gate.
- **Why:** Past-3-day BUY_YES review (`2026-05-29 00:00 PT`–`2026-06-01 00:00 PT`) showed broad BTC BUY_YES at `107` trades / `43.0%` WR, while `bitcoin|15m|up|bullish|spike` was `7` trades / `57.1%` WR. Generic `htf_bullish_side_long` was `72` trades / `37.5%` WR, below the operator's `55%` minimum and `62%` goal.
- **Hypothesis:** BTC BUY_YES count falls sharply, but admitted BUY_YES shifts from generic bullish bias toward the only recent BTC family above the minimum WR bar.
- **Expected outcome:** New rejected candidates include `buy_yes_wr_mode_family_block`; post-change BTC BUY_YES WR should be reviewed after at least 15 closed BTC BUY_YES entries.
- **Actual outcome:** `pending` (configuration/code change only; needs live post-change sample)
- **Status:** `pending`

### 2026-05-31 — Disable counter-trend BUY_NO (shorting into bullish HTF)

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) set bitcoin `disable_buy_no_counter_trend: true`. The flag and code already existed at [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py) `_resolve_btc_direction`; flipping it stops the `btc_bull_rollover_countertrend` short (BUY_NO when HTF is BULLISH but 4H MACD histogram is declining) and, via the shared guard, the quant-disagree SHORT flip. Commit `5d8cbc0`.
- **Why:** Held-to-resolution analysis of `data/calibration/trades_settled.jsonl` (n=632): BUY_NO while `primary_htf_bias=BULLISH` settled at 35% WR (n=88, all bitcoin); `conflict_type=bullish_htf_rollover_down` at 34% (n=53). Shorting into a bullish HTF is anti-predictive — below coin flip.
- **Hypothesis:** BTC stops manufacturing sub-coin-flip counter-trend shorts; BUY_YES (~48%) and bias-aligned BEARISH shorts are unaffected.
- **Expected outcome:** `btc_bull_rollover_countertrend` / `bullish_htf_rollover_down` BUY_NO entries vanish; BTC BUY_NO held-WR rises from 36%.
- **Actual outcome:** `pending` (needs bot restart + ~15 closed post-change BTC trades)
- **Status:** `pending`

### 2026-05-26 — Timeframe-scoped entry control config

- **What changed:** Moved BTC entry-control thresholds and windows from legacy flat timeframe keys (`min_edge_5m`, `entry_window_*`) into canonical `defaults` / `by_tf` config, and routed BTC entry policy reads through the shared timeframe resolver.
- **Why:** Static config audit showed the same 5m/15m values duplicated in flat keys and lane policy overrides, making it unclear which tuning surface was authoritative.
- **Hypothesis:** BTC 5m/15m/1h tuning changes should stay scoped to their `by_tf` cell with no cross-timeframe bleed.
- **Expected outcome:** Startup logs should show BTC `by_tf` overrides; focused tests should preserve the same effective min-edge/window values.
- **Actual outcome:** `pending` (config migration only; need >=15 closed BTC trades before performance judgment).
- **Status:** `pending`

### 2026-05-26 — Hold up/down winners to resolution

- **What changed:** Enabled `trading.exit_rules.updown_hold_winners_to_resolution` and suppressed up/down `take_profit` exits while that flag is true.
- **Why:** The audit showed early `take_profit` wins were much smaller than real resolution wins. This targets exit behavior directly instead of changing sizing.
- **Hypothesis:** Correct BTC trades should realize closer to binary-resolution payoff when held through settlement.
- **Expected outcome:** Fewer `take_profit` exits, more `RESOLVED:* (real)` exits, and higher avg-win dollars.
- **Actual outcome:** `pending` (need >=15 closed BTC trades after restart).
- **Status:** `pending`

### 2026-05-26 — Remove post-May-22 lane-size haircut

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), removed the `0.3x` lane `size_multiplier` that had been added to BTC 5m and 15m up/down entry policies after the May 22 baseline.
- **Why:** Current session attribution showed the regression was in per-trade economics, not BUY_YES/BUY_NO mix. The blanket haircut directly reduced realized winner dollars while leaving the same NO-heavy direction mix in place.
- **Hypothesis:** BTC average winner size should move closer to the May 22 baseline while existing Kelly/risk caps still bound notional.
- **Expected outcome:** Next BTC paper sample should show no `lane_size=0.30x` tag from 5m/15m BTC lane policy and should recover avg win/loss ratio if entry quality is otherwise comparable.
- **Actual outcome:** `pending` (need >=15 closed BTC trades after restart).
- **Status:** `pending`

### 2026-05-25 — Cap live lane-calibration alpha at identity

- **What changed:** In [src/analysis/lane_calibration.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_calibration.py), `ALPHA_CLAMP_HI` changed from `2.50` to `1.00`. Raw `alpha_ewma` telemetry can still exceed `1.0`, but live calibration can no longer amplify BTC probabilities away from 50/50; sub-1 shrinkage remains active.
- **Why:** Session attribution showed `alpha_used > 1.0` was damaging the overall session and alt lanes, while BTC 5m was the only clear beneficiary. The global clamp tests whether one-sided shrinkage improves expectancy without letting calibration over-size bad confidence.
- **Hypothesis:** BTC may lose some beneficial 5m amplification, but cross-lane drawdown should improve because calibration stops turning overconfident posteriors into larger entry edges.
- **Expected outcome:** Next live/non-shadow session should show no `alpha_used > 1.0` effective calibration, lower damage from high-alpha cohorts, and BTC 5m should be reviewed separately before restoring any per-lane amplification.
- **Actual outcome:** `pending` (need ≥15 closed BTC trades after this change).
- **Status:** `pending`

### 2026-05-21 — BTC high-edge cap moved from admission veto to sizing clamp

- **What changed:** In [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), `max_edge_updown` no longer rejects BTC up/down entries with `edge_above_cap`. BTC now keeps the trade admissible and clamps only the Kelly sizing input to the configured edge cap (`size_edge_cap=...` in reason/log context). Backtest parity was updated in [src/backtest/updown_engine.py](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py) so simulated BTC entries now behave the same way.
- **Why:** Ghost review showed the cap was acting as an entry blocker on some of the strongest-looking and correctly directed BTC shorts rather than as a true quality filter. High edge should change position size, not automatically veto the trade.
- **Hypothesis:** BTC should admit more high-conviction 5m/15m/1h setups while still preventing oversized Kelly sizing when the probability model prints extreme edge.
- **Expected outcome:** `edge_above_cap` should disappear from BTC skip telemetry; BTC signal counts should rise in the previously blocked high-edge bucket; PnL impact depends on whether those high-edge ghosts continue to resolve favorably out of sample.
- **Actual outcome:** `pending` (need ≥15 closed BTC trades after this change).
- **Status:** `pending`

### 2026-05-12 — Dashboard Live tab: stacked crypto layout + less poll flicker (UI only)

- **What changed:** Repository dashboard stacks live crypto cards (full-width column), coalesces live ticker refresh, and dedupes some Command Center DOM updates. **No** `bitcoin` strategy code or `config/settings.yaml` changes in the same ship bundle.
- **Why:** Operator readability and less visual churn on `/` Live view.
- **Actual outcome:** n/a
- **Status:** confirmed (display only — see `projects/polymarket-bot/changelog.md` § 2026-05-12 — Dashboard UX).

### 2026-05-12 — Live↔backtest BTC drift fixes (3) + LEAN candle-momentum tier

Refactor of the entry-decision logic in `src/strategies/_core/` unified live and backtest scoring across BTC paths. While extracting, three BTC drift bugs were found and fixed; one undercoded producer tier was added.

- **What changed:**
  - **Drift fix #1 — BTC 15m LTF threshold:** backtest `_ltf_strength` used `confirmed = s >= 0.35`, live used `>= 0.50`. A pure-bull-cross signal (score 0.40) confirmed in backtest but rejected in live. Both now use 0.50 ([src/strategies/_core/ltf_strength.py:42](src/strategies/_core/ltf_strength.py:42)).
  - **Drift fix #2 — BTC 5m 4H/1H histogram gate:** backtest `_edge_5m_btc` had a hard 4H reject; live has a 1H momentum-recovery fallback. Backtest was rejecting entries live takes during 4H-decel/1H-build windows. Both now share `btc_5m_4h_1h_hist_gate` ([src/strategies/_core/htf_boost.py:48](src/strategies/_core/htf_boost.py:48)).
  - **Drift fix #3 — BTC 15m HTF boost floor:** live had no floor on the graduated boost. When HTF=BULLISH was decided via recovery/early-bull votes (sabre=-1 + hist>0 below zero), the raw 3-vote lookup could yield a NEGATIVE htf_boost, contradicting the HTF decision. Backtest had a ±0.03 floor for this; live now applies it too ([src/strategies/_core/htf_boost.py](src/strategies/_core/htf_boost.py)).
  - **Code-correctness fix — LEAN tier:** `btc_price_service.calc_candle_momentum.m5_direction` only emitted SPIKE/DRIFT/empty, but `BitcoinStrategy`'s 5m path had `LEAN_UP/LEAN_DOWN` handler cases with ±0.01 weights that never fired. Added LEAN tier at `|move| > 0.01%` to producer + backtest replay. Live BTC 5m now gets the ±0.01 weak-nudge it always intended.

- **Why:** `src/backtest/updown_engine.py` was a ~2000-line hand-copy of live strategy logic. Drift between the two hand-copied implementations had accumulated silently — three of these biased backtest WR vs live in different directions, making backtest numbers unreliable as a live predictor.

- **Hypothesis:** BTC backtest results post-fix should track live more closely. Drift fix #1 will lower backtest 15m trade counts (some pure-bull-cross entries no longer confirm). Drift fix #2 will raise backtest 5m trade counts (1H-recovery entries that were dropped now fire). Drift fix #3 changes **live** behavior for BTC 15m: recovery/early-bull HTF=BULLISH windows now produce positive htf_boost instead of zero/negative — should produce more BTC 15m entries during recoveries. LEAN tier change is small (±0.01 adjustment in a sliver of the m5 spectrum).

- **Expected outcome:** Live BTC 15m WR during recovery/early-bull periods should not regress; trade counts should rise slightly during those regimes. Backtest BTC 5m / 15m WR should align with live within statistical noise (rather than the +5%-ish overstatement the LTF threshold gap caused). LEAN tier impact will be invisible in aggregate but observable in journal entries with `5m_mom=LEAN_*`.

- **Actual outcome:** `pending` (need ≥15 closed BTC trades after the live changes #3 and LEAN tier take effect — requires bot restart).

- **Status:** `pending`

- **Verification:** 314 tests pass including new parity tests in `tests/test_strategy_core_*.py` that lock live and backtest wrappers to identical output. Commits: `c62d778`, `3c21d3b`, `1fac82d`, `78b43bd`, `fe586da`, `36cd08d`, `bd40be6`, `be1a916`, `77b0e2a`. Merged to main as `7b7f503`.

### 2026-05-09 — Enforce composite + AI/shadow approval on BTC neutral 15m

- **What changed:** `bitcoin` 15m `HTF=NEUTRAL` up/down entries now must clear `neutral_15m_min_composite_score=0.68` and direct AI approval before Kelly sizing; `neutral_15m_requires_shadow_portfolio=true` requires the shadow portfolio to match as well. The existing `min_edge_15m_neutral=0.12` and low-confidence AI path remain in place.
- **Why:** The bad BTC lane was neutral 15m quant-only BUY_YES. Raising edge alone still allowed strong-looking but weak-context quant candidates to size without independent validation.
- **Hypothesis:** Neutral 15m entries become rare and evidence-backed; BTC 5m remains unaffected.
- **Expected outcome:** Post-change journal should show no BTC neutral 15m `ai_used=false` entries, and skipped candidates should surface `composite_score_below_floor` or `ai_decision_*` reasons.
- **Actual outcome:** `pending` (need ≥15 closed BTC 15m trades after this change).
- **Status:** `pending`

### 2026-05-09 — BTC 15m NEUTRAL edge floor to force AI/veto

- **What changed:** Added `strategies.bitcoin.min_edge_15m_neutral=0.12` and `neutral_15m_min_quant_confidence=0.58` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml). [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py) now applies that edge floor only to BTC 15m up/down markets when HTF bias is `NEUTRAL`; marginal entries below the floor must pass the existing AI up/down assist window or skip. It also applies a jmazzini-style composite confidence guard: low-confidence neutral BTC 15m entries must get AI confirmation even if raw edge clears the floor.
- **Why:** Latest paper session `test_20260509_022248` showed BTC 15m `BUY_YES` as the main current damage lane: `11` closes, `36.4%` WR, `-$8.52`, all `HTF=NEUTRAL`, `ai_used=false`, average confidence about `0.53`, and average edge about `0.11`. The prior neutral-RSI patch would not catch most of this cohort because the losing entries were often RSI `59-64`, not only `45-55`.
- **Hypothesis:** BTC 15m neutral-bias coin flips should either receive an AI confirmation/veto in the configured late-entry window or be skipped, matching the external-repo lesson that composite confidence must be an entry gate rather than a log-only field. BTC 5m remains unchanged because the same session showed BTC 5m positive (`24` closes, `58.3%` WR, `+$4.17`).
- **Expected outcome:** Post-restart BTC 15m `HTF=NEUTRAL`, `ai_used=false`, low-confidence entries should drop sharply; BTC 15m net PnL should stop being the main negative lane without reducing BTC 5m participation.
- **Actual outcome:** `pending` (need ≥15 closed BTC 15m trades after this change).
- **Status:** `pending`

### 2026-05-08 — Neutral-RSI up/down edge penalty

- **What changed:** Added configurable BTC neutral-RSI penalties in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml): `neutral_rsi_min=45.0`, `neutral_rsi_max=55.0`, `neutral_rsi_extra_min_edge=0.02`. [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py) now adds that penalty to the effective up/down edge floor when BTC RSI is in the configured neutral band.
- **Why:** Current paper session `test_20260508_050455` showed BTC RSI `45-55` at `16` closes, `25%` WR, `-$14.12`, while RSI `<45` was `16` closes, `75%` WR, `+$13.68`. The loss pattern was context-specific, not solved by simply raising all BTC edge floors.
- **Hypothesis:** Requiring an extra `0.02` edge in neutral RSI should suppress flat/chop BTC entries while preserving stronger non-neutral momentum and mean-reversion setups.
- **Expected outcome:** Post-restart BTC entries with RSI `45-55` should drop materially, BTC WR should improve above the current `50%`, and BTC net PnL should stop hovering near flat/negative.
- **Actual outcome:** `pending` (need ≥15 closed BTC trades after session `test_20260508_151000` restart).
- **Status:** `pending`

### 2026-05-06 — Pre-restart review: 50% WR is config-reload-lag artifact, not regression (commit `d6da79c`)

- **What changed:** No BTC config or code changes in this entry. Recorded as a review note documenting that the dashboard-visible ~50% WR for BTC 5m is a stale-config artifact, not a regression.
- **Why:** User flagged BTC 5m at ~50% WR despite the May-4 patches (`disable_buy_no_counter_trend: true` + `min_edge_buy_no: 0.11`). Investigation showed the patches were committed at 14:55:46 UTC May 4 but the running paper-trading process did NOT hot-reload — 74% of BUY_NO entries in early-post-commit tests fired below the 0.11 edge floor (i.e. running on stale cached config). The cleanest post-fix sample (`test_20260505_044854`, after a process restart) hit **70.7% WR / 41 trades / 17 BUY_NO**. The dashboard ~50% number averages stale-config and clean-config periods.
- **Hypothesis:** A clean restart loads the May-4 BTC fixes alongside this session's commit `d6da79c` and the 70.7% pattern resumes.
- **Expected outcome:** Within 1h of restart, BTC 5m BUY_NO entries fire only at edge ≥0.11. Within 12h, zero `counter_trend=btc_4h_hist_declining` tags. Within 24h, BTC 5m WR trends toward 70.7%.
- **Actual outcome:** `pending` (validation requires post-restart 24h sample).
- **Status:** `pending`
- **Failure criteria → escalate:** if BUY_NO entries below 0.11 appear post-restart, the YAML reload path has a code-level bug. If 24h sample still ~50% with no counter-trend tags, escalate to non-counter-trend loss-mode investigation.

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

### 2026-05-18 — Ghost calibration follow-up: BTC 1H short histogram gate is the main pending loosen candidate

- **Headline:** Settled ghost data currently points to the BTC `1h` short histogram reject as the clearest next BTC calibration move, but this is logged as a future candidate only until more resolved samples accumulate.
- **Evidence snapshot:** `bitcoin|1h|BUY_NO|hist_gate_1h_short_reject` in the settled ghost report showed `n=885`, `74.9%` WR, `total_realized_pct=+444.68`, `net_gate_value_pct=-444.68`. Lane view for `bitcoin|1h|down|bearish|rejected` showed `n=701`, `74.0%` WR, `total_realized_pct=+347.89`.
- **Possible next move after more data:** If the same pattern holds on the next settled batch, loosen the BTC 1H short histogram threshold rather than broad BTC entry gates.
- **Do not infer from this note:** This does **not** support broad loosening of BTC price-band or entry-window gates by itself; the strongest current BTC ghost signal is specific to the 1H bearish histogram reject path.

### 2026-05-07 — BTC 15m backtest admission parity patch validated

- **Headline:** BTC 15m backtest now produces real test-window trades after adding timing bonus and 1H recovery parity.
- **What changed in the backtest:** [`src/backtest/updown_engine.py`](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py) now mirrors live BTC 15m timing bonuses (`15m` / `5m` early momentum + prediction window) and the live 1H recovery pass when 4H histogram is decelerating against the trade.
- **Result:** Rerun [`backtest_crypto_BTC_15m_20260507_134222.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_BTC_15m_20260507_134222.json) produced `82` trades, `68.3%` WR, `+$325.05`, where the earlier same-window rerun had `0` trades.
- **Meaning:** The earlier BTC 15m backtest was under-representing the live admission path. This fixes a real parity defect, but it does not yet prove the live BTC 15m lane is healthy.

### 2026-05-07 — Active paper failure session `test_20260507_035930` + backtest parity review

- **Headline:** BTC is still the main damage lane in the active run, but the more important finding is that live and backtest are not parity-comparable right now.
- **Session result:** `37` closed BTC trades in the active session, about `-37.2` net by summary. Realized losses are dominated by `updown_time_stop`, especially `5m`.
- **Parity finding:** Live BTC is losing on path-dependent exits while the crypto up/down backtest had still been validating mostly settlement outcomes. Fresh forensic cut on the active session: `BTC 5m` time-stops `12` exits for `-39.25`; `BTC 15m` time-stops `4` exits for `-11.52`.
- **Implication:** Strong BTC 5m backtests from the prior methodology overstated live viability. Treat the next BTC backtests after the 2026-05-07 exit-parity patch as the new baseline; do not compare them directly to older settlement-only runs.

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
