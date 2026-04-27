# Polymarket bot — infrastructure & milestones

Strategy tuning and per-strategy results live in `strategy-log/*.md`, not here.

---

## 2026-04-26 — Exposure tier caps updated to 15 / 10 / 5

- **Config:** `config/settings.yaml` under `exposure` now sets `full_size: 15.0`, `moderate_size: 10.0`, `minimal_size: 5.0`.
- **Why:** Operator sizing target tightened so non-FULL conditions do not keep near-$15 tickets.

## 2026-04-26 — Exposure sizing floor now respects tier multiplier

- **Issue:** `ExposureManager.scale_size()` applied `exposure.min_trade_usd` as a flat post-multiplier floor. With `min_trade_usd: 10` and `MINIMAL` tier `x0.2`, trades were still floored near $10 instead of the expected ~$2.
- **Code:** `src/execution/exposure_manager.py` now applies a tier-aware floor: `min_trade_usd * tier_multiplier` (FULL=10, MODERATE=6, MINIMAL=2 with current config). Existing tier caps (`full_size/moderate_size/minimal_size`) still apply.
- **Tests:** Added `tests/test_exposure_manager_sizing.py` (3 regression cases covering FULL/MODERATE/MINIMAL behavior).

## 2026-04-26 — Journal tab: use same “resumable session” as the bot (disk-only dashboard)

- **Issue:** After a restart, a **newer empty** `data/paper_trades/<session_id>/` directory could be lexicographically first while the bot correctly resumed a **older folder with trades** (same rule as `TradeJournal(resume_latest=True)`). The dashboard process often has no in-memory `bot_instance`, so it read the empty stub and the Journal tab showed no metrics for the last test run.
- **Code:** `TradeJournal.newest_resumable_session_dir()` in `src/execution/trade_journal.py` (shared with resume + `_get_journal` / `summary` fallback in `src/dashboard/server.py`).
- **Tests:** `tests/test_trade_journal_resumable.py::test_newest_resumable_session_dir_skips_empty_stubs`
- **Strategy log (same release window):** `strategy-log/bitcoin.md` and `eth_macro.md` (2026-04-26 entries); SELL/exit + RSI changes are also summarized in the changelog block below and those strategy files.

## 2026-04-26 — SELL_YES take-profit exits buy back YES token

- **Issue:** `PositionExitManager` calculated `SELL_YES` PnL against the YES price but attempted to close profitable short-YES positions by buying the **NO** token. Entry execution sells the YES token for `SELL_YES`, so the exit leg must buy back YES.
- **Code:** `src/execution/live_testing.py` — `pos.outcome == "NO"` exit decisions now use `exit_action="BUY"` with `token_yes`, keeping token, price, and journal PnL conventions aligned.
- **Tests:** Added `tests/test_live_testing.py::test_sell_yes_take_profit_buys_back_yes_token`.

## 2026-04-22 — Single trading loop (`_unified_cycle`) — **verified working**

- **Before:** Two asyncio tasks — `_main_loop` (300s, exits + arb/fade/neh + resolution) and `_crypto_fast_loop` (120s, crypto only + resolution), each running a full `scan_for_opportunities()` — double scanner load and confusing logs. Operator expectation (“main loop” vs “fast loop”) was easy to misread; crypto-only live scope had arb/fade/neh off, so all entries came only from the fast path while exits trailed on 300s.
- **After:** One `_unified_trading_loop` calling `_unified_cycle`: **single** scan per tick, **TP/SL** `check_exits` on the **same** cadence as entries, optional arb/fade/neh if enabled, then **bitcoin** / **sol_macro** / **eth_macro** / **hype_macro** / **xrp_macro**, one `_run_resolution_check(label="[TRADING]")`. Cadence: **`trading.cycle_interval_sec`** (default **120**) in [`config/settings.yaml`](../../config/settings.yaml); `PolyBot.scan_interval` matches for `OPS_JSON` `scan_interval_sec`. Log prefix **`[TRADING]`** (replaces `[FAST]`) for scanner lookahead and crypto leg lines; BTC exception logging uses `exc_info=True`.
- **Code:** [`src/main.py`](../../src/main.py) — `start()` uses `asyncio.gather(self._unified_trading_loop(), self._daily_coach_loop())` only. Removed: `_main_loop`, `_crypto_fast_loop`, `_trading_cycle`, `_crypto_cycle`.
- **Tests:** `pytest` — 171 passed after refactor (local `uv run pytest tests/`).
- **Operator sign-off (same day):** Bot reported **working again** end-to-end after deploy/restart; tail logs for `Starting trading cycle...`, `[TRADING] Scanner lookahead`, `[TRADING] Crypto …`, `Cycle complete`, `OPS_JSON`.

