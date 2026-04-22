# Handoff: Strategy Entry Spec & Backtest Alignment

**Audience:** Senior engineer  
**Purpose:** Ensure the Strategy Entry Specification is implemented in the right places (live and backtest) and that backtesting faithfully reflects the same entry/sizing logic.  
**Spec reference:** `docs/STRATEGY_ENTRY_SPEC.md`

---

## 1. Entry spec (reminder)

- **Downside:** No hard floor on entry price. Enter when edge exceeds the minimum threshold even if price is below any “target” band (e.g. strong mispricings at 27¢ or 30¢ are valid).
- **Max entry (optional):** A cap (e.g. 70¢) may be enforced so we never pay above it; this is a risk/return preference only.
- **Chasing:** Do not chase random small dips; require edge above threshold and, where applicable, AI confidence.
- **Strong minor mispricings:** Allow entries when edge is sufficient; no target window or floor should block low prices when edge is there.
- **Position size:** Governed only by Kelly Criterion and existing risk limits (no extra price-based sizing rules).

---

## 2. Live implementation — where to enforce

Verify and, if needed, implement the spec in these places:

| Location | What to check / implement |
|----------|---------------------------|
| **`src/strategies/arbitrage.py`** | Entry is driven only by `effective_edge_yes/no > min_edge` and AI recommendation/confidence. **No** filter on `market.yes_price` or `market.no_price` band (no floor, no target window). Optionally add a **max_entry_price** (e.g. from config): skip or cap order price if it would exceed 70¢ (or configured value). |
| **`src/strategies/fade.py`** | Entry is driven by consensus threshold, `ipg_min`, and AI confidence. **No** filter on price level (e.g. no “only trade when YES in [0.55, 0.65]”). Optionally enforce **max_entry_price** for the side being bought. Ensure “don’t chase small dips” is satisfied by existing edge/confidence thresholds (no new ad‑hoc filters that would block strong mispricings). |
| **`src/strategies/consensus.py`** | Consensus is alert-only; no auto execution. If any suggested size or “recommended entry” is ever added, it must not impose a price floor—only edge/liquidity/expiration logic. |
| **`src/analysis/math_utils.py` (PositionSizer)** | Sizing uses Kelly + `min_position` / `max_position` / `max_position_pct` only. **No** extra logic that reduces or blocks size based on entry price band (no “size = 0 if price < 0.55”). |
| **`src/execution/clob_client.py` (RiskManager)** | `evaluate_entry` and `check_strategy_risk` use edge and exposure/size limits only. **No** price-floor or target-window checks here; optional **max_entry_price** can be applied at strategy or execution layer (e.g. reject or cap order if limit price > config). |
| **`src/main.py`** | Execution flow uses signals from strategies and risk checks. Ensure no extra filtering by price band before calling `place_order`. If max_entry is implemented, it can live in strategy (signal generation) or here (reject/cap before send). |

**Acceptance (live):**

- No code path rejects or skips a trade because entry price is “below” a target (e.g. 55¢) when edge already exceeds the minimum.
- Optional max entry (e.g. 70¢) is respected if configured; no target window used as a requirement.
- Position size is determined only by Kelly and config risk limits.

---

## 3. Backtesting — align with live behavior

Backtests must use the **same** entry and sizing rules as live so that “pass in backtest” implies “would behave the same live.”

| Location | What to check / implement |
|----------|---------------------------|
| **`src/backtest/engine.py`** | Uses `ArbitrageStrategy` / `FadeStrategy` with `BacktestAIAgent` and `PositionSizer` built from **the same config** as live (e.g. `config/settings.yaml`). Ensure: (1) no extra price-based filter (e.g. “skip if price < 0.55”) in the engine; (2) strategy instances receive the same `min_edge`, `ai_confidence_threshold`, `ipg_min`, etc.; (3) if **max_entry_price** is added for live, the backtest engine or strategy must apply it when generating/simulating signals (e.g. do not add a fill when signal price would exceed max_entry). |
| **`backtesting/adapters.py`** (Nautilus adapter) | Uses `poly_bot.arbitrage_strategy` / `poly_bot.fade_strategy` and their `scan_and_analyze`. So long as PolyBot is constructed with the same config and no extra filtering in main, adapters inherit correct behavior. Verify: no adapter-level filter that drops signals by price band; if max_entry is enforced in strategy or main, adapter will receive already-capped signals. |
| **Config used by backtest** | Scripts that run backtests (e.g. `scripts/run_backtest_rigorous.py`, `scripts/run_backtest_multi.py`) should load the **same** `config/settings.yaml` (or equivalent) so that `min_edge`, confidence thresholds, and any future `max_entry_price` are identical between live and backtest. |

**Acceptance (backtest):**

- Entry and sizing in backtest match the spec: no hard floor, optional max entry only, edge/confidence thresholds only for “don’t chase,” Kelly + risk limits for size.
- No backtest-only filter that would allow a trade in backtest but block it in live (or vice versa) based on price band.
- Config used for backtest is the same as (or explicitly overridden from) the live strategy config.

---

## 4. Optional config addition

If **max_entry_price** is implemented, add to `config/settings.yaml` under `strategies` (e.g. under `arbitrage` and/or `fade`), for example:

```yaml
# Optional: never pay more than this (e.g. 70¢) so payoff is "30% or better"
max_entry_price: 0.70   # set to null or omit to disable
```

Document in `STRATEGY_ENTRY_SPEC.md` that this is the only price-based entry cap; there is no target window or floor.

---

## 5. Summary checklist for senior engineer

- [ ] **Live:** Arbitrage and fade (and consensus, if it ever suggests size) have **no hard floor** and **no target window** on entry price; entry only requires edge (and confidence where applicable) above threshold.
- [ ] **Live:** Optional **max_entry_price** is respected if present in config; no other price-based entry rule.
- [ ] **Live:** Position size is governed only by Kelly and risk limits (no price-based sizing).
- [ ] **Backtest:** `src/backtest/engine.py` and any Nautilus adapters use the same entry/sizing logic and config as live; no backtest-only price-floor or target-window logic.
- [ ] **Backtest:** Config source for backtest runs matches live (same `settings.yaml` or explicit override).
- [ ] **Docs:** `STRATEGY_ENTRY_SPEC.md` and this handoff are updated if `max_entry_price` or any new rule is added.
