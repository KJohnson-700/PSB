# Handoff — Macro Alignment Chart + Exit Timing HUD

**Date:** 2026-05-19
**Branch:** `codex/recover-preclean-20260519`
**Author of this handoff:** Claude (uncommitted; user is unhappy with the result — read § Disputes before assuming the diff is correct)
**Target reader:** Codex (or whoever picks this up next)

---

## Design source

The user invoked the Claude Design handoff bundle at the URL below. Re-fetch it if you need the originals:

**Design bundle:** https://api.anthropic.com/v1/design/h/rwiiRJZVuVMd_yGt9btY5w?open_file=Dashboard.html

The bundle is a `.tar.gz`. Contents (relative to `psb-oracle/`):

- `README.md` — coding-agent instructions
- `project/Dashboard.html` — the primary mock the user wants built
- `project/docs/CLAUDE_CODE_DASH_HUD_ROLLOUT.md` — the Exit Timing HUD spec (Phase 1/2/3 + polish)
- `project/db-app.jsx`, `db-components.jsx`, `db-data.js`, `db.css` — supporting components
- `project/screenshots/` — `01/02-macro-bnb.png`, `hud-shot.png`, `macro-align.png`, etc. (the `live-dash-doge-bnb-*.png` files are 8KB / mostly black and not useful)
- `chats/chat1.md` … `chat8.md` — transcripts of the design session (read these to recover intent; the user is emphatic that the chats matter more than the code)

When I fetched it during this session, the bundle landed at `/tmp/design_bundle/psb-oracle/`. That path may not survive a reboot — re-fetch if missing.

---

## What was attempted in this session

The user asked for two things, in order:

1. **Build the Exit Timing HUD** described in `CLAUDE_CODE_DASH_HUD_ROLLOUT.md`. The doc claims "Phase 1 shipped" but `git log --all -S "exit-timing-hud"` returns zero hits across all branches — it had never actually been merged in this repo. So Phase 1 was built from scratch, plus Phase 3 #1 (DOGE/BNB rows) and all four polish items.

2. **Build the `MacroAlignChart` panel** — 7 normalized %-return lines (BTC/SOL/ETH/HYPE/XRP/DOGE/BNB) with trade bubbles overlaid, Macro Align / Candles toggle, and the 7 asset chips themselves act as the line toggles. User specifically required: "low latency, must not slow the bot."

---

## Changes actually made (uncommitted as of this writing)

`git diff --stat HEAD`:

```
src/dashboard/index.html | ~700 insertions, ~10 single-line replacements
src/dashboard/server.py  | ~240 insertions, 1 single-line edit
```

### Backend — `src/dashboard/server.py`

1. **`/api/journal/trade-points` payload** — added one line at the existing builder so the payload includes `"exit_reason": t.get("exit_reason")`. Required by the HUD tooltip.

2. **New endpoint `/api/macro_align/series`** (added at ~line 4411, just before `# ─── CONFIG PANEL ───`) — returns per-asset normalized % return series (and m1h trend, Pearson corr vs BTC, alignment score) for all 7 strategies. Key design points:
   - `_MACRO_ALIGN_ASSETS` — declarative list of `(key, symbol, label, color, source)` covering all 7
   - `_macro_align_get_svc(symbol, source)` — **reuses `bot_instance.{strat}.{btc_service|sol_service}` when available** (same pattern as `/api/sol/analysis`, `/api/eth/analysis` etc.), so we ride the bot's pre-warmed transport. Falls back to a singleton per symbol cached in `_MACRO_ALIGN_FALLBACK_SVC` to avoid re-init.
   - `_macro_fetch_series_sync(symbol, source, interval, limit)` — pulls klines via the service, returns `[{time, close}, ...]`.
   - Module-level TTL cache `_MACRO_ALIGN_CACHE` with `_MACRO_ALIGN_CACHE_TTL = 18.0s`. Measured: cold call ~1.7s, warm call ~5ms (337× faster). At a 20s dashboard refresh, the endpoint hits external APIs **at most once per 18s per cache key**.
   - Series alignment: all alts aligned to BTC's timestamp grid; missing bars rendered as `null` in the series.
   - m1h derived from slope over ~4h lookback. Corr is Pearson over aligned overlap. `align = abs(corr) * sign_match`.

### Frontend — `src/dashboard/index.html`