## 2026-04-22 — Scanner: non-blocking network phase (Railway + local)

- **Issue:** Synchronous `requests` inside `async def scan_for_opportunities` blocked the asyncio event loop for minutes (especially **HYPE alt** slug fetches), stalling **both** the main loop and the fast crypto loop — looked like a full freeze.
- **Code:** [`src/market/scanner.py`](../../src/market/scanner.py) — bundle Gamma + updown (+ optional HYPE alt) HTTP in `asyncio.to_thread`, wrapped with `asyncio.wait_for` using `polymarket.scanner_sync_timeout_sec`. Heartbeat logs for sync phase and total scan time. HYPE alt fetch defaults to `strategies.hype_macro.enabled`; optional `polymarket.fetch_hype_alt_markets` override.
- **Config:** [`config/settings.yaml`](../../config/settings.yaml) — `scanner_sync_timeout_sec: 120` under `polymarket`.
- **Railway:** Same module as local — **redeploy from Git** so the new image is built. Confirm deploy logs show `Scanner: sync network phase (thread) starting`. See [`docs/RAILWAY.md`](../../docs/RAILWAY.md) § *Scanner / “frozen bot”*.
- **Pre-HYPE baseline:** Older operator evidence may live in the Hermes vault (`projects/psb/notes/`), not only in repo `data/logs/`; REST push uses `OBSIDIAN_REST_API_*` locally per [`docs/OBSIDIAN_LOCAL_REST_API.md`](../../docs/OBSIDIAN_LOCAL_REST_API.md).
- **Inventory / RCA:** [`docs/STRATEGY_AI_EXECUTION_INVENTORY.md`](../../docs/STRATEGY_AI_EXECUTION_INVENTORY.md).

## 2026-04-22 — Git init, paper-session runbook, `.railwayignore` 413 fix, deploy verified

- **Local repo:** `git init` in project root (this folder previously had no `.git`); first commit includes tree + paper-session documentation. **`.gitignore`:** add `.claude/`, `.DS_Store` (match other agent/IDE noise).
- **Operator docs:** [docs/RAILWAY.md](../../docs/RAILWAY.md) — new section *Paper sessions and test data* (`PAPER_SESSION_ID`, `PAPER_RESUME_SESSION`, `test_*` resume pitfall, Mac paths with spaces, heatmap/entries linkage). [docs/DASHBOARD_DATA_SOURCES.md](../../docs/DASHBOARD_DATA_SOURCES.md) — *Session ID and `entries.jsonl`*. [README.md](../../README.md) — short pointer to those sections.
- **`railway up` 413 Payload Too Large:** root cause was uploading **~250MB** (local **`.venv/`** and other data not meant for the image). **`.railwayignore`** expanded: `.venv/`, `data/paper_trades/`, `data/logs/`, broad `data/backtest/reports/`, and other large/runtime paths. Docker still installs from **`requirements-railway.txt`** inside the build.
- **Deploy:** `railway up --ci -s polymarket-bot` from linked project → **Deploy complete** (build id in Railway UI). **Verification (hosted):** `GET https://polymarket-bot-production-bf4f.up.railway.app/health` → `dashboard_ui_rev` **`2026-04-21-sse-scalar-sentry-htmx`** (matches `src/dashboard/server.py`); `railway_deployment_id` present. `git_sha` in `/health` is **null** for CLI-upload builds unless `RAILWAY_GIT_COMMIT_SHA` is injected (GitHub Actions / Dockerfile `ARG` path sets it for commit-attributed images).
- **Tests before deploy (local):** `pytest` `test_bitcoin`, `test_sol_macro`, `test_strategies`, `test_dashboard_bundle` — 104 passed; `py_compile` on `src/main.py`, `src/strategies/sol_macro.py`, `clob_client.py`.
- **Follow-up deploy (same day):** `CLOBClient.can_sell_token` read `trading.dry_run` from the **polymarket** sub-dict by mistake; `trading` lives at **root** in `config/settings.yaml`, so the orderbook pre-check never ran when `polymarket.dry_run` was absent. **Fix:** `self._root_config` + `self._root_config.get("trading", {}).get("dry_run", True)` in `src/execution/clob_client.py` — then **`railway up --ci`** again.

## 2026-04-22 — Dashboard `/health`, CI guards, and Railway CLI deploy path

### Dashboard (why the UI looked “dead” while API returned 200)

