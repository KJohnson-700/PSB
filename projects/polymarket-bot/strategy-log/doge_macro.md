# DOGE macro (`doge_macro`)

DOGE **Up or Down** — inherits shared `SolMacroStrategy` signal path with DOGE market detection and `DOGEUSDT` spot leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed 1h trades | 1 | `data/calibration/trades.jsonl` |
| 1h Win rate | 0.0% | same |
| 1h Net PnL | -$6.19 | same |

## Change Log

### 2026-06-15 — Add lane-local stop cooldowns for DOGE BUY_YES bleed

- **What changed:** Added `lane_stop_halt` config support and enabled `doge_macro|5m|BUY_YES`, `doge_macro|15m|BUY_YES`, and `doge_macro|1h|BUY_YES` with 10-15 minute cooldowns after `updown_stop_loss`.
- **Why:** Running session `test_20260615_031614` showed DOGE BUY_YES at `14.3%` WR and `-$29.12` across 7 exits. Replay of the current session snapshot showed the new cooldown would have blocked 1 exited DOGE 5m BUY_YES entry with `-$2.94` realized PnL; the other DOGE lanes are included because their current closed sample was also negative and stop-led.
- **Hypothesis:** DOGE long lanes should pause briefly after a stop to avoid repeated bullish continuation entries during a failed DOGE micro-regime, while DOGE BUY_NO remains untouched.
- **Expected outcome:** Fewer repeated DOGE BUY_YES stop-losses; no change to DOGE BUY_NO admission.
- **Actual outcome:** `pending` — requires restart and at least 15 closed DOGE BUY_YES trades after rollout.
- **Status:** `pending`

### 2026-06-12 — Targeted 1h composite floor to relieve starvation

- **What changed:** Added `updown_composite.strategy_window_min_scores.doge_macro.1h: 0.50` and taught the shared scorer to apply strategy/window composite floor overrides.
- **Why:** Overnight diagnostics showed DOGE 1h candidates still reaching the composite gate and skipping on `composite_score_below_floor`. Settled DOGE 1h ghosts favored BUY_YES cohorts while BUY_NO buckets stayed weak/negative, so this targets 1h admission without reopening DOGE 1h shorts by side-specific gate.
- **Hypothesis:** DOGE 1h long-side candidates can enter instead of starving on the global 0.62/0.66 composite floor.
- **Expected outcome:** DOGE 1h starvation decreases; weak BUY_NO cohorts remain governed by existing edge/side gates.
- **Actual outcome:** `pending` — requires restart and at least 15 closed DOGE 1h trades after rollout.
- **Status:** `pending`

### 2026-06-12 — Let 5m BUY_NO inversion flip run before suppression

- **What changed:** In [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), `disable_buy_no_5m_native` now suppresses native 5m `BUY_NO` only when `buy_no_5m_flip_to_yes` is false. DOGE keeps `disable_buy_no_5m_native: true` and `buy_no_5m_flip_to_yes: true`, so DOGE 5m native shorts can reach the intended inversion flip instead of being logged as `buy_no_5m_native_suppressed`.
- **Why:** Current config intended to flip anti-selective DOGE 5m shorts to the long side, but suppression happened earlier in the shared scan path and prevented the rescue. Settled ghosts for `doge_macro|5m|buy_no_5m_native_suppressed` showed WR 55.2% on 6,988 rows with positive counterfactual value.
- **Hypothesis:** DOGE 5m activity shifts from `buy_no_5m_native_suppressed` rejects toward flipped `BUY_YES` candidates without reopening native short exposure.
- **Expected outcome:** DOGE 5m fills tagged with `buy_no_5m_to_yes_flip` appear; `buy_no_5m_native_suppressed` declines for DOGE while strategies without the flip can still suppress native shorts.
- **Actual outcome:** `pending` — requires restart and at least 15 closed DOGE 5m flipped trades after rollout.
- **Status:** `pending`

### 2026-06-07 — 1h BUY_YES price-banded floor bump

