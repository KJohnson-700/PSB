# BNB macro (`bnb_macro`)

BNB **Up or Down** — inherits shared `SolMacroStrategy` signal path with BNB market detection and `BNBUSDT` spot leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 28 | Paper `test_20260524_060424` — [docs/session_reports/eth_hype_bnb_session_audit_20260524_060424.md](/Users/mainfolder/Documents/psb-main%201/docs/session_reports/eth_hype_bnb_session_audit_20260524_060424.md) |
| Win rate | 32.1% | same |
| Net PnL | -$0.79 | same |

## Change Log

### 2026-05-25 — 1h exploration admission before restart

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), BNB 1h lane-policy floors were lowered to `up.min_edge: 0.08` and `down.min_edge: 0.075`, with 1h `entry_price_max` widened to `0.58`. Existing `0.3x` 1h calibration sizing remains.
- **Why:** BNB has zero closed 1h calibration trades in `data/calibration/trades.jsonl`; the lane is starved, not proven bad.
- **Hypothesis:** BNB 1h should begin producing small-notional samples, letting us evaluate whether the 1h horizon improves prediction quality versus noisy 5m/15m entries.
- **Expected outcome:** Next session should include BNB 1h entries when markets exist, with lane IDs and realized PnL available for review after ≥15 closed samples.
- **Actual outcome:** `pending` (need ≥15 closed BNB 1h trades after restart).
- **Status:** `pending`

### 2026-05-25 — Cap live lane-calibration alpha at identity

- **What changed:** In [src/analysis/lane_calibration.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_calibration.py), `ALPHA_CLAMP_HI` changed from `2.50` to `1.00`. Raw `alpha_ewma` telemetry can still exceed `1.0`, but live calibration can no longer amplify BNB probabilities away from 50/50; sub-1 shrinkage remains active.
- **Why:** Session attribution called out BNB high-alpha trades as loss contributors. This removes calibration-driven amplification while preserving shrinkage for historically overpredicted lanes.
- **Hypothesis:** BNB should see lower loss concentration in high-alpha buckets during the next live/non-shadow session.
- **Expected outcome:** Next live/non-shadow session should show no effective `alpha_used > 1.0` BNB entries.
- **Actual outcome:** `pending` (need ≥15 closed `bnb_macro` trades after this change).
- **Status:** `pending`

### 2026-05-24 — Restore BNB admission coverage after basis starvation

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), kept BNB edge floors unchanged, widened oracle basis validation from `18/22` bps to `30/40` bps, and set 15m/1h lane `size_multiplier: 0.3` to match the existing 5m calibration sizing.
- **Why:** Session `test_20260524_060424` showed BNB scanned heavily but converted poorly: `28` fills across `337` candidate events (`8.3%` entry rate). Rejections were dominated by `lane_min_edge` (`225`) and `oracle_basis_block` (`54`); blocked BNB oracle basis was tightly clustered around `24.4–29.1` bps, just above the prior cap.
- **Hypothesis:** BNB should stop acting mostly shadow-only and produce enough 5m/15m/1h calibration trades to judge the lane, while existing size multipliers and risk caps limit downside.
- **Expected outcome:** BNB entry rate rises materially and 1h candidates can enter when edge is positive instead of being dead on arrival.
- **Actual outcome:** `pending` (need ≥15 closed `bnb_macro` trades after this change).
- **Status:** `pending`

## Review sessions

### 2026-05-24 — Session `test_20260524_060424`

- BNB was near breakeven overall (`28` trades, `-$0.79`) but coverage was broken relative to scan volume. The problem to solve first is admission/fill coverage, not PnL tuning.

## Lessons learned

- `pending`
