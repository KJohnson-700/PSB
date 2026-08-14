# PSB Overcoding Audit + Consolidation Plan (2026-08-06)

Read-only audit by 3 parallel mappers (exit / sizing / side-resolution+dead-code+config). Goal: find the accreted duplicate/competing/dead paths, name the ONE live path per function, and stage a safe consolidation. **No edits made yet — this doc is for Codex review before any change.**

Context that frames everything: the bot's WR is real (52–56% lanes, 60%+ sessions). The money leak is **losers bigger than winners** (sizing + exit asymmetry) compounded by a month of stacked config/code the operator suspects is "overcoded." Scale: `sol_macro.py` 7,437 lines, `bitcoin.py` 5,218; settings.yaml 3,193 lines; 20 shadow logs; 449 `.bak` source files.

---

## PART 1 — THE REAL BUGS (fix these; they change behavior)

### BUG-A ★ BTC sizing still scales with est_prob → BTC losers sized bigger (operator's #1 complaint, BTC only)
- BTC uses `size_from_edge` because `strategies.bitcoin.use_true_kelly_sizing: false` (settings.yaml:953). `size_from_edge` (`kelly_sizer.py:283-308`) computes `base = edge*frac*bankroll` where `edge = est_prob − price`.
- **`flat_sizing_enabled` only gates the flat branch inside `size_binary_position` (kelly_sizer.py:346) — it does NOT gate `size_from_edge`.** So BTC never gets flat sizing; a false-confident BTC loser gets a larger raw size (est_prob is ~coinflip AUC, so this is the classic inversion, still live on BTC).
- Downstream $11 floor + $12–15 `lane_max_usd` cap compress it, but inside the $11→$15 band a BTC loser can still exceed a BTC winner.
- **Fix:** route BTC through the flat base too — set `use_true_kelly_sizing: true` for BTC, or add a flat branch to `size_from_edge`. Prerequisite before any proven-lane upsize (else BTC losers ride edge-scaled size up toward a raised ceiling).

### BUG-B Exit: hold_all leaves trailing-stop arm/gap non-zero (latent flag coupling)
- `hold_all:true` zeroes `updown_stop_loss_pct` + `dynamic_stop` (updown_exit_shared.py:541-544) but NOT `updown_trail_arm_pct:0.1 / updown_trail_gap_pct:0.15` (settings.yaml:442-443). The trailing-floor block (updown_exit_shared.py:666-688) can still return a non-zero effective stop once peak≥+10%, re-arming the `live_testing.py:1041` stop branch. It only nets to no-op because hold-means-hold (`:1263`) suppresses it. **If `hold_means_hold_enforce` is ever flipped off with hold_all on, the trail silently resurrects and cuts winners.**
- **Fix:** zero trail arm/gap inside the hold_all block, or gate the trail on `not hold_winners`.

### BUG-C Exit: per-lane loser floors are DEAD under hold_all (policy decision, not just cleanup)
- `hold_lane_loser_floor_enabled:true` (settings.yaml:363) + calibrated per-lane `updown_stop_loss_pct` floors (btc15m-up 0.30, eth1h-up, sol5m-down, doge1h-up 0.40) are advertised active, but hold_all sets stop=0.0 → `_lane_stop=0.0` fails the `0.0 < _lane_stop` guard (live_testing.py:1303). **Losers ride to −90% catastrophic instead of their −20/30/40% floor.**
- This is the exit side of "losers bigger than winners": hold_all fixed winner-cutting but removed the loser caps. **Operator decision needed:** pure symmetric hold (±~100%, current) vs re-enabling per-lane loser floors (cap losers at −30% while winners still resolve). Do NOT silently pick one.

### BUG-D Mislabeled live path (fog)
- `bitcoin.py:465-471 btc_mom_side_downbias_shadow` is named "shadow" but is **LIVE** — `btc_momentum_side_disagree_none:true` (settings.yaml:939) makes line 471 overwrite `momentum_side` live. The log tag says "shadow," misleading anyone reading live logs. **Fix:** rename/relabel; keep behavior.

### BUG-E Gate conflict: fade-short flip vs downstream re-block
- `overbought_fade_short` (LIVE sol/xrp/bnb) flips a blocked long into a SHORT (sol_macro.py:3935), but `alt_1h_require_confirm` + `require_quant_side_agreement` (both LIVE) can re-block that manufactured short. The flip and the vetoes pull against each other on the same candidate. **Verify the fade path carries intended exemptions; likely wasted computation / inconsistent behavior.**