- **What changed:** DOGE 1h: `1h_buy_yes_bullish_floor_bump: 0.30`, band **0.58–0.88** (via the new shared `_alt_buy_yes_bullish_floor_bump` price-band guard, `sol_macro.py:2129`).
- **Why:** DOGE 1h longs reject on `lane_min_edge` from negative model-edge (`est_prob_up ≈0.60 < yes_price`). +EV across 0.60–0.90 (+20–35% held EV, 93% WR, ghost n=125); weak at 0.50–0.58 (+2%), so band starts at 0.58. 0.90+ trap excluded. Pairs with the 1h liquidity floor 250→100 (ee926f2).
- **Hypothesis:** Banded bump admits the 0.58–0.88 +EV 1h-long cohort.
- **Expected outcome:** DOGE 1h BUY_YES entries appear.
- **Actual outcome:** `pending` (≥15 closed). Hold-to-resolution caveat; forward-test only.
- **Status:** `pending` — needs restart. codex re-derived the band.

### 2026-06-07 — Oracle basis cap 18→25 / relax 22→40 (DOGE was tightest of 7 assets) — REVERTED SAME DAY

> **STATUS: REVERTED to HEAD (oracle_max_basis_bps back to 18).** Bundled into a session the user reverted; oracle is only DOGE's 4th blocker (median basis 6.9 bps, 87% pass) so low priority. Re-propose standalone if desired. Analysis below retained for reference.


- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), DOGE `oracle_max_basis_bps` 18.0→25.0 and `oracle_basis_relax_max_bps` 22.0→40.0 (`oracle_stale_basis_relax_max_bps` left at 75.0).
- **Why:** DOGE had the single tightest oracle basis cap of all 7 assets (18) despite being the only penny-priced asset, which structurally runs the widest Binance/Chainlink basis. Peers: ETH 40/60, BNB 30/40, BTC 25/30. **Correction to the framing that prompted this:** oracle basis is NOT DOGE's main blocker. Real basis dist (today's bot log, n=4234): median 6.9, p90 19.7, p99 49.8 bps — 87.3% already pass the 18 cap. The "DOGE basis runs 28–51 bps" claim was from cherry-picked oracle IDs. DOGE's actual blocker hierarchy today: min_edge 34,741 (mostly too-early re-scan noise) > composite skip 3,049 (median 0.077 below the 0.660 floor) > liquidity 2,576 > **oracle_basis_block 2,172 (4th)**. This change only recovers the 22–40 bps band (~5% of scans) via the flagged `oracle_basis_relaxed` path; it does not on its own restore baseline DOGE frequency.
- **Hypothesis:** Aligning DOGE's clean cap with BTC (25) and widening the relax band to cover p90 lets the ~5% of legitimately-tradeable DOGE candidates in the 22–40 bps band through without admitting the ugly 40–60 (2.4%) / >60 (0.4%) tail.
- **Expected outcome:** Fewer `oracle_basis_block` DOGE rejects; some new DOGE fills tagged `oracle_basis_relaxed`. Not ghost-validatable — oracle-blocked rows carry no edge/outcome, so this is a forward-paper experiment only.
- **Actual outcome:** `pending` (needs restart — bot loaded old modules; then watch `trades_settled` for DOGE `oracle_basis_relaxed` fills).
- **Status:** `pending` — NOT committed, NOT live until restart.

### 2026-06-06 — Zero DOGE BUY_YES bullish-floor bumps

- **What changed:** `doge_macro.15m_buy_yes_bullish_floor_bump: 0.11 → 0.0`; the already-zeroed `doge_macro.5m_buy_yes_bullish_floor_bump` remains `0.0`.
- **Why:** Operator rule: zero every non-winner long bump and keep only the two proven winners, HYPE 5m and BNB 5m.
- **Hypothesis:** DOGE 15m BUY_YES no longer gets admitted by fabricated edge from the bullish floor bump.
- **Expected outcome:** DOGE 15m BUY_YES frequency drops where raw edge was insufficient without the bump.
- **Actual outcome:** `pending` — requires bot restart and at least 15 closed affected DOGE trades after rollout.
- **Status:** `pending`

### 2026-06-06 — 1h liquidity floor 250→100 (the one EV-clean 1h un-starve)

