# XRP macro (`xrp_macro`)

XRP **Up or Down** — inherits shared `SolMacroStrategy` signal path with XRP market detection and `XRPUSDT` spot leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 23 | Paper `test_20260504_034719` — [`docs/session_reports/session_parse_test_20260504_034719.json`](docs/session_reports/session_parse_test_20260504_034719.json) |
| Win rate | 69.6% | same |
| Net PnL | +$2.38 | same |
| Avg win / avg loss (session) | +$1.53 / -$3.15 | same |
| BUY_YES / BUY_NO (session) | 22 / +$5.43 vs 1 / -$3.05 | same |

## Change Log

### 2026-08-12 — Emergency cut: XRP 5m shorts disabled, 15m longs preserved

- **What changed:** Set `strategies.xrp_macro.disable_buy_no_5m: true` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml). Confirmed `disable_buy_yes_15m` remains `false` so the positive XRP 15m long lane is not accidentally cut.
- **Why:** Clean baseline since `20260811_2059` showed XRP 5m BUY_NO net `-$18.24`, PF `0.68`, 30% WR. In the same window, XRP 15m BUY_YES was `+$27.10` with PF `6.55`, and XRP 15m BUY_NO was `+$19.33` with PF `1.32`.
- **Hypothesis:** Cutting only XRP 5m shorts removes the bad low-WR cohort while preserving the strongest current XRP 15m edges.
- **Expected outcome:** No new XRP 5m BUY_NO entries; XRP contribution should be dominated by 15m native/fresh-cross/macd positive cohorts.
- **Actual outcome:** `pending` — requires at least 15 closed XRP trades after rollout.
- **Status:** `pending`

### 2026-07-13 — Let XRP 5m DOWN floor survive MINIMAL sizing brake

- **What changed:** In the shared SOL macro sizing path inherited by XRP, per-lane sizing now applies lift → floor → hard cap. `PAUSED` still blocks all overrides; `MINIMAL` skips lift but can honor explicit floor opt-ins. Config added `xrp_macro.lane_min_notional_5m_down: 15` and `xrp_macro.lane_min_notional_ignores_minimal_5m_down: true`.
- **Why:** Operator observed the XRP 5m short engine lane running as a known winner (+$77 same-day live record) while still receiving $5 fills under the global MINIMAL loss-streak brake.
- **Hypothesis:** XRP 5m DOWN keeps the intended $15 floor during MINIMAL while preserving the `PAUSED` kill switch and existing `lane_max_notional_5m_down: 25` hard cap.
- **Expected outcome:** XRP 5m DOWN fills use at least $15 when the opt-in flag is present, unless `PAUSED` is active; hard cap still applies last.
- **Actual outcome:** `pending` — requires restart and at least 15 closed affected XRP 5m DOWN trades after rollout.
- **Status:** `pending`

### 2026-06-15 — DIAGNOSIS (decision OPEN): xrp is -EV on EVERY lane, both sides

- **Finding:** Net-of-fee ghost EV by (window, side), all-time / recent:
  - `1h BUY_NO` −0.160 / −0.160 · `1h BUY_YES` −0.101 / −0.128
  - `5m BUY_NO` −0.091 / −0.071 · `5m BUY_YES` −0.036 / −0.082
  - `15m BUY_NO` −0.073 / −0.062 · `15m BUY_YES` −0.043 / −0.077
- **Read:** xrp is structurally negative everywhere — there is no +EV lane to protect. Unlike DOGE (short-side bleed, long kept) or BNB (long-side bleed, shorts kept), xrp has no clean side to keep. Sitting out the clear bleeders (1h both sides, 5m NO, 15m NO) is most of xrp's volume — effectively disabling the asset.
- **Decision:** **OPEN — no change applied.** This is a near-full-asset sit-out, which is a bigger call than a single lane and was left to the operator. Options: (a) cut the 4 worst lanes (`1h` both, `5m BUY_NO`, `15m BUY_NO`), (b) full xrp sit-out, (c) hold and keep gathering calibration data.
- **Status:** `decision pending` (no code/config change)

### 2026-06-15 — REVIEW (no value change): xrp 15m BUY_NO `min_edge` 0.5 is an intentional sit-out

- **Context:** Kimi flagged `by_tf.15m.min_edge_buy_no: 0.5` as "almost certainly a typo."
- **Finding:** NOT a typo. Git blame → added 2026-06-12 (`a016562`, "sit out eth/xrp 15m BUY_NO") for a confirmed 30% WR bleeder. The clarifying comment was accidentally stripped on 06-14 when the value was normalized `0.50`→`0.5`. Kimi's claim that entry_policy "overrides it to 0.06" is false — `entry_policy.15m.down.min_edge` is **also** `0.5` (config:1398). Sit-out is intentional and effective in both places.
- **Action:** Restored explanatory comments on both lines (config:1252, 1398). No threshold change. Do not lower without ghost evidence the lane has turned.
- **Also rejected:** xrp 5m double size-throttle (`calibration_size_multiplier_5m: 0.3` + entry_policy `size_multiplier: 0.3`) — both apply in code but are inert under the $15 Kelly floor; sizing change deferred per exit/sizing-data rule.

### 2026-06-12 — Remove residual live-entry AI blockers from alt 1h/15m quant path

- **What changed:** In the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) path inherited by XRP, set alt live-entry decision windows to empty and guarded the remaining `ai_window_closed_marginal_updown` branch behind that set. XRP 1h/15m entries now proceed through quant gates only; AI remains for observer/tuning/self-healing surfaces.
- **Why:** Operator clarified that live-entry AI belongs to BTC 1h/15m only. The inherited XRP path still had residual AI timing logic that could reject marginal alt candidates even when no live XRP AI should run.
- **Hypothesis:** Valid XRP 1h quant candidates should no longer be blocked by AI availability, AI timing windows, or marginal AI gate wiring.
- **Expected outcome:** XRP 1h candidates that pass native side, oracle, price, edge, composite, sizing, and risk gates can emit signals with `ai_used=false`.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-06-12 — Exit-policy classifier correction: keep XRP 15m BUY_YES hold+trail

