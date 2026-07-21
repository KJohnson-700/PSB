# ETH macro (`eth_macro`)

ETH **Up or Down** — inherits `SolMacroStrategy` (shared entry-window and scan logic); `ETHMacroStrategy` overrides market detection and `ETHUSDT` leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 9 | Paper `test_20260504_034719` — [`docs/session_reports/session_parse_test_20260504_034719.json`](docs/session_reports/session_parse_test_20260504_034719.json) |
| Win rate | 77.8% | same |
| Net PnL | +$13.45 | same |

## Change Log

### 2026-07-15 — Cap ETH 5m BUY_NO entries at yes<=0.51

- **What changed:** Added timeframe-scoped `buy_no_min_no_price_{tf}` lookup in [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py) and set `strategies.eth_macro.buy_no_min_no_price_5m: 0.49` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml). ETH 15m/1h BUY_NO keep the legacy fallback `0.20`; ETH BUY_YES does not read this key.
- **Why:** Operator review found ETH `5m|down` / `BUY_NO` at about `-$16` and `25%` WR, with losing shorts entering at high YES prices around `0.525-0.535`; wins clustered around `0.505-0.515`.
- **Hypothesis:** Requiring NO >= 0.49 (equivalent to YES <= 0.51) removes the high-YES 5m short cohort while preserving the lower-YES winner cluster and leaving ETH 5m BUY_YES plus 15m/1h down unchanged.
- **Expected outcome:** ETH 5m BUY_NO should stop taking shorts with `yes_price > 0.51`; estimated operator impact is removal of roughly `$12` of the observed `$16` loss cohort. Hot-reload should pick up the config/code path without a bot restart.
- **Actual outcome:** `pending` — requires at least 15 closed ETH 5m BUY_NO trades after rollout.
- **Status:** `pending`

### 2026-06-16 — ETH pocket-only: est_prob is unfixable noise, edge is SELECTION

- **Root cause (operator red flag: all 6 ETH lanes -EV).** ETH's est_prob_up is built inline in its duplicated scan loop (eth_macro.py ~1366: base 0.50 + small htf/btc/macd/RSI nudges), NOT via the SOL family path. Calibration check: ETH est_prob AUC ≈ **0.49** (its 0.85-confidence bucket resolves UP only ~45%); SOL ≈ 0.52. BUT — investigated "route ETH through `_estimate_probability`" and REJECTED it: that method is for traditional strike markets (`ETH above $X`), needs a price threshold the Up/Down questions don't have. There is **no separate calibrated up/down path** — both SOL and ETH hand-build est_prob, both ~0.50 AUC. Matches `calibration_not_viable_signal_floor`: est_prob is ~coin-flip across all assets; **the edge is admission SELECTION, not signal.** So there is no est_prob "model" to fix.
- **The pocket hunt (ghost sweep window×side×timing×RSI×bias) found ETH's real edge — the SHORT side at elevated RSI:**
  - `15m BUY_NO RSI 55-65`: **+0.103 all / +0.151 recent** (n=2028)
  - `1h BUY_NO RSI >65`: **+0.256 / +0.256** (n=321)
  - `1h BUY_NO RSI 55-65`: +0.147 / +0.204 (n=350)
  - `1h BUY_NO BULLISH`: +0.120 / +0.172 (n=1400); `15m BUY_NO BULLISH`: +0.045 / +0.070 (n=4193)
  - ALL ETH BUY_YES (longs) -EV (-0.087 to -0.168); ETH 5m dead both sides. Baseline looked -EV everywhere because it averaged the +EV high-RSI shorts with the junk.
- **What changed:** added `eth_pocket_only` gate to eth_macro.py (its own loop doesn't honor the sol_macro disable hooks). When on, ETH admits ONLY `BUY_NO` at `rsi_14 >= eth_buy_no_rsi_min` (55) on 15m/1h; blocks all ETH longs + all ETH 5m; ghost-logs the rest. Config: `eth_pocket_only: true`, `eth_buy_no_rsi_min: 55.0`. Default off = no-op.
- **Interpretation:** ETH's edge is contrarian — fade overbought/bullish ETH with shorts. Same shape as the per-lane selection fixes (not a model fix). Forward-test: ghost can't validate a new admission gate; watch live.
- **Status:** `pending` — live as the last paper before the next live smoke test.

### 2026-06-15 — Add lane-local stop cooldown for ETH 1h BUY_YES bleed

- **What changed:** Added `lane_stop_halt` config support and enabled `eth_macro|1h|BUY_YES` with a 15-minute cooldown after an `updown_stop_loss`.
- **Why:** Running session `test_20260615_031614` showed ETH 1h BUY_YES at `0%` WR and `-$9.49` across 2 exits. Sample is thin, so the change is a short cooldown rather than a model/gate rewrite.
- **Hypothesis:** ETH 1h long entries should pause after a stop to avoid immediate re-entry into the same failed continuation state, while ETH 5m/15m and BUY_NO lanes remain untouched.
- **Expected outcome:** Fewer repeated ETH 1h BUY_YES stop-outs without reducing unrelated ETH lanes.
- **Actual outcome:** `pending` — requires restart and at least 15 closed ETH 1h BUY_YES trades after rollout.
- **Status:** `pending`

### 2026-06-15 — eth 5m BUY_YES min_edge 0.10 → 0.085 (ghost-backed long loosen)

- **What changed:** `entry_policy.window_side_overrides.5m.up.min_edge` 0.10 → 0.085 (config/settings.yaml:935). `5m.down` left at 0.12 (untouched). Live path is entry_policy; legacy `by_tf.5m.min_edge: 0.1` left as-is.
- **Why:** Kimi flagged eth 5m min_edge as "too high vs other alts (0.085)" — first-principles, so I ghost-validated. Cut of `rejected_candidates_settled.jsonl` (eth_macro 5m, ts≥06-08) by model-edge band:
  - BUY_YES 0.085–0.10 band: **n=284, 54.2% WR, +0.126 EV/$** ✅
  - BUY_NO 0.085–0.10 band: n=157, 46.5% WR, −0.045 EV/$ ❌
- **Hypothesis:** The 0.10 long floor was clipping a genuinely +EV 5m BUY_YES cohort in the current bull tape. Lowering to 0.085 admits it; keeping down at 0.12 keeps the negative short band out.
- **Expected outcome:** More eth 5m BUY_YES fills, +EV. NEEDS RESTART to load. Forward-test on settled trades.
- **Rejected in same review:** `direction_source: btc` = dead config key (0 code refs, no effect on ETH direction). No change.

### 2026-06-12 — REJECTED before rollout: 15m BUY_NO hold+trail

- **What changed:** No live config change remains. A temporary `eth_macro` 15m `down` / `BUY_NO` hold+trail override was considered, then removed before rollout.
- **Why:** Operator is skeptical that hold behavior may have broken the bot, so this is deliberately narrow. Recent settled exit policy since `2026-06-11` showed `eth_macro|15m|BUY_NO` as one of only two current drift lanes clearing the sample floor: `n=23`, held WR `48%`, realized WR `39%`, gap `+$20.6`.
- **Hypothesis:** Rejected. The better next step is to inspect stop-cut recoverable winners vs true bad entries without reintroducing hold-to-resolution.
- **Expected outcome:** No behavior change from this entry.
- **Actual outcome:** `pending`
- **Status:** `reverted ❌` before rollout.

### 2026-06-12 — Remove residual live-entry AI blockers from alt 1h/15m quant path

- **What changed:** In the shared alt macro base and [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), live-entry decision windows are empty for alts and ETH's remaining `ai_window_closed` marginal branch is guarded behind that set. ETH 1h/15m entries now proceed through quant gates only; AI remains for observer/tuning/self-healing surfaces.
- **Why:** Operator clarified that live-entry AI belongs to BTC 1h/15m only. ETH inherited/shared residual AI timing logic that could reject marginal candidates even when no live ETH AI should run.
- **Hypothesis:** Valid ETH 1h quant candidates should no longer be blocked by AI availability, AI timing windows, or marginal AI gate wiring.
- **Expected outcome:** ETH 1h candidates that pass native side, oracle, price, edge, composite, sizing, and risk gates can emit signals with `ai_used=false`.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-06-12 — Exit-policy classifier correction: keep ETH 15m BUY_NO hold+trail

- **What changed:** Reverted the attempted tight-exit change before rollout. In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), `trading.exit_rules.updown_overrides.eth_macro.window_lane_overrides.15m.down` remains `updown_hold_winners_to_resolution: true` with trail arm/gap `0.10 / 0.15`. The exit-policy recommender now treats neutral positive-gap lanes as hold+trail instead of defaulting to tight TP/SL.
- **Why:** `lane_exit_audit.py` defines `gap = held_pnl - actual_pnl`; positive gap means holding to resolution earned more dollars than the realized exit path. `eth_macro|15m|BUY_NO` has `n=467`, held WR `46.3%`, realized WR `38.8%`, held `$242.33`, realized `$33.28`, gap `+209.06`, so tightening would preserve the undercapturing exit behavior.
- **Hypothesis:** Keeping hold+trail preserves the EV-favored exit path while the corrected recommender avoids future false "tight" drift from neutral positive-gap rows.
- **Expected outcome:** ETH 15m BUY_NO realized dollars should move toward the held-counterfactual gap after at least 15 closed post-change trades.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only; exit changes are not ghost-validatable.

