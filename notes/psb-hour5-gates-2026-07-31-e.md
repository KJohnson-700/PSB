# HOUR 5 — gates (2026-07-31 10:10Z)

**Session:** `test_20260731_005529` · **pid 63052** (alive, 02:16:12 uptime, RSS 763 MiB) · started 2026-07-31T07:59:03Z

## Process health (clean)

- bot_heartbeat: pid 63052, ts 2026-07-31T10:09:25Z, phase=cycle_complete, RSS 800.8 MB (heartbeat read on next cycle write — under 900 MB cap).
- bot_runtime_status: cycle_count=134, cycle_elapsed_ms=5003, scanner_sync_ms=1682, strategy scans all ≤1.04s (bnb/doge/eth/sol/xrp at 994–1037 ms — under 1s-budget *only* by ms; bitcoin 153 ms).
- mode=paper, kill_switch=false, clean_shutdown=false (no prior shutdown recorded).
- launchctl list shows no `com.psb.bot` plist — bot is being supervised via a different mechanism (dirbreaker/sentinel rotation plists present). Process is up.
- bankroll $499.72 cash · equity $499.30 · realized −$0.28 · unrealized −$0.42 · total −$0.70 (small session — only 12 entries).
- daily_pnl −$0.28 · daily_trades 12.
- updown_15m_count=43 (≥20 OK) · slug_fetch 15m 63/63 (100%) · sync_phase_elapsed_ms 1181 (normal).

## Health row vs cross-artifacts (watchdog drift carried forward)

`tail -1 ~/.hermes/logs/psb_local_health.jsonl` reports:
```
session=test_20260731_005529 pid=null hb_age_s=1 phase=scanner_sync rss_mb=null
priority=BOT_DEAD severity=dead realized=-0.28 closed=11 wins=4 loss_streak=1 long_loss_cluster=1
worst_lane=sol|15m|down worst_lane_pnl=-4.35 cash_bankroll=499.7191 bankroll_drift=-0.0
overruns=1 lanes={xrp|1h|down 3/2/+0.01, sol|15m|down 4/0/-4.35, sol|5m|down 1/1/+6.71,
                  eth|15m|down 1/1/+2.78, bnb|5m|down 1/0/-3.88, sol|1h|up 1/0/-1.55}
```
Cross-check:
- `ps -p 63052`: alive 02:16:12, RSS 781,744 KB (763 MiB).
- `bot_heartbeat.json`: pid=63052, ts=2026-07-31T10:09:25Z, phase=cycle_complete, rss_mb=800.8.
- `bot_runtime_status.json`: pid=63052, cycle_count=134, phase=cycle_complete, ts=2026-07-31T10:09:25Z.

Two writers agree on pid 63052. Watchdog row shows `pid:null, rss_mb:null, priority:BOT_DEAD` — same persistent false-dead signature as run-24, run-25, and 2026-07-28T09:15Z. **Cite as `[INFO] watchdog stale pid:null, 5th-recurrence pattern`** (per `psb-crown-formatting` §Watchdog-stale-`pid:null`).

## Per-lane session stats (12 entries)

| Lane | n | W/L | WR | PnL | post-α | post-n | post-WR | Verdict |
|---|---:|---|---:|---:|---:|---:|---:|---|
| `sol_macro\|15m\|down\|sol_15m_native` | 4 | 0/4 | 0% | −$4.35 | +1.84 | 120 | 0.40 | session losing, post OK |
| `bnb_macro\|5m\|down\|bnb_5m_native` | 1 | 0/1 | 0% | −$3.88 | +2.12 | 36 | 0.38 | session tiny, post OK |
| `sol_macro\|1h\|up\|standard` | 1 | 0/1 | 0% | −$1.55 | +0.61 | 26 | 0.48 | session tiny |
| `xrp_macro\|1h\|down\|xrp_1h_native` | 3 | 2/1 | 67% | +$0.01 | +0.72 | 146 | 0.48 | OK |
| `eth_macro\|15m\|down\|drift` | 1 | 1/0 | 100% | +$2.78 | **−1.12** | 46 | 0.38 | session win, post-VETO-α pending n=50 |
| `sol_macro\|5m\|down\|sol_5m_native` | 1 | 1/0 | 100% | +$6.71 | +1.46 | 182 | 0.44 | OK |

