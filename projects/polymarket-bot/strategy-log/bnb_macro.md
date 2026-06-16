# BNB macro (`bnb_macro`)

BNB **Up or Down** — inherits shared `SolMacroStrategy` signal path with BNB market detection and `BNBUSDT` spot leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 28 | Paper `test_20260524_060424` — [docs/session_reports/eth_hype_bnb_session_audit_20260524_060424.md](/Users/mainfolder/Documents/psb-main%201/docs/session_reports/eth_hype_bnb_session_audit_20260524_060424.md) |
| Win rate | 32.1% | same |
| Net PnL | -$0.79 | same |

## Change Log

### 2026-06-16 — BNB pocket refinements: short RSI floors + 15m-long reclaim

- **Context.** Pocket-hunt (queued task) re-verified on settled ghost. BNB est_prob is the best-calibrated alt, so its lanes have real edge concentrated in RSI slices. Live session `test_20260616_030355` also showed a −$12 stop on `1h BUY_NO` at RSI 37 — outside the +EV pocket.
- **Short RSI floors (new `buy_no_<window>_pocket_rsi_min` gate):**
  - `1h BUY_NO`: RSI<45 is −EV (−0.04 to −0.16; the live RSI-37 loser), RSI 45-55 = **+0.408** (n329). Set `buy_no_1h_pocket_rsi_min: 45`.
  - `15m BUY_NO`: RSI<35 = −0.098 (n3328), RSI≥35 +EV → lane flips positive. Set `buy_no_15m_pocket_rsi_min: 35`.
  - `5m BUY_NO`: left as-is — +EV in aggregate (+0.087), pocket is "extremes" (<35 +0.237 / >65 +0.216) which a min-floor can't express; candidate for a size bump instead.
- **15m BUY_YES reclaimed from blanket sit-out → pocket-restrict.** Aggregate −0.068 but RSI<35 = **+0.065** (n1838). Replaced `disable_buy_yes_15m` with `buy_yes_15m_pocket_rsi_max: 35`. **Bearish arm OFF** (`buy_yes_15m_pocket_include_bearish: false`): verified BNB bearish & RSI≥35 longs are −0.040 (unlike DOGE, where bearish longs are +0.073) — so the gate's bearish-OR clause is now per-lane configurable.
- **Validation.** 33 strategy-driver tests pass; py_compile clean; YAML parses.
- **Status:** `pending` — committed, NOT restarted (awaiting operator).

### 2026-06-15 — Sit out BNB 15m BUY_YES (long) — BNB's only -EV lane (NOT the shorts)

- **What changed:** Set `strategies.bnb_macro.disable_buy_yes_15m: true` (new per-window long sit-out hook; narrower than the all-window `disable_buy_yes`).
- **Why — and why it's the long side, counterintuitively:** When BNB looked like it was "losing on shorts" live (−$20 BUY_NO in `211313`), the net-of-fee ghost EV by lane said the **opposite** — BNB shorts are +EV (`1h BUY_NO` +0.094, `5m BUY_NO` +0.087, `15m BUY_NO` ≈0.00), and its **only** −EV lane is `15m BUY_YES` = **−0.048 all / −0.080 recent (n≈16k)**. The live short −$20 was 2 unlucky trades; BNB BUY_NO was +$53 / 67% WR the prior session (`150855`). So cutting BNB shorts would have thrown away an edge — cut the 15m long instead.
- **Lesson:** Diagnose the lane from ghost net-EV, not from the surface "which side lost money this session." Surface pattern said shorts; the data said longs.
- **Expected outcome:** Removes BNB's structurally −EV 15m long; keeps 1h/5m longs and all short windows (+EV).
- **Actual outcome:** `pending` — live since `test_20260615_232613`.
- **Status:** `pending`

### 2026-06-15 — REVIEW (no change): BNB UP-side RSI momentum HELD as regime-unstable

