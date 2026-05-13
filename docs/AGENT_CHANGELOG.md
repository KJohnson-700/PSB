# Agent changelog (backfill)

**Purpose:** Record **what shipped** when work was done in **Claude Code, Codex, Cursor**, or similar **without** a matching entry in the Obsidian strategy log or a written operator handoff. **Git remains the source of truth**; this file is a readable index.

**Strategy tuning and hypothesis tracking** still belong in `projects/polymarket-bot/strategy-log/` per `AGENTS.md`. This doc covers **codebase / infra / dashboard** provenance only.

**Canonical repo for this bot:** `https://github.com/KJohnson-700/PSB` (see `AGENTS.md` — do not confuse with other GitHub projects).

---

## 2026-05-12 — Dashboard: ops digest ticker (status-only, under deck hint)

**`index.html`:** **`#ops-digest-ticker`** between deck filter hint and detail panel — builds one line from existing **`/api/status`** / merged deck fields (**regime**, open **positions** count, **side_selection** lanes, top **scan_skip_digest** skips, **session** PnL, kill / **can_trade** reason). **Marquee** when text overflows viewport; **`prefers-reduced-motion`** wraps with no animation. SSE merges **`scan_skip_digest`** when present and refreshes ticker. **`dashboard_ui_rev`:** **`2026-05-12-ops-digest-ticker`**.

## 2026-05-12 — Dashboard: Active Positions master list above deck + detail below

**`index.html`:** Compact **master list** directly under the **Active Positions** heading (above Side/Skips deck); **detail** pane unchanged in **`pos-unified-panel`** below deck + filter hint; row click restores selection → detail. **`dashboard_ui_rev`:** **`2026-05-12-positions-master-above-deck`**.

## 2026-05-12 — Positions: persist CLOB token ids + single-panel layout

**`clob_client.Position`:** optional **`token_id_yes`** / **`token_id_no`** (filled at entry from signals). **`trade_journal.log_entry`:** stores ids on **`open_positions`** for resume. **`main.py`:** bitcoin / SOL–ETH macro / weather execution passes tokens into **`Position`** and **`log_entry`**; journal sync restores them. **`server.py`:** status **`positions`** include **`token_id_yes`**, **`token_id_no`**, and derived **`clob_token_id`** (held leg); disk positions JSON same. Detail pane shows abbreviated **CLOB token** when present. *(Layout: see **Active Positions master list above deck** entry.)*

## 2026-05-12 — Dashboard: metric deck white chrome (deck-only CSS)

**`index.html`:** Ops metric deck chips use **transparent** fills, **white** metric text and **white** border/outline glow; **cyan** kept on **`.chip-k`** (Side / skip labels). **Filter** chips use **white** borders/hover instead of gold. Slightly tighter chip sizing; **`deckPulse`** uses a **white** ring (not cyan). **Tail** tile matches (transparent + soft white edge glow; **`.deck-tail-k`** cyan). **`pos-deck-filter-hint`** muted white (no yellow). **Unchanged:** global **`.g:hover` / `.card:hover`** glow. **`server.py` `dashboard_ui_rev`:** **`2026-05-12-metric-deck-white-chrome`**.

## 2026-05-12 — Discord exit-only: embed URL hardening, main exit payload, kill parity

**`AGENTS.md`:** Discord policy — **`notify_exit`** for closes when enabled; **`notify_trade`** stub; Polymarket link or explicit placeholder when **`market_id`** missing. **`notification_manager.py`:** **`_polymarket_market_url_for_exit`**, exit embed fields (**Market id**, **Trade id**, longer Market text), startup log line clarifies entry/fill disabled in code. **`main.py`:** **`notify_exit`** gains **`entry_price`** / **`trade_id_tail`** from position; global kill Discord loop includes **`xrp_dump_hedge`**. **`config/settings.yaml`:** comments for **`alert_on_trade`** / **`alert_on_exit`**. **`tests/test_notification_manager.py`:** URL helper + embed assertions. *(Vault `projects/polymarket-bot/changelog.md` not present in this workspace copy — infra note here.)*

## 2026-05-12 — Dashboard Live: master–detail positions + ops metric deck rail

**`index.html`:** Command Center **Active Positions** → master list + detail (Polymarket link, optional **end_date** countdown); row **enter** animation; list **exit** flash when a position drops; Side deck chips can **filter** the list. **Metric deck:** tabs Gates / Skips / Side, scroll-snap chips, edge fades, change **pulse**, trailing open + session PnL, **staleness** from **`ts`**, **`prefers-reduced-motion`**. **`server.py`:** **`serialize_position`** + disk positions pass **`end_date`**; **`/api/status`** adds **`ts`**; **`dashboard_ui_rev`:** **`2026-05-12-live-positions-master-detail-deck`**. **`test_dashboard_bundle`:** DOM markers + status **`ts`**.

