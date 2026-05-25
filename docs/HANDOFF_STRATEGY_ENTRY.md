# Handoff: Strategy Entry Spec

**Audience:** Senior engineer
**Purpose:** Ensure the Strategy Entry Specification is implemented consistently in the live code path.
**Spec reference:** `docs/STRATEGY_ENTRY_SPEC.md`

> The in-repo backtest engines were removed 2026-05-24 (CLAUDE.md: they didn't faithfully replay live behavior). Validation is now via the ghost log + dashboard Ghost Lab. Backtest-alignment sections of this doc were dropped accordingly.

---

## 1. Entry spec (reminder)

- **Downside:** No hard floor on entry price. Enter when edge exceeds the minimum threshold even if price is below any “target” band (e.g. strong mispricings at 27¢ or 30¢ are valid).
- **Max entry (optional):** A cap (e.g. 70¢) may be enforced so we never pay above it; this is a risk/return preference only.
- **Chasing:** Do not chase random small dips; require edge above threshold and, where applicable, AI confidence.
- **Strong minor mispricings:** Allow entries when edge is sufficient; no target window or floor should block low prices when edge is there.
- **Position size:** Governed only by Kelly Criterion and existing risk limits (no extra price-based sizing rules).

**Short-window crypto up/down** (`bitcoin`, `sol_macro`, `eth_macro`, `xrp_macro`, `hype_macro`) deliberately use tighter controls (`entry_price_min`/`max`, mid-window “far from even” skips). See scope note in `docs/STRATEGY_ENTRY_SPEC.md`.

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
- [ ] **Validation:** Ghost log + Ghost Lab tab WR for affected lanes recorded before/after the change.
- [ ] **Docs:** `STRATEGY_ENTRY_SPEC.md` and this handoff are updated if `max_entry_price` or any new rule is added.