### 2026-06-06 — 1h BUY_NO: widen the % stop 0.20→0.40 — REVERTED (n=3 anecdotal; see changelog EV-driven revert)

- **What changed:** Added `down` under `exit_rules.updown_overrides.eth_macro.window_lane_overrides.1h` in `config/settings.yaml`: `updown_stop_loss_pct: 0.40` (up from the global 0.20). ETH 1h BUY_YES (`up`) lane unchanged.
- **Why:** Settled-exit audit (`data/calibration/trades_settled.jsonl`, BUY_NO 1h): the `updown_stop_loss` leg (n=3) realized **−$8.44** but held to resolution **+$7.99**, with 2/3 recovering; TP leg (n=6) +$28.35 vs held +$40.78. Same pattern as BTC 1h BUY_NO — the 20% stop cuts recoverable 1h trades. But n=9 total is below the audit's min-apply (20), so this lane gets a *conservative widen* (keep a backstop) rather than the full hold-to-resolution applied to BTC.
- **Hypothesis:** A 40% stop gives the 1h market room to recover most of the stop-leg gap while still capping a genuine adverse run; revisit toward hold-to-resolution once n grows.
- **Expected outcome:** Fewer ETH 1h BUY_NO `updown_stop_loss` exits; lane realized PnL moves toward the held-counterfactual.
- **Trade-off note:** Thin sample — directional, not proven. Forward-test only (exit change, not ghost-validatable); needs bot **restart**. Part of the per-lane SL/TP calibration pass (1h first). SOL/XRP/HYPE/DOGE/BNB have **zero** 1h BUY_NO settled exits, so they were left untouched.
- **Actual outcome:** `pending`
- **Status:** `pending` — config-only, not committed, needs restart.

### 2026-06-05 — Block 5m BUY_YES against bearish ETH 1h

- **What changed:** In [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), applied the same alt-native 1h alignment guard as SOL-family: ETH 5m `BUY_YES` is blocked when ETH's own 1h trend is `BEARISH`.
- **Why:** The current regression was a 5m alt-long burst caused by fast 5m bullish flips fighting bearish/neutral 1h tape. ETH has a separate scan loop, so it needs the same guard explicitly.
- **Hypothesis:** ETH 5m longs stop firing against bearish ETH 1h tape without using BTC context.
- **Expected outcome:** Fewer ETH 5m `BUY_YES` stop-loss cascades during bearish ETH 1h tape.
- **Actual outcome:** `pending` — requires restart and at least 15 closed ETH 5m BUY_YES candidates after rollout.
- **Status:** `pending`

### 2026-06-05 — Remove BTC leakage from ETH trade reasons and AI contexts

- **What changed:** In [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), removed BTC labels/values from ETH trade reason strings, scan diagnostics, and marginal AI decision context (`BTC_HTF`, BTC-follow prompt framing, BTC move/history blocks, BTC regime text).
- **Why:** BTC was already disabled as an ETH trade gate, but residual BTC strings made ETH explanations look BTC-driven.
- **Hypothesis:** ETH decisions and explanations read as ETH-native only; no BTC-looking tokens should appear in new ETH signal reasons or AI prompts after rollout.
- **Expected outcome:** Post-restart ETH entries/skips no longer include BTC labels in `signal_reason` or marginal AI contexts.
- **Actual outcome:** `pending` — requires restart and at least 15 closed ETH trades after rollout.
- **Status:** `pending`

### 2026-06-05 — Enforce alt-native rule: BTC no longer affects ETH trade decisions

- **What changed:** In [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), guarded ETH BTC-follow logic behind the shared disabled `_btc_trade_inputs_enabled()` path. BTC context remains logged, but no longer blocks ETH entries, adds BTC-follow penalties, boosts ETH 5m probability/confidence, applies BTC 1h regime min-edge/size multipliers, blocks macro-leg disagreement, or vetoes ETH direction via BTC bearish/bullish guards.
- **Why:** Operator reiterated the standing invariant: BTC must not decide alt trades. ETH had custom override logic outside the shared SOL-family scan path, so the base-class fix alone would not remove BTC-follow trade effects.
- **Hypothesis:** ETH entries and skips become ETH-native plus oracle/price/risk controls; BTC remains diagnostic context only.
- **Expected outcome:** Future ETH diagnostics should stop hard-skipping or penalizing candidates through `btc_5m_no_impulse`, `btc_1h_follow_penalty_*`, `btc_15m_follow_penalty_*`, macro-leg blocks, BTC regime multipliers, or BTC direction guards.
- **Actual outcome:** `pending` — requires restart and at least 15 closed ETH trades after rollout.
- **Status:** `pending`

### 2026-06-01 — Per-lane exit policy: extend hold+trail to eth 5m/1h BUY_YES + 15m BUY_NO

