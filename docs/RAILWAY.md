# Deploying on Railway

The bot runs in Docker and uses **`PORT`** (set by Railway) for the FastAPI dashboard. Bind **`0.0.0.0`** is automatic when `PORT` is present.

## One-time setup

1. Push this repo to GitHub (or connect your repo to Railway).
2. **New project** → **Deploy from GitHub** → select the repo.
3. Railway detects the root **`Dockerfile`**.
4. **Config baked into the image:** the build copies **`config/settings.yaml`** (see root `Dockerfile`). Trading and exposure sizing (e.g. `trading.default_position_size`, `exposure.min_trade_usd`) ship with that file unless you mount a volume or change the image. Secrets stay in Railway **Variables**, not in the image (`.dockerignore` excludes `config/secrets.env`).
5. **Variables** (service → Variables): add everything you normally use in `config/secrets.env`, for example:
   - `POLYMARKET_PRIVATE_KEY` or `PRIVATE_KEY`
   - `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE` (if needed)
   - `OPENROUTER_API_KEY` (or other LLM keys in your `provider_chain`)
   - **`DASHBOARD_API_KEY`** *(optional)* — only if you want mutating dashboard API routes to require `X-API-Key`. If unset, the server does not enforce this (you should still treat a public URL as sensitive; add edge auth if needed).
   - **`EXPOSURE_LOSS_KILL_SWITCH_ENABLED`** — optional override at **container start**: `true` / `1` / `yes` forces **per-lane** 3-loss pause on; `false` forces off. Use this if the service is still running an **old Docker image** whose baked `settings.yaml` has the switch off (redeploying from current Git also fixes that). After changing variables, **restart** the service. Startup logs and `OPS_JSON` include `exposure_loss_kill_enabled` so you can confirm.
6. Optional: **`DASHBOARD_HOST`** (default `0.0.0.0` when `PORT` is set).
7. **Generate domain** (Settings → Networking) so `RAILWAY_PUBLIC_DOMAIN` is set and logs show the correct HTTPS URL.

### CI deploy (fixes “stale image” / wrong builder)

If production `/health` shows an old `dashboard_ui_rev` while `main` on GitHub is newer, Railway’s GitHub integration may have deployed with **Railpack** or a **redeploy** that replayed an old digest. The repo includes **`.github/workflows/deploy-railway.yml`**, which runs on every push to **`main`** and executes **`railway up --ci`** so the **root `Dockerfile`** builds from the **exact commit** GitHub checked out.

1. In Railway: **Project → Settings → Tokens** → create a **project token** (not your account password).
2. In GitHub: **Repo → Settings → Secrets and variables → Actions** → **New repository secret** → name **`RAILWAY_TOKEN`**, paste the token.
3. Push to `main` (or re-run the workflow). The job fails fast if `RAILWAY_TOKEN` is missing.
4. **Optional but recommended:** In Railway → your **service** → **GitHub** settings, **disable automatic deploys** for this service if you rely only on this workflow—avoids two competing deploys (Git integration + CLI).

