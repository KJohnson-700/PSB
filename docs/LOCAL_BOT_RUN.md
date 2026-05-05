# Local bot: port, URL, stop, start

## Port and URL (local / no `PORT` env)

| Item | Value |
|------|--------|
| **Dashboard port** | **`8081`** — from `config/settings.yaml` → `dashboard.dashboard_port`. |
| **Bind address** | **`127.0.0.1`** (localhost only) when Railway/Render-style `PORT` is **not** set. |
| **Open in browser** | **http://127.0.0.1:8081** |

If **`PORT`** is set in the environment (some hosts), the process listens on **`0.0.0.0:$PORT`** instead; locally, leave `PORT` unset so behavior matches this doc.

## Start (paper + dashboard)

From repo root:

```bash
cd "/path/to/psb-main 1"   # repo root; quote if the path contains a space
.venv/bin/python start.py --paper
```

(`start.py` with no mode flag also defaults to `--paper`.)

## Stop

- In the terminal where the bot is running: **Ctrl+C**.
- **Do not** start a second `start.py` while one is already bound to **8081** — you will get `address already in use` and two bot processes.

## Restart (one clean instance)

1. Stop the running process (**Ctrl+C**), **or** find and kill what holds the port:

   ```bash
   lsof -nP -iTCP:8081 -sTCP:LISTEN
   kill -TERM <PID>
   ```

2. Start again with the **Start** command above.

## For agents / operators

- **Where to look:** anything listening on **TCP 8081** is the dashboard for this default config.
- **Config key:** `dashboard.dashboard_port` in `config/settings.yaml` (change port there if 8081 collides with another app).
- Entry points: **`start.py`** (recommended) or **`python -m src.main --paper`**.

## Dashboard API key (LAN / non-loopback only)

Mutating dashboard routes (`POST` config, exposure pause, etc.) require auth when:

- **`DASHBOARD_API_KEY`** is set in the process environment (bot must receive the **same** value in header `X-API-Key`), or
- **`DASHBOARD_API_KEY`** is unset but the HTTP client IP is **not** loopback: the server responds with **503** until you set the env var (see `_check_auth` in `src/dashboard/server.py`).

**Local-only (default):** With `dashboard.host: "127.0.0.1"` and **`DASHBOARD_API_KEY`** unset, the browser at `http://127.0.0.1:8081` can call mutating APIs without a key.

**From another machine on your LAN:** Set **`DASHBOARD_API_KEY`** in the bot environment to a random string; open the dashboard, trigger a mutation once, enter that string when prompted (stored as `localStorage` `psb_dashboard_api_key`).
