# SOL vs ETH — updown gate parity audit

**Date:** 2026-05-03  
**Scope:** [`src/strategies/sol_macro.py`](../src/strategies/sol_macro.py) `scan_and_analyze` updown path (`if is_updown:`) vs [`src/strategies/eth_macro.py`](../src/strategies/eth_macro.py) per-market loop.  
**Thesis:** SOL = **BTC–alt lag + alt LTF MACD + correlation**. ETH = **BTC-follow (STF + 1H) + ETH MACD confirm + HTF bias**. Parity means *documented alignment*, not identical code paths.

## Matrix

| Gate / skip (SOL key) | SOL updown behavior | ETH today | Decision | Notes |
|----------------------|---------------------|-----------|----------|--------|
| Per-market 15m skip (`skip_15m_reason`) | Skips all 15m updown when cycle-level gate fails (e.g. unconfirmed LTF) | **Absent** | **Keep absent** | ETH does not use SOL’s 15m confirmation stack; uses BTC-follow + ETH MACD instead. |
| `liquidity` | `market.liquidity < min_liquidity` | **Present** | **Aligned** | Same pattern. |
| `blocked_utc_hour` | Dead zone via `blocked_utc_hours_updown` | **Present** | **Aligned** | same config keys. |
| `missing_end_date` / `no_end_date` | No `end_date` → skip | **Present** | **Aligned** | Naming differs only. |
| `outside_entry_window` | `_resolve_entry_window_bounds` + latency | **Present** | **Aligned** | Same helper from `SolMacroStrategy`. |
| `btc_min_move_dollars` | Min $ BTC move; low-corr bypass | **Present** | **Aligned** | ETH uses `float()` reads; SOL now uses `float()` for parity. |
| `price_too_far_from_even` / `price_too_far` | YES outside ~0.2–0.8 | **Present** | **Aligned** | ETH band 0.20–0.80; SOL uses same idea with different log string. |
| `degraded_correlation` | Optional hard skip when `corr.degraded` | **Absent** | **Consider add** | ETH YAML sets `skip_on_degraded_correlation` but **eth_macro.py does not read it** — **drift risk**; either wire flag + `corr.degraded` or remove YAML noise. |
| `flat_btc_no_lag` | `require_btc_volatility_gate` + min BTC move % | **Absent** | **Keep absent** | Lag/spike framing is SOL-thesis; ETH already has BTC STF impulse. Optional: thin % move gate if you want “flat tape” protection on ETH without importing lag logic. |
| `sell_yes_suppressed_bullish_1h` / `buy_yes_suppressed_bearish_1h` | `enforce_alt_1h_alignment` vs `mtt.h1_trend` | **Present** (`eth_1h_*`) | **Aligned** | Same intent. |
| `rsi_extreme_block` / `rsi_block` | Config RSI hard blocks | **Present** | **Aligned** | |
| `oracle_basis_block` | Chainlink / basis veto | **Present** | **Aligned** | |
| 5m **SOL** 1H histogram gate | MACD 1H direction vs `allowed_side` | **Absent** | **Keep absent** | ETH uses `btc_follow_1h_ok` + `_btc_follow_5m_impulse_score`; not SOL’s alt 1H hist block. |
| `no_btc_catalyst_5m` | `require_btc_catalyst_5m` | **Absent** | **Drift risk** | Key exists in ETH YAML as parity copy; **not used** in eth_macro. Catalyst is partially implicit in BTC impulse. |
| 5m MACD stack (`m5_adj`, `weak_5m_signal`, etc.) | SOL alt 5m MACD tiers + min adj | **Absent** (replaced) | **Intentional** | ETH uses `_btc_follow_5m_impulse_score` + `_eth_5m_macd_score` + `btc_follow_5m_requires_impulse` and `btc_follow_5m_allow_1h_impulse_bypass`. |
| `sell_5m_low_corr` | SELL gated by `sell_5m_min_corr` | **Absent** | **Consider add** | If ETH short 5m misbehaves in low-corr regimes, add symmetric floor using `corr_1h` now populated on signals. |
| `low_corr_suppressed` (5m) | Hard floor or damping | **Absent** | **Keep absent** | ETH relies on `btc_min_move` low-corr bypass; no SOL-style corr damping on prob. Revisit if needed. |
| 15m `iql_15m_reject` | IQL histogram floor | **Absent** | **Keep absent** | ETH uses `eth_follow_15m_min_adj` + `_eth_15m_follow_score`; different instrument scale (see `iql_15m_hist_floor` in YAML — SOL path). |
| 15m SOL 1H histogram block | Same as 5m hist gate | **Absent** | **Keep absent** | |
| `no_btc_catalyst_15m_unconfirmed` | Catalyst when LTF unconfirmed | **Absent** | **Drift risk** | YAML-only echo; not wired in ETH. |
| 15m lag / macro_leg / center price | `_passes_15m_iql` after blocks; lag boosts; `center_price_band` | **Partial / absent** | **Intentional** | ETH 15m path is BTC 15m impulse + ETH 15m score + optional `btc_follow_stf_bypass_if_1h_ok`. No SOL `macro_leg` journal tie-in on ETH loop. |
| `nonpositive_edge` / `edge_below_min` / AI marginal chain | Shared structure | **Present** | **Aligned** | ETH uses `continue` after `ai_window_closed` (no double bump with `edge_below_min`). |
| Centered YES + catalyst | `center_price_band` rules | **Absent** | **Consider add** | Useful generic safety; not BTC-follow-specific — optional port behind `eth_macro` flag. |
| `entry_price_band` / `max_edge_updown` / Kelly / `size_too_small` | Sizing and caps | **Present** | **Aligned** | ETH uses `entry_price_min/max` from config. |

## Config drift (ETH YAML vs `eth_macro.py`)

Keys that appear under `eth_macro` in [`config/settings.yaml`](../config/settings.yaml) but are **not referenced** in [`eth_macro.py`](../src/strategies/eth_macro.py) (SOL parity / future use — verify before relying):

- `skip_on_degraded_correlation`, `degraded_correlation_size_multiplier`, `degraded_bearish_est_up`
- `require_btc_volatility_gate`, `min_btc_move_pct_*_for_lag_entries`
- `require_btc_catalyst_5m`, `require_btc_catalyst_15m_when_unconfirmed`
- `min_positive_m5_adj_5m` family (SOL 5m adj thresholds)
- Potentially others copied for “grep parity” — search `self.config.get` in `eth_macro.py` when tuning.

**Recommendation:** For each unused key, either **wire** it with explicit ETH semantics or **delete / comment** in YAML to avoid false confidence.

## Follow-up (code, optional)

Only after product sign-off:

1. **Degraded correlation:** `if self.skip_on_degraded_correlation and corr.degraded: _bump_skip(...)` in ETH loop (mirror SOL).
2. **SELL 5m min corr:** Port `sell_5m_min_corr` check for `SELL_YES` on 5m only.
3. **Center price + catalyst:** Port SOL’s centered-YES gate if live data supports it.

---

*Artifact produced as part of ETH macro follow-up plan execution.*
