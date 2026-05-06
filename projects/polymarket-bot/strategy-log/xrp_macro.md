# XRP macro (`xrp_macro`)

XRP **Up or Down** — inherits shared `SolMacroStrategy` signal path with XRP market detection and `XRPUSDT` spot leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 23 | Paper `test_20260504_034719` — [`docs/session_reports/session_parse_test_20260504_034719.json`](docs/session_reports/session_parse_test_20260504_034719.json) |
| Win rate | 69.6% | same |
| Net PnL | +$2.38 | same |
| Avg win / avg loss (session) | +$1.53 / -$3.15 | same |
| BUY_YES / BUY_NO (session) | 22 / +$5.43 vs 1 / -$3.05 | same |

## Change Log

### 2026-05-06 — Candidate C rollout (15m BUY_YES high-price tightening)

- **What changed:** Added 15m BUY_YES-specific up/down price cap support in [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) via optional config key `entry_price_max_15m_buy_yes` (defaults to existing `entry_price_max` behavior). Set [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.xrp_macro.entry_price_max_15m_buy_yes=0.55` (from implicit `0.58`).
- **Why:** Forensic audit identified poor recent XRP performance with one-sided BUY_YES participation and loss concentration; tightening rich 15m BUY_YES entries reduces late/overpriced long exposure without constraining 5m or BUY_NO paths.
- **Hypothesis:** Fewer high-price 15m BUY_YES fills should lower negative expectancy in the affected bucket while preserving valid mid-band entries.
- **Expected outcome:** Over next ~20 XRP closes, 15m BUY_YES entries above 0.55 should drop toward zero, with improved net PnL and reduced 15m adverse exits.
- **Actual outcome:** `pending` (need post-change sample).
- **Status:** `pending`

### 2026-05-05 — Forensic remediation rollout (Candidate A + B)

- **What changed:** Applied targeted XRP remediations from the forensic audit: in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), set `xrp_macro.enforce_alt_1h_alignment=false` and lowered `xrp_macro.min_edge_buy_no` from `0.10` to `0.08` (Candidate A). Added `trading.exit_rules.updown_overrides.xrp_macro` with `updown_stop_cents=0.04` and `updown_exit_window_mins=1.5`; [src/execution/live_testing.py](/Users/mainfolder/Documents/psb-main%201/src/execution/live_testing.py) now supports per-strategy up/down exit overrides (Candidate B) while preserving global defaults for other lanes.
- **Why:** Forensic report on recent bad cluster (`test_20260504_150648`, `test_20260505_044854`, `test_20260505_184014`) showed XRP net `-$11.62`, with `updown_time_stop` losses `-$23.84` (76.4% of negative PnL) and zero recent BUY_NO executions despite positive BUY_NO backtest contribution.
- **Hypothesis:** Restoring BUY_NO admission and softening XRP-specific late-window stop behavior should reduce one-sided BUY_YES drawdowns and lower XRP time-stop loss concentration without collateral effects to BTC/SOL/ETH/HYPE exits.
- **Expected outcome:** Over next ~20 XRP closes: BUY_NO share ≥10%, XRP net PnL non-negative, and `updown_time_stop` share of XRP negative PnL <30%.
- **Actual outcome:** `pending` (need live sample post-change).
- **Status:** `pending`

### 2026-05-05 — XRP loss concentration note (hold config for +1h data)

- **What changed:** No strategy code/config edits in this step. Logged session diagnosis for `test_20260505_044854`: `xrp_macro` closed **15** trades, net **-$3.90**; `updown_time_stop` exits were **6 trades / -$14.65** while `take_profit` exits were **7 trades / +$9.70**. BUY/side mix was effectively one-sided (`BUY_YES` only in that session).
- **Why:** Loss concentration is in the exit path and side mix; immediate tuning without another short sample risks overfitting to one session.
- **Hypothesis:** If underperformance persists over the next ~1 hour of additional XRP closes, the first adjustment should target `updown_time_stop` behavior and/or BUY_NO admission parity before widening BUY_YES risk.
- **Expected outcome:** Collect another short session slice, then decide whether to: (a) condition/widen time-stop logic for XRP, (b) re-enable practical BUY_NO participation when gates permit, or (c) keep unchanged if distribution normalizes.
- **Actual outcome:** `pending` (waiting for additional ~1h data collection and re-review).
- **Status:** `pending`

### 2026-05-02 — Inherited SOL entry-window timing fix + YAML band alignment

- **What changed:** Inherits [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) entry-window bound resolution (no fixed 15m cap on auto-align upper bound) and latency-adjusted `mins_left`. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.xrp_macro`: `entry_window_15m_max` **45.0**, `entry_window_15m_min` **2.0**, `entry_window_latency_buffer_sec` **12**, `ai_entry_window_15m_min` **2.0**.
- **Why:** XRP inherits `SolMacroStrategy`; timing gates run before BTC-move and macro-specific sizing gates.
- **Hypothesis:** XRP reaches real economics filters more often with the same risk posture.
- **Expected outcome:** Timing parity with SOL macro family.
- **Actual outcome:** `pending` (minimum ~15 closed XRP macro trades after deploy).
- **Status:** `pending`

### 2026-04-30 — Shared lag staleness and explicit inherited price band

- **What changed:** XRP inherits the shared [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) lag staleness fix and the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) up/down price-band fix.
- **Why:** XRP uses the shared BTC-lag macro architecture and inherited scan loop; stale lag timestamps and symmetric-by-accident price-band math affected inherited behavior.
- **Hypothesis:** XRP should suppress old BTC impulse carryover while preserving fresh impulse opportunities, and future asymmetric price-band tuning should apply exactly.
- **Expected outcome:** No default behavior change while XRP remains `0.46–0.54`; safer stale-lag behavior and explicit config fidelity.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed XRP macro trades after deploy).
- **Status:** `pending`

