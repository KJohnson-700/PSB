# CLAUDE.md — psb-main

## Current project phase: CALIBRATION / DATA GATHERING

Priorities, in order:

1. **Increase trade frequency.** The bot needs entries to generate calibration data. A 24h+ zero-trade window on any asset is a red flag, not "working as designed."
2. **Improve accuracy and profitability through DATA.** Find each asset's lane sweet spot by observing what actually trades and what wins — not by tightening from first principles.
3. **Per-asset lane optimization.** BTC, ETH, SOL, XRP, HYPE, DOGE, BNB each need their own tuned settings. Do not assume one asset's gate logic transfers to another without evidence.

## What NOT to propose

- Tightening gates, raising `min_edge`, narrowing entry windows.
- Adding new restrictive gates.
- Saying a lane is "working as designed" when its problem is *zero trades*.
- Tuning lanes that already work (e.g. HYPE 15m up). See `~/.claude/projects/.../memory/feedback_no_tightening_dont_tweak_winners.md`.

## How to diagnose a starved lane

1. **Verify with data first.** Count rejections in `data/calibration/rejected_candidates.jsonl` by `(strategy, window, side, reason)` for the relevant window. Get `htf_bias` distribution.
2. **Look for structural contradictions.** Common pattern: bias classifier says one direction, momentum gate blocks that direction, every entry rejected. Example: BTC 2026-05-20 — BEARISH bias 94.6% of the time but 4H histogram rising blocked every SHORT.
3. **Propose loosening, not tightening.** If a gate breaks a tie between contradicting signals by rejecting, convert it to a soft penalty or remove the contradiction at its source.
4. **Don't reverse-engineer from logs without checking the code path.** `est_prob_up` may be calibrated, not raw. The edge formula is absolute (`est − yes` for BUY_YES; `yes − est` for BUY_NO) — not relative.

## Repo layout notes

- Strategy entry logic: `src/strategies/{bitcoin,eth_macro,sol_macro,xrp_macro,hype_macro,doge_macro,bnb_macro}.py`. ETH macro duplicates the scan loop — sol_macro gate changes need a separate ETH port.
- Shared BTC 5m math: `src/strategies/btc_updown_5m.py`.
- Live rejection log: `src/analysis/rejected_candidate_log.py` (hardened with RLIMIT_NOFILE bump + in-memory fallback buffer).
- Calibration data: `data/calibration/{trades,rejected_candidates,rejected_candidates_settled,lane_posteriors}.jsonl`.
- Config: `config/settings.yaml`.

## Working rules

- Repo runs parallel Claude + Cursor edits. Always `git log --oneline -10` before starting work that creates files or implements named features.
- Exploratory prompts mean *analyze and propose*. Don't edit files until the user picks an option.
- Long-running bot may have loaded stale modules — verify imports actually picked up new code before declaring "ready."
- Alts (SOL/ETH/XRP/HYPE) are NOT decided by BTC. Direction and conviction come from alt-native indicators; BTC is at most an early indicator.
