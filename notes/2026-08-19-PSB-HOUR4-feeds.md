# PSB HOURLY BRIEF — HOUR 4 OF 6 (LOCAL bot)

**Date:** 2026-08-19T19:03Z (cron fired ~19:03:39Z heartbeat)
**Session:** test_20260819_013946
**Cycle:** 1002 (paper)
**Heartbeat age:** 27s — alive, NOT stale
**RSS:** 1376.2 MB
**Cycle elapsed:** 15439ms

## Headline

Feeds axis is the most degraded dimension this hour. Bot alive and cycling fine, but **oracle ages are out of bounds** for HYPE/DOGE and **WSS coverage is sustained-low at 11.4%** (REST fallback doing 88.6% of pricing). RSS ratcheting (1300→1376 since last brief). Session PnL drift to −$59.34 (35.2% WR vs baseline 46.4%) but no single lane has hit n≥50 alpha-bleed threshold yet — the lane_posteriors file has zero recent updates matching current lane_id format (last_updated May 2026).

## Feed health (HOUR 4 focus)

| Metric | Value | Threshold | Status |
|---|---|---|---|
| WSS connections (1h) | 513 reconnects | sustained flap? | OK |
| ws_cov (rolling) | 0.114 (>120s sustained) | n/a (info) | [INFO] sustained low |
| HYPE oracle_age_s | 2593s (ref_spot=True) | 180s (hype gate) | **OVER** |
| DOGE oracle_age_s | 916s | 450s (doge gate) | **OVER** |
| XRP/SOL/ETH oracle_age_s | 37-99s | 180s | OK |
| Oracle basis (HYPE) | +0.0bps | n/a | spot-only fallback |
| BTC spot | $68,398 | — | fresh |
| All-spot BULLISH (8/8) | yes | — | bullish regime cluster |
| updown_15m_count | 51 | ≥20 | OK |
| updown_5m_count | 14 | ≥20 | OK |
| AI providers | 2 (ready, no missing keys) | — | OK |

**WSS flap pattern:** three WS reconnects within last 90s (12:02:26, 12:03:03, 12:04:23 — 37s + 80s gaps). Pattern G territory but not continuous dropouts.

**Stale-spot oracle bypass:** `stale_spot_is_settlement` working-tree change in `updown_composite_score.py` — HYPE `ref_spot=True` with oracle_age_s=2593s is *passing* the gate. This is the bypass Slim flagged in the working tree. Currently live and working as designed, but worth noting that HYPE 15m signals are getting priced against 43-min-old spot. Watch for drift in next hour.

## Lane PnL (from health row)

| Lane | n | W | WR | PnL | Verdict |
|---|---|---|---|---|---|
| bitcoin|15m|up | 24 | 6 | 25.0% | **-$33.76** | [WARN] lane ≤ -15, WR << baseline |
| sol|1h|up | 4 | 0 | 0.0% | **-$22.80** | [WARN] lane ≤ -15 |
| hype|15m|up | 13 | 7 | 53.8% | **-$15.17** | [WARN] lane ≤ -15, WR ok but hold_policy bleeding |
| xrp|1h|up | 6 | 3 | 50.0% | +$12.56 | OK |
| xrp|5m|up | 21 | 8 | 38.1% | +$3.17 | OK |
| xrp|1h|down | 1 | 0 | 0.0% | -$5.45 | small sample |
| doge|5m|down | 1 | 1 | 100% | +$4.80 | small sample |
| hype|15m|down | 1 | 0 | 0.0% | -$2.69 | small sample |

**Session:** 71 closed, 25 wins = **35.2% WR** (baseline +$869.90 @ 46.4% WR over 75h, 1138 entries)

**Loss cluster:** loss_streak=3, long_loss_cluster=3 — NOT yet at the [WARN] thresholds (5 / 4). But consecutive_losses counter shows btc=4, sol=4, hype=1, xrp=1 — drift toward the EXPOSURE_LOSS_KILL configured-but-disabled-in-paper threshold of 3 (btc & sol are at 4 → if this were live the exposure_loss_kill would have armed already).

## Red flag tally

- [WARN] session realized -$59.34 (≤ -25 threshold)
- [WARN] 3 lanes ≤ -$15 (bitcoin|15m|up, sol|1h|up, hype|15m|up)
- [WARN] RSS 1376MB (above 900MB threshold; ratchet +76MB since last brief)
- [WARN] WR drift: 35.2% session vs 46.4% baseline (−11.2pp)
- [INFO] WSS ws_cov=0.114 sustained low; 3 reconnects in 90s
- [INFO] HYPE oracle_age_s=2593s (43min) — over 180s gate, ref_spot=True bypass active
- [INFO] DOGE oracle_age_s=916s — over 450s gate

**No [BAD] flags.** Bot alive, cycle not lagging, pricing not degenerate, AI ready.

## Verdict

Feeds axis: HYPE oracle staleness + sustained low WSS coverage = real but contained. Not a kill condition. Lane PnL drift is the dominant signal this hour, but mostly contained to two lanes (bitcoin|15m|up at 25% WR over 24 trades, sol|1h|up at 0% over 4 trades — small sample for sol). XRP lanes profitable. Bot stable to monitor.

## What I did not change

- No patches, no kills, no restarts
- No config edits
- No repo writes (this note lives in `notes/`, which the cron brief is permitted to write to)