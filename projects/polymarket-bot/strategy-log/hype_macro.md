# HYPE macro (`hype_macro`)

HYPE **Up or Down** — inherits shared `SolMacroStrategy` signal path with Hyperliquid HYPE market detection and `HYPEUSDT` spot leg from Hyperliquid.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 15 | Paper `test_20260504_034719` — [`docs/session_reports/session_parse_test_20260504_034719.json`](docs/session_reports/session_parse_test_20260504_034719.json) |
| Win rate | 86.7% | same |
| Net PnL | +$15.73 | same |
| Entries with `lag_magnitude` missing (joined ENTRY `extra`) | 9 / 15 (60%) | same |

## Change Log

### 2026-05-26 — Timeframe-scoped entry control config

- **What changed:** Moved HYPE entry-control thresholds and windows from legacy flat timeframe keys (`min_edge_5m`, `entry_window_*`) into canonical `defaults` / `by_tf` config, and routed shared macro entry policy reads through the timeframe resolver.
- **Why:** Static config audit showed the same 5m/15m values duplicated in flat keys and lane policy overrides, making it unclear which tuning surface was authoritative.
- **Hypothesis:** HYPE 5m/15m/1h tuning changes should stay scoped to their `by_tf` cell with no cross-timeframe bleed.
- **Expected outcome:** Startup logs should show HYPE `by_tf` overrides; focused tests should preserve the same effective min-edge/window values.
- **Actual outcome:** `pending` (config migration only; need >=15 closed HYPE trades before performance judgment).
- **Status:** `pending`

### 2026-05-26 — Resolver metadata parity for shared macro signals

- **What changed:** Added BTC-compatible resolver metadata to the shared macro signal path used by HYPE: `conflict_type`, `resolver_path`, `htf_side`, `quant_side`, and `momentum_side`, with journal and position persistence.
- **Why:** HYPE had HTF and oracle metadata, but direction-resolution details were not first-class like BTC.
- **Hypothesis:** Future ghost/trade reviews can separate HTF-aligned, quant-disagree, and momentum-disagree HYPE entries without changing entry behavior.
- **Expected outcome:** New HYPE entries should include resolver metadata in journal extras and `entry_signal`.
- **Actual outcome:** `pending` (need post-change entries to verify field coverage).
- **Status:** `pending`

### 2026-05-26 — Hold up/down winners to resolution

- **What changed:** Enabled `trading.exit_rules.updown_hold_winners_to_resolution` and suppressed up/down `take_profit` exits while that flag is true.
- **Why:** HYPE size grew while W/L ratio collapsed, so sizing cannot explain the damage. This targets premature winner clipping directly.
- **Hypothesis:** Correct HYPE trades should realize closer to binary-resolution payoff when held through settlement.
- **Expected outcome:** Fewer `take_profit` exits, more `RESOLVED:* (real)` exits, and higher avg-win dollars.
- **Actual outcome:** `pending` (need >=15 closed HYPE trades after restart).
- **Status:** `pending`

### 2026-05-26 — Remove post-May-22 HYPE lane-size haircut

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), removed the `0.3x` lane `size_multiplier` from HYPE 5m, 15m, and 1h up/down entry policies.
- **Why:** HYPE’s current avg win/loss ratio inverted despite the same NO-heavy trade mix. The blanket 0.3x exploration sizing was a direct post-baseline change and was no longer justified by the reported per-trade economics.
- **Hypothesis:** HYPE winners should stop being mechanically capped by the lane haircut; remaining damage should then be attributable to entry quality or stop behavior rather than sizing.
- **Expected outcome:** Next HYPE paper sample should show no `lane_size=0.30x` from lane policy and should recover avg winner dollars if the signal quality is comparable.
- **Actual outcome:** `pending` (need >=15 closed HYPE trades after restart).
- **Status:** `pending`

