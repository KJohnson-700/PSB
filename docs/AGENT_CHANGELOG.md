# Agent changelog (backfill)

**Purpose:** Record **what shipped** when work was done in **Claude Code, Codex, Cursor**, or similar **without** a matching entry in the Obsidian strategy log or a written operator handoff. **Git remains the source of truth**; this file is a readable index.

**Strategy tuning and hypothesis tracking** still belong in `projects/polymarket-bot/strategy-log/` per `AGENTS.md`. This doc covers **codebase / infra / dashboard** provenance only.

**Canonical repo for this bot:** `https://github.com/KJohnson-700/PSB` (see `AGENTS.md` — do not confuse with other GitHub projects).

---

## 2026-06-03 — Horizon-coherence: own-TF decider + larger-TF fallback (all strategies)

**[`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), [`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py):**

- **Rule enforced:** every lane decides on its OWN timeframe, fallback to the next LARGER timeframe only (5m→15m→1h→4h). No smaller timeframe may gate or decide a larger lane.
- **Deciders audited clean:** `_resolve_bias_for_tf` (BTC) and `_resolve_alt_bias_for_tf` (ETH + sol-family) already follow the ladder — larger TFs only add a disagreement penalty or act as neutral fallback.
- **Violations fixed:**
  - `bitcoin._check_lower_tf_confirmation` hardcoded `macd_15m` and hard-skipped 1h markets ("LTF confirmed late-entry") on a 15m signal → now horizon-coherent (`1h→macd_1h`, else `macd_15m`); call site passes `_updown_tf`.
  - sol/eth momentum-confirm else-branch could consult `macd_5m` for a 15m lane → restructured to own-TF + next-larger fallback.
  - `sol._passes_iql` skips entirely on 1h (15m-calibrated floor was wrong TF) — was flooding SOL 1h with `iql_15m_reject` (commit 0473711).
- **Commits:** `0473711`, `8384048`. Needs restart.

**[`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`src/dashboard/index.html`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html):**

- **Self-heal Discord rate-limit fix:** the self-healing supervisor sent one Discord/Telegram message per cold-lane escalation. On the first cycle of a new day, 25+ lanes escalate at once → 25 webhook calls → Discord 429s. Now all escalations from a run are batched into a **single** message, capped at `self_healing.escalation.max_notify_per_run` (default 3) with a `…+N more suppressed` suffix.
- **Regime-feed health badge (Ghost Lab tab):** `_glRenderCounts` now renders a chip from `current_deadzone` (`/api/ghosts/lab`) — 🟢 `REGIME FEED LIVE · <age>` when fresh, 🔴 `REGIME FEED STALE · <age>` (pulsing) when the feed drifts past `max_age_sec`, with a tooltip showing the restart command. Surfaces the failure mode where `tools/enhanced_price_tracker.py` (the standalone daemon that writes `market_regime.jsonl`, now under launchd `com.psb.regime-tracker`) dies and the deadzone heatmap silently freezes — which went unnoticed for 8 days (May 25 → Jun 3).
- **Validation:** `python -m py_compile src/main.py` + `node --check` on extracted page JS both pass; dashboard endpoints smoke-tested 200 via TestClient.

## 2026-06-02 — HYPE Binance 451 cooldown

**[`src/analysis/hyperliquid_hype_service.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/hyperliquid_hype_service.py), tests:**

- **Diagnosis:** local session logs showed repeated `Binance USDM HYPE ... 451 Client Error ... falling back to Hyperliquid` lines. Binance Futures was geo-blocked from the active network route, but the service retried Binance for every HYPE interval on every cycle before falling back, adding log noise and latency.
- **Fix:** when Binance returns HTTP `451` for HYPE klines, the service now starts a 1-hour Binance cooldown and routes HYPE directly to Hyperliquid during that window. Other transient Binance failures keep the existing one-shot fallback behavior.
- **Validation:** `.venv/bin/python -m pytest tests/test_market_data_fallbacks.py` → 10 passed.

## 2026-06-02 — Discord 429 fix for manual global stop alerts

**[`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), tests:**

- **Diagnosis:** Discord webhook was loaded, but local logs showed repeated `Discord webhook failed: 429` immediately after `Manual global stop active (data/KILL_SWITCH present)`. The kill-switch branch spawned six manual-stop Discord embeds every skipped cycle, which kept the webhook rate-limited and also caused exit-policy drift alerts to fail.
- **Fix:** de-duped manual global stop Discord notifications so the bot sends one six-strategy alert burst per kill-switch activation, then suppresses repeats until the kill switch clears.
- **Validation:** `.venv/bin/python -m pytest tests/test_strategy_execution_drivers.py::test_manual_global_stop_discord_alert_is_deduped_per_activation tests/test_notification_manager.py` → 11 passed.

## 2026-06-02 — Paper-session loss regression: stale-window execution guard + paper daily cap

**[`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`src/execution/clob_client.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/clob_client.py), tests:**

- **Diagnosis:** current local paper session `test_20260601_163448` closed 102 trades at `-99.52` PnL / `32.4%` WR. Losses clustered around stale short-window execution: the strategy scan phase reached ~202s, so 5m signals could be generated in-window but executed near/at market end. Worst cluster: `eth_macro` 5m `BUY_NO` (`n=10`, `WR=10%`, `PnL=-39.38`), with several entries showing `minutes_to_market_end=0`.
- **Risk bug:** config had `trading.daily_loss_limit: 0.15`, but `RiskManager` read only `risk.daily_loss_limit`; paper mode also ignored the daily loss cap and waited for the 25% emergency stop. `RiskManager` now falls back to `trading.daily_loss_limit` and enforces daily loss in paper by default (`risk.enforce_daily_loss_limit_in_paper`, default `true`).
- **Execution fix:** added a final `_check_fresh_entry_window` guard in both BTC and SOL-family execution paths, immediately before risk sizing/order placement. It recalculates seconds left from `signal.end_date` and logs `stale_signal_window` instead of placing orders that aged out behind slow scans/AI. Defaults: 5m needs at least 1.0 min left, 15m 2.0 min, 1h 3.0 min; strategy `entry_window_min` can only make that stricter.
- **Operator safety:** created `data/KILL_SWITCH` so the currently running old-code process cannot resume entries before a controlled restart. Restart required to load the code patch.
- **Validation:** `pytest tests/test_risk_manager_hardening.py tests/test_strategy_execution_drivers.py` → 34 passed.

## 2026-06-01 — Exit-policy drift Discord alerts + revert 5m hold+trail

**[`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`src/analysis/lane_exit_policy.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_exit_policy.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):**

- **Discord drift alerts (recommend-only):** the exit-policy recommender now runs on settle and pings Discord when live exit config disagrees with settled data. Wired into `_refresh_ghost_calibration_state` (runs in a worker thread, so a blocking `urllib` webhook POST is safe). Gated by new `lane_exit_policy` config (`enabled`, `alert_discord`, `recompute_min_new_settles: 25`, `min_lane_n`). De-dups on a drift signature so the same drift isn't re-pinged every cycle; only re-alerts when a new lane drifts or a recommendation flips. Helpers added to `lane_exit_policy.py`: `recompute`, `drift_signature`, `format_drift_message`, `post_discord_blocking`. **Never auto-applies** — operator reviews + edits config + restarts.
- **Reverted 5m hold+trail → plain +30% TP** on `eth/xrp/bnb 5m BUY_YES`. Diagnosis: the held→realized leak on these lanes is the STOP cutting winners (42–67% of held-winners stopped), which `hold_winners` doesn't touch (it only skips the take-profit). And the trailing floor, even at the live 10s fast-exit cadence, can't catch the worst 5m round-trips (observed bnb 5m: MFE +110% → realized −14% via stop). 5m lanes are coin-flip held-WR (46–48%) where this added variance with no upside. 15m/1h lanes kept on hold+trail.
- **Correction to prior session note:** the fast-exit loop is NOT uncommitted — it's live at `exit_check_interval_sec: 10` and routes through `PositionExitManager` (the trailing-floor class). Earlier "exits at 60s / fast loop uncommitted" claim was wrong.
- Tests: +4 (`test_lane_exit_policy.py`). Full suite 742 green.
- Forward-test only; config changes need a bot restart to load.

---

## 2026-06-01 — Exact AI gate economics logging (no more inferred PnL)

**[`src/strategies/{bitcoin,sol_macro,eth_macro}.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`scripts/ai_gate_value_report.py`](/Users/mainfolder/Documents/psb-main%201/scripts/ai_gate_value_report.py):**

- **Fix:** enriched every real AI decision-layer log call with decision-time economics: `yes_price`, derived `no_price`, derived side `entry_price`, `quant_edge`, `quant_confidence`, `quant_threshold`, raw/calibrated probabilities, and `lane_id` where available.
- **Why:** previous AI gate value analysis could only score direction from `decision_layer.jsonl`; it did **not** have price/size, so rejected-trade PnL estimates were assumption-heavy and not valid for live quant calibration.
- **Report:** added `scripts/ai_gate_value_report.py`, which computes exact normalized-stake gate value only for enriched rows and explicitly skips old rows missing entry economics.
- **Boundary:** no trading logic changed. This is instrumentation for future forward-test evidence; current historical AI gate rows remain unsuitable for exact PnL analysis.

---

## 2026-06-01 — Per-lane exit policy: scorecard + shadow recommender + A-lane expansion

**[`src/analysis/lane_exit_audit.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_exit_audit.py), [`src/analysis/lane_exit_policy.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_exit_policy.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):**

- **Premise:** the bot's realized WR (~45%) sits far below its held-to-resolution accuracy. The gap is the EXIT, and it is per-lane *opposite* — the +30% TP / stop destroys edge on some lanes and is the profit engine on others. One global exit can't serve both.
- **Scorecard (`lane_exit_audit.py`):** reads `trades_settled.jsonl`, compares held-WR vs realized-WR and dollar gap per `(strategy,window,side)`, classifies A (exit kills edge → hold+trail), B (exit is engine → keep tight TP/SL), C (entry-broken → not an exit fix). Forward-test scorecard; exits are NOT ghost-validatable.
- **Recommender (`lane_exit_policy.py`):** shadow-only. Writes `data/calibration/lane_exit_policy.json` with per-lane recommendation + live config, flagging `drift` (config vs data, n≥20). Never edits config or live exits — exit changes stay human-applied + forward-tested. Mirrors the entry-side `per_lane_thresholds` pattern.
- **A-lane config expansion** (held≫realized, exit kills edge): added hold-winners + positive trailing floor (`arm 0.10 / gap 0.15`) to `eth_macro` BUY_YES 5m/1h, `eth_macro` BUY_NO 15m, `xrp_macro` BUY_YES 5m/15m (joining the eth/xrp/bnb 15m + xrp 15m BUY_NO lanes from commit 1ea32a5). Bitcoin 15m BUY_YES (+$43 realized vs −$49 held) and hype 15m BUY_YES (+$47) left on tight TP/SL — exit is their engine.
- **Drift surfaced, not auto-changed:** bnb 5m/15m BUY_YES held-WR ~47.5% (just under the A-bar); kept hold+trail (gap positive, holding harmless) per operator decision.
- Tests: classifier bucketing incl. C-over-B precedence (4 cases). Full suite 734+ green.
- Tooling committed in `2c91e6d`; config A-lane edits land here.
- NOTE: this `settings.yaml` commit also carries concurrent parallel-session gate edits (`disable_buy_no_5m_native` flips, `min_min_edge_mult`, `enabled` toggles) that arrived in the same file and could not be split non-interactively.

---

## 2026-06-01 — Reopen ghost-positive XRP/HYPE 5m native BUY_NO gates

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), strategy logs:**

- **Config:** set `xrp_macro.disable_buy_no_5m_native: false` and `hype_macro.disable_buy_no_5m_native: false`; SOL/DOGE/BNB remain suppressed.
- **Why:** current session review showed the bot was collecting evidence but not closing most gate loops. Settled ghosts now show the previous 5m native BUY_NO suppression is negative gate value on XRP and HYPE specifically: XRP `n=651`, `WR=59.8%`, net gate value `-95.060`; HYPE `n=303`, `WR=61.7%`, net gate value `-35.241`.
- **Boundary:** did **not** globally expand `performance_feedback.overtight_reasons`; non-`lane_min_edge` gates generally lack `effective_min_edge` fields, so the current relax math would be inert. Did **not** loosen `eth_1h_weak_confirm` BUY_NO because the full settled sample is protective (`n=859`, `WR=48.2%`, net gate value `+77.111`). Did **not** raise AI call caps from the current forward-test config; full-file `ai_call_limit_marginal_threshold` evidence is mixed.
- **Validation:** forward-test only for reopened live entries; strategy-log outcomes remain `pending` until at least 15 closed post-change trades per reopened lane.

## 2026-05-31 — Positive trailing-floor exit (true trailing-after-MFE) for up/down lanes

**[`src/execution/updown_exit_shared.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/updown_exit_shared.py), [`src/execution/live_testing.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/live_testing.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):**

- **New mechanic:** `effective_updown_stop_loss_pct` gained two optional params (`trail_arm_pct`, `trail_gap_pct`, default `0.0` = off). Once the persisted high-water mark (`peak_pnl_pct`) clears `trail_arm_pct`, the exit floor trails at `peak − trail_gap_pct` and **may be positive** — banking gains, not just capping the from-entry loss like the existing `in_profit_stop_tighten`. Floor is `max(base_stop, peak − gap)`, so it is only ever *more* protective. Encoded as a possibly-negative magnitude; caller guard relaxed `> 0` → `!= 0` to admit positive floors.
- **Plumbing:** params threaded through `UpdownExitGlobals` / `UpdownResolvedExitParams` / `_UPDOWN_EXIT_PARAM_KEYS` / `parse_updown_exit_globals` / `resolve_updown_exit_params_for_position`, so they resolve at strategy→window→leg granularity like the other exit knobs. Defaults preserve byte-identical legacy behavior on every untouched lane.
- **Config:** enabled (`arm=0.10`, `gap=0.15`, `hold_winners=true`) on the three positive-early-TP-regret lanes only — `bnb_macro` BUY_YES 5m/15m (new block), `eth_macro` BUY_YES 15m (new `up` block), `xrp_macro` BUY_NO 15m. `bitcoin`/`hype` deliberately untouched (their TP-at-0.30 tested *negative* regret).
- **Provenance:** derived from per-lane `hold_minus_exit_pnl` on 230 `take_profit` rows in `data/calibration/trades_settled.jsonl`. Forward-test only — exits are not ghost-validatable. Needs bot restart.
- **Tests:** 2 new cases in `tests/test_updown_exit_shared.py` (positive floor locks gains; defaults-off preserve legacy). Full suite **731 passed, 2 skipped**.

---

## 2026-06-01 — Decisive AI system prompt (root-cause HOLD bias) + budget 5→6 for HYPE/DOGE/BNB

