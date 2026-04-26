# Handoff: BTC & SOL strategies — data review, issues, tweak ideas

**Purpose:** Standalone notes for a second reviewer (no chat history required).  
**Date context:** Written 2026-04-02. Paths are relative to repo root.

---

## 1) Leave Python as-is (Sabre / engine)

- **`src/analysis/btc_price_service.py`** → **`calc_trend_sabre()`** implements **Adaptive Trend Sabre (BOSWaves-style)**: HMA backbone, single ATR trail, tension, snap S/R. This is what **live strategies and backtests** use.
- **Dashboard** (`src/dashboard/index.html`) may visualize a **different** two-state ATR band model for **display only**. That JS must **not** replace Python unless there is an explicit product decision to port and re-backtest.
- **Do not conflate** “chart Sabre” with “bot Sabre” when tuning entries.

---

## 2) Where the numbers came from

| Source | Path / endpoint | What it shows |
|--------|-------------------|---------------|
| Paper journal (session) | `data/paper_trades/20260327_233727/summary.json` | Realized/unrealized PnL, per-strategy **closed-trade** stats for this paper session |
| BTC 15m backtest (example file) | `data/backtest/reports/backtest_crypto_BTC_15m_20260401_002219.json` | Large sample: `win_rate`, `expectancy`, `windows_entered` / `windows_scanned` |
| SOL 15m backtest | `data/backtest/reports/backtest_crypto_SOL_15m_20260331_173847.json` | Shorter date range than BTC; different universe length |

**Config knobs:** `config/settings.yaml` → `strategies.bitcoin` and `strategies.sol_macro`.  
**AI:** `config/settings.yaml` → `ai.enabled` was **false** at time of review (quant-only path unless changed).

---

## 3) Snapshot: paper session (`summary.json`)

Session id: `20260327_233727` (not necessarily calendar-aligned with “today”).

| Strategy | Closed trades | Win rate | Realized PnL (USD) |
|----------|---------------|----------|---------------------|
| **bitcoin** | 71 | **0.549** | **+22.47** |
| **sol_macro** | 4 | **0.25** | **-5.28** |
| fade | 44 | 0.068 | -33.89 |
| neh | 7 | 0.429 | -4.98 |

**Interpretation:**

- **BTC (paper):** Positive realized PnL and ~55% WR on **71** trades — meaningful sample; still **not** the same as backtest (different fills, markets, time mix).
- **SOL (paper):** **Only 4** closed trades — **not statistically meaningful** for WR; treat as “needs more data or blocked by gates,” not as true 25% WR.

---

## 4) Snapshot: backtest JSON (engine / universe)

### BTC 15m (`backtest_crypto_BTC_15m_20260401_002219.json`)

- **Period:** `2026-01-01` → `2026-04-01`
- **Trades:** 2324 entered / 8736 windows scanned  
- **Win rate:** **0.5353** (1244 W / 1080 L)  
- **Expectancy:** **0.1702** (per-trade unit in report)  
- **Net PnL (report):** +395.63 on 500 bankroll (simulated assumptions)

**Problem signals in trade rows (pattern):** Many sample rows show `"ltf_confirmed": false` with low `ltf_strength` — i.e. a **structural** share of entries may be **macro-led without strong 15m confirmation**, which caps win rate near **coin-flip** unless edge/exit rules compensate.

### SOL 15m (`backtest_crypto_SOL_15m_20260331_173847.json`)

- **Period:** `2026-03-14` → `2026-03-30` (short vs BTC file)
- **Trades:** 358 entered / 1632 scanned  
- **Win rate:** **0.5754**  
- **Expectancy:** **0.3708**  
- **Net PnL (report):** +132.76 on 500 bankroll  

**Contrast with paper:** Backtest WR **57.5%** vs paper **25%** on 4 trades — implies **live gating**, **different market mix**, **short sample**, or **sim vs live divergence**; do not merge these without a reconciliation pass (compare `updown_engine` assumptions to live `sol_macro` path).

---

## 5) Problem areas (actionable themes)

### A. Win rate vs profitability

- For **binary-ish** strategies at ~50% entry prices, **raw win rate** often sits near **50–55%** unless you **heavily filter** entries; **expectancy** and **R-multiple** matter as much as WR.
- **BTC backtest** already shows **positive** expectancy with ~53.5% WR — improving **WR** alone may **reduce trade count** and not improve net PnL if filters are wrong.

### B. “Unconfirmed LTF” bucket

- Backtest trades frequently show **`ltf_confirmed: false`**. If the thesis is “15m MACD must confirm,” then either:
  - the engine still enters on **macro-only** paths, or  
  - confirmation is defined such that many bars fail the flag while still trading.  
  **Worth tracing** in `src/backtest/updown_engine.py` vs `src/strategies/bitcoin.py` / `sol_macro.py` for parity.

### C. SOL: paper sample + thesis tension

- Code comments in **`sol_macro.py`** note historical patterns: **lag=None** trades performed **better** than **lag=value** in past samples; **`min_lag_magnitude_pct`** exists to damp weak lag.
- **Blocked UTC hours** (`blocked_utc_hours_updown`) exist because specific hours were negative in past live stats — re-validate after any logic change.