- **Context:** While shipping HYPE's UP-side RSI momentum nudge, BNB looked like a candidate — its all-time RSI×outcome curve also has positive long EV at high RSI (`>75` +0.073). Considered setting `est_prob.rsi_adj_up_*` to a momentum profile for BNB too.
- **HELD — not for lack of data (n=37,694), but regime instability.** Stability gate (`scripts/fit_hype_rsi_momentum.py bnb_macro`): 2 of 3 zones **sign-flip** all-time↔recent (`<30` −0.026→+0.000, `65–75` +0.004→−0.001). The one sign-stable zone (`>75`) has realized EV that **flipped negative recently**: `75–85` +0.075 (all-time, n=3,202) → **−0.115** (recent ≥06-08, n=1,304), with volume more than halved.
- **Contrast with HYPE (which shipped):** HYPE's zones were sign-stable *and strengthening*, recent realized EV still positive. BNB's pocket is decaying/negative in current tape — setting it would be overfitting to stale all-time data.
- **Applied anyway (uniform, safe):** per-asset ATR% bands `atr_low_pct: 0.0026 / atr_high_pct: 0.0038` (BNB's own p25/p75 — it's the calmest alt, median ATR% 0.32%, so SOL's `0.01/0.03` was most wrong here). UP-side RSI nudge left at mean-revert default.
- **Re-open trigger:** re-run the fit; if recent `>75` realized EV turns positive and zones stabilize, revisit.
- **Status:** `no change` (RSI), `applied` (ATR bands)

### 2026-06-15 — Add lane-local stop cooldown for BNB 1h BUY_YES bleed

- **What changed:** Added `lane_stop_halt` config support and enabled `bnb_macro|1h|BUY_YES` with a 15-minute cooldown after an `updown_stop_loss`.
- **Why:** Running session `test_20260615_031614` showed BNB 1h BUY_YES at `0%` WR and `-$21.72` across 5 exits. Replay of the current session snapshot showed the new cooldown would have blocked 2 exited BNB 1h BUY_YES entries with `-$5.52` realized PnL.
- **Hypothesis:** The BNB 1h bullish-floor lane should stop re-entering immediately after a failed long while preserving BNB 5m BUY_YES, which was positive in the same session snapshot.
- **Expected outcome:** Reduced BNB 1h BUY_YES stop-loss repetition without disabling BNB 5m or BUY_NO lanes.
- **Actual outcome:** `pending` — requires restart and at least 15 closed BNB 1h BUY_YES trades after rollout.
- **Status:** `pending`

### 2026-06-12 — REJECTED before rollout: 5m BUY_YES hold+trail

- **What changed:** No live config change remains. A temporary `bnb_macro` 5m `up` / `BUY_YES` hold+trail override was considered, then removed before rollout.
- **Why:** Operator is skeptical that hold behavior may have broken the bot, so this is deliberately narrow. Recent settled exit policy since `2026-06-11` showed `bnb_macro|5m|BUY_YES` as one of only two current drift lanes clearing the sample floor: `n=29`, held WR `59%`, realized WR `48%`, gap `+$27.8`.
- **Hypothesis:** Rejected. The better next step is to inspect stop-cut recoverable winners vs true bad entries without reintroducing hold-to-resolution.
- **Expected outcome:** No behavior change from this entry.
- **Actual outcome:** `pending`
- **Status:** `reverted ❌` before rollout.

### 2026-06-12 — Targeted 1h composite floor to relieve starvation

- **What changed:** Added `updown_composite.strategy_window_min_scores.bnb_macro.1h: 0.56` and taught the shared scorer to apply strategy/window composite floor overrides.
- **Why:** Overnight diagnostics showed BNB 1h candidates reaching the composite gate and skipping on `composite_score_below_floor`, while settled BNB 1h `lane_min_edge` ghosts were strongest on BUY_YES price bands. This does not reopen BNB 15m BUY_NO.
- **Hypothesis:** BNB 1h candidates with mid/high composite scores can enter at tuning size without lowering global composite floors.
- **Expected outcome:** BNB 1h starvation decreases; BNB 15m BUY_NO continues to skip on `lane_min_edge`.
- **Actual outcome:** `pending` — requires restart and at least 15 closed BNB 1h trades after rollout.
- **Status:** `pending`

### 2026-06-12 — Controlled BNB 15m BUY_YES reopen, BUY_NO stays disabled

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), reopened only BNB 15m `up`/`BUY_YES`: `by_tf.15m.min_edge 0.50 -> 0.09` and canonical `entry_policy.window_side_overrides.15m.up.min_edge 0.50 -> 0.09` with `size_multiplier: 0.3`. BNB 15m `down`/`BUY_NO` remains disabled at `min_edge: 0.50`.
- **Why:** The prior both-side 15m drop was based on older settings and a mixed-side result. Post-new-settings settled ghosts showed `BUY_YES lane_min_edge` remained strong (`post 2026-06-11: n=767, WR 65.2%, positive counterfactual`), while `BUY_NO lane_min_edge` was weak/negative (`n=694, WR 50.9%, negative counterfactual`) and broad `BUY_YES ai_none_marginal_threshold` remained negative.
- **Hypothesis:** A small-size BUY_YES-only forward test recovers the positive BNB 15m long cohort without reopening the weak short side.
- **Expected outcome:** BNB 15m BUY_YES entries appear at tuning size; BNB 15m BUY_NO continues to skip on `lane_min_edge`.
- **Actual outcome:** `pending` — requires restart and at least 15 closed BNB 15m BUY_YES trades after rollout.
- **Status:** `pending`; revert `15m.up.min_edge` to `0.50` if the post-change closed cohort is negative.

