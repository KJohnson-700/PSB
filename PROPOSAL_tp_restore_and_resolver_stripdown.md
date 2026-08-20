# PROPOSAL — TP restore + resolver strip-down (needs Codex second opinion before ship)

Context: paper `--paper` dry_run bot. Edge PROVEN (Olympus live + 48–65% WR July sessions). CURRENT regression: ~40% WR = BELOW coinflip = side-selection is anti-predictive (you can't underperform random without a defect). Payoff geometry is healthy (wins ≈ losses ~$11–12) so ~47–50% WR prints. Two regressions vs the winning `da6722ca`/+$869 config (`bak_pre_B1_htfboost_20260720`):

## FINDING 1 — Take-profit stopped banking winners
- Winning setup: 83–100% of winners banked by TAKE-PROFIT (vault playbook 2026-07-22).
- Now: winner exit reasons over last 12 sessions = `updown_expired` 39 (hold-to-resolution) + `take_profit_giveback` 17 (the trail I ripped this session). **ZERO regular `take_profit` exits.**
- Mechanism: `updown_hold_winners_to_resolution=true` DISABLES the regular TP (live_testing.py:318). Green winners then hold into a fresh coinflip at resolution instead of banking.
- The +869 config had per-lane clean TP: xrp5m `take_profit_pct: 1.0` + hold=false; btc15 `0.5`; xrp1h|up `0.4` + hold=false ("hold=true GATES BOTH exits: TP never fires").

### Proposed change 1 (per-lane, proven engine lanes only)
Restore clean per-lane take-profit + hold=false on the vault's leaderboard engine lanes (xrp5m up/down, hype5m up/down, btc5m/1h up, xrp1h down), matching the +869 levels (TP 0.4–1.0). NOT the feed-blind `tp_giveback` trail. Leave other lanes as-is. Reversible per-lane.

## FINDING 2 — Resolver overcoding buried the winning per-lane calibration
- +869 winning config: 57 direction keys of SURGICAL per-lane calibration (eth symmetric-reset `entry_admission_calibration_shrink: 1.0`, `min_est_prob_conviction_buy_yes: 0.0`, `require_macd_for_bearish_bias: false`, per-lane min_edge 0.03–0.12, fade_regime_windows tuning), each with break-conditions.
- Current: 76 direction keys — the +869 calibration overwritten/buried under BLUNT global gates added 08-07→08-10: eth `momentum_confirm` buy_yes/buy_no ['5m','15m','1h'], `regime_fade.enabled`, `require_tape_direction` (I set false this session), `rsi_fade` (I set false). These are the 08-06-audit "overcoding."
- The blunt gates block per side globally (momentum_confirm blocks the side fighting MACD; regime_fade benches <48% WR) — plausibly forcing the anti-predictive side.

### Proposed change 2 (strip accretion, restore winning calibration)
Strip the 08-07→08-10 blunt direction gates (eth momentum_confirm both sides back to [], regime_fade off, rsi_fade off, require_tape_direction off) and restore the +869 per-lane direction calibration. KEEP the current good payoff geometry (favorite killed, size 25, per-lane exits) + the lane allowlist. Goal = +869 WR × current payoff geometry.

## Questions for Codex
1. Change 1: is restoring per-lane clean TP (hold=false + TP 0.4–1.0) on the engine lanes correct, or does it re-open a known failure mode? Any lane where hold-to-resolution is genuinely better?
2. Change 2: which specific 08-07→08-10 accretions are safe to strip vs which earned their place? Is wholesale restore of the +869 per-lane direction block right, or should it be incremental (one gate at a time)?
3. Sequencing/risk: both at once, or TP first then resolver? What's the smoke-test break-condition for each?
4. Anything in the below-coinflip WR that neither change addresses (e.g. entry TIMING / repricing-lag, not side-selection)?