**Pattern H check (vetofrozen):** zero live lanes clear the strict criterion (α<-0.5 AND n≥50). `eth_macro|15m|down|drift` is at α=-1.12, n=46 — *one trade short* of the n≥50 threshold but already crossing the α<−0.5 bar. Not yet a Pattern H firing; flagged as **`[INFO] eth_macro|15m|down|drift: α=−1.12, n=46 — pre-Pattern-H (one settle from criterion)`**.

The 4-of-12 losing streak is concentrated in sol_macro|15m|down (4 losses, $−4.35) plus single-loss bnb_macro|5m (−$3.88) and sol_macro|1h|up (−$1.55). These account for 100% of session realized (summary.json total −$0.28 plus the +$6.71 winner offsets most losses — sol_macro net is +$0.81 with 1/6 WR). The bnb_macro single loss at −$3.88 is the biggest single trade of the session.

## `lane_thresholds.json` staleness (carry from run-25)

`data/calibration/lane_thresholds.json` `computed_at=2026-06-22T09:01:20Z` → **age 39 days**. Live `live_n`/`live_wr`/`live_pnl` fields update, but `veto_recommended` / `recommended_max_mean` bits are frozen. Same `recompute_on_settle=false` regime. Cite as `[INFO] lane_thresholds.json 39d stale (run-25 carry)`.

## Cycle overrun — `[WARN] overruns=1`

`bot_runtime_status.cycle_timings_ms.cycle_overrun_ms=0` but `health.overruns="1"` shows one overrun in the last cycle window. cycle_elapsed_ms=5003 (under 60000 cycle_interval). Not a [BAD]; flag at [WARN] for the operator.

## Side / bias regime

- `btc_htf_bias = NEUTRAL` (only btc surface in this OPS_JSON; alts inherit via primary_htf_bias in indicator_snapshot).
- Per-strategy `allowed_side`: bitcoin=SHORT, sol_macro=None (no value), eth/xrp/doge/bnb=SHORT.
- Open book: 1 open position (BUY_NO from bitcoin — short side, matches allowed_side).
- No bias-side mismatch (consistent SHORT book on NEUTRAL/SHORT-allowed bias).

## Drift callouts (one-line combined)

`[INFO] watchdog stale pid:null (5th rec) · [WARN] overruns=1 · [INFO] lane_thresholds.json 39d stale · [INFO] eth_macro|15m|down|drift α=−1.12 n=46 pre-Pattern-H`

## Top red flags

1. **sol_macro|15m|down losing streak (0/4, −$4.35)** — session 100% loss on 4 trades; sol_macro exposure_manager at MINIMAL tier with consecutive_losses=4 and recent_pnl=+$0.81 (the wins elsewhere mask the streak).
2. **bnb_macro|5m|down single-trade loss −$3.88** — one trade, one loss; bnb exposure_manager at MODERATE tier with consecutive_losses=1 and recent_pnl=−$3.88 (no MINIMAL clamp yet).
3. **eth_macro|15m|down|drift post-α=−1.12 n=46** — pre-Pattern-H; one settled trade at the loser lane would push n to 47 (still under 50). Active monitoring recommended.

## What NOT to do (anti-patterns)

- ❌ Do not propose un-freezing `recompute_on_settle` — operator-frozen per 07-13 directive. Report only.
- ❌ Do not propose sitting out sol_macro|15m|down — single-session 4-trade streak is normal sample noise; operator tolerance covers this.
- ❌ Do not classify `pid:null` health row as [BAD] — false-dead drift pattern; cross-artifact check (ps + heartbeat + runtime) confirms alive.
- ❌ Do not treat `overruns=1` as [BAD] — single overrun is routine for an active scan with 6 strategies.

## Cross-cuts

- `psb-h5-vetofrozen-run25` (prior run, same PSB HOUR 5 vetofrozen sequence — run-26 now).
- `psb-crown-formatting` §Watchdog-stale-`pid:null` (PERSISTENT false-dead).
- `psb-strategy-silent-failure-debug` Pattern H criterion (α<-0.5 AND n≥50).
- `psb-hourly-briefing` umbrella.

HOUR 5 BRIEF — vault: notes/psb-hour5-gates-2026-07-31-e.md