- **What changed:** Reverted the attempted tight-exit change before rollout. In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), `trading.exit_rules.updown_overrides.xrp_macro.window_lane_overrides.15m.up` keeps `updown_stop_loss_pct: 0.32`, `updown_hold_winners_to_resolution: true`, and trail arm/gap `0.10 / 0.15`. The exit-policy recommender now treats neutral positive-gap lanes as hold+trail instead of defaulting to tight TP/SL.
- **Why:** `lane_exit_audit.py` defines `gap = held_pnl - actual_pnl`; positive gap means holding to resolution earned more dollars than the realized exit path. `xrp_macro|15m|BUY_YES` has `n=254`, held WR `53.9%`, realized WR `67.7%`, held `$117.71`, realized `$63.42`, gap `+54.28`. Realized WR was higher, but EV dollars still favor holding.
- **Hypothesis:** Keeping hold+trail plus the widened stop preserves the EV-favored exit path without reverting to the tight path that undercaptured dollars.
- **Expected outcome:** XRP 15m BUY_YES realized dollars should move toward the held-counterfactual gap after at least 15 closed post-change trades.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only; exit changes are not ghost-validatable.

### 2026-06-07 — 1h BUY_YES price-banded floor bump (TIGHT band — XRP turns −EV above 0.66)

- **What changed:** XRP 1h: `1h_buy_yes_bullish_floor_bump: 0.10`, band **0.50–0.66** (via the new shared `_alt_buy_yes_bullish_floor_bump` price-band guard, `sol_macro.py:2129`).
- **Why:** XRP 1h longs reject on `lane_min_edge` from negative model-edge (`est_prob_up ≈0.62 < yes_price`). Unlike the other alts, XRP 1h long is +EV **only** in 0.50–0.65 (+23–34%) and goes **NEGATIVE at 0.70–0.90 (−33%)** — so the band is deliberately tight and the bump small (just enough to clear the 0.50–0.65 cohort). ghost n=146.
- **Hypothesis:** Small banded bump admits the 0.50–0.65 +EV cohort without touching the −EV 0.70–0.90 zone.
- **Expected outcome:** XRP 1h BUY_YES entries appear, concentrated below 0.66.
- **Actual outcome:** `pending` (≥15 closed). Hold-to-resolution caveat applies; forward-test only.
- **Status:** `pending` — needs restart. codex re-derived the band.

### 2026-06-06 — Zero XRP BUY_YES bullish-floor bumps

- **What changed:** `xrp_macro.5m_buy_yes_bullish_floor_bump: 0.22 → 0.0` and `xrp_macro.15m_buy_yes_bullish_floor_bump: 0.13 → 0.0`.
- **Why:** Operator rule: zero every non-winner long bump and keep only the two proven winners, HYPE 5m and BNB 5m.
- **Hypothesis:** XRP BUY_YES entries stop relying on straight probability inflation from the bullish floor and require real raw edge from the live signal path.
- **Expected outcome:** XRP 5m/15m BUY_YES frequency drops where edge was created only by the bump; accepted longs should have cleaner stated-edge provenance.
- **Actual outcome:** `pending` — requires bot restart and at least 15 closed affected XRP trades after rollout.
- **Status:** `pending`

### 2026-06-05 — Block weak 5m BUY_YES bounces against bearish/neutral 1h floor logic

- **What changed:** In the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) path inherited by XRP, 5m `BUY_YES` is blocked when XRP 1h is `BEARISH`, and the 5m bullish floor now requires the actual XRP 1h trend to be `BULLISH`.
- **Why:** Current session `test_20260605_130808` showed the 5m bullish floor inflating fast alt bounces during non-bullish 1h tape. The shared counterfactual would have filtered 12 of 15 alt 5m longs, covering `-26.4559` PnL.
- **Hypothesis:** XRP 5m longs stop firing on fast bounces unless the XRP 1h tape confirms.
- **Expected outcome:** Fewer XRP 5m `BUY_YES` stop-loss cascades during bearish/neutral XRP 1h tape.
- **Actual outcome:** `pending` — requires restart and at least 15 closed affected XRP trades after rollout.
- **Status:** `pending`

### 2026-06-05 — Remove BTC leakage from XRP trade reasons and AI contexts

- **What changed:** In the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) path inherited by XRP, removed BTC labels/values from trade reason strings, scan diagnostics, and AI decision context. Also removed the residual 5m confidence component based on BTC correlation.
- **Why:** BTC was already disabled as a trade gate, but inherited XRP explanations still exposed BTC-looking diagnostics.
- **Hypothesis:** XRP decisions and explanations read as XRP-native only after rollout.
- **Expected outcome:** Post-restart XRP entries/skips no longer include BTC labels in `signal_reason` or marginal AI contexts.
- **Actual outcome:** `pending` — requires restart and at least 15 closed XRP trades after rollout.
- **Status:** `pending`

### 2026-06-01 — Per-lane exit policy: hold+trail on xrp 5m/15m BUY_YES

- **What changed:** Held-vs-realized scorecard shows xrp longs are exit-killed: **5m BUY_YES** held 46% / realized 15% (held −$4.3 vs realized −$23.3), **15m BUY_YES** held 71% / realized 29% (held +$42.2 thrown away as −$9.1). Added hold-winners + trailing floor (`arm 0.10 / gap 0.15`) to both. (15m BUY_NO already on hold+trail from 1ea32a5.)
- **Why:** This corrected an earlier wrong instinct to *suppress* xrp longs — held-WR proves the entries are directionally fine (even +$42 on 15m); the **exit** is the bleed, not the entry. Suppression would have thrown away good signal.
- **Hypothesis:** holding xrp longs to resolution with a trailing floor recovers the directional edge.
- **Expected outcome:** xrp BUY_YES realized-WR converges toward held-WR (46%/71%); the −$23/−$9 realized losses shrink or flip positive.
- **Actual outcome:** `pending` — forward-test only; needs bot restart.
- **Status:** `pending`

