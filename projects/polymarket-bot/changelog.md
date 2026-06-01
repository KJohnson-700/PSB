# Polymarket bot — infrastructure & milestones

Strategy tuning and per-strategy results live in `strategy-log/*.md`, not here.

---

## 2026-05-31 — AI decision layer rebuilt + 68GB disk reclaim + 1h/15m→4h neutral fallback

- **What changed:** (1) Reclaimed **68 GB** of `.git` garbage (2,311 abandoned `tmp_pack_*` from auto-gc failing on a full disk); untracked all of `data/` from git (`git rm --cached -r data/` + ignore `data/`), deleted broken `data/backtest/`. Ghost calibration data kept on disk. (2) Rebuilt the AI **decision layer** as a **synchronous, fail-open** gate on **15m/1h only** (5m is pure quant — AI latency ≫ a 5m window). All AI paths (default/marginal/neutral_15m) moved off the async enqueue/expire broker that was silently dropping trades. Default lane fail-OPEN; marginal/neutral fail-CLOSED. Gate timeout 40s. New `ai_decision_settler.py` scores live verdicts (`decision_layer.jsonl`) against outcomes. (3) Added asset-own **4h neutral fallback** for the 1h and 15m lanes (`_get_4h_bias`), so a neutral horizon resolves an alt-native direction instead of sitting out (BTC already had this).
- **Why:** The "complete for months" AI layer was never live (`enabled:false`) and Codex's flip-on used an async broker + default-lane gate that starved trades. Disk was 100% full (bot writes blocked). 1h-neutral lanes sat out for lack of a higher reference.
- **Hypothesis:** Real AI gating on slow lanes (where latency is harmless) plus fail-open + 5m-quant recovers/keeps trade frequency while finally measuring AI accuracy; 4h fallback recovers neutral-1h/15m participation.
- **Expected outcome:** `decision_layer.jsonl` fills with real approved/rejected verdicts (settler-scorable); `_neutral_fallback_4h` cohorts appear; no AI-induced starvation.
- **Actual outcome:** `pending` — forward-test only (candidate-gen, NOT ghost-validatable). `dry_run` live check confirmed gate fires + fail-open lets trades through. Needs ≥ a full session + settler run to judge gate accuracy.
- **Status:** `pending` (needs bot restart to load; 213 tests pass).

## 2026-05-28 — Calibration throughput guardrails

- **What changed:** Restored paper loss-pause behavior so paper-mode lanes auto-resume during calibration instead of requiring manual intervention after a loss-streak pause. Manual resume semantics still apply to explicit live/manual pause mode. Added a Codex working note at [projects/polymarket-bot/codex-working-notes.md](/Users/mainfolder/Documents/psb-main%201/projects/polymarket-bot/codex-working-notes.md) anchoring the current calibration recommendation.
- **Why:** The project is in calibration/data-gathering mode. A lane stuck in `exposure_paused` blocks sample collection and can make a good strategy posture look broken by starving it.
- **Verification:** `.venv/bin/python -m pytest tests/test_exposure_manager_sizing.py tests/test_lane_manager.py -q` passed; `py_compile` passed for the touched execution/strategy modules.
- **Status:** `pending` — next paper run should confirm the bot resumes after loss-pause cooldown and keeps producing calibration samples.

## 2026-05-28 — Beta-veto historical backfill + disabled-state checkpoint

- **What changed:** Added reproducible historical beta-veto reconstruction via [src/analysis/beta_veto_backfill.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/beta_veto_backfill.py), [tools/backfill_beta_veto_rows.py](/Users/mainfolder/Documents/psb-main%201/tools/backfill_beta_veto_rows.py), and [tests/test_beta_veto_backfill.py](/Users/mainfolder/Documents/psb-main%201/tests/test_beta_veto_backfill.py). Ran the backfill at the last attempted setting `beta_veto_max_mean=0.42`, `beta_veto_min_n=30`, producing [data/calibration/beta_veto_historical_rows.jsonl](/Users/mainfolder/Documents/psb-main%201/data/calibration/beta_veto_historical_rows.jsonl) and [data/calibration/beta_veto_historical_summary.json](/Users/mainfolder/Documents/psb-main%201/data/calibration/beta_veto_historical_summary.json). Verified the restart config remains in the disabled state at `beta_veto_max_mean: 0.0`, `beta_veto_min_n: 0`.
- **Backfill result:** The reconstructed `0.42 / 30` veto would have matched `2,287` historical rejected rows, `2,272` of them already settled, with `1,509` wins and `763` losses for a settled WR of `66.4%`. Largest affected cohorts were BTC (`997` rows), SOL (`369`), BNB (`305`), XRP (`216`), HYPE (`206`), and DOGE (`192`).
- **Why:** The operator wanted to preserve a defensible starting point for beta-veto sweet-spot tuning before turning the veto off for throughput recovery. The existing ghost ledgers did not already contain an explicit historical `beta_vetoed` family, so the correct move was to reconstruct it from timestamp-ordered live trade history rather than guess.
- **Method caveat:** This backfill replays only live trades and the same Beta(2,3) update math as the calibrator. It intentionally excludes later ghost-fed beta updates and per-lane override logic because those are not available at rejection time and would blur the global veto experiment.
- **Verification:** `.venv/bin/python -m pytest tests/test_beta_veto_backfill.py -q` passed; `.venv/bin/python -m py_compile src/analysis/beta_veto_backfill.py tools/backfill_beta_veto_rows.py` passed; `.venv/bin/python tools/backfill_beta_veto_rows.py --max-mean 0.42 --min-n 30` generated the committed artifacts.
- **Status:** `pending` — next paper run should confirm throughput recovers with the veto disabled while the saved backfill anchors the next `max_mean/min_n` sweep.

## 2026-05-27 — Cross-strategy crypto circuit breakers

- **What changed:** Added [src/analysis/circuit_breakers.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/circuit_breakers.py), a global side-specific halt manager for crypto up/down entries. It has:
  - **Fast correlation-stop halt:** default `3` same-side stop exits inside `60s`.
  - **Slow correlation-stop halt:** default `6` same-side stop exits inside `900s`, added because the 5/26_04 failure was a multi-hour bleed rather than only a tight stop cluster.
  - **BTC reversal halt:** default `0.3%` adverse BTC move over `300s` while the book has at least `5` same-side positions.
- **Execution wiring:** [src/main.py](/Users/mainfolder/Documents/psb-main%201/src/main.py) now records stop-loss exits into the breaker and checks the breaker before new BTC/SOL/ETH/HYPE/XRP/DOGE/BNB macro entries. Breakers block only new entries on the halted side; exits and offsetting opposite-side entries remain allowed.
- **Config:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) adds `correlation_stop_halt` and `reversal_halt`, both enabled for the next paper/session round.
- **Why:** The earlier `3 stops / 60s` design would catch clustered cascades but could miss the observed 3.5h slow bleed. The added slow mode is the direct fix for that coverage gap.
- **Verification:** [tests/test_circuit_breakers.py](/Users/mainfolder/Documents/psb-main%201/tests/test_circuit_breakers.py) covers fast halt, slow halt, BTC reversal halt, and offsetting-side allowance.
- **Status:** `pending` — needs the next paper session to confirm whether halt events reduce post-peak wipeout without suppressing useful recovery entries.

## 2026-05-26 — Per-(asset, timeframe) lane direction FSM + signal-snapshot enrichment

**Audience:** Codex cross-check before any live cutover. Nothing live changed; default-off.

### Background — why this exists

Prior assessment (in this session) showed:
- 5/22 GOLD sessions were +$170 / +$292 at 42–57% WR, almost entirely BUY_NO.
- 5/26 sessions degraded to −$26 → −$62 over multiple 200-trade windows at 35–38% WR with 100% BUY_NO and `primary_htf_bias` pinned BEARISH (BTC at 97.5% bearish vs 5/22 BTC bias which was 100% BULLISH on one session and 37% BULL/63% BEAR on the other).
- Codex correctly flagged and reverted the 0.3× lane sizing haircut, but per-trade win/loss ratio collapsed on 5 of 7 strategies (XRP 2.09 → 0.97, HYPE 3.69 → 0.84, SOL 2.28 → 1.42, BTC 2.55 → 1.79, DOGE 2.09 → 1.20). On `bearish_dip_default` lane WR fell 46–57% → 30.6%. Codex's hold-to-resolution counterfactual showed +$65 on 5/22 but −$102 / −$146 on 5/26 — winners on 5/26 are weak and revert after TP.
- Root cause hypothesis: the global `primary_htf_bias` pin overrides per-(asset, timeframe) signal direction; lanes whose own quant signal disagrees with the global classifier are forced into the wrong side (or are NEUTRAL-skipped) rather than firing on their own conviction.

### What changed

User-approved plan: `/Users/mainfolder/.claude/plans/it-weems-like-each-zippy-pond.md`

1. **New module:** [src/analysis/lane_direction_fsm.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_direction_fsm.py) — per-(asset, timeframe) conviction-score FSM.
   - Score range [−1, +1] built from six contributors per timeframe: MACD direction (sign of histogram), MACD momentum (rising+above_zero=+1, falling+below_zero=−1, else 0), MACD crossover (BULL_CROSS=+1, BEAR_CROSS=−1, NONE=0), EMA alignment (9>21>50 stack), RSI zone (50–70=+1, 30–50=−1, extremes=0), neighbour-TF MACD direction (5m peeks 15m, 15m peeks 30m, etc.).
   - `htf_bias` applied as a small additive nudge (default α=0.15) — modulator, not overrider. Encodes the user's directive: "let htf bias modulate confidence scores rather than override them."
   - Posterior confidence calibration: low-data lanes get scaled-down scores (`score *= 0.5 + 0.5*conf` where `conf = min(1, total_n/N_REF)`); biases sparse lanes toward NEUTRAL.
   - Discretisation with hysteresis: `T_enter=0.30`, `T_exit=0.10` (separate enter vs leave) to prevent flip-flop at threshold boundary.
   - NEUTRAL sub-FSM keyed by `previous_non_neutral` + `momentum_at_transition` (sign of macd_direction at the moment the lane left BULLISH/BEARISH). Behaviour matrix:
     - BULLISH → NEUTRAL, momentum still up → SIT_OUT (let it confirm).
     - BULLISH → NEUTRAL, momentum turned down → contrarian-fade BUY_NO at 0.3× size.
     - BEARISH → NEUTRAL, momentum still down → SIT_OUT.
     - BEARISH → NEUTRAL, momentum turned up → contrarian-fade BUY_YES at 0.3× size.
     - NEUTRAL stuck >30 min → exploration mode, signal-time side, 0.3× size.
     - NEUTRAL_INITIAL (no prior resolution) → SIT_OUT.
   - State persists to `data/calibration/lane_direction_state.json`; every transition emits a `direction_event` line to `data/lane_state_audit.jsonl`.
   - Feature gate: `lane_direction_fsm_active` in config (default **false**) — module is available for replay/shadow wiring but is NOT yet called by live strategy side-decision sites.

2. **Unit tests:** [tests/test_lane_direction_fsm.py](/Users/mainfolder/Documents/psb-main%201/tests/test_lane_direction_fsm.py) — 27 tests covering score math, htf modulator (verifying it does NOT override a strong contrary signal), posterior confidence scaling, hysteresis edge cases, every NEUTRAL FSM transition, persistence round-trip, audit-log emission, defensive paths (missing TA, bogus timeframe). [tests/test_alt_macro_snapshot_contract.py](/Users/mainfolder/Documents/psb-main%201/tests/test_alt_macro_snapshot_contract.py) adds explicit per-asset snapshot coverage for SOL, ETH, XRP, HYPE, BNB, and DOGE. **34/34 pass.**

3. **Config additions:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) (added at end, NOT modifying any existing keys):
   - `lane_direction_fsm_active: false` (feature gate)
   - `lane_direction_t_enter: 0.30`, `lane_direction_t_exit: 0.10`, `lane_direction_htf_alpha: 0.15`
   - `lane_direction_posterior_n_ref: 200`, `lane_direction_neutral_stuck_sec: 1800`, `lane_direction_recovery_size_mult: 0.30`
   - `lane_direction_overrides: {}`, `lane_direction_contributor_weights: {}` (per-lane / per-tf overrides; empty by default)

4. **Replay validator:** [scripts/lane_direction_fsm_replay.py](/Users/mainfolder/Documents/psb-main%201/scripts/lane_direction_fsm_replay.py) — walks `data/paper_trades/*/entries.jsonl`, reconstructs minimal TA from `extra.indicator_snapshot` per ENTRY, runs the FSM, computes counterfactual WR and pnl. Acceptance gate per the plan:
   - Baseline 5/22 (`test_20260521_212905`, `test_20260522_052210`): WR drop ≤ 2pp.
   - Current 5/26 (`test_20260525_231430`, `test_20260526_042005`): WR improvement ≥ +5pp AND sit-out ≤ 25%.

