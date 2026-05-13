# Agent changelog (backfill)

**Purpose:** Record **what shipped** when work was done in **Claude Code, Codex, Cursor**, or similar **without** a matching entry in the Obsidian strategy log or a written operator handoff. **Git remains the source of truth**; this file is a readable index.

**Strategy tuning and hypothesis tracking** still belong in `projects/polymarket-bot/strategy-log/` per `AGENTS.md`. This doc covers **codebase / infra / dashboard** provenance only.

**Canonical repo for this bot:** `https://github.com/KJohnson-700/PSB` (see `AGENTS.md` — do not confuse with other GitHub projects).

---

## 2026-05-13 — Crypto backtest/live parity gates + Backtest-tab output tail

**`src/backtest/updown_engine.py`:** Backtest replay now mirrors live entry gating more closely: live-style `KellySizer` sizing before exposure clamps, structured `skip_counts` on `UpdownBacktestResult`, entry-window and timing-window filters during replay, `max_edge_updown` cap support, and ETH no longer hard-requires `btc_data` when `btc_follow_1h_required: false`.

**`tests/test_updown_backtest_parity.py`:** Adds focused regressions for ETH BTC-follow gating, `outside_entry_window` skip accounting, and `edge_above_cap` skip accounting.

**`src/dashboard/index.html`:** Backtest tab now includes a dedicated **Backtest output tail** panel wired to the existing `/api/backtest/status` polling, so subprocess stdout is visible on the tab while jobs run and after completion.

**`tests/test_dashboard_bundle.py`:** Guards the new Backtest-tab output tail DOM and polling hook.

## 2026-05-13 — Dashboard: fix inline JS brace/IIFE closure; CI parses scripts with node --check

**`src/dashboard/index.html`:** Restores missing `});` / IIFE terminator so `startCryptoBacktest` and boot paths parse as valid JavaScript.

**`tests/test_dashboard_bundle.py`:** Adds `test_dashboard_inline_scripts_parse_cleanly` — runs `node --check` on each inline `<script>` body.

## 2026-05-13 — Crypto backtest reliability: progress, cache-only oracle, faster alt replay

**`src/backtest/updown_engine.py`:** Normalizes OHLCV frames once with `_open_time_ns` so per-window history slices use `searchsorted`/`iloc` instead of boolean-copy scans; skips expensive Trend Sabre reconstruction for alt-family replays where BTC/SOL/ETH/XRP/HYPE paths do not consume it; adds replay `on_progress`, `progress_interval`, `max_seconds`, `total_windows`, `run_complete`, and `elapsed_seconds` metadata.

**`scripts/run_backtest_crypto.py`:** Adds heartbeat output (`--progress-interval`, default **1000**) and bounded partial runs via `--max-seconds`; default oracle mode is now **cache-only** so missing/stale Chainlink cache cannot hang a normal backtest. Use `--oracle-fetch` explicitly for slow RPC backfill; `--skip-oracle` still disables oracle replay entirely.

**`src/backtest/oracle_loader.py`:** `load_history(..., allow_fetch=False)` returns cached slices only and logs coverage misses instead of entering RPC fetch.

**`scripts/run_crypto_backtest_bundle.py`:** Forwards `--progress-interval`, `--max-seconds`, and `--oracle-fetch` to child runs.

**Verification:** ETH 15m `2026-01-20..2026-04-20` default cache-oracle run completed **8,736/8,736** windows in **54.2s** with progress; SOL 5m short-window smoke completed **1,728/1,728** windows in **2.8s**.

## 2026-05-13 — eth_macro BUY_NO audit fixes: symmetric macro-leg block + annotation/test cleanup

**`src/strategies/eth_macro.py`:** `block_counter_macro_leg_updown` now matches the ETH config comment for both sides: LONG blocks when `macro_leg < updown_macro_leg_min_for_long`; SHORT / `BUY_NO` blocks when `macro_leg > updown_macro_leg_max_for_short`. SHORT blocks emit `macro_leg_blocks_short` through normal skip and BUY_NO skip telemetry.

**`config/settings.yaml`:** Added `eth_macro.updown_macro_leg_max_for_short: 0.0`; clarified neutral ETH fallback and disabled per-market 1H alignment comments.

**`src/strategies/sol_macro.py`:** `_buy_no_ltf_override` type hint widened from `SOLTechnicalAnalysis` to `Any` because ETH/HYPE/XRP reuse the shared alt-TA shape.

**Tests:** `tests/test_eth_macro.py` covers SHORT-positive macro-leg blocking and LONG-negative behavior; `tests/test_strategy_execution_drivers.py` covers BUY_NO post-entry annotation receiving true YES mid.

