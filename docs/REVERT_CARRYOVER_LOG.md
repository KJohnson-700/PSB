# REVERT CARRY-OVER LOG (2026-08-07)

Reverting config to `settings.yaml.VPS_baseline_plus869_20260717`. **That baseline is OLYMPUS-optimized (fee=$0, event-resolution, marketable orders) — NOT Polymarket.** So the revert re-introduces the venue mismatch unless we carry the Polymarket-adaptation changes below. Even with them carried, the baseline is Olympus-tuned → expect Polymarket issues to work through in Phases 1–4 (this is a starting point, not a finished state).

Diff vs +869: **317 added keys, 99 changed values.** Categorized for your per-item decision.

---

## TIER 1 — MUST CARRY (Polymarket-critical; baseline is Olympus and physically lacks these — omitting = bot re-breaks on the venue)

### Execution / venue (baseline points at Olympus)
- `trading.execution_provider: olympus → clob`
- `trading.entry_mode: marketable → hybrid` (+ `trading.exit_maker_wait_sec: 6`)
- `polymarket.funder_address: 0x932845… → 0x7d81246BbE1e91f84f5A791D56fb1865545D78A9` (deposit wallet — ⚠️ GO-LIVE TRAP: without this live reads $1.03)
- `polymarket.signature_type: 1 → 3`
- `olympus.smoke_test.enabled: True → False`

### Fees (Olympus was $0; Polymarket crypto taker ≈ 0.07)
- ADD `trading.fee_aware_edge` block: `enabled:true`, `taker_fee_rate:0.07`, `olympus_taker_fee_rate:0.0`, `fee_legs:1.5`
- `trading.execution_fees.crypto_updown_15m_taker_fee_rate: 0.072 → 0.07`

### Feed / freshness (Polymarket WS is sparse; needs REST fallback + tight staleness)
- `trading.clob_ws.price_max_age_sec: 45 → 8`  · ADD `rest_book_price_fallback:true`
- `trading.clob_ws.ws_app_ping_sec: 0→10`, `subscribe_interval_sec: 15→30`, `universe_subscribe_cap: 400→250`

### Slippage guard (thin PM books)
- `slippage_guard.max_spread_cents: 0.03→0.05`, `require_full_depth: True→False`, `depth_price_ceiling_cents: 0.0→0.02` · ADD `apply_in_paper:true`, `paper_mode:observe`

### Exit — Polymarket resolves at the WINDOW BOUNDARY, not event-tick (the single biggest venue difference)
- ADD `exit_rules.hold_all:true`, `hold_5m_all:true`, `hold_catastrophic_stop_pct:0.9`, `hold_5m_loser_floor_pct:0.4`, `hold_lane_loser_floor_enabled:true`
- (Note: hold_lane_loser_floor is currently NEUTRALIZED by hold_all's stop=0.0 — decide the loser-floor policy on carry-over, Phase 3.)

### Sizing (the inversion fixes)
- ADD `trading.flat_sizing_enabled:true`, `trading.flat_base_usd:15.0`
- `strategies.bitcoin.use_true_kelly_sizing: false → true` (BUG-A: routes BTC to the flat path)

---

## TIER 2 — SHOULD CARRY (data-backed Polymarket-era wins; verify each on the clean base)
- **eth OFF at scanner** (`strategies.eth_macro.enabled:false`) — data: worst asset −$169.
- **Data-backed 15m loser-tightens** — e.g. hype/xrp 15m `entry_window_max 135→15` (operator-GO, "worst 15m short lane −30/WR13%"), the validated sit-outs (sol 15m NO/YES, doge/bnb marginal longs). Each has a comment + data rationale — carry the ones with proof.
- **adaptive_lane_sizer** (39 keys) — realized-ROI sizer (mult_ceil, lane_max_usd). Carry the framework; re-tune from scratch.
- **lane_breaker** (19 keys) — loss-streak entry breaker.

## TIER 3 — EVALUATE (mixed / mine-tonight; do NOT carry blindly)
- The August **direction gates** (xrp 30 / sol 27 / doge 21 / bnb 20 / btc 15 / eth 15 added keys): require_quant_side_agreement, overbought_fade_short, tape_map_veto, alt_1h_require_confirm/allow_neutral, oversold_hard_block, the 3 BTC short-into-bull guards, etc. THIS is the overcoding — carry only the ones that measurably help on the clean base; most should stay dropped.
- **My tonight tape-gates** (require_tape_direction all-alts, up-veto all-alts) — mixed; re-evaluate on clean base.
- `tape_hold_stop.by_lane` (32) — mostly inert under hold_all; likely drop.

## TIER 4 — DROP (junk / reverted)
- 12 vestigial config keys (inert via `_btc_trade_inputs_enabled()`=False).
- Dead code (dashboard unreachable body, get_live_drift stub, sol_macro dead attrs).
- `fade_regime` experiments (reverted tonight, n6058-refuted).
- Duplicate/contradictory window overrides (the self-contradictory 1h 60-vs-360 tangle).

---

## PROCEDURE
1. `cp` current config to a dated backup (preserve tonight's state).
2. Start from +869 baseline.
3. Apply TIER 1 wholesale (Polymarket-critical), TIER 2 verified, TIER 3 only what proves out.
4. Restart --paper, confirm it TRADES + WR not worse.
5. THEN Phases 1–4 (tape_map signal, strip remaining overcoding, exit/fee/horizon, sizing).

⚠️ Baseline is Olympus-tuned. Even with TIER 1 carried, expect Polymarket-specific issues — this is the clean foundation to iterate FROM, not the answer.
