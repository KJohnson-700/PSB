# Scope — AI Shadow Exit-Manager (paper/offline, champion-challenger)

**Status:** SCOPE ONLY — nothing built, nothing deployed. Gated on operator GO + Codex.
**Date:** 2026-08-02. Owner doc; companion: `docs/PSB_RESEARCH_ROADMAP.md` §6.

## 1. Goal & success metric

Answer, at **zero live-money risk**: *would an AI's per-lane, tape-aware EXIT calls beat
our current static stops?* Success = a measured, per-lane **$ delta** of the AI exit policy
vs the static exit (champion), on the same closed trades, with a small-sample-safe CI
(Wilson / Bayesian, per Script B discipline). If it doesn't beat static in paper, it never
graduates. The target leak it's chasing: the **$1.2k ETH give-back** (stops cutting green
trades) + wrong-side bleed.

## 2. The constraint that shapes the whole design

The live exit decision is `PositionExitManager.check_exits()` in
`src/execution/live_testing.py`, driven by `_run_exit_checks` / `_fast_exit_loop` /
`_handle_exit_decision(ExitDecision)` in `src/main.py`. **Both files are Cursor-owned —
off-limits to edit/commit, and edits are restart-class.** Therefore the shadow must be
**out-of-process / offline**, consuming existing telemetry — exactly the proven
`ghost-settler` / `taken_exit_settler` pattern. No live-path edits, no restart, no Cursor
files touched, no order path.

## 3. What we already own (reuse, don't rebuild)

| Asset | Gives us |
|-------|----------|
| `data/calibration/exit_excursion_shadow.jsonl` | per-position excursion ENVELOPE: `exit_mfe/mae_pct` (at actual exit), `full_mfe/mae_pct` (whole life to settle), `n_shadow_samples` |
| `src/analysis/taken_exit_settler.py` | actual exit PnL + hold-to-resolution counterfactual (`hold_minus_exit`) per trade |
| `data/calibration/trades.jsonl` | `mfe_pct`, `mae_pct`, `exit_reason`, `secs_to_expiry_at_exit`, notional |
| `src/analysis/window_watch.py` | near-window open-position tracking scaffold |
| `data/calibration/tape_map.jsonl` | per-asset tape state (direction/strength/vol/conf) — the "tape" the exit should read |

## 4. Data-sufficiency finding (the fork in the road)

We have the **envelope**, NOT the **ordered per-tick price path**. The excursion shadow
sampled 156 marks but persisted only aggregates (mfe/mae at exit + full). Consequences:

- **Coarse exit-policy comparison IS feasible now** (hold vs cut vs wider-stop vs time-exit),
  because full_mfe/mae + settled outcome bound what each policy family would have realized.
- **A true tick-by-tick AI exit manager is NOT faithfully simulable yet** — reacting to the
  path shape + evolving tape each tick needs the ordered `(t, price, tape_state,
  secs_to_expiry)` series, which we don't persist.

→ Two tiers.

## 5. Tier 0 — offline exit champion/challenger (build first, cheap, zero live touch)

Essentially **Script E with a pluggable policy slot**. For each closed trade, from the
envelope + settled outcome, score what each policy family WOULD have realized:
`static (actual)` · `hold-to-resolution` · `wider-stop (≥2·ATR)` · `time-exit` · `MFE-lock
(trail at k·MFE)` · **`ai_heuristic` (tape-conditioned rule: hold in trending tape matching
side, cut in FLAT/chop)**. Output: per-lane × per-tape $ delta vs static, CI-ranked.

- Answers "does a tape-aware exit STYLE beat static" at the envelope level.
- No new data, no live code, no restart. Read-only over existing JSONL.
- The `ai_heuristic` slot can be a coded rule first; an actual LLM call can score the same
  cases in batch offline (cheap, no live latency) to compare LLM-judgment vs the coded rule.

## 6. Tier 1 — real tick-by-tick AI exit manager (needs a data-loop first)