- **What changed:** Held-vs-realized scorecard ([`lane_exit_audit.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_exit_audit.py)) classifies eth as Policy-A (exit kills edge) on more than 15m BUY_YES. Added hold-winners + trailing floor (`arm 0.10 / gap 0.15`) to **eth 5m BUY_YES** (held 60% / realized 20%, n=5 thin), **eth 1h BUY_YES** (held 55% / realized 23%, n=22), and **eth 15m BUY_NO** (held 50% / realized 34%, n=38 — a legit 15m short, NOT the suppressed 5m-native).
- **Why:** eth's realized WR is far below held-to-resolution across windows; the +30% TP / stop is converting directionally-correct entries into losses. eth 1h BUY_YES alone throws away ~$24; 15m BUY_NO ~$38.
- **Hypothesis:** holding these to resolution with a positive trailing floor recovers the directional edge the exit was discarding.
- **Expected outcome:** eth realized-WR converges toward held-WR; fewer `take_profit`/`updown_stop_loss` exits on these lanes.
- **Actual outcome:** `pending` — forward-test only (exits not ghost-validatable); needs bot restart. Track via `python -m src.analysis.lane_exit_audit`.
- **Status:** `pending`

### 2026-05-31 — Early-TP regret: hold winners + positive trailing floor (BUY_YES 15m)

- **What changed:** Added an `up` (BUY_YES) block to `eth_macro` 15m in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml): `updown_hold_winners_to_resolution: true` + positive trailing floor (`updown_trail_arm_pct: 0.10`, `updown_trail_gap_pct: 0.15`). Backed by the new floor mechanic in [`effective_updown_stop_loss_pct`](/Users/mainfolder/Documents/psb-main%201/src/execution/updown_exit_shared.py) — once peak clears +10%, exit floor trails at `peak − 15%` and can be positive (banks gains), never wider than the base stop.
- **Why:** Settled `take_profit` rows: ETH BUY_YES 15m n=14, +$20.9 regret, 86% held-WR. Directionally-correct lane exiting too early at +30%. (5m n=1 and 1h n=5 too thin — left out.)
- **Hypothesis:** Trailing-after-MFE on ETH 15m longs captures the run currently surrendered at the +30% TP while limiting reversal give-back.
- **Expected outcome:** ETH BUY_YES 15m realized PnL per winner rises above the +30% cap; fewer `take_profit` exits.
- **Actual outcome:** `pending` — forward-test only (no ghost validation for exits); needs bot restart. Watch ETH BUY_YES 15m in `trades_settled.jsonl`.
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
- **Why:** Over a 5.5h window (`decision_layer.jsonl`) the gate approved only **3%** of AI-evaluated candidates — the model returned **HOLD 77%** of the time (conservative system prompt + a 0.60 confidence bar that near-coin-flip 15m/1h markets can't honestly clear), and HOLD was a hard veto. ETH approved only 4/40 AI-evaluated candidates in the window.
- **Hypothesis:** Restoring marginal admission (blocked only on confident AI opposition) reopens 15m/1h frequency without losing the AI's ability to stop a conviction-wrong trade.
- **Expected outcome:** ETH 15m/1h marginal entries resume; AI still vetoes confident-opposite cases.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only (AI-gate behavior is not ghost-validatable); needs bot restart to load.

### 2026-06-01 — BUY_YES overconfidence soft repair

- **What changed:** Added lane-specific BUY_YES soft repairs for ETH `5m native`, `15m native`, `1h native`, and `drift`: probability haircuts plus min-edge adders, with an extra small min-edge add when oracle basis is elevated. No ETH BUY_YES lane is disabled.
- **Why:** Past-3-day attribution (`2026-05-29`–`2026-06-01 PT`) showed ETH BUY_YES false positives with raw probability around `0.58–0.64` while WR was `16.7%–35.7%` across the targeted cohorts.
- **Hypothesis:** Overconfident ETH BUY_YES candidates now need enough extra edge to survive the haircut instead of passing on inflated raw probability.
- **Expected outcome:** Fewer weak ETH BUY_YES entries from the repaired cohorts; rejected candidates should continue to settle through `lane_min_edge` rather than a disable/allowlist gate.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-06-01 — Revert ETH BUY_YES disable

- **What changed:** Reverted the same-day ETH `5m up`, `15m up`, and `1h up` entry-policy disables. ETH BUY_YES returns to prior gating.
- **Why:** Operator rejected disabling losing lanes as a lazy way to improve aggregate WR. ETH needs a false-positive repair pass, not a blanket upside shutdown.
- **Hypothesis:** Keeping ETH BUY_YES active lets the next audit identify whether raw probability, calibration alpha, BTC-follow context, entry window, price band, or oracle basis is causing the low WR.
- **Expected outcome:** ETH BUY_YES resumes prior admission; no disabled-lane effect from the rejected WR-mode change.
- **Actual outcome:** `pending`
- **Status:** `reverted ❌`

### 2026-06-01 — Disable ETH BUY_YES for WR target

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), disabled ETH `5m up`, `15m up`, and `1h up` entry-policy lanes.
- **Why:** Past-3-day BUY_YES review showed ETH BUY_YES at `80` trades / `28.7%` WR / `-$17.55`, with every ETH upside window below the operator's `55%` minimum.
- **Hypothesis:** ETH stops dragging aggregate BUY_YES WR while ETH downside and ghost logging continue to collect evidence for future re-enable candidates.
- **Expected outcome:** ETH BUY_YES entries should cease; any ETH upside candidates should ghost-log through disabled-lane rejection paths for review.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-31 — Suppress anti-predictive 5m-native BUY_NO shorts

- **What changed:** Set eth_macro `disable_buy_no_5m_native: true`. ETH inherits the suppression in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) `_resolve_alt_bias_for_tf` via `ETHMacroStrategy(SolMacroStrategy)`; 5m BUY_NO sits out and ghost-logs as `buy_no_5m_native_suppressed`. Commit `5d8cbc0`. See `sol_macro.md` same date for the full rationale.
- **Why:** `eth_5m_native` BUY_NO held-to-resolution WR was 11.8% — the worst 5m short cell of any alt — vs ETH 15m-native at 50%. The 5m short signal is inverted (MACD-confirmed 5m shorts lose).
- **Hypothesis:** ETH BUY_NO held-WR rises toward 50%; ETH 5m longs and 15m shorts unaffected.
- **Expected outcome:** `buy_no_5m_native_suppressed` appears for ETH; ETH BUY_NO held-WR rises from 38.6%.
- **Actual outcome:** `pending` (needs restart + ~15 closed trades)
- **Status:** `pending`

### 2026-05-31 — Morning-session exit and ETH weak-confirm repair

- **What changed:** Lowered global up/down `take_profit_pct` from `0.50` to `0.30` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml). Added `eth_15m_weak_confirm_hard_gate_enabled: false` and changed [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py) so ETH 15m weak confirmation becomes a soft min-edge penalty instead of a hard discard.
- **Why:** Active session `test_20260531_041319` was `112` exits, `39.3%` WR, `-$4.64`; ETH was `6/17`, `-$8.62`. Replaying the active session's marks showed `tp=0.30` would have produced `51.3%` WR / `+$44.39` across replayable crypto paths versus `42.3%` WR / `+$4.33` at `tp=0.50`. Today's settled ghosts showed `eth_15m_weak_confirm` rejects at `61.5%` WR / `+21.3%` ROI (`n=600`), so the hard gate was blocking a profitable current-regime ETH cohort.
- **Hypothesis:** ETH 15m should stop starving valid weak-confirm candidates while still charging them extra edge, and the lower TP should bring realized paper WR closer to the requested ~50% lane target without changing stop-loss logic.
- **Expected outcome:** ETH `eth_15m_weak_confirm` hard rejects should fall; ETH 15m admitted weak-confirm-soft trades should be reviewed after at least 15 closed ETH trades.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-31 — Ghost-validated BUY_YES entry-window expansion

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), widened ETH upside entry windows where settled ghosts showed the old window was blocking profitable candidates: `15m up 24.0 → 120.0` and `1h up 120.0 → 360.0`. The ETH downside windows were left unchanged because the same ghost slice showed `15m BUY_NO lane_entry_window` was protective.
- **Why:** Settled ghosts since `2026-05-30T00:00Z` showed ETH upside entry-window misses: `eth_macro|15m|BUY_YES|lane_entry_window` `n=2,636`, `WR=53.0%`, `netGate=-164`; `eth_macro|1h|BUY_YES|lane_entry_window` `n=467`, `WR=79.0%`, `netGate=-271`. For ETH 15m, the `120+` minute bucket was protective, so the expansion stops at `120.0`.
- **Hypothesis:** ETH should recover upside participation during strong regimes without reopening weak downside lanes; remaining edge, price-band, oracle, and Kelly/risk controls still gate final entries.
- **Expected outcome:** ETH `BUY_YES` starvation from `lane_entry_window` should fall, especially on `15m` and `1h`; review actual closed ETH trades after at least 15 post-change exits.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-28 — BTC-follow LONG hard skips converted to soft penalties

- **What changed:** In [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), `btc_1h_not_following` and `btc_15m_not_following` no longer hard-skip ETH LONG candidates. They now mirror the SHORT path: dampen `est_prob_up`, add a small min-edge penalty, and preserve the candidate for later ETH-native gates.
- **Why:** Calibration review showed these skip reasons blocking settled winners, and the hard skip violated the current rule that alts are decided by alt-native indicators with BTC used only as context.
- **Hypothesis:** ETH LONG throughput should recover when ETH-native evidence is present, while the BTC disagreement still reduces confidence through a soft penalty.
- **Expected outcome:** Future skip diagnostics should stop showing `btc_1h_not_following` / `btc_15m_not_following` as hard ETH LONG blockers; ETH LONG trades should be judged after >=15 closed post-change ETH trades.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-26 — Timeframe-scoped entry control config

- **What changed:** Moved ETH entry-control thresholds and windows from legacy flat timeframe keys (`min_edge_5m`, `entry_window_*`) into canonical `defaults` / `by_tf` config, and routed shared macro entry policy reads through the timeframe resolver.
- **Why:** Static config audit showed the same 5m/15m values duplicated in flat keys and lane policy overrides, making it unclear which tuning surface was authoritative.
- **Hypothesis:** ETH 5m/15m/1h tuning changes should stay scoped to their `by_tf` cell with no cross-timeframe bleed.
- **Expected outcome:** Startup logs should show ETH `by_tf` overrides; focused tests should preserve the same effective min-edge/window values.
- **Actual outcome:** `pending` (config migration only; need >=15 closed ETH trades before performance judgment).
- **Status:** `pending`

### 2026-05-26 — Resolver metadata parity for macro signals

- **What changed:** Added BTC-compatible resolver metadata to ETH macro entries: `conflict_type`, `resolver_path`, `htf_side`, `quant_side`, and `momentum_side`, with journal and position persistence.
- **Why:** ETH had HTF and oracle metadata, but direction-resolution details were not first-class like BTC.
- **Hypothesis:** Future ghost/trade reviews can separate ETH HTF-aligned, quant-disagree, and momentum-disagree entries without changing entry behavior.
- **Expected outcome:** New ETH entries should include resolver metadata in journal extras and `entry_signal`.
- **Actual outcome:** `pending` (need post-change entries to verify field coverage).
- **Status:** `pending`

### 2026-05-26 — Hold up/down winners to resolution

- **What changed:** Enabled `trading.exit_rules.updown_hold_winners_to_resolution` and suppressed up/down `take_profit` exits while that flag is true.
- **Why:** ETH size was basically stable while W/L ratio deteriorated, so the problem is exit/selection rather than sizing.
- **Hypothesis:** Correct ETH trades should realize closer to binary-resolution payoff when held through settlement.
- **Expected outcome:** Fewer `take_profit` exits, more `RESOLVED:* (real)` exits, and higher avg-win dollars.
- **Actual outcome:** `pending` (need >=15 closed ETH trades after restart).
- **Status:** `pending`

### 2026-05-26 — Restore May-22 ETH sizing posture

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), restored ETH 5m lane `size_multiplier` from `0.3x` to the May 22 `0.4x`, and removed the added `0.3x` haircut from ETH 15m/1h lane policies.
- **Why:** Session attribution showed average winners collapsed across macro lanes. ETH’s exit logic did not change from the May 22 restore point, so the clean regression to undo is the post-baseline lane sizing haircut.
- **Hypothesis:** ETH average realized winner should improve without adding new entry restrictions; 5m remains calibration-sized at the original `0.4x`.
- **Expected outcome:** Next ETH paper sample should no longer show `lane_size=0.30x` on 15m/1h entries, and 5m should show the intended `0.40x` lane size.
- **Actual outcome:** `pending` (need >=15 closed ETH trades after restart).
- **Status:** `pending`

### 2026-05-26 — Pre-restart rollback of post-May-22 ETH momentum guard regression

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), restored active `eth_momentum_confirm.buy_yes` and `eth_momentum_confirm.buy_no` blocking on `5m`, `15m`, and `1h`. Existing shadow logging remains, but the active block list now takes precedence.
- **Why:** ETH shifted from profitable `BUY_YES` participation in the prior 24h to mostly `BUY_NO` / `alt_1h_legacy_btc_mode` losses. Baseline `62486e6` had default-on ETH-native momentum confirmation; the post-May-22 allowlist/shadow conversion left ETH admission materially weaker.
- **Hypothesis:** ETH entries should require ETH-native MACD confirmation before either side is admitted, reducing unconfirmed `alt_1h_legacy_btc_mode` fills.
- **Expected outcome:** Next paper run should show fewer unconfirmed ETH default-side fills and clearer skip attribution through `buy_*_no_eth_momentum_confirm`.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after restart on this config).
- **Status:** `pending`

### 2026-05-25 — Cap live lane-calibration alpha at identity

- **What changed:** In [src/analysis/lane_calibration.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_calibration.py), `ALPHA_CLAMP_HI` changed from `2.50` to `1.00`. Raw `alpha_ewma` telemetry can still exceed `1.0`, but live calibration can no longer amplify ETH probabilities away from 50/50; sub-1 shrinkage remains active.
- **Why:** Session attribution showed `alpha_used > 1.0` was damaging the overall session and alt lanes. This makes calibration a one-sided confidence reducer while the next sample accumulates.
- **Hypothesis:** ETH should avoid calibration-driven edge inflation in thin or miscalibrated lanes while still shrinking historically overpredicted setups.
- **Expected outcome:** Next live/non-shadow session should show no effective `alpha_used > 1.0` entries and lower loss concentration in high-alpha buckets.
- **Actual outcome:** `pending` (need ≥15 closed `eth_macro` trades after this change).
- **Status:** `pending`

### 2026-05-24 — Restore ETH coverage after session starvation

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), kept ETH edge floors unchanged, widened oracle basis validation from `10/15` bps to `20/30` bps, and set 5m/15m/1h lane `size_multiplier: 0.3` for calibration sizing.
- **Why:** Session `test_20260524_060424` showed only `3` ETH fills across `57` candidate events (`5.3%` entry rate) and no 15m/1h fills. Most later ETH candidates were blocked by the global daily trade cap; the cap has since been raised for paper. A broad edge loosen was tested and rejected because ETH 15m/1h cached replay produced high trade count but poor WR.
- **Hypothesis:** ETH should resume producing calibration trades after the paper cap fix without lowering edge floors; widened oracle tolerance prevents feed-basis noise from becoming the next starvation point, and `0.3x` sizing limits damage while the lane is remeasured.
- **Expected outcome:** Next paper run should show materially higher ETH candidate-to-entry conversion and at least some 15m/1h fills if markets are scanned.
- **Actual outcome:** `pending` (need ≥15 closed `eth_macro` trades after this change).
- **Status:** `pending`

### 2026-05-21 — ETH high-edge cap moved from skip to sizing clamp

- **What changed:** In [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), `max_edge_updown` no longer rejects ETH entries with `edge_above_cap`. ETH now keeps the trade admissible and caps only the Kelly sizing input used for position sizing (`size_edge_cap=...` added to reason/log context). Backtest parity for the shared engine was updated in [src/backtest/updown_engine.py](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py).
- **Why:** The same ghost pattern affecting BTC and SOL-family shorts applies here conceptually: high computed edge was being treated as proof the move was “already gone,” but in practice it was also suppressing otherwise valid entries before the risk layer could size them appropriately.
- **Hypothesis:** ETH should admit more strong-edge setups without allowing edge inflation to create oversized positions; the size cap should absorb that risk instead of the gate discarding the trade.
- **Expected outcome:** ETH skip telemetry should no longer report `edge_above_cap`; any change in WR / expectancy must be judged on the next ≥15 closed ETH trades, not on ghosts alone.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after this change).
- **Status:** `pending`

### 2026-05-13 — BUY_NO macro-leg audit fix: block SHORT when macro leg is positive

- **What changed:** In [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), `block_counter_macro_leg_updown` now applies symmetrically for ETH up/down entries: LONG blocks when `macro_leg < updown_macro_leg_min_for_long`; SHORT / `BUY_NO` blocks when `macro_leg > updown_macro_leg_max_for_short`. Added `strategies.eth_macro.updown_macro_leg_max_for_short: 0.0` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml). Also clarified comments around neutral ETH fallback and disabled per-market 1H alignment behavior, and widened the shared `_buy_no_ltf_override` type hint so ETH's alt-TA reuse is explicit.

- **Why:** The audit found spec drift: config said `block_counter_macro_leg_updown` blocked LONG with negative macro leg and SHORT with positive macro leg, but ETH code only enforced the LONG side. That allowed `BUY_NO` candidates through even when the lag/catch-up leg pointed upward.

- **Hypothesis:** ETH `BUY_NO` entries should be cleaner because short/down entries whose macro leg is still positive are filtered before sizing. This may reduce ETH short count but should improve the `BUY_NO` lane's average quality.

- **Expected outcome:** ETH skip telemetry may show `macro_leg_blocks_short`; surviving ETH `BUY_NO` entries should have macro-leg context aligned with DOWN. Watch the next ≥15 closed ETH trades, especially the `BUY_NO` subset.

- **Actual outcome:** `pending` (need ≥15 closed `eth_macro` trades after this change).

- **Status:** `pending`

### 2026-05-13 — ETH runs without BTC full-analysis dependency; neutral ETH can trade on ETH tape

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro.neutral_macro_require_spike_or_lag` was changed to `false` and `strategies.eth_macro.btc_1h_regime_gates.enabled` to `false`. In [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), `eth_macro` now requires only ETH full analysis to scan; if BTC full analysis is unavailable it continues with BTC HTF treated as `NEUTRAL`, skips BTC 15m/5m hard-gates that depend on missing full-analysis objects, and avoids BTC 1H regime-based min-edge / sizing adjustments. The bullish BUY_NO override bug was also corrected to use the ETH TA object, not an undefined name.

- **Why:** ETH was being starved or aborted by BTC-side dependencies even when the ETH leg itself was present, and a NameError existed on one BUY_NO override branch. This change makes ETH genuinely ETH-primary instead of “ETH unless BTC analysis is missing”.

- **Hypothesis:** ETH should emit more candidates in neutral ETH 1H conditions and avoid silent full-scan aborts when BTC full analysis is unavailable. Trade quality still depends on the remaining ETH-side follow, edge, price-band, and correlation gates.

- **Expected outcome:** Fewer `analysis_unavailable` and BTC-follow aborts for ETH-only failure cycles; more ETH participation in neutral ETH tape. The lane should no longer crash on the BUY_NO LTF override branch.

- **Actual outcome:** `pending` (need ≥15 closed `eth_macro` trades after this change).

- **Status:** `pending`

### 2026-05-12 — Dashboard Live tab: ETH-first KPI row (UI only)

- **What changed:** Crypto **ETH** panel hero order and labels on the dashboard now show **ETH Δ%** before **BTC Δ%** and **ETH–BTC lag** (not “BTC–ETH”), matching the live strategy log line that **ETH 1H is primary** and BTC context is secondary. No `eth_macro` config or Python signal logic changed in this commit.
- **Why:** Remove operator confusion between UI ordering and code hierarchy.
- **Hypothesis:** n/a
- **Expected outcome:** n/a
- **Actual outcome:** n/a
- **Status:** confirmed (display only; see repo `projects/polymarket-bot/changelog.md` § 2026-05-12 — Dashboard UX for the full bundle).

### 2026-05-12 — ETH 5m 0.02 MACD>signal tier (zero-trade backtest fix)

- **What changed:** Added a `0.02` `macd_line > signal_line` tier to `eth_5m_macd_score` in `strategies._core`. Tiers were previously `{0.06 bull cross, 0.04 hist>0+rising, 0 else, -0.05 against}`. Now: `{0.06, 0.04, 0.02, -0.05}` (LONG; SHORT mirrors). Affects both live `EthMacroStrategy._eth_5m_macd_score` and backtest `_edge_5m_eth_follow_from_df` since both delegate to the shared `_core` function ([src/strategies/_core/eth_follow.py:84](src/strategies/_core/eth_follow.py:84)).

- **Why:** ETH 5m backtests have been producing **zero trades** since the May 7 era. Config `strategies.eth_macro.eth_follow_5m_min_adj: 0.02` (comment "lowered from 0.04 — easier 5m entry in grindy tape") was a silent no-op: the scorer never emitted a value in `(0, 0.04)`, so lowering the threshold from 0.04 → 0.02 admitted no new entries. Every weak-bullish ETH 5m window (MACD above signal but hist not yet positive-and-rising) was rejected as `eth_5m_adj=0 < min=0.02`. The 0.02 tier now honors the config's clear intent and matches SOL's tier ladder.

- **Hypothesis:** ETH 5m will resume producing trades both live and in backtest. Trades that fire at the new 0.02 tier are by definition the weakest-positive signals; WR will likely be lower than 0.04-tier and 0.06-tier entries, but trade count goes from zero to non-zero, restoring lane participation.

- **Expected outcome:** Fresh ETH 5m backtest reports show non-zero `windows_entered`. Live ETH 5m journal entries will show some `reason_parts` containing `ETH5m MACD>signal`. Watch the next ~15 ETH 5m closed trades; if WR < 45% the 0.02 tier may need to be rejected (status `reverted`) and the config threshold raised instead.

- **Actual outcome:** `pending` (need ≥15 closed ETH 5m trades after restart).

- **Status:** `pending`

- **Verification:** 314 tests pass; new test `tests/test_strategy_core_eth_follow.py::test_eth_5m_macd_above_signal_tier` asserts the 0.02 tier fires correctly. Commit: `b009add`. Merged to main as `7b7f503`.

### 2026-05-12 — Live↔backtest ETH 15m follow scoring parity (drift fix)

- **What changed:** Backtest `_edge_15m_eth_follow` used tier scheme `{0.06 cross, 0.04 hist>0+rising, 0.02 MACD>signal&hist>0, 0 else}`. Live `_eth_15m_follow_score` used `{0.06 cross, 0.05 hist>=min_hist+rising, 0 / -0.05 against}`. Different tiers, different scoring. Both now delegate to `eth_15m_follow_score` in `_core` (using the **live** tier ladder: 0.06/0.05/0/-0.05). Backtest gains the live `-0.05` against penalty and the live `0.05` strong-hist tier; loses the 0.02 weak-MACD tier.

  Other primitives extracted in the same refactor pass: `btc_follow_5m_impulse`, `btc_follow_15m_impulse_ok`, `eth_5m_macd_score`, `eth_15m_follow_score`.

- **Why:** Backtest and live `EthMacroStrategy` had hand-copied scoring tiers that drifted apart. The pre-refactor backtest was both more permissive on weak signals (admitted 0.02-tier entries the live scorer didn't have) and more lenient on counter-trend signals (no -0.05 penalty). Aligning to live's tiers means backtest now mirrors what live's scoring produces.

- **Hypothesis:** ETH 15m backtest trade counts may shift slightly; WR should align with live numbers more closely than before. Direction of trade-count change depends on the regime: more `0.05 strong-hist` entries, fewer `0.02 weak-MACD` ones, and harder rejection of counter-trend.

- **Expected outcome:** Fresh ETH 15m backtest WR within statistical noise of live's same-window cohort.

- **Actual outcome:** `pending` (need ≥15 closed ETH 15m trades to compare backtest vs live).

- **Status:** `pending`

- **Verification:** 314 tests pass. Commits: `ff8a315`, `65cbe56`. Merged to main as `7b7f503`.

### 2026-05-09 — Oracle-first + composite score gate for ETH up/down

- **What changed:** `eth_macro` inherits the shared oracle-first and composite up/down gate, with `require_oracle_for_updown=true`, `oracle_max_age_sec=180`, and `oracle_max_basis_bps=10.0`.
- **Why:** ETH up/down resolution references can diverge from exchange spot; stale/missing oracle data should skip before AI or Kelly sizing.
- **Hypothesis:** ETH entries that remain after this change have valid oracle basis/freshness and stronger deterministic setup quality.
- **Expected outcome:** ETH skip telemetry surfaces `oracle_missing`, `oracle_stale`, `oracle_basis_block`, or composite component values when blocked.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after this change).
- **Status:** `pending`

### 2026-05-08 — Tighten ETH 5m calibration after weak live cohort

- **What changed:** Raised [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro.min_edge_5m` from `0.085` to `0.10`, mirrored `backtest.min_edge_eth_5m` to `0.10`, and reduced `strategies.eth_macro.calibration_size_multiplier_5m` from the prior calibration setting to `0.40`.
- **Why:** Current paper session `test_20260508_050455` showed `eth_macro` at `17` closes, `47.1%` WR, `-$2.55`; the 5m slice was the weak lane in the audit (`9` closes, `33.3%` WR, `-$2.23` before restart). ETH is no longer starved, but the recovered 5m participation is not yet profitable.
- **Hypothesis:** A higher 5m edge floor plus smaller 5m stake should cut the weakest marginal ETH 5m entries while still collecting enough calibration data.
- **Expected outcome:** ETH 5m trade count should fall, average ETH 5m loss impact should shrink, and ETH overall WR should recover toward or above `50%`.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after session `test_20260508_151000` restart).
- **Status:** `pending`

### 2026-05-08 — ETH 5m calibration-size cap while keeping lane active

- **What changed:** Added `strategies.eth_macro.calibration_size_multiplier_5m=0.60` in [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) and wired the 5m calibration multiplier in [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py). Added [`scripts/journal_lane_calibration.py`](/Users/mainfolder/Documents/psb-main%201/scripts/journal_lane_calibration.py) to report closed trades by `strategy|window|action`.
- **Why:** ETH 5m should stay active for calibration instead of being disabled/shadow-only, but latest local lane report on `test_20260508_050455` showed `eth_macro|5m|BUY_YES` at `9` closes, `-$2.23`, `33.3%` WR.
- **Hypothesis:** Smaller ETH 5m stakes will keep the live paper calibration sample flowing while limiting drawdown from the currently weak 5m lane.
- **Expected outcome:** ETH 5m continues to produce closed trades, but per-trade loss impact is lower while enough sample accumulates for settings calibration.
- **Actual outcome:** `pending` (need ≥15 closed ETH 5m trades after this change).
- **Status:** `pending`

### 2026-05-07 — Relax ETH per-market 1H bearish suppression (`enforce_alt_1h_alignment: false`)

- **What changed:** Set [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro.enforce_alt_1h_alignment` from `true` to `false`.
- **Why:** After disabling the whole-scan BTC 1H abort, ETH was still being overwhelmingly suppressed by the per-market `eth_1h_bearish` path in the active failure session. This is the next narrow lever to stop all-night ETH silence without removing the rest of the ETH follow, edge, catalyst, and price-band stack.
- **Hypothesis:** Disabling the per-market ETH 1H bearish suppression should let some ETH candidates survive into the remaining downstream gates, producing non-zero participation instead of a silent lane.
- **Expected outcome:** ETH skip mix should show fewer `eth_1h_bearish` suppressions and at least some live ETH signal count. If ETH still shows zero trades, the next suppressors are likely `price_too_far`, `eth_5m_weak_confirm`, and/or timing rather than the 1H bearish filter alone.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades post-change).
- **Status:** `pending`

### 2026-05-07 — Disable ETH whole-scan BTC 1H abort (`btc_follow_1h_required: false`)

- **What changed:** Set [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro.btc_follow_1h_required` from `true` to `false`.
- **Why:** In active paper session `test_20260507_035930`, `eth_macro` ran overnight and produced `0` trades. Session audit showed the whole-scan abort `btc_follow_1h_blocked` fired hundreds of times, preventing ETH from even reaching its later per-market filters. A silent lane is not a viable live strategy.
- **Hypothesis:** Removing the whole-scan BTC 1H abort should let ETH reach the existing per-market 5m/15m BTC-follow, ETH 1H alignment, edge, catalyst, and price-band checks. ETH should start participating again without fully unwinding the later confirmation stack.
- **Expected outcome:** ETH produces non-zero signal counts in the next live paper session. Skip mix should shift away from `btc_follow_1h_blocked` dominance toward per-market gates like `eth_1h_bearish`, `eth_5m_weak_confirm`, `edge_below_min`, and price-band checks.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades post-change).
- **Status:** `pending`

### 2026-05-06 — ETH 15m window widen + 5m edge tighten + 15m hist floor relax (commit `d6da79c`)

- **What changed:**
  - `entry_window_15m_min`: 2.0 → 1.0 ; `entry_window_15m_max`: 16.0 → 18.0
  - `min_edge_5m`: 0.07 → 0.085 (and `backtest.min_edge_eth_5m` mirror to 0.085)
  - `eth_follow_15m_hist_min`: 0.03 → 0.02
- **Why:** Same root pattern as SOL: 15m silent ~8h with `outside_entry_window` dominant, then ETH-specific 15m gates blocking what slips through. `eth_follow_15m_hist_min` is the only ETH 15m-exclusive lever — touching it doesn't unwind the 2026-05-04 `enforce_alt_1h_alignment: true` decision (only 2 days old, deliberately preserved). 5m bleed mitigation parallels SOL.
- **Hypothesis:** The window widen + hist floor relax restores 15m fires; the 5m edge bump compresses the marginal admissions that were losing to resolution variance. Net: ETH 15m becomes the primary fire path again, ETH 5m bleeds less.
- **Expected outcome:** ETH 15m fire rate ≥2 entries per 12h. ETH 5m fire rate drops ~15-25%; PnL trends up.
- **Actual outcome:** `pending` (need ≥15 closed trades post-change).
- **Status:** `pending`

### 2026-05-06 — ETH 5m logic correction: remove 1H-only impulse bypass; restore 15m math

- **What changed:** Removed the ETH 5m `bypass_5m_impulse_btc_1h_ok` admission path in [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), so ETH 5m now requires real short-window BTC impulse or the separate macro-agreement bypass path. At the same time, the same-day experimental ETH 15m math tweak was reverted, restoring the prior 15m score and confidence behavior.
- **Why:** Recent ETH 5m losses clustered around entries tagged `bypass_5m_impulse_btc_1h_ok` with `BTC5m=NONE`, `side_src=hybrid_fallback`, and weak `ETH5m green+rising` confirmation. That is a concrete failing decision path. The earlier 15m tweak targeted the wrong lane for the current issue and was therefore removed.
- **Hypothesis:** Removing the 1H-only 5m bypass should cut the low-quality ETH 5m fallback longs that were entering without real BTC short-window confirmation, while leaving ETH 15m unchanged from its prior behavior.
- **Expected outcome:** ETH 5m should show fewer `BTC5m=NONE` fallback entries and fewer losses from the `bypass_5m_impulse_btc_1h_ok` pattern. ETH 15m behavior should match the pre-tweak logic.
- **Actual outcome:** `pending` (need post-change ETH sample, minimum ~15 closed trades).
- **Status:** `pending`

### 2026-05-06 — ETH 15m logic correction: weaken grind credit, separate confidence, reduce BTC HTF bump

- **What changed:** Adjusted [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py) 15m decision math instead of suppressing trades. `ETH15m green+rising` / `red+falling` score was reduced from `0.05` to `0.04`; the 15m BTC HTF probability bump was reduced from `0.08` to `0.05`; and 15m confidence now comes from a separate helper that gives higher confidence to real crossovers than to weaker grind states.
- **Why:** ETH forensic review showed many losing 15m trades in the `0.10–0.12` edge band were admitted on weak reasons like `15m hist rising` / `15m MACD above signal`, while confidence was still printed around `0.61+` because it was derived almost directly from the same score constants. That made middling confirmation states look stronger than they were.
- **Hypothesis:** Lowering weak 15m follow-through credit and reducing the unconditional BTC HTF push should shrink overstated ETH 15m edge without harming the cleaner crossover-driven setups.
- **Expected outcome:** ETH 15m should emit fewer inflated `0.10–0.12`-style edges from weak grind states, and confidence should better separate true crossovers from softer histogram-follow entries.
- **Actual outcome:** `pending` (need post-change ETH sample, minimum ~15 closed trades).
- **Status:** `pending`

### 2026-05-06 — Reverted ETH guardrail hotfix; switching to root-cause logic audit

- **What changed:** Reverted the same-day ETH-only temporary guardrails that disabled `BUY_NO`, raised a hard 15m edge floor, and imposed a confidence floor. The codebase is back to its pre-guardrail ETH admission behavior while a root-cause logic/settings audit is performed.
- **Why:** The operator explicitly rejected trade-limiting as the intervention. The stated problem is ETH logic/settings quality, not Kelly sizing and not broad trade frequency. Containment without proving the decision logic fault was the wrong move.
- **Hypothesis:** A proper ETH forensic review should isolate the faulty score/gate/settings path more cleanly than hard-coded trade suppression.
- **Expected outcome:** ETH remains behaviorally unchanged until the logic audit identifies a targeted fix backed by evidence.
- **Actual outcome:** `pending`
- **Status:** `reverted ❌`

### 2026-05-06 — ETH guardrails: disable short lane, raise 15m floor, block low-confidence entries

- **What changed:** Added ETH-only guardrails in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) and [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py): `disable_buy_no: true`, `min_edge_15m_guard: 0.12`, and `min_confidence_floor: 0.55`. Strategy logic now skips ETH `BUY_NO`, enforces a 15m effective min edge floor of `0.12`, and rejects ETH entries whose pre-sizing confidence stays below `0.55`.
- **Why:** ETH audit on closed journal exits showed the problem is not broad frequency. The worst buckets were low-confidence ETH (`confidence < 0.55`, net **-$31.07**) and 15m ETH in the `0.10–0.12` edge band (`n=25`, net **-$32.40**). Historical ETH short exposure was also materially poor, and current live ETH shorts route through `BUY_NO`.
- **Hypothesis:** Removing ETH shorts for now and cutting the specific low-quality 15m / low-confidence buckets should improve ETH expectancy without changing Kelly sizing or reducing healthy higher-edge ETH longs.
- **Expected outcome:** ETH should show fewer marginal 15m admissions, no new `BUY_NO` entries, and a cleaner post-change sample concentrated in stronger-confidence, stronger-edge long setups.
- **Actual outcome:** `pending` (need post-change ETH sample, minimum ~15 closed trades).
- **Status:** `pending`

### 2026-05-06 — ETH 5m admission hardening: disable 1H-only impulse bypass

- **What changed:** Disabled [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro.btc_follow_5m_allow_1h_impulse_bypass` (`true` → `false`). ETH 15m behavior and ETH side-resolution mode remain unchanged.
- **Why:** Recent ETH 5m attribution showed the weakest admission bucket was the 5m fallback path tagged `bypass_5m_impulse_btc_1h_ok`: across recent audited sessions that bucket contributed **30** closes for **-$7.38** net, while ETH entries with real BTC 5m impulse were positive (**11** closes, **+$7.15**). Combined with the stop attribution, this indicates ETH 5m was admitting too many longs on 1H continuation alone without real short-term BTC impulse.
- **Hypothesis:** Requiring genuine BTC 5m impulse for ETH 5m should reduce low-quality `hybrid_fallback` BUY_YES entries that later hit `updown_time_stop`, without changing ETH 15m routing or BTC/SOL/HYPE/XRP logic.
- **Expected outcome:** ETH 5m should show fewer `bypass_5m_impulse_btc_1h_ok` entries, lower `updown_time_stop` count from fallback longs, and a cleaner split between real BTC-impulse-follow trades and skipped grind/noise entries.
- **Actual outcome:** `pending` (need post-change ETH sample).
- **Status:** `pending`

### 2026-05-06 — ETH 5m time-stop containment override

- **What changed:** Added ETH-specific up/down exit overrides in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml): `trading.exit_rules.updown_overrides.eth_macro.updown_stop_cents=0.02` and `updown_exit_window_mins=3.0`. No strategy admission/gating logic changed in this step.
- **Why:** Recent ETH 5m attribution across `test_20260504_150648`, `test_20260505_044854`, `test_20260505_184014`, and `test_20260505_225241` showed **36** closes, net only **+$1.18**, with `updown_time_stop` contributing **14** losses / **-$42.40** versus `take_profit` **20** wins / **+$40.25**. The stop bucket is too large relative to realized winners.
- **Hypothesis:** An earlier/tighter ETH-only 5m adverse exit should reduce average ETH time-stop loss size without altering BTC/SOL/HYPE/XRP exit behavior or ETH side-selection logic.
- **Expected outcome:** Over the next ~15 ETH closes, `updown_time_stop` average loss should improve materially from roughly **-$3.03** and ETH net should remain positive with lower downside spikes.
- **Actual outcome:** `pending` (need live sample post-change).
- **Status:** `pending`

### 2026-05-05 — Staged rollback (tier 1): `direction_source` signal_first → hybrid

- **What changed:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro.direction_source`: **`signal_first` → `hybrid`** (post-attribution isolation; SOL/XRP unchanged in same edit).
- **Why:** Multi-session attribution after `test_20260504_150648` showed concentrated ETH losses in **`updown_time_stop`** with **`side_src=signal_first_fallback`** (see [`docs/session_reports/attribution_since_post_may4_150648.md`](docs/session_reports/attribution_since_post_may4_150648.md)).
- **Hypothesis:** Hybrid side resolution (15m signal + 4h proxy agreement for overrides) reduces one-sided `signal_first_fallback` time stops versus legacy BTC-only routing without re-opening unconstrained signal-first noise.
- **Expected outcome:** ETH journal shows more `hybrid_strong_*` / `hybrid_fallback` mix and fewer large clusters of `signal_first_fallback` time-stop losses if the hypothesis holds.
- **Actual outcome:** `pending` (minimum ~15 closed `eth_macro` trades after restart; re-run `scripts/attribution_since.py` for the new session window).
- **Status:** `pending`

### 2026-05-03 — ETH side-selection rollout: discovery recap + live session now emitting signals

- **What changed:** No additional code beyond the committed ETH direction work (`direction_source` hybrid default, `signal_first` toggle, `_resolve_market_side`, thresholds in `config/settings.yaml`; tests in `tests/test_eth_macro.py`). This entry records operator-visible behavior after deploy/restart.
- **Why:** Preserve the research finding and the post-deploy ops signal in one place: previously, ETH’s execution side could behave like a **BTC-HTF-only router**, which **removed a clean signal-driven SHORT path** even when 5m/15m scoring had SHORT-branch structure — see Hermes note `2026-05-02-shorts-direction-research.md`. Hybrid mode requires **15m market signal + BTC HTF proxy** to agree before overriding the legacy side; `signal_first` is an explicit A/B toggle.
- **Hypothesis:** After timing/window parity fixes, ETH should reach side-resolution and economics gates more often; hybrid should occasionally emit `hybrid_strong_short` / `hybrid_strong_long` without opening the floodgates to pure contrarian noise.
- **Expected outcome:** Journal and scan stats show non-trivial `side_source_counts`; SHORT legs appear when spot-implied direction disagrees with BTC HTF but agrees with thresholds; skip mix shifts off pure `outside_entry_window` dominance once windows align.
- **Actual outcome:** Operator reports the bot **is firing** on a **new test run** (early ops pulse); quantitative expectancy / WR / net PnL vs prior epoch remains **`pending`** until **≥15 closed ETH macro trades** post-change. Prior short local A/B scans (2026-05-02 PT) remained inconclusive on closed-trade outcomes.
- **Status:** `pending`

### 2026-05-02 — Direction source modes for ETH side selection (hybrid default, signal_first toggle)

- **What changed:** [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py) now supports ETH side-resolution modes:
  - `btc` (legacy): side from BTC HTF bias only
  - `hybrid` (new default): strong side override only when 15m market signal and 4h proxy both agree
  - `signal_first` (test): 15m market signal can set side directly
  Added config keys in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) under `strategies.eth_macro`:
  `direction_source`, `signal_15m_long_threshold`, `signal_15m_short_threshold`, `signal_4h_long_threshold`, `signal_4h_short_threshold`.
- **Why:** ETH could produce SHORT only when BTC HTF was bearish/neutral branch selected SHORT; operator research asked for a signal-driven SHORT path with conservative thresholds and an explicit signal-first test toggle.
- **Hypothesis:** Hybrid mode should permit clearly bearish SHORT selections without making ETH fully counter-trend/noisy by default; `signal_first` can be toggled for controlled live A/B testing.
- **Expected outcome:** ETH logs show mixed side sources (`hybrid_strong_*` and fallback) instead of BTC-only side routing; SHORT opportunities appear when 15m signal is weak (<0.45) and BTC 4h proxy is bearish.
- **Actual outcome:** Local A/B scan sessions on 2026-05-02 PT (4 cycles each, equal 20s spacing, non-ETH settings frozen) produced no entries in either mode: `hybrid` side_sources=`hybrid_fallback:12, hybrid_strong_long:1` with top skips `outside_entry_window:52, btc_15m_not_following:9`; `signal_first` side_sources=`signal_first_fallback:12` with top skips `outside_entry_window:55, btc_15m_not_following:8`. No closed ETH trades yet in this epoch.
- **Status:** `inconclusive ⚠️`

### 2026-05-02 — Inherited SOL entry-window timing fix + YAML band alignment

- **What changed:** Inherits [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) entry-window bound resolution (no fixed 15m cap on auto-align upper bound) and latency-adjusted `mins_left` before the outside-window check. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro`: `entry_window_15m_max` **45.0**, `entry_window_15m_min` **2.0**, `entry_window_latency_buffer_sec` **12**, `ai_entry_window_15m_min` **2.0**.
- **Why:** ETH uses the shared macro scan loop; without aligned YAML and inherited code fixes, ETH could remain timing-excluded before BTC-follow or oracle gates run.
- **Hypothesis:** ETH sees fewer spurious `outside_entry_window` skips with unchanged edge/oracle/BTC-follow intent.
- **Expected outcome:** Timing eligibility matches SOL macro family fixes; economics gates unchanged.
- **Actual outcome:** `pending` (minimum ~15 closed ETH macro trades after deploy).
- **Status:** `pending`

### 2026-04-30 — Shared lag staleness and explicit inherited price band

- **What changed:** ETH inherits the shared [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) lag staleness fix and the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) up/down price-band fix.
- **Why:** ETH uses the shared macro service/loop; stale lag timestamps and symmetric-by-accident price-band math affected inherited behavior.
- **Hypothesis:** ETH should avoid old BTC impulse carryover and respect future asymmetric `entry_price_min` / `entry_price_max` tuning.
- **Expected outcome:** No default behavior change while ETH remains `0.46–0.54`; safer behavior if the entry band is tuned asymmetrically.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed ETH macro trades after deploy).
- **Status:** `pending`