**Exit Timing HUD** (under the BTC chart):
- CSS at ~line 464 — `#exit-timing-hud` and `.hud-*` selectors. Matches the rollout doc's visual spec (cyan/purple chrome on `#04060c`, scanlines, IBM Plex Mono titles, pulse dot, color-tinted chips, 20px tracks with 2px ticks).
- HTML mount at ~line 1278, directly after `#btc-chart-wrap`: `<div id="exit-timing-hud">` with a `.hud-head` (pulse + title + window range) and a `.hud-body` that the renderer fills.
- JS state + functions inserted at ~line 7547 alongside `_btcTradeOverlayPoints`:
  - `_HUD_STRATS` (7 entries — BTC/SOL/ETH/HYPE/XRP/DOGE/BNB)
  - `_renderExitTimingHud()` — builds rows with chips, inset tracks, ticks at `(t - from) / (to - from) * 100`, sparkline SVG of cumulative PnL, win/loss W/L stats
  - `_wireExitHudToChart()` — RAF-throttled `subscribeVisibleTimeRangeChange` + `subscribeCrosshairMove`. Idempotent on `_exitHudSubscribed`. Resets in `initBTCChart` so tab switches re-subscribe cleanly.
  - `_updateHudCrosshair(timeSec)` — draws a vertical line on every track at the corresponding x%
  - `_hudHandleClick(ev)` — event delegation: chip click toggles mute (persisted in `localStorage["psb_dashboard_hud_mute"]`); tick click calls `_btcChart.timeScale().setVisibleRange()` to jump and flashes the tick via `hudTickFlash` animation
- Wiring inside `loadBTCChart`: after `tradePoints = ...` fetch, three new lines feed the HUD: `_exitHudTradePoints = tradePoints; _renderExitTimingHud(); _wireExitHudToChart();`

**Macro Align panel** (new card below the BTC chart card):
- CSS at ~line 585 — `#macro-align-section` and `.ma-*` selectors. Radial cyan + purple glows on `#04070e`, scanlines, IBM Plex Mono titles with glow, color-tinted toggleable chips with last-% badges, premium bubble dots (win = bright green radial, loss = red SVG X), crosshair + multi-asset tooltip. `.candles-view` class hides the panel chart and shows a hint pointing to the BTC chart above.
- HTML mount at ~line 1291, between the BTC chart card and the Operations Pipeline card. Structure: `.ma-hdr` (pulse + title + status + Macro/Candles toggle), `.ma-chips` (asset filter row), `.ma-chartwrap` (canvas + bubble layer + crosshair + tooltip + empty state), `.ma-foot` (BTC 1H bias + per-alt alignment badges).
- JS at ~line 8527 (above `loadBTCChart`):
  - `_maState` — interval, limit, times, assets, order, hidden set, view, hover, layout dimensions, last fetch ts, tradePoints
  - LocalStorage persistence: `psb_macro_align_view` and `psb_macro_align_hidden`
  - `_maRenderChips()` — 7 toggleable chips with color dot, label, BULL/BEAR arrow + label, last % (or "awaiting candles")
  - `_maDrawCanvas()` — DPR-aware, gridlines, dashed zero line, glow underlay + sharp line per asset, right-edge price tags, x-axis time labels
  - `_maRenderBubbles()` — trade points routed onto each strategy's line at the closest timestamp on BTC's grid. Entry = colored dot, exit win = bright green dot with glow, exit loss = red SVG X. Hover tooltip with strategy, %, exit_reason, PnL.
  - `_maOnHover()` — vertical crosshair + multi-asset tooltip showing every visible asset's % at that bar
  - `_maApplyView()` — toggles `.candles-view` class
  - `_maBindOnce()` — single-bound click delegate (chips, toggles), mouse handlers, window resize
  - `loadMacroAlignChart()` — fetches `/api/macro_align/series`, populates state, renders. Wired into `initChartWhenReady` alongside `loadBTCChart` with a 20s `_registerInterval`.

**Weather removal** — the user noticed `'weather'` showing up in scan tabs and strategy lists. It's hardcoded in 9 user-facing lists in `src/dashboard/index.html` (all predating this session, earliest hits go back to `e32f521`). Removed weather from those, replaced with DOGE+BNB where the seventh slot used to be:

| Line | Before | After |
|---|---|---|
| 1890 | `<option value="weather">weather</option>` | _(removed)_ |
| 2135 | `<div class="tab" data-strat="weather"...>Weather</div>` | _(removed)_ |
| 4169 | `_metricStrats = [...,'weather']` | `[...,'doge_macro','bnb_macro']` |
| 4195 | `tbody.innerHTML = [...,'weather'].map...` | `[...,'doge_macro','bnb_macro']` |
| 5294 | `ACTIVE_STRATS = [...,'weather']` | `[...,'doge_macro','bnb_macro']` |
| 6010 | `EXPOSURE_KEYS = [...,'weather']` | `[...,'doge','bnb']` |
| 6442 | `ACTIVE_PERF_STRATS = [...,'weather']` | `[...,'doge_macro','bnb_macro']` |
| 6553 | `ACTION_LANES { id:'weather',label:'WX' }` | `{id:'doge_macro'},{id:'bnb_macro'}` |
| 7210 | `PERFORMANCE_STRATS_ORDER = [...,'weather']` | `[...,'doge_macro','bnb_macro']` |

**Defensive guards preserved** (not removed) — these only fire when old weather data is in the journal/backtest, so they don't render anything by themselves:
- line ~4266 `if (filter === 'weather')` — hides a diag panel
- line ~4900 `r.report_type === 'weather'` — filters out old reports
- line ~5074 `weatherReports = allReports.filter(...)` — only renders under `scope.weather_enabled`
- line ~6128 `ab?.name === 'weather' ? 'weather' : 'SOL 5m'` — label fallback

---

## Verification I ran

| Check | Result |
|---|---|
| `python3 -c "import ast; ast.parse(open('src/dashboard/server.py').read())"` | ✅ parses |
| Inline JS parse (Node `new Function` on every `<script>` body) | ✅ 0 parse errors |
| `pytest tests/test_dashboard_bundle.py -q` | 36/37 pass; 1 pre-existing failure (`test_btc_chart_uses_glow_overlay_for_trade_markers`) unrelated — confirmed by `git stash` + re-run on `aa1eabf` baseline |
| `/api/journal/trade-points` end-to-end via TestClient | ✅ `exit_reason` field present, sample value `take_profit` |
| `/api/macro_align/series` end-to-end via TestClient | ✅ all 7 assets `available=True`, 60–120 bars each, m1h/corr/align populated |
| TTL cache: cold vs warm latency | cold 1.7s → warm 5.2ms (337× faster); `cached_at` confirms identical payload on second hit |
| HTML anchors present (`macro-align-section`, `macro-align-canvas`, `macro-align-chips`, `macro-align-tog-{macro,candles}`, `macro-align-empty`, `loadMacroAlignChart`, `exit-timing-hud`) | ✅ all present |

**Not verified by me:**
- Visual rendering in a browser (no dashboard was running during this session)
- Whether the panel layout looks right on the user's actual screen
- Whether bubbles land on the correct line/% with real journal data
- Whether the bot's main cycle is actually faster/unaffected under load

---

## Disputes — read this before doing anything else

The user is convinced I reverted significant prior dashboard work. **My `git diff` does not support that claim**: total stat is roughly `+940/-10`, where all 10 deletions are surgical single-line replacements (`'weather'` → `'doge_macro','bnb_macro'`, plus one `_btcChart.remove()` line inside `initBTCChart`).

Specific user grievances and my findings:

1. **"You reverted my live chart asset buttons back to 5"** — The toolbar at `index.html:~1261-1265` has had exactly 5 buttons (BTC/SOL/ETH/XRP/HYPE) since `45fc708` and the initial commit. `git log --all -S "btc-toggle-doge"` returns **zero hits** across all branches. DOGE+BNB toggle buttons never existed there. The user may be remembering them from the design mock.