See also [Deploying with the CLI](https://docs.railway.com/cli/deploying) (project tokens and `RAILWAY_TOKEN`).

## Health checks

The dashboard binds **`0.0.0.0:$PORT`** as soon as settings load, **before** the bot finishes journal replay. Point Railway’s HTTP health check at **`/health`** (or `/`) so deploys don’t sit in “Initializing” while `entries.jsonl` is replayed.

## Start command

`Dockerfile` defaults to **`python -m src.main --paper`**. In the Railway service **Settings → Deploy → Custom start command**, you can override, for example:

- `--dashboard-only` — dashboard + backtests only; no trading loop.
- `--paper` — paper trading (default image).
- `--live` — requires your usual live safeguards; avoid interactive prompts in CI.

**Do not** use **`--backtest`** as the primary service start command. That entry mode forwards to `scripts/run_backtest.py` and **exits** when the run finishes; it does **not** keep the trading loops alive. Use the dashboard backtest API (below) or a **separate** Cron/job for CLI backtests.

## Backtests while paper or live runs

On Railway, **one container** runs `python -m src.main --paper` (or `--live`). The same process serves the FastAPI dashboard on **`PORT`** and runs the bot.

- **While the service is up:** open the dashboard → **Backtest** tab → **Run Backtest** / **Run Crypto BT**. The server handles `POST /api/backtest/start` by spawning a **subprocess** in the container; trading continues in parallel (subject to CPU/RAM on your plan). When the job finishes, the UI refreshes **`GET /api/backtest/reports`** (and live drift pulls the same `data/backtest/reports/backtest_*.json` files), so you see new ETH/SOL/BTC cards and XRP sim summaries without restarting the service.
- **CLI snippets** in the Commands tab (`python src/main.py --backtest …`, `python scripts/run_backtest.py …`) are for **local shells** or a **second Railway service** (e.g. scheduled job), not for replacing the trading service command.
- **Optional regression:** Railway **Cron** (or another service) can run `python scripts/run_backtest_rigorous.py` / `run_backtest_crypto.py` on a schedule; keep the main deploy on `--paper` or `--live`.

## Data persistence

`data/` (journal, snapshots, kill switch) is **ephemeral** on the default disk. For durable state, attach a **Railway volume** and mount it at **`/app/data`** (the `Dockerfile` uses `WORKDIR /app`, so journal paths like `data/paper_trades/` resolve to `/app/data/paper_trades/`).

Backtest outputs (e.g. **`data/backtest/reports/`**, and cached OHLCV under `data/backtest/ohlcv/`) follow the same rule: without a volume they disappear on redeploy.

File logs go to **`data/logs/polybot_YYYYMMDD.log`** (same root as the journal), so a volume at `/app/data` keeps both.

### What should survive deploys vs what can reset

| Path under `data/` | Keep across deploys? | Notes |
|--------------------|----------------------|--------|
| **`paper_trades/`** | **Yes** | Trade log (`entries.jsonl`), open positions, **`snapshots.jsonl`** (journal PnL chart), `summary.json`. The bot **resumes the latest session with data** on restart so the dashboard is not empty after a redeploy. |
| **`paper_trades_archive/`** | **Yes** | Archived sessions if you move runs here. |
| **`logs/`** | **Yes** | Operator visibility. |
| **`KILL_SWITCH`** | **Usually yes** | If the file persists, trading stays blocked until you clear it—matches “real” disk. If you rely on redeploys to clear a stuck kill switch, omit persistence or delete the file once after deploy. |
| **`backtest/reports/`** | Optional | Nice to keep for history; safe to delete or exclude from backups if large. |
| **`backtest/ohlcv/`** | Optional | Cache only—can be huge; OK to drop or prune; refetch or regenerate. |

One volume mounted at **`/app/data`** covers everything you want durable; periodically prune `backtest/ohlcv/` if disk grows.

## Paper sessions and test data (local + hosted)

`data/paper_trades/` is **gitignored**; sessions exist only on disk (or a Railway volume). Do not expect Git to preserve operator runs.

| Mechanism | Behavior |
|-----------|----------|
| **Default (no extra env)** | Each **new** process start creates a **fresh** session: `test_%Y%m%d_%H%M%S` under `data/paper_trades/`. This matches the comment in `PolyBot` that treats container restart as a new test cycle. |
| **`PAPER_SESSION_ID=<name>`** | Use that **exact** directory name (e.g. `PAPER_SESSION_ID=reset_20260416`). Creates or continues that folder. |
| **`PAPER_RESUME_SESSION=true`** (and no `PAPER_SESSION_ID`) | **Opt-in** resume: `TradeJournal(resume_latest=True)` picks the **newest** `data/paper_trades/*` that already has journal artifacts (`entries.jsonl` / `positions.json` / `summary.json`). **Pitfall:** old stray directories named `test_*` can be picked as “latest” and confuse heatmaps—archive or remove sessions you do not want resumed (see [changelog](../projects/polymarket-bot/changelog.md) paper-journal note). |
| **Without a Railway volume** on `/app/data` | Every **redeploy** starts with an **empty** `data/`; you get a **new** session on boot unless you set env to force a name. |

**Mac mini / paths with spaces** — if the project folder is named like `psb-main 1`, always **quote** paths in shell and IDE configs, e.g. `cd "/Users/me/Documents/psb-main 1"` and `"/Users/me/Documents/psb-main 1/.venv/bin/python" start.py`.

**Hourly analysis** — [scripts/hourly_heatmap.py](../scripts/hourly_heatmap.py) needs **closed** trade lines in `entries.jsonl` (or journal-backed data) per strategy; it will show **0 trades** if only `snapshots.jsonl` / `summary.json` exist. See [DASHBOARD_DATA_SOURCES.md](DASHBOARD_DATA_SOURCES.md) for UI vs disk truth.

**If `OPS_JSON` shows `total_entries: 0` forever** — check **Deploy** logs for Python errors in the strategy or execution path first. If the loop runs but you still see no fills, check: UTC **blocklist** in `config/settings.yaml`, `can_sell_token` liquidity skips, risk limits, `data/KILL_SWITCH`, and `trading.dry_run`.

## Seeing results (trades, PnL, “did it do anything?”)

Railway only shows what the process prints to **stdout/stderr** unless you attach storage.

1. **Structured ops lines (best for log drains)**  
   Every main trading cycle and every crypto fast cycle emits one line:
   `OPS_JSON {"event":"ops_pulse",...}`  
   Filter in the Railway UI or CLI:
   - `railway logs` then search for `OPS_JSON`
   - Windows: `railway logs 2>&1 | findstr OPS_JSON`

   Fields include: `session_id`, `journal_dir`, `dry_run`, `kill_switch`, `open_positions`, `closed_trades`, `total_entries`, `realized_pnl`, `unrealized_pnl`, `bankroll`, `last_signal_counts`, `cumulative_signal_counts`, `dashboard_url`.

2. **HTTP (no log parsing)**  
   After you generate a public domain:
   - `GET https://<your-domain>/api/ops/summary` — same JSON as `OPS_JSON` pulses  
   - `GET https://<your-domain>/api/journal/summary` — session PnL / win rate  
   - `GET https://<your-domain>/api/journal/entries?limit=50` — recent ENTRY/EXIT/SKIP rows. For a public URL, use optional `DASHBOARD_API_KEY` and/or edge auth if you need to lock down mutating routes.

3. **Disable ops pulses** (quieter logs): in `config/settings.yaml` set `logging.ops_pulse: false`.

## Stuck on “Initializing” or deploy logs empty / “0 loading”

1. **Separate Build vs Deploy logs** in the Railway UI. **Build** = Docker + `pip` (can take several minutes). **Deploy** = container start + stdout/stderr. An empty or stuck **Deploy** log often means the **build never finished** or failed—check **Build** first.
2. **Heavy image** — the root `Dockerfile` uses **`requirements-railway.txt`** (no `nautilus_trader`) so installs stay smaller. If you switched back to full `requirements.txt` in Docker, expect long builds and possible OOM on small plans.
3. **Health check** — [`railway.json`](../railway.json) probes `GET /health` with a generous timeout. If the process crashes before Uvicorn binds (e.g. `ModuleNotFoundError`), the deploy fails: read **Deploy** logs from the **first** error line.
4. **Log UI** — Railway occasionally lags; use **`railway logs`** from the CLI or redeploy. Ensure `PYTHONUNBUFFERED=1` (set in the `Dockerfile`) so lines flush to stdout.
5. **Verify locally:** `docker build -t polybot . && docker run --rm -e PORT=8080 -p 8080:8080 polybot` then `curl http://127.0.0.1:8080/health`.

## Verifying the dashboard bundle (not stuck on an old image)

Production HTML is served from **`src/dashboard/index.html` inside the container** (`Dockerfile` copies `src/`). If the UI looks outdated (e.g. missing **Trade journey** or still saying **BTC/SOL Reason Buckets** instead of **Crypto reason buckets**), the running deploy is almost certainly **not** the latest build.

1. **Compare `/health` to Git** — responses include `dashboard_ui_rev`, optional `git_sha` (baked via `Dockerfile` `ARG RAILWAY_GIT_COMMIT_SHA` on GitHub builds), and `railway_deployment_id` (new id when a new deployment rolls out):
   ```bash
   curl -s https://<your-service>.up.railway.app/health
   ```
   A body of only `{"status":"ok"}` with no `dashboard_ui_rev` means the container is still on **pre-April-2026** dashboard code.
2. **“Redeploy” vs a new Git build** — Railway’s **Redeploy** can **reuse the same Docker image** (same digest) if no new build runs. That keeps an **old** `dashboard_ui_rev` even though you clicked deploy. You need a **fresh build from Git** (push to `main`, or **Deploy** / **Deploy latest commit** from the connected branch) so the Dockerfile runs again and copies current `src/`. The root `Dockerfile` bakes `RAILWAY_GIT_COMMIT_SHA` into the image so `git_sha` in `/health` matches the commit that was **built**, not a token issue.
3. **Force a clean build (no “clear cache” button)** — Railway does **not** expose a separate “clear build cache” control in the UI. Per [Railway build configuration](https://docs.railway.com/guides/build-configuration#disable-build-layer-caching), add a **service variable** **`NO_CACHE=1`**, save, and let the service redeploy (or trigger a new deploy from the latest commit). That disables Docker layer caching for builds. After a good deploy, remove `NO_CACHE` or set it to `0` if you want faster builds again. Alternatives: push a new commit, or `railway up` from repo root (uploads your tree; respects `.railwayignore` / ignore rules).
4. **Confirm the connected repo/branch** — Settings → **Source** should be the same GitHub repo and branch you push to (`main` vs a fork).
5. **Quick HTML check** — `curl -s https://<your-domain>/ | findstr trade-journey` (Windows) or `grep trade-journey` — the new bundle includes `id="trade-journey-strategy"`.

## Local Docker

```bash
docker build -t polybot .
docker run --rm -p 8080:8080 -e PORT=8080 --env-file config/secrets.env polybot
```

Use `127.0.0.1:8080` in the browser when mapping `8080` locally.
