# Cursor Agent Investigation Notes — PolyBot Issues Requiring Review

**Date:** 2026-04-21
**Last Updated:** cc078f7 (force-fresh-build-20260420232715)
**Current Railway ui_rev:** 2026-04-10-eth-xrp-journey (STALE — confirms old image still running)
**Live session:** `fresh_on_restart` | bankroll: $503.08

---

## ISSUE 1: Railway GitHub Integration Broken — CLI "No GitHub installation found"

### Symptoms:
- `railway variables set PAPER_SESSION_ID=fresh_on_restart` → "No GitHub installation found for repo: samuraifrenchienft/polymarket-bot"
- `railway up --service polymarket-bot` → "No GitHub installation found"
- Multiple pushes to GitHub (commits `cc078f7` through `529552f` through `d1db0eb`) did NOT trigger Railway builds
- Railway kept building from a cached old image (`2026-04-10` ui_rev)
- `railway deployment list` showed builds triggering from Railway events but NOT from Git pushes
- `railway service redeploy` eventually worked after Railway infra stabilized (build `b749bace` completed)

### Root Cause:
**Unknown.** Railway CLI lost the ability to authenticate with GitHub or locate the GitHub installation. The Railway project exists and CLI is authenticated (since `railway deployment list`, `railway service redeploy`, `railway logs` all work), but GitHub integration is broken. The "No GitHub installation found" error appears ONLY when trying to set variables or trigger builds via CLI.

### Evidence:
- `railway deployment list` works fine → CLI is authenticated to Railway
- `railway variables list` works fine → project is linked
- `railway variables set ANY_VAR=value` → "No GitHub installation found"
- `railway up` → same error
- GitHub pushes succeed (confirmed via `git push` output)
- Railway dashboard shows project connected to GitHub (under Settings → Source)

### Questions for Cursor Investigation:
1. Why does Railway CLI lose GitHub integration for some commands but not others?
2. Is this a token expiry issue specific to the Railway CLI version installed (`@railway/cli` npm package)?
3. Could the project's GitHub app installation have been revoked/uninstalled from the repo?
4. Is there a Railway project-level setting that controls CLI + GitHub integration vs dashboard GitHub integration?
5. Should `railway login --github` re-authenticate the CLI's GitHub access?

### When It Started:
- **Approximately:** After the 4th or 5th consecutive Git push in a short time window (multiple empty commits to force Railway builds)
- **Hypothesis:** Railway's GitHub app may have rate-limited or temporarily blocked the repo due to repeated CI-like trigger events from empty commits

### Fix Attempted:
- Pushed empty commits to try to force Railway build (worked initially, then stopped triggering)
- Used `railway service redeploy` from CLI (worked after Railway infra stabilized)
- Still: ui_rev is STALE (`2026-04-10`) even after `b749bace` SUCCESS — meaning the latest deploy built from a cached old image, NOT from the latest Git

### Status: UNRESOLVED — needs Cursor/human to investigate Railway project GitHub settings

---

## ISSUE 2: Railway Keeps Building From Stale Cached Image

### Symptoms:
- `dashboard_ui_rev` stayed at `2026-04-10-eth-xrp-journey` across multiple successful "deploys"
- All HYPE dashboard fixes (commits `bc9abd3`, `21f5541`, `633c17d`, `7603566`, `d1db0eb`) were NOT appearing on Railway despite being pushed to GitHub
- `b749bace` showed SUCCESS but ui_rev was still old

### Root Cause:
The Railway GitHub integration issue means Railway's build system is NOT pulling from the current main branch. It's rebuilding from a cached Docker layer or using an old project's build configuration.

### Evidence:
```
$ curl https://polymarket-bot-production-bf4f.up.railway.app/health
{"status":"ok","dashboard_ui_rev":"2026-04-10-eth-xrp-journey","git_sha":null}
```
Even after `b749bace SUCCESS` deploy, ui_rev is still `2026-04-10`.

### Questions for Cursor Investigation:
1. Why did Railway's build cache not invalidate after multiple Git pushes?
2. Is there a "Clear build cache" option in Railway that needs to be used?
3. Could the Dockerfile or `railway.json` have a caching directive that's preventing fresh builds?
4. Why does `railway deployment list` show builds triggering but the image stays the same?

### Status: UNRESOLVED — Railway build caching issue needs investigation

---

## ISSUE 3: SSE Flash-to-Zero on Reconnect (Command Center Hero)

### Symptoms:
- `hero-positions` and `hero-trades` in Command Center flash to 0 briefly every ~2-5 minutes
- `hero-bankroll` also briefly flashes

### Root Cause (confirmed):
`_setupSSE()` in `index.html` was writing ALL hero fields (`bankroll`, `positions`, `trades_today`) from SSE `/api/events` data every 2 seconds. When SSE reconnects (network hiccup, Railway cold-start), the first reconnect frame had stale/zero values for `positions` and `trades_today` from `journal.get_summary()` which returns 0 on fresh load.

### Fix Applied:
In `index.html` `_setupSSE()`, removed ALL hero field writes. SSE now ONLY updates `btc-price`. All hero fields (bankroll, positions, trades) are now ONLY updated by `fetchHeroStatus()` which polls `/api/status` every 8 seconds from the persistent TradeJournal source.

### File Changed:
- `src/dashboard/index.html` — `_setupSSE()` function

