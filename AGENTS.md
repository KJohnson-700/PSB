## Learned User Preferences

- Prefer very short default chat; use full sentences and clear structure for deliverables (reports, docs, long answers).
- Before deep work on complex or explicitly high-priority tasks with thin scope, ask a few short clarifying questions (goal, constraints, priority).
- Do not guess on technical claims; research and verify before recommending changes.
- When comparing strategies or external approaches, give data-backed assessment, not reasoning only from the local codebase.
- For long backtests or heavy jobs, show progress and concrete outputs; avoid silent stalls or loops with no results.
- Prefer running commands and fixing issues in the environment over only suggesting steps the user must run. If a tool fails (e.g. Railway CLI `Unauthorized`), **retry** and confirm basics (cwd, linked project) on the agent side first; **do not** imply the user misconfigured or “forgot to connect” without evidence—report the observed error and treat transient token/session expiry as a normal cause. When the user asks for a **“Railway update”** (or similar: status of the hosted bot / paper session), prioritize **paper-trading / ops signal**—e.g. latest `OPS_JSON` or journal-related lines via `railway logs` from the linked repo—not deploy metadata alone unless they asked about deploys. For paper-trading restarts, prefer dashboard-native flows and avoid suggesting UX that requires pasting API keys into the dashboard.
- For mispricing-style entry (arbitrage, fade, and any consensus path that sizes trades), follow `docs/STRATEGY_ENTRY_SPEC.md`: enter when edge is above the minimum threshold, keep downside flexible with no hard floor, use an optional max-entry cap only if configured, avoid chasing noise, allow strong mispricings at low prices, and size with Kelly plus existing risk limits.
- Discord (and the bot’s notification module): **only** execution outcomes — `notify_trade` / `notify_exit` for `bitcoin`, `sol_macro`, `eth_macro`, `hype_macro`, `xrp_macro`, and `xrp_dump_hedge` — plus optional errors/status. No consensus, contrarian, or other opportunity-only webhooks from this codebase. If consensus- or contrarian-style opportunity alerts still reach Discord, treat that as unintended and trace the sender path (config, webhook, or code).
- For calendar-day questions (today, yesterday, daily PnL or trade totals) when discussing the hosted bot or Railway logs, assume **America/Los_Angeles** unless the user specifies another timezone.
- Before enabling execution on a path that was idle or gated, give a brief explicit reason (config, risk gate, or code path) so the decision is understandable without relying on chat memory.
- Strategy and backtest auditing (distinct from routine pytest/CI green checks): prioritize finding bugs, miscalculations, inconsistencies, and spec drift in data, strategy code, and reports, and give actionable improvement suggestions; follow `docs/polymarket-backtest-subagent-skill.md`.
- After material strategy changes (not minor bugfixes), deliver concise review or backtest summaries and prefer in-repo or dashboard-visible artifacts over copy-paste-only handoffs. When scoping a **new** short-window or new-asset strategy (e.g. HYPE on Polymarket), prioritize external research on documented approaches, fees, and resolution mechanics—not only porting an existing lag template.

## Strategy Log — AI Editor Instructions

The Obsidian vault at `projects/polymarket-bot/strategy-log/` is the authoritative record for all strategy changes, test results, and reviews. When making strategy-related changes:

1. **Read `_index.md` first** — it defines the exact template format. Do not invent your own structure.
2. **Append only** — never delete or rewrite existing entries. Always add new entries at the TOP of the relevant section.
3. **One file per strategy:** `bitcoin.md`, `sol_macro.md`, `eth_macro.md`, `fade.md`, `neh.md`
4. **Every config/code change** gets a Change Log entry with: what changed, why, hypothesis, expected outcome, actual outcome, and status.
5. **Fill "Actual outcome" with real data only** — minimum 15 closed trades since the change. Leave `pending` until then.
6. **Review sessions** are triggered by: 15–20 new closed trades, a strategy change, or a regime shift.
7. **Lessons Learned** are evergreen truths confirmed by data — add at top of that section, dated.
8. **Status values:** `pending` → `confirmed ✅` | `reverted ❌` | `inconclusive ⚠️`
9. **Quick Stats table** — update from `/api/journal/summary` live data or backtest JSON. Do not estimate.
10. The vault also has `projects/polymarket-bot/changelog.md` for infrastructure/milestone changes (not strategy tuning — that goes in the strategy log).

## Strategy test review (AI)

For **auditing** backtest outputs, paper/live journals, and strategy code—hunting bugs, miscalculations, inconsistencies, and spec drift—use **`docs/polymarket-backtest-subagent-skill.md`**. That role is **not** the same as routine pytest/CI test running (green builds); it produces structured review findings and improvement suggestions.

## Learned Workspace Facts