### 2026-04-29 — Fixed-cycle scheduler + wider ETH timing windows

- **What changed:** [src/main.py](/Users/mainfolder/Documents/psb-main%201/src/main.py) now maintains fixed scan cadence by subtracting cycle runtime before sleeping. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) widened ETH up/down entry windows to 15m `8.0–15.0` and 5m `0.75–5.0` minutes remaining.
- **Why:** Live ops showed ETH markets arriving but repeatedly skipping as `outside_window`. ETH’s stricter BTC-follow and oracle gates cannot be evaluated if timing excludes every market first.
- **Hypothesis:** ETH should reach its actual BTC-follow, RSI, oracle-basis, edge, and price-band gates more often without weakening those gates.
- **Expected outcome:** ETH `outside_window` should drop in ops; any continued no-fire behavior should be attributable to BTC continuation, ETH confirmation, oracle basis, edge, or price-band checks.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed ETH trades after deploy in this epoch).
- **Status:** `pending`

### 2026-04-27 — ETH Chainlink oracle verification + basis veto

- **What changed:** [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) now fetches ETH/USD from the asset-specific Chainlink reference feed on Polygon for the ETH leg, records oracle price/network/basis on the analysis object, and [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py) now blocks entries when `oracle_max_basis_bps` is exceeded. Added `strategies.eth_macro.oracle_max_basis_bps: 10.0` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml).
- **Why:** ETH up/down markets resolve against an oracle source, so exchange spot drifting too far from the reference feed is a real basis risk for short-window binaries.
- **Hypothesis:** Blocking ETH entries when Binance-vs-Chainlink basis is too wide should remove a class of false edge where exchange momentum does not match the likely resolution source.
- **Expected outcome:** Fewer ETH trades during basis dislocations; cleaner ETH live sample when exchange and oracle are aligned.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after deploy in this epoch).
- **Status:** `pending`