### 2026-05-26 — BUY_YES recovery tweak after rollback

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), changed HYPE `alt_momentum_confirm.buy_yes` to `15m` only while keeping `buy_no` confirmation on `5m`, `15m`, and `1h`.
- **Why:** The pre-restart rollback protected against unconfirmed downside floods, but applying all-window confirmation to BUY_YES risked recreating the BUY_YES starvation problem.
- **Hypothesis:** HYPE can still collect BUY_YES samples on cleaner 5m/1h setups while preserving confirmation on 15m.
- **Expected outcome:** Next paper run should show nonzero HYPE BUY_YES eligibility without a broad increase in BUY_NO defaults.
- **Actual outcome:** `pending` (need ≥15 closed HYPE trades after restart on this config).
- **Status:** `pending`

### 2026-05-26 — Pre-restart rollback of post-May-22 momentum guard regression

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), added active `hype_macro.alt_momentum_confirm` blocking for `BUY_YES` and `BUY_NO` on `5m`, `15m`, and `1h`; restored HYPE `min_edge 0.085 -> 0.09`, `min_edge_buy_no 0.075 -> 0.08`, down-lane edge overrides back to `0.08`, and `15m` entry-window max back to `36` minutes.
- **Why:** HYPE was the post-restart bright spot, but the same shared guard regression affected its admission path. This keeps HYPE tradeable while removing the exploration loosen that was not part of the May 22 baseline.
- **Hypothesis:** HYPE should retain confirmed directional fills but avoid unconfirmed downside/default entries admitted only by the weakened allowlist posture.
- **Expected outcome:** Next paper run should show lower unconfirmed HYPE throughput and cleaner side-source attribution without disabling HYPE.
- **Actual outcome:** `pending` (need ≥15 closed HYPE trades after restart on this config).
- **Status:** `pending`

### 2026-05-25 — BUY_NO exploration threshold nudge

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), lowered HYPE BUY_NO admission slightly: `min_edge_buy_no 0.08 -> 0.075`, and 5m/15m `down` lane-policy `min_edge 0.08 -> 0.075`. Restored wider discovery timing windows; existing `0.3x` exploratory size remains.
- **Why:** Current session `test_20260525_051023` had HYPE positive (`15` trades, `+$11.54`), while HYPE skips were mostly `lane_min_edge`. This is a throughput nudge for calibration, not a defensive cut.
- **Hypothesis:** HYPE should generate more BUY_NO samples without materially increasing notional risk, letting us evaluate whether the profitable 15m down families persist.
- **Expected outcome:** Higher HYPE entry count and fewer near-threshold `lane_min_edge` skips; maintain lane-level review after ≥15 closed post-change HYPE trades.
- **Actual outcome:** `pending` (need ≥15 closed `hype_macro` trades after this change).
- **Status:** `pending`

### 2026-05-25 — Cap live lane-calibration alpha at identity

- **What changed:** In [src/analysis/lane_calibration.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/lane_calibration.py), `ALPHA_CLAMP_HI` changed from `2.50` to `1.00`. Raw `alpha_ewma` telemetry can still exceed `1.0`, but live calibration can no longer amplify HYPE probabilities away from 50/50; sub-1 shrinkage remains active.
- **Why:** Session attribution showed `alpha_used > 1.0` was damaging the overall session and alt lanes. HYPE should remain in observation for whether shrink-only calibration improves realized expectancy.
- **Hypothesis:** HYPE drawdown from high-alpha cohorts should decline without blocking base signal generation.
- **Expected outcome:** Next live/non-shadow session should show no effective `alpha_used > 1.0` HYPE entries and reduced high-alpha loss concentration.
- **Actual outcome:** `pending` (need ≥15 closed `hype_macro` trades after this change).
- **Status:** `pending`

### 2026-05-24 — Restore HYPE admission coverage after basis starvation

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), kept HYPE edge floors unchanged, widened oracle basis validation from `25/30` bps to `40/60` bps, and set 5m/15m/1h lane `size_multiplier: 0.3` for calibration sizing.
- **Why:** Session `test_20260524_060424` showed HYPE was effectively idle: `3` fills across `116` candidate events (`2.6%` entry rate), with `oracle_basis_block` (`56`) and `lane_min_edge` (`29`) dominating rejects. The blocked basis distribution was mostly `31.8–57.0` bps, just above the prior cap.
- **Hypothesis:** HYPE should participate across 5m/15m/1h instead of idling when basis is modestly outside the old cap, while still rejecting zero/negative-edge candidates and keeping existing size/risk controls.
- **Expected outcome:** HYPE entry rate should rise materially; `oracle_basis_block` and `lane_min_edge` should no longer dominate every scan cycle.
- **Actual outcome:** `pending` (need ≥15 closed `hype_macro` trades after this change).
- **Status:** `pending`