## 2026-05-13 — Up/down backtest parity: Polymarket-mark integration coverage

**`tests/test_updown_backtest_parity.py`:** Added integration checks that ``UpdownBacktestEngine._settle_updown_with_live_exit_proxy`` prefers supplied Polymarket YES marks over the synthetic underlying proxy and that sparse minute marks still drive exit decisions via ``Series.asof`` near expiry.

## 2026-05-13 — Polymarket marks tests: fetch + parquet cache behavior

**`tests/test_updown_polymarket_slug.py`:** Covers `_yes_series_from_prices_df` (dedupe, sort, clip, empty/malformed), no API key (no fetch), cache hit (skips fetch), sparse cache → fetch, sparse fetch → no file, successful fetch → parquet write, corrupt cache → fetch, fetch exception → None. Mocks `PolymarketDataLoader` on `updown_polymarket_marks` module.

## 2026-05-13 — eth_macro: ETH-primary gates + optional BTC full analysis

**`config/settings.yaml`:** `eth_macro.neutral_macro_require_spike_or_lag: false`; `eth_macro.btc_1h_regime_gates.enabled: false`.

**`src/strategies/eth_macro.py`:** Require only ETH `get_full_analysis` to start a scan; if BTC full analysis is missing, use neutral BTC HTF placeholder, skip BTC 15m/5m impulse hard-gates (ETH leg + correlation path), and avoid regime sizing/min_edge without `btc_ta`. NEUTRAL+BTC HTF fallback uses explicit `BULLISH`/`BEARISH` only (not `!= NEUTRAL`).

**`tests/test_eth_macro.py`:** `test_eth_scan_eth_only_when_btc_full_analysis_unavailable`.

## 2026-05-13 — HYPE OHLCV: Binance USDM first, Hyperliquid fallback; dash report refresh retries

**`src/backtest/ohlcv_loader.py`:** For HYPE, try **Binance futures** ``HYPEUSDT`` klines (``fapi``) before Hyperliquid; same cache keys under ``data/backtest/ohlcv/HYPE_*.parquet``.

**`src/dashboard/index.html`:** ``loadBacktestReports(retries, gapMs)`` — after crypto backtest job completes (including resume), **5** polls at **700ms** gap plus ``fetchAll()`` when the Backtest tab is active so new ``backtest_*.json`` files show on cards.

**`src/analysis/sol_btc_service.py`:** Comment on HYPE oracle — Arbitrum Chainlink only in this repo; Pyth not integrated.

## 2026-05-13 — Dashboard crypto backtest: ALL bundle + Polymarket marks from settings

**`src/dashboard/server.py`:** ``POST /api/backtest/start`` accepts ``symbol: ALL`` (runs ``scripts/run_crypto_backtest_bundle.py``); optional ``start``, ``end``, ``parallel`` in JSON; when ``backtest.polymarket_marks.enabled`` in ``config/settings.yaml``, appends ``--polymarket-marks`` to single-symbol and bundle commands.

**`scripts/run_crypto_backtest_bundle.py`:** ``--polymarket-marks`` forwarded to each child ``run_backtest_crypto.py``.

**`src/dashboard/index.html`:** Crypto backtest dropdown adds **ALL {w}m (bundle)** rows; client sends ``symbol: ALL``.

**`tests/test_dashboard_bundle.py`:** HTML guard + bundle start smoke test.

## 2026-05-13 — Polymarket YES 1m marks for crypto up/down backtest (option A)

**`src/backtest/updown_polymarket_marks.py`:** Unix slug parity with scanner (``{asset}-updown-{5m|15m|30m}-{UTC_aligned_epoch}``); PolymarketData 1m YES fetch + parquet cache under ``data/backtest/polymarket_marks/``; requires ``POLYMARKETDATA_API_KEY``.

**`src/backtest/updown_engine.py`:** Per-window load; ``_settle_updown_with_live_exit_proxy`` uses ``Series.asof`` on PM YES when present, else underlying proxy.

**`config/settings.yaml`:** ``backtest.polymarket_marks`` (default ``enabled: false``).

**`scripts/run_backtest_crypto.py`:** ``--polymarket-marks`` flips config flag.

**`.gitignore`:** ignore cached ``*.parquet`` for those marks.

**`tests/test_updown_polymarket_slug.py`:** Slug alignment + disabled loader.

## 2026-05-13 — Shared crypto up/down exit config + backtest proxy aligned to live rules

**`src/execution/updown_exit_shared.py`:** Single parser for ``trading.exit_rules`` up/down fields (including ``updown_exit_window_max_fraction`` in overrides), ``CRYPTO_UPDOWN_STRATEGIES``, and pure helpers: scaled exit window, high-entry cents stop, in-profit % stop tighten, adverse time-stop branches (incl. short YES). Docstring states CLOB vs synthetic-mark limitation.