### 2026-04-29 — XRP-specific beta clamp and low-correlation tuning

- **What changed:** [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) now supports asset-specific dynamic-beta clamps, and [src/strategies/xrp_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/xrp_macro.py) now rebuilds its `XRPUSDT` lag service from `strategies.xrp_macro` config. Added XRP-specific `dynamic_beta_min/max/extreme_max` plus `low_corr_threshold_1h` / `low_corr_damping` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml).
- **Why:** XRP was inheriting SOL-oriented correlation damping and beta assumptions even though XRP commonly runs at lower BTC correlation and a different response profile.
- **Hypothesis:** Lowering the low-correlation trigger and using milder damping should stop XRP from being permanently treated as a low-conviction SOL clone, while still shrinking edges when BTC/XRP linkage is genuinely weak.
- **Expected outcome:** More XRP windows should survive the correlation confidence layer and be evaluated on their actual macro/LTF quality rather than being routinely compressed by SOL-style defaults.
- **Actual outcome:** `pending` (need ≥15 closed XRP macro trades after deploy for first live review).
- **Status:** `pending`

### 2026-04-29 — XRP liquidity floor lowered + explicit timing config

- **What changed:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) lowered `strategies.xrp_macro.min_liquidity` from `5000` to `1000`, and added explicit XRP up/down entry windows: 15m `8.0–15.0`, 5m `0.75–5.0`, with auto-alignment enabled.
- **Why:** Live ops showed XRP mostly blocked by `liquidity` and `outside_entry_window`. The `5000` liquidity floor was materially stricter than BTC/SOL/ETH and far above HYPE, despite XRP still needing live validation sample size.
- **Hypothesis:** XRP should collect a usable paper sample instead of being filtered out before edge evaluation, while still retaining edge, price-band, BTC-move, max-position, and sizing gates.
- **Expected outcome:** XRP should show fewer liquidity/timing skips and more cycles reaching `edge_below_min`, price-band, or BTC/ALT confirmation gates. Closed-trade validation remains required before treating the lane as proven.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed XRP macro trades after deploy).
- **Status:** `pending`

### 2026-04-27 — XRP oracle coverage via shared macro service