**[`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):**

- **Root cause:** even after the marginal lane went veto-only, the shared decision/analysis `SYSTEM_PROMPT` was the deeper driver of the 77%-HOLD / 3%-approval behavior — it told the model to "be conservative — prediction markets often overestimate certain outcomes" and offered HOLD as a co-equal third option with no usage guidance, so a conservative engine on near-coin-flip 15m/1h markets defaulted to HOLD.
- **Rewrite:** removed the inaction bias; instructed it to commit to a direction on any real evidence lean; reframed HOLD as requiring a *specific, evidence-based* reason (never a default for uncertainty); clarified `confidence_score` = strength of the directional evidence, not certainty of outcome. Generic phrasing (no quant-edge assumption) so it's safe for all `analyze_market` callers (decision layer, narrators, strategy assists, annotation).
- **Versioning:** `DEFAULT_PROMPT_VERSION` + config `prompt_version` → `lane-feedback-v2-decisive` so `ai_decision_settler` can split pre/post verdicts.
- **Budget:** `max_ai_calls_per_scan` 5→6 for HYPE/DOGE/BNB (chose 6 over 7: serial-call worst case 6×40s=240s stays near the ~214s median scan cadence; 7×40s=280s would overrun under timeout clusters / tight p25 cadence — revisit upward only after trimming the alt decision timeout).
- **Boundary:** forward-test only (not ghost-validatable). Needs bot restart. Watch HOLD% / approval% in `decision_layer.jsonl`. Tests: 238 green (ai_agent_parse/bitcoin/sol/eth/exec-drivers).

## 2026-06-01 — Marginal AI gate → veto-only + BTC decision-timeout fix + per-asset AI-call budget bump

**[`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py), [`src/strategies/{bitcoin,sol_macro,eth_macro}.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`tests/test_strategy_execution_drivers.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_strategy_execution_drivers.py):**

- **Diagnosis (5.5h of `data/logs/ai_pipeline/decision_layer.jsonl`):** the rebuilt gate approved only **10/307 (3%)** of AI-evaluated candidates. Causes: (1) system prompt biases toward HOLD ("be conservative" + HOLD offered as a co-equal option) → **77% HOLD** on near-coin-flip 15m/1h markets; (2) `min_confidence: 0.60` is structurally unreachable for honest ~50/50 calls; (3) HOLD is a hard veto on the fail-closed marginal lane; (4) **34% of calls time out** (BTC at 15s, alts' MiniMax tail past 40s). Net effect: XRP **0/16**, DOGE **0/63**, HYPE **1/95**, BNB **4/89** on the AI path — 15m/1h effectively shut off, surviving only on the 5m pure-quant path.
- **Marginal lane → veto-only:** new `veto_only` param on `evaluate_trade_decision`. The AI may only REJECT a marginal candidate with a *confident, directly-opposing* directional call (conf ≥ `min_confidence`); HOLD / SKIP / low-confidence / agreement fall back to the quant trade. Threaded through the 4 marginal call sites (sol_macro ×2, eth_macro, bitcoin marginal-branch only — `neutral_15m` keeps the strict contract); guarded the redundant local re-gate at the "thick" sites. Opt-out: `decision_layer.marginal_veto_only: true`.
- **BTC decision timeout:** added `ai_decision_timeout_sec: 40` to the BTC block (was falling back to legacy 15s → 100% timeout vs MiniMax ~22s).
- **AI-call budget:** `max_ai_calls_per_scan` 3→5 for HYPE/DOGE/BNB (pegged the budget in 41–52% of scans; serial-call latency ~15s median vs ~214s scan cadence leaves ample headroom). BTC/ETH/SOL/XRP left at 3 (not budget-constrained).
- **Test fix:** `_bare_polybot()` now seeds `_bg_tasks` (the fixture skips `__init__`, so the `_spawn_bg` background-task registry was missing → 11 pre-existing red tests in `test_strategy_execution_drivers.py`, unrelated to the backtest removal). Now green.
- **Boundary:** AI-gate behavior is forward-test only (not ghost-validatable). `dry_run` throughout. Needs a bot restart to load. Tests: 234 (ai_agent/bitcoin/sol/eth) + 25 (execution drivers) + 14 (buy_yes/self_healing) green. Bundles in-progress parallel-agent BUY_YES lane-repair / self-healing work already present in the working tree.

## 2026-05-31 — AI decision layer rebuilt (sync, 15m/1h-only, fail-open) + 68GB disk reclaim + 1h/15m→4h neutral fallback

**[`src/strategies/{bitcoin,sol_macro,eth_macro}.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`src/analysis/ai_decision_settler.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_decision_settler.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), `.gitignore`:**

- **Disk:** reclaimed **68 GB** by deleting 2,311 abandoned `.git/objects/pack/tmp_pack_*` files (garbage from auto-gc dying on a full disk; real history is only 387 MB). Root cause: runtime data files tracked in git. Fix: `git rm --cached -r data/` (working copies kept) + ignore `data/` wholesale; deleted broken `data/backtest/`. Ghost calibration data preserved on disk.
- **AI decision layer made real:** every AI entry path (default + marginal + btc neutral_15m) converted from the async enqueue/expire broker (which silently dropped trades) to a **synchronous, bounded-timeout** call. **5m never calls AI** (latency ≫ window) — pure quant. Gate runs **15m/1h only**. **Default lane fail-OPEN** (AI down/slow/timeout → take quant trade); marginal/neutral fail-CLOSED (skip the below-threshold extra). Gate timeout 60s→40s (MiniMax answers ~22s). Every verdict logged to `data/logs/ai_pipeline/decision_layer.jsonl`.
- **Settler:** new `ai_decision_settler.py` scores logged verdicts vs real Polymarket outcomes (Brier, directional hit-rate, veto quality, REAL-DECISION-LAYER section for the live gate).
- **1h/15m neutral fallback:** when an alt's 1h (or 15m) horizon bias is NEUTRAL it falls back to the asset's OWN 4h (`_get_4h_bias`), neutral-only, so the lane resolves an alt-native direction instead of sitting out. BTC already had this. Decided trades unchanged.

**Boundary:** AI gate + fallback edits are forward-test only (candidate-gen / not ghost-validatable). `dry_run: true` throughout. Needs a bot restart to load.

**Verification:** `py_compile` all touched modules; `pytest tests/test_bitcoin.py tests/test_sol_macro.py tests/test_eth_macro.py tests/test_ai_agent_parse.py` → **213 passed**. Live dry_run confirmed gate fires and fail-open lets trades through (no starvation).

## 2026-05-31 — Suppress anti-predictive BUY_NO short cells

**[`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml)** — commit `5d8cbc0`. Held-to-resolution analysis of `trades_settled.jsonl` (n=632) found true entry accuracy 45.6% (below coin flip), driven entirely by BUY_NO/short (40.1% vs BUY_YES 49.9%). Disabled two structural-contradiction short cells: BTC counter-trend `disable_buy_no_counter_trend: true` (BUY_NO-while-BULLISH, 35% WR n=88), and a new opt-in `disable_buy_no_5m_native` (ON for all 6 alts) suppressing inverted 5m-native shorts (eth 11.8% / xrp 16.7% / doge 27.8% / sol 33% vs 15m-native 50-65%), routed through `_log_skip_reject` to keep ghost-logging. IQL/MACD soft-scoring (Kimi plan) parked — correlation showed the 5m-short veto is a coin-flip, so the signal not the gate is inverted. Hypothesis tracking in the strategy vault (`bitcoin.md`, `sol_macro.md`, and alt files, 2026-05-31).

---

## 2026-05-31 — AI decision layer made binding on default entry lanes

**[`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py), [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), tests:** Corrected the AI decision layer from marginal/shadow-style behavior toward actual bounded entry control.

- `AIAgent.evaluate_trade_decision()` no longer turns marginal `HOLD`, low-confidence direct answers, or shadow-portfolio holds into approved “abstain” decisions. Those now reject the candidate with explicit `direct_ai_hold`, `direct_ai_low_confidence`, `shadow_portfolio_hold`, or `shadow_portfolio_low_confidence` reasons.
- Enabled `default` decision-layer enforcement for SOL-family alt strategies (`sol_macro`, `hype_macro`, `xrp_macro`, `doge_macro`, `bnb_macro`) via the existing post-composite default-lane hook.
- Wired `eth_macro` default-lane entries into the enforced AI decision path explicitly, since ETH has a separate scan loop and would not honor the config-only `default` lane otherwise.
- Wired BTC default up/down entries into the same enforced decision path, including 5m candidates through the async decision broker. The legacy `use_ai_updown_5m` marginal-assist flag remains separate; the execution layer now keys off `ai.decision_layer.enforced_lanes.bitcoin: [default, neutral_15m, marginal]`.
- Made the boundary explicit: `ai.decision_layer` is the execution layer and is toggleable via `ai.decision_layer.enabled`; `ai.shadow_pipeline` / `ai.shadow_observer` are data/telemetry layers only and do not gate entries when the decision layer is off.

**Boundary:** This is not structural-gate bypass yet. Hard/safety gates remain deterministic; AI now supervises admitted default entry candidates and existing marginal candidates.

**Verification:** `.venv/bin/python -m pytest tests/test_ai_agent_parse.py tests/test_ops_pulse.py tests/test_ai_decision_broker.py tests/test_eth_macro.py tests/test_sol_macro.py -q` passed (`175 passed`, one upstream `websockets.legacy` deprecation warning).
Additional BTC/broker verification: `.venv/bin/python -m pytest tests/test_ai_agent_parse.py tests/test_ai_decision_broker.py tests/test_strategy_two_cycle_resolve.py tests/test_bitcoin_scenarios.py -q` passed (`66 passed, 2 skipped`).

## 2026-05-31 — Morning-session damage control: exits, ETH gate, SOL/XRP 1h starvation

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), strategy logs:** Audited active local session `test_20260531_041319` against May 30/May 31 ghost and journal data, then shipped three scoped repairs.

- Lowered global up/down `take_profit_pct` from `0.50` to `0.30`; active-session mark replay showed `tp=0.30` at `51.3%` WR / `+$44.39` on replayable crypto paths versus `42.3%` WR / `+$4.33` at `tp=0.50`.
- Added `eth_15m_weak_confirm_hard_gate_enabled: false`; ETH 15m weak confirmation now adds a soft min-edge penalty instead of hard-skipping when disabled. May 31 settled ghosts showed that gate blocking `61.5%` WR / `+21.3%` ROI (`n=600`).
- Widened SOL and XRP 1h entry windows from `60.0` to `360.0` in both `by_tf` and entry-policy overrides, matching the future-listed 1h market feed and prior ghost evidence. DOGE 1h was left capped because its 1h ghost lane was below 50%.

Validation: `.venv/bin/python -m pytest tests/test_eth_macro.py tests/test_sol_macro.py tests/test_updown_exit_shared.py` passed (`146 passed`).

## 2026-05-31 — Ghost-backed entry-window optimization + stale legacy tests retired

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`tests/test_sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_sol_macro.py), [`tests/test_eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_eth_macro.py):** Widened only the entry-window cells with clear settled-ghost missed EV since `2026-05-30T00:00Z`: BNB `5m/15m/1h BUY_YES`, BNB `15m BUY_NO`, ETH `15m/1h BUY_YES`, and HYPE `1h BUY_YES`. Left protective/noisy DOGE, XRP, SOL, ETH downside, and HYPE 15m buckets unchanged.

**Why:** `lane_entry_window` was still blocking profitable upside candidates after the earlier May 28 pass. The largest current misses were `bnb_macro|15m|BUY_YES|lane_entry_window` (`n=8,972`, `WR=59.8%`, `netGate=-1,689`), `eth_macro|1h|BUY_YES|lane_entry_window` (`n=467`, `WR=79.0%`, `netGate=-271`), and `hype_macro|1h|BUY_YES|lane_entry_window` (`n=367`, `WR=66.2%`, `netGate=-118`).

**Legacy cleanup:** Updated stale tests that still expected BTC-regime short blockers to fire by default and called the removed `_passes_15m_iql` helper. The tests now assert the intended replacement behavior: BTC-regime short blocks are opt-in only, and IQL coverage uses the current horizon-aware `_passes_iql` helper.

---