### 2026-06-12 — Enforce BNB per-strategy cap and active 15m drop

- **What changed:** Fixed `RiskManager.can_trade(strategy=...)` to enforce `strategies.bnb_macro.max_concurrent_positions` before order placement. Also aligned `bnb_macro.entry_policy.window_side_overrides.15m` with the documented `by_tf.15m` DROP setting by setting both 15m `up` and `down` active min-edge to `0.50`. Added regression coverage in [tests/test_risk_manager_hardening.py](/Users/mainfolder/Documents/psb-main%201/tests/test_risk_manager_hardening.py) and [tests/test_lane_entry_policy.py](/Users/mainfolder/Documents/psb-main%201/tests/test_lane_entry_policy.py).
- **Why:** Local paper session `test_20260611_220323` opened 7 BNB 15m positions in one cycle despite `bnb_macro.max_concurrent_positions: 2`. The scanner was not skipping 5m/1h (`bnb_5m_native=342`, `bnb_1h_native=285` across the last 300 ops pulses); the visible issue was BNB 15m future-window signals passing through a dead per-strategy risk cap. A second config bug kept BNB 15m live because canonical `entry_policy` min-edges (`0.085/0.08`) overrode the legacy `by_tf.15m` drop (`0.50/0.50`).
- **Hypothesis:** BNB can still scan far-ahead 15m windows for observability, but execution will not enter BNB 15m while the documented drop is active; any other BNB batch is capped by the per-strategy open-position limit.
- **Expected outcome:** No more BNB-only 7-position ladder from one scan; BNB 15m candidates skip on `lane_min_edge`, and additional non-15m BNB signals skip with `Max concurrent positions reached for bnb_macro` once two BNB positions are open.
- **Actual outcome:** `pending` — needs restart and at least 15 closed BNB trades after rollout.
- **Status:** `pending`

### 2026-06-07 — 1h BUY_YES price-banded floor bump (cleanest of the 4 alts)

- **What changed:** BNB 1h: `1h_buy_yes_bullish_floor_bump: 0.30`, band **0.50–0.88** (via the new shared `_alt_buy_yes_bullish_floor_bump` price-band guard, `sol_macro.py:2129`).
- **Why:** BNB 1h longs reject on `lane_min_edge` from negative model-edge (`est_prob_up ≈0.63 < yes_price`) — `min_edge` already 0 (a9610d9), so the bar isn't the problem, the under-shooting model is. BNB is the cleanest case: +EV across the **whole** 0.50–0.90 band (+22–56% held EV, 87% WR, ghost n=213). Only the 0.90+ cheap-money trap excluded.
- **Hypothesis:** Banded bump admits the 0.50–0.88 +EV 1h-long cohort.
- **Expected outcome:** BNB 1h BUY_YES entries appear.
- **Actual outcome:** `pending` (≥15 closed). Hold-to-resolution caveat; forward-test only.
- **Status:** `pending` — needs restart. codex re-derived the band.

### 2026-06-06 — Keep only 5m BUY_YES bullish-floor winner

- **What changed:** `bnb_macro.15m_buy_yes_bullish_floor_bump: 0.19 → 0.0`; `bnb_macro.5m_buy_yes_bullish_floor_bump` kept at `0.19`.
- **Why:** Operator rule: zero every non-winner long bump and keep only the two proven winners, HYPE 5m and BNB 5m.
- **Hypothesis:** BNB 15m no longer admits fabricated-edge longs from the bullish floor, while the live-winning BNB 5m bump remains active.
- **Expected outcome:** BNB 15m BUY_YES frequency drops where raw edge was created only by the bump; BNB 5m BUY_YES remains unchanged.
- **Actual outcome:** `pending` — requires bot restart and at least 15 closed affected BNB trades after rollout.
- **Status:** `pending`

### 2026-06-05 — Block weak 5m BUY_YES bounces against bearish/neutral 1h floor logic

- **What changed:** In the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) path inherited by BNB, 5m `BUY_YES` is blocked when BNB 1h is `BEARISH`, and the 5m bullish floor now requires the actual BNB 1h trend to be `BULLISH`.
- **Why:** Current session `test_20260605_130808` showed the 5m bullish floor inflating fast alt bounces during non-bullish 1h tape. The shared counterfactual would have filtered 12 of 15 alt 5m longs, covering `-26.4559` PnL.
- **Hypothesis:** BNB 5m longs stop firing on fast bounces unless the BNB 1h tape confirms.
- **Expected outcome:** Fewer BNB 5m `BUY_YES` stop-loss cascades during bearish/neutral BNB 1h tape.
- **Actual outcome:** `pending` — requires restart and at least 15 closed affected BNB trades after rollout.
- **Status:** `pending`