### 2026-06-01 — Reopen ghost-positive 5m native BUY_NO gate

- **What changed:** Set `xrp_macro.disable_buy_no_5m_native: false` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), reopening the shared 5m native BUY_NO path previously ghost-logged as `buy_no_5m_native_suppressed`.
- **Why:** Settled ghosts for `xrp_macro|5m|BUY_NO|buy_no_5m_native_suppressed` now show `n=651`, `WR=59.8%`, missed EV `357.060`, protected loss `262.000`, net gate value `-95.060`; the current session also shows XRP as the largest drag, so reopening the positive short-side ghost cohort is the least blunt way to address the gate without disabling upside lanes.
- **Hypothesis:** Reopening XRP 5m native BUY_NO should improve XRP trade mix by admitting a ghost-positive downside cohort while existing BUY_YES soft repairs continue filtering overconfident upside entries.
- **Expected outcome:** XRP 5m native BUY_NO entries resume; after at least 15 closed post-change XRP 5m BUY_NO trades, WR should remain above breakeven and XRP aggregate PnL should stop being dominated by missed downside opportunities.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-31 — Early-TP regret: hold winners + positive trailing floor (BUY_NO 15m)

- **What changed:** Added `updown_hold_winners_to_resolution: true` + positive trailing floor (`updown_trail_arm_pct: 0.10`, `updown_trail_gap_pct: 0.15`) to the existing `xrp_macro` 15m `down` (BUY_NO) block in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml). New floor mechanic in [`effective_updown_stop_loss_pct`](/Users/mainfolder/Documents/psb-main%201/src/execution/updown_exit_shared.py): once peak clears +10%, exit floor trails at `peak − 15%`, can be positive (banks gains), never wider than base stop; coexists with the lane's existing in-profit tighten via `max()`.
- **Why:** Settled `take_profit` rows: XRP BUY_NO 15m n=11, **+$42.3 regret (+$3.84/trade, strongest per-trade), 100% held-WR**. A short lane that is directionally *right* here (not the usual BUY_NO inversion) and was exiting too early at +30%.
- **Hypothesis:** Holding the winning shorts with a trailing floor captures the run XRP BUY_NO 15m gave up at +30%, protecting most give-back on reversals.
- **Expected outcome:** XRP BUY_NO 15m realized PnL per winner rises above the +30% cap; fewer `take_profit` exits.
- **Actual outcome:** `pending` — forward-test only (no ghost validation for exits); needs bot restart. Watch XRP BUY_NO 15m in `trades_settled.jsonl`. Caveat: n=11; if the 100% held-WR proves to be a single cluster, revisit.
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
- **Why:** Over a 5.5h window (`decision_layer.jsonl`) the gate approved only **3%** of AI-evaluated candidates — the model returned **HOLD 77%** of the time (conservative system prompt + a 0.60 confidence bar that near-coin-flip 15m/1h markets can't honestly clear), and HOLD was a hard veto. XRP was fully shut off on the AI path: **0/16** approved (all marginal; 12 HOLD + 4 timeout).
- **Hypothesis:** Restoring marginal admission (blocked only on confident AI opposition) reopens 15m/1h frequency without losing the AI's ability to stop a conviction-wrong trade.
- **Expected outcome:** XRP 15m/1h marginal entries resume; AI still vetoes confident-opposite cases.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only (AI-gate behavior is not ghost-validatable); needs bot restart to load.

### 2026-06-01 — BUY_YES overconfidence soft repair

- **What changed:** Added lane-specific BUY_YES soft repairs for XRP `5m native`, `15m native`, and `5m neutral_fallback_1h`: probability haircuts plus min-edge adders, with an extra small min-edge add when oracle basis is elevated. No XRP BUY_YES lane is disabled.
- **Why:** Past-3-day attribution showed XRP BUY_YES as the worst WR source: `xrp_5m_native` `10` trades / `10.0%` WR, `xrp_15m_native` `6` trades / `16.7%` WR, and `xrp_5m_neutral_fallback_1h` `5` trades / `20.0%` WR.
- **Hypothesis:** XRP BUY_YES false positives are overconfidence/edge-quality failures; haircuts and min-edge adders should force weak entries to settle as ghosts before any hard lane pause is considered.
- **Expected outcome:** Lower XRP BUY_YES throughput from weak overconfident cohorts, with remaining trades showing stronger post-repair edge.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-06-01 — Revert XRP BUY_YES disable

- **What changed:** Reverted the same-day XRP `5m up`, `15m up`, and `1h up` entry-policy disables. XRP BUY_YES returns to prior gating.
- **Why:** Operator rejected disabling losing lanes as a lazy WR fix. XRP needs direct diagnosis of why BUY_YES entries are false-positive heavy.
- **Hypothesis:** Restoring XRP BUY_YES preserves evidence for a proper repair pass across probability construction, price bands, BTC-secondary gates, and oracle basis.
- **Expected outcome:** XRP BUY_YES resumes prior admission; no disabled-lane effect from the rejected WR-mode change.
- **Actual outcome:** `pending`
- **Status:** `reverted ❌`

### 2026-06-01 — Disable XRP BUY_YES for WR target

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), disabled XRP `5m up`, `15m up`, and `1h up` entry-policy lanes.
- **Why:** Past-3-day BUY_YES review showed XRP BUY_YES at `25` trades / `20.0%` WR / `-$45.79`, with `5m` at `18` trades / `16.7%` WR and `15m` at `7` trades / `28.6%` WR.
- **Hypothesis:** XRP stops dragging aggregate BUY_YES WR while downside and ghost logging continue; upside can be re-enabled only after a ghost/live cohort clears the `55%` minimum.
- **Expected outcome:** XRP BUY_YES entries cease; XRP upside candidates should appear as disabled-lane ghosts for future review.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-31 — Suppress anti-predictive 5m-native BUY_NO shorts