## 2026-05-28 — Kimi removed; MiniMax promoted to primary + upgraded to highspeed

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py), [`src/ai_status.py`](/Users/mainfolder/Documents/psb-main%201/src/ai_status.py), [`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`src/dashboard/server.py`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py), tests:** Removed the Kimi (kimi_coding / Moonshot-OAuth) integration entirely and made MiniMax the sole paid decision provider, upgraded to the highspeed M2.7 build for faster decision cycles.

- **Config:** `direct_decision_provider_names` `[kimi_coding]` → `[minimax]`; dropped the `kimi_coding` `provider_chain` block; `minimax` model `MiniMax-M2.7` → `MiniMax-M2.7-highspeed`. Chain is now `minimax (highspeed) → ollama_local`.
- **ai_agent.py:** deleted `_kimi_oauth_lock`, `_is_kimi_coding_provider`, `_kimi_code_device_model`, `_kimi_code_common_headers`, `_kimi_code_access_token`, `_analyze_with_kimi_coding` and the kimi-only helpers `_expand_user_path` / `_ascii_header_value`; removed the now-unused `import platform`; `run_minimax_live_probe` default model → highspeed. Provider dispatch is dynamic (`_analyze_with_<type>`), so no router edits were needed.
- **ai_status.py:** removed the kimi_coding OAuth readiness branch + `_has_kimi_code_oauth`.
- **main.py / dashboard/server.py:** dropped `MOONSHOT_API_KEY` from key loading; dashboard `_ai_summary_minimax` model → highspeed.
- **usage_tracker.py:** unchanged — `MiniMax-M2.7-highspeed` pricing ($0.60/$2.40 per M, 2× standard) already present; cost resolves by model string.
- **Tests:** deleted the two `_analyze_with_kimi_coding` unit tests and the two kimi OAuth `ai_status` tests; rewrote the provider-scope tests to use minimax-primary + local-fallback. `tests/test_ai_agent_parse.py` + `tests/test_ai_status.py` (36) and `tests/test_ops_pulse.py` (5) green.

**Cost note:** highspeed is 2× per-token vs standard M2.7. With the decision layer still `ai.decision_layer.enabled: false`, MiniMax currently runs only shadow/research/narration, so live decision spend is unchanged until the decision layer is turned on. `.env` `MINIMAX_API_KEY` retained; legacy `MOONSHOT_API_KEY` is now unused (left in `.env`, no longer read).

---

## 2026-05-28 — Performance-feedback loop CLOSED (loosen live, tighten disarmed)

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Flipped `performance_feedback.enabled` to `true` and made it safe to leave on. Root cause of every prior failed "close": the loop has two halves and enabling it turned on both —

1. **Drift-tighten** ([`refresh_performance_feedback`](/Users/mainfolder/Documents/psb-main%201/src/execution/performance_feedback.py) → `load_backtest_expectations`) compares live performance to `data/backtest/reports/backtest_*.json`. There are **263** such reports and they are known-broken (per CLAUDE.md): they report ~8–25% win rates against a 40–50% reality. With those expectations, `check_drift` flags every strategy as "diverging" and pushes `min_edge` to the `max_min_edge_mult` ceiling (1.15). That is the mechanism that tightened the bot every time the loop was switched on.
2. **Overtight-loosen** ([`check_overtight`](/Users/mainfolder/Documents/psb-main%201/src/execution/performance_feedback.py)) reads `rejected_candidates_settled.jsonl` (the ghost log — the truthful counterfactual) and *loosens* `min_edge` on lanes whose ghost-rejected pool wins ≥ `overtight_ghost_wr_threshold` (0.58). This is the half we want.

**Fix (config-only, robust):** permanently disarm the tighten half by clamping `min_min_edge_mult = max_min_edge_mult = diverge_min_edge_mult = kelly_mult_when_diverging = 1.0`. The drift multiplier is `max(min, min(diverge, max))`, so at 1.0/1.0/1.0 it is unconditionally 1.0 — the broken backtests can never tighten the gate again, no matter what they contain or how live samples grow. The loosen half runs unaffected off the ghost log.

**Verified live** against the real config + ghost log (not asserted): all 7 strategies return drift `min_edge ×1.000` / `kelly ×1.000` with the 263 broken reports present; loosen side returns `hype_macro|15m|down|bearish ×0.966` (ghost WR 72%, n=451) and `hype_macro|15m|up|bullish ×0.934` (ghost WR 64%, n=387) — matching the baseline doc's overtight flags.

Also reverted a same-day mistaken edit that added `lane_entry_window`/`iql_15m_reject`/`hist_gate_15m_short_reject` to `overtight_reasons`: the loosen math requires edge context that time/quality rejects don't carry, so those reasons are inert there and only risk diluting lane WR buckets. Entry-window starvation is handled by the per-lane window widening below.

**Do not raise the drift multipliers until the backtester is fixed and its reports are trustworthy.**

---

## 2026-05-28 — Per-lane `lane_entry_window` widening + overtight watchlist expansion

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** `lane_entry_window` is the largest rejection gate (~44% of ghost rejects). The previous take — "mostly future-market ladder being correctly skipped" — was partially right: ladder noise dominates the count, but the gate was *also* killing real +EV markets where ghost-settled WR clears 53–66% on lanes the bot would gladly take. Bucketed all 4,445 `lane_entry_window` rejects by `(strategy, window, side, eval_mins_left)` and kept only adjacent buckets with n≥150 AND WR≥53% AND EV≥+0.04. Five lanes qualified:

| Lane | Window | Ghost evidence |
|---|---|---|
| `bnb_macro\|15m\|SHORT` | 2–32 → **2–50** | 30–40 n=1496 WR 53.1% +0.063 / 40–50 n=1515 WR 52.8% +0.056 |
| `bnb_macro\|1h\|SHORT`  | 0–60 → **0–120** | 60–120 n=756 WR 55.2% +0.107 |
| `hype_macro\|15m\|LONG` | 2–36 → **2–50** | 40–50 n=568 WR 53.2% +0.063 |
| `hype_macro\|1h\|SHORT` | 1–59 → **1–120** | 60–120 n=499 WR 58.9% +0.179 |
| `eth_macro\|1h\|LONG`   | 0–60 → **0–120** | 60–120 n=143 WR 66.4% +0.349 (smaller n; watch first 50 admitted) |

Lanes deliberately untouched: all SOL (−EV outside window), all DOGE (−EV), all 5m (−EV adjacent), all 15m LONG on bnb/doge/xrp (−EV bridge), XRP 15m (non-monotone bridge — the 30–60 trough is mildly −EV even though 60–120 recovers). Removing the gate entirely was rejected — exit logic assumes short remaining-time windows and the rejected pool is dominated by random-WR ladder noise, so removal would flood without quality filtering.

Separately, `performance_feedback.overtight_reasons` was only watching `lane_min_edge` — adding `lane_entry_window`, `iql_15m_reject`, and `hist_gate_15m_short_reject` (the top three rejection gates by count). The auto-loosener stays gated by `performance_feedback.enabled: false`; this is preparation only.

Plan file: `/Users/mainfolder/.claude/plans/whimsical-purring-owl.md`.

---

## 2026-05-28 — Per-lane BUY_YES floor bumps on 5m/15m (BTC + BNB + HYPE)

**[`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), [`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Extended the BTC-1h BUY_YES floor-bump pattern (`7d28c4e`) down to **5m and 15m** and to the **alt** strategies. Diagnosis: 98% of BUY_YES candidates carry a *correct* BULLISH bias but are rejected on `lane_min_edge` because `est_prob_up` under-shoots UP probability — median `yes_price` (0.55–0.90) sits above the model `est` (0.44–0.58), so model-edge goes negative. The ghost log (`rejected_candidates_settled.jsonl`) settles those rejected in-window BUY_YES at **68–76% WR** and **+EV per $1** on btc/hype/bnb 5m+15m, confirming the gate is firing on miscalibrated input rather than real −EV. (`lane_entry_window` reject volume is mostly the future-market ladder being correctly skipped, not starvation.)

- BTC: generalized the `bitcoin.py` floor hook to read `btc_<tf>_buy_yes_bullish_floor_bump` per window; config adds `btc_5m=0.20`, `btc_15m=0.26` (1h keeps code default 0.08).
- Alts: new `_alt_buy_yes_bullish_floor_bump` helper, applied **post-calibration** (edge space, mirrors BTC) in both the 5m and 15m/1h updown paths. Asymmetric (BUY_NO untouched), BULLISH-only. Enabled via `<tf>_buy_yes_bullish_floor_bump` per strategy: `hype_macro` 5m=0.18/15m=0.10, `bnb_macro` 5m=0.19/15m=0.19.
- Sizing is ghost-calibrated per lane so the median in-window est lifts to ≈ realized WR (just clears `min_edge`). **SOL (all TFs) and DOGE/XRP 15m are intentionally left off — ghost shows them −EV.** DOGE-1h / XRP-1h +EV is real but uncapturable (entry price ≈0.90 vs the 0.90 est clamp), so not bumped. Any window's bump = 0.0 disables it without redeploy.

---

## 2026-05-28 — Alt 1H BUY_YES native-bias uplift

**[`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`tests/test_sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_sol_macro.py), [`tests/test_xrp_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_xrp_macro.py), [`tests/test_hype_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_hype_macro.py):** Added a narrow raw-probability uplift hook for SOL-family strategies on **native 1h bullish BUY_YES** lanes when 15m confirmation is already present, but left the hook **default-off** in shared code. It is now explicitly enabled only for `xrp_macro` and `hype_macro` in config. The uplift is limited to `window_size == "1h"`, `allowed_side == "LONG"`, `*_1h_native` side sources, and a minimum LTF-strength threshold, so mixed or fallback hourly lanes are unchanged and DOGE does not inherit it by accident. BTC was intentionally left untouched in this edit.

**Why:** Active `lane_identity_v2_source_resolver` posteriors showed current-version 1h upside starvation across the alt stack, especially XRP/HYPE-class inheritance, while BTC already had separate in-progress work. Rejected-ghost review showed some 1h bullish alt setups were reaching the raw model but stalling near the edge floor rather than being corrected by calibration, so this change addresses the pre-calibration underconfidence without globally loosening weak 1h lanes.

## 2026-05-28 — Ghost lane reconstruction aligned to uniform bias families

**[`src/analysis/lane_identity.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_identity.py), [`src/analysis/calibration_buckets.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/calibration_buckets.py), [`src/analysis/rejected_candidate_log.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/rejected_candidate_log.py), [`src/analysis/ghost_calibration.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ghost_calibration.py), [`src/analysis/lane_thresholds.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_thresholds.py), [`tests/test_lane_identity.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_lane_identity.py), [`tests/test_ghost_calibration.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ghost_calibration.py):** Preserved the new `*_native`, `*_vs_slower`, and `*_neutral_fallback_*` side families inside the ghost and calibration data loop instead of collapsing missing live lane IDs back to `standard`. Rejected-candidate writes now infer `lane_family` when callers omit it, Ghost settlement and lane-threshold learning rebuild live lane IDs from recorded side-selection metadata, and side-source bucketing now tags the uniform architecture explicitly as `native`, `vs_slower`, or `neutral_fallback`.

**Why:** The strategy refactor introduced new side-source taxonomy, but ghost settlement and threshold derivation still had legacy fallbacks that forced many records into old `standard` lanes. This change keeps the feedback loop closed so Ghost Lab, per-lane thresholds, and future post-merge analysis keep reading the same family structure the live strategies are now emitting.

## 2026-05-27 — Uniform multi-timeframe bias architecture cutover

**[`src/analysis/sol_btc_service.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py), [`src/analysis/btc_price_service.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/btc_price_service.py), [`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), [`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), [`tests/test_sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_sol_macro.py), [`tests/test_eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_eth_macro.py), [`tests/test_bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_bitcoin.py):** Added explicit 5m/15m/1h indicator snapshots to the BTC and shared alt analysis objects, then rewired BTC, SOL-family, and ETH direction selection onto uniform per-timeframe bias producers. Each bias helper now resolves directional state from a consistent 3-vote model (MACD state, RSI zone, EMA/price alignment), side-selection is market-timeframe specific instead of cycle-global, and neutral matched-horizon paths can fall back to slower non-neutral bias with a soft est-prob penalty instead of forcing a hard sit-out. BTC keeps 4H bias in the system as the documented slow backup filter via `btc_<tf>_vs_slower` / `btc_<tf>_neutral_fallback_4h` resolver paths; SOL/XRP/HYPE/BNB/DOGE inherit the shared alt implementation, and ETH now uses the same native / vs-slower / neutral-fallback taxonomy instead of the old market `hybrid` / `signal_first` side chooser.

**Why:** The prior architecture mixed cycle-level side decisions, BTC-secondary overrides, and strategy-specific naming (`primary_htf`, `hybrid_alt_first`, `btc_htf_bias`) in ways that made 5m/15m/1h behavior uneven across assets. The operator requested one mental model across all short-window strategies while preserving today’s thresholds as soft translations, so the cutover keeps the current edge/risk machinery but makes timeframe-native bias resolution explicit and comparable.

## 2026-05-28 — Beta-veto historical backfill and disabled-state checkpoint

**[`src/analysis/beta_veto_backfill.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/beta_veto_backfill.py), [`tools/backfill_beta_veto_rows.py`](/Users/mainfolder/Documents/psb-main%201/tools/backfill_beta_veto_rows.py), [`tests/test_beta_veto_backfill.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_beta_veto_backfill.py):** Added a reproducible historical beta-veto reconstruction pass that replays live lane Beta(2,3) posteriors from `data/calibration/trades.jsonl` and emits derived veto-eligible ghost rows for a chosen `(max_mean, min_n)` setting. Ran it for the unfinished `0.42 / 30` experiment and committed the outputs at [`data/calibration/beta_veto_historical_rows.jsonl`](/Users/mainfolder/Documents/psb-main%201/data/calibration/beta_veto_historical_rows.jsonl) and [`data/calibration/beta_veto_historical_summary.json`](/Users/mainfolder/Documents/psb-main%201/data/calibration/beta_veto_historical_summary.json). The working-tree restart config was also verified to be in the disabled state (`beta_veto_max_mean: 0.0`, `beta_veto_min_n: 0`) before handoff.

**Why:** The operator wanted the current beta-veto experiment preserved before throughput recovery. Existing rejected/settled ghost ledgers did not already contain a mature explicit `beta_vetoed` family, so the correct provenance path was historical reconstruction from live trade chronology rather than inference-by-memory.

## 2026-05-27 — Loss-streak auto-resume wiring: recovery + green/non-deadzone gates

**[`src/execution/exposure_manager.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/exposure_manager.py), [`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`tests/test_exposure_manager_sizing.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_exposure_manager_sizing.py):** Added lane pause reactivation controls so a loss-streak pause can require (a) portfolio PnL recovery vs a pause-time anchor (`loss_pause_recovery_multiple`), (b) a green window, and (c) non-deadzone regime context before auto-resume. Main now syncs daily realized PnL into all lane exposure managers and feeds regime gate state (`combined_regime` deadzone flag + gate-allowed state) into each lane’s resume context.

**Why:** Operator requested safer per-asset reactivation semantics after 3-loss pauses instead of pure cycle-based resume. This keeps the hard loss-streak pause while preventing immediate re-entry in deadzone windows or before adequate recovery.

## 2026-05-27 — Live calibration cuts for four leaking gates

**[`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), [`src/strategies/btc_updown_5m.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/btc_updown_5m.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`tests/test_bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_bitcoin.py):** Converted BTC non-5m `lane_min_edge_bias_quant_disagree` from a hard min-edge rejection path into an admitted path with `bias_quant_disagree_size_multiplier: 0.5`; BTC 5m disagreement remains strict because the post-commit ghost slice was negative on realized value. Lowered only selected lane-level `lane_min_edge` thresholds with positive post-commit ghost support, dropped HYPE 1h BUY_NO liquidity to its existing 1h base floor, and changed BTC `hist_gate_5m_short_reject` to telemetry-only when `hist_gate_5m_short_hard_reject: false`.

**Why:** Post-commit ghost results from `test_20260527_042014` showed the remaining missed winners concentrated in four legacy gates: `lane_min_edge_bias_quant_disagree`, residual `lane_min_edge`, 1h BUY_NO `liquidity`, and `hist_gate_5m_short_reject`. The edit intentionally avoids global min-edge loosening; DOGE/BNB liquidity and negative/weak buckets are left unchanged.

## 2026-05-27 — Crypto circuit breakers for correlated stop cascades

**[`src/analysis/circuit_breakers.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/circuit_breakers.py), [`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`tests/test_circuit_breakers.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_circuit_breakers.py):** Added global side-specific crypto up/down circuit breakers. Fast mode halts a side after 3 same-side stop exits inside 60 seconds; slow mode halts after 6 same-side stop exits inside 15 minutes; BTC reversal mode halts new entries on a dominant side after a 0.3% adverse BTC move over 5 minutes with at least 5 same-side open positions.

**Why:** The prior breaker proposal handled clustered cascades but not the observed slow 5/26_04 bleed. This change blocks only new entries on the damaged side while keeping exits and offsetting opposite-side entries available for the next paper/session round.

## 2026-05-26 — Alt macro resolver metadata parity

**[`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), [`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`tests/test_strategy_execution_drivers.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_strategy_execution_drivers.py), [`tests/test_sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_sol_macro.py):** Added BTC-compatible resolver metadata to shared macro signals and ETH macro signals: `conflict_type`, `resolver_path`, `htf_side`, `quant_side`, and `momentum_side`. The execution path now persists those fields into journal extras and position `entry_signal`; selected tests cover metadata construction plus journal/position propagation.

**Why:** SOL/ETH/HYPE/XRP/DOGE/BNB already had oracle validation config plumbing and HTF bias fields, but their direction-resolution metadata was weaker than BTC’s. This is an observability/parity change only; it does not alter entry gates, thresholds, sizing, or order routing.

## 2026-05-25 — Calibration lane identity v2 and persistent ghost metadata