- **What changed:** `config/settings.yaml` doge_macro `min_liquidity_1h` (+ `_1h_buy_no`, `_1h_buy_yes`) 250→100.
- **Why:** After the EV-driven revert (see changelog), this is the single 1h block that recomputes **EV-positive across EVERY price band** (not WR-only noise): ghost `rejected_candidates_settled` doge 1h `liquidity` rejects (n=132) settle <.40 +15.6%, .40–.50 +56.6%, .50–.60 +87.7%, ≥.60 +81.6% (avg +48.6%). The blocked candidates' actual liquidity was 58–250 (median 187) — all under the 250 floor. Lowering to 100 admits the profitable bulk while keeping a floor against truly illiquid (<100) markets.
- **Why ONLY this lane:** other 1h blocks failed the EV test — `lane_min_edge` rejects sit at ~0/negative computed edge (they're +EV only because 1h est_prob is under-confident — a calibration problem, not a gate, so a blanket min_edge cut is inert/risky), and hype liquidity has a −41% .50–.60 bulk. doge liquidity is the clean one.
- **Hypothesis:** admits ~120 +EV doge 1h trades/era that were liquidity-gated; restores doge 1h frequency.
- **Trade-off note:** ghost EV is hold-to-resolution; live exit policy (stops) may shave it, but the +EV margin is large and uniform. Paper/calibration phase. Forward-test only; needs bot **restart**. Revert = set back to 250.
- **Actual outcome:** `pending`
- **Status:** `pending` — config-only, needs restart. Watch doge 1h `liquidity` skips drop + doge 1h fills appear.

### 2026-06-05 — Block weak 5m BUY_YES bounces against bearish/neutral 1h floor logic

- **What changed:** In the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) path inherited by DOGE, 5m `BUY_YES` is blocked when DOGE 1h is `BEARISH`, and the 5m bullish floor now requires the actual DOGE 1h trend to be `BULLISH`.
- **Why:** Current session `test_20260605_130808` showed the 5m bullish floor inflating fast alt bounces during non-bullish 1h tape. The shared counterfactual would have filtered 12 of 15 alt 5m longs, covering `-26.4559` PnL.
- **Hypothesis:** DOGE 5m longs stop firing on fast bounces unless the DOGE 1h tape confirms.
- **Expected outcome:** Fewer DOGE 5m `BUY_YES` stop-loss cascades during bearish/neutral DOGE 1h tape.
- **Actual outcome:** `pending` — requires restart and at least 15 closed affected DOGE trades after rollout.
- **Status:** `pending`

### 2026-06-05 — Remove BTC leakage from DOGE trade reasons and AI contexts

- **What changed:** In the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) path inherited by DOGE, removed BTC labels/values from trade reason strings, scan diagnostics, and AI decision context. Also removed the residual 5m confidence component based on BTC correlation.
- **Why:** BTC was already disabled as a trade gate, but inherited DOGE explanations still exposed BTC-looking diagnostics.
- **Hypothesis:** DOGE decisions and explanations read as DOGE-native only after rollout.
- **Expected outcome:** Post-restart DOGE entries/skips no longer include BTC labels in `signal_reason` or marginal AI contexts.
- **Actual outcome:** `pending` — requires restart and at least 15 closed DOGE trades after rollout.
- **Status:** `pending`

### 2026-06-04 — Hold winners + trailing floor (5m BUY_YES exit leak)

- **What changed:** Added a `doge_macro` `updown_overrides` `5m up` (leg=up / BUY_YES) block in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml): `updown_hold_winners_to_resolution: true` + positive trailing floor (`updown_trail_arm_pct: 0.10`, `updown_trail_gap_pct: 0.15`). Reuses the floor mechanic in [`effective_updown_stop_loss_pct`](/Users/mainfolder/Documents/psb-main%201/src/execution/updown_exit_shared.py). DOGE previously had only a `5m down` override (the BUY_NO 5m-native suppression) and no `5m up` exit policy.
- **Why:** Settled exit recompute (`data/calibration/lane_exit_policy.json`, n=34) classified `doge_macro|5m|BUY_YES` as policy **A — hold+trail (exit kills edge)**: held-to-resolution WR 53% / held PnL **+$35.17** vs realized WR 38% / **−$23.59** under the tight exit. The lane is directionally fine; the +30% TP cap and stop churn were turning a profitable lane into a loss. `drift: true` — live config diverged from the recompute recommendation.
- **Hypothesis:** Holding 5m BUY_YES winners with a trailing floor recovers the run the +30% TP was giving up; the floor protects most of the give-back on reversals. Realized PnL moves toward the +$35 held counterfactual.
- **Expected outcome:** DOGE 5m BUY_YES `exit_reason` shifts from `take_profit` toward `updown_resolution`/trail; mean realized PnL per winner rises; realized WR converges toward held 53%.
- **Actual outcome:** `pending` — forward-test only (exits not ghost-validatable); needs bot restart. Watch DOGE 5m BUY_YES exit reasons + realized PnL in `trades_settled.jsonl`.
- **Status:** `pending`

