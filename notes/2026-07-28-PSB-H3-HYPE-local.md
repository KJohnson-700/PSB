# PSB HOUR 3 — HYPE (LOCAL)

- Captured: 2026-07-28T20:16 UTC
- Session: `test_20260728_074851`
- Health row: `priority=BOT_DEAD`, `severity=dead`, `pid=null`, realized `+$50.44`, closed 45, wins 15, loss streak 0, open 6. This conflicts with live kernel/runtime: PID 90135 is running and heartbeat is fresh; likely health monitor PID classification lag/false positive.
- Heartbeat: PID 90135, `cycle_complete`, RSS 230.4 MB, heartbeat age 8.6s.
- Runtime: paper, cycle 328, elapsed 10,222 ms, overrun 0; per-lane scans max 894 ms. No CYCLE_LAG or scan breach.
- Latest OPS_JSON inspected was stale (2026-07-21, session `test_20260720_181702`) and must not be used for current-session metrics.
- Current-session HYPE entries: zero records in `entries.jsonl`; minimal/no HYPE activity.
- HYPE exposure manager: unpaused, FULL tier, multiplier 1.0, recent trades 0, recent PnL $0.
- Current calibration keys: no exact current-session HYPE posterior. Historical source-resolver HYPE entries include negative alpha values, but all relevant observed n are below 50; no calibrated-loser warning under the n>=50 rule. Legacy aggregate HYPE rows are mixed and stale.
- Pricing freshness/count unavailable in current health; stale OPS had `updown_15m_count=61`, not current-session evidence.
- Flags: BAD health monitor reports BOT_DEAD despite live PID 90135 and fresh heartbeat; INFO current-session OPS pulse unavailable/stale; no HYPE alpha warning, loss-streak warning, cycle lag, RSS, or pricing-count conclusion.

## Telegram compression