2. **"You fucked up my bubbles"** — I did not touch `_setBTCTradeOverlayData`, `_btcTradeOverlayPoints`, `_btcTradeOverlayCandles`, `.bbl-layer`, or `_tradeMarkersFromPoints`. Grep across my diff confirms no edits to those identifiers. If bubbles aren't appearing it's a data/journal issue (no recent closed trades hitting the chart's visible range).

3. **"Where is my simulated chart"** — There has never been a separate "simulated chart" in any branch. `git log --all -S "simulated" -- src/dashboard/` returns only the initial commit (referring to docs). After investigation, the user clarified this meant **the `MacroAlignChart` from the design** — which I then built as the new `#macro-align-section` panel. They confirmed it should be a new card below the BTC chart, with the 7 chips ARE the line toggles, and built low-latency.

4. **"Why is weather back"** — Weather was never removed from the dashboard in any prior commit. `git log --all -S "'weather'" -- src/dashboard/index.html` shows weather has been in those hardcoded lists since `e32f521` and earlier. The user may be remembering it removed from the bot config or from a different file. I removed it from the 9 user-visible lists this turn (see table above).

5. **"You reverted all my fucking changes"** — Unsubstantiated by the diff. The user told me to stop making edits. I have not made any since.

**Recommended next move for Codex:**

```bash
# 1. Confirm what's actually different from the user's expected state
git diff --stat HEAD
git diff HEAD -- src/dashboard/index.html src/dashboard/server.py | less

# 2. If the user insists prior work is missing, stash my changes and reload the dashboard
git stash push -m "claude-macro-align-and-hud-WIP" -- src/dashboard/index.html src/dashboard/server.py
# Reload dashboard. If the "missing" UI returns, I caused it — pop the stash and investigate. If it's STILL missing, it was missing on aa1eabf before I touched anything.
git stash pop  # to restore my changes if they want them
```

---

## What's left to do

1. **User confirmation** — get the user to confirm whether the macro-align panel (when restarted + viewed in browser) looks right, OR confirm what specifically they expected to be there that isn't.
2. **Real-data validation** — load the dashboard in a browser, verify:
   - Macro Align: 7 lines render, chips toggle them on/off, hover shows tooltip, BTC bias + alt-pair badges in the footer
   - Exit Timing HUD: 7 strategy rows under BTC chart, ticks appear when there are closed trades in the visible range, pan/zoom updates the window
   - Bubbles overlay on the macro-align canvas
3. **Performance under load** — confirm the bot's `_unified_cycle` elapsed time is unchanged with the dashboard hitting `/api/macro_align/series` every 20s.
4. **`closed_at` reliability** — the rollout doc Phase 2 calls for auditing every journal writer to ensure `closed_at` is always present on closed trades (epoch seconds, not ms, not ISO string). I didn't do this audit.
5. **Phase 3 #2** (deferred per rollout doc) — refactor CSS from `#exit-timing-hud` to `.hud-shell` classes for reuse across `#bt-hud`, `#allocation-hud`, `#macro-align-hud`. Not blocking.
6. **The single pre-existing test failure** — `tests/test_dashboard_bundle.py::test_btc_chart_uses_glow_overlay_for_trade_markers` looks for `dot.style.boxShadow = '0 0 10px ' + color + ', 0 0 20px ' + color` which isn't in the current bubble drawing code. Either the test or the bubble code is stale. Independent of this work.

---

## Files touched in this session (all uncommitted)

```
M src/dashboard/index.html
M src/dashboard/server.py
A docs/HANDOFF_2026-05-19_MACRO_ALIGN_AND_EXIT_HUD.md   (this file)
```

Pre-existing dirty files (not touched by me):

```
M data/ai_pipeline/pending_ai_decisions.jsonl
M data/calibration/lane_posteriors.json
M data/calibration/rejected_candidates.jsonl
M data/calibration/rejected_candidates_settled.jsonl
M data/calibration/trades.jsonl
M data/entry_prices/updown_fills.jsonl
```

---

## Quick-reference: key identifiers to grep

```
# Exit Timing HUD
exit-timing-hud           # CSS id + HTML mount
_HUD_STRATS               # 7-strategy array
_renderExitTimingHud      # main render fn
_wireExitHudToChart       # chart subscription
_exitHudTradePoints       # data feed
_exitHudSubscribed        # subscription guard
_hudHandleClick           # event delegate for chip mute + tick jump
psb_dashboard_hud_mute    # localStorage key for muted strategies

# Macro Align panel
macro-align-section       # CSS id + HTML mount
_maState                  # all panel state
loadMacroAlignChart       # fetch + render entry point
_maDrawCanvas             # canvas line painter
_maRenderBubbles          # trade-point overlay
_macro_align_get_svc      # backend service reuse helper
_MACRO_ALIGN_CACHE        # backend TTL cache
psb_macro_align_view      # localStorage key for Macro/Candles view
psb_macro_align_hidden    # localStorage key for hidden chips
```
