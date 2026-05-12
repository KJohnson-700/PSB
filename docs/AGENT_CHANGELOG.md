# Agent changelog (backfill)

**Purpose:** Record **what shipped** when work was done in **Claude Code, Codex, Cursor**, or similar **without** a matching entry in the Obsidian strategy log or a written operator handoff. **Git remains the source of truth**; this file is a readable index.

**Strategy tuning and hypothesis tracking** still belong in `projects/polymarket-bot/strategy-log/` per `AGENTS.md`. This doc covers **codebase / infra / dashboard** provenance only.

**Canonical repo for this bot:** `https://github.com/KJohnson-700/PSB` (see `AGENTS.md` — do not confuse with other GitHub projects).

---

## 2026-05-12 — Crypto updown replay: oracle basis uses 1m spot (not HTF close)

**`UpdownBacktestEngine`:** `oracle_max_basis_bps` compared Chainlink to **`ta.current_price`**, which is the last **4h/1h** close before the window—often very stale vs a fresh oracle tick, so ETH (and other) backtests with oracle replay could show **zero trades** while the same run with `oracle_history=None` still produced signals. Basis now prefers **last 1m close strictly before `window_open`**, falling back to `ta.current_price` if 1m is missing (**`src/backtest/updown_engine.py`**). **`tests/test_backtest_oracle_replay.py`:** align ETH patches with **`_get_sol_htf_bias`**, keep **`_ohlcv_1m`** open≠close so windows can settle.

## 2026-05-12 — Pytest and crypto CLI: explicit completion banners

**`tests/conftest.py`** — **`pytest_sessionfinish`** prints a **`PYTEST COMPLETE`** footer (pass vs non-zero exit). **`scripts/run_backtest_crypto.py`** prints **`CRYPTO BACKTEST COMPLETE (SYMBOL Xm)`** after a successful full run.

## 2026-05-12 — Dashboard Backtest: single Run Crypto BT button

Removed legacy **Run Backtest** stub (duplicate UX; **`startBacktest()`** only warned about rigorous mode). **`Run Crypto BT`** uses **`crypto-bt-run-btn`**. **`dashboard_ui_rev`:** **`2026-05-12-backtest-single-run-btn`**.

## 2026-05-12 — Dashboard Backtest: remove optional test-split date input

Backtest tab no longer shows the walk-forward **`test_start`** date picker; dashboard runs stay full-range in-sample unless **`--test-start`** is passed from CLI. **`dashboard_ui_rev`:** **`2026-05-12-backtest-no-test-date-ui`**.

## 2026-05-12 — Dashboard Backtest tab: mirrors live strategies from settings.yaml

**`GET /api/backtest/reports`** adds **`live_scope`** (crypto **`strategies.*.enabled`**, **`windows`** [5,15,30] aligned with live routing buckets, **`weather_enabled`**). **`index.html`** renders only enabled journal keys (**`bitcoin`**, **`*_macro`**), dropdown options built from that scope, **weather** summary only when **`strategies.weather.enabled`**. **XRP dump-hedge** sim removed from dashboard UI (not live); **`xrp_dump_hedge_sim`** JSON remains excluded from crypto card dedupe in **`server.py`**. Weather reports use **`backtest_weather_*.json`** under **`data/backtest/reports/`**. **`dashboard_ui_rev`:** **`2026-05-12-backtest-live-scope`**.

## 2026-05-12 — Live crypto macro direction rule: alt-first, BTC-secondary

Operator correction: **all non-BTC live strategies must choose direction from the asset’s own alt indicators first**; BTC is secondary context/fallback and must not override a usable alt HTF side. Patched **`sol_macro.py`** base behavior and **`eth_macro.py`** so `direction_source: hybrid` no longer lets BTC/market proxy choose over ETH 1H. Important cleanup note: **`hype_macro.py`** and **`xrp_macro.py` currently inherit `SolMacroStrategy` in code while swapping their own services/market filters**, so they are affected by base-class behavior even though they are not SOL strategies and should retain their own oracle/data assumptions. Follow-up audit: split/rename the shared alt macro base or verify each subclass override so no HYPE/XRP/ETH path accidentally inherits SOL-specific assumptions.

## 2026-05-11 — Dashboard: Performance Exit Reasons strip (undefined `$`)

**`updateExitReasons`** called `$('…')` but **`$`** was only defined inside **`updateLivePerf`**, so successful **`/api/journal/exit-reason-summary`** responses threw **`ReferenceError`**, **`fetchAll` caught it**, and the bar stayed blank. Added local **`const $ = (id) => document.getElementById(id)`** inside **`updateExitReasons`**. **`dashboard_ui_rev`:** `2026-05-11-exit-reasons-strip-fix`.

## 2026-05-11 — Dashboard: Performance tab polls journal summary for Session Summary

**`fetchAll`** uses **`needJournalSummary = needJournal || needPerformance`** so **`GET /api/journal/summary`** runs on **Performance**, not only **Journal**. **`updateSessionSummary(j)`** always runs off that payload; **`updateJournal(j)`** still only when **Journal** and not archive-pinned. Fixes empty **Session Summary** card when staying on Performance. **`dashboard_ui_rev`:** `2026-05-11-performance-session-summary-poll`.

## 2026-05-11 — Dashboard: Journal session history + cleaner BTC header

- **Test Session History** moved **Performance → Journal** (after **Test runs**; same `/api/journal/sessions` + row click → `selectJournalSession`). **`loadSessionHistory()`** tied to Journal tab, journal poll, and refresh when Journal is active.
- **BTC card:** removed **`#btc-signal-badges`** mini gate summary (duplicate of **Signal Gates** below). Removed **`syncCryptoGateSummaryBar`**.
- **`dashboard_ui_rev`:** superseded by **Performance session-summary poll** bump (verify **`/health`**).