---

## PART 2 — THE ONE LIVE PATH PER FUNCTION (what should remain)

**EXIT** (under hold_all): (1) hold-to-resolution `updown_expired`/`updown_time_limit`; (2) `hold_catastrophic_stop` −90% backstop; (3) `take_profit_late` (btc-1h-up only). That triad is the entire live surface.

**SIZING**: flat base $15 (kelly_sizer.py:346, est_prob-free) → `exposure.scale_size` tier cap → `_apply_adaptive_realized_size` (realized-ROI EMA mult [floor..ceil], `lane_max_usd` cap, `$11 min_live_notional` floor) → `risk_manager.evaluate_entry`. **Realized-ROI driven, not est_prob** (except the BTC leak, BUG-A).

**SIDE**: per-family champion resolver (`bitcoin.py:444 _resolve_btc_direction`; sol/eth scan-loop stacks). `side_resolver_v2` stays a shadow (not wired). Keep.

---

## PART 3 — REMOVABLE (dead/redundant; no behavior change)

**Exit (inert behind hold_all):** dynamic-stop machinery (updown_exit_shared.py:621-688 + config 427-436); late-only stop gate (live_testing.py:869-893); `tape_hold_stop` (918-966); `hold_5m_all` (subsumed); NGC + severity gate; `updown_time_stop`; flatten; give-back TP (config-off); regime-conditioned exits (config-off); bid-depth exit (config-off). NOTE: several are operator-reversible levers → **document/quarantine, don't necessarily delete.**

**Sizing (dead/neutralized):** kelly-on-est_prob else-branch (kelly_sizer.py:349-350); `_size_multiplier_for_lane` hardcoded return 1.0 (sol_macro.py:2491); `lane_policy.size_multiplier` apply blocks (gated off); `calibration_size_multiplier_5m` (gated off); `tuning_size_multiplier` (1.0 no-op); BTC conviction-ceiling (config 0.0); `_apply_tape_adapter_size` (shadow/off); streak-multiplier sizing use (unused). Consolidate in-strategy `lane_max_notional` into Layer-3 `lane_max_usd` (two cap systems both bind).

**Dead code:** dashboard/server.py:2841+ (unreachable legacy body), `get_live_drift` deprecated stub (3789); sol_macro.py dead attrs (859-861, 1007-1009, 1018-1020); the five `if…: pass` vestigial-gate stubs.

**Vestigial config (12 keys, none read for a decision — all inert via `_btc_trade_inputs_enabled()` returning False):** buy_no/buy_yes_ltf_override_*_btc_5m_pct, require_btc_volatility_gate, min_btc_move_pct_{5m,15m}_for_lag_entries, require_btc_catalyst_5m, require_btc_catalyst_15m_when_unconfirmed, btc_min_move_dollars_{5m,15m}, btc_min_move_low_corr_threshold, center_price_requires_catalyst, neutral_macro_require_spike_or_lag. (Several still set `true` → misleading.)

---

## PART 4 — THE $100 LEVER (size UP proven lanes, AFTER BUG-A)
Machinery exists and is inversion-safe IF growth stays realized-driven:
1. Fix BUG-A (flat-size BTC) first.
2. Raise `adaptive_sizer.mult_ceil` above 1.0 (currently 1.0) — growth comes only from `_target_mult` size-up on realized ROI, gated `n>=12 & wr>=proven_wr_min & roi>=proven_roi_min` (never est_prob).
3. Set proven-short ceilings ($28/$40) in `lane_max_usd` for the lanes that clear the gate.
Keep `flat_sizing_enabled:true` for ALL assets throughout.

---

## PROPOSED ORDER (one change, confirm/revert each — no stacking)
1. **BUG-A** flat-size BTC (closes the est_prob inversion) — highest value, directly on the complaint.
2. **BUG-C** decide loser-floor policy (operator call) + **BUG-B** guard the trail coupling.
3. Relabel BUG-D, verify/simplify BUG-E.
4. Delete vestigial config (12 keys) + dead code (safe, no behavior change).
5. Quarantine (not delete) the reversible exit levers; document the ONE live triad.
6. THEN the $100 lever: mult_ceil>1.0 + proven ceilings.
Codex review this plan for: (a) any "removable" item that is actually load-bearing, (b) correctness of BUG-A's claim that `size_from_edge` bypasses flat_sizing, (c) whether fixing BUG-A could destabilize BTC sizing, (d) safest order.
