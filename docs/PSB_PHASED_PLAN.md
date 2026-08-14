# PSB Recovery — PHASED PLAN (2026-08-07)

Rule for every phase: ONE change at a time → shadow/measure → keep if better, REVERT if worse → log to EDIT_LEDGER. No stacking. No blind flips (n6058 refuted that). Fallback if Phase 0 can't get healthy frequency: revert whole config to +869/Olympus baseline and rebuild from clean.

Current state (why we're here): bot barely trades (3 trades/session) — starved by the ACCRETED gate stack: neutral_bias sit-out (549 skips), narrowed entry windows (454), disabled long lanes (560), centered-price (303). tape_map is a 3-indicator LAGGING vote (MACD+EMA+trend) = ~53%, only good in stable trend, wrong at the tape change. The overcoding muddies even that.

---

## PHASE 0 — TRADE PROPERLY AGAIN (frequency + not-junk). GOAL: healthy trade count (~50/session) on a clean config, WITHOUT trading more coinflips.
The starvation is the accreted gates, so un-choke toward where edge is real (in-trend, non-flat), not blanket-open:
- 0a. **neutral_bias (549)** — biggest. Turn on `alt_neutral_tape_backup` + `alt_1h_allow_neutral`: when the MACD-vote is NEUTRAL, trade the *tape* direction instead of sitting out. (Built already.) Measure freq + WR.
- 0b. **entry windows (454)** — restore toward +869 baseline (they were slashed 150→35 / 360→60 in the drift; pure frequency, no WR cost per operator).
- 0c. **disabled longs (560)** — re-enable the long lanes the resolver correctly wants IN AN UP-TAPE (tape-gated so they only fire in real up-trend, not flat).
Do 0a → measure → 0b → measure → 0c → measure. Each revertible. STOP if WR craters (means we're un-gating coinflips, not edge).
**Exit criterion:** ~40-50 trades/session AND WR not worse than baseline.

## PHASE 1 — FIX THE TAPE_MAP SIGNAL (the 53% → the real WR lever).
tape_map.py:115 is 3 LAGGING votes → late at the tape change (exactly the operator's point). Add faster/leading turn-detection so it's near-certain in-trend and degrades ONLY at the transition. Build as SHADOW, score vs realized (ai_direction_score), promote only if it beats the 53% at n>=30. qwen-vision (chart read) is a candidate leading signal — score it here.

## PHASE 2 — STRIP THE OVERCODING (recover the Olympus-clean direction path).
From the 3-mapper audit + Codex: remove/relabel the accreted direction junk ONE Codex-verified item at a time — the mislabeled live "shadow" (bitcoin.py:465), the 3 overlapping short-into-bull suppression guards, the overbought-fade-vs-veto conflict, dead code (dashboard), 12 vestigial config keys. Diff current direction path vs OLYMPUS_LIVE_REVIEW.md to target what got layered on. Measure tape_map hit-rate stays/climbs after each removal. (Codex already NO-GO'd bulk deletion — per-item only; eth size_multiplier is load-bearing, exit-code deletion is coupled.)

## PHASE 3 — EXIT / FEE / HORIZON (the net-of-fee lever, per n6058 note).
Direction isn't the leak — the leak is over-trading 5m/15m where the thin ~53% signal minus 0.07x2 taker fee goes NET-NEGATIVE. Concentrate on the 1h horizon (tape_map 56-60% there, beats fees); quantify under-trading of 1h; fix exit (hold_all vs loser-floor policy) + maker-first fee reduction.

## PHASE 4 — SIZING (LAST, only after direction+freq+exit are right).
BUG-A done (BTC flat). Then size winners up / losers down on REALIZED edge (adaptive_lane_sizer), never est_prob. Equal notional both sides.

---
Log each step to EDIT_LEDGER with confirm-metric + revert. Hermes vault handoff at each phase boundary.