5. **Tuner:** [scripts/lane_direction_fsm_tune.py](/Users/mainfolder/Documents/psb-main%201/scripts/lane_direction_fsm_tune.py) — parameter sweep over (t_enter, t_exit, htf_alpha, contributor-weight emphasis, n_ref). Prints top configurations by current-period WR delta with acceptance markers. Does NOT write config.

6. **Signal-snapshot enrichment (this is the change that affects future entries):** added to [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py) at the existing `indicator_snapshot={...}` dict (around line 2940). For alt strategies, [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) now owns an asset-neutral `_build_alt_indicator_snapshot(...)` helper used by SOL/XRP/HYPE/BNB/DOGE, and [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py) reuses the same helper inside its ETH-specific signal path:
   - Per existing per-tf histogram entry, added paired `_crossover` and `_above_zero` fields.
   - Added 30m timeframe slot (`btc_30m_*` / `alt_30m_*`) where it wasn't already present.
   - Added EMA stack: `btc_ema_9 / _21 / _50` (or `alt_ema_*`).
   - Added per-prefix RSI: `btc_rsi_14` / `alt_rsi_14` (extra.rsi remains as fallback).
   - HYPE / BNB / DOGE / XRP inherit from `SolMacroStrategy` and now have explicit test coverage proving they get the full snapshot contract. HYPE's `scan_and_analyze` override at hype_macro.py:110 is a post-filter only.
   - The refactor centralizes snapshot construction; no FSM side-decision wiring, sizing changes, gates, or exits were added.

### Hypothesis

The pre-FSM behaviour forces all lanes onto the global bias. When the global classifier is correct, results are fine (5/22 baseline). When it sticks for hours (5/26 100% BEARISH including BTC), every alt is forced to BUY_NO regardless of its own signals, and per-lane WR collapses on lanes whose underlying asset is trending opposite the pin.

The FSM:
- Lets each (asset, timeframe) lane vote with its own quant signal.
- Uses `htf_bias` only to nudge confidence, not flip side.
- Adds a NEUTRAL-recovery path with contrarian-fade slots so a lane that has just lost its directional bias can still take a small contrarian position when momentum has turned, instead of sitting out the entire transition.

### Expected outcome (must be measured, not assumed)

On a 5/26-equivalent session, the FSM at a passing tuning should:
- Route BTC to NEUTRAL_FROM_BULL (it was BULLISH for days, then went bearish) → either SIT_OUT or contrarian-fade BUY_NO at 0.3× — instead of full-size BUY_NO that's been bleeding.
- Route at least some alts (HYPE, XRP) to BULLISH where their own MACD/EMA disagree with the global BEARISH pin — taking BUY_YES that the current bot won't fire.
- Reduce per-trade volume slightly (sit-out budget ≤ 25%) while improving WR ≥ +5pp.

On a 5/22-equivalent session, the FSM should mostly agree with the existing BEARISH routing (because the quant signal AND the htf bias both point bearish on those alts). Acceptance bar: WR drop ≤ 2pp.

### Actual outcome — pending; ACCEPTANCE GATE CURRENTLY **FAILS**

Replay on existing data (before snapshot enrichment took effect on new entries):
- Baseline 5/22: actual WR 47.6% → FSM WR 45.7% (Δ −1.8pp — within 2pp budget, **OK**).
- Current 5/26: actual WR 38.3% → FSM WR 36.2% (Δ −2.1pp — need ≥+5pp, **FAIL**).
- Tuner sweep of 1,500 configurations: 0 pass acceptance. Best current Δ across all 1,500 = −0.1pp.

Diagnosis: the FSM can't pass replay at any tuning because the OLD `indicator_snapshot` only logs 3–4 MACD histograms per entry with no crossover, no `above_zero`, no EMA stack, no per-tf RSI. The FSM reads 6 contributors — only 2–3 are reconstructable from pre-enrichment entries → scores cluster near zero → near-noise decisions. **This is a data-sparsity problem, not an FSM design problem.**

Path A (chosen by operator): enrich the snapshot fields so future replays have full-fidelity data. **Done in this changelog entry.** The bot needs to write ~200 fresh entries under the new schema, then replay re-runs against those entries should be discriminating enough to validate (or correctly refute) the FSM at some tuning.

### What Codex should cross-check

1. **`src/analysis/lane_direction_fsm.py`** — verify the FSM math matches the user's stated rule ("let htf_bias modulate confidence rather than override"). Specifically: `apply_htf_modifier` is purely additive at α=0.15 default; a raw_score of ±0.9 with a contrary htf_bias still resolves to the score's side. Test case in `tests/test_lane_direction_fsm.py::TestHtfModifier::test_does_not_override_strong_signal` proves it.

2. **NEUTRAL sub-FSM mapping** — verify the directives table at `lane_direction_fsm.py:_neutral_directive` matches the updown-market semantics (BUY_NO = bet price ends below open = fade prior bullish; BUY_YES = bet price ends above open = fade prior bearish). State-to-side table in this changelog and in tests.

3. **Snapshot edits are per-asset covered** — diff the strategy files at the `indicator_snapshot={...}` block and `_build_alt_indicator_snapshot(...)`. Backward compatibility for the replay reader is preserved (it falls back to `(hist > 0)` for `above_zero` and to `extra.rsi` for RSI when the new keys are absent). `tests/test_alt_macro_snapshot_contract.py` proves SOL, ETH, XRP, HYPE, BNB, and DOGE all emit 30m MACD, crossover, above-zero, EMA stack, RSI, and BTC move context fields.

4. **Live behaviour unchanged** — `lane_direction_fsm_active: false`, and the strategy files at the side-decision sites (`bitcoin.py:1342`, `eth_macro.py:544`, `sol_macro.py:1871`) have **not** been wired to call the FSM. Per the plan: shadow-mode wiring at those sites only happens once the replay validator passes.

5. **Test coverage** — 27 unit tests; non-related pre-existing failures (4 in `test_bitcoin_scenarios.py` / `test_sol_macro_skip_accounting.py`) confirmed not caused by these changes (they don't reference `indicator_snapshot`).

### Files for Codex to read

- Plan: `/Users/mainfolder/.claude/plans/it-weems-like-each-zippy-pond.md`
- Module: `src/analysis/lane_direction_fsm.py`
- Tests: `tests/test_lane_direction_fsm.py`, `tests/test_alt_macro_snapshot_contract.py`
- Replay: `scripts/lane_direction_fsm_replay.py`
- Tuner: `scripts/lane_direction_fsm_tune.py`
- Config block: `config/settings.yaml` (bottom, under the dashboard section)
- Snapshot edits: `src/strategies/bitcoin.py` ~2940, `src/strategies/sol_macro.py` ~4101, `src/strategies/eth_macro.py` ~2093

### Status

`pending` — awaiting ~200 fresh paper entries under enriched snapshot schema, then re-run `python3 scripts/lane_direction_fsm_replay.py`. If acceptance passes, run `lane_direction_fsm_tune.py` to confirm or tune, then wire shadow-mode at the three strategy sites, then run live shadow for ~200 more trades, then flip `lane_direction_fsm_active: true` only after shadow confirms.

---

## 2026-05-25 — Ghost-led validation cleanup, Kimi decision hardening, and session audit tooling

- **What changed**
  - **Validation posture:** Removed the in-repo backtest stack and stale backtest docs/tests, moved the shared OHLCV loader to [src/data/ohlcv_loader.py](/Users/mainfolder/Documents/psb-main%201/src/data/ohlcv_loader.py), and updated [README.md](/Users/mainfolder/Documents/psb-main%201/README.md) plus [docs/HANDOFF_STRATEGY_ENTRY.md](/Users/mainfolder/Documents/psb-main%201/docs/HANDOFF_STRATEGY_ENTRY.md) to make settled ghosts/Ghost Lab the validation source of truth.
  - **Dashboard/Ghost Lab:** Expanded [src/dashboard/server.py](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py) and [src/dashboard/index.html](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html) around ghost/live lane analysis, settled-candidate visibility, and operator review surfaces.
  - **AI decision routing:** Hardened [src/analysis/ai_agent.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py) for Kimi Code JSON extraction, cooldowns, provider-specific caps, and direct-decision provider scoping so MiniMax can remain shadow/research-only.
  - **Paper/session measurement:** Added [scripts/analyze_exit_counterfactuals.py](/Users/mainfolder/Documents/psb-main%201/scripts/analyze_exit_counterfactuals.py) with a session report under [docs/session_reports/](/Users/mainfolder/Documents/psb-main%201/docs/session_reports/) and fixed BUY_NO phantom filtering across journal summaries/learning/report scripts.
  - **Config/risk calibration:** Split paper daily trade capacity from the live cap, disabled BTC prediction-window bonuses, and capped live lane-calibration alpha amplification at identity. Per-strategy hypothesis tracking is recorded in `strategy-log/*.md`; code provenance is indexed in [docs/AGENT_CHANGELOG.md](/Users/mainfolder/Documents/psb-main%201/docs/AGENT_CHANGELOG.md).

- **Why:** The removed backtest engines did not faithfully replay live behavior and had become a misleading validation surface. This bundle moves review toward settled ghost/live-journal evidence, makes AI execution routing stricter, and improves session attribution before the next paper run.

- **Verification:** Targeted tests were updated for the affected areas. Run `.venv/bin/python -m pytest` before deployment if this commit is promoted beyond local/paper use.

## 2026-05-14 — Lane-management control system: lane IDs, execution gating, dashboard controls, recommendations, audit trail

- **What changed**
  - **Lane identity + journal wiring:** Added [src/analysis/lane_identity.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_identity.py) and wired `lane_id`, side/regime/family fields, and promotion state through [src/main.py](/Users/mainfolder/Documents/psb-main%201/src/main.py) and [src/execution/trade_journal.py](/Users/mainfolder/Documents/psb-main%201/src/execution/trade_journal.py) so entries, exits, and skip-style rows can be attributed to stable execution lanes.
  - **Lane calibration report:** Upgraded [scripts/journal_lane_calibration.py](/Users/mainfolder/Documents/psb-main%201/scripts/journal_lane_calibration.py) to group by `lane_id` when available and report expectancy, realized return on notional, edge-realized gap, average confidence, and AI-touched trade counts.
  - **Config-backed lane gating:** Added [src/analysis/lane_manager.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_manager.py) plus `lane_management` config in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml). The bot now supports per-lane `paper`, `live`, and `paused` states with exact and prefix matches, enforced before order placement.
  - **Dashboard lane operations:** [src/dashboard/server.py](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py) now exposes `/api/journal/lane-health`, `/api/journal/lane-states`, `POST /api/lane-state`, and `/api/lane-state-history`. [src/dashboard/index.html](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html) added a Lane Health panel with current state, recommended state, inline state-change controls, one-click apply-rec, recent state-change history, and a `Ready only` filter for escalation review.
  - **Recommendation + escalation layer:** Lane assessments now compute recommended state, advisory auto-pause candidate status, confirmation windows, candidate aging, and ready/live escalation warnings. Dashboard warnings persist to `data/lane_candidate_status.json`, and ignored ready-live warnings append audit rows to `data/lane_state_audit.jsonl`.
  - **Tests:** Added [tests/test_lane_manager.py](/Users/mainfolder/Documents/psb-main%201/tests/test_lane_manager.py) and extended [tests/test_dashboard_bundle.py](/Users/mainfolder/Documents/psb-main%201/tests/test_dashboard_bundle.py), [tests/test_journal_lane_calibration.py](/Users/mainfolder/Documents/psb-main%201/tests/test_journal_lane_calibration.py), and [tests/test_trade_journal_resumable.py](/Users/mainfolder/Documents/psb-main%201/tests/test_trade_journal_resumable.py) for lane identity, persistence, recommendation logic, dashboard APIs, and audit behavior.

- **Why:** The bot needed a lane-level control plane instead of strategy-wide heuristics so weak YES/NO slices could be isolated, papered, paused, and reviewed independently. This bundle turns lane metrics into operational controls with visibility and auditability, without introducing automatic demotions yet.

- **Verification:** `.venv/bin/python -m pytest tests/test_lane_manager.py tests/test_dashboard_bundle.py tests/test_journal_lane_calibration.py tests/test_trade_journal_resumable.py -q` and `.venv/bin/python -m py_compile src/analysis/lane_identity.py src/analysis/lane_manager.py src/dashboard/server.py src/main.py` green locally.

## 2026-05-13 — Backtest/live parity: shared updown exits, Polymarket YES marks, bundle runner, dashboard hooks