### 2026-04-27 — ETH AI timing window + shared marginal-AI path

- **What changed:** ETH now uses the same style of marginal up/down AI assist as the other crypto strategies, but only inside an explicit AI timing band. Added `ai_entry_window_15m_min/max` and `ai_entry_window_5m_min/max` to `strategies.eth_macro` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), and wired [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py) to consult AI on marginal edges only when that timing window is open. Also made ETH read its own `ai_hold_veto_ttl_sec` / `min_edge_5m_ai_override` config instead of inheriting the shared SOL defaults accidentally.
- **Why:** ETH’s custom BTC-follow path was still quant-only while BTC/SOL/HYPE already had LLM tie-break support. The goal of this pass is to let AI weigh in after some candle data has formed, without turning ETH into an AI-first strategy or moving the whole quant entry framework.
- **Hypothesis:** AI should help most on borderline ETH up/down entries when there is enough intra-window information to reason about continuation versus exhaustion. The timing window should reduce low-information AI calls near candle open.
- **Expected outcome:** ETH keeps its BTC-follow quant core, but marginal 5m/15m up/down decisions can now be assisted by AI when timing is favorable; future journal data should show whether AI reduces borderline ETH misses.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after deploy in this epoch).
- **Status:** `pending`

### 2026-04-27 — ETH 15m hardening + explicit oversold SELL block (new evaluation epoch)