**`src/execution/live_testing.py`:** ``PositionExitManager`` delegates to shared parser/helpers (behavior unchanged for existing tests).

**`src/backtest/updown_engine.py`:** Loads same globals; ``_settle_updown_with_live_exit_proxy`` applies high-entry cents, in-profit % stop, scaled near-expiry window, and ``adverse_for_updown_cents_time_stop`` (long YES / long NO). Module docstring updated.

**`tests/test_updown_exit_shared.py`:** Coverage for parser and helpers.

## 2026-05-13 — Dashboard: Active Positions section starts collapsed

**`index.html`:** **`positionsCollapsed`** default **true**; **`positions-body-wrap`** **`display:none`** and chevron **▶** on load. **`server.py`:** **`dashboard_ui_rev`:** **`2026-05-13-positions-section-default-collapsed`**.

## 2026-05-13 — Polymarket CLOB WebSocket URL + subscribe wire-up

**`src/market/websocket.py`:** Connect to **`wss://ws-subscriptions-clob.polymarket.com/ws/market`** (bare **`/ws`** returns **404**). Initial payload **`{"type":"market","assets_ids":[...]}`**; add/remove uses **`operation` `subscribe` / `unsubscribe`**. Optional override **`trading.clob_ws.wss_url`**. On connect, clear in-memory subscription sets so reconnect resyncs. **`_clob_ws_cfg` / `_asset_ids_key`** helper.

## 2026-05-13 — Dashboard: Active Positions detail default closed

**`index.html`:** **`dashRenderPositionsMasterDetail`** — no longer auto-selects the first open row on load or when positions arrive; detail pane stays on **“Select a row…”** until the operator clicks a row. If the selected id is not in the current filtered list, selection clears instead of jumping to the first row.

**`server.py`:** **`dashboard_ui_rev`:** **`2026-05-13-positions-detail-default-closed`**.

## 2026-05-13 — May 11 exit audit: journal vs YAML (`updown_stop_loss` vs `updown_time_stop`)

**`docs/session_reports/may11_2026_exit_reason_reconciliation.md`:** Reconciles informal audit language with code — **`exit_reason: updown_stop_loss`** maps to **`updown_stop_loss_pct`** (% adverse), **not** **`updown_stop_cents`**. **`updown_time_stop`** maps to late-window cents + **`updown_exit_window_mins`**. Includes **`test_20260511_*`** paper slice (**7** EXIT rows: **4** pct-stop, **3** TP, **0** time-stop in-repo folders).

**`scripts/slice_paper_exits_by_session_prefix.py`:** CLI to aggregate EXIT rows by session dir prefix (exit_reason × window).

**`config/settings.yaml`:** `trading.exit_rules` comments — journal mapping pointer to the doc above.

**`src/analysis/underperformance_audit.py`:** Overall + per-strategy **`recent_buy_yes_stop_loss_loss`** / **`_share_of_negative_pnl`**; hypothesis evidence splits **time_stop $** vs **pct_stop $**; Markdown summary line for % stop share; fix-candidate text names both cohorts.

## 2026-05-13 — Up/down oracle relax, BUY_NO skip counts, entry timing window rename

**`src/analysis/updown_composite_score.py`:** **`validate_oracle_reference`** — **`stale_basis_relax_max_bps`** (pass when feed `updatedAt` is older than **`max_age_sec`** but \|basis\| within relax cap); **`allow_exchange_when_oracle_missing`** (pass when both Chainlink fields are absent but exchange spot exists). **`src/strategies/sol_macro.py`:** wires new flags from strategy config; on oracle fail **`BUY_NO`** now **`_emit_buy_no_skip`** so diagnostics match **`skip`** logs. Renamed **`_within_ai_decision_window`** → **`_within_entry_timing_window`** (config **`entry_timing_window_*`**, legacy **`ai_entry_window_*`** still read). **`src/strategies/bitcoin.py`**, **`src/strategies/eth_macro.py`:** same timing-window rename. **`config/settings.yaml`:** global **`ai_entry_window_` → `entry_timing_window_`**; **`hype_macro`** **`oracle_stale_basis_relax_max_bps: 45`**; XRP **`entry_price_max_15m_yes_side`** (code also reads legacy **`entry_price_max_15m_buy_yes`**). **Tests:** **`tests/test_updown_composite_score.py`**, **`tests/test_sol_macro.py`**.

## 2026-05-13 — Entry windows (SOL/HYPE), HYPE AI 15m max, 30m human compact slugs

