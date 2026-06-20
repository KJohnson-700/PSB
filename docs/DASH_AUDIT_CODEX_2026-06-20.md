## Executive Summary
- The dashboard and bot are correctly split into separate processes by `start.py`; the main remaining bot-impact risks are **shared disk I/O, CPU, memory, and file-read-while-write behavior**.
- The biggest dashboard slow/drop-out mechanism is still **sync disk and CPU work inside `async def` FastAPI handlers**, which serializes requests in a single-worker uvicorn process.
- The old Ghost Lab OOM path is mostly neutered server-side, but **dead frontend Ghost Lab code still calls the stub and can throw because it expects the old payload shape**.
- The highest-impact low-risk fixes are: move selected heavy handlers to sync `def` or `asyncio.to_thread`, add TTL/mtime caches, and remove dead client calls.
- Do **not** repeat the prior large dashboard rewrite; use small, reversible route-by-route changes with timing logs before and after.

## Endpoint Inventory

| route | def vs async | what it reads | per-request cost | cached? | health flag |
|---|---:|---|---|---|---|
| `/` | async | `src/dashboard/index.html` full file, optional env DSN | medium | no | BLOCKING |
| `/health` | async | env only | cheap | n/a | OK |
| `/api/dashboard/health-snippet` | async | env via `_health_payload()` | cheap | no | OK |
| `/api/ops/summary` | async | in-process bot or cached journal summary | medium | 6s journal TTL | OK |
| `/api/events` | async stream | config YAML once, then runtime status JSON, config YAML, journal summary, positions/snapshots | medium every ~2s | partial 6s summary TTL | SLOW |
| `/api/status` | async | runtime status JSON, config YAML, positions.json, journal summary, entries tail, snapshots tail | medium/heavy | partial 6s summary TTL | SLOW/BLOCKING |
| `/api/ai/health` | async | config YAML, env key presence, MiniMax probe | heavy/network | no | BLOCKING-ish |
| `/api/orderbook` | async | bot WS cache or async CLOB REST | medium/network | no | OK |
| `/api/usage/summary` | async | usage tracker memory | cheap | tracker-owned | OK |
| `/api/usage/records` | async | usage tracker memory, unbounded records | medium memory/response | tracker-owned | SLOW |
| `/api/ghosts/morning-summary` | async | none; disabled stub | cheap | n/a | DEAD |
| `/api/ghosts/lab` | async | none; disabled stub | cheap | n/a | DEAD |
| `/api/ghosts/regime-breakdown` | async | none; disabled stub | cheap | n/a | DEAD |
| `/api/ghosts/decision-digest` | async | none; disabled stub | cheap | n/a | DEAD |
| `/api/live/performance` | async | current session summary, `TradeJournal.get_closed_trades()` | medium | partial summary cache | SLOW |
| `/api/live/drift` | async | none; deprecated response | cheap | n/a | DEAD |
| `/api/live/status` | async | runtime status JSON | cheap | no | OK |
| `/api/live/start` | async POST | runtime status JSON, subprocess spawn | medium | no | OK/manual |
| `/api/live/stop` | async POST | creates `data/KILL_SWITCH` | cheap | n/a | OK/manual |
| `/api/live/shutdown` | async POST | runtime status JSON, sends signals | cheap | no | OK/manual |
| `/api/live/resume` | async POST | deletes `data/KILL_SWITCH` | cheap | n/a | OK/manual |
| `/api/tests/results` | async | runs full pytest subprocess, captures output | very heavy | no | BLOCKING/manual |
| `/api/strategy/watchlist` | async | config YAML, live scan JSON, external spot fetches in threads | heavy | no | SLOW |
| `/api/strategy/metrics` | async | summary, backtest report JSONs, latest scan JSON, positions | medium | partial summary cache | SLOW/DUP |
| `/api/journal/invalidate-cache` | async POST | clears in-memory caches | cheap | n/a | OK/manual |
| `/api/journal/summary` | async | `TradeJournal` session load, entries/positions/summary | medium/heavy for archive/session_id | active cached indirectly | SLOW/BLOCKING |
| `/api/journal/positions` | async | `TradeJournal`, positions | medium | active journal cache | OK/SLOW |
| `/api/journal/trades` | async | `TradeJournal`, all closed trades response | medium/unbounded | active journal cache | SLOW |
| `/api/journal/exit-reason-summary` | async | closed trades aggregation | medium | mtime cache | OK |
| `/api/journal/action_breakdown` | async | closed trades aggregation | medium | mtime cache | OK |
| `/api/lane_gates` | async | config YAML, lane state JSON/meta | medium | no | SLOW |
| `/api/journal/trade-points` | async | closed trades, capped output | medium | active journal cache | OK/SLOW |
| `/api/session/equity_history` | async | full `snapshots.jsonl`, decimates after loading | heavy if long session | no | BLOCKING |
| `/api/journal/trade_journey` | async | all closed trades, sorts | medium | active journal cache | DEAD-ish |
| `/api/journal/updown_breakdown` | async | 7 days of logs plus all closed trades | heavy | no | BLOCKING |
| `/api/strategy/reason-buckets` | async | entries.jsonl via `get_all_entries`, then calls watchlist | heavy | no | BLOCKING |
| `/api/journal/entries` | async | full entries.jsonl then last N | heavy as file grows | no | BLOCKING |
| `/api/journal/lane-health` | async | lane entries/closed trades | medium/heavy | no specific cache | SLOW |
| `/api/journal/lane-states` | async | lane states plus lane health | medium/heavy duplicated | no | SLOW/DUP |
| `/api/lane-state-history` | async | full lane audit JSONL then tail | medium if log grows | no | SLOW |
| `/api/journal/snapshots` | async | full snapshots.jsonl then last N | heavy as file grows | no | BLOCKING |
| `/api/journal/sessions` | async | session directories + summary files | medium | no | SLOW |
| `/api/journal/prune-short-sessions` | async POST | session dirs/files, optional delete | heavy/manual | no | OK/manual |
| `/api/journal/session/{session_id}` | async | constructs `TradeJournal`, summary | heavy for archive | no | SLOW |
| `/api/journal/settle-archived` | async POST | subprocess up to 120s | very heavy | no | BLOCKING/manual |
| `/api/journal/ai-summary` | async | journal summary, entries scan, all sessions, optional MiniMax call | very heavy | text cache only | BLOCKING |
| `/api/exposure` | async | runtime status JSON or in-memory managers | cheap | no | OK |
| `/api/exposure/pause`, `/resume`, `/{lane}` | async POST | in-memory managers | cheap | n/a | OK/manual |
| `/api/bitcoin/analysis` | async | in-memory/background BTC cache | cheap | singleton cache | OK |
| `/api/bitcoin/candles` | async | Binance klines via `to_thread`, pandas iteration | medium/network | no | OK/SLOW |
| `/api/sol/analysis` | async | external/service analysis via `to_thread` | heavy/network | service caches only | OK/SLOW |
| `/api/eth/analysis` | async | external/service analysis via `to_thread` | heavy/network | service caches only | OK/SLOW |
| `/api/hype/analysis` | async | config YAML inside thread, Hyperliquid analysis | heavy/network | service caches only | OK/SLOW |
| `/api/xrp/analysis` | async | external/service analysis via `to_thread` | heavy/network | service caches only | OK/SLOW |
| `/api/doge/analysis` | async | external/service analysis via `to_thread` | heavy/network | service caches only | OK/SLOW |
| `/api/bnb/analysis` | async | external/service analysis via `to_thread` | heavy/network | service caches only | OK/SLOW |
| `/api/macro_align/series` | async | 7 kline fetches via `to_thread`, pandas iteration | heavy/network | 18s TTL | OK/SLOW |
| `/api/config` | async | config YAML | cheap/medium | no | OK |
| `/api/config` POST | async | config YAML read/write, live apply | medium/manual | no | OK/manual |
| `/api/lane-state` POST | async | config YAML read/write, audit JSONL append | medium/manual | no | OK/manual |
| `/api/trade` POST | async | market scanner and CLOB order path | heavy/manual | no | OK/manual |
| `/api/paper/reset` POST | async | journal/session mutation under bot lock | heavy/manual | no | OK/manual |

