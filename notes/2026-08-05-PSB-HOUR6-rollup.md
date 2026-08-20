# PSB HOUR 6 — Daily Rollup (2026-08-05)

## TL;DR
- **Day PnL: +$19.74** single-session (one session active, test_20260805_005317, ~5h lived)
- **21 entries, 20 closed, 12 wins (60% WR), 1 open** — WR above baseline (46.4%) but small sample
- **Bot is alive** (PID 36870, 5h19m up, RSS 1016.6MB **CRIT** — climbed from 1005→1016 in last 12 min)
- **All lanes real-PnL live**: eth|5m|up +$19.85 (hero), eth|1h|up +$6.50, sol|1h|up +$2.77, sol|15m|up +$0.92, xrp|1h|down -$4.56, eth|15m|down -$4.51, xrp|15m|down -$0.81, eth|15m|up -$0.42
- **No lane alpha_ewma reads usable** — lane_posteriors.json last_updated 2026-05-17 (80 days stale). All current lanes n=1–5, none meet n>=50 calibrated-loser threshold
- **Drift**: Pace vs baseline $11.60/hr is **$3.95/hr** (5h, +$19.74) — under baseline; small sample caveat
- **session severity=crit priority=RSS_HIGH** — bot is already flagging itself

## Daily Rollup by Session
Only one session active today. Yesterday's prior sessions not in scope (HOUR 6 = day rollup, but bot ran continuously since 2026-08-05 08:02Z).

| Session | Started | Closed | Wins | WR | Realized |
|---------|---------|--------|------|------|----------|
| test_20260805_005317 | 08-05 08:02Z (current) | 20 | 12 | 60.0% | +$19.74 |
| **TOTAL** | 5h so far | **20** | **12** | **60.0%** | **+$19.74** |

## Current Session vs Baseline (test_20260714_070245)
- Baseline: $869.90 / 75h / 1138 entries / 46.4% WR → $11.60/hr, 15.2 entries/hr
- Current: $19.74 / 5h / 21 entries → $3.95/hr, 4.2 entries/hr
- **Pace concerning** (1/3 baseline), but **WR above baseline** (60% vs 46.4%), no catastrophic lane bleed
- **tiny sample** — 21 entries is 1.8% of baseline. Lane-level variance dominates.

## Per-Lane (current session, sorted by PnL)

| Lane | n | WR | PnL | alpha_ewma | Verdict |
|------|---|------|------|------------|---------|
| eth|5m|up | 2 | 100% | +$19.85 | (n<50) | **hero** |
| eth|1h|up | 4 | 50% | +$6.50 | (n<50) | positive |
| sol|1h|up | 2 | 100% | +$2.77 | (n<50) | positive |
| sol|15m|up | 2 | 50% | +$0.92 | (n<50) | flat |
| eth|15m|up | 2 | 50% | -$0.42 | (n<50) | flat |
| xrp|15m|down | 2 | 50% | -$0.81 | (n<50) | flat |
| eth|15m|down | 1 | 0% | -$4.51 | (n<50) | **bleeder (n=1)** |
| xrp|1h|down | 5 | 60% | -$4.56 | (n<50) | **loss source** |

## Strategy Rollup (from summary.json)

| Strategy | Trades | Wins | WR | PnL |
|----------|--------|------|------|-----|
| xrp_macro | 7 | 4 | 57.1% | -$5.37 |
| eth_macro | 9 | 5 | 55.6% | +$21.42 |
| sol_macro | 4 | 3 | 75.0% | +$3.69 |
| btc_macro | 0 | 0 | — | $0.00 |
| bnb_macro | 0 | 0 | — | $0.00 |
| doge_macro | 0 | 0 | — | $0.00 |
| hype_macro | 0 | 0 | — | $0.00 |

## Cycle Heartbeat (last cycle, cycle_count=320)
- cycle_elapsed_ms: 5282 (under 20s threshold)
- scanner_sync_ms: 1789 (well under 1s alert — non-strategy sync)
- **strategy_scan_by_name_ms: bitcoin 49ms, eth_macro/doge_macro/sol_macro/xrp_macro/bnb_macro all 1008–1010ms** — each lane scan > 1s [WARN borderline]
- cycle_overrun_ms: 0 — no overrun
- clean_shutdown: false (normal — bot running)

## RSS History (last 4h, every ~30min)
- 09:11Z: 794
- 10:42Z: 959
- 12:12Z: 923
- 13:42Z: 787 (relief)
- 15:12Z: 913
- 16:42Z: 869
- 18:12Z: 882
- 19:43Z: 889
- **21:10Z: 1016.6 — CRIT, climbing fast**

Last 5 readings (last 12 min): 1005 → 1006 → 1010 → 1009 → **1016.6** — bot is now in CRIT territory. Memory-relief log line at 13:12:59Z showed "reclaimed 0.0MB" — relief is firing but not freeing. This is the leading failure mode for the bot (wedge-on-memory pattern).

## Exposure Manager Tiers
All 7 strategies have positive portfolio_pnl = $19.74 (matched because they share session pnl). No strategy is paused. No consecutive_loss kill triggered. Tiers: btc=FULL, hype=FULL, bnb/sol/xrp=MODERATE, eth/doge=MINIMAL (post any prior loss streak).

## Side Selection (last pulse)
- LONG: bitcoin, bnb_macro (2)
- SHORT: sol_macro, xrp_macro (2)
- unknown/null: eth_macro, hype_macro, doge_macro (3)
- Per-strategy recent: bitcoin LONG 89/SHORT 32 (lookback 121), eth_macro LONG 60/SHORT 28, xrp_macro LONG 19/SHORT 70 — balanced

## Red Flags (synthesized)
1. **[BAD] RSS = 1016.6MB** — over 900MB threshold, climbing ~1MB/min, mem-relief reclaimed 0.0MB. Severity=crit priority=RSS_HIGH (bot already flagged). Lead indicator for future wedge.
2. **[WARN] 5 of 6 strategy_lane scans > 1000ms** — borderline over 1s lane scan rule. Could be RSI/data-fetch heaviness. Not blocking.
3. **[INFO] tiny sample** — 21 entries over 5h, lane-level n=1–5. No lane reads as calibrated winner or loser. Surface single-trade badges cautiously.

## Compares to Baseline (Shape)
- Shape: small sample, broken-out across 3 active strategies (eth/xrp/sol). Baseline concentrated more on bitcoin and hype.
- WR: 60% > 46.4% baseline (good)
- PnL/hr: $3.95 << $11.60 baseline (concerning — but n=21 is tiny)
- Single hero lane (eth|5m|up +$19.85) carrying 80% of session pnl — same shape as baseline where 1–2 lanes dominated
- Conflict: the bot is showing high WR but low pace; baseline is the inverse. Could be a gate regime change (more selective, fewer trades, higher quality).

## Action Items
- **WATCH**: RSS climbing — if it crosses 1100MB or mem-relief continues to reclaim 0, the wedge-risk window is open
- **WAIT**: more entries needed to confirm if WR=60% is real or tail. Re-check at Hour 2 tomorrow.
- **NO ACTION** on bot. Read-only brief.