- **What changed:** Set xrp_macro `disable_buy_no_5m_native: true`; inherits the 5m BUY_NO sit-out in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), ghost-logged as `buy_no_5m_native_suppressed`. Commit `5d8cbc0`. Full rationale in `sol_macro.md` same date.
- **Why:** `xrp_5m_native` BUY_NO held-to-resolution WR was 16.7%, vs XRP 15m-native at a healthy 65.4% — the 5m short signal is inverted. (XRP BUY_NO overall is the least-bad alt at 52.5%, but the 5m cell is the exception.)
- **Hypothesis:** Removing the inverted 5m short cell preserves XRP's strong 15m short lane while cutting the 5m bleed.
- **Expected outcome:** `buy_no_5m_native_suppressed` appears for XRP; 15m BUY_NO and all BUY_YES unchanged.
- **Actual outcome:** `pending` (needs restart + ~15 closed trades)
- **Status:** `pending`

### 2026-05-31 — XRP 1h starvation window repair after bad morning sample

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), widened XRP 1h entry windows from `60.0` to `360.0` minutes in both `by_tf.1h` and `entry_policy.window_side_overrides.1h` for `up` and `down`.
- **Why:** Active session `test_20260531_041319` showed XRP at `0/7`, `-$28.49`; those losses were concentrated in 5m/15m `BUY_YES`, while the 1h XRP path was effectively starved. May 30 settled ghosts showed XRP 1h rejected candidates at `54.1%` WR (`n=471`), with `xrp_macro|1h|LONG|lane_min_edge` at `98.6%` WR / `+19.9%` ROI (`n=74`), indicating the current 1h starvation is the wrong place to restrict XRP learning.
- **Hypothesis:** XRP should shift some sample collection toward the less-starved 1h path while existing edge, price-band, oracle, and `0.3x` 1h sizing controls limit exposure.
- **Expected outcome:** XRP 1h entries should appear in the next comparable run; XRP 5m/15m BUY_YES remains a watch item because today's closed sample was materially bad.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-28 — Inherited `sell_5m_low_corr` hard skip downgraded

- **What changed:** XRP inherits the shared SOL-family scan path in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py); `sell_5m_low_corr` no longer hard-skips 5m `BUY_NO` candidates and is now diagnostic context only.
- **Why:** Calibration review showed the hard BTC-correlation skip could throw away valid alt-native downside entries.
- **Hypothesis:** XRP 5m downside candidates should reach later edge/risk gates instead of being blocked by BTC correlation alone.
- **Expected outcome:** Future XRP diagnostics should stop reporting `sell_5m_low_corr` as a hard skip; review after >=15 closed post-change XRP trades.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-26 — Timeframe-scoped entry control config

- **What changed:** Moved XRP entry-control thresholds and windows from legacy flat timeframe keys (`min_edge_5m`, `entry_window_*`) into canonical `defaults` / `by_tf` config, and routed shared macro entry policy reads through the timeframe resolver.
- **Why:** Static config audit showed the same 5m/15m values duplicated in flat keys and lane policy overrides, making it unclear which tuning surface was authoritative.
- **Hypothesis:** XRP 5m/15m/1h tuning changes should stay scoped to their `by_tf` cell with no cross-timeframe bleed.
- **Expected outcome:** Startup logs should show XRP `by_tf` overrides; focused tests should preserve the same effective min-edge/window values.
- **Actual outcome:** `pending` (config migration only; need >=15 closed XRP trades before performance judgment).
- **Status:** `pending`

### 2026-05-26 — Resolver metadata parity for shared macro signals

- **What changed:** Added BTC-compatible resolver metadata to the shared macro signal path used by XRP: `conflict_type`, `resolver_path`, `htf_side`, `quant_side`, and `momentum_side`, with journal and position persistence.
- **Why:** XRP had HTF and oracle metadata, but direction-resolution details were not first-class like BTC.
- **Hypothesis:** Future ghost/trade reviews can separate HTF-aligned, quant-disagree, and momentum-disagree XRP entries without changing entry behavior.
- **Expected outcome:** New XRP entries should include resolver metadata in journal extras and `entry_signal`.
- **Actual outcome:** `pending` (need post-change entries to verify field coverage).
- **Status:** `pending`

### 2026-05-26 — Hold up/down winners to resolution

- **What changed:** Enabled `trading.exit_rules.updown_hold_winners_to_resolution` and suppressed up/down `take_profit` exits while that flag is true.
- **Why:** XRP showed sizing, exit, and selection damage. This addresses the exit side without further entry-gate tightening.
- **Hypothesis:** Correct XRP trades should realize closer to binary-resolution payoff when held through settlement.
- **Expected outcome:** Fewer `take_profit` exits, more `RESOLVED:* (real)` exits, and higher avg-win dollars.
- **Actual outcome:** `pending` (need >=15 closed XRP trades after restart).
- **Status:** `pending`

### 2026-05-26 — BUY_YES recovery tweak after rollback

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), changed XRP `alt_momentum_confirm.buy_yes` to `15m` only while keeping `buy_no` confirmation on `5m`, `15m`, and `1h`.
- **Why:** XRP was still a winner but started giving back; the goal is not to suppress BUY_YES entirely while fixing unconfirmed downside flow.
- **Hypothesis:** XRP BUY_YES remains available when edge/price/oracle checks clear, while BUY_NO remains protected by explicit momentum confirmation.
- **Expected outcome:** Next paper run should show XRP BUY_YES not globally starved, with fewer unconfirmed downside fills.
- **Actual outcome:** `pending` (need ≥15 closed XRP trades after restart on this config).
- **Status:** `pending`