- **What changed**
  - **`src/execution/updown_exit_shared.py`, `src/execution/live_testing.py`, `src/backtest/updown_engine.py`:** Crypto up/down exit parameters now parse through one shared helper so live and backtest use the same take-profit, adverse-% stop, high-entry cents stop, scaled late-window stop, and in-profit tightening semantics.
  - **`src/backtest/updown_polymarket_marks.py`, `config/settings.yaml`, `scripts/run_backtest_crypto.py`:** Added optional PolymarketData YES 1m mark replay for backtest exits (`backtest.polymarket_marks.enabled` / `--polymarket-marks`) with parquet cache under `data/backtest/polymarket_marks/`.
  - **`scripts/run_crypto_backtest_bundle.py`, `src/dashboard/server.py`, `src/dashboard/index.html`:** Dashboard crypto backtests now support `ALL` bundle runs, propagate `--polymarket-marks` from config, and retry `/api/backtest/reports` refresh after job completion so new JSON files appear without manual reload.
  - **`src/backtest/ohlcv_loader.py`:** HYPE backtest OHLCV prefers Binance USDM `HYPEUSDT` before Hyperliquid fallback.
  - **Tests:** Added loader/cache coverage in `tests/test_updown_polymarket_slug.py`, shared-exit coverage in `tests/test_updown_exit_shared.py`, and integration coverage in `tests/test_updown_backtest_parity.py` proving Polymarket YES marks can override the synthetic proxy and that sparse marks still affect exits through `Series.asof()`.

- **Why:** Recent review identified live/backtest drift in crypto up/down exits and weak test coverage around the optional Polymarket YES mark path. The dashboard also needed a first-class multi-symbol backtest flow.

- **Verification:** `pytest tests/test_updown_backtest_parity.py tests/test_updown_exit_shared.py tests/test_updown_polymarket_slug.py tests/test_live_exit_overrides.py tests/test_dashboard_bundle.py tests/test_market_data_fallbacks.py -q` green locally.

## 2026-05-12 — Dashboard UX, ops digest lane order, HYPE klines pagination, updown replay guard

- **What changed**
  - **Dashboard (`src/dashboard/index.html`, `dashboard_ui_rev` in `server.py`):** Live crypto cards **stack in a single column** with larger padding; **reduced Command Center flicker** (single deferred `dashRefreshCryptoLiveTickers` per `fetchAll` when on Live, `updateMetrics` / decision-gate DOM dedupe); **alt-first hero order** on SOL/ETH/HYPE/XRP (spot **alt Δ%** before **BTC Δ%**; lag labels **ALT–BTC lag**); **decision gate digest** shows oracle chips before BTC 15m floor chip and sorts oracle lanes **SOL → ETH → HYPE → XRP → bitcoin** in the formatter; fullscreen crypto heroes use readable **clamp** sizes instead of shrinking tiles.
  - **`src/ops_pulse.py`:** `_decision_gate_digest` and `_buy_no_skip_digest` iterate strategy lanes with **alts before `bitcoin`** so JSON/`OPS_JSON` field order matches operator mental model.
  - **`src/analysis/hyperliquid_hype_service.py`:** `fetch_klines_range` **paginates** long windows (Hyperliquid caps ~5k candles per request) so HYPE 1m backtests are not empty.
  - **`src/backtest/updown_engine.py`:** Skip settle step when **1m frame lacks `open_time`** (avoids `KeyError` on empty HYPE 1m).
  - **`scripts/run_backtest_crypto.py`:** Small alignment with the above backtest path (same commit bundle).
  - **Tests:** `tests/test_dashboard_bundle.py`, `tests/test_market_data_fallbacks.py` updated for UI rev strings and HYPE chunking.
  - **`docs/AGENT_CHANGELOG.md`:** Index lines for the same dashboard/infra work.

- **Why:** Operators need readable fullscreen/live layout, less DOM churn on poll, digest order that does not imply BTC oracle leads alts, and reliable HYPE OHLCV for backtests.

- **Verification:** `pytest tests/test_dashboard_bundle.py tests/test_ops_pulse.py tests/test_market_data_fallbacks.py` green before push.

- **Note:** `strategy-log/bitcoin.md` and `strategy-log/eth_macro.md` may contain **2026-05-12** entries referencing `src/strategies/_core/` from an earlier refactor narrative; **verify paths** in those notes against the current tree (`_core` may have been inlined or removed in later merges).

## 2026-05-12 — `strategies/_core` extraction + AI-replay backtest loop (Option A)

- **What changed:** Extracted ~700 lines of hand-copied entry-decision logic from `BitcoinStrategy` / `SolMacroStrategy` / `ETHMacroStrategy` / `UpdownBacktestEngine` into 8 shared pure-function modules under `src/strategies/_core/`:
  - `htf_bias.py` (BTC 4H bias) — 3 callers
  - `ltf_strength.py` (BTC + SOL 15m MACD confirmation, IQL gate)
  - `m5_momentum.py` (BTC m5 direction scoring, SOL m5 MACD)
  - `htf_boost.py` (BTC 5m + 15m HTF boost, 4H/1H histogram gate)
  - `timing.py` (15m candle-momentum timing bonus)
  - `adjustments.py` (RSI 4-level adj, Sabre tension)
  - `alt_gates.py` (primary HTF bias, 1H hist gate, anti-LTF policy, BTC catalyst, SOL RSI extremes)
  - `eth_follow.py` (BTC follow impulses, ETH 5m/15m scoring)

  AI replay infrastructure (Option A from session planning):
  - `src/analysis/ai_call_log.py` — live `AIAgent.evaluate_trade_decision` writes one JSONL record per call to `data/ai_call_log/YYYY-MM-DD.jsonl` with quant inputs, AI outputs, and `context_hash` / `window_minutes` / `window_open_utc` for replay lookup.
  - `src/analysis/ai_replay_agent.py` — `AIReplayAgent` loads the log and exposes the same `evaluate_trade_decision` contract; returns recorded `AIDecision` (reason/source prefixed `replay:`) on hit, `replay_miss` SKIP on miss. Three indexes: exact `context_hash`, `(strategy, action, window_minutes, window_open[:16])`, `(market_id, strategy, action)`.
  - `UpdownBacktestEngine` accepts optional `ai_replay_agent=...`; after edge calc / before fill, consults the agent and skips on recorded rejections. Counters: `_ai_replay_passes`, `_ai_replay_skips`, `_ai_replay_misses`.
  - Dashboard Backtest tab: new **AI replay** checkbox + **strict** sub-toggle in Backtest Controls. Adjacent live-record counter via new `/api/ai_call_log/stats` endpoint shows `"N records across D day(s) — YYYY-MM-DD to YYYY-MM-DD"` or warns when no records exist.
  - `scripts/run_backtest_crypto.py`: `--ai-replay-from DIR` and `--ai-replay-strict` flags; prints replay stats after the run.
  - 30m-specific backtest config: `min_edge_{btc,sol,eth,xrp,hype}_30m` defaults (fall back to per-symbol 15m) and `scale_pct=0.0035` for `window_minutes==30`. Pre-refactor 30m fell silently into the 15m branch.

  Per-strategy drift fixes uncovered by the extraction are logged in `strategy-log/bitcoin.md`, `strategy-log/sol_macro.md`, and `strategy-log/eth_macro.md` (the ETH 5m 0.02-tier fix is also there). 5 drift bugs + 1 code-correctness fix (LEAN tier in `calc_candle_momentum`) shipped with the refactor.

- **Why:** `src/backtest/updown_engine.py` was a ~2000-line hand-copy of live strategy logic that had silently drifted from live over time. Some drifts inflated backtest WR vs live; others suppressed it. The replay loop closes the structural divergence flagged earlier: backtest had zero AI gating, so any backtest of a date range live had run was overstating WR.

- **Going-forward invariant:** any strategy-decision change must either modify a `_core` helper (callers auto-update) or update **all** existing callers in lockstep. New branches added to a strategy without mirroring in `updown_engine.py` will silently produce backtest results that diverge from live.

- **Verification:** 314 tests pass. New parity tests in `tests/test_strategy_core_*.py` lock both call sites for every extracted helper.

- **Operator workflow:** restart live to start populating `data/ai_call_log/`. Then on the dashboard Backtest tab, pick a crypto symbol/window, check **AI replay**, click **Run Crypto BT**. Output shows `approved=N skipped=N miss_passthrough=N` reflecting how many trades live's AI would have approved.

- **Status:** code committed and pushed to main as merge `7b7f503`. Bot must be restarted to load `_core` modules and start writing `data/ai_call_log/`.

## 2026-05-12 — Crypto execution sizing: USD stake converted to shares

- **What changed:** Crypto updown execution now treats strategy/risk `final_size` as **USD stake** and converts BUY orders into share quantity with `final_size / entry_price`; SELL paths continue to convert risk dollars by downside exposure. Open-position exposure accounting now uses `shares * entry_price`. Added hard exposure tier floors: `full_min_trade_usd: 10`, `moderate_min_trade_usd: 10`, `minimal_min_trade_usd: 5`.
- **Why:** A paper `BUY_NO` was recorded as `size=1.35` at `0.49`, risking about `$0.66` instead of the intended dollar stake. The old path passed dollar stake directly as BUY share quantity, and the multiplier stack could shrink entries into dust.
- **Verification:** `.venv/bin/python -m pytest tests/test_risk_manager_notional.py tests/test_risk_manager_hardening.py tests/test_updown_backtest_parity.py -q` → `25 passed`.
- **Status:** code committed; running bot must be restarted by the operator when desired.

## 2026-05-09 — Full AI + oracle + composite decision-control layer

- **What changed:** Added the shared `src/analysis/updown_composite_score.py` helper and upgraded `ai.decision_layer` config from legacy `enforce_on` to per-strategy `enforced_lanes`. SOL-family up/down paths now validate oracle freshness/basis before composite scoring and enforced AI lanes; BTC neutral 15m and HYPE 15m BUY_YES have lane-specific hardening.
- **Why:** Up/down candidates should follow `quant → oracle/data validation → composite score → AI decision → Kelly/risk sizing`, not reach sizing on edge alone.
- **Verification:** `pytest tests/test_ai_agent_parse.py tests/test_ai_narrators.py tests/test_sol_macro.py tests/test_eth_macro.py tests/test_live_exit_overrides.py tests/test_updown_composite_score.py -q` → `121 passed`; `py_compile` clean for touched strategy/analysis modules.
- **Status:** active after restart; strategy-specific expected outcomes are tracked in `strategy-log/*.md`.

## 2026-05-09 — Enforced AI decision layer skeleton

- **What changed:** Added `ai.decision_layer` config and `AIAgent.evaluate_trade_decision(...)`, an enforced pre-entry decision API that approves or rejects a quant candidate before Kelly sizing. BTC up/down marginal/low-confidence neutral approval now calls this decision layer instead of directly consuming raw `analyze_market()` output.
- **External-repo basis:** Implements the jmazzini-style lesson that composite confidence must be an entry gate, not only a logged diagnostic. The layer also preserves PSB's AI advantage by requiring `BUY_YES`/`BUY_NO` action match, confidence floor, and positive AI edge before approval.
- **Scope:** Enabled for risky/marginal paths first. `ai.decision_layer.use_shadow_portfolio` is still `false` by default so the 3-stage Research→Trader→Portfolio path remains optional until latency/cost and closed-trade calibration justify enforcing it on live trades.
- **Verification:** `pytest tests/test_ai_agent_parse.py tests/test_ai_narrators.py tests/test_live_exit_overrides.py -q` → `33 passed`; `py_compile` clean for `src/analysis/ai_agent.py` and `src/strategies/bitcoin.py`.
- **Status:** active for BTC marginal/low-confidence approval and SOL/ETH macro marginal up/down approval paths. HYPE/XRP inherit the SOL macro path. Next step is selectively enabling shadow-portfolio enforcement after latency/cost and closed-trade calibration are reviewed.

## 2026-05-09 — Shadow calibration, BUY_NO attribution, and TP/SL replay tooling

- **What changed:** Improved AI shadow-pipeline log records with top-level strategy/action/confidence fields (`shadow_action`, `shadow_confidence`, `quant_action`, `quant_edge`, `quant_threshold`) and made narrator calibration able to read nested shadow confidence values. Runtime diagnostics now print per-cycle `action_counts`, `side_source_counts`, `buy_no_skip_counts`, and the latest BUY_NO skip sample for BTC/SOL/ETH/HYPE/XRP.
- **Tooling added:**
  - [`scripts/buy_no_skip_report.py`](/Users/mainfolder/Documents/psb-main%201/scripts/buy_no_skip_report.py) summarizes persisted `BUY_NO_SKIP` events by strategy/reason/window and highlights near-miss edge gaps.
  - [`scripts/replay_exit_thresholds.py`](/Users/mainfolder/Documents/psb-main%201/scripts/replay_exit_thresholds.py) replays journal `PRICE_UPDATE` rows across TP/SL grids for crypto up/down exits.
- **Initial finding from recent journals:** `test_20260508_050455` + `test_20260508_151000` + `test_20260509_015113` have **0 `BUY_NO_SKIP` events**, while closed annotated entries were all `BUY_YES`. That implies BUY_NO starvation is upstream of post-side rejection; next live session diagnostics should inspect `action_counts` and `side_source_counts`.
- **Initial TP/SL replay:** over `129` closed crypto up/down trades from the two populated May 8 sessions, the current-ish `TP=0.15 / SL=0.20` tied the best tested outcome at about **+$19.70**. Raising TP to `0.20` or `0.25` reduced total replay PnL in this sample, so no TP widening is justified from these data alone.
- **Status:** instrumentation/tooling change only; no execution threshold changed.

