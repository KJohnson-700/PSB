# RESTART BUNDLE — 2026-07-27 (recovery restart)

**Purpose:** activate staged fixes + un-break the suppressed exit logic that is bleeding the resumed session.
**Launch = OPERATOR action.** Claude does NOT restart. Claude runs post-restart verification.

## WHY FRESH (LIVE_FRESH_SESSION=1)
The resumed session `test_20260727_021601` has a corrupted held-time anchor (held-eff ≈ −7.8h,
"window-open anchored"). This SUPPRESSES never_green_cut, stops, AND take-profit on positions →
losers ride to full resolution loss and never_green_cut can't fire. A FRESH session anchors each
position at real fill time → exits fire normally → the recovery mechanism works again. It also
re-anchors bankroll at the real wallet and resets realized to 0 (clean recovery tracking).

## 🆕 DIAGNOSTIC-SPRINT FIXES ADDED (2026-07-27 evening, Codex-reviewed, validated)
- **F1 🔴 EXIT-SUPPRESSION FIX** (live_testing.py:726) — the root of today's bleed. Guard now
  `0.0 <= held_eff < floor`, so a resumed position's bogus-negative held_eff no longer suppresses
  stops/TP/never_green_cut. **Codex GO.** 89 tests pass, logic asserted. Backup saved.
  (Emergency HOT lever if ever needed without restart: `trading.exit_rules.updown_min_hold_anchor_window_open: false`.)
- **F2 🔴 CLOB STATUS FIX** (clob_client.py:1169) — filled orders reading `MATCHED`/`MINED`/`CONFIRMED`
  now classify as FILLED (were stranded PENDING); failed/rejected/expired → CANCELLED (re-arm). **Codex GO-w-nits (applied).**
- **F3 🟠 ALT SIMPLE_LONG BANDS** (config) — alt_1h_simple_long.entry_min 0.50→0.55 for sol/doge/bnb
  (mirrors btc; stops forced-LONG midline coin-flips). **Codex GO.**

## 🆕 EVENING WAVE 2 (2026-07-27 ~19:00, Codex-reviewed, DEPLOYED to paper + config)
- **eth 1h BUY_YES hold+trail** (config `eth_macro/1h/up`: hold_winners false→true, trail_arm
  0.25→0.10, gap 0.10→0.15; stop stays 0.15). Fixes the green-stop leak on a winning lane
  (held-WR 65% vs realized 29%, exit −$23 → hold +$38, n=17). **Codex GO-WITH-NITS** (no ride-to-zero,
  stop stays active, hot-reloadable). Already hot-applied in paper 18:35. Side-isolated (down unchanged).
- **Adaptive per-lane SIZER (SHADOW)** — `src/analysis/adaptive_lane_sizer.py` + config
  `trading.adaptive_sizer` (mode:shadow) + main.py post-settle recompute hook. ONE realized-P&L-driven
  size mult per lane/side, replaces the dead kelly_mult + scattered knobs. Sizes DOWN losers (n≥6),
  UP winners only (n≥12), bounded [0.4,1.6], EMA-smoothed, recent-sessions-only. **Codex GO** (all nits
  fixed). SHADOW = resolve_size_mult returns 1.0, moves NO real size. Phase 2 (mode:live) needs a
  separate operator GO after forward-test review. On restart the bot auto-recomputes it post-settle.
- **hype lane-gates card fix** (dashboard server.py `_build_lane_gates`) — disabled strategies now show
  all lanes closed (kind strategy_disabled) instead of falsely "active". Display-only. Codex GO. Dashboard
  already restarted; no bot restart needed for this one.
- **Break-Trigger Board fix** — NEW `src/analysis/break_trigger_board.py` generates
  `data/dashboard/break_triggers.json` (the definitions the /api/triggers card needs — nothing ever wrote
  it, card blank since 2026-07-16). server.py `_triggers_payload_sync` now SELF-HEALS (regenerates on
  missing/stale-session, passes server-resolved session_dir, atomic write). Codex NO-GO→fixed→GO-WITH-NITS,
  verified (11 lanes populate). Dashboard-only; no bot restart needed. PURE VISUALISATION (cuts nothing).

## 🔎 AUDIT FINDINGS (2026-07-27) — silently-dead features (backtest/ghost removal fallout)
- **kelly_mult loss/win sizing DEAD** (performance_feedback tied to removed backtest expectations; also
  `enabled:false`). Superseded by `adaptive_lane_sizer` (realized-driven, shadow). Do NOT revive backtests.
- **live-vs-backtest drift detection DEAD** (3 call sites need `src/execution/backtest_expectations.py` =
  MISSING; guarded by `if expectations:` so no crash, never runs). Dead weight — delete or rewire to realized.
- **per-lane breaker = SHADOW** (`lane_management.execution_enforcement_enabled: false`); fires ~58
  would_cuts/day, enforces nothing. VERIFIED do-NOT-enforce: blanket enforce nets **−$34** (saves $48 on
  eth1h-YES/doge5m-NO/btc1h-YES, costs $82 on 4 recoverers, esp xrp5m-NO +$57). Shadow is correct. KEEP.
