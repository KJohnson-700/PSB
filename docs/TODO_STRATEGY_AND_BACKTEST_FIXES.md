# To-Do: Strategy & Backtest Fixes

Detailed action items from research (PANews 112k/90k wallet analysis, GitHub bots) and user feedback. Prioritized for implementation.

---

## 1. Odds range (data says 0.2–0.4 is best)

**Issue:** On-chain data shows true alpha is concentrated in **0.2–0.4** (cognitive arbitrage zone). Avoid &lt;0.2 (lottery trap) and &gt;0.8 (certainty trap).

**Current state:**
- Fade: `consensus_threshold: 0.95` → we enter when YES≥0.95 or NO≥0.95. That means buying NO at ~0.05 or YES at ~0.05 = **lottery zone**.
- Arbitrage: no explicit entry-price filter; AI edge can trigger at any price.

**Fixes:**
- [x] **Fade:** Add `entry_price_min` and `entry_price_max` (e.g. 0.15–0.45). Only fade when the side we're *buying* is in that range. If consensus is YES at 0.95, we buy NO at 0.05 → block (lottery). Consider fading when consensus is 0.80–0.90 (buy NO at 0.10–0.20) instead.
- [x] **Fade:** Add config: `consensus_threshold_lower: 0.80`, `consensus_threshold_upper: 0.90` so we fade "overconfident but not extreme" instead of 0.95+.
- [x] **Arbitrage:** Add `entry_price_min: 0.20`, `entry_price_max: 0.40` so we only trade when market price is in the sweet spot.
- [ ] **Both:** Document the 0.2–0.4 rule in `docs/STRATEGY_ENTRY_SPEC.md`.

---

## 2. Category specialization (verify pipeline)

**Issue:** User specifically set up category-specific strategies in `config/backtest_market_categories.yaml`. Need to verify the pipeline correctly applies them and that we're not inadvertently mixing or ignoring categories.

**Current state:**
- `backtest_market_categories.yaml`: `arbitrage_*`, `fade_*`, `both_*` categories with strategy tags.
- `get_slugs_for_strategy()` loads from `backtest_markets.yaml` by strategy key (`arbitrage`, `fade`, `both`).
- Build script assigns slugs to categories by keywords; each category has a `strategy` field.

**Fixes:**
- [x] **Audit:** Trace `build_backtest_market_list.py` → `backtest_markets.yaml` → `get_slugs_for_strategy()`. Confirmed: fade gets `fade_*` + `both_*`, arbitrage gets `arbitrage_*` + `both_*`.
- [x] **Audit:** Ensure `both` categories are intentionally shared (e.g. elections) — confirmed.
- [x] **CLI:** Add `--strategy-categories-only` to run each strategy on *only* its dedicated categories (exclude `both`) for a purity test.
- [x] **Logging:** Log which categories contributed slugs for each strategy in `run_backtest_rigorous.py`.

---

## 3. Early exit formula (we don't have to hold to settlement)

**Issue:** Top wallets hold 18–72 hours on average; many trade price moves, not outcomes. We currently hold to settlement only.

**Current state:**
- `BacktestEngine.run()`: positions are held until `_settle_positions()` at resolution. No exit logic.

**Fixes:**
- [x] **Define exit rules:** (a) time-based: max hold 18–72 hrs (configurable); (b) profit target: exit when unrealized gain ≥ 20%; (c) stop-loss: exit when unrealized loss ≥ 15%.
- [x] **Engine:** Add `exit_strategy` config: `hold_to_settlement` | `time_and_target`. Implemented in engine loop: each bar, check open positions for exit conditions.
- [x] **Engine:** Track `(action, size, fill_price, entry_ts)` per position. On exit, compute PnL from current bar price and close position.
- [x] **Config:** Added to `settings.yaml` under backtest: `max_hold_hours`, `take_profit_pct`, `stop_loss_pct`.
- [x] **Backtest:** Default resolution is 1h; sufficient bar frequency for intraday exits.

---

## 4. Backtest engine: early exits, fees, slippage

**Issue:** Engine must model early exits, fees, and slippage more accurately so rule-based logic is trustworthy before layering AI.