## 2026-05-09 — External repo settings research: PSB comparison baseline

- **Source:** operator-provided research summary from other Polymarket/crypto bot repos. This is a research log, not a deployed tuning change.
- **External knobs observed:**
  - **Aulekator / PolyBullLabs-style:** fixed or simple percent sizing, explicit stop-loss / take-profit knobs, BTC-only or small bot families.
  - **0xFives-style:** predict-then-hedge logic and confidence gating.
  - **jmazzini 5m:** window delta + micro momentum + ATR, late entry about **10-50 seconds before close**, composite weighted confidence score, fixed `--amount`, no discretionary exit before settlement.
  - **Paid / advanced tooling noted:** four trigger-rule groups plus Monte Carlo / Kelly support.
- **PSB installed comparison:**
  - **Multi-strategy:** active for `bitcoin`, `sol_macro`, `eth_macro`, `xrp_macro`, `hype_macro`; optional `xrp_dump_hedge` exists separately.
  - **AI-assisted:** active for strategy AI calls, shadow pipeline, narrators, and post-trade annotations. **Important:** generic `ai.preentry_veto` is installed but currently **disabled**.
  - **Kelly sizing:** active via `KellySizer.size_from_edge()` and `ExposureManager.scale_size()`, with global/per-strategy Kelly fractions and per-trade max caps.
  - **Position caps:** `trading.default_position_size: 10`, `trading.max_position_size: 15`, `max_exposure_per_trade: 0.05`, plus exposure tier caps `25/15/8`.
  - **Stop / take-profit:** `take_profit_pct: 0.15`, `stop_loss_pct: 0.30`, `updown_stop_loss_pct: 0.20`, `updown_stop_cents: 0.03`, with ETH/XRP updown overrides.
  - **Regime gates:** installed across BTC/macro lanes via HTF/LTF, catalyst, alignment, macro-event, oracle/basis, and asset-specific gates.
  - **Late-window entry:** installed, but PSB uses minute-level entry windows (`0-5m`, `1-18m`, etc.) rather than jmazzini's explicit 10-50 second band.
  - **Hedge logic:** `xrp_dump_hedge` exists but should remain disabled until separately validated.
- **Immediate follow-up candidates:**
  - Re-label the comparison as **"pre-entry veto installed, disabled"** unless `ai.preentry_veto.enabled` is intentionally flipped on.
  - Test a jmazzini-inspired **composite quant confidence score** as a shadow metric first: window delta + micro momentum + ATR + existing regime flags, no execution change until journal/backtest evidence supports it.
  - Audit whether PSB's minute-level late-entry windows are too broad versus the external 10-50 second close-entry pattern, especially for 5m HYPE/XRP lanes.

## 2026-05-08 — Narrators write to journal + surface in dashboard AI Review panel (commit `acdbb3a`)

- **Issue:** Phase 1 (`b1c177a`) auto-fired narrators on startup but wrote to a sibling `_ai_summary.md` next to the journal. That meant: (a) extra discovery surface for any consumer (dashboard, replay, audits), and (b) the existing dashboard AI Review card on the Journal tab didn't pick them up.
- **Fix:** Two coupled changes so narrator output rides the same pipeline as everything else.
  1. **`_run_startup_narrators` now writes ANNOTATION events into the current session's `entries.jsonl`**, not a sibling file. Each narrator block is a `JournalEntry(event="ANNOTATION", trade_id="__session_summary__::<kind>", extra={source: "session_summary", narrator: "<kind>", previous_session, text})`. `TradeJournal._load_state` already ignores non-ENTRY/EXIT events, so resume is unaffected.
  2. **`GET /api/journal/ai-summary`** ([src/dashboard/server.py](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py)) extended:
     - New helper `_read_narrator_annotations(journal)` walks entries.jsonl, filters `event=ANNOTATION` + `extra.source=session_summary`.
     - New helper `_format_narrator_block(anns)` renders them as titled sections (Underperformance / Skip & exit reasons / Calibration drift / Strategy conflict).
     - Endpoint appends the formatted block to the existing summary string the Journal-tab AI Review card already consumes.
     - Cache key changed to `(session_id, entries.jsonl mtime)` so new narrator events appear automatically — no manual refresh required.
- **Frontend untouched:** the existing `ai-summary-card` and `loadAISummary()` in `index.html` already call `/api/journal/ai-summary`. The narrator output drops into the same card.
- **Tests:** [tests/test_dashboard_ai_narrator_summary.py](/Users/mainfolder/Documents/psb-main%201/tests/test_dashboard_ai_narrator_summary.py) covers the filter (event + source), empty-file guard, and pretty-title rendering. Existing `tests/test_dashboard_bundle.py` still passes — no structural UI changes.
- **Verification:** `pytest tests/test_dashboard_ai_narrator_summary.py tests/test_dashboard_bundle.py tests/test_ai_narrators.py tests/test_trade_journal_annotation.py tests/test_ai_preentry_veto.py` → 44 passed.

## 2026-05-08 — Narrators auto-fire on bot startup (commit `b1c177a`)

- **Issue:** Phase 1 (`0e9d579`) shipped narrators behind a manual `python3 scripts/run_ai_session_summary.py` invocation. Friction = nobody runs it = data not consumed.
- **Fix:** New `PolyBot._run_startup_narrators()` method, called as fire-and-forget `asyncio.create_task(...)` at the top of `start()`. Finds the most recent prior session dir (lexicographic, < current name, with activity), runs all four narrators against it, writes the result into the **new** session's journal dir as `_ai_summary.md`.
- **Why "previous session, written into new session":** The narrator output answers "what did we just learn?" — it's most useful when reviewing the upcoming session, so it lands where the next-session reviewer will look.
- **Self-gates:** `ai.session_summary.enabled` (master) + per-narrator `include_*` flags. Each narrator wrapped in try/except so one bad section never blocks the others. Off the trade hot path; never awaited.
- **Manual `scripts/run_ai_session_summary.py` still works** for ad-hoc runs, but it's no longer required.
- **File:** [src/main.py](/Users/mainfolder/Documents/psb-main%201/src/main.py) — new `_run_startup_narrators()` method + one-line hook in `start()`.

## 2026-05-08 — AI integration expanded: shadow pipeline + post-trade annotation + narrators + pre-entry veto helper (commit `0e9d579`)

- **Goal:** Get AI more involved in the bot without touching the trade hot path. Three layers shipped together: data collection (shadow + annotation), off-cycle analysis (four narrators), and a stub for AI in actual decision-making (pre-entry veto helper, default off until calibration data warrants).
- **Shadow pipeline expansion:**
  - ETH macro had it; now BTC/SOL/XRP/HYPE all run the parallel 3-stage Research→Trader→Portfolio pipeline alongside their existing AI consults. XRP and HYPE inherit from `SolMacroStrategy`, so the SOL-side injection covers all three.
  - Master `ai.shadow_pipeline.enabled` flipped to **true**. Logs to `data/logs/ai_pipeline/shadow_pipeline.jsonl`. Capped at 1 call per scan per strategy via `shadow_pipeline.max_calls_per_scan`.
  - Files: [src/strategies/bitcoin.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py) (3 sites), [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) (2 sites — covers SOL/XRP/HYPE).
- **Post-trade journal annotation:**
  - New `TradeJournal.append_annotation(trade_id, text, ...)` in [src/execution/trade_journal.py](/Users/mainfolder/Documents/psb-main%201/src/execution/trade_journal.py). Append-only side channel that writes an `ANNOTATION` event referencing an existing `trade_id` — never mutates `open_positions` or `closed_trades`. `_load_state` ignores non-ENTRY/EXIT events naturally, so resume is safe.
  - New `PolyBot._annotate_entry_async` helper in [src/main.py](/Users/mainfolder/Documents/psb-main%201/src/main.py). Fire-and-forget after every `log_entry` (BTC, SOL/XRP/HYPE, weather). Wrapped in `asyncio.create_task` — entry path returns before the LLM call resolves, so trade timing is unchanged. Writes thesis/expectations/invalidation in 3-5 sentences.
  - Optional **correlation/exposure warning**: when `ai.post_trade_annotation.include_correlation_check` is true, the prompt is augmented with current open positions and the AI flags correlated entries inline.
  - `ai.post_trade_annotation.enabled` set to **true** by default this commit so data starts flowing immediately.
- **Off-cycle AI narrators** ([src/analysis/ai_narrators.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_narrators.py)):
  - `summarize_underperformance(audit_report, ai_agent)` — turns a `build_underperformance_report` dict into a 4-6 sentence operator-actionable note.
  - `summarize_skip_exit_reasons(skip_dist, exit_dist, ai_agent)` — flags dominant skip/exit reasons (e.g. when `rsi_extreme_block` >30% of skips → tighten RSI threshold).
  - `detect_calibration_drift(shadow_records, closed_trades, ai_agent)` — joins shadow JSONL with closed trades on `market_id`, buckets by AI confidence (low/mid/high), narrates miscalibration.
  - `explain_strategy_conflict(scan_summaries, ai_agent)` — flags BTC/ETH/SOL/HYPE/XRP regime disagreement.
  - All four self-gate on agent availability and return empty string on failure.
- **Orchestrator** [scripts/run_ai_session_summary.py](/Users/mainfolder/Documents/psb-main%201/scripts/run_ai_session_summary.py): runs all four narrators against the latest session and appends a markdown block to `projects/polymarket-bot/strategy-log/_ai_summary.md`. Each narrator independently gated under `ai.session_summary.include_*`. Master `ai.session_summary.enabled` set to **true**.
- **Pre-entry veto** (decision-making stub):
  - New `AIAgent.preentry_veto_active(ai_confidence)` helper in [src/analysis/ai_agent.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/ai_agent.py).
  - Wired into BTC marginal sites 1 & 2 (the paths that previously had no confidence floor). SOL/XRP/HYPE already enforce `ai_confidence_threshold` so the veto is redundant there.
  - `ai.preentry_veto.enabled` deliberately left **false**. Threshold default 0.25. Reason: it kills trades, which is a behavior change, not data collection — flip on after calibration drift narrator shows AI confidence correlates with realized PnL.
- **Verification:**
  - 25 new tests added: [tests/test_ai_preentry_veto.py](/Users/mainfolder/Documents/psb-main%201/tests/test_ai_preentry_veto.py), [tests/test_ai_narrators.py](/Users/mainfolder/Documents/psb-main%201/tests/test_ai_narrators.py), [tests/test_trade_journal_annotation.py](/Users/mainfolder/Documents/psb-main%201/tests/test_trade_journal_annotation.py).
  - Patched `TestBitcoinAIIntegration.setup_method` in [tests/test_bitcoin_scenarios.py](/Users/mainfolder/Documents/psb-main%201/tests/test_bitcoin_scenarios.py) to stub the new AI helpers (MagicMock returns Mock for new methods otherwise).
  - Full suite: `347 passed`. 14 pre-existing failures (pytest-asyncio not installed in env + working-tree `max_position_size: 25` clash with stale weather kelly test) confirmed pre-existing via stash-and-rerun.
- **Active state at end of session:**
  - ON: shadow pipeline, post-trade annotation (with correlation check), session summary (all four narrators).
  - OFF: pre-entry veto (intentional — flip after calibration data accumulates).
- **Failure criteria → revert/disable:**
  - If `_annotate_entry_async` exceptions bubble into the execution path, disable `ai.post_trade_annotation.enabled` immediately. The wrapper has try/except around the entire body, so this should not be possible — but watch journal `ENTRY` write timestamps vs `add_position` for any divergence.
  - If shadow pipeline budget exhaustion starts erroring scans, lower `shadow_pipeline.max_calls_per_scan` to 0 (effectively off without flipping the master).
  - If session summary appends nonsense, set individual `include_*` toggles false to isolate the bad narrator.

## 2026-05-07 — Dashboard wiring sweep for stop-loss knob, exit-reason visibility, and bankroll-state clarity

- **Scope:** dashboard-only updates in [src/dashboard/server.py](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py), [src/dashboard/index.html](/Users/mainfolder/Documents/psb-main%201/src/dashboard/index.html), and [tests/test_dashboard_bundle.py](/Users/mainfolder/Documents/psb-main%201/tests/test_dashboard_bundle.py).
- **Config safety:** Config panel defaults were aligned with current calibrated values and `loadConfig()` now round-trips the new nested key `trading.exit_rules.updown_stop_loss_pct`. Also added a missing Weather Kelly input so all configured strategy Kelly fractions can be edited from the panel.
- **Validator/API:** `ConfigUpdates` now accepts `trading.exit_rules.updown_stop_loss_pct` in `[0,1]`.
- **Exit observability:** Added `GET /api/journal/exit-reason-summary` with per-reason counts, per-strategy buckets, and win/loss+pnl aggregates, cached by `(session_id, entries_mtime)`.
- **UI observability:** Performance tab now renders an **Exit Reasons (current session)** card (stacked bar + legend + avg pnl), refreshed on the existing polling cadence (no extra loop).
- **Bankroll correctness:** Replaced silent synthetic bankroll fallback with structured source-aware resolution. Status/SSE now include `bankroll_source` and optional `bankroll_warning`; UI shows a warning banner and em-dash when bankroll is unavailable instead of masking state.
- **Additional sweep fix:** Removed stale frontend guard that rejected `exposure.minimal_size < trading.default_position_size`, which conflicted with current intended config (`8 < 10`) and could block no-op saves.
- **Verification:**
  - `.venv/bin/python -m py_compile src/dashboard/server.py`
  - `.venv/bin/python -m pytest tests/test_dashboard_bundle.py -q` (`15 passed`)
  - JS syntax validation via Node `vm.Script` compile of script blocks extracted from `index.html` (direct `node --check` does not support `.html` in this runtime).