### 2026-05-26 — Pre-restart rollback of post-May-22 momentum guard regression

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), added active `xrp_macro.alt_momentum_confirm` blocking for `BUY_YES` and `BUY_NO` on `5m`, `15m`, and `1h`; restored XRP base `min_edge 0.085 -> 0.09` and restored XRP up-lane edge overrides to the May 22 levels.
- **Why:** XRP remained a net winner but started giving back while the shared alt macro path admitted far more unconfirmed default-side trades. This rolls XRP back toward the confirmed-entry baseline without disabling the lane.
- **Hypothesis:** XRP should keep higher-quality confirmed trades while reducing marginal fills admitted by the post-May-22 exploration posture.
- **Expected outcome:** Next paper run should show fewer unconfirmed XRP entries, especially default-side 5m/15m fills, with outcome pending until enough closed trades accrue.
- **Actual outcome:** `pending` (need ≥15 closed XRP trades after restart on this config).
- **Status:** `pending`

### 2026-05-25 — Cap live lane-calibration alpha at identity

- **What changed:** In [src/analysis/lane_calibration.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_calibration.py), `ALPHA_CLAMP_HI` changed from `2.50` to `1.00`. Raw `alpha_ewma` telemetry can still exceed `1.0`, but live calibration can no longer amplify XRP probabilities away from 50/50; sub-1 shrinkage remains active.
- **Why:** Session attribution showed high-alpha amplification was damaging alt lanes overall. XRP shares the macro calibration path, so it should use the same shrink-only posture.
- **Hypothesis:** XRP avoids calibration-driven edge inflation while still shrinking overpredicted lanes.
- **Expected outcome:** Next live/non-shadow session should show no effective `alpha_used > 1.0` XRP entries.
- **Actual outcome:** `pending` (need ≥15 closed `xrp_macro` trades after this change).
- **Status:** `pending`

### 2026-05-17 — Ghost-mode reopen: XRP 15m BUY_YES only

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), eased only the `xrp_macro` `15m` `BUY_YES` lane: `entry_price_max_15m_yes_side` **0.55 → 0.57**, and the `entry_policy.window_side_overrides.15m.up.min_edge` **0.09 → 0.085** with matching `entry_price_max` **0.55 → 0.57**. No `5m` XRP settings changed, and ETH/HYPE were left untouched.
- **Why:** The operator explicitly wants a ghost-mode calibration probe, not another loss-containment tightening pass. Recent XRP evidence does **not** support reopening `5m BUY_YES`, but `15m BUY_YES` was close enough to flat to justify a small upward-side admission probe while keeping the bad short-window path unchanged.
- **Hypothesis:** A modest reopen on XRP `15m BUY_YES` should increase live sample count in the salvageable lane without reintroducing the clearly poor `5m BUY_YES` behavior.
- **Expected outcome:** Over the next ~15 closed `xrp_macro` `15m BUY_YES` trades, trade count should rise versus the prior gate, and net PnL should stay near flat or improve. If the reopened cohort turns clearly negative, revert the `15m` reopen and keep XRP upside learning restricted to other paths.
- **Actual outcome:** `pending` (need ≥15 closed XRP `15m BUY_YES` trades after this change).
- **Status:** `pending`

### 2026-05-09 — Oracle-first + composite score gate for XRP up/down

- **What changed:** `xrp_macro` inherits the shared oracle-first and composite up/down gate, with `require_oracle_for_updown=true`, `oracle_max_age_sec=180`, and `oracle_max_basis_bps=10.0`.
- **Why:** XRP short-window entries should not size when the oracle reference is missing/stale or materially off exchange spot.
- **Hypothesis:** Weak XRP entries become explicit oracle/composite skips, while remaining trades have cleaner resolution-source alignment.
- **Expected outcome:** XRP skip telemetry includes oracle freshness/basis and composite component values whenever this gate blocks.
- **Actual outcome:** `pending` (need ≥15 closed XRP trades after this change).
- **Status:** `pending`

### 2026-05-08 — Tighten XRP 5m diagnostic lane

- **What changed:** Raised [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.xrp_macro.min_edge_5m` from `0.07` to `0.085`, mirrored `backtest.min_edge_xrp_5m` to `0.085`, and reduced `strategies.xrp_macro.calibration_size_multiplier_5m` to `0.30`.
- **Why:** Current paper session `test_20260508_050455` showed `xrp_macro` at `8` closes, `37.5%` WR, `-$1.95`; the XRP 5m slice was `2` closes, `0%` WR, `-$1.98`, and the latest stored `XRP 5m` backtest remains strongly negative. The lane should remain diagnostic, not normal risk.
- **Hypothesis:** A higher XRP 5m edge floor and smaller 5m size should reduce weak short-window XRP entries while keeping limited data collection alive for future BUY_YES/BUY_NO calibration.
- **Expected outcome:** XRP 5m entries should be rarer and smaller; XRP total PnL should be less exposed to 5m drawdown while 15m continues to provide the primary sample.
- **Actual outcome:** `pending` (need ≥15 closed XRP trades after session `test_20260508_151000` restart).
- **Status:** `pending`

### 2026-05-08 — XRP 5m calibration-size cap while keeping lane active

- **What changed:** Added `strategies.xrp_macro.calibration_size_multiplier_5m=0.60` in [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml). XRP inherits the 5m calibration multiplier from [`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py). Added [`scripts/journal_lane_calibration.py`](/Users/mainfolder/Documents/psb-main%201/scripts/journal_lane_calibration.py) to report closed trades by `strategy|window|action`.
- **Why:** XRP 5m should stay active for calibration instead of being disabled/shadow-only, but latest local lane report on `test_20260508_050455` showed `xrp_macro|5m|BUY_YES` at `2` closes, `-$1.98`, `0.0%` WR.
- **Hypothesis:** Smaller XRP 5m stakes will preserve data collection for both YES/NO calibration while limiting loss impact from the still-unproven 5m lane.
- **Expected outcome:** XRP 5m continues to collect closed-trade samples, with downside reduced until the lane has enough post-change data to judge gates and BUY_NO settings.
- **Actual outcome:** `pending` (need ≥15 closed XRP 5m trades after this change).
- **Status:** `pending`

### 2026-05-07 — Full intervention: restore bearish-1H BUY_YES suppression and tighten XRP 5m catalyst gate