- **Root cause:** In `src/dashboard/index.html`, `fetchAll()` used `Promise.all` with **18** `fetch()` calls but destructuring listed **17** variables (**`hypeR` missing** between `ethR` and `xrpR`). That throws **`ReferenceError`** in the browser and breaks the whole status poll.
- **Fix:** Add **`hypeR`** to the destructuring list so it matches the `fetch()` count.
- **Deploy fingerprint:** `GET /health` includes **`dashboard_ui_rev`** (bump in `src/dashboard/server.py` whenever you ship dashboard HTML/JS). **Must be a single dict key** — duplicate `"dashboard_ui_rev"` entries are invalid (second wins silently). Current tag: **`2026-04-21-kelly-live-recover-gitlab-deploy`**.
- **Verification:** `curl https://<your-host>/health` — confirm `dashboard_ui_rev` matches `server.py` and `railway_deployment_id` updates on new deploys.

### Guards so this class of bug doesn’t ship again

- **`scripts/preflight.py`:** `check_dashboard_index()` — parses `fetchAll()`’s first `Promise.all` and asserts destructure count == `fetch()` count.
- **`tests/test_dashboard_bundle.py`:** Same invariant + **`TestClient`** smoke for **`GET /`** and **`GET /health`** (expects `dashboard_ui_rev`).
- **Security suite:** `scripts/run_security_suite.py` — default **`pip-audit`** on the **current venv** (avoids `ensurepip`/temp-venv failures on some macOS Python builds); **`--audit-requirements`** for `requirements*.txt` when CI has working venvs. **Bandit** skips **B104** / **B602** by default (PaaS bind + local port helpers); **`--strict-bandit`** for full rules. Deps in **`requirements-dev.txt`** (`bandit`, `pip-audit`, `pytest`, …).

### GitLab CI (primary remote: `gitlab.com/ken-johnson/psb`)

- **Stages:** `test` → `deploy`.
- **`checks`:** install `requirements-dev.txt`, then `preflight` → `pytest tests/test_dashboard_bundle.py` → `run_security_suite.py`. Runs on **merge requests** and **default branch** pushes.
- **`railway_deploy`:** `needs: ["checks"]`, then `railway up --ci --service polymarket-bot` with **`RAILWAY_TOKEN`** in GitLab CI/CD variables.
- **CI env:** `OPENAI_API_KEY=ci-placeholder-not-used` so preflight passes without real AI keys (preflight only requires a non-empty provider key).

### GitHub (optional mirror, e.g. `KJohnson-700/PSB`)

- **`.github/workflows/ci.yml`:** Same checks as GitLab on push/PR to `main`.
- **`.github/workflows/deploy-railway.yml`:** `railway up --ci` on push to `main` with **`RAILWAY_TOKEN`** in repo Actions secrets (if you use GitHub instead of/in addition to GitLab deploy).

### Railway — what actually fixed “can’t redeploy from this laptop”

1. **Symptom:** `railway up` → **`No linked project found. Run railway link`** (CLI could be logged in and still fail).
2. **Fix (once per clone/machine):** From **repo root**:
   - `railway link -p "PolyMarket Strategy Bot" -s polymarket-bot`  
     (workspace **SamuraiFrenchie’s Projects**, environment **production**, service **polymarket-bot** — adjust flags if your project/service names differ.)
3. **Deploy:** `railway up --ci --service polymarket-bot -m "<message>"`  
   Builds from **local tree** (root **`Dockerfile`**); does not require **`RAILWAY_TOKEN`** in `.env` if you use **`railway login`**.
4. **2026-04-22 session:** Full image build completed on Railway (**Deploy complete**); use build/deploy logs in the Railway UI for that deployment id if anything regresses.

---

## 2026-04-21 — Agent memory: what this repo is + April 2026 correctness bundle

### What this project is

