# DOGE macro (`doge_macro`)

DOGE **Up or Down** — inherits shared `SolMacroStrategy` signal path with DOGE market detection and `DOGEUSDT` spot leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed 1h trades | 1 | `data/calibration/trades.jsonl` |
| 1h Win rate | 0.0% | same |
| 1h Net PnL | -$6.19 | same |

## Change Log

### 2026-05-26 — Resolver metadata parity for shared macro signals

- **What changed:** Added BTC-compatible resolver metadata to the shared macro signal path used by DOGE: `conflict_type`, `resolver_path`, `htf_side`, `quant_side`, and `momentum_side`, with journal and position persistence.
- **Why:** DOGE had HTF and oracle metadata, but direction-resolution details were not first-class like BTC.
- **Hypothesis:** Future ghost/trade reviews can separate HTF-aligned, quant-disagree, and momentum-disagree DOGE entries without changing entry behavior.
- **Expected outcome:** New DOGE entries should include resolver metadata in journal extras and `entry_signal`.
- **Actual outcome:** `pending` (need post-change entries to verify field coverage).
- **Status:** `pending`

### 2026-05-26 — Route DOGE through up/down exits and hold winners

- **What changed:** Added `doge_macro` to the shared crypto up/down exit strategy set and enabled `trading.exit_rules.updown_hold_winners_to_resolution`.
- **Why:** DOGE was missing from the specialized up/down exit path, so it could bypass the stop/window/resolution behavior used by BTC/SOL/ETH/HYPE/XRP.
- **Hypothesis:** DOGE exits should now use up/down-specific stop/window semantics, and correct winners should settle instead of being clipped by early TP.
- **Expected outcome:** DOGE exits should include up/down-specific reasons and more `RESOLVED:* (real)` winners when correct.
- **Actual outcome:** `pending` (need >=15 closed DOGE trades after restart).
- **Status:** `pending`

### 2026-05-26 — Remove DOGE 1h exploration size haircut

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), removed the added `0.3x` lane `size_multiplier` from DOGE 1h up/down policies while keeping the existing 5m calibration sizing unchanged.
- **Why:** DOGE’s avg loss worsened and the post-May-22 exploration layer added 1h size haircuts before the lane had a useful closed-trade sample. The rollback keeps the proven/current 5m posture and removes only the later 1h sizing experiment.
- **Hypothesis:** DOGE 1h entries should reflect Kelly/risk sizing directly instead of an extra exploration haircut, making per-trade economics easier to compare with baseline cohorts.
- **Expected outcome:** Next DOGE sample should show no 1h `lane_size=0.30x` tag from lane policy.
- **Actual outcome:** `pending` (need >=15 closed DOGE trades after restart).
- **Status:** `pending`

### 2026-05-26 — BUY_YES recovery tweak after rollback

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), changed DOGE `alt_momentum_confirm.buy_yes` to `15m` only while keeping `buy_no` confirmation on `5m`, `15m`, and `1h`.
- **Why:** DOGE still needs quality control, but all-window BUY_YES confirmation would likely starve the side we are trying to recover across the shared alt path.
- **Hypothesis:** DOGE BUY_YES can collect small confirmed-or-clean samples, while BUY_NO remains gated against unconfirmed downside defaults.
- **Expected outcome:** Next paper run should show lower downside flood risk with nonzero DOGE BUY_YES opportunity.
- **Actual outcome:** `pending` (need ≥15 closed DOGE trades after restart on this config).
- **Status:** `pending`

### 2026-05-26 — Pre-restart rollback of post-May-22 momentum guard regression

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), added active `doge_macro.alt_momentum_confirm` blocking for `BUY_YES` and `BUY_NO` on `5m`, `15m`, and `1h`; restored DOGE 1h edge overrides to `up: 0.09` and `down: 0.08`.
- **Why:** DOGE volume rose while remaining a loss contributor. The post-May-22 guard regression let default-side entries through without DOGE-native confirmation, and the 1h exploration loosen increased exposure before the lane had proof.
- **Hypothesis:** DOGE should trade less often and only when its own MACD confirms the selected side, reducing default bearish/downside bleed.
- **Expected outcome:** Next paper run should show lower DOGE fill count and fewer `bearish_dip_default` DOGE fills unless confirmed by DOGE tape.
- **Actual outcome:** `pending` (need ≥15 closed DOGE trades after restart on this config).
- **Status:** `pending`

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