**`config/settings.yaml`:** **`strategies.sol_macro.entry_window_15m_max`** **18 → 23**; **`strategies.hype_macro.entry_window_15m_max`** **16 → 27**; **`strategies.hype_macro.entry_timing_window_15m_max`** **15 → 25** (marginal up/down tie-break timing vs widened quant entry band). **`polymarket.fetch_updown_30m_human_compact_slugs`** (default **true**) — second slug family for 30m Gamma discovery.

**`scanner.py`:** **`_compact_updown_range_time_et`**, **`_iter_updown_30m_human_compact_slugs`** — ET half-hour slugs like **`bitcoin-up-or-down-may-13-530am-600am-et`** merged with **`{asset}-updown-30m-{unix}`** in **`fetch_updown_30m_markets`** (deduped); post-fetch duration band **20–52** minutes. **`fetch_updown_markets`** returns **`(15m_bucket, ~30m_carry)`** — rows from the **15m slug** responses with **`window_minutes` 26–34** (title-inferred half-hour) are no longer dropped; **`_sync_network_phase`** merges carry into **`updown_30m`** when **`polymarket.updown_30m_merge_from_15m_slug_batch`** is true (default). **`_dedupe_markets_by_id`**. Slugs with **`updown-15m`** and **`window_minutes` None** → **15m bucket**. **Tests:** **`tests/test_scanner_crypto_enhancements.py`**.

**RCA note:** Gamma **`btc-updown-30m-{unix}`** often **`[]`** while **`btc-updown-15m-{unix}`** resolves; compact human slugs match the **`hyperliquid-up-or-down-…-415am-430am-et`** family. **`updown_30m_count`** may stay **0** until Polymarket lists matching events. HYPE skip drivers vary by pulse (**`oracle_stale`** / **`liquidity`** vs **`outside_entry_window`**); rank from multi-cycle **`scan_skip_digest`**, not one ops snapshot. **Bulk Gamma caveat:** see **`docs/GAMMA_UPDOWN_30M_DISCOVERY.md`** (matching both substrings **`updown`** and **`30`** on a slug hits **`5m`** epoch false positives, not **`30m`** markets).

## 2026-05-12 — Dashboard: CLOB order book (WebSocket cache + REST fallback)

**`index.html`:** **`#positions-orderbook-wrap`** in Active Positions **detail** — polls **`GET /api/orderbook`** every **2s** for the held outcome token; shows **websocket** vs **rest** source and top bid/ask rows.

**`server.py`:** **`GET /api/orderbook?token_id=`** — prefers non-empty **WS** snapshot; else **public REST** via **`clob_client.fetch_order_book_snapshot`** (level-0 `get_order_book`). **`dashboard_ui_rev`:** **`2026-05-12-orderbook-ws-api`**.

**`websocket.py`:** Subscribe uses **`trading.clob_ws.asset_ids_json_key`** (default **`assets_ids`**); **`_merge_orders`** bid/ask sort fix; **`snapshot_order_book_json`**.

**`main.py`:** Background **`ws_client.listen()`** + subscription sync for **open-position** YES/NO tokens (`trading.clob_ws`, channel default **`market`**).

**`config/settings.yaml`:** **`trading.clob_ws`**.

## 2026-05-12 — HYPE OHLCV: HL UTC timestamps, 1m bisect pagination, 5m→1m fallback

**`hyperliquid_hype_service.py`:** **`utc=True`** on parsed candle times — fixes range merge/filter **`datetime64` vs `Timestamp`** failures that zeroed 5m/15m fetches. **Smaller 1m chunks**, **`_hl_bisect_fetch_chunk`** on empty snapshots. **`ohlcv_loader.py`:** if HL **1m** missing but **5m** exists — **resample forward-fill 5m→1m**; tz-safe localize. **`run_backtest_crypto.py`:** exit if **HYPE `1m` &lt; 50 bars** with HL retention hint.

## 2026-05-12 — Planned follow-up (superseded): Live dashboard CLOB order book / depth

**Update:** Initial **plan-only** note — **shipped** under **Dashboard: CLOB order book (WebSocket cache + REST fallback)** above.

## 2026-05-12 — Backtest: exit code in `/api/backtest/status`, UI failure line; HYPE min_edge YAML parity

**`server.py`:** **`exit_code`** on finished dashboard-spawned backtest jobs (scoped **`job_id`** + per-job list). **`index.html`:** **`pollBacktestJobUntilDone`** shows red failure when **`exit_code !== 0`** (e.g. HYPE OHLCV empty). **`config/settings.yaml`:** **`backtest.min_edge_hype_5m`** **0.07** to match **`strategies.hype_macro.min_edge_5m`** (was 0.09). **`dashboard_ui_rev`:** **`2026-05-12-bt-exit-hype-minedge`**.

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