- **What changed:**
  - [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml)
  - `strategies.xrp_macro.enforce_alt_1h_alignment: false -> true`
  - `strategies.xrp_macro.require_btc_catalyst_5m: false -> true`
- **Why:** In the active failure run `test_20260507_035930`, all 4 XRP closes were `15m` `BUY_YES`; 3 lost on `updown_time_stop` for `-$7.40` total and every entry was taken with `ALT_HTF=BEARISH`. The bearish-1H suppression branch in the shared `SolMacroStrategy` only blocks `BUY_YES` longs while still allowing `BUY_NO` diagnostically, so restoring it is a cleaner fix than shrinking price bands further. Separately, latest `XRP 5m` backtest remains strongly negative (`770` trades / `-492.825`), so unstimulated 5m entries do not deserve relaxed admission.
- **Hypothesis:** Restoring bearish-1H long suppression should stop XRP from repeatedly buying into a bearish alt context while preserving the BUY_NO path the prior forensic work wanted to reopen. Requiring a BTC catalyst on `5m` prevents the worst short-window path from re-expanding while `15m` is re-evaluated.
- **Expected outcome:** Fewer XRP `BUY_YES` longs in bearish-alt conditions; higher share of valid BUY_NO participation when the market side flips; no 5m expansion unless there is an actual BTC impulse/lag catalyst.
- **Actual outcome:** `pending` (current live process has not been restarted onto this config yet).
- **Status:** `pending`

### 2026-05-06 — Candidate B revert: restore tighter time-stop / exit window (commit `d6da79c`)

- **What changed:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `trading.exit_rules.updown_overrides.xrp_macro`:
  - `updown_stop_cents`: 0.04 → **0.03** (back to global parity)
  - `updown_exit_window_mins`: 1.5 → **2.25** (back to global parity)
- **Why:** Live 48-72h slice showed XRP 15m at 66.7% WR / +$3.55 net with -$28.47 of time_stop loss across 9 trades (0% WR on time_stop) wiping ~70% of take_profit gains. Candidate B (deployed earlier) had widened the stop and shortened the exit window — the wrong direction for a strategy bleeding on time-stop. The earlier "0.09 → 0.11 min_edge" recommendation from the forensic audit was withdrawn after live data showed 0.09–0.11 bucket too thin (3 trades) and ≥0.11 bucket already at 67% WR / +$4.10 (no edge-tightening warranted).
- **Hypothesis:** Tighter stop (3¢) + longer adverse-check window (2.25m) catches losers earlier and gives take_profit more time to hit. Brings XRP back to the same exit profile as HYPE/SOL/BTC for clean attribution baseline.
- **Expected outcome:** XRP 15m avg time-stop loss compresses from -$3.16 (baseline) toward < -$2.50; take_profit count stable or up.
- **Actual outcome:** `pending` (need ≥10 XRP 15m closed trades post-change).
- **Status:** `pending`
- **Failure criteria → escalate:** if avg time-stop loss unchanged or worse after 24h, the lever isn't here — investigate exit-routing in `sol_macro.py` `updown_time_stop` path.

### 2026-05-06 — Candidate C rollout (15m BUY_YES high-price tightening)

- **What changed:** Added 15m BUY_YES-specific up/down price cap support in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) via optional config key `entry_price_max_15m_buy_yes` (defaults to existing `entry_price_max` behavior). Set [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.xrp_macro.entry_price_max_15m_buy_yes=0.55` (from implicit `0.58`).
- **Why:** Forensic audit identified poor recent XRP performance with one-sided BUY_YES participation and loss concentration; tightening rich 15m BUY_YES entries reduces late/overpriced long exposure without constraining 5m or BUY_NO paths.
- **Hypothesis:** Fewer high-price 15m BUY_YES fills should lower negative expectancy in the affected bucket while preserving valid mid-band entries.
- **Expected outcome:** Over next ~20 XRP closes, 15m BUY_YES entries above 0.55 should drop toward zero, with improved net PnL and reduced 15m adverse exits.
- **Actual outcome:** `pending` (need post-change sample).
- **Status:** `pending`

### 2026-05-05 — Forensic remediation rollout (Candidate A + B)

- **What changed:** Applied targeted XRP remediations from the forensic audit: in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), set `xrp_macro.enforce_alt_1h_alignment=false` and lowered `xrp_macro.min_edge_buy_no` from `0.10` to `0.08` (Candidate A). Added `trading.exit_rules.updown_overrides.xrp_macro` with `updown_stop_cents=0.04` and `updown_exit_window_mins=1.5`; [src/execution/live_testing.py](/Users/mainfolder/Documents/psb-main%201/src/execution/live_testing.py) now supports per-strategy up/down exit overrides (Candidate B) while preserving global defaults for other lanes.
- **Why:** Forensic report on recent bad cluster (`test_20260504_150648`, `test_20260505_044854`, `test_20260505_184014`) showed XRP net `-$11.62`, with `updown_time_stop` losses `-$23.84` (76.4% of negative PnL) and zero recent BUY_NO executions despite positive BUY_NO backtest contribution.
- **Hypothesis:** Restoring BUY_NO admission and softening XRP-specific late-window stop behavior should reduce one-sided BUY_YES drawdowns and lower XRP time-stop loss concentration without collateral effects to BTC/SOL/ETH/HYPE exits.
- **Expected outcome:** Over next ~20 XRP closes: BUY_NO share ≥10%, XRP net PnL non-negative, and `updown_time_stop` share of XRP negative PnL <30%.
- **Actual outcome:** `pending` (need live sample post-change).
- **Status:** `pending`

### 2026-05-05 — XRP loss concentration note (hold config for +1h data)

- **What changed:** No strategy code/config edits in this step. Logged session diagnosis for `test_20260505_044854`: `xrp_macro` closed **15** trades, net **-$3.90**; `updown_time_stop` exits were **6 trades / -$14.65** while `take_profit` exits were **7 trades / +$9.70**. BUY/side mix was effectively one-sided (`BUY_YES` only in that session).
- **Why:** Loss concentration is in the exit path and side mix; immediate tuning without another short sample risks overfitting to one session.
- **Hypothesis:** If underperformance persists over the next ~1 hour of additional XRP closes, the first adjustment should target `updown_time_stop` behavior and/or BUY_NO admission parity before widening BUY_YES risk.
- **Expected outcome:** Collect another short session slice, then decide whether to: (a) condition/widen time-stop logic for XRP, (b) re-enable practical BUY_NO participation when gates permit, or (c) keep unchanged if distribution normalizes.
- **Actual outcome:** `pending` (waiting for additional ~1h data collection and re-review).
- **Status:** `pending`

### 2026-05-02 — Inherited SOL entry-window timing fix + YAML band alignment

- **What changed:** Inherits [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) entry-window bound resolution (no fixed 15m cap on auto-align upper bound) and latency-adjusted `mins_left`. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.xrp_macro`: `entry_window_15m_max` **45.0**, `entry_window_15m_min` **2.0**, `entry_window_latency_buffer_sec` **12**, `ai_entry_window_15m_min` **2.0**.
- **Why:** XRP inherits `SolMacroStrategy`; timing gates run before BTC-move and macro-specific sizing gates.
- **Hypothesis:** XRP reaches real economics filters more often with the same risk posture.
- **Expected outcome:** Timing parity with SOL macro family.
- **Actual outcome:** `pending` (minimum ~15 closed XRP macro trades after deploy).
- **Status:** `pending`

### 2026-04-30 — Shared lag staleness and explicit inherited price band

- **What changed:** XRP inherits the shared [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) lag staleness fix and the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) up/down price-band fix.
- **Why:** XRP uses the shared BTC-lag macro architecture and inherited scan loop; stale lag timestamps and symmetric-by-accident price-band math affected inherited behavior.
- **Hypothesis:** XRP should suppress old BTC impulse carryover while preserving fresh impulse opportunities, and future asymmetric price-band tuning should apply exactly.
- **Expected outcome:** No default behavior change while XRP remains `0.46–0.54`; safer stale-lag behavior and explicit config fidelity.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed XRP macro trades after deploy).
- **Status:** `pending`