## Bot-Impact Risks (Ranked)

1. **Async handlers do synchronous journal/log file scans on the uvicorn loop.**  
   Evidence: `/api/journal/snapshots` calls `j.get_snapshots()` at `src/dashboard/server.py:4572`, and `TradeJournal.get_snapshots()` reads the full `snapshots.jsonl` before slicing at `src/execution/trade_journal.py:716`. `/api/journal/entries` calls `get_all_entries()` at `server.py:4518`, and that reads full `entries.jsonl` before slicing at `trade_journal.py:704`. These endpoints are called by the frontend journal/chart paths at `index.html:5812`, `index.html:5857`, `index.html:6031`, and every 30s on the journal view at `index.html:8196`.  
   Why it can hit the bot: the dashboard process can consume disk bandwidth/page cache and CPU while the bot writes the same session files.

2. **`/api/status` and `/api/events` repeat disk state assembly at high cadence.**  
   Evidence: SSE sends every configured interval, default 2s, at `server.py:1323` and `server.py:1607`. In split mode it reads `bot_runtime_status.json` at `server.py:1385`, may call `j_runtime.get_summary()` at `server.py:1405`, reads disk positions at `server.py:1414` or `server.py:1418`, and may read config YAML at `server.py:1441`. `/api/status` repeats similar work at `server.py:2003`, `server.py:2016`, `server.py:2020`, `server.py:2031`, `server.py:2047`, and `server.py:2073`.  
   Why it can hit the bot: this is constant shared-file polling against files the bot updates on the hot path.