- **What changed:** Tightened the 15m BTC-follow confirm path in [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py): weak `ETH15m above signal` / `below signal` states no longer score positively; 15m confirmation now requires either a crossover or same-direction histogram magnitude with in-direction momentum. BTC 15m follow gating also no longer accepts candle drift by itself; it now requires MACD impulse or MACD-plus-candle agreement. Added `strategies.eth_macro.rsi_sell_block_below: 40.0`, `eth_follow_15m_hist_min: 0.03`, and raised `eth_follow_15m_min_adj` to `0.05` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml).
- **Why:** ETH 5m improved materially after the BTC-follow/backtest-fidelity fix, but ETH 15m remained negative. Recent bad `SELL_YES` entries were also showing oversold RSI, which should be blocked explicitly instead of only being implied by softer scoring.
- **Hypothesis:** Hardening 15m confirmation should remove weak continuation entries without materially hurting ETH 5m. The explicit oversold SELL block should filter the bounce-prone `SELL_YES` setups seen in recent ETH paper losses.
- **Expected outcome:** ETH 15m improves versus pre-change baseline while ETH 5m stays directionally similar to its strong 2026-04-27 baseline.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after the change; backtest comparison also pending for this epoch).
- **Status:** `pending`
- **Epoch note:** Treat this as a new ETH evaluation epoch starting **2026-04-27**. Keep prior ETH paper trades in the journal, but do not combine pre- and post-redesign/fidelity-fix ETH samples into a single performance judgement.
- **Pre-change baselines:** 5m [backtest_crypto_ETH_5m_20260427_183330.json](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_ETH_5m_20260427_183330.json); 15m [backtest_crypto_ETH_15m_20260427_183516.json](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_ETH_15m_20260427_183516.json)