- **CircuitBreakerManager** = no config block → disabled (0 blocks). Separate from the per-lane breaker.
- CONFIRMED FINE: hold-to-resolution, dashboard-absence handling, CLOB (no transactionHashes dependency).

## 🆕 SCANNER STARVATION FIXES (2026-07-27 evening, Codex-reviewed, verified)
- **S1 🟢 DROP DEAD HYPE FETCH** (config) — `polymarket.fetch_hype_alt_markets: false`. hype_macro is
  disabled, but the flag forced a slow ~13-slug fetch every cycle producing 0 rows (ate a sync slot +
  slug-pool budget, starved the slower windows). **Codex GO.** ⚠️ set back to `true` if hype_macro is ever re-enabled.
  (Read live but `polymarket` section may not be in the hot-reload set — treat as restart-applied.)
- **S2 🟢 INNER SLUG-FETCH TIMEOUT** (scanner.py ~1685 + helper ~558) — the inner slug loop had NO
  timeout, so one hung slug dropped a WHOLE window all-or-nothing at the 35s outer guillotine. Now
  bounded (default 25s, capped outer−3) → keeps whatever completed (partial >> full-window zero).
  **Codex GO-w-nits** (logging kept). Tunable: `trading.scanner_slug_fetch_timeout_sec`.
- **S3 🟢 ADAPTIVE SCANNER** (scanner.py + main.py + config, Codex scoped+reviewed GO-w-nits, nits fixed) —
  per-asset EMA productivity (0.8·prev+0.2·signals, fed from main.py) orders DEEPER-lookahead slug fetches
  so the S2 timeout cuts unproductive tails not producers. TWO-PHASE: nearest window always canonical
  (every asset covered each cycle) — no starvation feedback loop; escape-hatch cycle; atomic-swap + NaN
  guard. Config `trading.scanner_adaptive_slug_order: true` (ON, flip false to disable). 21 scanner + 43
  exit tests pass; paper-validated clean (no errors, full coverage, hype dropped).
- ⚠️ NOTE: the eth-70 vs doge/bnb-0 signal gap is DOWNSTREAM GATING (disabled lanes), NOT the scanner —
  S1/S2/S3 fix scan robustness/efficiency/fairness, they do NOT rebalance doge/bnb. That's a separate per-lane gate review.

## WHAT LOADS ON THIS RESTART (all compile-clean, verified)
1. **btc 1h `bitcoin_1h_simple_long.entry_min` 0.50→0.55** (config, __init__-frozen) — stops the
   band force-admitting BUY_YES longs at the 0.50–0.54 midline (the 2 −16.5 losers). Codex GO.
   Preserves the genuine bull-longs (btc 1h YES is historically the highest-edge lane).
2. **CLOB market-order entry fix** (clob_client.py) — routes FAK/FOK via create_market_order;
   fixes the 400 "invalid amounts" entry outage.
3. **Exit-marketable A/A+** (live_testing.py + main.py) — loss-cutting exits placed FAK/marketable
   (ride-to-zero fix).
4. **Killed-FAK re-arm + position reconciler** (main.py + clob_client.py) — stuck-exit handling +
   phantom/manual-close cleanup via Data-API /positions.
5. **eth 15m momentum-confirm** — already LIVE (config hot-reload); persists.
6. **Dashboard P&L-trace scale-guard** (index.html) — already live (hot); persists.

## PRE-FLIGHT (operator)
- [ ] Wait for the 5 open eth positions (5m/15m windows, near-flat ~−$0.86) to resolve → **0 open**.
      (Or manually close them on Polymarket — they're basically flat — if you want to restart now.)
- [ ] Confirm **0 open on the Polymarket UI** (a fresh session won't track pre-restart positions).
- [ ] VPN check if you route through it (local bot rule).

## LAUNCH
```bash
LIVE_FRESH_SESSION=1 .venv/bin/python src/main.py --live --confirm-live
```
Then type **YES** at the prompt.

## POST-RESTART VERIFY (Claude runs)
1. New session id (fresh `test_2026072x_xxxx`, NOT `...021601`).
2. Bankroll anchored at real wallet; realized starts 0.
3. **Exit logic UN-suppressed:** no more `Suppress never_green_cut ... held-eff -28000s`; held-eff
   positive on fresh positions; stops/never_green_cut/TP fire.
4. btc 1h: first BUY_YES fires only at yes_price ≥ 0.55 (no 0.50 midline forced longs).
5. Entries filling (no 400 invalid-amounts).
6. Reconciler running ("Venue position reconcile"); 0 phantom drift.

## TIMING
It's ~15:30 PT — the 5–9pm PT red block (−$65 to −$88/hr historically) is ahead. After restart,
run light or stand down through the evening; resume in the strong 10pm→11am window (peak 5–7am).