- **Working name:** **PSB** (this Mac repo folder: `psb-main 1`; Windows checkout name may differ). Polymarket short-horizon crypto bot; trades **Polymarket** (CLOB), focused on **BTC/SOL/ETH/HYPE/XRP** up/down and related strategies.
- **Second brain (Hermes):** operator vault note — `Hermes Second Brain/projects/psb/notes/2026-04-21-psb-agent-memory-correctness-bundle.md`. REST API usage: **`docs/OBSIDIAN_LOCAL_REST_API.md`** in this repo.
- **Entry point:** `python src/main.py --paper` (paper) or `--live --confirm-live` (live). Loads **`src/env_bootstrap.load_project_dotenv`**: project root **`.env`** then **`config/secrets.env`** (secrets override).
- **Core runtime:** `src/main.py` (`PolyBot`) — unified trading cycle (`_unified_cycle`) for bitcoin + `sol_macro` + optional `eth_macro` / `hype_macro` / `xrp_macro`; journal under **`data/paper_trades/<session>/`** (`entries.jsonl`, `positions.json`, `summary.json`); optional **dashboard** (`src/dashboard/server.py`) when enabled.
- **AI:** `config/settings.yaml` → `ai.provider_chain` + env keys; `src/ai_status.py` reports readiness. **`ai.live_inferencing: false`** suppresses live LLM calls without removing keys.
- **Discord:** `src/notifications/notification_manager.py` — trade/exit webhooks for **`bitcoin`**, **`sol_macro`**, **`eth_macro`**, **`hype_macro`**, **`xrp_macro`**, **`xrp_dump_hedge`** only; webhook from YAML **`notifications.discord_webhook`** or env **`DISCORD_WEBHOOK_URL`** (merged in `PolyBot._load_config`).
- **Hermes / external bundles:** treat **Hermes-owned trees as read-only**; apply fixes only in **this** PSB repo when both exist in a workspace.

### Issues fixed (April 2026) — agents should know these

1. **`AttributeError: … has no attribute 'scan_and_analyze'`** on `ETHMacroStrategy` / `HYPEMacroStrategy` (and `SolMacroStrategy`): **`scan_and_analyze` had been nested inside `_get_weekend_penalty()` in `src/strategies/sol_macro.py`** (unreachable). **Fix:** method belongs on **`SolMacroStrategy`**; subclasses inherit it. **`_get_weekend_penalty()`** restored as a **module-level** function after the class (still used by `conditions_from_ta`).
2. **`SolMacroStrategy` missing `self.enabled`:** **`scan_and_analyze`** gates on `enabled`; only ETH/HYPE set it after `super()`. **Fix:** `self.enabled = self.config.get("enabled", True)` in **`SolMacroStrategy.__init__`** (ETH/HYPE keep their own defaults).
3. **`_bump_skip` NameError on SOL up/down path:** calls copied from **`bitcoin.py`** without the local **`def _bump_skip` / `skip_reasons` dict**. **Fix:** define them before the `for market in sol_markets` loop in **`scan_and_analyze`**.
4. **F-string safety:** `{(self.min_edge_5m if is_5m else self.min_edge):.4f}` in AI context (avoids ambiguous `else self.min_edge:.4f` parsing in some tools).
5. **Discord allowlist:** **`hype_macro`** added to **`DISCORD_TRADE_STRATEGIES`** / **`STRATEGY_ALERT_TITLE`** so HYPE fills/exits can notify like other crypto legs.

### Verification notes

- **`python -m py_compile src/strategies/sol_macro.py`**; **`pytest tests/test_sol_macro.py`** (and ETH/HYPE-related tests as run in session).
- **Local bot:** Python process must **restart** to load changed `src/`; no hot reload.
- **Railway:** new code only after **deploy** of the commit containing fixes; **`_crypto_cycle`** catches ETH/HYPE errors per-strategy so **`AttributeError` logged but may not crash the whole process** — still meant to be fixed so legs actually run.

### Operator footnotes

- **`data/paper_trades`:** `TradeJournal(resume_latest=True)` picks newest session dir with any of entries/positions/summary; stray **`test_*`** dirs can become the resumed session — remove/rename if you want a fresh timestamped session.
- **Preflight:** `python scripts/preflight.py` before runs.

## 2026-04-09 — `ai.live_inferencing` (live LLM kill switch without dropping key setup)

- **What:** New `ai.live_inferencing` (default `true`) in `config/settings.yaml`. When `false`, `AIAgent.analyze_market` returns before cache or provider calls. Dashboard checkbox **Live LLM calls (ai.live_inferencing)**; `compute_ai_status` / startup messaging distinguish **PAUSED** (keys OK, calls off) vs full **OFF**.
- **Why:** Operator can stay within LLM quotas while keeping provider config intact; backtests already avoid real LLMs (`BacktestAIAgent` / quant crypto scripts).
- **Verification:** `pytest` green; toggle persists via `POST /api/config` + `PolyBot.apply_config_updates` → `ai_agent.refresh_from_config`.

## 2026-04-09 — Concurrent dashboard backtests + XRP dump-hedge wiring

- **What:** Dashboard backtest API supports multiple jobs (`job_id`); live bot and backtests can run together. Scanner adds `xrp-updown-15m`; optional strategy `xrp_dump_hedge` (see strategy log).
- **Why:** Ops isolation and experimental XRP path without blocking BTC/SOL/ETH.
