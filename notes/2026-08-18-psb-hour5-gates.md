# PSB HOUR 5 — Lane gates + calibration
**Date:** 2026-08-18 21:06 UTC (cron snapshot)
**Session:** test_20260818_031931
**PID:** 16650 — alive, hb_age=3s, phase=cycle_complete
**Mode:** paper, dry_run=true, journal_dir=/Users/mainfolder/Documents/psb-main 1/data/paper_trades/test_20260818_031931

## Headline
- **Realized: -$39.55 over 67 closed trades** (28 wins, 41.8% WR). Bankroll $960.45 (start $1000). Below baseline shape — baseline test_20260714_070245 was +$869.90 over 75h/1138 entries (46.4% WR).
- **Lane cuts: 2 lanes above the -$15 cut threshold:** `eth|1h|up` (-$17.22, n=6, 33% WR) and `eth|5m|up` (-$16.05, n=7, 29% WR). Both intent-side UP lanes on ETH are bleeding.
- **Worst lane:** `eth|1h|up` (-$17.22, n=6, 2W4L). Lane-level `$15 PnL cut` appears to be **not firing** (recompute_on_settle FROZEN false per operator 07-13, beta_veto_max_mean DISABLED=0.0).
- **Bot state:** priority=PNL_CRIT, severity=crit, exp_long_losses=22 (escalating exposure-loss sequence), giveback_usd=$156.56, updown_15m_count=49 (healthy).

## Per-lane table (health row 12736, ts=2026-08-18T21:06:28)

| Lane | n | Wins | WR | PnL | alpha_ewma (post) | n_post | Verdict |
|---|---|---|---|---|---|---|---|
| eth\|5m\|down | 15 | 8 | 53.3% | +$12.46 | need lookup | — | HEALTHY (+) |
| eth\|5m\|up | 7 | 2 | 28.6% | -$16.05 | n/a | — | LANE-CUT — small-N |
| bnb\|15m\|down | 4 | 1 | 25.0% | -$10.04 | n/a | — | small-N, monitor |
| sol\|1h\|up | 1 | 0 | 0.0% | -$4.02 | n/a | — | small-N |
| eth\|1h\|up | 6 | 2 | 33.3% | -$17.22 | n/a | — | LANE-CUT ≥-$15 |
| doge\|5m\|up | 12 | 5 | 41.7% | -$5.01 | n/a | — | WR<0.4 |
| xrp\|5m\|up | 20 | 9 | 45.0% | -$0.52 | n/a | — | monitor |
| hype\|5m\|up | 1 | 1 | 100% | +$3.16 | n/a | — | small-N |
| xrp\|1h\|down | 1 | 0 | 0.0% | -$2.31 | n/a | — | small-N |

**Session totals:** 67 closed, 28 wins, 41.8% WR, -$39.55. Open: 1. Deployed: $5.05. Giveback: $156.56.

## Calibration deep-dive (lane_posteriors.json)
- File loaded (426KB, 12703 lines). Format: `lane_identity_v2_source_resolver::<asset>|<window>|<side>|<regime>|<strategy>` keys with `alpha_ewma`, `beta_a`, `beta_b`, `n`, `last_updated`.
- **Active lane keys** (matching the 9 health-row lanes) were not directly searchable from the truncated prefix — the file follows `v2_source_resolver::` namespace, not the short `eth|5m|down` form. Without a confirmed lookup, I cannot assert a CALIBRATED-LOSER (alpha<-0.5 + n_post>=50) flag for any active lane.
- **Observation:** many v2_resolver keys have `n_post=0` and `alpha_ewma=1.0` (uninitialized). Active trading lanes with real data should be visible, but the prefix mismatch means I cannot map short-form lane → posterior key without the full resolver lookup table.

## Exposure manager state (bot_runtime_status.json)
- cycle_count=1290, cycle_elapsed_ms=5658 (well under 20s), cycle_interval_ms=30000, cycle_overrun_ms=0.
- Hype_macro scan = 694ms (longest single strategy, still under 1s cap).
- exp_long_losses=22 globally. Per-asset consecutive_losses: doge=2, eth=3, sol=1, xrp=1, bnb=3, btc=0, hype=0. **None of the lanes show `paused=true` or `paused_lanes` populated** — the loss-pause mechanism is not biting.
- tier state: doge/bnb=MINIMAL, eth/xrp/sol/hype/btc=MODERATE.

## Cycle performance
- cycle_elapsed_ms = 5658 (target 30s) — healthy.
- scanner_sync_ms = 4185 (dominant).
- 0 strategy scan > 1s (hype 694ms ceiling).
- No fits.

## Red flags
- [WARN] **2 lanes past -$15 cut threshold** (eth|1h|up, eth|5m|up) — but no paused state in exposure_managers. Beta_veto is DISABLED (0.0), so mechanism will not fire automatically.
- [WARN] **Realized -$39.55** — below -$25 threshold (checklist says <=-25 OR lane<=15 triggers).
- [WARN] **ETH losing streak on UP side** — both eth|5m|up and eth|1h|up are bleeding while eth|5m|down is +$12.46. Direction asymmetry on ETH.
- [WARN] **exp_long_losses=22** sustained — increasing exposure-loss sequence.
- [WARN] **giveback_usd=$156.56** — large realized giveback relative to -$39.55 net PnL (high turnover with net whipsaw).
- [INFO] **Calibration → lane mapping unverified** — lane_posteriors.json uses v2_source_resolver namespace; short-form lane names in health row didn't match by simple substring. Active-lane alpha_ewma cannot be confirmed without resolver lookup table.

## Checklist call
- 5 WARN flags, 0 BAD, 0 INFO.
- **Drift callout:** session_realized=-$39.55 (below -$25 cap); 2 lanes at -$15 cut threshold without auto-pause firing.

## Vault note: notes/2026-08-18-PSB-HOUR5-gates.md

## What Claude should check next
1. Confirm whether `recompute_on_settle` truly remains FROZEN false and whether `beta_veto_max_mean` is still 0.0 — if yes, the lane-cut on eth|1h|up / eth|5m|up WILL NOT auto-trigger. Manual intervention may be needed.
2. Inspect the lane_identity_v2_source_resolver key mapping to confirm whether the active eth|5m|down lane's positive alpha contradicts the other eth up-lanes — or whether v2 keys should be queried by the health-row short form.
3. Confirm whether `exp_long_losses=22` triggers any exposure_kill (the json shows `exposure_loss_kill_enabled=false` in pulse, so NO — but verify config).