### 2026-04-26 — Extreme RSI hard gate for ETH BUY entries

- **What changed:** Added `strategies.eth_macro.rsi_buy_block_above: 80.0` in `config/settings.yaml`; `SolMacroStrategy` now honors optional `rsi_buy_block_above` / `rsi_sell_block_below` hard gates before emitting signals.
- **Why:** Apr 26 paper session showed ETH BUY losses at RSI 84.8 and 83.4 resolving fully against the trade. The prior RSI penalty only reduced estimated probability slightly, so extreme overbought BUYs could still clear `min_edge`.
- **Hypothesis:** Blocking ETH BUY_YES above RSI 80 removes the observed exhaustion entries without changing SOL/HYPE/XRP defaults.
- **Expected outcome:** No ETH BUY_YES entries when RSI is ≥80; lower rate of complete directional misses in overbought ETH windows.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after restart).
- **Status:** `pending`

### 2026-04-21 — UTC blocklist scope-back to Tier A + re-audit cadence

- **What changed:** `strategies.eth_macro.blocked_utc_hours_updown` narrowed from `[1, 15, 17, 20, 23]` to **`[1, 15, 23]`** in `config/settings.yaml`. H17 / H20 removed from the block (downgraded to "watch").
- **Why:** Evidence audit (see `.cursor/plans/block-list-evidence-audit_f364fc11.plan.md`) found ETH's backtest is the strongest base of the three strategies — 807 trades / Mar 1 – Apr 9 2026 ≈ 34 trades/hour. H1 (41% WR, -$42.19), H15 (46.8% WR, -$24.77), and H23 (34.5% WR, -$52.71) are statistically robust and stay blocked. H17 was added on **6 live trades** (0% WR, -$35.90) while backtest was only borderline (-$6.47); 0-for-6 is inside the 95% CI of a 50% WR hour — not statistically separable from noise, and below the `MIN_TRADES=5` confidence bar that `scripts/hourly_heatmap.py` enforces. H20 was 47.4% WR / -$13.14 — borderline and modestly negative. The file's own history (previous `[18, 22]` was a SOL copy-paste that was wrong for ETH because H22 = +$31.18 for ETH) already confirms that small/wrong samples cause real damage; the same principle now applied to H17.
- **Hypothesis:** Tier A blocks keep the protection that matters; removing H17 / H20 lets ETH macro trade ~2 more hours/day and accumulate live evidence in those hours.
- **Expected outcome:** More closed ETH trades/day; within ~2 weeks the per-hour sample on H17 / H20 crosses `MIN_TRADES=5` and we can re-validate on live data instead of paper.
- **Actual outcome:** `pending`.
- **Re-audit cadence:** Weekly `python scripts/hourly_heatmap.py --days 14 --suggest`; re-promote a watched hour to Tier A only on **≥15 trades AND WR < 0.46 AND avg PnL < -$2**.
- **Status:** `pending`

