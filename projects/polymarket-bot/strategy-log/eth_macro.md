# ETH macro (`eth_macro`)

ETH **Up or Down** — inherits `SolMacroStrategy` (shared entry-window and scan logic); `ETHMacroStrategy` overrides market detection and `ETHUSDT` leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | - | `/api/journal/summary` |
| Win rate | - | same |
| Net PnL | - | same |

## Change Log

### 2026-04-26 — Extreme RSI hard gate for ETH BUY entries

- **What changed:** Added `strategies.eth_macro.rsi_buy_block_above: 80.0` in `config/settings.yaml`; `SolMacroStrategy` now honors optional `rsi_buy_block_above` / `rsi_sell_block_below` hard gates before emitting signals.
- **Why:** Apr 26 paper session showed ETH BUY losses at RSI 84.8 and 83.4 resolving fully against the trade. The prior RSI penalty only reduced estimated probability slightly, so extreme overbought BUYs could still clear `min_edge`.
- **Hypothesis:** Blocking ETH BUY_YES above RSI 80 removes the observed exhaustion entries without changing SOL/HYPE/XRP defaults.
- **Expected outcome:** No ETH BUY_YES entries when RSI is ≥80; lower rate of complete directional misses in overbought ETH windows.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after restart).
- **Status:** `pending`

### 2026-04-21 — UTC blocklist scope-back to Tier A + re-audit cadence

- **What changed:** `strategies.eth_macro.blocked_utc_hours_updown` narrowed from `[1, 15, 17, 20, 23]` to **`[1, 15, 23]`** in `config/settings.yaml`. H17 / H20 removed from the block (downgraded to "watch").
- **Why:** Evidence audit (see `.cursor/plans/block-list-evidence-audit_f364fc11.plan.md`) found ETH's backtest is the strongest base of the three strategies — 807 trades / Mar 1 – Apr 9 2026 ≈ 34 trades/hour. H1 (41% WR, -$42.19), H15 (46.8% WR, -$24.77), and H23 (34.5% WR, -$52.71) are statistically robust and stay blocked. H17 was added on **6 live trades** (0% WR, -$35.90) while backtest was only borderline (-$6.47); 0-for-6 is inside the 95% CI of a 50% WR hour — not statistically separable from noise, and below the `MIN_TRADES=5` confidence bar that `scripts/hourly_heatmap.py` enforces. H20 was 47.4% WR / -$13.14 — borderline and modestly negative. The file's own history (previous `[18, 22]` was a SOL copy-paste that was wrong for ETH because H22 = +$31.18 for ETH) already confirms that small/wrong samples cause real damage; the same principle now applied to H17.
- **Hypothesis:** Tier A blocks keep the protection that matters; removing H17 / H20 lets ETH macro trade ~2 more hours/day and accumulate live evidence in those hours.
- **Expected outcome:** More closed ETH trades/day; within ~2 weeks the per-hour sample on H17 / H20 crosses `MIN_TRADES=5` and we can re-validate on live data instead of paper.
- **Actual outcome:** `pending`.
- **Re-audit cadence:** Weekly `python scripts/hourly_heatmap.py --days 14 --suggest`; re-promote a watched hour to Tier A only on **≥15 trades AND WR < 0.46 AND avg PnL < -$2**.
- **Status:** `pending`

### 2026-04-11 — Entry window auto-alignment (shared SOL path + config)

- **What changed:** Same `_resolve_entry_window_bounds()` behavior as `sol_macro` (class inheritance from `SolMacroStrategy`). `strategies.eth_macro` in `config/settings.yaml` now sets `entry_window_auto_align`, `entry_window_align_scan_interval_sec`, `entry_window_auto_align_max_expand_min`, `entry_window_auto_align_jitter_sec` to match SOL.
- **Why:** ETH up/down uses the identical up/down timing guard; without config parity, ETH could behave differently from SOL despite shared code.
- **Hypothesis:** Parity + cadence-aware widening reduces `outside_entry_window` noise for ETH the same way as SOL.
- **Expected outcome:** ETH eligibility aligned with SOL’s window policy post-deploy.
- **Actual outcome:** `pending` (≥15 closed trades post-deploy or ops evidence).
- **Status:** `pending`

## Review sessions

_(none yet)_

## Lessons learned

_(none yet — add only after data)_
