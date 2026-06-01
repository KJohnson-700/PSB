# DOGE macro (`doge_macro`)

DOGE **Up or Down** — inherits shared `SolMacroStrategy` signal path with DOGE market detection and `DOGEUSDT` spot leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed 1h trades | 1 | `data/calibration/trades.jsonl` |
| 1h Win rate | 0.0% | same |
| 1h Net PnL | -$6.19 | same |

## Change Log

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