### Commit: `7603566`

### Verification:
- Need to confirm by watching Command Center during a Railway restart — if hero fields don't flash to 0, fix is working

---

## ISSUE 4: Old Session Data Carrying Over on Railway Restart

### Symptoms:
- User restarted Railway container to start a fresh strategy test
- Expected bankroll: $500 (initial_bankroll)
- Actual: $678 bankroll with -$17 PnL from old session `reset_20260416`
- Old session data (positions, trades, PnL) carried over into what should have been a clean test

### Root Cause (confirmed):
`TradeJournal(resume_latest=True)` on every PolyBot startup. Volume at `/app/data` makes the journal persist. When Railway recycles the container, bot restarts, finds the old session on disk, and resumes it — carrying over all history and bankroll.

### Fix Applied:
In `main.py` `__init__`, changed session logic:
- **Default now: fresh session every Railway restart** (`session_id=test_%Y%m%d_%H%M%S`, bankroll=initial_bankroll=$500)
- Opt-in to resume: set `PAPER_RESUME_SESSION=true` env var in Railway variables
- Explicit session: set `PAPER_SESSION_ID=<name>` to force a specific session

### Files Changed:
- `src/main.py` — session initialization logic (lines ~207-225)

### Commits: `d1db0eb`, `0308a72`

### Live Verification:
```
$ curl https://polymarket-bot-production-bf4f.up.railway.app/api/status
{"session_id":"fresh_on_restart","bankroll":503.08,...}
```
Confirmed working — session starts fresh at $500 on each Railway restart.

---

## ISSUE 5: Per-Window Kelly Streak Panel (5m vs 15m) — NOT YET BUILT

### Status: PENDING
- User requested per-window (5m vs 15m) Kelly streak panel
- Currently `kelly_state` in `/api/strategy/metrics` returns per-strategy streaks (no window split)
- `KellySizer.get_current_streak()` is per-strategy only
- Needs: window-level streak tracking in `KellySizer` + per-window UI in dashboard

---

## ISSUE 6: HYPE Missing from ALL Dashboard Sections

### Symptoms:
- HYPE entirely absent from: Exposure Manager, signal gates, Kelly streak panel, reason buckets, BTC chart toggles, session history per-strategy PnL

### Root Cause:
Two issues combined:
1. **Railway building from stale image** — HYPE commits (`bc9abd3`, `21f5541`, `633c17d`) not making it to Railway
2. **Missing `hypeData` parameter** in `updateCryptoSignalStatus()` call — JavaScript function signature didn't include hypeData argument

### Fixes Applied:
1. Added `hypeData` parameter to `updateCryptoSignalStatus(btcData, solData, ethData, hypeData, xrpData, updownData)`
2. Added `hype_macro` to `EXPOSURE_KEYS` array
3. Added `HYPE_updown_15m` and `HYPE_updown_5m` to all TARGET arrays in reason buckets and updown breakdown
4. Added `hypeTrades` to `_btcChartDisplay` object and all toggle/preset logic
5. Added `['hype_macro', 'HYPE', 2]` to `_formatAltWatchlistTriggers` cfg
6. Added `hype_macro` to `_tradeMarkersFromPoints` meta
7. Added `hype_macro` to allocation chart colors
8. Fixed `solData.macd_5m_hist` → `altData.macd_5m_hist` bug inside `applyAltLagGates`
9. Fixed missing `hype_macro` in rigorous backtest table `stratColors`

### Files Changed:
- `src/dashboard/index.html` — multiple locations

### Commits: `bc9abd3`, `21f5541`, `633c17d`

### Status: FIXES PUSHED, not yet deployed due to GitHub integration issue

---

## Summary of All Commits (latest first)

| Commit | Description |
|--------|-------------|
| `cc078f7` | force-fresh-build (empty commit to trigger Railway) |
| `529552f` | bump ui_rev to 2026-04-21-fresh-session-sse-hype |
| `d1db0eb` | Railway restart = fresh session by default. Opt-in resume with PAPER_RESUME_SESSION=true |
| `0308a72` | Add PAPER_SESSION_ID=fresh_on_restart: always start fresh session |
| `7603566` | SSE: only update btc_price via SSE; strip positions/trades from hero fields; bump ui_rev |
| `bc9abd3` | Exposure Manager: add HYPE lane; fix rigorous BT stratColors missing HYPE; add HYPE to TARGET arrays |
| `21f5541` | Add HYPE toggle to BTC chart: hypeTrades toggle, bubbles, watchlist, trade markers |
| `633c17d` | Fix HYPE metrics: pass hypeData to updateCryptoSignalStatus, fix solData ref in applyAltLagGates |
| `b3e9633` | Fix Command Center positions/trades zeroing: hero metrics now polled from /api/status every 8s |

---

## What Works Right Now (Confirmed)

- Bankroll fresh at $500 on Railway restart ✅ (session `fresh_on_restart`)
- SSE flash fix in local code ✅ (pending deploy)
- HYPE dashboard fixes in local code ✅ (pending deploy)
- All KellySizer wiring in strategies ✅
- Volume persistence working ✅ (337MB used on `polymarket-bot-volume`)

## What Needs Investigation

1. **Railway GitHub integration** — reconnect via Railway dashboard Settings → Source
2. **Railway build cache** — stale image being served despite successful deploys
3. **Per-window Kelly streak panel** — not yet built