### 2026-05-21 — Consensus ghost loosen: HYPE LONG liquidity, entry window, and oracle basis

- **What changed:** In [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), loosened HYPE admission gates supported by settled ghost review: `strategies.hype_macro.min_liquidity` **300 → 100**, `entry_window_15m_max` and `entry_policy.window_side_overrides.15m.up.entry_window_max` **36.0 → 45.0**, `oracle_max_basis_bps` **18.0 → 25.0**, and added `oracle_basis_relax_max_bps: 30.0`.
- **Why:** Consensus review flagged `hype_macro|LONG|liquidity` at **74.8% WR / 2,143 samples**, `hype_macro|LONG|lane_entry_window` at **60.0% WR / 5,895 samples**, and `hype_macro|LONG|oracle_basis_block` at **61.2% WR** as overly restrictive. ETH loosening was intentionally skipped because live ETH was being damaged by stop-loss exits.
- **Hypothesis:** HYPE LONG throughput should increase in the highest-WR missed buckets while preserving oracle overlap safety via a bounded 25 bps normal cap and 30 bps relaxed cap.
- **Expected outcome:** HYPE skip telemetry should show materially fewer `liquidity`, `outside_entry_window`, and `oracle_basis_block` skips for LONG candidates; post-change closed HYPE LONG trades should maintain positive expectancy.
- **Actual outcome:** `pending` (need ≥15 closed HYPE LONG trades after this change).
- **Status:** `pending`

### 2026-05-09 — Protect HYPE 15m BUY_YES with oracle/composite/AI/shadow

- **What changed:** `hype_macro` now requires oracle validation (`oracle_max_basis_bps=12.0`), composite score `>=0.70`, direct AI approval, shadow portfolio approval, and a `0.35x` calibration size multiplier for `15m BUY_YES`.
- **Why:** The latest bad HYPE lane was 15m BUY_YES; it needed direct protection rather than another broad HYPE or SOL-family gate.
- **Hypothesis:** HYPE 15m BUY_YES participation falls to only high-quality validated setups, and losses are capped while the next clean post-change sample accumulates.
- **Expected outcome:** No HYPE 15m BUY_YES signal reaches sizing without oracle/composite/AI/shadow approval; size logs include `15m_buy_yes_size=0.35x`.
- **Actual outcome:** `pending` (need ≥15 clean post-change closed HYPE 15m BUY_YES trades and positive lane PnL).
- **Status:** `pending`

### 2026-05-07 — Tighten HYPE 5m only: require BTC catalyst on 5m

