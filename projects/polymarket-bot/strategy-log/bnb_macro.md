# BNB macro (`bnb_macro`)

BNB **Up or Down** — inherits shared `SolMacroStrategy` signal path with BNB market detection and `BNBUSDT` spot leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 28 | Paper `test_20260524_060424` — [docs/session_reports/eth_hype_bnb_session_audit_20260524_060424.md](/Users/mainfolder/Documents/psb-main%201/docs/session_reports/eth_hype_bnb_session_audit_20260524_060424.md) |
| Win rate | 32.1% | same |
| Net PnL | -$0.79 | same |

## Change Log

### 2026-05-26 — Resolver metadata parity for shared macro signals

- **What changed:** Added BTC-compatible resolver metadata to the shared macro signal path used by BNB: `conflict_type`, `resolver_path`, `htf_side`, `quant_side`, and `momentum_side`, with journal and position persistence.
- **Why:** BNB had HTF and oracle metadata, but direction-resolution details were not first-class like BTC.
- **Hypothesis:** Future ghost/trade reviews can separate HTF-aligned, quant-disagree, and momentum-disagree BNB entries without changing entry behavior.
- **Expected outcome:** New BNB entries should include resolver metadata in journal extras and `entry_signal`.
- **Actual outcome:** `pending` (need post-change entries to verify field coverage).
- **Status:** `pending`

### 2026-05-26 — Route BNB through up/down exits and hold winners

- **What changed:** Added `bnb_macro` to the shared crypto up/down exit strategy set and enabled `trading.exit_rules.updown_hold_winners_to_resolution`.
- **Why:** BNB was missing from the specialized up/down exit path, so it could bypass the stop/window/resolution behavior used by BTC/SOL/ETH/HYPE/XRP.
- **Hypothesis:** BNB exits should now use up/down-specific stop/window semantics, and correct winners should settle instead of being clipped by early TP.
- **Expected outcome:** BNB exits should include up/down-specific reasons and more `RESOLVED:* (real)` winners when correct.
- **Actual outcome:** `pending` (need >=15 closed BNB trades after restart).
- **Status:** `pending`

### 2026-05-26 — Remove BNB 15m/1h exploration size haircut

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), removed the added `0.3x` lane `size_multiplier` from BNB 15m and 1h up/down policies while keeping the existing 5m calibration sizing unchanged.
- **Why:** BNB was the only strategy whose win/loss ratio improved, so this is not a defensive tighten. It removes the broad post-baseline sizing experiment so BNB economics remain comparable with the May 22-style Kelly posture.
- **Hypothesis:** BNB 15m/1h avg winner and avg loser magnitudes should become interpretable without an extra lane-policy haircut.
- **Expected outcome:** Next BNB sample should show no 15m/1h `lane_size=0.30x` tag from lane policy.
- **Actual outcome:** `pending` (need >=15 closed BNB trades after restart).
- **Status:** `pending`

### 2026-05-26 — BUY_YES recovery tweak and missing BNB settings

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), changed BNB `alt_momentum_confirm.buy_yes` to `15m` only while keeping `buy_no` confirmation on `5m`, `15m`, and `1h`. Added/activated BNB BUY_YES settings by keeping `oracle_max_basis_bps_15m_buy_yes` / `oracle_basis_relax_max_bps_15m_buy_yes`, adding `entry_price_max_15m_yes_side`, and widening the BNB 15m up entry price cap to `0.57`. The shared oracle validator now reads those side/window settings.
- **Why:** BNB had partial BUY_YES settings in YAML, but the live validator was not using side/window oracle overrides. The all-window BUY_YES confirm rollback also risked suppressing the side we need to recover.
- **Hypothesis:** BNB should admit measured BUY_YES samples on cleaner setups without reopening unconfirmed BUY_NO downside flow.
- **Expected outcome:** Next paper run should show BNB BUY_YES eligibility/fills when 15m price/oracle conditions are acceptable, with BUY_NO still momentum-confirmed.
- **Actual outcome:** `pending` (need ≥15 closed BNB trades after restart on this config).
- **Status:** `pending`

### 2026-05-26 — Pre-restart rollback of post-May-22 momentum guard regression

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), added active `bnb_macro.alt_momentum_confirm` blocking for `BUY_YES` and `BUY_NO` on `5m`, `15m`, and `1h`; restored BNB 1h edge overrides to `up: 0.09` and `down: 0.08`.
- **Why:** BNB was less bad than before but still not proven, and the post-May-22 guard regression exposed it to unconfirmed default-side fills. This removes the premature 1h exploration loosen before restart.
- **Hypothesis:** BNB throughput should fall until BNB-native MACD confirms side selection, improving quality of any remaining fills.
- **Expected outcome:** Next paper run should show fewer BNB unconfirmed default-side entries and clearer momentum-confirm skip attribution.
- **Actual outcome:** `pending` (need ≥15 closed BNB trades after restart on this config).
- **Status:** `pending`

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