### 2026-06-01 — Decisive AI prompt (kill the HOLD default)

- **What changed:** Rewrote the shared decision/analysis system prompt ([`ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py) `SYSTEM_PROMPT`): removed the "be conservative — markets overestimate" inaction bias, reframed HOLD as requiring a *specific, evidence-based* reason (never a default for uncertainty), told it to commit to a direction on any real lean, and clarified `confidence_score` = strength of the directional evidence. Bumped `prompt_version` → `lane-feedback-v2-decisive` so the settler can split pre/post verdicts. Also raised `max_ai_calls_per_scan` 5→6 for added budget headroom.
- **Why:** Diagnosis showed the AI returned **HOLD on 77%** of responses and approved only **3%** — the prompt itself was steering the model toward inaction on near-coin-flip 15m/1h markets, which the veto-only marginal change alone does not fix.
- **Hypothesis:** A decisive prompt cuts spurious HOLDs and surfaces more directional calls (and more *confident-opposition* vetoes), making the gate a real tiebreaker rather than a near-total veto.
- **Expected outcome:** HOLD share drops well below 77%; more BUY_YES/BUY_NO verdicts; gate approval rate rises off 3%.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only; needs bot restart. Watch HOLD% / approval% in `decision_layer.jsonl` under prompt_version `lane-feedback-v2-decisive`.

### 2026-06-01 — Marginal lane → AI veto-only (unblock 15m/1h)

- **What changed:** Also raised `max_ai_calls_per_scan` 3→5 (DOGE pegged the 3-call/scan budget in ~52% of scans). The marginal-lane AI gate flipped from fail-closed to **veto-only**: the AI can now only REJECT a below-threshold candidate with a *confident, directly-opposing* directional call (conf ≥ `decision_layer.min_confidence`). HOLD / SKIP / low-confidence / agreement fall back to the quant trade. Central change in [`evaluate_trade_decision`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py) (new `veto_only` param) threaded through the marginal call site(s); guarded the redundant local re-checks; opt-out `decision_layer.marginal_veto_only`.
- **Why:** Over a 5.5h window (`decision_layer.jsonl`) the gate approved only **3%** of AI-evaluated candidates — the model returned **HOLD 77%** of the time (conservative system prompt + a 0.60 confidence bar that near-coin-flip 15m/1h markets can't honestly clear), and HOLD was a hard veto. DOGE was fully shut off on the AI path: **0/63** approved.
- **Hypothesis:** Restoring marginal admission (blocked only on confident AI opposition) reopens 15m/1h frequency without losing the AI's ability to stop a conviction-wrong trade.
- **Expected outcome:** DOGE 15m/1h marginal entries resume; AI still vetoes confident-opposite cases.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only (AI-gate behavior is not ghost-validatable); needs bot restart to load.

### 2026-06-01 — Revert DOGE BUY_YES disable

- **What changed:** Reverted the same-day DOGE `5m up`, `15m up`, and `1h up` entry-policy disables. DOGE BUY_YES returns to prior gating.
- **Why:** Operator rejected disabling losers as the wrong correction path. DOGE needs sample-backed tuning of the signal inputs and calibration, not a blanket upside pause.
- **Hypothesis:** Restored DOGE BUY_YES admission keeps enough evidence to determine whether the issue is family mix, probability inflation, price band, or timing.
- **Expected outcome:** DOGE BUY_YES resumes prior admission; no disabled-lane effect from the rejected WR-mode change.
- **Actual outcome:** `pending`
- **Status:** `reverted ❌`

### 2026-06-01 — Disable DOGE BUY_YES for WR target

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), disabled DOGE `5m up`, `15m up`, and `1h up` entry-policy lanes.
- **Why:** Past-3-day BUY_YES review showed DOGE BUY_YES at `27` trades / `48.1%` WR. A tiny 5m native slice reached `60%` WR (`n=5`), but the broader DOGE upside sample did not clear the operator's `55%` minimum.
- **Hypothesis:** DOGE BUY_YES sits out until a larger ghost/live cohort supports re-enabling; aggregate BUY_YES WR improves immediately by removing the below-minimum broad lane.
- **Expected outcome:** DOGE BUY_YES entries cease; review disabled-lane ghosts before reopening any DOGE upside family.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-31 — Suppress anti-predictive 5m-native BUY_NO shorts

- **What changed:** Set doge_macro `disable_buy_no_5m_native: true`; inherits the 5m BUY_NO sit-out in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), ghost-logged as `buy_no_5m_native_suppressed`. Commit `5d8cbc0`. Full rationale in `sol_macro.md` same date.
- **Why:** `doge_5m_native` BUY_NO held-to-resolution WR was 27.8%, part of the consistent cross-alt 5m-short inversion vs 15m-native at 50-65%.
- **Hypothesis:** DOGE BUY_NO held-WR rises toward 50%; 5m longs and 15m shorts unaffected.
- **Expected outcome:** `buy_no_5m_native_suppressed` appears for DOGE; DOGE BUY_NO held-WR rises from 39.4%.
- **Actual outcome:** `pending` (needs restart + ~15 closed trades)
- **Status:** `pending`

### 2026-05-28 — Inherited `sell_5m_low_corr` hard skip downgraded

- **What changed:** DOGE inherits the shared SOL-family scan path in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py); `sell_5m_low_corr` no longer hard-skips 5m `BUY_NO` candidates and is now diagnostic context only.
- **Why:** Calibration review showed the hard BTC-correlation skip could throw away valid alt-native downside entries.
- **Hypothesis:** DOGE 5m downside candidates should reach later edge/risk gates instead of being blocked by BTC correlation alone.
- **Expected outcome:** Future DOGE diagnostics should stop reporting `sell_5m_low_corr` as a hard skip; review after >=15 closed post-change DOGE trades.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-26 — Timeframe-scoped entry control config

- **What changed:** Moved DOGE entry-control thresholds and windows from legacy flat timeframe keys (`min_edge_5m`, `entry_window_*`) into canonical `defaults` / `by_tf` config, and routed shared macro entry policy reads through the timeframe resolver.
- **Why:** Static config audit showed the same 5m/15m values duplicated in flat keys and lane policy overrides, making it unclear which tuning surface was authoritative.
- **Hypothesis:** DOGE 5m/15m/1h tuning changes should stay scoped to their `by_tf` cell with no cross-timeframe bleed.
- **Expected outcome:** Startup logs should show DOGE `by_tf` overrides; focused tests should preserve the same effective min-edge/window values.
- **Actual outcome:** `pending` (config migration only; need >=15 closed DOGE trades before performance judgment).
- **Status:** `pending`

### 2026-05-26 — Resolver metadata parity for shared macro signals

- **What changed:** Added BTC-compatible resolver metadata to the shared macro signal path used by DOGE: `conflict_type`, `resolver_path`, `htf_side`, `quant_side`, and `momentum_side`, with journal and position persistence.
- **Why:** DOGE had HTF and oracle metadata, but direction-resolution details were not first-class like BTC.
- **Hypothesis:** Future ghost/trade reviews can separate HTF-aligned, quant-disagree, and momentum-disagree DOGE entries without changing entry behavior.
- **Expected outcome:** New DOGE entries should include resolver metadata in journal extras and `entry_signal`.
- **Actual outcome:** `pending` (need post-change entries to verify field coverage).
- **Status:** `pending`

### 2026-05-26 — Route DOGE through up/down exits and hold winners

- **What changed:** Added `doge_macro` to the shared crypto up/down exit strategy set and enabled `trading.exit_rules.updown_hold_winners_to_resolution`.
- **Why:** DOGE was missing from the specialized up/down exit path, so it could bypass the stop/window/resolution behavior used by BTC/SOL/ETH/HYPE/XRP.
- **Hypothesis:** DOGE exits should now use up/down-specific stop/window semantics, and correct winners should settle instead of being clipped by early TP.
- **Expected outcome:** DOGE exits should include up/down-specific reasons and more `RESOLVED:* (real)` winners when correct.
- **Actual outcome:** `pending` (need >=15 closed DOGE trades after restart).
- **Status:** `pending`

### 2026-05-26 — Remove DOGE 1h exploration size haircut

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), removed the added `0.3x` lane `size_multiplier` from DOGE 1h up/down policies while keeping the existing 5m calibration sizing unchanged.
- **Why:** DOGE’s avg loss worsened and the post-May-22 exploration layer added 1h size haircuts before the lane had a useful closed-trade sample. The rollback keeps the proven/current 5m posture and removes only the later 1h sizing experiment.
- **Hypothesis:** DOGE 1h entries should reflect Kelly/risk sizing directly instead of an extra exploration haircut, making per-trade economics easier to compare with baseline cohorts.
- **Expected outcome:** Next DOGE sample should show no 1h `lane_size=0.30x` tag from lane policy.
- **Actual outcome:** `pending` (need >=15 closed DOGE trades after restart).
- **Status:** `pending`

### 2026-05-26 — BUY_YES recovery tweak after rollback

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), changed DOGE `alt_momentum_confirm.buy_yes` to `15m` only while keeping `buy_no` confirmation on `5m`, `15m`, and `1h`.
- **Why:** DOGE still needs quality control, but all-window BUY_YES confirmation would likely starve the side we are trying to recover across the shared alt path.
- **Hypothesis:** DOGE BUY_YES can collect small confirmed-or-clean samples, while BUY_NO remains gated against unconfirmed downside defaults.
- **Expected outcome:** Next paper run should show lower downside flood risk with nonzero DOGE BUY_YES opportunity.
- **Actual outcome:** `pending` (need ≥15 closed DOGE trades after restart on this config).
- **Status:** `pending`

### 2026-05-26 — Pre-restart rollback of post-May-22 momentum guard regression

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), added active `doge_macro.alt_momentum_confirm` blocking for `BUY_YES` and `BUY_NO` on `5m`, `15m`, and `1h`; restored DOGE 1h edge overrides to `up: 0.09` and `down: 0.08`.
- **Why:** DOGE volume rose while remaining a loss contributor. The post-May-22 guard regression let default-side entries through without DOGE-native confirmation, and the 1h exploration loosen increased exposure before the lane had proof.
- **Hypothesis:** DOGE should trade less often and only when its own MACD confirms the selected side, reducing default bearish/downside bleed.
- **Expected outcome:** Next paper run should show lower DOGE fill count and fewer `bearish_dip_default` DOGE fills unless confirmed by DOGE tape.
- **Actual outcome:** `pending` (need ≥15 closed DOGE trades after restart on this config).
- **Status:** `pending`

### 2026-05-25 — 1h exploration admission before restart

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), DOGE 1h lane-policy floors were lowered to `up.min_edge: 0.08` and `down.min_edge: 0.075`, 1h `entry_price_max` widened to `0.58`, and both 1h sides now use `size_multiplier: 0.3`.
- **Why:** DOGE has only one closed 1h calibration trade, so the lane is under-sampled. This is an exploration setting, not a claim that DOGE 1h is good.
- **Hypothesis:** DOGE 1h should produce enough small-notional samples to compare against its currently active 5m/15m lanes.
- **Expected outcome:** Next session should include DOGE 1h entries when markets exist, with enough closed samples to review lane family, side, and regime attribution.
- **Actual outcome:** `pending` (need >=15 closed DOGE 1h trades after restart).
- **Status:** `pending`

## Review sessions

- `pending`

## Lessons learned

- `pending`

## 2026-06-11 — 5m BUY_NO inversion flip → +EV long (forward-test)
- **Finding:** doge_macro **5m BUY_NO** is structurally inverted — held-to-resolution WR **31%** over n=212 (settled since ~05-20), **$-227** live PnL. On the *same* markets the YES side resolves ITM ~69%, so the short is anti-selective and the cheap long is +EV.
- **Change:** flip BUY_NO→BUY_YES at the 5m edge stage via the shared sol loop (`buy_no_5m_flip_to_yes: true`). Uses the **complement** of the native est_prob (`max(1−est, 0.50)`) so the normal edge gate then admits only the *cheap* longs (low yes_price) — the +EV pocket. Candidate has already cleared all short-side gates; downstream directional guards inert (`_btc_trade_inputs_enabled()==False`). Default opt-out flag.
- **Status:** LIVE post-restart in session `test_20260611_181157`. Family flip (sol/xrp/doge/bnb) observed firing (`+buy_no_5m_to_yes_flip side=LONG`); eth/hype loaded but **dormant** until their 5m side next goes short (book was all-LONG at restart).
- **Watch:** confirm flipped longs *convert to fills* over next sessions, not 100% re-skipped by lane_entry_window/composite/iql. Validate flipped-long held-WR vs the ~69% thesis.