### D. Regime / macro (BTC dumps)

- **SOL** strategy is explicitly **BTC-correlation / macro-layered**. Fast BTC dumps can violate assumptions (lag, macro alignment, “near 50/50” entry). Expect **clustered losses** unless **volatility regime filters** (already partially exposure-based) are tightened.

### E. Session contamination (paper)

- **Portfolio-level** PnL mixes **fade** (very negative in this session) with crypto strategies. Attributing “BTC tanked, did we trade?” requires filtering journal by **`strategy == bitcoin`** / **`sol_macro`**, not headline session PnL.

---

## 6) Suggested tweaks to explore (hypotheses — backtest before live)

These are **starting points** for the second model / developer; each should be validated on **`run_backtest_crypto` / reports**, not gut-feel.

### Bitcoin

1. **Tighten the “unconfirmed 15m” path:** When `ltf_strength == 0` (or `ltf_confirmed` false), require **higher `min_edge`** (settings already bump edge in some cases — align live with backtest assumptions).
2. **Revisit `min_edge` / `min_edge_5m`:** Settings YAML contains data-driven comments; small changes move trade count a lot — optimize for **expectancy**, not WR alone.
3. **`blocked_utc_hours_updown`:** Expand or shrink based on **rolling** bucket analysis (WR/PnL by hour); avoid permanent blocks from tiny samples.
4. **Sabre tension:** Strategy already references tension thresholds in places — ensure **consistent** penalty/blocks when `tension_abs` is extreme (see `bitcoin.py` logging).
5. **AI off:** With `ai.enabled: false`, marginal trades use pure quant rules only — turning AI on changes **frequency** and **cost**; run shadow mode or A/B in paper first.

### SOL

1. **Entry price band:** Code documents **0.47–0.49** as strong vs **0.44–0.46** weak; `entry_price_min` / `entry_price_max` in YAML encode this — **narrowing** may raise WR but drop count.
2. **`min_edge` for 15m when LTF missing:** Mirror **bitcoin** pattern: higher edge when **no** 15m confirmation (`sol_macro.py` already increases min edge when `ltf_strength == 0` for 15m updown — verify it fires in practice).
3. **`blocked_utc_hours_updown: [18, 22]`:** Re-check on latest data; timezone/session boundaries matter for “bad hours.”
4. **BTC minimum move filters:** `btc_min_move_dollars_15m` / `_5m` — if WR is poor during **low realized BTC move**, consider **raising** thresholds slightly; if too few trades, lower.
5. **Max concurrent SOL positions:** `max_concurrent_positions: 2` — prevents stacking; good for risk, may hide **true** per-signal WR if opportunities are correlated.

### Both

1. **Parity check:** Diff **gates** between `updown_engine` (backtest) and **live** `bitcoin` / `sol_macro` scanners — silent drift is the #1 cause of “backtest good, live meh.”
2. **Exit rules:** `trading.exit_rules` (TP/SL/hold) apply in live; backtest crypto reports may use **different** exit assumptions — confirm in report metadata / engine.

---

## 7) Files to read first (second reviewer)

| Topic | File |
|-------|------|
| BTC live logic | `src/strategies/bitcoin.py` |
| SOL live logic | `src/strategies/sol_macro.py` |
| Sabre (Python) | `src/analysis/btc_price_service.py` → `calc_trend_sabre` |
| SOL/BTC services | `src/analysis/sol_btc_service.py` |
| Crypto backtest engine | `src/backtest/updown_engine.py` |
| Config | `config/settings.yaml` → `strategies.bitcoin`, `strategies.sol_macro` |

---

## 8) Explicit non-goals (unless user requests)

- Replacing **Python** `calc_trend_sabre` with the **dashboard** two-band JS model without a full backtest + sign-off.
- Optimizing **global** win rate by **disabling** strategies that are negative in one session without longer samples.

---

## 9) Final tuning decisions applied (2026-04-02)

These were applied as **config-only** changes to avoid touching Python indicator math:

- **Keep BTC 5m mostly untouched** (recent short-window reports show strongest consistency among BTC windows).
- **Tighten BTC 15m only**:
  - `strategies.bitcoin.min_edge: 0.14 -> 0.15`
  - Added `entry_window_15m_min: 13.0`, `entry_window_15m_max: 14.2` (15m-only quality filter)
- **Tighten weaker SOL 5m path** while preserving SOL 15m baseline:
  - `strategies.sol_macro.min_edge_5m: 0.09 -> 0.11`
  - `strategies.sol_macro.entry_window_5m_min/max: 2.75-3.75 -> 2.9-3.6`
  - `strategies.sol_macro.btc_min_move_dollars_5m: 40.0 -> 55.0`
- **Left unchanged intentionally** (until larger live sample):
  - Python Sabre (`calc_trend_sabre`)
  - SOL 15m `min_edge` / entry band / lag threshold
  - BTC 5m `min_edge_5m`

Assessment: **not all BTC/SOL paths are broken**. Evidence points to a stronger BTC 5m path and weaker/noisier SOL 5m path; tune selectively rather than globally.

---

*End of handoff.*