### 2026-06-05 — Remove BTC leakage from BNB trade reasons and AI contexts

- **What changed:** In the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) path inherited by BNB, removed BTC labels/values from trade reason strings, scan diagnostics, and AI decision context. Also removed the residual 5m confidence component based on BTC correlation.
- **Why:** BTC was already disabled as a trade gate, but inherited BNB explanations still exposed BTC-looking diagnostics.
- **Hypothesis:** BNB decisions and explanations read as BNB-native only after rollout.
- **Expected outcome:** Post-restart BNB entries/skips no longer include BTC labels in `signal_reason` or marginal AI contexts.
- **Actual outcome:** `pending` — requires restart and at least 15 closed BNB trades after rollout.
- **Status:** `pending`

### 2026-06-04 — Hold winners + trailing floor (15m BUY_NO exit leak, LOW confidence)

- **What changed:** Added a `bnb_macro` `updown_overrides` `15m down` (leg=down / BUY_NO) block in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml): `updown_hold_winners_to_resolution: true` + positive trailing floor (`updown_trail_arm_pct: 0.10`, `updown_trail_gap_pct: 0.15`). Uses the same floor mechanic added 2026-05-31 for BNB BUY_YES. BNB previously had only a `5m down` override; no `15m` exit policy.
- **Why:** Settled exit recompute (`data/calibration/lane_exit_policy.json`, n=22) classified `bnb_macro|15m|BUY_NO` as policy **A — hold+trail (exit kills edge)**: held WR 50% / held PnL **+$4.10** vs realized WR 27% / **−$20.71**; `drift: true`. The tight exit is bleeding a roughly-breakeven held lane.
- **Hypothesis:** Holding 15m BUY_NO winners with a trailing floor stops the realized-vs-held leak; realized PnL moves off −$21 toward the held +$4.
- **Expected outcome:** BNB 15m BUY_NO `exit_reason` shifts toward `updown_resolution`/trail; realized WR rises off 27%.
- **Actual outcome:** `pending` — forward-test only; needs bot restart. Watch `trades_settled.jsonl`.
- **Status:** `pending` — **LOW CONFIDENCE**: n=22 and held edge is only +$4.10 (marginal, noisy). Revisit once the BNB 15m BUY_NO cohort grows; revert if realized PnL doesn't improve.

### 2026-05-31 — Early-TP regret: hold winners + positive trailing floor (BUY_YES 5m/15m)