3. **`/api/strategy/reason-buckets` chains multiple heavy paths in one async request.**  
   Evidence: it reads recent entries at `server.py:4446` via `TradeJournal.get_all_entries()` and then awaits `get_strategy_watchlist()` at `server.py:4500`. The watchlist loads config at `server.py:3539`, starts multiple spot fetches at `server.py:3630`, and reads the latest scan JSON at `server.py:3652`. The frontend fetches it as part of the heavy performance tier at `index.html:3866`.  
   Why it can hit the bot: a single operator tab can combine journal disk scans, latest scan JSON reads, CPU parsing, and external market calls.

4. **`/api/session/equity_history` scans full snapshots and only decimates after building the list.**  
   Evidence: full file loop at `server.py:4274`, unbounded `points.append()` at `server.py:4296`, decimation only after all rows are loaded at `server.py:4299`. Frontend seeds Command Center history through this endpoint at `index.html:6103`.  
   Why it can hit the bot: long sessions can create avoidable memory and disk pressure.

5. **Manual endpoints can monopolize the dashboard process if clicked.**  
   Evidence: `/api/tests/results` runs pytest in an async handler with `subprocess.run(... timeout=120)` at `server.py:3451` and `server.py:3465`; `/api/journal/settle-archived` runs a 120s settle subprocess at `server.py:4618` and `server.py:4626`; `/api/journal/ai-summary` can do journal aggregation plus an external 20s MiniMax call at `server.py:4898` and `server.py:4955`.  
   Why it can hit the bot: even though manual, these can drive CPU/memory/disk on the same 2GB VPS and make panels vanish while the single uvicorn loop is occupied.

6. **Dead Ghost Lab client path is still wired.**  
   Evidence: server stubs `/api/ghosts/*` at `server.py:3039`, `server.py:3129`, `server.py:3144`, and `server.py:3152`. The frontend still switches to `ghosts` at `index.html:3561`, calls `loadGhostLab()` at `index.html:3587`, on visibility at `index.html:3641`, on init at `index.html:8179`, and assumes old fields like `_gl.raw.lanes` at `index.html:8287`.  
   Why it can hit the bot: server-side load is gone, but frontend exceptions explain drop-outs/vanishing panels and noisy retries/operator confusion.

## Slow-Load + Drop-Out Root Causes