Prereq **data-loop (owned module, NOT Cursor's):** persist the ordered per-tick path for
open positions — extend the shadow writer *we own* (e.g. a new `src/analysis/
exit_path_shadow.py`, or extend the `tape_entry_shadow` we built) to append
`(trade_id, t, mark_price, secs_to_expiry, tape_state)` each tick. If the only place that
sees the live mark is the Cursor exit loop, coordinate a **one-line** hook with Cursor (they
own that file) — do not edit it unilaterally.

Then the **AI exit policy** (offline replay of the path):
- **Inputs per tick:** entry, side, current mark, excursion-so-far (mfe/mae), secs-to-expiry,
  lane, **tape-map state**, realized-adapter delta for the lane.
- **Output:** `ExitAction ∈ {HOLD, TAKE_PROFIT, CUT, TRAIL(k), TIME_EXIT}` + one-line reason.
- **Champion** = the static `check_exits` decision (already logged as `exit_reason`).
  **Challenger** = AI. Log both to `exit_ai_shadow.jsonl` keyed by `(trade_id, tick)`; settle
  both PnL via the existing settler. Never acts.
- Graduation ladder: offline replay → live paper-shadow (proposes, logs, never executes,
  behind an Aegis-style kill-switch) → (only if it beats static in paper, with GO+Codex)
  a per-lane, human-approved live exit assist. **Live money is the last rung, never the first.**

## 7. Guardrails (non-negotiable)

Paper/offline only; NO order path in Tier 0/1; never edit `main.py`/`live_testing.py`
(Cursor-owned, restart-class) — stay out-of-process; per-lane, side-isolated; measured vs the
static control with small-n CI; ghost data quarantined; Codex-review any code that could ever
touch a live decision; no restart without operator GO.

## 8. Build order

1. **Tier 0** champion/challenger evaluator (= Script E + policy slot). Read-only. → GO.
2. Data-loop: owned per-tick path writer (coordinate the one-line live hook w/ Cursor if
   needed). Logging-first.
3. **Tier 1** offline AI exit policy over the path; `exit_ai_shadow.jsonl`; settle both.
4. Live paper-shadow (propose-only, kill-switch). Measure vs static.
5. Decision gate: only if it beats static in paper → scope a human-approved live assist.

## 9. Off-the-shelf options (research complete 2026-08-02)

**No drop-in exists** — nobody ships an AI shadow-exit-manager for a bespoke prediction-market
venue. We already own the two hardest-to-source assets (the Polymarket execution seam + per-
trade MFE/MAE excursion logs), which inverts build-vs-buy: build thin, borrow patterns.

Closest references (STUDY, do not fork):
- **guberm/polymarket-bot** (7★, MIT) — Claude-driven exit RE-ESTIMATION on Polymarket:
  re-reads the position when price moves >10% and decides hold-vs-cut (+ >25% stop-with-
  reestimate, ≥0.95 TP, edge-gone). The exact "AI re-reads position" brain we want for the
  Tier-1 policy. Tiny/unproven, and it ACTS (no shadow comparison). Lift the LOGIC pattern.
- **Homerun / braedonsaunders/homerun** (162★, **AGPL-3.0**) — mature Polymarket+Kalshi
  platform with a real `mode="shadow"` vs `mode="live"` toggle, a Cox-hazards fill SIMULATOR,
  and a `should_exit` pipeline. Best shadow/live scaffolding on our venue — but AGPL would
  infect our private bot and its exits are strategy-coded, not LLM. Study the shadow-fill
  model + pipeline; don't fork.
- **HKUDS/Vibe-Trading** (29.3k★, MIT) — "Shadow Account": replays what you *should* have
  traded, reports P&L left on the table from early exits. Pattern reference for our
  counterfactual-P&L report half.
- **DataRobot champion/challenger** (MLOps pattern) — the canonical "run challenger in shadow,
  simulate what it would've done, never touch the real env" vocabulary. champion=static stop,
  challenger=AI exit.
- **Exit-callback SHAPE** to model our AI call on: freqtrade `custom_exit()`/`custom_stoploss()`
  (cleanest per-trade seam) + NautilusTrader position hooks. CEX-only — template, not adoptable.
- **No Polymarket exit MCP exists**; Alpaca/ccxt MCPs are tool-schema templates only. No Claude
  Code plugin does exit management. RL exit libs (FinRL/gym-trading-env) = heavier, offline-
  eval-fragile (our memory already distrusts backtest/ghost) — revisit only after the LLM
  shadow proves the concept, to distill a cheaper policy.

**Decision:** BUILD the thin custom shadow harness (§5–§6). Borrow: counterfactual-P&L report
shape ← Vibe-Trading; champion/challenger discipline ← DataRobot pattern; AI re-estimation exit
logic ← guberm as the initial Tier-1 policy; shadow-fill realism ← read Homerun (AGPL, no fork).