### 2026-04-29 — XRP-specific beta clamp and low-correlation tuning

- **What changed:** [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) now supports asset-specific dynamic-beta clamps, and [src/strategies/xrp_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/xrp_macro.py) now rebuilds its `XRPUSDT` lag service from `strategies.xrp_macro` config. Added XRP-specific `dynamic_beta_min/max/extreme_max` plus `low_corr_threshold_1h` / `low_corr_damping` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml).
- **Why:** XRP was inheriting SOL-oriented correlation damping and beta assumptions even though XRP commonly runs at lower BTC correlation and a different response profile.
- **Hypothesis:** Lowering the low-correlation trigger and using milder damping should stop XRP from being permanently treated as a low-conviction SOL clone, while still shrinking edges when BTC/XRP linkage is genuinely weak.
- **Expected outcome:** More XRP windows should survive the correlation confidence layer and be evaluated on their actual macro/LTF quality rather than being routinely compressed by SOL-style defaults.
- **Actual outcome:** `pending` (need ≥15 closed XRP macro trades after deploy for first live review).
- **Status:** `pending`

### 2026-04-29 — XRP liquidity floor lowered + explicit timing config

- **What changed:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) lowered `strategies.xrp_macro.min_liquidity` from `5000` to `1000`, and added explicit XRP up/down entry windows: 15m `8.0–15.0`, 5m `0.75–5.0`, with auto-alignment enabled.
- **Why:** Live ops showed XRP mostly blocked by `liquidity` and `outside_entry_window`. The `5000` liquidity floor was materially stricter than BTC/SOL/ETH and far above HYPE, despite XRP still needing live validation sample size.
- **Hypothesis:** XRP should collect a usable paper sample instead of being filtered out before edge evaluation, while still retaining edge, price-band, BTC-move, max-position, and sizing gates.
- **Expected outcome:** XRP should show fewer liquidity/timing skips and more cycles reaching `edge_below_min`, price-band, or BTC/ALT confirmation gates. Closed-trade validation remains required before treating the lane as proven.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed XRP macro trades after deploy).
- **Status:** `pending`

### 2026-04-27 — XRP oracle coverage via shared macro service

- **What changed:** Through the shared [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) update, XRP now has an asset-specific Chainlink XRP/USD reference feed on Polygon available to the macro analysis object. This pass does not enable a hard XRP basis veto yet; it adds the oracle coverage so basis can be observed first.
- **Why:** XRP was previously missing an asset-specific oracle reference in the strategy stack.
- **Hypothesis:** Observing XRP basis before adding a hard gate is the safer rollout path for a lane with a very small live sample.
- **Expected outcome:** XRP analysis can now surface oracle-vs-exchange divergence for later review.
- **Actual outcome:** `pending`.
- **Status:** `pending`

### 2026-04-27 — Enable XRP AI assist and shared timing window

- **What changed:** Turned `strategies.xrp_macro.use_ai` and `use_ai_updown` on in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), and added the same `ai_entry_window_*`, `ai_hold_veto_ttl_sec`, and `min_edge_5m_ai_override` controls used by the other macro up/down lanes. [src/strategies/xrp_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/xrp_macro.py) now reads its own AI-hold settings instead of inheriting the SOL defaults accidentally.
- **Why:** XRP was the only active crypto macro lane still running quant-only despite the shared AI-assist path already existing in `SolMacroStrategy`.
- **Hypothesis:** XRP should stay quant-first, but AI can help on marginal up/down entries once enough candle data has formed.
- **Expected outcome:** XRP becomes operationally consistent with the other crypto macro lanes, with AI only participating inside the configured timing band.
- **Actual outcome:** `pending` (need ≥15 closed XRP macro trades after deploy).
- **Status:** `pending`

### 2026-04-26 — Sample-size caution from first XRP macro batch