- **Head-of-line blocking inside one dashboard worker.** Most routes are `async def`, but many do blocking file I/O directly. When one request is parsing entries/snapshots/logs, other dashboard requests queue. Browser-side `fetchT()` aborts after 6-14s at `index.html:3774`, but aborting the browser request does not undo server CPU/disk work already running.
- **Overlapping poll batches.** `fetchAll()` fires status, strategy metrics, exposure, journal/updown, and sometimes heavy performance endpoints together at `index.html:3848`. It repeats every 14s at `index.html:8194`; performance heavy tier runs every third poll and on tab open at `index.html:3824` and `index.html:3583`.
- **SSE and polling both update the same visible state.** SSE runs every ~2s at `server.py:1323`; `fetchAll()` also updates status every 14s at `index.html:3849`. If `/api/status` or the SSE generator stalls on disk work, UI state can appear to load, then be overwritten or stop refreshing.
- **Journal tab triggers extra non-timeout fetches.** `loadJournalTab()` uses raw `fetch()` with no abort for positions, trades, and entries at `index.html:5815`, `index.html:5836`, and `index.html:5857`. `loadPnLChart()` also uses raw `fetch()` at `index.html:6031`. A slow server response can leave stale DOM or no update.
- **Ghost tab expects removed response shape.** `/api/ghosts/lab` now returns `{disabled: true, ...}` at `server.py:3137`, but `_glRenderControls()` assumes `_gl.raw.lanes.slice()` at `index.html:8287`. That is a concrete “load then vanish/fail to refresh” class of bug.
- **Unbounded or oversized responses remain.** `/api/journal/trades` returns all closed trades at `server.py:3899`; `/api/usage/records` returns all usage records at `server.py:2251`; `/api/journal/sessions` returns every listed session at `server.py:4580`. Even with recently purged data, these are future regressions as data grows.

## Prioritized Action Plan

### P0 — Lowest-Risk, Highest-Impact

| item | what to change | expected impact | risk | reversible |
|---|---|---|---|---|
| P0.1 | Convert heavy read-only endpoints from `async def` to plain `def`: `/api/journal/entries`, `/api/journal/snapshots`, `/api/session/equity_history`, `/api/journal/updown_breakdown`, `/api/strategy/reason-buckets`, `/api/journal/ai-summary`, `/api/tests/results`, `/api/journal/settle-archived`. | FastAPI runs sync handlers in threadpool, reducing event-loop stalls and panel drop-outs. | Low/medium: route signatures unchanged, but threadpool saturation must be monitored. | One-line per endpoint revert back to `async def`. |
| P0.2 | Remove or hard-disable the frontend Ghost Lab entry path: do not call `loadGhostLab()` when `psb_main_view=ghosts`; render a static “removed” notice. | Eliminates frontend exceptions from stub payload mismatch. | Low. | Re-enable the tab/calls if Ghost Lab returns real payload again. |
| P0.3 | Add server timing logs for endpoints over 500ms and include route name + elapsed only. | Makes bottlenecks measurable before changing internals. | Low. | Remove middleware/log wrapper. |
| P0.4 | Add abort timeouts to raw journal fetches: `loadJournalTab()`, `loadPnLChart()`, `loadJournalSessionList()`, `loadSessionHistory()`. | Prevents panels waiting indefinitely and preserves last-good state. | Low. | Replace `fetchT` calls with previous `fetch`. |
| P0.5 | Stop polling dead `/api/backtest/reports` code path entirely. `needBacktest` is stale at `index.html:3819`; route is removed at `server.py:3141`. | Reduces dead error noise and simplifies poll batch. | Low. | Restore if backtest tab returns. |

### P1 — Small Caches and Bounded Reads