- **PSB (this repo) + Second Brain:** Project name **PSB**; Mac folder is often `psb-main 1` (Windows name may differ). **Hermes Obsidian vault note (operator second brain):** `Hermes Second Brain/projects/psb/notes/2026-04-21-psb-agent-memory-correctness-bundle.md`. **REST API (when plugin is up):** `docs/OBSIDIAN_LOCAL_REST_API.md`. If API is offline, **writing that vault path directly** is equivalent—Obsidian will see the file on disk.
- **PSB April 2026 fixes (Cursor / Claude):** **`scan_and_analyze` on `SolMacroStrategy`** (not nested in `_get_weekend_penalty`); **`_get_weekend_penalty`** module-level; **`self.enabled`** on base SOL macro; **`_bump_skip`** in SOL macro loop; **Discord** allowlist **`hype_macro`**; restart process for `src/` changes. **Infra log:** `projects/polymarket-bot/changelog.md` § 2026-04-21. **Hermes:** don’t treat Hermes app repos as PSB; patch PSB codebase only.
- Intended ops: intermittent bot runs (multi-day sessions, partial monthly uptime) while strategies are tuned; treat true 24/7 production-style hosting as gated until at least two strategies are proven and higher infra spend is justified.
- Railway-oriented deploy uses a root Dockerfile and `railway.json`; in production, the dashboard follows `PORT` and binds `0.0.0.0` when `PORT` is set (see `docs/RAILWAY.md`). `railway redeploy` can replay the latest image without a new Git build or local upload; use `railway up -s <service> --ci` from the linked repo when the container must match the current working tree, and run `railway service link` from the repo root if CLI commands show no service. If saving variables triggers a redeploy modal with “no GitHub installation found,” use Deployments-tab redeploy, a new commit deploy, or `railway up` instead of relying on that modal. Hosted **live** state from Cursor agents expects **authenticated `railway` CLI + project link** or pasted `/api/ops/summary` / `/api/journal/summary`—there is **no Railway MCP** in the default Cursor MCP bundle here.
- **Deploy freshness (dashboard / bot):** `GET /health` → `dashboard_ui_rev` should match the string in `src/dashboard/server.py`. If it shows an **older** tag, the public URL is a **stale image** (fix GitHub/Railway deploy, `NO_CACHE` if needed, active deployment)—not a browser-cache issue. **`OPS_JSON` `last_signal_counts`** missing keys that exist in current `main.py` (e.g. `hype_macro`) is the same staleness signal, not proof that env vars are wrong.
- **Railway GitHub settings:** set **Root Directory** only when the Dockerfile and app live in a **subfolder** (e.g. a deliberate bundle path); for a normal repo with a root `Dockerfile`, leave it at **repo root**. **Wait for CI** gates deploys on successful GitHub Actions for that commit—if deploys never land or stay `SKIPPED`/`WAITING`, check Actions status and this toggle before assuming Docker or env breakage.
- **Container env log:** `config/secrets.env` is not copied into the image (`.dockerignore`); startup messages about **no local secrets file** mean “use injected process env,” not “Railway variables were deleted.”
- **`DASHBOARD_API_KEY` is optional:** if unset, mutating dashboard routes do not require `X-API-Key` (server `_check_auth` is a no-op). A **403** on those routes means the env var **is** set and the request lacked a matching header—not a sign that operators must add this variable for a typical public Railway URL.
- Hosted runs should make recent activity, blocks, and test or paper outcomes easy to verify from logs plus dashboard or journal endpoints; opaque long runs without a clear status surface are treated as an operator-visibility gap to fix. Built-in **daily** counters (`daily_pnl`, `daily_trades`, journal “today”) use **UTC** calendar days from Railway server time (`datetime.now().date()`), not the operator’s local date—local “yesterday” (e.g. Pacific) needs an explicit UTC window (such as `07:00Z`–`07:00Z` for PDT) or timezone-aware slicing, not the bot’s daily rollup alone. The Command Center hero can drift from `/api/status` if `/api/events` (SSE) overwrites “Trades today” or open positions using the wrong sources (`journal.get_summary()` has no `total_trades`; `PolyBot` has no `.positions`); SSE should use `risk_manager.daily_trades` and `len(risk_manager.active_positions)` in `src/dashboard/server.py`.
- LLM readiness for operators is surfaced via `src/ai_status.py`, dashboard `/api/status` AI fields, dashboard badge, and startup logging; `ai.live_inferencing` in `config/settings.yaml` pauses live provider calls when false without removing key setup (status distinguishes paused vs fully off).
- Obsidian Local REST API (coddingtonbear plugin): typically HTTPS on `127.0.0.1:27124` with `Authorization: Bearer <key>`; callers must handle self-signed TLS on localhost; confirm port in Obsidian plugin settings. When the API is unreachable, use markdown written to a manual-import drop (e.g. pending-transfer notes) instead of dropping the content.
- Paper journal continuity vs deploy checks: `PolyBot` uses `TradeJournal(resume_latest=True)` and resumes the newest `data/paper_trades/<session>` that already has journal data (so restarts keep trades and snapshots in view); without a Railway volume on `/app/data`, redeploys still wipe disk. The Performance tab line “Old code — restart bot to activate” is driven by log-file markers scanned under `data/logs/`, not by Git commit freshness—confirm the running image with the deployment commit or `/health` `git_sha` when set.
- Root `CURSOR_HANDOFF.md` is the dated checklist for ETH 5m/15m up/down and `eth_macro` parity with SOL macro; use it when changing that feature set.
- `eth_macro` targets short-horizon ETH markets discovered via `eth-updown-15m` and `eth-updown-5m` slug prefixes; crypto up/down backtests can run `ETH` through `scripts/run_backtest_crypto.py` and the shared updown OHLCV path. Optional `hype_macro` (BTC vs Hyperliquid HYPE, `src/strategies/hype_macro.py` + `src/analysis/hyperliquid_hype_service.py`) is wired in `main.py` but **disabled** by default; the scanner includes `hype-updown-*` and `hyperliquid-up-or-down-*` slug families alongside BTC/SOL/ETH/XRP up/down.
- Optional `xrp_dump_hedge` targets 15m XRP Up/Down via `xrp-updown-15m-*` slugs (quant-only); stress-style simulation without Polymarket history uses `scripts/run_backtest_xrp_dump_hedge.py`; keep disabled in config until live validation.
- Keep live execution and backtests aligned with `docs/STRATEGY_ENTRY_SPEC.md` and the implementation map in `docs/HANDOFF_STRATEGY_ENTRY_AND_BACKTEST.md`.
- Crypto strategy backtests stay local (no separate Railway service for that workload); support triggering local backtests manually from the dashboard without stopping the hosted Railway bot. Binance OHLCV fetches can return HTTP 451 in geo-restricted regions; keep `--start`/`--end` inside ranges fully covered by cached `data/backtest/ohlcv` parquet—if the loader treats `--end` as end-of-day UTC and the cache stops mid-day on the last date, a fetch may still run and fail. For **live** short-window Polymarket up/down, exchange-based features can diverge from each market’s **stated resolution oracle**—treat that basis explicitly (align feeds or measure the gap), especially when modeling XRP/HYPE 5m/15m payoffs.
- When changing per-trade sizing or other deploy-critical trading settings, align config and execution code and confirm Docker/Railway deployments pick up the same values (e.g. ~$10–$15 per trade rather than $1–$3 when that is the intended target).