## 2026-05-12 — Dashboard: alt-first crypto hero order + lag labels

**`index.html`:** SOL/ETH/HYPE/XRP **hero rows** put **alt Δ%** before **BTC Δ%**; lag tile labels **SOL–BTC lag**, **ETH–BTC lag**, **HYPE–BTC lag**, **XRP–BTC lag** (matches strategy: alt 1H primary, lag/BTC secondary). HYPE/XRP blurbs updated. **`server.py` `dashboard_ui_rev`:** **`2026-05-12-dashboard-alt-first-crypto-heroes`**.

## 2026-05-12 — Dashboard: crypto + equal-card rows stacked (single column)

**`index.html`:** **`crypto-grid`** and **`equal-card-grid.two`** use **`grid-template-columns: 1fr`** so BTC/SOL/ETH/HYPE/XRP cards stack vertically at full row width; larger gap and **`crypto-grid>.card`** padding. Fullscreen no longer forces a 2-column crypto row; hero tiles use readable min-heights and **`clamp`** font sizes. **`server.py` `dashboard_ui_rev`:** **`2026-05-12-dashboard-crypto-cards-stacked`**.

## 2026-05-12 — Dashboard: reduce Command Center flicker (ticker + badges + metrics)

**`index.html`:** **`dashRefreshCryptoLiveTickers`** removed from per-panel crypto status updates; **one** deferred pass at end of **`fetchAll`** when **`currentView === 'live'`** (double **`requestAnimationFrame`** after layout). **`updateMetrics`** skips rewriting **strategy boxes / table** when a stable numeric fingerprint is unchanged. **`ops-decision-gates`** skips **`innerHTML`** when digest HTML unchanged. **`server.py` `dashboard_ui_rev`:** **`2026-05-12-dashboard-flicker-ticker-coalesce`**.

## 2026-05-12 — Dashboard: crypto gate tiles + strategy metric grid (ticker + scroll)

**`index.html`:** Removed **`overflow:visible`** on **signal gate** cells (was letting columns grow past the grid, misshaping tiles and breaking **`dashApplySlowTickerIfNeeded`** width math). Gate wrappers are **`display:flex; flex-direction:column; min-width:0; overflow:hidden`**. **`[id$="-status-detail"]`** uses **`max-height:7em; overflow-y:auto`** so long gate copy scrolls vertically. **Strategy Performance** boxes use **`.strategy-metric-grid`** (`repeat(6, minmax(0,1fr))` + responsive 3/2 columns) instead of **`auto-fit` / `minmax(120px,1fr)`** for even tiles. **`dashRefreshCryptoLiveTickers`** runs tickers again after **`requestAnimationFrame`** so measurements happen post-layout. **`server.py` `dashboard_ui_rev`:** **`2026-05-12-crypto-cards-ticker-scroll-layout`**.

## 2026-05-12 — Hyperliquid HYPE `fetch_klines_range`: paginate long windows (fix empty 1m)

**`hyperliquid_hype_service.py`:** **`candleSnapshot`** was called once with the full **`[startTime, endTime]`** span (e.g. months of **1m**). Hyperliquid returns at most on the order of **~5000** candles per response; wide requests often came back **`[]`**, so backtests had **no HYPE 1m** and zero trades / crashes downstream. **`fetch_klines_range`** now **chunks** in **`_HL_MAX_CANDLES_PER_RANGE_REQUEST` (4000)** bars per request, advances from the last returned **`open_time`**, concatenates, dedupes, then applies the existing date filter. **`test_market_data_fallbacks`:** first chunk **`endTime`** assertion updated to match pagination.

## 2026-05-12 — `updown_engine`: skip settle when 1m frame missing `open_time`

**`updown_engine.py`:** Crypto updown replay assumed `data["1m"]` always had an **`open_time`** column. **HYPE** runs with no 1m bars (Hyperliquid range empty / partial cache) passed an empty frame and crashed with **`KeyError: 'open_time'`**. Before slicing for exit replay, **continue** the scan step when **`df_1m` is empty or lacks `open_time`** (same as “cannot settle”). Local **`run_backtest_crypto.py --skip-oracle`** matrix can finish; dashboard still lists saved **`backtest_crypto_*.json`** under **`data/backtest/reports/`**.

## 2026-05-12 — main: strategy refactors, updown/oracle/dashboard, pytest alignment (448 green)