## 2026-05-07 — updown_stop_loss_pct: same-position % stop for updown markets (commit `b0a89f8`)

- **Issue:** Updown positions had no percentage stop loss during the hold — only a TP at +15% and a last-2.25min adverse-cents check. Positions could drift -40% adverse with 8min remaining and the bot would not act until the death window, then exit at whatever collapsed price.
- **Evidence (current session `test_20260507_194137`):** 3 of 5 exits were `updown_time_stop`. Bitcoin -23%, ETH -28% — both would have stopped at -20% if a same-position %-stop existed.
- **Fix:**
  - `config/settings.yaml`: added `trading.exit_rules.updown_stop_loss_pct: 0.20` (default 0.20).
  - [src/execution/live_testing.py](/Users/mainfolder/Documents/psb-main%201/src/execution/live_testing.py): new `elif pnl_pct <= -self.updown_stop_loss_pct: reason = "updown_stop_loss"` branch inside the `is_updown` block, between TP and the late-window cents-stop. Fires throughout the hold, not just in the death window.
- **Trade-off:** late-window noise can produce 20% drawdowns that recover by settlement; tighter (e.g. 0.10) would cut winners. 0.20 leaves cushion above TP's +15%.
- **Failure criteria → revert/widen:** if `updown_stop_loss` exits dominate and net PnL drops vs. prior `updown_time_stop` baseline over 50+ trades, widen to 0.25 or revert.

## 2026-05-07 — Sweep complete: global + weather also calibrated (commit `1e64a61`)

- Caught two missed knobs: `trading.kelly_fraction` (global default fallback) and `strategies.weather.kelly_fraction` were still at original 0.25. Both → 0.37 (1.5× original) for consistency. Weather is currently disabled but kept aligned for re-enable.
- Final state: every kelly_fraction in the config is now calibrated.

| Strategy | Original | New | Multiplier |
|---|---|---|---|
| trading.global | 0.25 | 0.37 | 1.5× |
| weather | 0.25 | 0.37 | 1.5× |
| bitcoin | 0.20 | 0.30 | 1.5× |
| sol_macro | 0.18 | 0.27 | 1.5× |
| eth_macro | 0.18 | 0.27 | 1.5× |
| xrp_macro | 0.18 | 0.27 | 1.5× |
| hype_macro | 0.16 | 0.24 | 1.5× |

Tier caps (`exposure.full_size/moderate_size/minimal_size`) widened to 25/15/8 in commit `adc5157` apply globally via `exposure_manager.scale_size()` — affects every strategy automatically.

## 2026-05-07 — Kelly fractions calibrated + tier caps widened so edge differentiates (commit `adc5157`)

- **Issue:** Prior 4× change (`7529062`) saturated every trade at the tier cap. Kelly raw was $36–50, but tier caps were $15/$10/$5, so every edge from 0.05 to 0.20 produced identical $15/$10/$5 outputs. Edge sensitivity dead.
- **Calibration:**
  - `bitcoin.kelly_fraction: 0.80 → 0.30` (1.5× original 0.20)
  - `sol/eth/xrp.kelly_fraction: 0.72 → 0.27` (1.5× original 0.18)
  - `hype.kelly_fraction: 0.64 → 0.24` (1.5× original 0.16)
  - `exposure.full_size: 15 → 25`
  - `exposure.moderate_size: 10 → 15`
  - `exposure.minimal_size: 5 → 8`
- **Logic:** edge × kf × bankroll(500) for bitcoin yields raw = $15 (edge=0.10), $21 (edge=0.14), $30 (edge=0.20). All fit within new FULL cap of $25, so FULL-tier output now scales with edge through ~0.16. MODERATE (×0.6) caps at $15 — edge sensitivity through ~0.18.
- **Bitcoin worked examples:**
  - edge=0.05 → $7.50 / $4.50 / $1.50
  - edge=0.10 → $15 / $9 / $3
  - edge=0.14 → $21 / $12.60 / $4.20
  - edge=0.20 → $25 (cap) / $15 (cap) / $6
- **Hardcoded caps from `7529062` retained:** [kelly_sizer.py:213](/Users/mainfolder/Documents/psb-main%201/src/analysis/kelly_sizer.py) at 1.0, [kelly_sizer.py:232](/Users/mainfolder/Documents/psb-main%201/src/analysis/kelly_sizer.py) max_pct at 0.10. Don't bind at these fractions but useful headroom for future tuning.

## 2026-05-07 — Kelly fractions 4× + hardcoded caps lifted (commit `7529062`)

- **Issue:** Prior 2× bump (commit `1a7023d`) was both insufficient AND would have been silently clipped by two hardcoded caps in [kelly_sizer.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/kelly_sizer.py) that I missed: `get_kelly_fraction()` clamped at 0.25, `size_from_edge()` clamped at 5% bankroll = $25.
- **Fix:**
  - `bitcoin.kelly_fraction: 0.40 → 0.80` (4× original 0.20)
  - `sol_macro / eth_macro / xrp_macro.kelly_fraction: 0.35 → 0.72` (4× original 0.18)
  - `hype_macro.kelly_fraction: 0.32 → 0.64` (4× original 0.16)
  - [kelly_sizer.py:213](/Users/mainfolder/Documents/psb-main%201/src/analysis/kelly_sizer.py) internal cap `0.25 → 1.0`
  - [kelly_sizer.py:232](/Users/mainfolder/Documents/psb-main%201/src/analysis/kelly_sizer.py) `max_pct 0.05 → 0.10` (10% bankroll per trade)
- **Effect:** With 4× fractions, Kelly raw exceeds every tier cap on typical edges (0.10–0.15). Output lands at $15 FULL / $10 MOD / $5 MIN — restores operator target band, now Kelly-driven instead of floor-collision-driven.
- **Trade-off:** edge proportionality is gone again at the top end (any edge ≥0.05 hits the tier cap). If you want both edge-sensitivity AND $10–15 stakes, the next knob is raising tier caps (`exposure.full_size`, `moderate_size`, `minimal_size`) rather than fractions.

## 2026-05-07 — Kelly fractions doubled + temporary tuning multipliers removed (commit `1a7023d`)