- **What changed:** No code/config change in this entry; this logs the first meaningful XRP macro paper slice.
- **Why:** Session produced only 3 XRP closes (all wins), which is too small to treat as validated edge.
- **Hypothesis:** Current XRP entries skew to higher edge (roughly 0.11–0.145) with earlier profit-taking, which can look strong in tiny samples but may not hold as sample broadens.
- **Expected outcome:** Keep strategy enabled and collect larger sample before changing gates or sizing.
- **Actual outcome:** `pending` (need ≥15 closed XRP macro trades for first confidence pass).
- **Status:** `pending`

## Review sessions

### 2026-05-07 — Active failure run read: XRP is a 15m BUY_YES problem, not a broad lane-wide mystery

- **Session:** `test_20260507_035930`
- **Closed trades (`xrp_macro`):** `4`
- **Net PnL:** `-$6.55`
- **Exit shape:** `3x updown_time_stop`, `1x take_profit`
- **Entry shape:** all `15m` `BUY_YES`, all with `ALT_HTF=BEARISH`, entry prices `0.435`, `0.495`, `0.50`, `0.51`
- **Read:** this is the cleanest case for restoring shared bearish-1H `BUY_YES` suppression. The live problem is not that XRP trades too often; it is that it keeps taking long-side 15m entries against its own bearish alt context.

### 2026-05-06 — 1h follow-up after Candidate A/B/C rollout

- **Window reviewed:** post-note heartbeat check at `2026-05-06T04:54:21Z` through `2026-05-06T07:02:51Z`.
- **Closed trades (`xrp_macro`):** 1
- **Net PnL:** `-$4.95`
- **Exit mix:** `updown_time_stop` = 1/1 (100%)
- **BUY_YES vs BUY_NO (closed):** `1 / 0`
- **Interpretation:** still too little new sample for another immediate patch, but this is a negative datapoint consistent with the prior loss mode concentration.
- **Guardrail for next action:** if next ~6–10 XRP closes still show zero BUY_NO participation or `updown_time_stop` remains >50% of loss contribution, apply next XRP-only adjustment/revert path immediately.

### 2026-05-04 — Paper `test_20260504_034719`

- **Headline:** Positive net **+$2.38** with **69.6%** WR but **payoff skew** — average loss (~-$3.15) larger than average win (~+$1.53); one **BUY_NO** loss (-$3.05) — sample tiny on the short side.
- **Rolling heatmap:** `scripts/hourly_heatmap.py --days 30 --hour-axis exit_pt --suggest` (output [`docs/session_reports/hourly_heatmap_20260504_exit_pt.txt`](docs/session_reports/hourly_heatmap_20260504_exit_pt.txt)) flags **Pacific exit hour H11** for `xrp_macro`: 33.3% WR, avg **-$0.70**/trade, **n=6** — script suggests considering `blocked_utc_hours_updown: [11]`; **operator decision pending** (low n; policy says ≥7 days / not acting on noise alone).
- **Config follow-up (after session):** `enforce_alt_1h_alignment: true`, `min_edge_buy_no: 0.10` — **actual outcome `pending`** (≥15 closes post-change).
- **Heatmap script gap:** blocked-hour column for XRP is **not** wired in `STRATEGY_CONFIG_KEYS`; interpret XRP hour tables as performance-only unless the script is extended.

### 2026-04-30 — Structural low-correlation watch

- XRP is expected to spend more time below SOL/ETH-style BTC correlation levels, so its edge can be structurally damped by design rather than because of a single bad configuration.
- Future calibration should avoid simply raising `min_edge` after weak samples without checking whether `max_edge_updown`, correlation damping, and the XRP-specific thesis still leave a meaningful tradeable band.
- Treat XRP as a weaker BTC-lag lane until live data proves otherwise; do not chase small samples by squeezing the edge band asymmetrically.

### 2026-04-26 — Early paper read

- 3/3 wins is directional positive signal only; not statistically meaningful yet.
- Continue watching edge distribution and exit path behavior before tuning.

## Lessons learned

_(none yet — add only after data)_

## 2026-06-11 — 5m BUY_NO inversion flip → +EV long (forward-test)
- **Finding:** xrp_macro **5m BUY_NO** is structurally inverted — held-to-resolution WR **33%** over n=159 (settled since ~05-20), **$-258** live PnL. On the *same* markets the YES side resolves ITM ~67%, so the short is anti-selective and the cheap long is +EV.
- **Change:** flip BUY_NO→BUY_YES at the 5m edge stage via the shared sol loop (`buy_no_5m_flip_to_yes: true`). Uses the **complement** of the native est_prob (`max(1−est, 0.50)`) so the normal edge gate then admits only the *cheap* longs (low yes_price) — the +EV pocket. Candidate has already cleared all short-side gates; downstream directional guards inert (`_btc_trade_inputs_enabled()==False`). Default opt-out flag.
- **Also (exit-side, same batch):** 15m up stop 0.28→0.32 (held-WR 53%, +$45 n=15). Forward-test only — ghost log can't validate stop changes; basis is the fresh taken-exit settler (`held_win`/`hold_minus_exit_pnl`).
- **Status:** LIVE post-restart in session `test_20260611_181157`. Family flip (sol/xrp/doge/bnb) observed firing (`+buy_no_5m_to_yes_flip side=LONG`); eth/hype loaded but **dormant** until their 5m side next goes short (book was all-LONG at restart).
- **Watch:** confirm flipped longs *convert to fills* over next sessions, not 100% re-skipped by lane_entry_window/composite/iql. Validate flipped-long held-WR vs the ~67% thesis.

## 2026-07-12 01:18Z — per-lane composite floor / short-door reopen — STATUS: PENDING
Settled-cohort basis (36h, sanctioned gate-flip use of ghost settles): see vault handoff #3 addendum. Change: 1h:SHORT composite floor -> 0.0 (blocked cohort 79%/112; was 65 blocks/75min live)   . Guards remaining: min_edge, shrink 0.28, conviction, momentum-confirm, oracle, sizing, kill-switch. **Outcome stays PENDING until >=15 closed trades on the lane; review then.**