## 2026-05-11 — Dashboard: real 30m MACD & % moves on existing rows

**SOL/BTC service:** `SOLAnalysis.macd_30m`, `BTCSOLCorrelation` 30m %-moves (1m×31), 120×1m pulls; **BTC** `TechnicalAnalysis.macd_30m` + **`get_full_analysis`**; **Hyperliquid** `"30m"` interval; **backtest** `_build_ta` populates `macd_30m`; **API:** `/api/bitcoin/analysis` macd 1h/30m/15m fields; alt payload `macd_30m_*`, `btc_move_30m`, `sol_move_30m`, `{alt}_move_30m`. **Dashboard:** BTC hero + meta strip MACD 30m; alt meta **MACD 30m \| 15m \| 5m \| Corr \| ATR**; hero **5·15·30m** Δ% triples. `dashboard_ui_rev` bumped.

## 2026-05-11 — Commit + Codex review handoff

Git commit batches the **30m** chain (scanner, strategies, Kelly, tests) and **dashboard/API/analysis** work; **`data/entry_prices/updown_fills.jsonl`** excluded from that commit (operator-local). Second-pass brief for reasons, expected outcomes, checklist: **`docs/HANDOFF_CODEX_30M_REVIEW_2026-05-11.md`**.

## 2026-05-11 — Updown WR on Live (existing gates only)

`/api/journal/updown_breakdown` is fetched when **Live** or **Performance** is open so **existing** Signal Gate WR lines (30m / 15m / 5m) populate; no extra Journal tab cards. `dashboard_ui_rev` bumped.

## 2026-05-11 — Up/down 30m chain + dashboard session P&L

Scanner merge + strategies already carried **30m** Polymarket buckets; this pass completes **dashboard classification** (`*_updown_30m`), **Performance TARGET** rows, **Kelly `_window_stats` 30m**, **SSE `session_total_pnl`** mapped into hero **Session P&L** (prefers journal **`total_pnl`** over realized-only), **`main.py`** lookahead logging + **`window_minutes ≤ 45`** crypto exemption + **`_detect_window_from_question`** 30m deltas, **`live_strategy_scan.py`** 9-tuple unpack, and test updates (`test_hype_integration`, `test_sol_macro`, classify/Kelly).

## 2026-05-10 — Dashboard session fills consistency

Journal: **`get_summary()`** **`total_entries` / `total_exits`** now mirror **`open_positions`** + **`closed_trades`** (phantom-skipped trades no longer deflate session fill totals on the dashboard). **`log_entry`** infers **`entry_leg`** from action/side/outcome when not passed explicitly (fixes **`BUY_NO`** defaulting to YES phantom guard at exit). **`_build_closed_stats`** token-flip filter matches **`log_exit`** (**YES-leg** only).

## 2026-05-09 — Session bundle (May 9)

Work landed on `main` the same calendar day; individual commits below are the audit trail where separate agent sessions did not leave vault notes.

| Commit | Summary |
|--------|---------|
| `1e4d433` | Drift runtime feedback (`performance_feedback`, `_runtime_feedback`), shared backtest expectations loader, strategy min-edge + Kelly hooks, dashboard drift/status + `STRATS` aliases + trade markers (darker/lighter in/out), journal learning module + `run_learning_loop.py` + tests. |
| `30d9252` | Dashboard: Claude design handoff visuals + live tab restructure. |
| `04f95b9` | Up/down backtest parity, sizing, narrators, session artifacts. |
| `add4724` | Dashboard: crypto tiles, decision gates, fullscreen layout. |
| `65a2c09` | Hide AI reasoning in dashboard summaries. |
| `6a24c8f` | Surface decision gates in dashboard. |
| `6afdc9a` | Oracle and composite decision gates. |
| `35116dd` | Use AI decision layer in macro strategies. |
| `0f28b69` | Enforced AI decision layer. |
| `1281189` | Require AI for low-confidence BTC neutral entries. |
| `d2299f7` | Tighten BTC neutral 15m entries. |
| `b417971` | Shadow calibration and exit replay tooling. |
| `c46e4f4` | Buy-no status diagnostics. |

---

## 2026-05-08 — AI / journal surface

| Commit | Summary |
|--------|---------|
| `acdbb3a` | Narrator output → journal + dashboard AI summary panel. |
| `b1c177a` | Auto-run narrators on bot startup against previous session. |
| `0e9d579` | Shadow pipeline expansion, post-trade annotation, narrators, pre-entry veto. |

---

## 2026-05-07 — Kelly, exits, dashboard wiring

| Commit | Summary |
|--------|---------|
| `e0718f5` | Docs: dashboard wiring sweep (stop-loss, exit-reason visibility). |
| `591266f` | Dashboard: stop-loss config, exit-reason summary, bankroll unavailable state. |
| `b0a89f8` | `updown_stop_loss_pct` (time-stop bleed). |
| `1e64a61` / `adc5157` / `7529062` / `1a7023d` | Kelly fraction calibration and cap fixes. |
| `cd66c96` | Config: alt-lane gate intervention. |
| `30061a3` | Lower `min_trade_usd` so Kelly sizing flows through. |

---

## How to extend

1. After a merge or push, append a **dated section** or add rows to the table.  
2. Prefer **one line per commit**; link to PRs if you use them.  
3. If you *did* update the strategy vault, cross-link the vault date/heading here (optional).
