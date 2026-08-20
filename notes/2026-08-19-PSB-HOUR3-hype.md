# PSB HOUR 3 BRIEF — HYPE lane focus (LOCAL bot)

**Generated:** 2026-08-19 23:06 UTC (cron)
**Session:** test_20260819_134916
**Bot PID:** 58507 (alive, hb_age 11s, RSS 1220.6 MB — severity=crit self-flagged)
**Session started:** 2026-08-19T13:49:16 UTC (~9h17m elapsed)
**Bankroll:** $978.11 (cash, deployed 0, open 0)

---

## TL;DR

One [BAD] flag only: **RSS 1220.6 MB** (severity=crit from health row itself). No lane bleed, no heartbeat stale, no cycle lag, no pricing degeneracy. HYPE-specific volume is tiny (n=3 in session) but no calibrated-loser lane (all alpha_ewma values for live HYPE lanes are positive at sufficient n).

---

## Drift callout

Session realized **-$21.89** over 12 closed (4W-8L = **33.3% WR**). HYPE contributed **-$9.69** across 3 trades (1W-2L = 33.3%). HYPE is the worst lane by %PnL but the sample is tiny. RSS is the real red flag — the bot's self-monitoring has already escalated severity=crit.

## Checklist

- [BAD] **RSS 1220.6 MB > 900 MB threshold** — bot self-flagged severity=crit
- No other red flags fired
- No [WARN] from session realized ($-21.89, threshold -$25), per-lane worst (-$6.78 < -$15 threshold), loss_streak (0), long_loss_cluster (0)
- No [WARN] LANE alpha_ewma<-0.5 with n>=50 (no HYPE lane meets both criteria)

---

## HYPE per-lane table

| Lane | session n | WR | session PnL | alpha_ewma (calibration n) | Verdict |
|------|-----------|------|-------------|----------------------------|---------|
| hype\|15m\|up | 2 | 50.0% (1W-1L) | -$6.78 | +1.97 (n=11) | calibrated positive; sample too small |
| hype\|15m\|down | 1 | 0.0% (0W-1L) | -$2.91 | +4.70 (n=10) | calibrated positive; sample too small |
| hype\|15m\|down\|spike | 0 | — | — | -1.15 (n=9) | negative alpha, n<50 — not flag-eligible |
| hype\|15m\|up\|spike | 0 | — | — | +4.76 (n=1) | n too small |
| hype\|5m\|up\|standard | 0 | — | — | -2.14 (n=7) | negative alpha, n<50 — not flag-eligible |
| hype\|5m\|down\|bullish__bullish__bull\|override | 0 | — | — | +5.0 (n=1) | n too small |

**Verdict:** HYPE has no calibrated loser at n≥50. The 2 losses in this session were catastrophic_stop fires — both entered BUY_YES (one) and BUY_NO (one) on strong directional tape that reversed within 2-3 minutes. Not an alpha issue, a hold/exit timing signature. The single favorite-lane trade resolved cleanly (+$1.80).

---

## Trade-level forensics (HYPE only)

| trade_id | market | side | entry | exit | pnl | hold | exit reason |
|----------|--------|------|-------|------|-----|------|-------------|
| dry_1787174131 | 5:15-5:30 PM | BUY_NO | 0.57 | 0.37 | -$2.91 | 235s | hold_catastrophic_stop |
| dry_1787175939 | 5:45-6:00 PM | BUY_YES | 0.54 | 0.30 | -$8.58 | 161s | hold_catastrophic_stop |
| dry_1787179193 | 6:30-6:45 PM | BUY_YES | 0.92 | 1.00 | +$1.80 | 916s | RESOLVED:YES (real) |

Two catastrophic stops within ~30 min of each other — both at 161-235s hold. Entry RSI was 76-78 (overbought) on both longs. The favorite_lane entry was a different family (no indicator snapshot, no RSI) and won. Worth Claude's attention: are the RSI-soft-penalty + BUY_YES at RSI>75 entries actually getting filtered, or is the threshold not strict enough?

---

## Top flags (detailed)

### [BAD] RSS 1220.6 MB (severity=crit)

- **Fact:** bot_heartbeat.json shows rss_mb=1220.6; health row severity field = "crit"; priority = "RSS_HIGH"
- **Cause:** bot has been running ~9h17m, no OOM yet but the ratchet pattern is clear. Standard for a paper run to climb to ~1.2 GB without a leak; not a kill-switch condition but worth monitoring for swap growth.

### [INFO] hype_macro strategy_scan = 4,748 ms (single largest)

- **Fact:** cycle_timings_ms.strategy_scan_by_name_ms shows hype_macro=4748 vs ETH/SOL/DOGE/XRP/BNB all ~1.6s and bitcoin=44ms
- **Cause:** hype_macro is the only lane that fires BUY_NO (`window_delta_flip`) plus the standard BUY_YES path, doubling its candidate evaluation cost. Not a red flag (cycle still completes in 12.8s, under the 20s budget) but the asymmetry is notable.

### [INFO] OPS_JSON log path from brief doesn't exist

- **Fact:** brief referenced `data/logs/polybot_20260720.log` for OPS_JSON tail — file not found
- **Cause:** the live OPS_JSON equivalent is fully captured in `data/runtime/bot_runtime_status.json` (read this turn, gave bankroll/cycle/lane breakdown). Stale path in the cron prompt — minor doc drift.

---

## Heartbeat / process

- pid 58507 alive 2h17m of CPU
- rss 1220.6 MB → 1.25 GB
- phase = cycle_complete
- hb_age_s = 11 (fresh)
- priority = RSS_HIGH, severity = crit
- cycle_count = 231 (steady)
- cycle_elapsed_ms = 12,815 (under 20s budget ✓)
- scanner_sync_ms = 4,562 (wss healthy, no disconnect signature)
- cycle_interval_ms = 30,000 (nominal)
- trading lock detail: held by `{pid:12100, started:2026-08-19T08:39:46Z, mode:paper}` — pre-existing paper-mode lock from this morning; no conflict

## Exposure manager state (HYPE)

- tier: FULL
- multiplier: 1.0
- max_size: 25.0
- paused: false
- consecutive_losses: 1
- recent_pnl: -$9.69
- recent_trades: 3
- portfolio_pnl: -$21.89 (same as session realized — exposure manager tracks session)
- pause_reason: ""
- cycles_since_pause: 0
- conditions: trend_strength=0.6, volatility=0.0172 (elevated vs others), volume_ratio=1.0

## Pricing freshness

- updown_15m_count = 47 (well above 20 floor)
- no [INFO] pricing freshness sustained alert
- wss price_age_ms on last HYPE entry was 11.4 ms (live)

---

## Bottom line

HYPE itself is fine in calibration. The 2 catastrophic_stop fires are an exit-policy concern, not an alpha concern — RSI>75 longs entered and immediately reversed. The genuine red flag is RSS at 1220 MB (self-flagged crit). No bot intervention recommended; flag RSS ratchet and RSI-gating question to Claude.
