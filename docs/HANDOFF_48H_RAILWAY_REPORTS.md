# Handoff — ~48h window (2026-04-17 → 2026-04-19)

**Purpose:** Capture problems, in-repo report outcomes, deploy-relevant code changes, and what is required to pull **live** Railway paper-trading data from the next session or operator.

---

## 1. Code / repo activity (last ~48 hours)

**Latest commit (2026-04-19, America/Los_Angeles):** `2221411` — *feat(exposure): per-lane pause API, loss-kill default on, env override for Railway*

- Per-lane pause/resume API and dashboard controls (`btc` / `sol` / `eth` / `xrp` / `event` lanes).
- `exposure.loss_kill_switch_enabled` default-on path; config reload / `apply_config_updates` alignment.
- **`EXPOSURE_LOSS_KILL_SWITCH_ENABLED`** env override at container start (documented in `docs/RAILWAY.md`) so old images or remote config drift can be corrected without a full settings bake.
- Startup warning + **`ops_pulse`** fields for visibility (per project docs: filter logs for `OPS_JSON` / `event":"ops_pulse"`).
- Dashboard `/health` bump (`dashboard_ui_rev`) for deploy verification.

**Implication for ops:** After deploy, confirm loss-kill and lane pause behavior via logs or `/api/ops/summary` (see §4).

---

## 2. Problems and blockers (investigation session)

### 2.1 Live Railway state — **not observable from this workspace**

| Issue | Evidence | Impact |
|--------|-----------|--------|
| **Railway CLI not authenticated** | `railway status` → `Unauthorized. Please login with railway login` | No `railway logs`, no service metadata, no deploy-triggered log tail from CLI. |
| **No project link in clone** | No `.railway/` directory in repo root | CLI commands may fail even after login until `railway link` (or equivalent) from this tree. |
| **No local mirror of hosted journal** | No `data/paper_trades/**/*.jsonl` in workspace | Cannot reconstruct open positions, session PnL, or recent ENTRY/EXIT from disk here. |

**What was *not* determined:** current paper session health, trade count, PnL, kill-switch file on disk, or whether a volume is mounted at `/app/data` (journal persistence).

### 2.2 Operator / environment follow-ups

- Run **`railway login`** on the machine that should pull logs (or use Railway web UI → Deployments → Logs).
- From repo root: **`railway link`** if the project is not linked.
- Prefer **`OPS_JSON`** lines or HTTP **`GET /api/ops/summary`** + **`GET /api/journal/summary`** for structured handoffs (see `docs/RAILWAY.md`).

### 2.3 MCP vs CLI for “live data”

- **Enabled MCP servers in this Cursor project** include Render, Vercel, Obsidian, browser, etc. — **not** a Railway MCP server. There is **no** substitute here for Railway logs or the dashboard API without adding an integration or pasting output.
- **Railway CLI** (authenticated + linked) remains the primary scripted path for log drains and service inspection unless you expose **`/api/ops/summary`** / **`/api/journal/summary`** via curl with `DASHBOARD_API_KEY`.

---

## 3. In-repo backtest / report results (artifacts on disk)

These files are **not** tied to the last 48 hours of *new* runs; they are the authoritative markdown summaries checked into `data/backtest/reports/`. Use them for strategy assessment until a fresh run produces new JSON/MD.

### 3.1 `BACKTEST_RIGOROUS_REPORT.md` (generated 2026-03-14)

- **Fade (2024 Q1–Q4, rigorous config):** Total PnL **~−$150**; **Sharpe ~−30.5**; **max drawdown ~−7.3%**; test (Q3+Q4) worse than train — **no clear OOS edge**.
- **Arbitrage:** **~−$6** total, almost all from Q4; **only one market** had sufficient bars across periods (2024 presidential); train periods had **zero** trades — **inconclusive** as a strategy read.
- **Execution:** Fade trades largely filled; costs reported per period; arbitrage barely active.

### 3.2 `BACKTEST_RESULTS_40PCT.md` (60 slugs / strategy, run 2026-03-14)

- **Fade:** Baseline total **~−$150**; same train/test deterioration pattern; stress scenarios worsen losses (slippage / fees).
- **Arbitrage:** **~−$6** total; same single-market data limitation.
- **Recommendation echoed in report:** Fade needs refinement; arbitrage needs universe/data rebuild before conclusions.

### 3.3 `RUN_STATUS.md`

- Marks a **32/32** backtest batch **complete**; last updated **2026-03-14 23:21:35** (checkpoint only, not a PnL report).

### 3.4 Older companion file

- `BACKTEST_RIGOROUS_REPORT_20260313.md` — prior rigorous run snapshot; use `BACKTEST_RIGOROUS_REPORT.md` unless diffing runs.

---

## 4. Checklist for the next operator / agent (live paper truth)

1. **Auth:** `railway login` → `railway link` (this repo).
2. **Logs:** Filter for `OPS_JSON` / `ops_pulse`; note `kill_switch`, `open_positions`, `realized_pnl`, `session_id`, `journal_dir`.
3. **HTTP (if domain + key available):** `/health` (incl. `git_sha` if set), `/api/ops/summary`, `/api/journal/summary`, optional `/api/journal/entries?limit=50`.
4. **Persistence:** Confirm whether **`/app/data`** volume exists; without it, redeploys reset `data/paper_trades/` and logs.
5. **Timezones:** Daily counters in-app may be **UTC**; calendar questions in **America/Los_Angeles** need explicit time windows (per `AGENTS.md`).

---

## 5. Summary

- **Shipped in ~48h:** Exposure loss-kill defaults, per-lane pause/resume, Railway env override, ops visibility hooks — commit **`2221411`**.
- **Open gap:** **No live Railway paper session analysis** from this environment — CLI unauthorized, no linked project metadata, no local journal copy.
- **Static reports:** Rigorous + 40% slug backtests show **negative Fade PnL** and **non-diagnostic Arbitrage** on the 2024 universe as documented in March 2026 reports.
- **For live data:** Use **authenticated Railway CLI** and/or **dashboard APIs**; **no Railway MCP** in current tooling — add one or paste JSON if automation must continue without CLI.
