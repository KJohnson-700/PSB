# PSB Bot Audit Note — Startup/Scanner Session Issues
Date: 2026-04-28

## Findings

1. Duplicate startup auto-backtests
- Startup auto-backtests are launching more than once for the same session and strategy.
- This creates duplicate reports, wasted CPU, and misleading backtest visibility on the dashboard.
- Likely cause: startup dedupe is only in-memory and is not robust to repeated boot or `set_bot_instance(...)` paths.

2. Weather candidate pollution
- Dedicated weather fetch is still admitting many non-weather markets into the weather candidate pool.
- This wastes scan budget and helps explain why live weather frequently collapses into zero usable city/temp/precip matches.
- The current candidate filter is still too broad for some slug/title patterns.

3. Scanner timeout is longer than cycle interval
- `scanner_sync_timeout_sec` is greater than `cycle_interval_sec`.
- Result: one slow scanner cycle can overrun the next cycle and effectively blind the bot for extended periods.
- Current behavior degrades poorly because timeout often results in an empty scan instead of partial usable results.

4. Empty-session suppression is needed
- Fresh paper sessions were being surfaced even when no trades occurred.
- This polluted journal history, confused dashboard session selection, and weakened evaluation quality.
- Empty runs should not be treated as meaningful sessions unless they contain actual learned or diagnostic value.

## Action Items

1. Make startup auto-backtests idempotent across the full boot flow.
- Use persistent per-session/per-spec dedupe, not only process-memory guards.

2. Tighten dedicated weather candidate filtering.
- Restrict to stronger weather-shaped slug/question patterns.
- Prefer explicit temperature/precipitation structures over broad keyword hints.

3. Change scanner timeout behavior.
- Timeout budget should be below cycle interval.
- Return partial results when one source is slow instead of zeroing the whole cycle.

4. Suppress empty sessions by default.
- Do not persist or feature sessions with zero entries and zero exits.
- If a no-trade run still produced a meaningful lesson, log it as an operator or diagnostic note rather than a paper-trade session.

## Update

- Empty-session suppression was implemented in `src/execution/trade_journal.py` on 2026-04-28.
- Startup auto-backtest dedupe was hardened in `src/dashboard/server.py` on 2026-04-28.
- Dedicated weather candidate filtering was tightened in `src/market/scanner.py` on 2026-04-28.
- Current status:
  - empty summary-only folders no longer count as resumable sessions
  - empty summary-only folders no longer appear in session history
  - summary files are no longer auto-written for zero-activity sessions during journal load
  - startup auto-backtests now reserve their dedupe key under a lock before spawning
  - dedicated weather fetch now only admits temperature-market and measurable-precipitation market shapes