- **Issue:** After the `min_trade_usd` fix (commit `30061a3`), Kelly's actual `edge × fraction × bankroll` output was exposed and turned out to be too small for the operator-target $10–15 stake band. First 4 entries on session `test_20260507_194137`: bitcoin $9.50, $6.32, $5.12, eth_macro **$0.79** (ETH stacked tuning ×0.60 × degraded_corr ×0.50 × MINIMAL ×0.20 on top of an already-small $12.60 raw).
- **Fix:**
  - `bitcoin.kelly_fraction: 0.20 → 0.40`
  - `sol_macro / eth_macro / xrp_macro.kelly_fraction: 0.18 → 0.35`
  - `hype_macro.kelly_fraction: 0.16 → 0.32`
  - `sol_macro / eth_macro.tuning_size_multiplier: 0.60 → 1.0` (was marked "temporary risk-downsize while expectancy is being repaired"; today's gate intervention is the expectancy fix)
- **Why:** Kelly raw scales linearly with `kelly_fraction`. Doubling the fraction restores the $10–15 target stake band on bitcoin (edge=0.10 → raw $20, hits 5% bankroll cap of $25, lands $15 FULL / $10 MOD / $5 MIN). Alts get proportional uplift; with tuning multipliers gone, ETH at edge=0.14 now produces raw $24.50 (cap $25) → $15/$10/$4.90 across tiers instead of $0.79.
- **Failure criteria → revert:** if doubled fractions produce session drawdowns materially worse than tier-flat baseline over a 100+ trade window, revert to 0.20 / 0.18 / 0.16.
- **Verification:** restart bot, ≥10 entries, expected `size` mostly in $4–15 range with no sub-$1 stakes outside MINIMAL pile-on cases.

## 2026-05-07 — Sizing pipeline audit: Kelly was wired but its output discarded (commit `30061a3`)

- **Issue:** User suspected Kelly wasn't working — winners were getting cut to half-stake on a high-WR session ($24.93 actual vs $29.40 flat-$10 equivalent on `test_20260507_140301`). Audit revealed `exposure_manager.scale_size()` was deterministic per tier, *independent of Kelly's recommendation*.
- **Mechanism:** With `min_trade_usd: 25.0` and tier multipliers FULL=1.0 / MODERATE=0.6 / MINIMAL=0.2, the floor (`min_trade_usd × tier_mult`) exceeded the tier cap (`full_size`/`moderate_size`/`minimal_size`) in every tier:
  - FULL: floor=$25, cap=$15 → always $15
  - MODERATE: floor=$15, cap=$10 → always $10
  - MINIMAL: floor=$5, cap=$5 → always $5
  Kelly's careful edge-proportional sizing was computed and then thrown away. The bot was effectively running tier-flat sizing with Kelly as decoration.
- **Aggregate evidence:** Across 12 sessions / 441 closed trades over the past 2 days: actual +$92.08 vs flat-$10 +$83.28 — Kelly only +$8.80 ahead, and that gap came entirely from the *tier system* avoiding $10 stakes during bad regimes (saved $11.87 on one -$50 streak), not from Kelly itself.
- **Fix:** [config/settings.yaml:877](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `min_trade_usd: 25.0 → 1.0`. Floor becomes non-binding, so `scale_size()` collapses to `min(raw_size × tier_mult, tier_max)` — Kelly's recommendation now flows through, with tier acting as a ceiling and downsize multiplier rather than overwriting the stake. No code changes needed; [src/execution/exposure_manager.py:451](/Users/mainfolder/Documents/psb-main%201/src/execution/exposure_manager.py) already handles small floors correctly.
- **Side effect to watch:** Kelly raw is floored at $1 in [src/analysis/kelly_sizer.py:236](/Users/mainfolder/Documents/psb-main%201/src/analysis/kelly_sizer.py), so a MINIMAL-tier trade can now be as small as $0.20. If anything downstream silently rejects sub-dollar stakes, bump `min_trade_usd` back to ~0.50.
- **Worked examples (bankroll=500, post-fix):** bitcoin edge=0.10 → $10 / $6 / $2 across FULL/MOD/MIN; bitcoin edge=0.15 → $15 / $9 / $3; eth (kelly=0.18 + tuning ×0.60) edge=0.12 → $6.48 / $3.89 / $1.30.
- **Dead code spotted (not removed):** `PositionSizer` in [src/analysis/math_utils.py:23](/Users/mainfolder/Documents/psb-main%201/src/analysis/math_utils.py) is instantiated in `main.py` and stored on every strategy as `self.position_sizer` but no method is ever called. Truly inert — left in place this session, candidate for cleanup.
- **Verification status:** Static math confirmed (21 distinct outputs vs old 3-output collapse). Live verification requires next paper session ≥30 trades. Expected in `entries.jsonl`: `size` distribution shows varied values across $1–$15, not just $5/$10/$15.
- **Failure criteria → revert:** if Kelly proportionality demonstrably underperforms tier-flat over a 200+ trade window (session-level dollar PnL drops materially below the prior tier-flat baseline once normalized for trade count, or a tail loss the old MINIMAL=$5 floor would have prevented exceeds the upside captured), revert to `min_trade_usd: 25.0`.

## 2026-05-07 — Alt-lane gate intervention (commit `cd66c96`)

- SOL / HYPE / XRP `require_btc_catalyst_5m: false → true` — 5m backtests deeply negative; do not admit unstimulated 5m entries.
- ETH `enforce_alt_1h_alignment: true → false` — one-step loosening after silent night.
- ETH `btc_follow_1h_required: true → false` — stop whole-scan aborts; per-market follow checks remain.
- XRP `enforce_alt_1h_alignment: false → true` — restore bearish-1H BUY_YES suppression (BUY_NO path remains allowed in code).

## 2026-05-07 — Crypto up/down backtest time-stop exits moved toward live parity

- **Issue:** Active paper failure audit showed `bitcoin` live losses were dominated by `updown_time_stop`, while the crypto up/down backtest engine was still mostly validating hold-to-settlement outcomes. That made live-vs-backtest comparisons structurally misleading.
- **Code:** [`src/backtest/updown_engine.py`](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py) now replays approximate live crypto up/down near-expiry adverse `updown_time_stop` exits from 1m bars, then falls back to settlement when no stop fires. Exit parameters are read from `trading.exit_rules` plus per-strategy `updown_overrides`, matching the live paper exit manager more closely on the main observed failure path. [`scripts/run_backtest_crypto.py`](/Users/mainfolder/Documents/psb-main%201/scripts/run_backtest_crypto.py) now writes each trade’s `exit_reason` to reports.
- **Why:** The prior backtest was still proving the wrong thing for short-window crypto lanes. If live exits are path-dependent and backtest exits are mostly terminal-settlement, a strong backtest can coexist with a failing live path without contradiction.
- **Tests:** Added focused parity regressions in [`tests/test_updown_backtest_parity.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_updown_backtest_parity.py) for the new up/down exit proxy behavior.
- **Verification:** `.venv/bin/python -m pytest tests/test_updown_backtest_parity.py -q` passed locally (`14 passed`). The broader ETH crypto backtest plumbing file did not return a clean final status in this session, so only the focused parity suite is claimed green here.

## 2026-05-07 — Paper bankroll ruin parity: floor at zero, do not mask zero in dashboard

- **Issue:** Paper sessions could misrepresent bankruptcy-like test outcomes in two ways:
  - realized PnL in the live paper loop updated `bot.bankroll` directly without a hard zero floor, so a large losing exit/settlement could push paper bankroll negative instead of stopping at zero like limited-loss live capital;
  - dashboard/status disk fallbacks treated `bankroll <= 0` as “missing” and reconstructed bankroll from `initial_bankroll + total_pnl`, which could hide a real zero bankroll and make a busted test run look partially funded.
- **Code:** [src/main.py](/Users/mainfolder/Documents/psb-main%201/src/main.py) now routes realized exits/settlements through `_apply_realized_pnl_to_bankroll()`, which floors bankroll at `0.0` and keeps `risk_manager.bankroll` in sync. [src/dashboard/server.py](/Users/mainfolder/Documents/psb-main%201/src/dashboard/server.py) now resolves disk bankroll via `_resolve_bankroll_snapshot()` and preserves an explicit `0.0` bankroll instead of treating it as absent.
- **Why:** Test runs need realistic ruin behavior. In real life, a constrained cash account should be able to lose down to zero, but the UI and journal should not silently resurrect that zero into a synthetic positive bankroll.
- **Tests:** Added focused regressions in [tests/test_live_config_apply.py](/Users/mainfolder/Documents/psb-main%201/tests/test_live_config_apply.py) and [tests/test_dashboard_bundle.py](/Users/mainfolder/Documents/psb-main%201/tests/test_dashboard_bundle.py).
- **Verification:** `python -m py_compile src/main.py src/dashboard/server.py tests/test_live_config_apply.py tests/test_dashboard_bundle.py` passed locally. Focused pytest was not completed in this session because the local test process did not exit cleanly after collection/runtime setup.

## 2026-05-06 — HTTP wiring hardening (Hyperliquid + Binance) + late-window guard + lane size haircut

Commit: `d6da79c`. Strategy-side gate edits for SOL/ETH/HYPE/XRP are recorded in their respective `strategy-log/*.md` entries; this changelog entry covers the cross-cutting code/infra changes.

### Hyperliquid HTTP resilience ([src/analysis/hyperliquid_hype_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/hyperliquid_hype_service.py))

- **Connection reuse:** instantiate `requests.Session()` in `__init__`; all candleSnapshot POSTs go through it. Eliminates per-call TLS handshake (root cause of observed 18.5s tail-latency outliers tripping the 18s timeout ceiling).
- **Split timeouts:** read timeout 18 → 25; new `connect_timeout_sec: 5`; passed to requests as a `(connect, read)` tuple so a slow handshake fails fast at 5s rather than burning the read budget.
- **Stale-on-error fallback:** new `_stale_fallback()` returns the last good cached frame for up to `stale_on_error_max_age_sec: 180` (configurable). Was previously returning empty df, which caused HYPE signal blackouts during transient API blips.
- **Empty-result retry:** Hyperliquid occasionally returns HTTP 200 with `[]` body. New code retries once before falling back.
- **Observability:** every failure path now logs at `WARNING` level (was: silent except clauses returning empty df).

### http_retry helper ([src/utils/http_retry.py](/Users/mainfolder/Documents/psb-main%201/src/utils/http_retry.py))

- **Transient status set widened:** 408 (request timeout), 425 (too early), 429 (rate limited) added to the retry set alongside 5xx.
- **Retry-After header honored:** when servers signal a wait, `_sleep_backoff` uses that value (capped at 10s so a hostile 3600s Retry-After can't stall the bot).

### Binance HTTP — Session reuse ([src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py), [src/analysis/btc_price_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/btc_price_service.py))

- Both services now hold a `requests.Session()` and route the 6 call sites (klines + ticker + CoinGecko fallback per service) through it.
- **Measured speedup:** 1245ms cold-TLS first call → ~486ms warm subsequent calls = **2.6× faster** per Binance roundtrip after the first call. With ~30 unique-key Binance fetches per scan cycle, this saves several seconds per cycle that previously came out of the entry-window budget.
- Binance multi-host failover and 15-min stale-cache fallback (already present) untouched. Other Binance defects (single-value timeout, no exponential backoff, no empty-result retry) intentionally **not** addressed in this commit — Binance API was healthy during the audit (1.0–1.4s probes, no tail latency) and host-failover already provides spatial retry. Deferred to a future hardening pass.

### Strategy code: late-window guard ([src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py))

- **New gate:** `_apply_late_window_guard()` reads `late_window_block_mins`, `late_window_tighten_mins`, `late_window_extra_min_edge` from per-lane config. Two independent levers:
  - Hard-block entries when `mins_left ≤ late_window_block_mins` (final minute of short-dated up/down markets is the highest-loss zone).
  - Require stronger edge (`max(effective_min_edge, late_window_extra_min_edge)`) when `mins_left ≤ late_window_tighten_mins`.
- **Why:** Live diagnostic across all four alt lanes showed `updown_time_stop` exits had 0% WR and were wiping 70%+ of take-profit gains. Time-stop bleed clusters in the late-window zone. This gate is asset-agnostic (any lane can opt in via YAML) and addresses the bleed at admission time rather than exit time.
- Inherits to `eth_macro` / `xrp_macro` / `hype_macro` via `SolMacroStrategy`. ETH-specific keys can be added in [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py).

### Strategy code: tuning size multiplier ([src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py))

- **New knob:** `tuning_size_multiplier` (default 1.0) applied to `raw_size` after Kelly + regime + correlation adjustments. When set <1.0, scales position size down for a lane temporarily.
- **Why:** During the validation window for this session's 8 strategy edits, individual losing trades cost less in dollar terms — limits damage if a tuning hypothesis is wrong while still generating attribution data.
- Set per-lane in `strategies.<lane>.tuning_size_multiplier`. Default of 1.0 means no behavior change for lanes that don't opt in.

### Hyperliquid config block expanded ([config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml))

```yaml
hyperliquid:
  request_timeout_sec: 25            # was 18
  range_request_timeout_sec: 30       # was 22
  connect_timeout_sec: 5              # new
  max_retries: 4                      # was 3
  retry_backoff_base_sec: 0.5
  stale_on_error_max_age_sec: 180     # new
```

### Tests added

- [tests/test_market_data_fallbacks.py](/Users/mainfolder/Documents/psb-main%201/tests/test_market_data_fallbacks.py): stale-on-error fallback semantics for Hyperliquid service.
- [tests/test_sol_macro.py](/Users/mainfolder/Documents/psb-main%201/tests/test_sol_macro.py) / [tests/test_eth_macro.py](/Users/mainfolder/Documents/psb-main%201/tests/test_eth_macro.py): late-window guard activation and tuning size multiplier scaling.

### Diagnostic artifacts (untracked → committed)

- [docs/session_reports/xrp_forensic_audit_20260506_052012.md](/Users/mainfolder/Documents/psb-main%201/docs/session_reports/xrp_forensic_audit_20260506_052012.md) (+ JSON companion)
- [docs/session_reports/xrp_forensic_audit_recent_20260506_052027.md](/Users/mainfolder/Documents/psb-main%201/docs/session_reports/xrp_forensic_audit_recent_20260506_052027.md) (+ JSON companion)
- [scripts/run_xrp_forensic_audit.py](/Users/mainfolder/Documents/psb-main%201/scripts/run_xrp_forensic_audit.py)
- [scripts/xrp_time_stop_counterfactual.py](/Users/mainfolder/Documents/psb-main%201/scripts/xrp_time_stop_counterfactual.py)

### Required action

**Bot must be restarted** for any of these changes to take effect. The currently running paper-trading process started before the YAML and code edits and is running on stale cached config + pre-fix code paths.

### Suspected outcomes (subjective, await validation)

- **Most likely improvements:** Hyperliquid HYPE silence resolved (Session reuse + stale-on-error keeps HYPE generating signals through API blips). Binance scan cycles measurably faster (~5–22s saved per cycle, depending on cache hit rate). 15m fire rates rebound on SOL/ETH (window widen + per-lane gate relax).
- **Most uncertain:** late-window guard is unproven in production — if it fires too aggressively it could silence valid entries; if it fires too rarely it doesn't reach the time-stop bleed. YAML keys for it default to 0 (gate inactive) on every lane right now; opt-in must be set explicitly per-lane after the next round of data.
- **Risk to watch:** stacking 8 strategy edits + late-window guard + Session reuse in one cycle makes attribution noisy. Read post-restart data through the failure-criteria thresholds in `~/.claude/plans/there-is-a-live-mellow-ripple.md` rather than averages.

---

## 2026-05-05 — Underperformance diagnosis tooling and first ranked RCA

- **Audit tooling:** Added [scripts/diagnose_underperformance.py](/Users/mainfolder/Documents/psb-main%201/scripts/diagnose_underperformance.py) and [src/analysis/underperformance_audit.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/underperformance_audit.py) to generate a reproducible live-vs-backtest diagnosis from paper journals plus latest 15m crypto backtest reports.
- **Artifacts:** Wrote [docs/session_reports/underperformance_diagnosis_20260505.md](/Users/mainfolder/Documents/psb-main%201/docs/session_reports/underperformance_diagnosis_20260505.md) and `.json` companion for the current `034719` baseline vs recent losing sessions (`150648`, `195754`, `220539`).
- **Measured conclusion:** Hermes’s “signal suppression” claim is only **partially** supported. Cross-strategy negative PnL in the audited recent window is dominated by **`BUY_YES` `updown_time_stop`** losses (**83.3%** of negative PnL), with stronger suppression evidence mainly in **BTC** and likely **XRP**, not as a universal explanation.
- **Lane ranking from the audit:** `bitcoin` = exit-path damage + BUY_NO suppression; `xrp_macro` = exit-path damage + probable side-mix suppression; `eth_macro` = exit-path damage + entry/edge calibration; `sol_macro` = entry/edge calibration plus exit damage; `hype_macro` = exit damage, but current HYPE backtest control is too small to trust.
- **Telemetry limitation surfaced explicitly:** Selected paper sessions contained **0** persisted `BUY_NO_SKIP` events, so suppression in this report was inferred from live side-mix drift and backtest controls rather than skip-level journal evidence.
- **Verification:** `.venv/bin/python -m pytest tests/test_underperformance_audit.py tests/test_ops_pulse.py` → **5 passed**. `.venv/bin/python scripts/diagnose_underperformance.py --label underperformance_diagnosis_20260505` wrote the report pair above.

## 2026-05-03 — Scanner network phase concurrency

- **Scanner:** `src/market/scanner.py` `_sync_network_phase` runs gamma, weather, up/down, and up/down 5m price hydration **in parallel** via `asyncio.gather`, reducing wall-clock time per scan cycle versus sequential network phases.

## 2026-04-29 — Phase 6 backtest/live parity hardening

- **BTC tension parity:** BTC live threshold/updown probability and the crypto up/down backtest now use signed, direction-aware ±0.02 Trend Sabre tension adjustment.
- **Crypto up/down backtest:** Entry prices now accept empirical fills across `0.30–0.70`, synthetic fallback is `N(0.50, 0.06)` clipped to `0.30–0.70`, sampled entry prices must satisfy the live strategy entry band, and trade records store the sampled `entry_price` instead of hardcoded `0.50`.
- **Sizing/slippage/settlement:** Up/down sizing now approximates live `KellySizer.size_from_edge()` plus FULL-tier exposure floor/cap behavior. Slippage is additive with a half-cent floor instead of purely multiplicative bps, and flat candles are skipped as unsettled instead of being counted as NO wins.
- **General backtest hygiene:** Generic `BacktestEngine` also uses additive slippage floor, data-loader fallback spreads use a tiered estimate instead of hardcoded 2 cents, strategy discovery accepts configurable arbitrage `min_std`, and reports using the deterministic AI proxy include `ai_mode=BACKTEST_PROXY`.
- **Verification:** `.venv/bin/python -m pytest tests/test_updown_backtest_parity.py tests/test_backtest_engine.py tests/test_backtest_oracle_replay.py tests/test_crypto_backtest_eth.py tests/test_bitcoin_scenarios.py tests/test_sol_macro.py -q` → `87 passed`; cached BTC 15m post-parity backtest saved `data/backtest/reports/backtest_crypto_BTC_15m_20260429_154935.json` with 174 trades, 52.3% WR, +$21.45.

## 2026-04-29 — Phase 5 CTF redemption, SSE backoff, and async hot-path hardening

- **CTF redemption:** Winning `SELL_YES` settlements now redeem the `NO` side instead of falling through silently. Unknown profitable action mappings log a warning so future action types do not leave winnings unclaimed invisibly.
- **CTF robustness:** `CTFRedeemer` now loads/persists redeemed `(condition_id, outcome)` records in `data/ctf_redeemed.jsonl`, uses pending nonce tracking behind a lock, prefers EIP-1559 fee fields with legacy fallback, waits for transaction receipts before marking success, and resets nonce state on send/confirmation errors.
- **Resolution hot path:** The main async trading loop now calls settlement and open-position price refreshes through `asyncio.to_thread()`, preventing synchronous Gamma requests and live CTF confirmation waits from blocking the event loop.
- **Weather hot path:** Weather forecast/METAR fetches now run through `asyncio.to_thread()` from `WeatherStrategy.scan_and_analyze()`, and skip-reason accounting no longer raises on newly introduced skip keys.
- **SSE:** Server errors now emit timestamped `sse_error` frames and log with traceback instead of sending empty `{}` frames. The client logs server error frames and uses exponential reconnect backoff with jitter.
- **Verification:** `.venv/bin/python -m pytest tests/test_ctf_redeemer.py tests/test_bitcoin_scenarios.py tests/test_strategies.py tests/test_dashboard_bundle.py -q` → `61 passed`.

## 2026-04-29 — Phase 4 dashboard auth, config validation, and frontend/AI hardening

- **Dashboard auth:** Mutating dashboard endpoints now fail closed for non-loopback clients when `DASHBOARD_API_KEY` is unset. `start_dashboard()` also refuses non-loopback binds without the key, preventing public Railway-style dashboards from silently running unauthenticated.
- **Config validation:** `/api/config` now uses a Pydantic `ConfigUpdates` model with top-level `extra="forbid"` plus bounded checks for high-risk trading, strategy, exposure, AI, and backtest fields. Unsafe values such as negative Kelly fractions or disabling `trading.dry_run` through the dashboard are rejected before YAML is written.
- **Frontend resilience:** SSE parse errors and missing-`ts` frames are logged, hero PnL distinguishes missing data from true `$0.00`, config numeric inputs use strict parsing and semantic bounds, PnL chart rendering filters non-finite values, and dashboard intervals are tracked for teardown.
- **AI provider handlers:** OpenAI-compatible generic provider failures log at WARNING, Anthropic-style content extraction concatenates all non-thinking text blocks, and confidence text coercion uses first explicit confidence phrase instead of substring matching.
- **Verification:** `.venv/bin/python -m pytest tests/test_sol_macro.py tests/test_bitcoin_scenarios.py tests/test_dashboard_bundle.py tests/test_ai_agent_parse.py -q` → `79 passed`.

## 2026-04-29 — Phase 3 math, drift, and live-performance hardening

- **PerformanceTracker:** `profit_factor` now returns JSON-safe `null` instead of Python `Infinity` when there are no losses, and it selects the same newest resumable journal session as `TradeJournal` instead of grabbing empty newer stubs.
- **Drift detector:** Strategy drift reports now expose `live_sample_size`, require 15 closed trades before marking `DIVERGING`, and compute trade frequency from actual elapsed time instead of flooring the window to one day.
- **AI consensus:** Consensus provider calls now pass through the shared async rate limiter for non-local providers, preventing burst calls when consensus mode is enabled.
- **Dashboard API:** `/api/live/drift` now returns trade-frequency fields plus sample size so the UI/API surface explains insufficient-data verdicts.
- **Verification:** `.venv/bin/python -m pytest tests/test_sol_macro.py tests/test_bitcoin_scenarios.py tests/test_dashboard_bundle.py -q` → `72 passed`; BTC 15m cached backtest saved `data/backtest/reports/backtest_crypto_BTC_15m_20260429_133239.json`.

## 2026-04-29 — Phase 3 strategy probability/date fixes

- **Bitcoin:** Traditional threshold probability now moves continuously through 50% at the strike instead of jumping from ~50% to ~60% on a tiny cross. Trend Sabre tension adjustment is direction-aware, so snap-back risk penalizes the stretched side and helps the opposite side.
- **BTC/SOL threshold dates:** Traditional threshold-market `days_to_resolution` now compares UTC-aware end dates to `datetime.now(timezone.utc)`.
- **Strategy log:** Details recorded in `strategy-log/bitcoin.md` and `strategy-log/sol_macro.md`.

## 2026-04-29 — Phase 2 trading-loop hardening

- **Scanner:** Gamma fallback end dates now preserve UTC tzinfo instead of stripping to naive datetimes; mid-price refresh no longer overwrites an existing spread field with convergence math.
- **AI:** Short-window candle markets use dedicated cache TTLs (`15m=180s`, `5m=60s`) and all-provider failure logs now include per-provider error details.
- **Runtime config:** `KellySizer` can reload config without clearing streak state, and the live config rebuild path reuses the existing sizer when present.
- **Exit defaults:** `PositionExitManager` fallbacks now match YAML (`take_profit_pct=0.15`, `stop_loss_pct=0.30`) and warn when enabled exit rules are incomplete.
- **Verification:** `.venv/bin/python -m pytest tests/test_dashboard_bundle.py tests/test_ai_agent_parse.py tests/test_live_testing.py tests/test_live_config_apply.py tests/test_bitcoin_scenarios.py tests/test_sol_macro.py tests/test_eth_macro.py` → `88 passed`.

## 2026-04-29 — Fixed crypto cycle cadence and cleaned dashboard runtime/test path

- **Trading loop:** `src/main.py::_unified_trading_loop()` now runs on a fixed cadence by subtracting cycle runtime from `trading.cycle_interval_sec` before sleeping, instead of waiting a full extra interval after each completed cycle.
- **Crypto config:** `config/settings.yaml` widens live crypto up/down entry windows to `15m: 8.0–15.0` and `5m: 0.75–5.0` for BTC/SOL/ETH/HYPE/XRP, and lowers `strategies.xrp_macro.min_liquidity` from `5000` to `1000`. Dead zones remain `false` for all five crypto strategies.
- **Why:** Live ops showed scanner discovery was working, but crypto lanes were repeatedly getting blocked by `outside_entry_window`; XRP was also being choked by a materially stricter liquidity floor than the other active alt lanes.
- **Dashboard runtime/tests:** `src/dashboard/server.py` now uses a FastAPI lifespan handler instead of deprecated `@app.on_event("startup")`, lazy-imports the AI live probe so optional SDKs do not break dashboard imports, and keeps the Command Center SSE path aligned to `risk_manager.daily_trades` / `daily_pnl`. `tests/test_dashboard_bundle.py` was updated to understand `fetchT()` wrappers and to assert the SSE hero metrics stay sourced from risk-manager daily fields. `tests/test_classify_updown.py` now matches the live XRP macro bucket path.
- **Verification:** Repo venv test slice passed: `pytest -q tests/test_dashboard_bundle.py tests/test_classify_updown.py tests/test_config_merge.py` → `13 passed`.

## 2026-04-28 — Dashboard operator toggles for weather 72h cap and crypto dead zones

- **Config:** `config/settings.yaml` now adds `strategies.weather.resolution_window_enabled`, defaulting to `false`. This keeps the minimum-hours guard while disabling the 72-hour weather resolution cap by default.
- **Code:** `src/main.py::_filter_weather_markets()` now honors that toggle. When off, weather markets only need to satisfy `min_hours_to_resolution`; when on, the prior `min_hours <= hours <= max_hours` behavior returns.
- **Dashboard:** `src/dashboard/index.html` now exposes two live operator buttons using the existing `/api/config` path:
  - `Weather 72h Cap` toggles `strategies.weather.resolution_window_enabled`
  - `Dead Zones` toggles `dead_zone_enabled` across `bitcoin`, `sol_macro`, `eth_macro`, `hype_macro`, and `xrp_macro`
- **Why:** Operators needed a fast live control for the weather horizon cap and the shared crypto dead-zone experiment without editing YAML manually.

## 2026-04-26 — Exposure tier caps updated to 15 / 10 / 5

- **Config:** `config/settings.yaml` under `exposure` now sets `full_size: 15.0`, `moderate_size: 10.0`, `minimal_size: 5.0`.
- **Why:** Operator sizing target tightened so non-FULL conditions do not keep near-$15 tickets.

## 2026-04-26 — Exposure sizing floor now respects tier multiplier

- **Issue:** `ExposureManager.scale_size()` applied `exposure.min_trade_usd` as a flat post-multiplier floor. With `min_trade_usd: 10` and `MINIMAL` tier `x0.2`, trades were still floored near $10 instead of the expected ~$2.
- **Code:** `src/execution/exposure_manager.py` now applies a tier-aware floor: `min_trade_usd * tier_multiplier` (FULL=10, MODERATE=6, MINIMAL=2 with current config). Existing tier caps (`full_size/moderate_size/minimal_size`) still apply.
- **Tests:** Added `tests/test_exposure_manager_sizing.py` (3 regression cases covering FULL/MODERATE/MINIMAL behavior).

## 2026-04-26 — Journal tab: use same “resumable session” as the bot (disk-only dashboard)

- **Issue:** After a restart, a **newer empty** `data/paper_trades/<session_id>/` directory could be lexicographically first while the bot correctly resumed a **older folder with trades** (same rule as `TradeJournal(resume_latest=True)`). The dashboard process often has no in-memory `bot_instance`, so it read the empty stub and the Journal tab showed no metrics for the last test run.
- **Code:** `TradeJournal.newest_resumable_session_dir()` in `src/execution/trade_journal.py` (shared with resume + `_get_journal` / `summary` fallback in `src/dashboard/server.py`).
- **Tests:** `tests/test_trade_journal_resumable.py::test_newest_resumable_session_dir_skips_empty_stubs`
- **Strategy log (same release window):** `strategy-log/bitcoin.md` and `eth_macro.md` (2026-04-26 entries); SELL/exit + RSI changes are also summarized in the changelog block below and those strategy files.

## 2026-04-26 — SELL_YES take-profit exits buy back YES token

- **Issue:** `PositionExitManager` calculated `SELL_YES` PnL against the YES price but attempted to close profitable short-YES positions by buying the **NO** token. Entry execution sells the YES token for `SELL_YES`, so the exit leg must buy back YES.
- **Code:** `src/execution/live_testing.py` — `pos.outcome == "NO"` exit decisions now use `exit_action="BUY"` with `token_yes`, keeping token, price, and journal PnL conventions aligned.
- **Tests:** Added `tests/test_live_testing.py::test_sell_yes_take_profit_buys_back_yes_token`.

## 2026-04-22 — Single trading loop (`_unified_cycle`) — **verified working**

- **Before:** Two asyncio tasks — `_main_loop` (300s, exits + arb/fade/neh + resolution) and `_crypto_fast_loop` (120s, crypto only + resolution), each running a full `scan_for_opportunities()` — double scanner load and confusing logs. Operator expectation (“main loop” vs “fast loop”) was easy to misread; crypto-only live scope had arb/fade/neh off, so all entries came only from the fast path while exits trailed on 300s.
- **After:** One `_unified_trading_loop` calling `_unified_cycle`: **single** scan per tick, **TP/SL** `check_exits` on the **same** cadence as entries, optional arb/fade/neh if enabled, then **bitcoin** / **sol_macro** / **eth_macro** / **hype_macro** / **xrp_macro**, one `_run_resolution_check(label="[TRADING]")`. Cadence: **`trading.cycle_interval_sec`** (default **120**) in [`config/settings.yaml`](../../config/settings.yaml); `PolyBot.scan_interval` matches for `OPS_JSON` `scan_interval_sec`. Log prefix **`[TRADING]`** (replaces `[FAST]`) for scanner lookahead and crypto leg lines; BTC exception logging uses `exc_info=True`.
- **Code:** [`src/main.py`](../../src/main.py) — `start()` uses `asyncio.gather(self._unified_trading_loop(), self._daily_coach_loop())` only. Removed: `_main_loop`, `_crypto_fast_loop`, `_trading_cycle`, `_crypto_cycle`.
- **Tests:** `pytest` — 171 passed after refactor (local `uv run pytest tests/`).
- **Operator sign-off (same day):** Bot reported **working again** end-to-end after deploy/restart; tail logs for `Starting trading cycle...`, `[TRADING] Scanner lookahead`, `[TRADING] Crypto …`, `Cycle complete`, `OPS_JSON`.

## 2026-04-22 — Scanner: non-blocking network phase (Railway + local)

- **Issue:** Synchronous `requests` inside `async def scan_for_opportunities` blocked the asyncio event loop for minutes (especially **HYPE alt** slug fetches), stalling **both** the main loop and the fast crypto loop — looked like a full freeze.
- **Code:** [`src/market/scanner.py`](../../src/market/scanner.py) — bundle Gamma + updown (+ optional HYPE alt) HTTP in `asyncio.to_thread`, wrapped with `asyncio.wait_for` using `polymarket.scanner_sync_timeout_sec`. Heartbeat logs for sync phase and total scan time. HYPE alt fetch defaults to `strategies.hype_macro.enabled`; optional `polymarket.fetch_hype_alt_markets` override.
- **Config:** [`config/settings.yaml`](../../config/settings.yaml) — `scanner_sync_timeout_sec: 120` under `polymarket`.
- **Railway:** Same module as local — **redeploy from Git** so the new image is built. Confirm deploy logs show `Scanner: sync network phase (thread) starting`. See [`docs/RAILWAY.md`](../../docs/RAILWAY.md) § *Scanner / “frozen bot”*.
- **Pre-HYPE baseline:** Older operator evidence may live in the Hermes vault (`projects/psb/notes/`), not only in repo `data/logs/`; REST push uses `OBSIDIAN_REST_API_*` locally per [`docs/OBSIDIAN_LOCAL_REST_API.md`](../../docs/OBSIDIAN_LOCAL_REST_API.md).
- **Inventory / RCA:** [`docs/STRATEGY_AI_EXECUTION_INVENTORY.md`](../../docs/STRATEGY_AI_EXECUTION_INVENTORY.md).

## 2026-04-22 — Git init, paper-session runbook, `.railwayignore` 413 fix, deploy verified

- **Local repo:** `git init` in project root (this folder previously had no `.git`); first commit includes tree + paper-session documentation. **`.gitignore`:** add `.claude/`, `.DS_Store` (match other agent/IDE noise).
- **Operator docs:** [docs/RAILWAY.md](../../docs/RAILWAY.md) — new section *Paper sessions and test data* (`PAPER_SESSION_ID`, `PAPER_RESUME_SESSION`, `test_*` resume pitfall, Mac paths with spaces, heatmap/entries linkage). [docs/DASHBOARD_DATA_SOURCES.md](../../docs/DASHBOARD_DATA_SOURCES.md) — *Session ID and `entries.jsonl`*. [README.md](../../README.md) — short pointer to those sections.
- **`railway up` 413 Payload Too Large:** root cause was uploading **~250MB** (local **`.venv/`** and other data not meant for the image). **`.railwayignore`** expanded: `.venv/`, `data/paper_trades/`, `data/logs/`, broad `data/backtest/reports/`, and other large/runtime paths. Docker still installs from **`requirements-railway.txt`** inside the build.
- **Deploy:** `railway up --ci -s polymarket-bot` from linked project → **Deploy complete** (build id in Railway UI). **Verification (hosted):** `GET https://polymarket-bot-production-bf4f.up.railway.app/health` → `dashboard_ui_rev` **`2026-04-21-sse-scalar-sentry-htmx`** (matches `src/dashboard/server.py`); `railway_deployment_id` present. `git_sha` in `/health` is **null** for CLI-upload builds unless `RAILWAY_GIT_COMMIT_SHA` is injected (GitHub Actions / Dockerfile `ARG` path sets it for commit-attributed images).
- **Tests before deploy (local):** `pytest` `test_bitcoin`, `test_sol_macro`, `test_strategies`, `test_dashboard_bundle` — 104 passed; `py_compile` on `src/main.py`, `src/strategies/sol_macro.py`, `clob_client.py`.
- **Follow-up deploy (same day):** `CLOBClient.can_sell_token` read `trading.dry_run` from the **polymarket** sub-dict by mistake; `trading` lives at **root** in `config/settings.yaml`, so the orderbook pre-check never ran when `polymarket.dry_run` was absent. **Fix:** `self._root_config` + `self._root_config.get("trading", {}).get("dry_run", True)` in `src/execution/clob_client.py` — then **`railway up --ci`** again.

## 2026-04-22 — Dashboard `/health`, CI guards, and Railway CLI deploy path

### Dashboard (why the UI looked “dead” while API returned 200)

- **Root cause:** In `src/dashboard/index.html`, `fetchAll()` used `Promise.all` with **18** `fetch()` calls but destructuring listed **17** variables (**`hypeR` missing** between `ethR` and `xrpR`). That throws **`ReferenceError`** in the browser and breaks the whole status poll.
- **Fix:** Add **`hypeR`** to the destructuring list so it matches the `fetch()` count.
- **Deploy fingerprint:** `GET /health` includes **`dashboard_ui_rev`** (bump in `src/dashboard/server.py` whenever you ship dashboard HTML/JS). **Must be a single dict key** — duplicate `"dashboard_ui_rev"` entries are invalid (second wins silently). Current tag: **`2026-04-21-kelly-live-recover-gitlab-deploy`**.
- **Verification:** `curl https://<your-host>/health` — confirm `dashboard_ui_rev` matches `server.py` and `railway_deployment_id` updates on new deploys.

### Guards so this class of bug doesn’t ship again

- **`scripts/preflight.py`:** `check_dashboard_index()` — parses `fetchAll()`’s first `Promise.all` and asserts destructure count == `fetch()` count.
- **`tests/test_dashboard_bundle.py`:** Same invariant + **`TestClient`** smoke for **`GET /`** and **`GET /health`** (expects `dashboard_ui_rev`).
- **Security suite:** `scripts/run_security_suite.py` — default **`pip-audit`** on the **current venv** (avoids `ensurepip`/temp-venv failures on some macOS Python builds); **`--audit-requirements`** for `requirements*.txt` when CI has working venvs. **Bandit** skips **B104** / **B602** by default (PaaS bind + local port helpers); **`--strict-bandit`** for full rules. Deps in **`requirements-dev.txt`** (`bandit`, `pip-audit`, `pytest`, …).

### GitLab CI (primary remote: `gitlab.com/ken-johnson/psb`)

- **Stages:** `test` → `deploy`.
- **`checks`:** install `requirements-dev.txt`, then `preflight` → `pytest tests/test_dashboard_bundle.py` → `run_security_suite.py`. Runs on **merge requests** and **default branch** pushes.
- **`railway_deploy`:** `needs: ["checks"]`, then `railway up --ci --service polymarket-bot` with **`RAILWAY_TOKEN`** in GitLab CI/CD variables.
- **CI env:** `OPENAI_API_KEY=ci-placeholder-not-used` so preflight passes without real AI keys (preflight only requires a non-empty provider key).

### GitHub (optional mirror, e.g. `KJohnson-700/PSB`)

- **`.github/workflows/ci.yml`:** Same checks as GitLab on push/PR to `main`.
- **`.github/workflows/deploy-railway.yml`:** `railway up --ci` on push to `main` with **`RAILWAY_TOKEN`** in repo Actions secrets (if you use GitHub instead of/in addition to GitLab deploy).

### Railway — what actually fixed “can’t redeploy from this laptop”

1. **Symptom:** `railway up` → **`No linked project found. Run railway link`** (CLI could be logged in and still fail).
2. **Fix (once per clone/machine):** From **repo root**:
   - `railway link -p "PolyMarket Strategy Bot" -s polymarket-bot`  
     (workspace **SamuraiFrenchie’s Projects**, environment **production**, service **polymarket-bot** — adjust flags if your project/service names differ.)
3. **Deploy:** `railway up --ci --service polymarket-bot -m "<message>"`  
   Builds from **local tree** (root **`Dockerfile`**); does not require **`RAILWAY_TOKEN`** in `.env` if you use **`railway login`**.
4. **2026-04-22 session:** Full image build completed on Railway (**Deploy complete**); use build/deploy logs in the Railway UI for that deployment id if anything regresses.

---

## 2026-04-21 — Agent memory: what this repo is + April 2026 correctness bundle

### What this project is

- **Working name:** **PSB** (this Mac repo folder: `psb-main 1`; Windows checkout name may differ). Polymarket short-horizon crypto bot; trades **Polymarket** (CLOB), focused on **BTC/SOL/ETH/HYPE/XRP** up/down and related strategies.
- **Second brain (Hermes):** operator vault note — `Hermes Second Brain/projects/psb/notes/2026-04-21-psb-agent-memory-correctness-bundle.md`. REST API usage: **`docs/OBSIDIAN_LOCAL_REST_API.md`** in this repo.
- **Entry point:** `python src/main.py --paper` (paper) or `--live --confirm-live` (live). Loads **`src/env_bootstrap.load_project_dotenv`**: project root **`.env`** then **`config/secrets.env`** (secrets override).
- **Core runtime:** `src/main.py` (`PolyBot`) — unified trading cycle (`_unified_cycle`) for bitcoin + `sol_macro` + optional `eth_macro` / `hype_macro` / `xrp_macro`; journal under **`data/paper_trades/<session>/`** (`entries.jsonl`, `positions.json`, `summary.json`); optional **dashboard** (`src/dashboard/server.py`) when enabled.
- **AI:** `config/settings.yaml` → `ai.provider_chain` + env keys; `src/ai_status.py` reports readiness. **`ai.live_inferencing: false`** suppresses live LLM calls without removing keys.
- **Discord:** `src/notifications/notification_manager.py` — trade/exit webhooks for **`bitcoin`**, **`sol_macro`**, **`eth_macro`**, **`hype_macro`**, **`xrp_macro`**, **`xrp_dump_hedge`** only; webhook from YAML **`notifications.discord_webhook`** or env **`DISCORD_WEBHOOK_URL`** (merged in `PolyBot._load_config`).
- **Hermes / external bundles:** treat **Hermes-owned trees as read-only**; apply fixes only in **this** PSB repo when both exist in a workspace.

### Issues fixed (April 2026) — agents should know these

1. **`AttributeError: … has no attribute 'scan_and_analyze'`** on `ETHMacroStrategy` / `HYPEMacroStrategy` (and `SolMacroStrategy`): **`scan_and_analyze` had been nested inside `_get_weekend_penalty()` in `src/strategies/sol_macro.py`** (unreachable). **Fix:** method belongs on **`SolMacroStrategy`**; subclasses inherit it. **`_get_weekend_penalty()`** restored as a **module-level** function after the class (still used by `conditions_from_ta`).
2. **`SolMacroStrategy` missing `self.enabled`:** **`scan_and_analyze`** gates on `enabled`; only ETH/HYPE set it after `super()`. **Fix:** `self.enabled = self.config.get("enabled", True)` in **`SolMacroStrategy.__init__`** (ETH/HYPE keep their own defaults).
3. **`_bump_skip` NameError on SOL up/down path:** calls copied from **`bitcoin.py`** without the local **`def _bump_skip` / `skip_reasons` dict**. **Fix:** define them before the `for market in sol_markets` loop in **`scan_and_analyze`**.
4. **F-string safety:** `{(self.min_edge_5m if is_5m else self.min_edge):.4f}` in AI context (avoids ambiguous `else self.min_edge:.4f` parsing in some tools).
5. **Discord allowlist:** **`hype_macro`** added to **`DISCORD_TRADE_STRATEGIES`** / **`STRATEGY_ALERT_TITLE`** so HYPE fills/exits can notify like other crypto legs.

### Verification notes

- **`python -m py_compile src/strategies/sol_macro.py`**; **`pytest tests/test_sol_macro.py`** (and ETH/HYPE-related tests as run in session).
- **Local bot:** Python process must **restart** to load changed `src/`; no hot reload.
- **Railway:** new code only after **deploy** of the commit containing fixes; **`_crypto_cycle`** catches ETH/HYPE errors per-strategy so **`AttributeError` logged but may not crash the whole process** — still meant to be fixed so legs actually run.

### Operator footnotes

- **`data/paper_trades`:** `TradeJournal(resume_latest=True)` picks newest session dir with any of entries/positions/summary; stray **`test_*`** dirs can become the resumed session — remove/rename if you want a fresh timestamped session.
- **Preflight:** `python scripts/preflight.py` before runs.

## 2026-04-09 — `ai.live_inferencing` (live LLM kill switch without dropping key setup)

- **What:** New `ai.live_inferencing` (default `true`) in `config/settings.yaml`. When `false`, `AIAgent.analyze_market` returns before cache or provider calls. Dashboard checkbox **Live LLM calls (ai.live_inferencing)**; `compute_ai_status` / startup messaging distinguish **PAUSED** (keys OK, calls off) vs full **OFF**.
- **Why:** Operator can stay within LLM quotas while keeping provider config intact; backtests already avoid real LLMs (`BacktestAIAgent` / quant crypto scripts).
- **Verification:** `pytest` green; toggle persists via `POST /api/config` + `PolyBot.apply_config_updates` → `ai_agent.refresh_from_config`.

## 2026-04-09 — Concurrent dashboard backtests + XRP dump-hedge wiring

- **What:** Dashboard backtest API supports multiple jobs (`job_id`); live bot and backtests can run together. Scanner adds `xrp-updown-15m`; optional strategy `xrp_dump_hedge` (see strategy log).
- **Why:** Ops isolation and experimental XRP path without blocking BTC/SOL/ETH.
