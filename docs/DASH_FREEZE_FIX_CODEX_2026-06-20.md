## Dashboard Freeze Fix — 2026-06-20

### Root Cause

The dashboard freeze was caused by **blocking dashboard work running inside async FastAPI handlers on the single uvicorn event loop**. In this deployment model, one stuck async handler blocks every other request, including `/health` and `/`.

Verified blocking surfaces:

- `src/dashboard/server.py:1395` `/api/events` previously did config YAML reads, runtime-status JSON reads, journal summary/open-position reads, and bankroll tail scans directly inside the async SSE loop every ~2s.
- `src/dashboard/server.py:1677` `/api/status` assembled journal, position, config, runtime, and ops payloads in an async handler.
- `src/dashboard/server.py:3598` `/api/strategy/metrics` read summary/report/scan JSON in an async handler.
- `src/dashboard/server.py:2105` `/api/orderbook` had an unbounded CLOB REST fallback await.

No `TradeJournal` `threading.Lock` was found. `src/execution/trade_journal.py` query methods read in-memory state and files without a shared lock, so the freeze mechanism is event-loop head-of-line blocking, not a journal lock deadlock.

### Changes Made

- Added `_run_dashboard_blocking()` in `src/dashboard/server.py:1031` to run blocking dashboard payload builders with `asyncio.to_thread()` plus `asyncio.wait_for()`.
- Moved `/api/status` heavy assembly into `_get_status_payload_sync()` and call it off-loop with a 12s deadline.
- Rebuilt `/api/events` so each SSE snapshot calls the status payload off-loop with a 4s deadline. If it misses, SSE emits an error heartbeat instead of freezing the event loop.
- Moved `/api/strategy/metrics` heavy assembly into `_get_strategy_metrics_payload_sync()` and call it off-loop with a 5s deadline.
- Added `/api/scanner/health` at `src/dashboard/server.py:1688`. It reads only in-memory `last_cycle_times` or the small runtime-status JSON and does not parse journal data.
- Added a 4s timeout around `/api/orderbook` REST fallback calls.
- Added browser-side per-URL fetch backoff and a non-overlapping `fetchAll()` guard in `src/dashboard/index.html`.

### Status Orb Fix

The scanner orb no longer depends only on `/api/strategy/metrics`.

- `fetchAll()` now calls `/api/scanner/health` with a 2s timeout.
- If scanner-health returns fresh data and the newest cycle is old, the orb shows real scanner lag/stuck.
- If scanner-health cannot be fetched, the orb shows **Dashboard blind** with a tooltip explaining that the dashboard cannot reach scanner health and this is not proof the bot is stalled.
- Failed fetches back off up to 60s so the browser does not flood the loopback dashboard while sockets are already piled up.

### Junk-Removal Regression Check

Confirmed no dashboard code path reads the removed stale shadow logger files:

- `window_delta_shadow`
- `neutral_sitout_shadow`
- `exit_excursion_shadow`

Current dashboard/server references do not depend on those files, so commit `8615941` did not explain this dashboard freeze.

### Validation

- Passed: `.venv/bin/python -m py_compile src/dashboard/server.py`
- Ran: `.venv/bin/python -m pytest tests/test_dashboard_bundle.py -q`
- Result: 62 passed, 7 failed.

The remaining failures are pre-existing dashboard/test drift unrelated to this freeze fix: disabled Ghost Lab endpoint expectations, missing old Ghost tab marker expectations, bankroll snapshot expectation drift, and an action-breakdown string expectation. The status-route 504s caused by the first timeout setting were fixed before the final run.

### Restart Required

The VPS bot must be restarted after copying these files, because the dashboard runs in-process with the trading bot. `index.html` is served disk-fresh after restart, and `server.py` route changes require process restart.

### Metadata/Summary

Tags: #PSB #Dashboard #FastAPI #Uvicorn #ProductionIncident #ScannerHealth

Related Concepts: [[Dashboard Event Loop]], [[FastAPI Blocking I/O]], [[Server-Sent Events]], [[Scanner Health Orb]], [[Runtime Status]]

Summary: The hard freeze came from blocking disk/journal/status work inside async dashboard handlers on a single uvicorn event loop. The fix moves the heavy payloads off-loop with deadlines, adds a lightweight scanner-health endpoint, and makes the UI distinguish scanner staleness from dashboard fetch failure.