### 2026-04-11 — Entry window auto-alignment (shared SOL path + config)

- **What changed:** Same `_resolve_entry_window_bounds()` behavior as `sol_macro` (class inheritance from `SolMacroStrategy`). `strategies.eth_macro` in `config/settings.yaml` now sets `entry_window_auto_align`, `entry_window_align_scan_interval_sec`, `entry_window_auto_align_max_expand_min`, `entry_window_auto_align_jitter_sec` to match SOL.
- **Why:** ETH up/down uses the identical up/down timing guard; without config parity, ETH could behave differently from SOL despite shared code.
- **Hypothesis:** Parity + cadence-aware widening reduces `outside_entry_window` noise for ETH the same way as SOL.
- **Expected outcome:** ETH eligibility aligned with SOL’s window policy post-deploy.
- **Actual outcome:** `pending` (≥15 closed trades post-deploy or ops evidence).
- **Status:** `pending`

## Review sessions

### 2026-05-18 — Ghost calibration follow-up: ETH BUY_YES remains the main pending tighten/disable candidate

- **Headline:** Settled ghost data still says the weak ETH lane is `BUY_YES`, so any future ETH calibration move should stay focused on longs rather than loosening ETH weak-confirm gates.
- **Evidence snapshot:** Aggregate settled ghost `eth_macro` `BUY_YES` results were `n=2354`, `WR=38.8%`, `total_realized_pct=-560.65`. The largest protective gate remained `eth_macro|1h|BUY_YES|eth_1h_weak_confirm` at `n=721`, `25.5%` WR, `total_realized_pct=-364.83`; `eth_macro|15m|BUY_YES|eth_15m_weak_confirm` was `n=1378`, `46.2%` WR, `total_realized_pct=-115.48`.
- **Possible next move after more data:** If the next settled sample confirms the same pattern, raise ETH long-side effective min edge further or disable the weakest ETH `BUY_YES` lane explicitly.
- **Do not loosen from this note:** Current ghost data does **not** support loosening `eth_1h_weak_confirm` or `eth_15m_weak_confirm`; those gates are still blocking net losers.

### 2026-05-07 — Post-loosening short backtest slice still silent

- **Slice:** [`backtest_crypto_ETH_5m_20260507_134222.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_ETH_5m_20260507_134222.json) and [`backtest_crypto_ETH_15m_20260507_134220.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_ETH_15m_20260507_134220.json)
- **Result:** Both reruns still produced `0` trades even after `btc_follow_1h_required: false` and `enforce_alt_1h_alignment: false`.
- **Meaning:** ETH starvation is broader than the two most obvious gates. The next likely suppressors are ETH-specific entry quality and price/timing filters rather than just the 1H abort/alignment pair.

### 2026-05-04 — Paper `test_20260504_034719`

- **Headline:** Strong slice: **9** closes, **77.8%** WR, **+$13.45** — all `BUY_YES` in this session parse.
- **Artifact:** [`docs/session_reports/session_parse_test_20260504_034719.json`](docs/session_reports/session_parse_test_20260504_034719.json); heatmap [`docs/session_reports/hourly_heatmap_20260504_exit_pt.txt`](docs/session_reports/hourly_heatmap_20260504_exit_pt.txt).

## Lessons learned

_(none yet — add only after data)_

## 2026-06-11 — 5m BUY_NO inversion flip → +EV long (forward-test)
- **Finding:** eth_macro **5m BUY_NO** is structurally inverted — held-to-resolution WR **26%** over n=156 (settled since ~05-20), **$-442** live PnL. On the *same* markets the YES side resolves ITM ~74%, so the short is anti-selective and the cheap long is +EV.
- **Change:** flip BUY_NO→BUY_YES at the 5m edge stage via the own duplicated loop (`eth_5m_buy_no_flip_to_yes: true`). Uses the **complement** of the native est_prob (`max(1−est, 0.50)`) so the normal edge gate then admits only the *cheap* longs (low yes_price) — the +EV pocket. Candidate has already cleared all short-side gates; downstream directional guards inert (`_btc_trade_inputs_enabled()==False`). Default opt-out flag.
- **Also (exit-side, same batch):** 15m up stop 0.16→0.22 (held-WR 69%, +$105 left on table n=16); 15m down stop 0.24→0.28 (held-WR 50%, +$28 n=66). Forward-test only — ghost log can't validate stop changes; basis is the fresh taken-exit settler (`held_win`/`hold_minus_exit_pnl`).
- **Status:** LIVE post-restart in session `test_20260611_181157`. Family flip (sol/xrp/doge/bnb) observed firing (`+buy_no_5m_to_yes_flip side=LONG`); eth/hype loaded but **dormant** until their 5m side next goes short (book was all-LONG at restart).
- **Watch:** confirm flipped longs *convert to fills* over next sessions, not 100% re-skipped by lane_entry_window/composite/iql. Validate flipped-long held-WR vs the ~74% thesis.
