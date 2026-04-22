# Bitcoin up/down (`bitcoin`)

BTC **Up or Down** markets (15m / 5m) with hierarchical HTF/LTF gates, optional LLM assist, and entry timing windows.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | - | `/api/journal/summary` |
| Win rate | - | same |
| Net PnL | - | same |

## Change Log

### 2026-04-21 — UTC blocklist scope-back to Tier A + re-audit cadence

- **What changed:** `strategies.bitcoin.blocked_utc_hours_updown` narrowed from `[0, 1, 2, 3, 9, 15, 18, 22]` to **`[0, 1, 2]`** in `config/settings.yaml`. H3 / H9 / H15 / H18 / H22 removed from the block (downgraded to "watch"). This is the **first Change Log entry for this blocklist** — prior expansions predate AGENTS.md rule #4 and were not journaled here.
- **Why:** Evidence audit (see `.cursor/plans/block-list-evidence-audit_f364fc11.plan.md`) found that only the extreme backtest losers cleared the project's own `MIN_TRADES=5` / `BAD_WR_THRESHOLD=0.46` / `BAD_EV_THRESHOLD=-$2` bar with high confidence. Backtest was 540 trades / Mar 2026 ≈ 22 trades/hour. H1 (26.9% WR, -$61.68) and H2 (27.3% WR, -$50.28) are statistically robust; H3/H9/H15/H18 sit at 41–44% WR where the 95% CI at n≈22 overlaps 50%; H22 had no sample size cited. The wider list also contributed to zero-trade cycles (live BTC signals dropped at H2/H3 UTC on 2026-04-21).
- **Hypothesis:** A narrower Tier-A block retains the highest-EV protection while letting trades flow in hours that are "bad-leaning but not confirmed bad," so live data can earn the re-block.
- **Expected outcome:** More closed trades per day (~5 previously blocked hours unblocked); live heatmap (`scripts/hourly_heatmap.py`) accumulates enough per-hour samples within ~2 weeks to re-validate or re-promote Tier-B hours on evidence rather than backtest alone.
- **Actual outcome:** `pending` (need ≥15 closed trades in each previously-blocked hour post-deploy).
- **Re-audit cadence:** Weekly `python scripts/hourly_heatmap.py --days 14 --suggest` once live trades resume; re-promote a Tier-B hour to Tier A only if it meets **≥15 trades AND WR < 0.46 AND avg PnL < -$2** in the live window.
- **Status:** `pending`

### 2026-04-11 — Entry window auto-alignment (scan cadence)

- **What changed:** `BitcoinStrategy._resolve_entry_window_bounds()` widens `entry_window_*_min/max` slightly when `entry_window_auto_align: true`, using `entry_window_align_scan_interval_sec` (default 300), `entry_window_auto_align_max_expand_min`, and `entry_window_auto_align_jitter_sec`. Wired in `src/strategies/bitcoin.py`; flags added under `strategies.bitcoin` in `config/settings.yaml`.
- **Why:** Main loop scans every ~5m; narrow “minutes until resolution” bands caused repeated `outside_entry_window` skips even when edge/HTF/LTF would otherwise allow evaluation.
- **Hypothesis:** A small, bounded expansion keeps “early candle” intent while letting at least one scan per interval intersect the valid band (plus clock/request jitter).
- **Expected outcome:** Fewer spurious `outside_entry_window` skips; BTC up/down gets a fair shot at signal generation without materially trading late-window noise (still capped by existing price/edge gates).
- **Actual outcome:** `pending` (need ≥15 closed trades after deploy, or clear before/after ops skip-reason mix).
- **Status:** `pending`

## Review sessions

_(none yet)_

## Lessons learned

_(none yet — add only after data)_