- **What changed:** Through the shared [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) update, XRP now has an asset-specific Chainlink XRP/USD reference feed on Polygon available to the macro analysis object. This pass does not enable a hard XRP basis veto yet; it adds the oracle coverage so basis can be observed first.
- **Why:** XRP was previously missing an asset-specific oracle reference in the strategy stack.
- **Hypothesis:** Observing XRP basis before adding a hard gate is the safer rollout path for a lane with a very small live sample.
- **Expected outcome:** XRP analysis can now surface oracle-vs-exchange divergence for later review.
- **Actual outcome:** `pending`.
- **Status:** `pending`

### 2026-04-27 — Enable XRP AI assist and shared timing window

- **What changed:** Turned `strategies.xrp_macro.use_ai` and `use_ai_updown` on in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), and added the same `ai_entry_window_*`, `ai_hold_veto_ttl_sec`, and `min_edge_5m_ai_override` controls used by the other macro up/down lanes. [src/strategies/xrp_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/xrp_macro.py) now reads its own AI-hold settings instead of inheriting the SOL defaults accidentally.
- **Why:** XRP was the only active crypto macro lane still running quant-only despite the shared AI-assist path already existing in `SolMacroStrategy`.
- **Hypothesis:** XRP should stay quant-first, but AI can help on marginal up/down entries once enough candle data has formed.
- **Expected outcome:** XRP becomes operationally consistent with the other crypto macro lanes, with AI only participating inside the configured timing band.
- **Actual outcome:** `pending` (need ≥15 closed XRP macro trades after deploy).
- **Status:** `pending`

### 2026-04-26 — Sample-size caution from first XRP macro batch

- **What changed:** No code/config change in this entry; this logs the first meaningful XRP macro paper slice.
- **Why:** Session produced only 3 XRP closes (all wins), which is too small to treat as validated edge.
- **Hypothesis:** Current XRP entries skew to higher edge (roughly 0.11–0.145) with earlier profit-taking, which can look strong in tiny samples but may not hold as sample broadens.
- **Expected outcome:** Keep strategy enabled and collect larger sample before changing gates or sizing.
- **Actual outcome:** `pending` (need ≥15 closed XRP macro trades for first confidence pass).
- **Status:** `pending`

## Review sessions

### 2026-05-04 — Paper `test_20260504_034719`

- **Headline:** Positive net **+$2.38** with **69.6%** WR but **payoff skew** — average loss (~-$3.15) larger than average win (~+$1.53); one **BUY_NO** loss (-$3.05) — sample tiny on the short side.
- **Rolling heatmap:** `scripts/hourly_heatmap.py --days 30 --hour-axis exit_pt --suggest` (output [`docs/session_reports/hourly_heatmap_20260504_exit_pt.txt`](docs/session_reports/hourly_heatmap_20260504_exit_pt.txt)) flags **Pacific exit hour H11** for `xrp_macro`: 33.3% WR, avg **-$0.70**/trade, **n=6** — script suggests considering `blocked_utc_hours_updown: [11]`; **operator decision pending** (low n; policy says ≥7 days / not acting on noise alone).
- **Config follow-up (after session):** `enforce_alt_1h_alignment: true`, `min_edge_buy_no: 0.10` — **actual outcome `pending`** (≥15 closes post-change).
- **Heatmap script gap:** blocked-hour column for XRP is **not** wired in `STRATEGY_CONFIG_KEYS`; interpret XRP hour tables as performance-only unless the script is extended.

### 2026-04-30 — Structural low-correlation watch

- XRP is expected to spend more time below SOL/ETH-style BTC correlation levels, so its edge can be structurally damped by design rather than because of a single bad configuration.
- Future calibration should avoid simply raising `min_edge` after weak samples without checking whether `max_edge_updown`, correlation damping, and the XRP-specific thesis still leave a meaningful tradeable band.
- Treat XRP as a weaker BTC-lag lane until live data proves otherwise; do not chase small samples by squeezing the edge band asymmetrically.

### 2026-04-26 — Early paper read

- 3/3 wins is directional positive signal only; not statistically meaningful yet.
- Continue watching edge distribution and exit path behavior before tuning.

## Lessons learned

_(none yet — add only after data)_