**Bundle:** Removes **`src/strategies/_core/*`**, **`ai_call_log` / `ai_replay_agent`** and their tests; updates **`bitcoin`**, **`eth_macro`**, **`sol_macro`**, **`updown_engine`**, **`ai_agent`**, **`run_backtest_crypto`**, dashboard **markers / STRATS / decision digest**. **`tests`:** **`evaluate_trade_decision`** **`AsyncMock`** + **`AIDecision`** in BTC AI integration; **`test_dashboard_bundle`** matches decision chips (no **HYPE floor** chip), crypto hero CSS selector, **Session fills** on journal vs removed duplicate hero line; **`test_strategies`** weather size **25.0**. **`oracle_loader`:** warn when cache rows exist but requested window has no overlap (see below). **Not committed:** `data/entry_prices/updown_fills.jsonl` (local fill log lines).

## 2026-05-12 — Oracle loader: warn when JSONL exists but date window misses cache

**`oracle_loader.py`:** Local **`ethusdt_polygon_chainlink.jsonl`** (and similar) can span **April 2026** while backtests use **Jan–Feb 2026**, producing an **empty slice** and a misleading **“no oracle history fetched”** after failed RPC backfill. When the cache has rows but **none** in the requested `[start, end]`, log an explicit warning with **row count** and **cached timestamp span** so operators align **`--start` / `--end`** with the file or run from a network where Polygon RPC backfill succeeds.

## 2026-05-12 — BTC chart markers: darker on entry (in), lighter on exit (out)

**`index.html`:** Restored **`_shadeStratHex`** (hex darken / lighten). **`_tradeMarkersFromPoints`** uses **`stratHex`** then **`colorIn = shade(-0.36)`** for entry / **`colorOut = shade(+0.34)`** for exit; open trades keep the darker entry only. Win/loss remains shape + bar position + signed PnL text (no strategy-agnostic red). **`dashboard_ui_rev`:** **`2026-05-12-chart-markers-in-dark-out-light`**.

## 2026-05-12 — BTC chart trade markers: keep strategy color on losses

**`index.html`:** `_tradeMarkersFromPoints` painted **any** losing trade **`#b91c1c`**, so BTC markers looked red instead of **gold** and ETH red instead of **blue**. Markers now always use **`stratHex(strategy)`**; win vs loss stays **circle / square**, **belowBar / aboveBar**, and **signed PnL** in marker text. **`dashboard_ui_rev`:** **`2026-05-12-btc-chart-markers-strategy-hue-only`**.

## 2026-05-12 — Command Center strategy boxes + positions: use canonical `STRATS` / `stratHex`

**`index.html`:** The six **strategy metric boxes** (BTC/SOL/ETH/HYPE/XRP/Weather signal counts) still used **hardcoded colors** (`var(--green)` for bitcoin, `#fb923c` for eth_macro, etc.), so the same strategy looked **different** from journal pills, scan badges, and chart markers. **`sigBox`** now takes `(strat, label)` and sets the big number color with **`stratHex(strat)`**. **Active positions** strategy column uses **`stratPillHTML`** instead of a generic cyan badge. Backtest weather summary card uses **`stratPillHTML('weather')`**. Removed unused **`_hexToRgbTrade` / `_shadeHexTrade`** helpers after marker color simplification. **`dashboard_ui_rev`:** **`2026-05-12-strategy-metric-boxes-strathex`**.

## 2026-05-12 — Command Center: drop duplicate session fills line under Trades today

**`index.html`:** Removed **`hero-trades-sub`** (`fills this session: N`) under the hero trades figure — it repeated the same count as **`daily_trades`** for many operators and read as redundant noise. Session-scoped entry counts remain on **Paper Trade Journal** (**Session fills**). Tooltip now states UTC day vs session scope. **`dashboard_ui_rev`:** **`2026-05-12-command-center-no-duplicate-trades-line`**.

## 2026-05-12 — Scrub unused May 9 `15m_buy_yes` AI bridge

Removed **`hype_macro`** enforced lane **`15m_buy_yes`**, **`updown_composite.hype_15m_buy_yes_min_score`**, and digest/UI wiring (`size_multiplier_15m_buy_yes`, hype floor chip). **`sol_macro`** up/down scoring lane is always **`default`** for AI enforcement checks; **XRP** **`entry_price_max_15m_buy_yes`** (price cap) is unchanged. Tests updated for lane-agnostic composite floor. **`dashboard_ui_rev`:** **`2026-05-12-scrub-15m-buy-yes-bridge-ui`**.

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