- **What changed:** Added a `bnb_macro` `updown_overrides` block in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) for BUY_YES (leg=up) 5m and 15m: `updown_hold_winners_to_resolution: true` + a new **positive trailing floor** (`updown_trail_arm_pct: 0.10`, `updown_trail_gap_pct: 0.15`). New floor mechanic added to [`effective_updown_stop_loss_pct`](/Users/mainfolder/Documents/psb-main%201/src/execution/updown_exit_shared.py): once the high-water mark clears +10%, the exit floor trails at `peak − 15%` and **can be positive** (banks gains), vs. the prior in-profit tighten that only capped the from-entry loss. Floor is always `max(base_stop, peak−gap)` — never wider than the base stop.
- **Why:** On 230 settled `take_profit` rows, BNB BUY_YES showed the strongest positive early-TP regret: 5m n=15 (+$10.0, 87% held-WR) and 15m n=11 (+$23.6, 82% held-WR). Exiting at +30% left money on the table on a directionally-correct lane.
- **Hypothesis:** Holding winners with a trailing floor captures the run BNB BUY_YES was giving up at +30%, while the floor protects most of the give-back when a winner reverses.
- **Expected outcome:** BNB BUY_YES 5m/15m mean realized PnL per winner rises above the +30% TP cap; `exit_reason` shifts from `take_profit` toward `updown_resolution`/trail exits.
- **Actual outcome:** `pending` — forward-test only (exits can't be ghost-validated); needs bot restart. Watch BNB BUY_YES exit reasons + realized PnL in `trades_settled.jsonl`.
- **Status:** `pending`

### 2026-06-01 — Decisive AI prompt (kill the HOLD default)

- **What changed:** Rewrote the shared decision/analysis system prompt ([`ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py) `SYSTEM_PROMPT`): removed the "be conservative — markets overestimate" inaction bias, reframed HOLD as requiring a *specific, evidence-based* reason (never a default for uncertainty), told it to commit to a direction on any real lean, and clarified `confidence_score` = strength of the directional evidence. Bumped `prompt_version` → `lane-feedback-v2-decisive` so the settler can split pre/post verdicts. Also raised `max_ai_calls_per_scan` 5→6 for added budget headroom.
- **Why:** Diagnosis showed the AI returned **HOLD on 77%** of responses and approved only **3%** — the prompt itself was steering the model toward inaction on near-coin-flip 15m/1h markets, which the veto-only marginal change alone does not fix.
- **Hypothesis:** A decisive prompt cuts spurious HOLDs and surfaces more directional calls (and more *confident-opposition* vetoes), making the gate a real tiebreaker rather than a near-total veto.
- **Expected outcome:** HOLD share drops well below 77%; more BUY_YES/BUY_NO verdicts; gate approval rate rises off 3%.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only; needs bot restart. Watch HOLD% / approval% in `decision_layer.jsonl` under prompt_version `lane-feedback-v2-decisive`.

### 2026-06-01 — Marginal lane → AI veto-only (unblock 15m/1h)

- **What changed:** Also raised `max_ai_calls_per_scan` 3→5 (BNB pegged the 3-call/scan budget in ~41% of scans). The marginal-lane AI gate flipped from fail-closed to **veto-only**: the AI can now only REJECT a below-threshold candidate with a *confident, directly-opposing* directional call (conf ≥ `decision_layer.min_confidence`). HOLD / SKIP / low-confidence / agreement fall back to the quant trade. Central change in [`evaluate_trade_decision`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py) (new `veto_only` param) threaded through the marginal call site(s); guarded the redundant local re-checks; opt-out `decision_layer.marginal_veto_only`.
- **Why:** Over a 5.5h window (`decision_layer.jsonl`) the gate approved only **3%** of AI-evaluated candidates — the model returned **HOLD 77%** of the time (conservative system prompt + a 0.60 confidence bar that near-coin-flip 15m/1h markets can't honestly clear), and HOLD was a hard veto. BNB approved only 4/89 AI-evaluated candidates in the window.
- **Hypothesis:** Restoring marginal admission (blocked only on confident AI opposition) reopens 15m/1h frequency without losing the AI's ability to stop a conviction-wrong trade.
- **Expected outcome:** BNB 15m/1h marginal entries resume; AI still vetoes confident-opposite cases.
- **Actual outcome:** `pending`
- **Status:** `pending` — forward-test only (AI-gate behavior is not ghost-validatable); needs bot restart to load.

### 2026-06-01 — Revert BNB BUY_YES price-band clamp

- **What changed:** Reverted the same-day BNB BUY_YES price-band narrowing and `1h up` disable. BNB BUY_YES returns to prior price bands and entry-policy behavior.
- **Why:** Operator rejected narrowing winners/losers as a lazy WR fix. BNB needs causal repair of false positives, not post-hoc price slicing from a tiny short-window sample.
- **Hypothesis:** Restoring BNB BUY_YES keeps the sample honest while the next pass inspects whether the model overstates edge by price, window, family, or oracle-basis condition.
- **Expected outcome:** BNB BUY_YES resumes prior admission; no price-band clamp effect from the rejected WR-mode change.
- **Actual outcome:** `pending`
- **Status:** `reverted ❌`

### 2026-06-01 — Narrow BNB BUY_YES price bands for WR target

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), narrowed BNB BUY_YES admission to recent high-WR price bands: `5m up` YES price `0.46–0.48`, `15m up` YES price `0.44–0.46`, and disabled `1h up`.
- **Why:** Past-3-day BUY_YES review showed broad BNB BUY_YES at `75` trades / `45.3%` WR. The retained historical slices were `5m` price `0.46–0.48` at `16` trades / `62.5%` WR and `15m` price `0.44–0.46` at `5` trades / `80.0%` WR.
- **Hypothesis:** BNB keeps some upside throughput while lifting hit rate toward the operator's `55%` minimum / `62%` goal; 1h is paused because its sample was only `n=3`.
- **Expected outcome:** BNB BUY_YES entries outside the narrowed price bands should skip via `lane_price_band`; review after at least 15 closed post-change BNB BUY_YES trades.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-31 — Suppress anti-predictive 5m-native BUY_NO shorts

- **What changed:** Set bnb_macro `disable_buy_no_5m_native: true`; inherits the 5m BUY_NO sit-out in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), ghost-logged as `buy_no_5m_native_suppressed`. Commit `5d8cbc0`. Full rationale in `sol_macro.md` same date.
- **Why:** The cross-alt 5m-native short inversion (eth 11.8% / xrp 16.7% / doge 27.8% / sol 33% vs 50-65% on 15m-native) is consistent and structural; BNB is suppressed for parity ahead of its own 5m n building up (BNB BUY_NO overall 54.5%, but driven by 15m).
- **Hypothesis:** BNB 5m short bleed removed without touching its healthy 15m short lane.
- **Expected outcome:** `buy_no_5m_native_suppressed` appears for BNB.
- **Actual outcome:** `pending` (needs restart + ~15 closed trades)
- **Status:** `pending`

### 2026-05-31 — Ghost-validated BUY_YES entry-window expansion

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), widened BNB `BUY_YES` entry windows where settled ghosts showed the old window was blocking profitable candidates: `5m up 4.5 → 10.0`, `15m up 32.0 → 150.0`, and `1h up 60.0 → 360.0`. Also widened `15m down 50.0 → 180.0` from the smaller positive ghost bucket. Kept unrelated DOGE/XRP/SOL windows unchanged.
- **Why:** Settled ghosts since `2026-05-30T00:00Z` showed BNB entry-window rejects were the largest missed-EV family: `bnb_macro|15m|BUY_YES|lane_entry_window` `n=8,972`, `WR=59.8%`, `netGate=-1,689`; `bnb_macro|5m|BUY_YES|lane_entry_window` `n=1,107`, `WR=58.6%`, `netGate=-201`; `bnb_macro|1h|BUY_YES|lane_entry_window` `n=371`, `WR=74.4%`, `netGate=-184`.
- **Hypothesis:** BNB should admit more high-conviction upside candidates instead of sitting out profitable future-ladder windows, while existing edge, price-band, oracle, and Kelly/risk limits still control quality and sizing.
- **Expected outcome:** BNB `lane_entry_window` skip volume should fall sharply on `BUY_YES`; post-change BNB `BUY_YES` closed trades should maintain positive expectancy after at least 15 closed trades.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-28 — BNB-local 5m downside guard plus explicit BUY_YES/BUY_NO wiring coverage

- **What changed:** In [src/strategies/bnb_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bnb_macro.py), added BNB-local post-scan guards that block `5m` `BUY_NO` neutral-fallback branches in `btc_1h_regime=BULL`, and block `bnb_5m_native` `BUY_NO` when BNB 1H is no longer bearish or the YES side is already too rich (`bnb_5m_native_buy_no_max_yes_price_bull_1h`, default `0.60`). Added focused BNB guard tests in [tests/test_bnb_macro.py](/Users/mainfolder/Documents/psb-main%201/tests/test_bnb_macro.py) and explicit `bnb_macro` `BUY_YES` / `BUY_NO` execution-path coverage in [tests/test_strategy_execution_drivers.py](/Users/mainfolder/Documents/psb-main%201/tests/test_strategy_execution_drivers.py).
- **Why:** The 126-trade session showed BNB losses concentrated in `5m BUY_NO`, especially `bnb_5m_neutral_fallback_*` and late/expensive native shorts, while the operator explicitly asked to verify BNB side wiring before any broader 1h review.
- **Hypothesis:** BNB should preserve clean native downside entries while dropping the worst `5m` short branches that were producing stop-loss-heavy outcomes under bullish BTC 1h context. Explicit BUY-side execution coverage should prevent hidden side-routing drift.
- **Expected outcome:** Future BNB skip stats should show `local_bnb_guard`; `5m` BNB short losses should compress without removing valid `15m` or `BUY_YES` paths.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-28 — Inherited `sell_5m_low_corr` hard skip downgraded

- **What changed:** BNB inherits the shared SOL-family scan path in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py); `sell_5m_low_corr` no longer hard-skips 5m `BUY_NO` candidates and is now diagnostic context only.
- **Why:** Calibration review showed the hard BTC-correlation skip could throw away valid alt-native downside entries.
- **Hypothesis:** BNB 5m downside candidates should reach later edge/risk gates instead of being blocked by BTC correlation alone.
- **Expected outcome:** Future BNB diagnostics should stop reporting `sell_5m_low_corr` as a hard skip; review after >=15 closed post-change BNB trades.
- **Actual outcome:** `pending`
- **Status:** `pending`

### 2026-05-26 — Timeframe-scoped entry control config

- **What changed:** Moved BNB entry-control thresholds and windows from legacy flat timeframe keys (`min_edge_5m`, `entry_window_*`) into canonical `defaults` / `by_tf` config, and routed shared macro entry policy reads through the timeframe resolver.
- **Why:** Static config audit showed the same 5m/15m values duplicated in flat keys and lane policy overrides, making it unclear which tuning surface was authoritative.
- **Hypothesis:** BNB 5m/15m/1h tuning changes should stay scoped to their `by_tf` cell with no cross-timeframe bleed.
- **Expected outcome:** Startup logs should show BNB `by_tf` overrides; focused tests should preserve the same effective min-edge/window values.
- **Actual outcome:** `pending` (config migration only; need >=15 closed BNB trades before performance judgment).
- **Status:** `pending`

### 2026-05-26 — Resolver metadata parity for shared macro signals

- **What changed:** Added BTC-compatible resolver metadata to the shared macro signal path used by BNB: `conflict_type`, `resolver_path`, `htf_side`, `quant_side`, and `momentum_side`, with journal and position persistence.
- **Why:** BNB had HTF and oracle metadata, but direction-resolution details were not first-class like BTC.
- **Hypothesis:** Future ghost/trade reviews can separate HTF-aligned, quant-disagree, and momentum-disagree BNB entries without changing entry behavior.
- **Expected outcome:** New BNB entries should include resolver metadata in journal extras and `entry_signal`.
- **Actual outcome:** `pending` (need post-change entries to verify field coverage).
- **Status:** `pending`

### 2026-05-26 — Route BNB through up/down exits and hold winners

- **What changed:** Added `bnb_macro` to the shared crypto up/down exit strategy set and enabled `trading.exit_rules.updown_hold_winners_to_resolution`.
- **Why:** BNB was missing from the specialized up/down exit path, so it could bypass the stop/window/resolution behavior used by BTC/SOL/ETH/HYPE/XRP.
- **Hypothesis:** BNB exits should now use up/down-specific stop/window semantics, and correct winners should settle instead of being clipped by early TP.
- **Expected outcome:** BNB exits should include up/down-specific reasons and more `RESOLVED:* (real)` winners when correct.
- **Actual outcome:** `pending` (need >=15 closed BNB trades after restart).
- **Status:** `pending`

### 2026-05-26 — Remove BNB 15m/1h exploration size haircut

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), removed the added `0.3x` lane `size_multiplier` from BNB 15m and 1h up/down policies while keeping the existing 5m calibration sizing unchanged.
- **Why:** BNB was the only strategy whose win/loss ratio improved, so this is not a defensive tighten. It removes the broad post-baseline sizing experiment so BNB economics remain comparable with the May 22-style Kelly posture.
- **Hypothesis:** BNB 15m/1h avg winner and avg loser magnitudes should become interpretable without an extra lane-policy haircut.
- **Expected outcome:** Next BNB sample should show no 15m/1h `lane_size=0.30x` tag from lane policy.
- **Actual outcome:** `pending` (need >=15 closed BNB trades after restart).
- **Status:** `pending`

### 2026-05-26 — BUY_YES recovery tweak and missing BNB settings

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), changed BNB `alt_momentum_confirm.buy_yes` to `15m` only while keeping `buy_no` confirmation on `5m`, `15m`, and `1h`. Added/activated BNB BUY_YES settings by keeping `oracle_max_basis_bps_15m_buy_yes` / `oracle_basis_relax_max_bps_15m_buy_yes`, adding `entry_price_max_15m_yes_side`, and widening the BNB 15m up entry price cap to `0.57`. The shared oracle validator now reads those side/window settings.
- **Why:** BNB had partial BUY_YES settings in YAML, but the live validator was not using side/window oracle overrides. The all-window BUY_YES confirm rollback also risked suppressing the side we need to recover.
- **Hypothesis:** BNB should admit measured BUY_YES samples on cleaner setups without reopening unconfirmed BUY_NO downside flow.
- **Expected outcome:** Next paper run should show BNB BUY_YES eligibility/fills when 15m price/oracle conditions are acceptable, with BUY_NO still momentum-confirmed.
- **Actual outcome:** `pending` (need ≥15 closed BNB trades after restart on this config).
- **Status:** `pending`

### 2026-05-26 — Pre-restart rollback of post-May-22 momentum guard regression

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), added active `bnb_macro.alt_momentum_confirm` blocking for `BUY_YES` and `BUY_NO` on `5m`, `15m`, and `1h`; restored BNB 1h edge overrides to `up: 0.09` and `down: 0.08`.
- **Why:** BNB was less bad than before but still not proven, and the post-May-22 guard regression exposed it to unconfirmed default-side fills. This removes the premature 1h exploration loosen before restart.
- **Hypothesis:** BNB throughput should fall until BNB-native MACD confirms side selection, improving quality of any remaining fills.
- **Expected outcome:** Next paper run should show fewer BNB unconfirmed default-side entries and clearer momentum-confirm skip attribution.
- **Actual outcome:** `pending` (need ≥15 closed BNB trades after restart on this config).
- **Status:** `pending`

### 2026-05-25 — 1h exploration admission before restart

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), BNB 1h lane-policy floors were lowered to `up.min_edge: 0.08` and `down.min_edge: 0.075`, with 1h `entry_price_max` widened to `0.58`. Existing `0.3x` 1h calibration sizing remains.
- **Why:** BNB has zero closed 1h calibration trades in `data/calibration/trades.jsonl`; the lane is starved, not proven bad.
- **Hypothesis:** BNB 1h should begin producing small-notional samples, letting us evaluate whether the 1h horizon improves prediction quality versus noisy 5m/15m entries.
- **Expected outcome:** Next session should include BNB 1h entries when markets exist, with lane IDs and realized PnL available for review after ≥15 closed samples.
- **Actual outcome:** `pending` (need ≥15 closed BNB 1h trades after restart).
- **Status:** `pending`

### 2026-05-25 — Cap live lane-calibration alpha at identity

- **What changed:** In [src/analysis/lane_calibration.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_calibration.py), `ALPHA_CLAMP_HI` changed from `2.50` to `1.00`. Raw `alpha_ewma` telemetry can still exceed `1.0`, but live calibration can no longer amplify BNB probabilities away from 50/50; sub-1 shrinkage remains active.
- **Why:** Session attribution called out BNB high-alpha trades as loss contributors. This removes calibration-driven amplification while preserving shrinkage for historically overpredicted lanes.
- **Hypothesis:** BNB should see lower loss concentration in high-alpha buckets during the next live/non-shadow session.
- **Expected outcome:** Next live/non-shadow session should show no effective `alpha_used > 1.0` BNB entries.
- **Actual outcome:** `pending` (need ≥15 closed `bnb_macro` trades after this change).
- **Status:** `pending`

### 2026-05-24 — Restore BNB admission coverage after basis starvation

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), kept BNB edge floors unchanged, widened oracle basis validation from `18/22` bps to `30/40` bps, and set 15m/1h lane `size_multiplier: 0.3` to match the existing 5m calibration sizing.
- **Why:** Session `test_20260524_060424` showed BNB scanned heavily but converted poorly: `28` fills across `337` candidate events (`8.3%` entry rate). Rejections were dominated by `lane_min_edge` (`225`) and `oracle_basis_block` (`54`); blocked BNB oracle basis was tightly clustered around `24.4–29.1` bps, just above the prior cap.
- **Hypothesis:** BNB should stop acting mostly shadow-only and produce enough 5m/15m/1h calibration trades to judge the lane, while existing size multipliers and risk caps limit downside.
- **Expected outcome:** BNB entry rate rises materially and 1h candidates can enter when edge is positive instead of being dead on arrival.
- **Actual outcome:** `pending` (need ≥15 closed `bnb_macro` trades after this change).
- **Status:** `pending`

## Review sessions

### 2026-05-24 — Session `test_20260524_060424`

- BNB was near breakeven overall (`28` trades, `-$0.79`) but coverage was broken relative to scan volume. The problem to solve first is admission/fill coverage, not PnL tuning.

## Lessons learned

- `pending`

## 2026-06-11 — 5m BUY_NO inversion flip → +EV long (forward-test)
- **Finding:** bnb_macro **5m BUY_NO** is structurally inverted — held-to-resolution WR **30%** over n=235 (settled since ~05-20), **$-267** live PnL. On the *same* markets the YES side resolves ITM ~70%, so the short is anti-selective and the cheap long is +EV.
- **Change:** flip BUY_NO→BUY_YES at the 5m edge stage via the shared sol loop (`buy_no_5m_flip_to_yes: true`). Uses the **complement** of the native est_prob (`max(1−est, 0.50)`) so the normal edge gate then admits only the *cheap* longs (low yes_price) — the +EV pocket. Candidate has already cleared all short-side gates; downstream directional guards inert (`_btc_trade_inputs_enabled()==False`). Default opt-out flag.
- **Status:** LIVE post-restart in session `test_20260611_181157`. Family flip (sol/xrp/doge/bnb) observed firing (`+buy_no_5m_to_yes_flip side=LONG`); eth/hype loaded but **dormant** until their 5m side next goes short (book was all-LONG at restart).
- **Watch:** confirm flipped longs *convert to fills* over next sessions, not 100% re-skipped by lane_entry_window/composite/iql. Validate flipped-long held-WR vs the ~70% thesis.