| item | what to change | expected impact | risk | reversible |
|---|---|---|---|---|
| P1.1 | Add mtime+limit TTL cache for `/api/journal/snapshots` and `/api/session/equity_history`; read tails or reservoir-decimate while streaming instead of building full lists. | Cuts disk reads and memory pressure from chart refreshes. | Medium: chart shape must be verified. | Keep old implementation behind helper and switch back. |
| P1.2 | Replace `TradeJournal.get_all_entries(limit)` dashboard usage with a tail-reader helper that does not parse the whole file. | Directly reduces entries.jsonl disk/CPU cost. | Medium: must handle partial last line during bot write. | Fallback to existing `get_all_entries()` on parse errors. |
| P1.3 | Cache `/api/strategy/reason-buckets` for 30-60s keyed by entries mtime + latest scan mtime. | Heavy performance tab becomes cheap between polls. | Low/medium: reason buckets can lag by TTL. | Clear cache on journal invalidate or set TTL to 0. |
| P1.4 | Cache `/api/lane_gates` by config mtime and lane audit mtime. | Avoid repeated YAML/state rebuild every performance interval. | Low. | Remove cache wrapper. |
| P1.5 | Bound `/api/journal/trades` with a default `limit` and add a separate archived/full export if needed. | Prevents closed-trade response growth from reintroducing slow panels. | Medium: frontend expects all trades in some views. | Keep old unlimited behavior behind `limit=all` for manual use. |

### P2 — Structural Cleanup, Still Incremental

| item | what to change | expected impact | risk | reversible |
|---|---|---|---|---|
| P2.1 | Create one dashboard data-access module for safe tail reads, mtime TTL, and atomic read retries. | Reduces duplicate file-read logic without touching UI behavior. | Medium. | Migrate one endpoint at a time. |
| P2.2 | Deduplicate SOL/ETH/XRP/DOGE/BNB analysis route registration through a small table, preserving current handlers/URLs. | Reduces duplicate code and future drift. | Low/medium. | Keep wrappers calling shared helper. |
| P2.3 | Add “last good payload” server cache for status/SSE so a transient file-write race returns previous state plus `stale=true`. | Fewer panel drop-outs during bot writes. | Medium: must not hide stale state too long. | TTL-gated cache can be disabled. |
| P2.4 | Move manual heavy jobs (`pytest`, settle archived, AI summary) to background job endpoints with status polling. | Prevents manual clicks from blocking dashboard interactivity. | Medium/high. | Keep old route as admin-only fallback during transition. |
| P2.5 | Delete dead Ghost Lab/backtest frontend/server code after the static disabled state has shipped cleanly. | Smaller dashboard and fewer accidental calls. | Low, but deletion should be separate. | Git revert of deletion commit. |

## Explicit DO-NOT List

- Do **not** rewrite `server.py` or `index.html` wholesale. The prior 525-line destructive dashboard rewrite and loop-wedge regression make a big-bang split too risky.
- Do **not** re-enable in-process Ghost Lab parsing of `data/calibration/rejected_candidates_settled.jsonl`; even at ~226MB, it is still the wrong shape for a live dashboard on a 2GB VPS.
- Do **not** move dashboard and bot back into the same asyncio loop or same long-lived process for convenience.
- Do **not** add pandas/DataFrame parsing to request handlers for journal/ghost/trade files.
- Do **not** add faster frontend polling to mask slow endpoints. It will increase queueing and shared-file contention.
- Do **not** make the dashboard write, compact, or repair bot hot-path files during normal page load.
- Do **not** add broad locks around shared files that the bot also needs unless the bot write path already uses compatible non-blocking/atomic patterns.
- Do **not** remove browser aborts; they are not sufficient alone, but they are still useful for panel resilience.
- Do **not** treat disabled/dead endpoints as harmless if the frontend still calls them and assumes old payloads.

## Metadata/Summary

**Tags:** #PSB #DashboardAudit #FastAPI #AsyncIO #TradingBot #Performance  
**Related Concepts:** [[FastAPI async blocking]], [[Dashboard polling]], [[TradeJournal]], [[Ghost Lab]], [[Shared file contention]], [[SSE]]  
**Summary:** The dashboard is process-isolated from the trading bot, but many async handlers still do synchronous file and CPU work that can stall the single dashboard worker and consume shared VPS resources. The safest path is incremental: move the worst endpoints to threadpool/sync handlers, add bounded tail reads and TTL caches, and remove dead Ghost/backtest client calls before any broader cleanup.
