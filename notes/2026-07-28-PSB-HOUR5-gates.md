# PSB HOUR 5 — Lane gates + calibration
**Date:** 2026-07-28  ~15:09Z (HB ts) / 15:10Z local
**Session:** `test_20260728_074851` (started 14:51:25Z, ~18min in)
**Previous session:** `test_20260728_012751` ended at realized = -$46.62 over 64 closed trades; bot was restarted ~07:48 local → new session.

## Health row
- pid=90135, hb_age=4-8s, phase=`cycle_complete`, rss=721.2MB, priority=`ok`, severity=`ok`
- cycle_count=21, cycle_elapsed_ms=3188 (well under 20s), scanner_sync_ms=3084
- Per-strategy scan_ms: all 76-78ms, healthy
- updown_15m_count=null this hour (was present in last 3 hours), but 1h scan sync_phase_elapsed=1514ms, no API issue visible

## OPS_JSON pulse (latest, 15:09:25Z)
- bankroll=$506.41, equity=$506.41 (all flat, 0 open), realized=$+6.12, daily_pnl=$+6.41
- closed_trades=4, total_entries=4, WR=50%
- All 6 macro lanes ran. **0 signals fired this cycle.** `last_signal_counts={all zeros}`.
- Cumulative signals: xrp=4, bitcoin=1, eth=1, rest=0
- side_selection: only **bitcoin=LONG**, xrp=SHORT, doge=SHORT. sol/eth/hype/bnb all allowed_side=null.
- Lane min_edge skip on xrp (5 times, edge 0.06 vs min 0.09).

## Regime context (the drift)
- Btc 1h regime: **BEAR** (was BULL in last hour's brief)
- htf_bias = BEARISH, alt_htf_bias = BEARISH (was BULLISH last hour)
- aggregate skip reasons: **neutral_bias=30, lane_entry_window=21, rsi_hard_blocked=8**
- Skip pattern shifted from last hour's "lane_entry_window dominant" → this hour **neutral_bias is now the dominant gate** (13 on sol, 8 on xrp, 3 on bnb, 3 on doge).

## Lane table (current session only, 4 entries)
| Lane | n | WR | PnL |
|---|---|---|---|
| bitcoin\|1h\|up | 1 | 0% | -$6.13 |
| eth_macro\|5m\|up | 1 | 100% | +$12.07 |
| xrp_macro\|5m\|up | 2 | 50% | +$0.17 |

Net session: +$6.12 (2 wins, 2 losses).

## Lane_posterior check
- Posteriors keyed by `strategy|window|side|regime|family` (e.g. `bitcoin|1h|up|bearish|htf_bearish_side_long`) — **does NOT match the simple `bitcoin|1h|up` keys used in health row's `lanes` dict** or in OPS_JSON's stats.
- With n=1-2 entries per lane in this session, **no lane meets n≥50 threshold for calibrated loser criterion** (alpha_ewma < -0.5). Cannot meaningfully trigger the wr_veto_threshold=0.4 check; freeze does not matter at this n.
- `recompute_on_settle` is still FROZEN false (operator 07-13), `beta_veto_max_mean` DISABLED = 0.0 — these configs remain as set, unchanged.

## Red flag checklist
- [BAD] BOT DEAD — NO, pid 90135 alive
- [BAD] HEARTBEAT STALE — NO, hb_age 4-8s
- [BAD] CYCLE_LAG — NO, 3.2s < 20s
- [BAD] RSS > 900MB — NO, 721MB
- [WARN] session realized <= -25 — NO, current session +$6.12 (prior session -$46.62, but session restarted)
- [WARN] LOSS_STREAK >= 5 — NO, loss_streak=0
- [WARN] LANE alpha_ewma < -0.5 with n>=50 — NO, sample too thin
- [INFO] Pricing freshness > 120s — updown_15m_count=null (not degenerate; 1h sync healthy at 1514ms, hit rates all 100%)

## Verdict
Healthy bot, regime flipped BULL→BEAR, majority of lanes sidelined on neutral_bias, only 4 trades this session, net positive +$6.12. No actionable red flags. Lane_posterior n too small for veto checks to matter.