**[`src/analysis/lane_identity.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_identity.py), [`src/analysis/calibration_log.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/calibration_log.py), [`src/analysis/lane_calibration.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_calibration.py), [`src/analysis/rejected_candidate_log.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/rejected_candidate_log.py), [`src/analysis/ghost_calibration.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ghost_calibration.py), [`src/dashboard/server.py`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py):** Added persistent normalized ghost/trade metadata, a `lane_identity_v2_source_resolver` posterior namespace, BTC resolver-path lane families, and alt 5m downside side-source lane families. Settled ghosts now preserve `ghost_lane_id` and `live_lane_id` so Ghost Lab can aggregate against persistent live calibration lanes instead of per-session reject buckets.

**Why:** The audit showed plenty of ghost volume, but BTC conflict paths and alt 5m downside sources were being mixed into broad posterior buckets. This pass makes the data feed persistent and separable before any threshold or gate tuning.

## 2026-05-25 — BTC direction conflict resolver refactor

**[`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), [`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`tests/test_bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_bitcoin.py), [`tests/test_strategy_execution_drivers.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_strategy_execution_drivers.py):** Replaced BTC's scattered HTF/rollover/quant-flip side assignment with a `BTCDirectionDecision` resolver object that carries `conflict_type`, `resolver_path`, `htf_side`, `quant_side`, and `momentum_side` into signals, position `entry_signal`, journal extras, and rejected-candidate context. Current thresholds and admission gates are unchanged; this pass makes the conflict path first-class and testable.

**Why:** The BTC audit showed admitted 15m downside lanes underperforming while rejected bias/quant disagreement ghosts were strong. Before tuning that conflict, side resolution needs one canonical decision surface instead of separate rollover and quant-flip mutations.

## 2026-05-25 — Alt macro lane bias fields split

**[`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), [`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`tests/test_strategy_execution_drivers.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_strategy_execution_drivers.py):** Added explicit `primary_htf_bias`, `alt_htf_bias`, and `btc_htf_bias` fields to macro signals, populated them from the strategy scanners, and changed live lane construction to use distinct `primary + alt + btc_1h` regime tokens instead of duplicating `signal.htf_bias` into both the primary and alt slots. The position `entry_signal` now carries the resolved lane metadata for closed-trade calibration.

**Why:** Session review showed SOL-family alt trades collapsing into the same `bearish__bearish__bull` lane bucket. The execution path was building lane metadata from `signal.htf_bias` twice, so calibration could not distinguish primary macro bias from alt-native HTF state even when the scanner had richer diagnostics.

## 2026-05-25 — Ghost Lab deadzone decision digest added

**[`src/dashboard/server.py`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py), [`src/dashboard/index.html`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html), [`tests/test_dashboard_bundle.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_dashboard_bundle.py):** Added `/api/ghosts/decision-digest`, a structured dashboard endpoint that combines deadzone counterfactual buckets, ghost-gate report rows, and lane calibration rows without importing Hermes cron wrappers or shelling out from the request path. Ghost Lab now has a **Deadzone theory** panel showing resolved deadzone-skip performance by UTC hour/regime beside the top ghost-gate and calibration signals. The older one-off `ghost_regime_report.py` artifact was removed; `tools/ghost_gate_report.py` remains the canonical ghost report surface.

**Why:** The deadzone thesis needs live, structured evidence while data is collected: whether would-be blocked hours are actually cold, which regimes matter, and whether gates are saving loss or blocking winners. Hermes can still format notifications, but PSB owns the data and dashboard-visible interpretation.

## 2026-05-25 — AI decision-gate attribution added

**[`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`tests/test_strategy_execution_drivers.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_strategy_execution_drivers.py):** Added entry-journal attribution fields for `ai_enabled_at_entry`, `ai_consulted`, `ai_verdict`, and `ai_influenced_decision`, plus explicit aliases for decision-gate state and analytics/live-inferencing state. Current `decision_gates.enabled: false` remains unchanged, so the next session can keep collecting quant-first entries while preserving clean AI attribution columns.

**Why:** The existing `ai_used` field only showed whether an AI path was touched. It did not distinguish analytics availability from pre-entry decision-gate enforcement, which made “AI off” versus “AI available but not gating” ambiguous in session review.

## 2026-05-25 — BNB/DOGE 1h exploration before restart

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Added explicit BNB/DOGE 1h exploration policy: 1h `up.min_edge 0.09 -> 0.08`, 1h `down.min_edge 0.08 -> 0.075`, 1h `entry_price_max 0.55 -> 0.58`, and `size_multiplier: 0.3` for DOGE 1h to match BNB’s calibration-sized 1h posture.

**Why:** Current calibration data showed BNB had zero 1h closed trades and DOGE had only one 1h closed trade. This is starvation, not enough evidence to classify those lanes as losers. The change is meant to collect 1h samples at small notional before restart.

## 2026-05-25 — SOL/HYPE exploration posture instead of suppression

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Re-opened SOL sample collection by leaving `alt_momentum_confirm` empty and `iql_15m_enabled: false`, but added `0.30x` SOL entry-policy sizing on 5m/15m up/down lanes. Nudged HYPE BUY_NO thresholds slightly lower (`min_edge_buy_no 0.08 -> 0.075`, 5m/15m down lane overrides `0.08 -> 0.075`) and restored HYPE’s wider discovery timing windows while keeping its exploratory `0.30x` lane sizing.

**Why:** The goal is not to simply cut losing lanes this early. SOL needs enough live/ghost samples to learn which predictions can become winners; the control should be smaller exploratory notional and better lane attribution, not starvation. HYPE was profitable in `test_20260525_051023` but mostly skipped on `lane_min_edge`, so it gets a small throughput nudge.

## 2026-05-25 — Restore SOL/DOGE gate posture after weak session

**Superseded:** Same-day operator feedback clarified that SOL should keep trading for calibration; the entry above re-opened SOL gates with smaller exploratory sizing.

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`src/analysis/lane_entry_policy.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_entry_policy.py), [`tests/test_lane_entry_policy.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_lane_entry_policy.py):** Reverted the uncommitted paper-loop loosening that removed SOL alt-momentum confirmation, disabled SOL 15m IQL, widened DOGE windows, and lowered the shared composite floor. Also fixed lane entry-policy side resolution so BUY_NO/down trades use the `down` override instead of the `up` override.

**Why:** Current paper session `test_20260525_051023` underperformed the May 22 baseline on WR/avg trade, with losses concentrated in SOL standard down lanes (`sol_macro|5m|down|bearish__bearish__bull|standard`: 1/9, -$37.57; SOL total: 3/16, -$44.31). The removed SOL gates were meant to protect the exact lane family that bled.

## 2026-05-25 — Same-market re-entry blocked

**[`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`tests/test_strategy_execution_drivers.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_strategy_execution_drivers.py):** Added a session-level traded-market ledger and execution guard. Once any strategy has entered a Polymarket `market_id` in the current session, later signals for that same market are skipped as `duplicate_session_market`, even if the prior position already exited.

**Why:** Fresh paper session `test_20260525_041239` re-entered HYPE market `2346183` three times after stop-loss exits inside the same 15m market. The old duplicate guard only excluded currently open markets, so repeated stop/re-entry loops were possible.

## 2026-05-25 — Session scan diagnostics persisted

**[`src/execution/trade_journal.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/trade_journal.py), [`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`tests/test_trade_journal_resumable.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_trade_journal_resumable.py), [`tests/test_scan_diagnostics_annotation.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_scan_diagnostics_annotation.py):** `summary.json` now refreshes immediately after entries, and each cycle appends a compact `scan_diagnostics` annotation with per-strategy signal counts, allowed side, action counts, side-source counts, and top skip reasons.

**Why:** Session review showed enabled bullish lanes could look silent in `entries.jsonl` even though live diagnostics had scanned and rejected them. Persisting scan diagnostics makes no-entry lane coverage auditable from the session journal instead of relying on transient dashboard state.

## 2026-05-25 — Lane calibration alpha amplification capped

**[`src/analysis/lane_calibration.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_calibration.py), [`tests/test_lane_calibration.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_lane_calibration.py):** Changed `ALPHA_CLAMP_HI` from `2.50` to `1.00`. Raw `alpha_ewma` can still exceed identity for telemetry, but the effective alpha used by live calibration can no longer amplify raw model confidence away from 50/50; sub-1 alpha shrinkage remains active.

**Why:** Session attribution showed `alpha_used > 1.0` concentrated losses in alt lanes while only BTC 5m benefited. The next session should test calibration as a one-sided risk reducer instead of an edge amplifier.

## 2026-05-24 — BUY_NO phantom filter corrected in journal summaries

**[`src/execution/trade_journal.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/trade_journal.py), [`scripts/parse_session_journal.py`](/Users/mainfolder/Documents/psb-main%201/scripts/parse_session_journal.py), [`src/analysis/journal_learning.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/journal_learning.py), attribution/report scripts, [`tests/test_trade_journal_resumable.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_trade_journal_resumable.py), [`docs/session_reports/eth_hype_bnb_session_audit_20260524_060424.md`](/Users/mainfolder/Documents/psb-main%201/docs/session_reports/eth_hype_bnb_session_audit_20260524_060424.md):** Fixed legacy phantom-exit filtering so `entry_price + current_price ~= 1.0` is treated as a token-flip phantom only for YES-leg rows. Long-NO exits can legitimately have complementary prices and should remain in realized PnL, win rate, session lists, reload state, session parsing, learning aggregates, and attribution reports.

**Why:** Session `test_20260524_060424` showed HYPE as `0/2, -$11.19` in the saved summary, but the raw journal had a valid HYPE take-profit at lines 1328/1335. Corrected parsing reports HYPE as `1/3, -$6.00`; this changes measurement only, not strategy entry/exit behavior.

## 2026-05-24 — Lane exit counterfactual analyzer

**[`scripts/analyze_exit_counterfactuals.py`](/Users/mainfolder/Documents/psb-main%201/scripts/analyze_exit_counterfactuals.py), [`tests/test_analyze_exit_counterfactuals.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_analyze_exit_counterfactuals.py), [`docs/session_reports/exit_counterfactuals_20260524_235019.md`](/Users/mainfolder/Documents/psb-main%201/docs/session_reports/exit_counterfactuals_20260524_235019.md):** Added a read-only lane-level exit analysis tool that reconstructs closed crypto up/down trade paths from journal marks plus optional no-cache OHLCV proxy marks, then reports MFE/MAE, hold-vs-actual regret, profit-capture ratio, triple-barrier labels, and winner-exit classes.

**Why:** The existing threshold replay only saw marks before the bot exited. This report gives PSB a lane-level way to test whether profitable exits were premature before changing live take-profit, stop-loss, or time-stop settings.

## 2026-05-24 — Kimi-only direct decision policy

**[`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py), [`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`tests/test_ai_agent_parse.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ai_agent_parse.py):** Split AI provider routing by scope so direct execution / tie-break decisions use only `kimi_coding`, with no MiniMax fallback. MiniMax remains in the general provider chain for shadow observation, research narration, and calibration analysis. Reduced active macro strategy `max_ai_calls_per_scan` caps to `1` so Kimi quota is spent on the highest-priority marginal candidate per scan.

**Why:** The available settled sample showed Kimi's one graded directional call beat quant while MiniMax underperformed quant on the same markets. This change keeps execution quant-first, reserves Kimi for scarce premium tie-breaks, and prevents MiniMax from steering entries while still preserving its usefulness for non-execution data gathering.

## 2026-05-24 — Kimi quota efficiency and JSON hardening

**[`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`tests/test_ai_agent_parse.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ai_agent_parse.py):** Hardened the Kimi Code provider so each market uses one JSON-mode attempt by default, extracts usable text from alternate OpenAI-compatible message fields, applies provider-specific low-temperature / short-output caps, and cools Kimi down after empty/invalid JSON or quota errors instead of retrying it every scan.

**Why:** Live logs showed Kimi spending quota on HTTP 200 responses with empty/unparseable `message.content`, then often making a second fallback attempt before MiniMax. The new behavior preserves Kimi for high-value marginal tie-breaks while reducing repeated quota burn and falling through to MiniMax faster when Kimi is exhausted or malformed.

## 2026-05-24 — BTC prediction-window bonus disabled for next paper restart

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), [`tests/test_bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_bitcoin.py):** Added configurable BTC prediction-window timing bonuses and set both `prediction_window_bonus_15m` and `prediction_window_bonus_5m` to `0.0`.

**Why:** The current 200-trade paper session showed BTC `predict_window` as the cleanest repeat loser (`16` trades, `-$35.47`) while BTC drift/spike/standard short families were positive. Disabling the standalone prediction-window boost removes that weak admission path without turning off the stronger BTC countertrend/momentum families.

## 2026-05-24 — Paper trade limit separated from live cap

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`src/execution/clob_client.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/clob_client.py), [`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`tests/test_risk_manager_hardening.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_risk_manager_hardening.py):** Raised the live daily trade cap to `500` and added `risk.paper_max_trades_per_day: 2000` for dry-run calibration. `RiskManager.can_trade()` now checks the effective cap for the active mode, and cycle logs report that same effective limit.

**Why:** The previous single `200` cap stopped paper calibration early and also looked too low for live operation. Keeping separate caps preserves a runaway-trade guardrail while allowing paper sessions to collect enough samples for lane calibration.

## 2026-05-24 — Kimi Code OAuth decision provider corrected