- **What changed:** Set [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.hype_macro.require_btc_catalyst_5m` from `false` to `true`.
- **Why:** Active failure session audit showed the clearest HYPE damage came from `5m` BUY_YES longs under `btc_1h_regime=BEAR`, often with low-correlation tags, then bleeding into `updown_time_stop`. This lever is narrow: it tightens the failing HYPE `5m` path without changing HYPE `15m`.
- **Hypothesis:** Requiring an actual BTC catalyst (`lag_opportunity` or `btc_spike_detected`) on HYPE `5m` should cut marginal grind/noise entries in bearish regime while leaving the cleaner HYPE `15m` path alone.
- **Expected outcome:** HYPE `5m` fire rate drops; `updown_time_stop` concentration in HYPE `5m` declines. HYPE `15m` should remain behaviorally similar.
- **Actual outcome:** `pending` (need ≥15 closed HYPE trades post-change).
- **Status:** `pending`

### 2026-05-06 — HYPE BUY_NO admission softening: add `min_edge_buy_no: 0.08` (commit `d6da79c`)

- **What changed:** Added `min_edge_buy_no: 0.08` to `strategies.hype_macro` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) — mirrors the override XRP candidate A already runs. Previously HYPE inherited base `min_edge: 0.09` for both sides.
- **Why:** Live 48-72h slice showed HYPE 15m at 69.4% WR / +$17.21 net but with severe payoff skew: time_stop wiped -$52.29 (14 trades, 0% WR) against +$71.35 take_profit gains. Within 15m closed trades, HYPE was 61/62 BUY_YES (98% one-sided). BUY_NO suppression diagnostic showed `edge_below_min` accounted for 48% of HYPE BUY_NO skips — i.e. the symmetric 0.09 floor was rejecting BEARISH-regime BUY_NO opportunities that did meet 0.08.
- **Hypothesis:** Lowering BUY_NO edge floor by 1pt restores BUY_NO admission in BEARISH BTC regimes (HYPE BUY_NO trades cluster there: 10 BEARISH / 7 NEUTRAL of 17 BUY_NO sample) without changing BUY_YES gating.
- **Expected outcome:** HYPE 15m BUY_NO share rises from <2% baseline within 15m closed slice toward ≥10%. Overall fire rate not materially down (this is admission widening, not tightening).
- **Actual outcome:** `pending` (need ≥15 closed trades post-change).
- **Status:** `pending`
- **Failure criteria → revert:** if BUY_NO still <3% of 15m closes after 24h, the cause is upstream of `min_edge_buy_no` (likely regime gates / `flat_btc_no_lag`); revert and investigate.

### 2026-05-05 — Staged rollback (tier 2, HYPE only): `enforce_alt_1h_alignment` true → false

- **What changed:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.hype_macro.enforce_alt_1h_alignment`: **`true` → `false`**. ETH/SOL/XRP macro lanes left unchanged in this step (playbook: one lane at a time).
- **Why:** Post–May-4 attribution bucketed meaningful HYPE losses under **`updown_time_stop`** / **`updown_expired`** alongside ETH/SOL/XRP tightening era; isolate HYPE macro gate before touching ETH/XRP `enforce_alt_1h` or SOL.
- **Hypothesis:** If HYPE skips fire-rate collapse was driven by the shared alt-1H alignment gate, relaxing **HYPE only** should change HYPE entry mix without unmasking all macro lanes at once.
- **Expected outcome:** Observable shift in HYPE skip mix vs `alt_1h` / MACD-alignment-style reasons; journal PnL by HYPE exit reason moves off pure time-stop clustering if gate interaction was the driver.
- **Actual outcome:** `pending` (≥15 closed `hype_macro` trades after restart; compare to [`docs/session_reports/attribution_since_post_may4_150648.json`](docs/session_reports/attribution_since_post_may4_150648.json) baseline).
- **Status:** `pending`

### 2026-05-03 — SOL-parity prediction stack (remove defensive HYPE-only layering)

- **What changed:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.hype_macro`: **`min_liquidity` 100→1000**, **`min_edge`/`min_edge_5m`** → **0.09 / 0.07**, **`hard_min_edge` 0.05→0.07**, added **`min_edge_15m_when_ltf_unconfirmed: 0.11`**, **`center_price_band` 0.005→0.0**, **`min_edge_when_centered` 0.13→0.12**, **`min_lag_magnitude_pct` 0.30→0.40**, **`btc_spike_floor_pct_*`** → **0.15 / 0.40** (sol_macro), **`low_corr_suppresses_entries` false** (keep damping only), **`iql_15m_enabled` false**, **`btc_min_4h_hist_magnitude` reverted to 20** (same BTC HTF conviction band as SOL macro family — bitcoin up/down lane remains **35** separately). Removed stacked narrow-center / higher spike-floor / hard low-corr suppress / IQL veto philosophy for HYPE-only tightness.
- **Why:** Operator intent is **directional prediction quality** via the shared lag architecture, not extra HYPE-specific skip stacks (`low_corr_suppresses_entries`, tighter edges, higher spike floors, IQL on thin bars) that mainly reduced evaluated tape vs SOL without being requested as a thesis layer.
- **Hypothesis:** HYPE trades through the **same gate semantics as SOL** where economics apply; residual differentiation stays **`dynamic_beta_*`** (Hyperliquid regime vs SOL spot scale).
- **Expected outcome:** Journal/`last_signal_counts` for `hype_macro` reflects signal path comparable to SOL macro family; low correlation windows **damp** edges rather than hard-suppressing entries unless other gates skip.
- **Actual outcome:** `pending` (≥15 closed `hype_macro` trades post-deploy — do not estimate).
- **Status:** `pending`

### 2026-05-03 — BTC 4H conviction middle band + SOL-parity neutral/catalyst gates

- **What changed:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.hype_macro`: **`btc_min_4h_hist_magnitude` 20 → 35** (matches `strategies.bitcoin.min_4h_hist_magnitude` middle ground — same BTC TA series as `_get_btc_htf_bias`). **`neutral_macro_require_spike_or_lag` true → false** (align with `sol_macro`: NEUTRAL chop can use alt 1H bias when no BTC spike/lag). **`require_btc_catalyst_5m` true → false** (align with SOL/XRP 5m path).
- **Why:** HYPE inherited `SolMacroStrategy` but YAML had **stricter** NEUTRAL + 5m catalyst rules than SOL while ops compared firing to looser lanes; BTC conviction at **20** also diverged from BTC up/down lane **35**.
- **Hypothesis:** Fewer unexplained idle cycles vs SOL when BTC HTF is NEUTRAL; BTC HTF downgrade logs align with bitcoin strategy band; **35** avoids fully loose macro **20** on HYPE if operator wants consistency with BTC lane only (SOL/ETH/XRP macros unchanged at **20**).
- **Expected outcome:** More HYPE evaluations passing macro-neutral branch when spike/lag absent but alt has 1H bias; 5m markets not gated out solely by `require_btc_catalyst_5m`; logs show shared conviction threshold vs BTC bitcoin where hist sits mid-band.
- **Actual outcome:** `pending` (≥15 closed `hype_macro` trades post-deploy + ops skip mix — do not estimate).
- **Status:** `pending`

### 2026-05-02 — Inherited SOL entry-window timing fix + YAML band alignment

- **What changed:** Inherits [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) entry-window bound resolution (no fixed 15m cap on auto-align upper bound) and latency-adjusted `mins_left`. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.hype_macro`: `entry_window_15m_max` **45.0**, `entry_window_15m_min` **2.0**, `entry_window_latency_buffer_sec` **12**, `ai_entry_window_15m_min` **2.0**.
- **Why:** HYPE inherits `SolMacroStrategy`; timing exclusions applied before beta/lag/low-correlation gates.
- **Hypothesis:** Fewer false `outside_entry_window` skips without loosening HYPE-specific correlation suppression or edge gates.
- **Expected outcome:** Timing parity with SOL macro family; HYPE-specific guards unchanged.
- **Actual outcome:** `pending` (minimum ~15 closed HYPE macro trades after deploy).
- **Status:** `pending`

### 2026-04-30 — Hard suppress HYPE entries in decoupled BTC regime

- **What changed:** Added config-driven `low_corr_suppresses_entries` in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) and enabled it for HYPE in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml). HYPE now skips entries when 1H BTC↔HYPE correlation is below `0.40` instead of only damping the edge.
- **Why:** HYPE can decouple from BTC during idiosyncratic flow; a BTC-lag thesis should not keep trading when the linkage itself is below the configured floor.
- **Hypothesis:** The HYPE lane should avoid low-correlation false positives where BTC is moving but HYPE is trading its own regime.
- **Expected outcome:** HYPE `top_skip_reasons` may show `low_corr_suppressed`; trade count should drop during decoupled HYPE windows.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed HYPE macro trades after deploy).
- **Status:** `pending`

### 2026-04-30 — Shared lag staleness and explicit inherited price band

- **What changed:** HYPE inherits the shared [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) lag staleness fix and the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) up/down price-band fix.
- **Why:** HYPE uses the shared BTC-lag macro architecture and inherited scan loop; stale lag timestamps and symmetric-by-accident price-band math affected inherited behavior.
- **Hypothesis:** HYPE should suppress old BTC impulse carryover while still refreshing on materially larger BTC moves, and future asymmetric price-band tuning should apply exactly.
- **Expected outcome:** No default behavior change while HYPE remains `0.46–0.54`; safer stale-lag behavior in decaying impulse windows.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed HYPE macro trades after deploy).
- **Status:** `pending`

### 2026-04-29 — HYPE-specific beta clamp and low-correlation tuning

- **What changed:** [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) now accepts asset-specific dynamic-beta clamp parameters, [src/analysis/hyperliquid_hype_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/hyperliquid_hype_service.py) forwards them, and [src/strategies/hype_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/hype_macro.py) now rebuilds its HYPE service from `strategies.hype_macro` config. Added `dynamic_beta_min/max/extreme_max` and `low_corr_threshold_1h` / `low_corr_damping` to [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) for HYPE.
- **Why:** HYPE was using the shared SOL/BTC lag architecture with SOL-tuned beta clamps and correlation damping even though HYPE trades on a different venue and has a wider, more unstable beta regime.
- **Hypothesis:** Giving HYPE a wider beta envelope and a looser low-correlation trigger should reduce false “SOL-like” suppression while still damping genuinely decoupled HYPE windows.
- **Expected outcome:** HYPE should generate lag estimates that better reflect its own volatility regime, with fewer borderline windows automatically compressed by SOL-oriented correlation assumptions.
- **Actual outcome:** `pending` (need ≥15 closed HYPE macro trades after deploy; backtest/live review also pending once HYPE backtests are run).
- **Status:** `pending`

## Review sessions

### 2026-05-18 — Ghost calibration follow-up: HYPE short-side liquidity is the clearest pending loosen candidate

- **Headline:** Settled ghost data currently favors a HYPE-short liquidity loosen before any broader HYPE gate changes, but this remains a logged next-step candidate pending more settled markets.
- **Evidence snapshot:** `hype_macro|1h|BUY_NO|liquidity` showed `n=68`, `100%` WR, `total_realized_pct=+66.26`; `hype_macro|15m|BUY_NO|liquidity` showed `n=73`, `71.2%` WR, `+31.17`; `hype_macro|5m|BUY_NO|liquidity` showed `n=86`, `58.1%` WR, `+46.79`. Combined `hype_macro|1h|down|bearish|rejected` lane was `n=84`, `100%` WR, `+82.30`.
- **Possible next move after more data:** If this stays stable, lower HYPE short-side liquidity thresholds first, then re-check whether `lane_entry_window` needs a narrower lane-specific loosen.
- **Not supported yet:** A broad HYPE short `min_edge` reduction is **not** yet supported by the current settled ghost report; `lane_min_edge` for HYPE shorts is still a tiny sample.

### 2026-05-07 — Active paper failure session `test_20260507_035930`

- **Headline:** HYPE is the clearest lane that actually deteriorates later in the session: `18` closes, `-17.52` net.
- **Early vs late:** early half `9` exits, `-2.50`, `55.6%` WR; late half `9` exits, `-15.03`, `33.3%` WR.
- **Regime split:** BTC 1H regime on the HYPE exits was almost entirely `BEAR`. `BEAR 15m` was `10` exits, `-3.60`, `60%` WR, `3` time-stops; `BEAR 5m` was `7` exits, `-9.93`, `28.6%` WR, `5` time-stops. One `RANGE 15m` trade lost `-4.00` via time-stop.
- **Key finding:** Later HYPE trades were not obviously lower-edge than early HYPE trades; deterioration showed up as more `updown_time_stop` losses, not weaker nominal edge. This is a regime/path problem, especially on `5m`, more than a simple threshold-too-low problem.

### 2026-05-04 — Paper `test_20260504_034719`

- **Headline:** Standout lane: **15** closes, **86.7%** WR, **+$15.73**; two losses avg about **-$3.30**.
- **Lag / journal:** **9/15** closed trades had **no** `lag_magnitude` on ENTRY `extra` when joined to EXIT (60%) — matches “no lag opportunity” / `macro_leg=None`-style journals; aligns with SOL thesis that lag=None can still work, but worth monitoring for **LONG** bias without catch-up confirmation.
- **Config parity note:** `strategies.hype_macro.enforce_alt_1h_alignment` remains **false** while **xrp_macro** moved to **true** (2026-05-04) — optional follow-up if ops wants macro-family alignment.
- **Artifact:** same session JSON as other lanes; rolling heatmap in [`docs/session_reports/hourly_heatmap_20260504_exit_pt.txt`](docs/session_reports/hourly_heatmap_20260504_exit_pt.txt) (XRP/HYPE rows without YAML block overlay).

## Lessons learned

_(none yet — add only after data)_
