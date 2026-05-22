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

## What NOT to run

- **Do not run `src/backtest/*` engines to validate strategy/gate/config edits.** The backtests are known-broken as of 2026-05-21 — some don't run, the ones that do produce numbers that don't match live behavior (point-in-time config, posteriors, regime, Polymarket YES/NO depth, AI veto state aren't faithfully replayed). Do not cite backtest output as validation evidence.
- **Validate with the ghost log instead.** `data/calibration/rejected_candidates_settled.jsonl` is the shadow / forward-paper record: every candidate the live scanner rejected, settled against the real Polymarket outcome. This is the truthful counterfactual and is what the user makes decisions on.
- **Ghosts cover:** gate on/off flips, threshold tuning, regime/family analysis, lane WR-at-volume.
- **Ghosts do NOT cover:** exit/stop/time-decay changes, sizing/Kelly changes, portfolio-level capital interaction, new candidate-generation logic, new features not previously logged, new lanes or assets. For those, flag to the user that ghosts can't answer the question and ask before proceeding — do not silently fall back to the broken backtester.

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
- Alts (SOL/ETH/XRP/HYPE/BNB/DOGE) are NOT decided by BTC. Direction and conviction come from alt-native indicators; BTC is at most an early indicator.

## Current strategy state (as of 2026-05-22)

### Live momentum guards (added 2026-05-21)
All three are **default-on**, opt-out via config flag.

- **BTC** [`_maybe_quant_flip`](src/strategies/bitcoin.py:268): counter-trend flips (`btc_quant_disagree_flip`) require BTC m5 or m15 DRIFT/SPIKE in the flipped direction. Opt-out: `quant_flip_require_momentum_confirm: false`. Suppressed flips log `quant_flip_suppressed=no_*_momentum`.
- **ETH** [eth_macro.py:898](src/strategies/eth_macro.py:898): BUY_NO and BUY_YES both require `eth.macd_5m` or `eth.macd_15m` confirmation in the matching direction. Opt-out: `buy_no_require_eth_momentum_confirm: false` / `buy_yes_require_eth_momentum_confirm: false`. Skip reasons: `buy_no_no_eth_momentum_confirm` / `buy_yes_no_eth_momentum_confirm`.
- **SOL family** [sol_macro.py:2170](src/strategies/sol_macro.py:2170) (inherited by xrp/hype/bnb/doge): same as ETH but uses `sol.macd_5m`/`sol.macd_15m` (which is the alt-specific MACD in subclass context). Opt-out: `buy_no_require_alt_momentum_confirm: false` / `buy_yes_require_alt_momentum_confirm: false`. Skip reasons: `buy_no_no_alt_momentum_confirm` / `buy_yes_no_alt_momentum_confirm`.

### BTC fully decoupled from alt decisions (completed 2026-05-22)
Per the "alts decided by alt-native indicators" rule, three classes of BTC-deciding-alt code were removed: side selection, admission gating, edge/sizing influence. Specifically removed:

- ETH NEUTRAL-macro path: BTC spike/lag/HTF can no longer pick ETH's side. ETH sits out when its own 1H is NEUTRAL ([eth_macro.py:583](src/strategies/eth_macro.py:583)).
- SOL-family NEUTRAL-macro path: same fix ([sol_macro.py:1802](src/strategies/sol_macro.py:1802)).
- `_bearish_dip_ltf_ok` / `_bullish_rally_ltf_ok` ([sol_macro.py:663,712](src/strategies/sol_macro.py:663)): dropped BTC 5m co-condition; exception paths now require alt MACD + RSI only.
- `lag_adj` in alt `_estimate_probability` ([sol_macro.py:1622](src/strategies/sol_macro.py:1622)): zeroed (BTC lag no longer adjusts alt edge — also data-confirmed harmful, lag=value had 50% WR vs 63% for lag=None).
- `btc_min_move_dollars` in sol_macro scan loop ([sol_macro.py:2330](src/strategies/sol_macro.py:2330)) and ETH ([eth_macro.py:1032](src/strategies/eth_macro.py:1032)): removed gate; diagnostic-only.
- `require_btc_volatility_gate` ([sol_macro.py:2420](src/strategies/sol_macro.py:2420)): diagnostic-only.
- `require_btc_catalyst_5m` ([sol_macro.py:2622](src/strategies/sol_macro.py:2622)) and `require_btc_catalyst_15m_when_unconfirmed` ([sol_macro.py:2919](src/strategies/sol_macro.py:2919)): diagnostic-only.
- ETH `center_price_requires_catalyst` ([eth_macro.py:1923](src/strategies/eth_macro.py:1923)): removed; the higher min-edge bar for centered prices stays (alt-native).

### Vestigial config keys
Still present in `config/settings.yaml` for back-compat; code now ignores them for decisions. Safe to leave; safe to delete later.

`buy_no_ltf_override_max_btc_5m_pct`, `buy_yes_ltf_override_min_btc_5m_pct`, `require_btc_volatility_gate`, `min_btc_move_pct_5m_for_lag_entries`, `min_btc_move_pct_15m_for_lag_entries`, `require_btc_catalyst_5m`, `require_btc_catalyst_15m_when_unconfirmed`, `btc_min_move_dollars_5m`, `btc_min_move_dollars_15m`, `btc_min_move_low_corr_threshold`, `center_price_requires_catalyst`, `neutral_macro_require_spike_or_lag`.

### When auditing for "BTC deciding alt"
Three distinct mechanisms — search for all three on any audit:
1. **Side selection** — does BTC pick which side (BUY_YES vs BUY_NO) the alt trades?
2. **Admission gating** — does BTC state (spike/move/regime) block the alt entry from firing?
3. **Edge/sizing influence** — does BTC adjust the alt's est_prob, min_edge, or notional?

Grep prompts: `corr.btc_*`, `btc_spike`, `btc_move`, `btc_catalyst`, `btc_min_move`, `lag_opportunity`, `opportunity_direction`, `btc_1h_regime_gates`.
