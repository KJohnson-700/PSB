# DOGE macro (`doge_macro`)

DOGE **Up or Down** — inherits shared `SolMacroStrategy` signal path with DOGE market detection and `DOGEUSDT` spot leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed 1h trades | 1 | `data/calibration/trades.jsonl` |
| 1h Win rate | 0.0% | same |
| 1h Net PnL | -$6.19 | same |

## Change Log

### 2026-05-25 — 1h exploration admission before restart

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), DOGE 1h lane-policy floors were lowered to `up.min_edge: 0.08` and `down.min_edge: 0.075`, 1h `entry_price_max` widened to `0.58`, and both 1h sides now use `size_multiplier: 0.3`.
- **Why:** DOGE has only one closed 1h calibration trade, so the lane is under-sampled. This is an exploration setting, not a claim that DOGE 1h is good.
- **Hypothesis:** DOGE 1h should produce enough small-notional samples to compare against its currently active 5m/15m lanes.
- **Expected outcome:** Next session should include DOGE 1h entries when markets exist, with enough closed samples to review lane family, side, and regime attribution.
- **Actual outcome:** `pending` (need >=15 closed DOGE 1h trades after restart).
- **Status:** `pending`

## Review sessions

- `pending`

## Lessons learned

- `pending`