## Backtest Workflow (post-strategy-edit)

After any **material strategy change** (signal logic, thresholds, sizing, new asset), run backtests locally and record results to vault before deploying.

### Which backtests to run (locally, not on Railway)
```powershell
# Core crypto up/down backtests — run after any signal/sizing change
python scripts/run_backtest_crypto.py --symbol BTC  --window 15 --start 2026-01-20 --end 2026-04-20
python scripts/run_backtest_crypto.py --symbol ETH  --window 15 --start 2026-01-20 --end 2026-04-20
python scripts/run_backtest_crypto.py --symbol ETH  --window  5 --start 2026-01-20 --end 2026-04-20
python scripts/run_backtest_crypto.py --symbol XRP  --window 15 --start 2026-01-20 --end 2026-04-20
python scripts/run_backtest_crypto.py --symbol XRP  --window  5 --start 2026-01-20 --end 2026-04-20
python scripts/run_backtest_crypto.py --symbol HYPE --window 15 --start 2026-01-20 --end 2026-04-20

# SOL backtest (correlation lag, not pure up/down)
python scripts/run_backtest_crypto.py --symbol SOL  --window 15 --start 2026-01-20 --end 2026-04-20

# XRP dump-hedge simulation (separate script, synthetic book)
python scripts/run_backtest_xrp_dump_hedge.py
```

### Backtest parameters (from run_backtest_crypto.py --help)
- `--symbol`: BTC | SOL | ETH | HYPE | XRP
- `--window`: 5 | 15 (minutes)
- `--start` / `--end`: YYYY-MM-DD (default: last 90 days)
- `--no-cache`: force fresh Binance download
- `--no-save-report`: skip JSON report save

### After running — record to vault
1. Note key metrics: win rate, net PnL, return %, trades, expectancy
2. Compare vs previous backtest results (same symbol/window)
3. If materially different (WR >2% change, PnL sign flip): investigate before deploying
4. Update strategy log with Change Log entry: "Backtest vs prior run: WR=X% (prev Y%), +$Z (prev $W)"