**[`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Replaced the normal Moonshot OpenAI-compatible decision entry with a dedicated `kimi_coding` provider that reads the local Kimi Code OAuth credentials from `~/.kimi/credentials/kimi-code.json`, refreshes through `https://auth.kimi.com/api/oauth/token`, and calls `https://api.kimi.com/coding/v1` with the Kimi CLI `X-Msh-*` headers and `model: kimi-for-coding`.

**[`src/ai_status.py`](/Users/mainfolder/Documents/psb-main%201/src/ai_status.py), [`tests/test_ai_status.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ai_status.py):** AI readiness now treats the Kimi Code OAuth file as the callable credential for `type: kimi_coding` instead of requiring `MOONSHOT_API_KEY`. Shadow/research remains pinned to MiniMax and Ollama remains final local fallback.

**Why:** The previous `kimi_decision` entry targeted the separate Moonshot billing API and could fail with account/balance errors even though the operator's Kimi Code CLI account was valid. The live probe now succeeds against `kimi_coding` without printing or storing tokens in repo config.

## 2026-05-24 — Baseline recovery beta veto disabled

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Corrected the May 22 baseline recovery guardrails after reviewing the operator handoff. `lane_calibration.beta_veto_max_mean` is now `0` and `lane_calibration.per_lane_thresholds.enabled` is now `false`, matching `docs/baselines/2026-05-22_baseline.md` Tier 1 recovery guidance and the known-good `2791e48` config posture.

**Why:** The previous recovery pass left the global beta veto and per-lane threshold veto path active. That could still block high-WR ghost lanes such as BTC 15m neutral/down and BTC 1h bullish/bearish despite paper calibration being in shadow mode. This change disables those veto layers before restart without changing BTC/SOL/XRP strategy thresholds or the BTC-decoupled alt logic.

## 2026-05-24 — Kimi decision provider restored

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Added `kimi_decision` as the first live `ai.provider_chain` entry using the existing OpenAI-compatible adapter, `MOONSHOT_API_KEY`, `https://api.moonshot.ai/v1`, and `model: kimi-k2.6`. Kept `minimax` as the next fallback and `ollama_local` as the final failsafe. Pinned `shadow_pipeline.provider_name` and `research_narrative.provider_name` to `minimax` so lower-stakes shadow/summary work does not consume the Kimi decision provider by default.

**Why:** The Moonshot key was still present, but Kimi was not in the active provider chain, so logs could only show MiniMax/Ollama. Kimi's official API is OpenAI-compatible at the Moonshot base URL, so no custom provider code is needed.

## 2026-05-24 — May 22 baseline recovery guardrails

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Restored paper calibration to baseline-style shadow mode (`shadow_mode: true`, `paper_shadow_mode: true`) while keeping live calibration available via `live_shadow_mode: false`. Re-enabled the beta/per-lane veto guardrails (`beta_veto_max_mean: 0.4`, `per_lane_thresholds.enabled: true`) with threshold auto-recompute disabled so the veto set does not silently churn during a session.

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Reverted broad macro loosening back toward the May 22 baseline posture for spike/lag and 5m BTC-catalyst requirements, and restored HYPE oracle basis caps to the previous tighter values. Operational-only improvements from the Claude pass remain in place: provider logging/timeouts, MiniMax cooldown handling, Chainlink result caching, and ETH momentum shadow logging. The unverified Kimi OAuth provider and disabled directional-breaker experiment were removed from the worktree cleanup.

**[`src/analysis/lane_calibration.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_calibration.py), [`tests/test_lane_calibration.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_lane_calibration.py):** Rejected the asymmetric alpha-clamp experiment and returned to the May 22 baseline calibration semantics: sub-1 alpha can shrink down to the symmetric low clamp instead of being forced back to identity.

**Why:** The May 22 baseline note identified symmetric alpha shrinkage and BTC-dominated paper shadow behavior as the reliable reference, while the post-baseline asymmetric clamp was explicitly marked as the bug that allowed later bleed. This recovery keeps useful observability/runtime work but restores the strategy guardrails needed to compare future sessions against the baseline cleanly.

## 2026-05-21 — Ghost historical metadata reconstruction closed

**[`tools/reconstruct_ghost_metadata.py`](/Users/mainfolder/Documents/psb-main%201/tools/reconstruct_ghost_metadata.py):** Added an operator reconstruction pass for settled ghost rows written before BTC 1H regime and convergence telemetry existed. The tool extends BTC 15m OHLCV through the settled ghost window, resamples completed 1H candles without lookahead, classifies `btc_1h_regime`, reconstructs `convergence_score` from copied probe/edge metadata when available, falls back to explicit reason-prior scores when old rows lack probes, writes a timestamped backup, and emits a JSON coverage report.

**[`data/calibration/rejected_candidates_settled.jsonl`](/Users/mainfolder/Documents/psb-main%201/data/calibration/rejected_candidates_settled.jsonl):** Rewritten in place from `122,878` settled ghost rows. Post-run coverage is `0` missing `btc_1h_regime` and `0` missing `convergence_score`; BTC regime counts are `BULL=71,417`, `RANGE=29,211`, `BEAR=22,250`, all sourced from `ohlcv_15m_resample`. Convergence sources are `probe=62,534`, `probe_edge=3,255`, `edge=206`, and `reason_prior=56,883`.

**[`tests/test_reconstruct_ghost_metadata.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_reconstruct_ghost_metadata.py):** Added regression coverage for no-lookahead BTC 1H lookup, probe/edge convergence reconstruction, and fallback behavior guaranteeing rows do not remain unlabeled.

**[`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`tests/test_market_regime_gate.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_market_regime_gate.py):** Added the live market-regime deadzone execution gate. When `trading.market_regime_gate.enabled` is true, active/signal regimes pass, but `combined_regime=deadzone*` blocks entries whose `convergence_score` is missing or below `deadzone_min_convergence` (`0.55` default). Allowed entries and blocked skips both carry regime metadata into journal extras through the existing lane metadata path.

**[`src/analysis/updown_composite_score.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/updown_composite_score.py), [`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), [`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py):** Priority-2 regime/composite loop is now wired at entry. Composite scoring accepts `action` + `btc_1h_regime`, adds `btc_1h_regime_alignment`, and blocks weak same-direction regime-chase entries (`BUY_YES` in `BULL`, `BUY_NO` in `BEAR`) when convergence is below `updown_composite.regime_action_min_convergence` (`0.55` default). Strong-convergence entries are allowed through.

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), [`src/execution/updown_exit_shared.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/updown_exit_shared.py), [`src/execution/live_testing.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/live_testing.py):** Priority-3 stop-loss loop is active in config. `dynamic_stop_enabled: true` now explicitly applies BTC 1H regime, entry volatility, and convergence multipliers to the adverse up/down percentage stop: `BULL=0.95x`, `RANGE=1.05x`, `BEAR=1.15x`, high volatility `1.15x`, low convergence `1.10x`, high convergence `0.95x`. Live exits already consume `entry_signal.btc_1h_regime`, `entry_signal.entry_volatility`, and `entry_signal.convergence_score`; tests now assert the dynamic policy is parsed and applied.

**Why:** The previous ghost-mode work only made future rows and report joins better; it did not close the historical settled ledger. This pass closes the actual analysis loop so `ghost_gate_report.py` can segment the current settled ghost population by BTC regime and convergence instead of reporting `unknown`.

## 2026-05-21 — Ghost regime enrichment completed

**[`src/analysis/ghost_calibration.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ghost_calibration.py):** Added timestamp-based market-regime enrichment for newly settled rejected candidates. Settled ghost rows now carry `price_regime`, `polymarket_regime`, `combined_regime`, `regime_ts`, `regime_match_age_sec`, and `regime_source` when a nearby `market_regime.jsonl` snapshot exists. Added a reusable backfill function for existing settled ghost logs.

**[`tools/backfill_ghost_regimes.py`](/Users/mainfolder/Documents/psb-main%201/tools/backfill_ghost_regimes.py), [`tools/ghost_gate_report.py`](/Users/mainfolder/Documents/psb-main%201/tools/ghost_gate_report.py), [`tools/settle_rejected_candidates.py`](/Users/mainfolder/Documents/psb-main%201/tools/settle_rejected_candidates.py):** Added an operator backfill command, report-time regime enrichment, regime filters, regime/gate aggregation, and deadzone-specific report sections. The standalone settlement CLI now stamps the same regime labels as the runtime settlement path.

**[`tools/enhanced_price_tracker.py`](/Users/mainfolder/Documents/psb-main%201/tools/enhanced_price_tracker.py), [`tools/ccxt_price_tracker.py`](/Users/mainfolder/Documents/psb-main%201/tools/ccxt_price_tracker.py):** Removed hardcoded repo paths and corrected tracker wording so regime snapshots are clearly produced by trackers and joined by settlement/report tooling.

**[`tests/test_ghost_calibration.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ghost_calibration.py), [`tests/test_ghost_gate_report.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ghost_gate_report.py):** Added regression coverage for settle-time enrichment, backfill behavior, report-time enrichment, and regime/deadzone report buckets.

**Why:** Regime snapshots were being written but never consumed by the live ghost settlement path or ghost analysis report. This completes the missing join so ghost outcomes can be segmented by market regime instead of remaining analytically blind to deadzone/signal conditions.

## 2026-05-21 — Ghost BTC-regime/convergence metadata + dynamic updown stops

**[`src/analysis/rejected_candidate_log.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/rejected_candidate_log.py), [`src/analysis/updown_composite_score.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/updown_composite_score.py):** Rejected ghosts now compute a margin-aware `convergence_score` from baseline probe variants plus edge quality, and composite-scored live up/down candidates now expose a separate convergence/consensus score instead of only a flat composite pass/fail.

**[`src/execution/updown_exit_shared.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/updown_exit_shared.py), [`src/execution/live_testing.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/live_testing.py), [`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`src/execution/clob_client.py`](/Users/mainfolder/Documents/psb-main%201/src/execution/clob_client.py):** Up/down percentage stops now support entry-aware dynamic widening/tightening based on stored `btc_1h_regime`, entry volatility, and convergence score. Fresh positions and journal-resumed positions now both carry enough entry metadata for the shared exit helper to make the same adjustment.

**[`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), [`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), [`tools/backfill_ghost_regimes.py`](/Users/mainfolder/Documents/psb-main%201/tools/backfill_ghost_regimes.py), [`tools/ghost_gate_report.py`](/Users/mainfolder/Documents/psb-main%201/tools/ghost_gate_report.py):** New entries and rejected ghosts now persist `btc_1h_regime`, `convergence_score`, and entry-volatility metadata where available; the ghost report adds BTC-1H and convergence bucket slices; and the backfill tool now copies historical rejected-candidate metadata into settled ghosts when the source rejected row still exists.

**Why:** Market-regime segmentation alone did not solve the narrower audit gap around BTC 1H tape state, weak-vs-strong gate agreement, and stop-loss damage. This pass makes those signals visible in ghost data and usable in live exit behavior without introducing a separate modeling stack.

## 2026-05-21 — Local bot crash forensics + supervised restart

**[`start.py`](/Users/mainfolder/Documents/psb-main%201/start.py):** Reworked the local launcher into a small parent supervisor. It now spawns the actual bot as a child process, forwards `Ctrl+C` cleanly, and automatically restarts the child after any unclean exit instead of leaving the overnight session dead.

**[`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py):** Added a persistent runtime status breadcrumb at `data/runtime/bot_runtime_status.json` and armed Python `faulthandler` to append fatal-signal stack dumps to `data/runtime/polybot_fault.log`. The main loop now records coarse phases such as `scanner_sync`, `strategy_scans_running`, `resolution_and_calibration`, and `shutdown_complete`, so a hard mid-cycle death leaves a concrete last-known phase even when no Python exception is logged.

**[`docs/LOCAL_BOT_RUN.md`](/Users/mainfolder/Documents/psb-main%201/docs/LOCAL_BOT_RUN.md):** Documented the supervised local start path and the new runtime artifacts used for overnight crash triage.

**Why:** Recent local overnight failures ended mid-cycle with no traceback, no shutdown banner, and stale `ops_pulse` state. That pattern is consistent with a hard process death outside normal Python exception handling. This ship does not assume the process will stay alive; it preserves better evidence on the next crash and auto-recovers the local bot instead of leaving it silently down.

## 2026-05-21 — Phase-1 calibration ship: DOGE/BNB/HYPE catalyst loosens + BTC disagreement override

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Shipped the first agreed calibration pass from the 2026-05-21 audit. `doge_macro.require_btc_catalyst_5m` and `bnb_macro.require_btc_catalyst_5m` now flip to `false`, and `hype_macro.require_btc_catalyst_15m_when_unconfirmed` now flips to `false`. This stays narrower than a blanket “all alts off BTC” change: only the lanes with already-profitable settled-reject cohorts were loosened.

**[`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py):** Added a BTC-only, config-driven override for HTF-aligned `bias_quant_disagree` rejects on non-5m up/down lanes. The post-quant side flip was already symmetric in current code; the remaining starvation was happening later at the `lane_min_edge_bias_quant_disagree` reject path, so this pass softens that exact gate for moderate disagreement on 15m/1h while keeping 5m strict.

**[`tests/test_bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_bitcoin.py):** Added focused coverage proving the new BTC disagreement override admits a moderate bullish 15m-style gap and still refuses to relax the 5m lane.

**Why:** Settled reject analysis supported immediate removal of BTC catalyst blockers on `doge 5m`, `bnb 5m`, and `hype 15m`, while BTC’s LONG starvation no longer matched the old “missing mirror flip” hypothesis. The smallest defensible code change was to keep the existing flip logic intact and make the later min-edge disagreement rejection configurable for the slower BTC lanes.

## 2026-05-20 — Probability diagnostics stack added, deferred repo queue logged

**[`scripts/probability_diagnostics.py`](/Users/mainfolder/Documents/psb-main%201/scripts/probability_diagnostics.py):** Added a new offline diagnostics report over resolved paper exits that computes the current probability-evaluation stack in one place: reliability buckets, Murphy decomposition (`reliability`, `resolution`, `uncertainty`), Brier score against constant / market / empirical-table baselines, lane-level take-rate, and a small SVG reliability chart artifact. The empirical baseline is leave-one-out on `(strategy, window, yes_price_bucket)` with fallbacks to `(strategy, window)`, then `window`, then global so it stays useful on thin cohorts without cheating on the evaluated trade itself.

**[`src/analysis/time_aware_split.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/time_aware_split.py):** Added a dependency-free chronological split helper with a backward purge window. This is intentionally narrow: it does not introduce a full training stack, it just gives future supervised calibration/modeling code a leakage-aware default instead of ad hoc random splits.

**[`docs/CALIBRATION_TOOLING_QUEUE.md`](/Users/mainfolder/Documents/psb-main%201/docs/CALIBRATION_TOOLING_QUEUE.md):** Logged the agreed queue in-repo. Items 1-4 are marked implemented now; pooled isotonic and selection-bias extensions are queued to revisit once the new diagnostics are in regular use; meta-labeling, `river`, Optuna, and endpoint-forecasting stacks are explicitly marked as review-soon but not added yet.

**[`tests/test_probability_diagnostics.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_probability_diagnostics.py), [`tests/test_time_aware_split.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_time_aware_split.py):** Added regression coverage for the new report outputs, artifact writing, and time-aware fold behavior.

**Why:** The current repo already had lane calibration bookkeeping and lightweight journal summaries, but it did not have one canonical offline report for “does the probability model beat constant, market, or an empirical conditioned baseline?” nor a standard leakage-aware split helper for the next supervised step. This pass adds the measurement layer first and parks the higher-complexity repo ideas in a visible queue instead of in chat history.

## 2026-05-20 — HYPE/XRP ghost-backed admission loosens

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Loosened the most supported HYPE/XRP admission bottlenecks from the settled ghost ledger. `hype_macro.min_liquidity` was reduced `500 -> 300`; HYPE 15m up lane `entry_policy.window_side_overrides.15m.up.min_edge` moved `0.09 -> 0.08`; and HYPE 15m admission window widened `entry_window_15m_max 32 -> 36` (with matching 15m policy window max updates). On XRP, `xrp_macro.min_liquidity` moved `1000 -> 750`, and `xrp_macro.oracle_max_basis_bps` moved `10 -> 15`.

**Why:** The current settled ghost report showed the clearest missed-EV on `hype_macro|1h|BUY_YES|liquidity`, `hype_macro|15m|BUY_YES|lane_entry_window`, `hype_macro|15m|BUY_YES|lane_min_edge`, `xrp_macro|1h|BUY_YES|liquidity`, and `xrp_macro|15m|BUY_YES|oracle_basis_block`. The goal of this pass is straightforward: increase live trade throughput and collect more decision data, while keeping the loosens limited to the gate families with the strongest settled support.

**Expected outcome:** More HYPE 1h / 15m BUY_YES admissions, fewer XRP rejects on fresh basis overshoots and thinner books, and a higher overnight paper trade count without a broad removal of guardrails.

**Follow-up:** `sol_macro` received the same style of throughput-focused loosen after a follow-up settled ghost review showed `sol_macro|1h|BUY_YES|liquidity` and `sol_macro|15m|BUY_YES|lane_entry_window` as the cleanest missed-EV families. `sol_macro.min_liquidity` moved `1000 -> 750`, and SOL 15m admission widened `entry_window_15m_max 28 -> 32` at both the top-level lane config and the 15m policy override.

**1h starvation pass:** Investigated `doge_macro`, `eth_macro`, `xrp_macro`, `sol_macro`, and `bnb_macro` hourly starvation from recent reject logs plus settled ghost gates. The main fixes were lane-specific 1h admission loosens rather than a single global toggle: every affected asset now admits the full 1h candle lifecycle in policy (`entry_window_min/max: 0.0 -> 60.0`), `eth_macro` now has `eth_follow_1h_min_adj: 0.02` and `oracle_basis_relax_max_bps: 15.0`, `xrp_macro` now allows `1h` long entries up to `entry_price_max: 0.58` and uses `oracle_basis_relax_max_bps: 15.0`, `sol_macro` now uses `oracle_basis_relax_max_bps: 15.0` with a softer shared `iql_15m_hist_floor: 0.06`, and bootstrap `doge_macro` / `bnb_macro` both moved `min_liquidity 1000 -> 750` plus `oracle_basis_relax_max_bps: 15.0`.

## 2026-05-20 — Commands tab card reorder + custom-fullscreen header sizing

**[`src/dashboard/index.html`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html):** (1) Moved the **Configuration** panel and **API Usage** card from the top of the Commands tab to the bottom (after the Shutdown card) so Start commands / Backtest / Kill switch are the first thing visible when opening the tab. Done via in-place HTML reorder, no JS or ID changes. (2) Custom-fullscreen (`html.dashboard-fullscreen`) overrides for `.cmd-pnl-title` ("Live P&L Trace") and the new `h3.active-positions-title` ("Active Positions") — both pinned to 18px so they no longer balloon to the 30px global fullscreen h3 ramp. Positions-count badge inside the h3 also dropped to 0.65rem to match. Half-screen and native browser fullscreen are unaffected.

**Why:** User feedback — Config + API Usage at the top of the Commands tab pushed the actually-used CLI snippets below the fold; and the two card headers looked oversized vs. the rest of the card body once the Enter Fullscreen button was used.

**Restart:** Not required (dashboard CSS/markup only). Hard-reload the browser.

---

## 2026-05-20 — BTC post-quant counter-trend flip + dashboard PnL/Exit-Timing gradient parity + Start Live brightness

**[`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py):** New helper `_maybe_quant_flip` (placed adjacent to `_calibrate_est_prob`) that flips the picked action when the raw quant probability clearly contradicts the HTF-picked side: `BUY_YES` → `BUY_NO` when `raw_est_prob < quant_disagree_flip_thresh` (default 0.48), and `BUY_NO` → `BUY_YES` when `raw_est_prob > 1 - thresh` (default 0.52). Respects `disable_buy_no_counter_trend` and `disable_buy_yes` flags. Wired in at both updown evaluation sites — the 5m path right after `raw_est_prob = quant.est_prob_up`, and the 15m/1h path right after `raw_est_prob = est_prob_up` is clamped — both before `_calibrate_est_prob` runs so calibration sees the corrected action. `side_source` becomes `btc_quant_disagree_flip` and `reason_parts` records `quant_flip=raw(X.XXX)<0.48` (or `>0.52`).

**Why:** Post-9fe48a1 restart still showed BTC zero-trade: every reject was tagged `lane_min_edge_bias_quant_disagree` with `htf_bias=BULLISH`, `side_source=btc_htf_bias`, `raw_est_prob` 0.42–0.47, edge −0.015 to −0.075 on LONG/BUY_YES. The pre-quant counter-trend gate at lines 1335-1339 only consults the 4H MACD histogram, never the quant probability — so when the histogram was still rising but the quant said down, the strategy locked into BUY_YES and let the edge gate kill the trade. New flip closes that gap by letting the quant veto a clearly losing side instead of forcing rejection.

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** New knob `strategies.bitcoin.quant_disagree_flip_thresh: 0.48` so the flip threshold is tunable without code edits. Comment block above it captures the rationale and the observed 0.42–0.47 reject band that motivated 0.48.

**[`src/dashboard/index.html`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html) (PnL Trace + Exit Timing cards):** Brought both cards onto the same gradient/glow recipe as the `.card` cascade from a45269f — replaced their bespoke 4-stop gradients with the canonical `linear-gradient(155deg,rgba(255,68,102,.06) 0%,rgba(0,0,0,.62) 28%,rgba(176,96,255,.08) 62%,rgba(0,255,136,.05) 100%)`, swapped the single inset shadow for the 5-layer outer-glow stack (black/purple/cyan/green + inset hairline), and matched the scanline overlay (`opacity:.32`, `border-radius:inherit`). They now visually belong to the same fleet as the BTC Live Chart and crypto cards.

**[`src/dashboard/index.html`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html) (PnL Trace header):** Collapsed the three-line header (kicker `Paper Session Curve` + title + long sub paragraph) to a single line containing only the title `Live P&L Trace`. Removed the kicker element and the sub paragraph from markup, set `.cmd-pnl-sub{display:none}` as a guard, tightened `.cmd-pnl-head` paddings, set the title to a fixed 15px (no clamp) so font size stops scaling between viewport sizes. Card width unchanged — only the header was the operator complaint.

**[`src/dashboard/index.html`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html) (Start Live Test button):** `#live-start-btn:not(:disabled)` overrides the shared `.ag` `rgba(0,255,136,.1)` fill with `rgba(0,255,136,.28)` and bumps the text to `#c8ffde` so the button reads at the same visual weight as the red Stop Trading / yellow Loss Kill / blue Dead Zones / cyan New Round siblings (green at the same alpha was perceptually dimmer than the warm colors). No pulse, no glow — just brightness parity.

**Why (dashboard):** Operator pass on the Command Center after the gradient cascade work: PnL and Exit Timing cards were the two remaining surfaces that hadn't picked up the cascade; the PnL header was visually heavy at fullscreen; the Start Live Test button looked dim next to its siblings. Single follow-up polish pass.

## 2026-05-20 — Dashboard HUD gradient applied to every card + Command Center PnL y-axis fix

**[`src/dashboard/index.html`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html) (cards):** Base `.g,.card` background swapped from flat `rgba(0,0,0,.62)` to the same 4-stop 155° diagonal gradient already used by `.bt-hud` and `.bt-shell` — pink bleed (TL) → near-black vignette stop at 28% → purple shoulder at 62% → green bleed (BR). Added scanline overlay via `::before` (`repeating-linear-gradient`, `mix-blend-mode:multiply`, opacity .32) and lifted card children to `position:relative;z-index:1` so content paints above the overlay. Layered colored outer glows (purple/cyan/green) + inset hairline. Added suppression rule `.card.bt-shell::before, .card.bt-hud::before { display:none }` so cards that already carry a HUD scanline don't double-overlay. Cascades automatically to crypto cards, BTC Live Chart (`#btc-chart-section`), Command Center, Performance / Journal / Backtest tab cards.

**[`src/dashboard/index.html`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html) (Command Center PnL chart):** Replaced `downRoom = max(|minSeen|, baseline, 25)` / `upRoom = max(|maxSeen|, baseline*0.35, 25)` with bounds derived from observed PnL magnitude only (`max(|seen|*1.2, observedMag*1.2, 25)`). The old formula forced the y-axis span to scale with starting equity (e.g. ±$10k for a $10k baseline), so small live-PnL swings looked like a flat ruler. Now a –$15 dip scales against an axis spanning ~±$30, making movement visible immediately.

**Why:** Operator flagged that the Command Center PnL line "looked straight" at –$15 and asked whether the chart can render negative — root cause was axis scaling, not draw logic. Same session: operator wanted the gradient/scanline treatment from the backtest STANDBY HUD propagated across every card surface. Both ship together as a single dashboard polish pass.

## 2026-05-20 — BTC 71h zero-trade fix: HTF boost bump + counter-trend BUY_NO re-enabled + bias/quant disagree logging

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Re-enabled counter-trend BUY_NO under BULLISH bias by flipping `disable_buy_no_counter_trend: true` → `false`. Added four new HTF boost knobs (`btc_htf_boost_strong_1h: 0.14`, `btc_htf_boost_weak_1h: 0.07`, `btc_htf_boost_strong_15m: 0.12`, `btc_htf_boost_weak_15m: 0.06`) — ~50% bump over the old hardcoded 0.09/0.04 (1h) and 0.08/0.03 (15m) values so `raw_est_prob` under a clean 3/3 BULLISH setup reaches ~0.64 instead of ~0.59 (median yes_price is 0.505, mean 0.554). 5m boost intentionally unchanged.

**[`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py):** Updown 15m/1h path at the est_prob_up construction site now reads the boost values from config with the new defaults. At the `lane_min_edge` rejection site, split the reject reason into `lane_min_edge` vs `lane_min_edge_bias_quant_disagree` and added a `bias_quant_disagree` boolean to the rejection context, so we can isolate the cohort where the htf_bias classifier and the quant model disagree directionally (raw_est on opposite side of yes_price from bias).

**[`CLAUDE.md`](/Users/mainfolder/Documents/psb-main%201/CLAUDE.md):** New file codifying the current project phase as calibration / data gathering. Priorities: increase trade frequency, find each asset's lane sweet spot from observed data, never tighten gates or raise min_edge. Includes diagnostic checklist for starved lanes and notes on per-asset macro overrides.

**Why:** Last filled BTC trade in `trades.jsonl` was 2026-05-17 09:49 UTC — ~71 hours of zero entries despite the bot actively evaluating 515 BTC markets today. 501 of those 515 were rejected by `lane_min_edge`. Distribution analysis (`raw_est_prob` vs `yes_price` across the 480 BUY_YES under BULLISH bias) showed median raw_est=0.480 vs yes_price=0.505, mean 0.486 vs 0.554 — the quant model was producing probabilities ~7 pp below market under exactly the regime the classifier wanted to be long. Calibration was confirmed to be a no-op (raw == calibrated, mean shift 0.000), so the gap is in the HTF boost weights and the side-selection lock to BULLISH-only-LONG. This patch addresses both: (a) re-enables the existing `btc_bull_rollover` BUY_NO path that was config-disabled in early May, (b) bumps HTF boost so BULLISH conviction actually produces edge against market, (c) tags disagreement events so we can measure whether the bias or the quant is closer to truth on next post-restart pass. Running bot needs restart to pick up the new code paths.

## 2026-05-19 — ETH up/down oracle validator restored; Gamma scanner sockets bounded

**[`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py):** The live ETH up/down scan path now uses the shared `_validate_updown_oracle(...)` result instead of the older `_oracle_basis_blocks_entry(...)` basis-only shortcut. That restores enforcement of `require_oracle_for_updown`, `oracle_max_age_sec`, and any configured fresh/stale basis relax policy in the actual ETH runtime lane, and it logs oracle freshness/basis details under the true reject reason (`oracle_basis_block`, `oracle_missing`, `oracle_stale`, etc.).

**[`src/market/scanner.py`](/Users/mainfolder/Documents/psb-main%201/src/market/scanner.py):** Gamma `requests` sessions now mount their adapter with `pool_block=True` so slug-fetch worker threads stay within the configured connection pool instead of opening extra sockets under bursty parallel fetch pressure.

**[`tests/test_eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_eth_macro.py):** Added regression coverage proving an ETH candidate with `oracle_basis_bps=11.0` passes live scan when `oracle_basis_relax_max_bps=12.0`, rather than being incorrectly rejected by the stale basis-only runtime path.

**Why:** Tonight’s ghost/reject stream showed ETH `oracle_basis_block` dominating fresh rejects, while code inspection showed the ETH scan path had drifted from the shared oracle validator already used by the SOL-derived lanes. The same review pass also found live `scanner.gamma` warnings with `OSError(24, 'Too many open files')`, so the Gamma adapter was hardened before the overnight run.

## 2026-05-19 — Command Center live equity chart + trading-halt controls cleanup

**[`src/dashboard/index.html`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html):** Added a dedicated Command Center live equity/PnL chart above Active Positions, then iterated its renderer to use live status data, session-baseline seeding, sign-based green/yellow/red coloring, and an anchored zero/baseline treatment that still shows drawdown from the starting bankroll after mid-session refreshes. Simplified the card to graph-only, reduced idle glow to match the backtest HUD family, and flattened the remaining live-tab glow on Operations Pipeline, Exit Timing HUD, and Macro Alignment.

**[`src/dashboard/server.py`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py):** Changed live Command Center status payloads to expose mark-to-market equity for the hero bankroll/equity field instead of cash-only bankroll, so the hero number stays consistent with session total PnL and the new live equity chart.

**Live control semantics:** Reworked dashboard stop behavior so `Stop Trading` arms the manual `data/KILL_SWITCH` halt without killing the running bot subprocess, and added `POST /api/live/resume` so the same button can flip to `Resume Trading`. The dashboard loss-kill-switch and dead-zone buttons remain wired to config toggles rather than overlapping with the manual stop.

**Why:** The Command Center previously had no live curve, and initial attempts mixed past-session journal chart behavior with current-session telemetry, then compared cash bankroll against mark-to-market PnL, which made the live numbers look internally inconsistent. The follow-up pass made the live tab read from one coherent MTM source and aligned the visual treatment with the calmer backtest status example card.

## 2026-05-19 — Dashboard live controls + DOGE/BNB scope hardening

**[`src/dashboard/index.html`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html):** Replaced the stale live-tab `Weather 72h Cap` control/state with the actual `exposure.loss_kill_switch_enabled` control/state beside `Dead Zones`, extended dead-zone toggles to `doge_macro` and `bnb_macro`, expanded the fallback Performance scope to include DOGE/BNB, and standardized XRP’s live palette from yellow to `#38bdf8` so it no longer collides visually with BNB gold.

**[`src/dashboard/server.py`](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py):** Added `doge_macro` and `bnb_macro` to the authoritative backtest/live-scope config list and to config-patch validation, then bumped `dashboard_ui_rev` so stale browser bundles do not keep resurrecting the older dashboard state.

**Why:** The dashboard had drifted into a mixed state where newer DOGE/BNB UI paths existed, but core config/live-scope plumbing still assumed the older asset set and the command-center strip still rendered a legacy weather cap control. This patch makes the backend the source of truth for the seven-asset dashboard and removes one of the recurring stale-UI indicators from the live tab.

## 2026-05-19 — XRP fresh oracle-basis relax aligned with SOL

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Added `xrp_macro.oracle_basis_relax_max_bps: 12.0` so fresh XRP Chainlink-vs-spot basis overshoots can pass when they are only slightly above the `10.0` bps hard cap, matching the existing SOL behavior.

**Why:** In active paper session `test_20260519_201151`, XRP was scanning but not firing because the journal was dominated by `oracle_basis_block` rows with fresh basis values clustered just above the configured cap. SOL already had a small fresh-basis relax path; XRP did not, so this patch closes that config drift without broadening stale-oracle tolerance.

## 2026-05-19 — Flat-BTC gate narrowed to neutral-alt fallback for shared alt lanes

**[`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py):** Added a shared `flat_btc_only_blocks_when_alt_neutral` path so `flat_btc_no_lag` only hard-blocks when the alt lacks a usable native `1h` bias. When enabled, any directional alt `1h` trend bypasses the flat-BTC hard skip instead of requiring exact action alignment.

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Enabled the broader neutral-only BTC gate behavior for `sol_macro`, `hype_macro`, `xrp_macro`, `doge_macro`, and `bnb_macro`. Left `eth_macro.flat_btc_only_blocks_when_alt_neutral: false` with an explicit watch note because ETH is currently trading and was not approved for behavior change in this pass.

**[`tests/test_sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_sol_macro.py):** Added focused coverage for the new shared bypass helper in both the new neutral-only mode and the legacy alignment-only mode.

**Why:** The repo had already moved non-BTC lanes to alt-first direction selection, but several shared-lane configs still let flat BTC behave like a hard admission blocker. This change keeps BTC as secondary context for DOGE/BNB/XRP and the other shared alt lanes while preserving ETH’s stricter behavior pending separate review.

---

## 2026-05-19 — Dashboard bt-shell panels + DOGE hourly slug + macro/exit HUD handoff

**[`src/dashboard/index.html`](src/dashboard/index.html):** Added shared `.bt-shell` chrome for Operations Pipeline, Backtest Controls, Test Results, and Scan Diagnostics; toned Macro Alignment section to match; improved BTC chart trade bubbles (`_btcTradeOverlayStyle`, DOGE/BNB overlay lanes, CSS-variable dot fill/border + glow).

**[`src/market/scanner.py`](src/market/scanner.py), [`tests/test_scanner_crypto_enhancements.py`](tests/test_scanner_crypto_enhancements.py):** Hourly Up/Down discovery uses live Gamma prefix `dogecoin-` (not short-window `doge-`).

**[`docs/HANDOFF_2026-05-19_MACRO_ALIGN_AND_EXIT_HUD.md`](docs/HANDOFF_2026-05-19_MACRO_ALIGN_AND_EXIT_HUD.md):** Handoff for Macro Align chart + Exit Timing HUD work (design bundle, backend `/api/macro_align/series`, disputes).

**Why:** Unify live/backtest panel styling with the backtest HUD; fix DOGE `1h` ghost intake; document in-flight macro/exit HUD session for the next agent.

## 2026-05-19 — DOGE/BNB oracle feeds wired; BNB shorts can clear thinner books

**[`src/analysis/sol_btc_service.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py):** Added official Chainlink `DOGE/USD` and `BNB/USD` feed mappings on Arbitrum to the shared alt-macro oracle map, plus CoinGecko fallback IDs for `DOGEUSDT` and `BNBUSDT` so non-Binance price fallback behavior stays consistent with the other alt lanes.

**[`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Added `oracle_stale_basis_relax_max_bps: 75.0` to both `doge_macro` and `bnb_macro` because these new Chainlink feeds are Arbitrum-only and update much more slowly than the repo’s default `oracle_max_age_sec: 180`. Added `bnb_macro.min_liquidity_buy_no: 300` so BNB short-side candidates can trade on thinner books without loosening the long-side floor.

**[`tests/test_sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_sol_macro.py):** Extended the oracle-map regression to cover `DOGEUSDT` and `BNBUSDT`.

**Why:** On **May 19, 2026**, DOGE up/down markets started firing live, but `doge_macro` was hard-blocked by `oracle_missing` because the shared oracle feed map had no DOGE entry. BNB showed the same structural oracle gap plus heavy `liquidity` rejects on short candidates even while settled ghosts indicated the short thesis was directionally correct.

## 2026-05-19 — DOGE hourly scanner slug fixed for ghost calibration intake

**[`src/market/scanner.py`](/Users/mainfolder/Documents/psb-main%201/src/market/scanner.py):** Fixed the hourly crypto Up/Down asset slug map so DOGE uses the live Gamma hourly prefix `dogecoin-up-or-down-...` instead of the short-window-only `doge-up-or-down-...`. `5m` and `15m` discovery already worked; the regression only affected DOGE `1h` discovery, which meant hourly ghost/rejected-candidate intake never happened for DOGE.

**[`tests/test_scanner_crypto_enhancements.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_scanner_crypto_enhancements.py):** Updated hourly slug regression coverage to assert the `dogecoin-` hourly shape and reject the stale `doge-` variant.

**Verification:** `.venv/bin/python -m pytest tests/test_scanner_crypto_enhancements.py -q` passed, and a live Gamma probe on **May 19, 2026** confirmed hourly `dogecoin-up-or-down-*` and `bnb-up-or-down-*` markets are both discovered by the scanner.

## 2026-05-19 — Dashboard: BTC chart bubbles share chip color + CSS glow

**[`src/dashboard/index.html`](src/dashboard/index.html):** Replaced canvas trade markers with DOM `.bbl-dot` elements. `stratChipHex` / `stratChipHexForJournal` are the single color source for `.strat-cf` toggles and chart bubbles (exact `window.STRATS` hex — no `_shadeStratHex` / white stroke). Bubbles now match the asset chip tone exactly: `color + '18'` translucent fill, `color + '88'` border, no box-shadow glow (lighter, static; chip-driven pulse-sync attempts reverted).

**[`src/dashboard/server.py`](src/dashboard/server.py):** `dashboard_ui_rev` → `2026-05-19-btc-chart-bubbles-dom`.

## 2026-05-18 — Rejected observer retries now have cooldown/backpressure and AI clients close on timeout

**[`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py):** Added per-strategy rejected-observer backpressure so the same structurally rejected market is not re-observed every cycle while still inside a cooldown window, and so only a bounded number of observer tasks can stay inflight at once. This applies to `sol_macro`, `eth_macro`, and inherited `hype_macro` / `xrp_macro`.

**[`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py):** Provider SDK clients are now explicitly closed after OpenAI-compatible, Groq, Anthropic, Gemini, and MiniMax calls, including timeout/error paths.

**[`tests/test_sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_sol_macro.py), [`tests/test_eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_eth_macro.py):** Added regression coverage proving repeated scans do not re-launch the same rejected observer during cooldown.

**Why:** Live May 18 local logs showed repeated `rejected observer timeout ... after 20.0s` warnings across the same alt lanes together with `Trading cycle overran configured interval: elapsed=63.3s interval=60s` and later `OSError(24, 'Too many open files')` on Gamma fetches. The observer path is diagnostic-only, so repeated retries and leaked client sockets were avoidable pressure on the 60s scan loop.

**Verification:** `.venv/bin/python -m pytest tests/test_sol_macro.py -k shadow_observer -q` and `.venv/bin/python -m pytest tests/test_eth_macro.py -k shadow_observer -q` passed.

## 2026-05-18 — Ghost gate report now emits probe-backed relaxation candidates

**[`tools/ghost_gate_report.py`](/Users/mainfolder/Documents/psb-main%201/tools/ghost_gate_report.py):** Extended the settled ghost report with probe-level Wilson confidence intervals, win/loss counts, and a new `actionable_probe_relaxations` section. The new section is intentionally read-only and picks the smallest relax delta per `strategy|window|action|reason|probe` bucket that has enough settled sample, confidence above 50%, and negative net gate value.

**[`tests/test_ghost_gate_report.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ghost_gate_report.py):** Added regression coverage for probe-backed relaxation selection and report output.

**Why:** The runtime `performance_feedback` path only knows how to loosen `min_edge` using `context.edge` versus `context.effective_min_edge`. Large gate families such as BTC histogram rejects and oracle-basis blocks already carry `probe_variants`, but there was no operator report converting those settled probe rows into concrete threshold-relaxation candidates.

**Verification:** `.venv/bin/python -m pytest tests/test_ghost_gate_report.py tests/test_ghost_calibration.py -q` passed.

## 2026-05-18 — Ghost gate report now exposes confidence bounds and actionable sample floors

**[`tools/ghost_gate_report.py`](/Users/mainfolder/Documents/psb-main%201/tools/ghost_gate_report.py):** Added Wilson win-rate confidence intervals to lane and gate summaries plus a new `actionable overtight gates` section that only ranks gates with `n >= 100`, `win_rate_ci_low > 50%`, and negative net gate value. This keeps tiny high-WR buckets from outranking larger, better-supported gate families during operator review.

**[`tests/test_ghost_gate_report.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ghost_gate_report.py):** Added regression coverage for the confidence interval fields and the actionable-overtight filter.

**Verification:** `.venv/bin/python -m pytest tests/test_ghost_gate_report.py tests/test_ghost_calibration.py -q` passed.

## 2026-05-18 — Rejected-candidate observer moved off alt scan hot path

**[`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py):** Rejected-candidate observer calls now run as background tasks instead of being awaited inline during `scan_and_analyze`, so `sol_macro` / `eth_macro` and inherited `xrp_macro` / `hype_macro` no longer spend scan wall time waiting on observer-only AI diagnostics. Observer attempts still consume per-scan budget immediately, and success counters are updated in task callbacks.

**Default timeout change:** The alt observer watchdog default was cut from the old `max(30s, legacy_ai_timeout)` behavior to an `8s` cap by default (with explicit config override still respected), which better fits a `60s` trading loop for non-execution telemetry.

**Why:** Live May 18 logs showed repeated `rejected observer timeout ... after 30.0s` lines across multiple alt strategies together with `Trading cycle overran configured interval: elapsed=92.2s interval=60s`. The observer path is diagnostic-only, so letting it sit in the scan hot path for up to 30 seconds per lane was an avoidable productivity hit.

## 2026-05-18 — Startup narrator contention + AI timeout budget split

**[`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml):** Delayed startup AI narrators by `90s` (`ai.session_summary.startup_delay_seconds`) so the previous-session summary work no longer competes with the first live decision-layer scans immediately after boot.

**[`src/strategies/bitcoin.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py), [`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py), [`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py):** Split the old single `ai_call_timeout_sec` into per-path runtime budgets across every strategy-owned AI watchdog. BTC now distinguishes `ai_analysis_timeout_sec` vs `ai_decision_timeout_sec`; SOL/ETH distinguish `ai_decision_timeout_sec` vs `ai_observer_timeout_sec`. The observer default remains at least `30s`, which matches the fact that it can perform more than one AI stage per attempt.

**Verification:** `.venv/bin/python -m pytest tests/test_eth_macro.py -q` and `.venv/bin/python -m pytest tests/test_sol_macro.py -q` → `115 passed`.

**Why:** On **May 18, 2026 around 02:07:36–02:07:51 PT**, the bot logged `evaluate_trade_decision timeout ... after 15.0s` and `rejected observer timeout ... after 15.0s` while startup narrators were also finishing AI work. The direct decision path, reject observer, and startup narrators were all sharing the same provider budget, but the provider chain itself allows much longer calls (`timeout_seconds: 120` for MiniMax), so the outer `15s` watchdog was too aggressive for observers and noisy during startup contention.

## 2026-05-18 — Decision-layer marginal abstention + bounded non-BTC AI latency

**[`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py):** Fixed the decision-layer bug where marginal setups were passed to AI and then often vetoed simply for being marginal. For `quant_edge < quant_threshold`, direct/shadow `HOLD` and low-confidence outputs now count as AI abstention rather than hard rejection; real disagreement still vetoes.

**[`src/strategies/eth_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py), [`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py):** Added `asyncio.wait_for(...)` timeouts around non-BTC `evaluate_trade_decision(...)` and `observe_rejected_candidate(...)` calls, matching the bounded-latency pattern that BTC already used. Timeout now degrades to an explicit skip reason instead of allowing one LLM call to stall a whole cycle.

**[`tests/test_ai_agent_parse.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ai_agent_parse.py):** Added regression coverage for marginal abstention and preserved non-marginal rejection semantics. Verification run: `.venv/bin/python -m pytest tests/test_ai_agent_parse.py tests/test_eth_macro.py tests/test_sol_macro.py -q` → `137 passed`.

**Why:** Live May 18 logs showed two separate problems: `ai_decision_direct_ai_hold` on marginal candidates, and 220–300 second cycle overruns with repeated sequential Minimax calls inside ETH/SOL/HYPE/XRP paths. These changes address both without changing the structural non-AI gates.

## 2026-05-18 — Decision layer no longer tautologically vetoes marginal trades

**[`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py):** Changed `evaluate_trade_decision(...)` so marginal candidates (`quant_edge < quant_threshold`) treat direct/shadow `HOLD` and low-confidence outputs as **AI abstention**, not hard rejection. Strong disagreement still vetoes: opposite action and non-positive AI edge remain blocking.

**[`tests/test_ai_agent_parse.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ai_agent_parse.py):** Added regression coverage for marginal `HOLD` / low-confidence abstention while keeping non-marginal `HOLD` / low-confidence rejection intact.

**Why:** The decision layer was being invoked exactly on sub-threshold marginal setups, and the model was often vetoing them for being sub-threshold or uncertain. In practice that made the “AI tiebreaker” behave like a tautological rejector and cut trade count beyond the intended marginal confirmation role.

## 2026-05-18 — Minimax portfolio summary alias fallback

**[`src/analysis/ai_agent.py`](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py):** Hardened portfolio-stage JSON parsing so the shadow pipeline accepts `summary`, `rationale`, or `reasoning` when a provider omits the exact `executive_summary` key but still returns usable summary text.

**[`tests/test_ai_agent_parse.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_ai_agent_parse.py):** Added regression coverage for the `summary` alias so provider-specific schema drift does not silently re-break the parser.

**Why:** Minimax returned HTTP `200 OK` but the portfolio parser rejected market `2283169` because the payload lacked the exact `executive_summary` field. This preserves strict failure for truly empty summaries while tolerating common key-name drift from Anthropic-compatible providers.

## 2026-05-18 — Ghost overtight feedback loop for min-edge loosening

**`src/execution/performance_feedback.py`:** Added a second runtime feedback path, `check_overtight(...)`, which reads settled ghost rejects from `data/calibration/rejected_candidates_settled.jsonl`, identifies min-edge lanes with strong blocked ghost win rates, and emits per-lane loosening recommendations into `config["_runtime_feedback"]["by_lane"]`. Recommendations are based on the actual shortfall needed for a ghost candidate to clear `effective_min_edge`, not only the small fixed probe deltas.

**`src/strategies/bitcoin.py`, `src/strategies/eth_macro.py`, `src/strategies/sol_macro.py`:** Entry gating now multiplies `effective_min_edge` by the new lane-level loosening multiplier in addition to the existing strategy-level drift multiplier, so over-tight lanes can relax automatically at runtime without editing YAML.

**`config/settings.yaml`:** Documented the overtight knobs under `performance_feedback`, including lane sample thresholds, ghost WR threshold, max relax delta, and the floor/ceiling for the loosening multiplier.

**`tests/test_performance_feedback.py`:** Added coverage for lane-level multiplier lookup and for overtight feedback population when drift expectations are empty.

**Why:** Settled ghost data already recorded blocked winners, but there was no code path converting that evidence into a runtime entry-bar adjustment. This closes the missing “loosening” half of the feedback loop.

## 2026-05-18 — AI shadow observer for structurally rejected candidates

**`src/analysis/ai_agent.py`:** Added an observation-only rejected-candidate AI path plus `data/logs/ai_pipeline/rejected_candidate_observer.jsonl`. When enabled, selected pre-gate rejects can still emit direct marginal logs and run the Tier C shadow pipeline without affecting execution decisions.

**`src/strategies/sol_macro.py`, `src/strategies/eth_macro.py`:** Wired the observer into the three structural reject reasons currently starving the AI layer in live scans: `liquidity`, `lane_entry_window`, and `edge_above_cap`. The hook is bounded per scan, best-effort only, and updates per-strategy scan stats with `shadow_observer_calls` / `shadow_observer_ok`.

**`src/ops_pulse.py`, `config/settings.yaml`:** Added observer telemetry aliases to ops aggregation and enabled a new `ai.shadow_observer` config block with a one-call-per-scan cap for those reject reasons.

**`tests/test_ai_preentry_veto.py`, `tests/test_eth_macro.py`, `tests/test_sol_macro.py`:** Added coverage for the new observer config helpers and for liquidity-path observer invocation on ETH/SOL macro scans.

**Why:** Recent paper cycles showed `ai_calls: 0` even with AI enabled because candidates were dying at structural gates before reaching the normal marginal/enforced AI path. This restores bounded AI/shadow visibility on rejected candidates so the learning loop is not completely starved during structurally quiet regimes.

**Verification:** `.venv/bin/python -m pytest tests/test_ai_preentry_veto.py tests/test_eth_macro.py tests/test_sol_macro.py -q` passed (`121 passed`).

## 2026-05-18 — Agent preference: no unsolicited gate tightening

**`AGENTS.md`:** Added operator rule — on strategy/performance questions, report data patterns and bugs; do **not** suggest raising `min_edge`, disabling windows, blocking hours, or similar restriction unless the user explicitly asks to tighten or reduce activity.

---

## 2026-05-18 — Lane calibration: separate paper vs live shadow mode

**`src/main.py`:** `PolyBot` resolves `LaneCalibrator` shadow mode from `trading.dry_run`: `lane_calibration.paper_shadow_mode` for paper/dry-run, `lane_calibration.live_shadow_mode` for live (legacy `shadow_mode` remains fallback). Hot config reload also refreshes calibrator when `trading` block changes.

**`tests/test_live_config_apply.py`:** Coverage for paper vs live shadow resolution on config apply.

**Why:** Same process can paper-trade in observation-only calibration while applying shrunk posteriors on live entries without flipping a global YAML flag between sessions.

---

## 2026-05-18 — Backtest replay: 1h window labels + 4H-hist override parity

**`src/backtest/updown_engine.py`:** `replay_window_tf_label()` maps 5 / 15 / 60-minute windows to live lane keys (`5m`, `15m`, `1h`); replay LTF override helpers now mirror live `buy_*_4h_hist_override_enabled` via resampled alt 4H MACD from 1H bars. Bundle/dashboard backtest start accepts window **60** (not legacy 30).

**`src/dashboard/server.py`:** `_parse_crypto_backtest_window()` validates 5 / 15 / 60 for dashboard-triggered crypto backtests.

**Tests:** `tests/test_updown_backtest_parity.py`, `tests/test_dashboard_bundle.py` updated for 1h replay contract.

---

## 2026-05-18 — Main loop: weather scan hard-disabled

**`src/main.py`:** Removed live weather `scan_and_analyze` / execute path from the trading cycle; forces `last_signal_counts.weather = 0` so ops telemetry does not imply weather is still firing. Execution helpers remain for manual/tests.

---

## 2026-05-18 — Local calibration batch (paper session artifacts)

**`data/calibration/`:** Appended local paper-session rows — `trades.jsonl`, `rejected_candidates.jsonl`, `rejected_candidates_settled.jsonl`, `lane_posteriors.json`; `data/entry_prices/updown_fills.jsonl`, `data/lane_state_audit.jsonl` for May 15–18 HYPE/SOL/ETH/XRP macro runs used in BUY_YES / ghost-gate analysis.

---

## 2026-05-17 — Side-selection observability: current pulse vs recent LONG/SHORT history

**`src/ops_pulse.py`:** Expanded `side_selection` telemetry so operators can distinguish “currently short-biased” from “BUY_YES disabled.” Each pulse now includes `long_lanes`, per-strategy `buy_yes_possible_this_pulse`, and a `recent_side_rollup` over the latest ops pulses so recent LONG/SHORT selection remains visible even when the current scan happens to be all `SHORT`.

**`tests/test_ops_pulse.py`:** Added coverage for the new side-selection fields and isolated the tests from the real local `data/logs/ops_pulse.jsonl` file by monkeypatching the pulse path.

**Why:** Investigation of the 2026-05-17 evening local run showed the merge itself was not down-only: `sol_macro` and `hype_macro` had recent `LONG` / `BUY_YES` side selection in live pulses, while `eth_macro` and `xrp_macro` remained `SHORT` because their underlying 1H bias inputs were bearish in the observed window. The old payload exposed only the current pulse, which made a transient all-short snapshot look like BUY_YES had been turned off globally.

**Verification:** `.venv/bin/python -m pytest tests/test_ops_pulse.py -q` passed.

---

## 2026-05-17 — Rejected-candidate logging expanded for major ETH/XRP short suppressors

**`src/strategies/eth_macro.py`:** Added rejected-candidate logging for previously invisible ETH up/down skips: `liquidity`, `price_too_far`, `oracle_basis_block`, and `lane_entry_window`. These rows now persist the operative side/action plus basic gate context such as liquidity floor, entry-price band, oracle basis cap, and evaluated minutes-left window.

**`src/strategies/sol_macro.py`:** Added the same rejected-candidate logging coverage for the shared SOL-family up/down path used by `sol_macro`, `xrp_macro`, and `hype_macro` for `liquidity`, `price_too_far_from_even`, oracle validation failures (including `oracle_basis_block` / `oracle_stale` / `oracle_missing`), and `lane_entry_window`.

**`tests/test_eth_macro.py`, `tests/test_sol_macro.py`:** Added regression tests proving the ETH oracle-basis block, ETH lane-entry-window block, and SOL-family up/down oracle block all write to the rejected-candidate ledger path.

**Why:** Short-side audit on the 2026-05-17 local run showed the biggest live ETH/XRP suppressors were visible in `ops_pulse` but missing from `data/calibration/rejected_candidates*.jsonl`, which meant the ghost calibrator could not learn whether those gates were protective or over-tight. This patch fixes the main observability blind spot without loosening thresholds yet.

**Verification:** `.venv/bin/python -m pytest tests/test_eth_macro.py tests/test_sol_macro.py -q` passed (`111 passed`).

---

## 2026-05-17 — Counterfactual probe coverage extended beyond `min_edge`

**`src/analysis/rejected_candidate_log.py`:** Added reusable probe builders for:
- upper-cap gates (`observed <= cap`), used for oracle basis hard vetoes
- range-band gates (`min <= observed <= max`), used for entry-price and entry-window filters

**`src/strategies/eth_macro.py`, `src/strategies/sol_macro.py`:** Major live suppressors now emit probe variants instead of raw rejects only:
- `oracle_basis_block` / oracle-basis validation → `oracle_basis_abs_bps`
- `lane_entry_window` → `entry_window_mins_left`
- `price_too_far` / `price_too_far_from_even` → `entry_price_band`

**`src/strategies/bitcoin.py`:** BTC histogram rejects now emit a support-count probe (`hist_support_count`) so settled ghost analysis can compare the current “require at least one supportive 4H/1H histogram vote” rule against a looser “allow zero support” or tighter “require both” rule. This closes the main probe gap on the known over-tight BTC short histogram family.

**`tests/test_bitcoin.py`, `tests/test_eth_macro.py`, `tests/test_sol_macro.py`:** Added regression coverage to assert the new probe payloads and policy versions are present on BTC histogram, ETH oracle/window, and SOL-family oracle reject logs.

**Why:** The settled ghost report had enough evidence to identify over-tight BTC histogram rejects and protective ETH weak-confirm rejects, but the biggest live suppressors still lacked parameterized counterfactuals. This patch means the next run will start producing probe-level settled rows for the exact gate families that were previously un-auditable.

**Verification:** `.venv/bin/python -m pytest tests/test_bitcoin.py tests/test_eth_macro.py tests/test_sol_macro.py -q` passed (`150 passed`).

---

## 2026-05-17 — AI decision logging enabled for post-restart observability

**`config/settings.yaml`:** Turned `ai.structured_log` on so validated direct AI decisions are written to `data/logs/ai_pipeline/marginal_analysis.jsonl`. Added an explicit note that `ai.shadow_pipeline.log_jsonl` only writes when the Tier-C shadow path actually runs on a qualifying AI-gated setup; it is not a per-scan heartbeat.

**Why:** Post-restart diagnosis showed `shadow_pipeline.jsonl` stopped advancing after `2026-05-17 20:14:30 PDT`, but `ops_pulse.jsonl` also showed recent pulses with `shadow_pipeline_calls: 0`. That means the write path was not broken; the bot simply was not entering the shadow branch. Enabling structured marginal logs restores disk visibility into direct AI approve/reject decisions without forcing extra shadow latency on every candidate.

**Verification:** Confirmed code-path behavior in `src/analysis/ai_agent.py`: direct AI logs are gated by `structured_log`, while shadow logs append independently when `run_shadow_pipeline(...)` completes. Recent `ops_pulse.jsonl` pulses after restart reported `shadow_pipeline_calls: 0`, matching the stalled shadow file.

---

## 2026-05-17 — Settled ghost lane/gate report for missed-EV vs protected-loss ranking

**`tools/ghost_gate_report.py`:** Added a read-only operator report over `data/calibration/rejected_candidates_settled.jsonl`. It summarizes settled ghost outcomes at three levels:
- lane-level ghost calibration
- gate-level rankings (`strategy|window|action|reason`)
- probe-variant sensitivity buckets when newer rows contain `probe_variants`

The economics are split explicitly into **`missed_ev_pct`** (positive realized returns blocked by the gate) and **`protected_loss_pct`** (negative realized returns avoided by the gate), plus **`net_gate_value_pct = protected_loss_pct - missed_ev_pct`** so a positive number means the gate helped overall and a negative number means it likely blocked more value than it saved.

**`tests/test_ghost_gate_report.py`:** Added coverage for lane/gate aggregation and probe-variant aggregation semantics.

**Verification:** `.venv/bin/python -m pytest tests/test_ghost_gate_report.py tests/test_ghost_calibration.py -q` passed. The tool also ran successfully against the live settled file (`22324` rows at run time).

---

## 2026-05-17 — Ghost probes + shadow lineage + ETH 15m short-only weak-confirm relaxation

**`src/analysis/rejected_candidate_log.py`, `src/analysis/ghost_calibration.py`:** Extended rejected-candidate rows with additive probe telemetry for threshold counterfactuals (`probe_variants`) plus optional `policy_version` / `feature_hash`, and preserved those fields into the settled ghost ledger. This closes the main observability gap for rejected trades: lane/gate rejects can now record nearby threshold relax/tighten outcomes instead of only the raw blocked snapshot.

**`src/strategies/bitcoin.py`, `src/strategies/sol_macro.py`, `src/strategies/eth_macro.py`:** Started logging `lane_min_edge` rejects into the ghost dataset with probe variants keyed off the live `effective_min_edge` and actual edge at the decision point. ETH also now supports side-specific `eth_follow_15m_min_adj_{long,short}` thresholds; config sets only `eth_follow_15m_min_adj_short: 0.03`, leaving the global/base 15m confirm bar at `0.04`. That is an intentional narrow relaxation for the ghost-identified ETH 15m short weak-confirm lane, not a global widening.

**`src/analysis/ai_agent.py`, `src/analysis/null_ai_agent.py`, `src/backtest/backtest_ai.py`, `config/settings.yaml`:** Shadow pipeline outputs now carry explicit `prompt_version`, `policy_version`, and deterministic `feature_hash`. The config now pins `ai.prompt_version` / `ai.policy_version`, and the null/backtest AI shims were kept interface-compatible with the live agent after the new arguments/helper methods landed.

**Tests:** Added coverage for shadow log lineage, ETH short-lane threshold override, and null-AI helper compatibility; updated the backtest AI proxy signature for parity with live AI. Verification passed:
- `.venv/bin/python -m pytest tests/test_ai_agent_parse.py tests/test_eth_macro.py tests/test_strategies.py tests/test_ghost_calibration.py -q`
- `.venv/bin/python -m pytest tests/test_bitcoin.py tests/test_sol_macro.py -q`

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

**`src/dashboard/index.html`:** Replaced unused Strategy Performance signal metric boxes with **per-strategy lanes** (BTC/SOL/ETH/HYPE/XRP/WX), each showing Buy YES vs Buy NO WR, W/L, and net PnL; session slip banner when both sides have 3+ closes; lane grid always rendered (loading/empty states). `dashboard_ui_rev`: `2026-05-17-action-lanes-always-visible`.

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

## 2026-05-17 — Lane-specific AI decision feedback and cache isolation

**`src/analysis/ai_agent.py`:** Added lane-aware prompt feedback for the live AI decision path. The AI prompt can now include exact-lane posterior stats from `lane_posteriors.json`, broader lane-family calibration from `trades.jsonl`, and rejected-candidate sibling win-rate from `rejected_candidates_settled.jsonl`. Added `prompt_version` tagging to marginal/shadow logs, plus lane-specific cache keys so different lanes in the same strategy no longer reuse one cached AI response.

**`src/strategies/bitcoin.py`, `src/strategies/sol_macro.py`, `src/strategies/eth_macro.py`:** Threaded `lane_id` into AI decision/shadow calls so BTC/SOL/ETH AI gating is no longer strategy-only; the live AI layer now receives lane-specific context when evaluating marginal/enforced trades.

**`tests/test_ai_agent_parse.py`:** Added regression coverage for lane-specific cache keys and lane-feedback prompt context.

**Verification:** `.venv/bin/python -m pytest tests/test_ai_agent_parse.py tests/test_ai_preentry_veto.py -q` passed (`28 passed`). `.venv/bin/python -m py_compile src/analysis/ai_agent.py src/strategies/bitcoin.py src/strategies/sol_macro.py src/strategies/eth_macro.py` passed.

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


## 2026-05-19 — DOGE / BNB runtime wiring for calibration

| Commit | Summary |
|--------|---------|
| `uncommitted` | Added real `doge_macro` / `bnb_macro` strategy classes, runtime instantiation in [`src/main.py`](/Users/mainfolder/Documents/psb-main%201/src/main.py), exposure-manager routing, scan/execution diagnostics, and bootstrap config blocks in [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) so both lanes can generate rejected-candidate logs and settle into ghost-calibration data. Also extended [`tests/test_live_config_apply.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_live_config_apply.py) for the new runtime lanes. |

---

## 2026-05-25 — Paper loop gate cleanup

| Commit | Summary |
|--------|---------|
| `uncommitted` | Loosened live paper blockers that were starving `sol_macro` and `doge_macro`: SOL no longer requires the stale 15m IQL / alt-momentum confirmations before paper entries, DOGE entry windows widened to match active scan cadence, and the up/down composite floor lowered so candidates can reach paper execution for calibration. |

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