**Current state:**
- `_simulate_fill()`: uses spread/2 or slippage_bps; applies fee_bps. No L2 book in most runs (spread=0.02 default).
- No early exit; settlement only at resolution.
- Stress scenarios: `slippage_mult`, `fee_bps_override` exist but execution path may not be fully realistic.

**Fixes:**
- [x] **Early exits:** Implemented per §3. Exit fills use same `_simulate_fill()` (slippage on exit).
- [x] **Fees:** Fee applied on both entry and exit (including settlement). Configurable via `fee_bps`.
- [x] **Slippage:** Size-dependent slippage added: `sqrt(size / 50)` scaling, capped at 2x. Larger sizes face proportionally more slippage.
- [x] **L2 simulation:** When `books_df` is available, engine uses `_weighted_fill()` for VWAP fill. Documented that default run uses simplified spread model.
- [x] **Validation:** Unit tests added in `tests/test_backtest_engine.py` covering fill simulation, settlement, fees, and ruin cap.

---

## 5. Rule-based logic (dial it in for AI to build on)

**Issue:** If the rule-based backtest is broken or unrealistic, the AI has no solid foundation. Rule-based must be correct first.

**Fixes:**
- [x] **BacktestAIAgent:** Documented in `src/backtest/backtest_ai.py` and `docs/BACKTEST.md` — deterministic proxy with fixed 0.72 confidence, mean-reversion formulas.
- [x] **Strategy parity:** Fade and Arbitrage in backtest use same thresholds as live (`ipg_min`, `min_edge`, `ai_confidence_threshold`, `entry_price_min/max` all match).
- [ ] **Sanity checks:** Run backtest on synthetic data: (a) perfect edge → positive PnL; (b) random signals → near-zero or negative PnL. If (a) fails, engine is wrong.
- [x] **Reduce variables:** Rule-based fade and arbitrage working with fixed rules. Fade: "if consensus in [0.80,0.95] and opposite side in [0.15,0.45], enter".

---

## 6. Add weather strategy (backup / alternative)

**Strategy:** Weather markets — compare NOAA/Open-Meteo forecast probability vs Polymarket price. If gap &gt; 15¢, enter.

**Why:** Documented results: $1K → $24K (London weather since Apr 2025); $65K across NYC, London, Seoul. Official forecasts are highly accurate 1–2 days out; human-set prices often wrong.

**Fixes:**
- [x] **Add to strategy list:** Create `src/strategies/weather.py`.
- [x] **Data sources:** Integrated Open-Meteo API (free) for forecast probability.
- [x] **Entry rule:** `gap = |forecast_prob - market_price|`; if `gap >= 0.15`, enter on the forecast side.
- [x] **Categories:** Added `weather_*` to `backtest_market_categories.yaml` (temperature, precipitation, city).
- [ ] **Market list:** Ensure `build_backtest_market_list.py` can discover weather slugs.
- [x] **Backtest:** Added proxy mode: uses resolution outcome as "perfect forecast" (YES won → forecast=0.99, NO won → forecast=0.01). Valid for testing rule-based entry logic.
- [x] **Config:** Added `strategies.weather` section: `enabled`, `gap_min` (0.15), `forecast_horizon_days` (2).

---

## Summary checklist

| # | Area | Priority | Status |
|---|------|----------|--------|
| 1 | Odds range 0.2–0.4 | High | **Done** — Fade [0.15,0.45], Arb [0.20,0.40] |
| 2 | Category pipeline audit | High | **Done** — logging added, `--strategy-categories-only` CLI flag |
| 3 | Early exit formula | High | **Done** — `time_and_target` with TP/SL/time exits |
| 4 | Engine: exits, fees, slippage | High | **Done** — fees on entry+exit, size-dependent slippage, unit tests |
| 5 | Rule-based dial-in | High | **Mostly done** — parity verified, docs added. Sanity checks still pending. |
| 6 | Weather strategy | Medium | **Done** — module added, proxy backtest mode, config in settings.yaml |

---

## References

- PANews: "Deconstructing 112,000 Polymarket addresses" (5 patterns)
- Hubble AI: "Who is the real god on Polymarket?" (90k addresses, 4 rules)
- GitHub: qntrade/polymarket-arbitrage-bot, TrendTechVista/polymarket-ai-trading-bot
- Weather: $1K→$24K London, $65K NYC/London/Seoul (documented)
