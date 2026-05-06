# ETH macro (`eth_macro`)

ETH **Up or Down** — inherits `SolMacroStrategy` (shared entry-window and scan logic); `ETHMacroStrategy` overrides market detection and `ETHUSDT` leg.

## Quick Stats

| Metric | Value | Source |
|--------|-------|--------|
| Closed trades (strategy) | 9 | Paper `test_20260504_034719` — [`docs/session_reports/session_parse_test_20260504_034719.json`](docs/session_reports/session_parse_test_20260504_034719.json) |
| Win rate | 77.8% | same |
| Net PnL | +$13.45 | same |

## Change Log

### 2026-05-06 — ETH 5m admission hardening: disable 1H-only impulse bypass

- **What changed:** Disabled [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro.btc_follow_5m_allow_1h_impulse_bypass` (`true` → `false`). ETH 15m behavior and ETH side-resolution mode remain unchanged.
- **Why:** Recent ETH 5m attribution showed the weakest admission bucket was the 5m fallback path tagged `bypass_5m_impulse_btc_1h_ok`: across recent audited sessions that bucket contributed **30** closes for **-$7.38** net, while ETH entries with real BTC 5m impulse were positive (**11** closes, **+$7.15**). Combined with the stop attribution, this indicates ETH 5m was admitting too many longs on 1H continuation alone without real short-term BTC impulse.
- **Hypothesis:** Requiring genuine BTC 5m impulse for ETH 5m should reduce low-quality `hybrid_fallback` BUY_YES entries that later hit `updown_time_stop`, without changing ETH 15m routing or BTC/SOL/HYPE/XRP logic.
- **Expected outcome:** ETH 5m should show fewer `bypass_5m_impulse_btc_1h_ok` entries, lower `updown_time_stop` count from fallback longs, and a cleaner split between real BTC-impulse-follow trades and skipped grind/noise entries.
- **Actual outcome:** `pending` (need post-change ETH sample).
- **Status:** `pending`

### 2026-05-06 — ETH 5m time-stop containment override

- **What changed:** Added ETH-specific up/down exit overrides in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml): `trading.exit_rules.updown_overrides.eth_macro.updown_stop_cents=0.02` and `updown_exit_window_mins=3.0`. No strategy admission/gating logic changed in this step.
- **Why:** Recent ETH 5m attribution across `test_20260504_150648`, `test_20260505_044854`, `test_20260505_184014`, and `test_20260505_225241` showed **36** closes, net only **+$1.18**, with `updown_time_stop` contributing **14** losses / **-$42.40** versus `take_profit` **20** wins / **+$40.25**. The stop bucket is too large relative to realized winners.
- **Hypothesis:** An earlier/tighter ETH-only 5m adverse exit should reduce average ETH time-stop loss size without altering BTC/SOL/HYPE/XRP exit behavior or ETH side-selection logic.
- **Expected outcome:** Over the next ~15 ETH closes, `updown_time_stop` average loss should improve materially from roughly **-$3.03** and ETH net should remain positive with lower downside spikes.
- **Actual outcome:** `pending` (need live sample post-change).
- **Status:** `pending`

### 2026-05-05 — Staged rollback (tier 1): `direction_source` signal_first → hybrid

- **What changed:** [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro.direction_source`: **`signal_first` → `hybrid`** (post-attribution isolation; SOL/XRP unchanged in same edit).
- **Why:** Multi-session attribution after `test_20260504_150648` showed concentrated ETH losses in **`updown_time_stop`** with **`side_src=signal_first_fallback`** (see [`docs/session_reports/attribution_since_post_may4_150648.md`](docs/session_reports/attribution_since_post_may4_150648.md)).
- **Hypothesis:** Hybrid side resolution (15m signal + 4h proxy agreement for overrides) reduces one-sided `signal_first_fallback` time stops versus legacy BTC-only routing without re-opening unconstrained signal-first noise.
- **Expected outcome:** ETH journal shows more `hybrid_strong_*` / `hybrid_fallback` mix and fewer large clusters of `signal_first_fallback` time-stop losses if the hypothesis holds.
- **Actual outcome:** `pending` (minimum ~15 closed `eth_macro` trades after restart; re-run `scripts/attribution_since.py` for the new session window).
- **Status:** `pending`

### 2026-05-03 — ETH side-selection rollout: discovery recap + live session now emitting signals

- **What changed:** No additional code beyond the committed ETH direction work (`direction_source` hybrid default, `signal_first` toggle, `_resolve_market_side`, thresholds in `config/settings.yaml`; tests in `tests/test_eth_macro.py`). This entry records operator-visible behavior after deploy/restart.
- **Why:** Preserve the research finding and the post-deploy ops signal in one place: previously, ETH’s execution side could behave like a **BTC-HTF-only router**, which **removed a clean signal-driven SHORT path** even when 5m/15m scoring had SHORT-branch structure — see Hermes note `2026-05-02-shorts-direction-research.md`. Hybrid mode requires **15m market signal + BTC HTF proxy** to agree before overriding the legacy side; `signal_first` is an explicit A/B toggle.
- **Hypothesis:** After timing/window parity fixes, ETH should reach side-resolution and economics gates more often; hybrid should occasionally emit `hybrid_strong_short` / `hybrid_strong_long` without opening the floodgates to pure contrarian noise.
- **Expected outcome:** Journal and scan stats show non-trivial `side_source_counts`; SHORT legs appear when spot-implied direction disagrees with BTC HTF but agrees with thresholds; skip mix shifts off pure `outside_entry_window` dominance once windows align.
- **Actual outcome:** Operator reports the bot **is firing** on a **new test run** (early ops pulse); quantitative expectancy / WR / net PnL vs prior epoch remains **`pending`** until **≥15 closed ETH macro trades** post-change. Prior short local A/B scans (2026-05-02 PT) remained inconclusive on closed-trade outcomes.
- **Status:** `pending`

### 2026-05-02 — Direction source modes for ETH side selection (hybrid default, signal_first toggle)

- **What changed:** [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py) now supports ETH side-resolution modes:
  - `btc` (legacy): side from BTC HTF bias only
  - `hybrid` (new default): strong side override only when 15m market signal and 4h proxy both agree
  - `signal_first` (test): 15m market signal can set side directly
  Added config keys in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) under `strategies.eth_macro`:
  `direction_source`, `signal_15m_long_threshold`, `signal_15m_short_threshold`, `signal_4h_long_threshold`, `signal_4h_short_threshold`.
- **Why:** ETH could produce SHORT only when BTC HTF was bearish/neutral branch selected SHORT; operator research asked for a signal-driven SHORT path with conservative thresholds and an explicit signal-first test toggle.
- **Hypothesis:** Hybrid mode should permit clearly bearish SHORT selections without making ETH fully counter-trend/noisy by default; `signal_first` can be toggled for controlled live A/B testing.
- **Expected outcome:** ETH logs show mixed side sources (`hybrid_strong_*` and fallback) instead of BTC-only side routing; SHORT opportunities appear when 15m signal is weak (<0.45) and BTC 4h proxy is bearish.
- **Actual outcome:** Local A/B scan sessions on 2026-05-02 PT (4 cycles each, equal 20s spacing, non-ETH settings frozen) produced no entries in either mode: `hybrid` side_sources=`hybrid_fallback:12, hybrid_strong_long:1` with top skips `outside_entry_window:52, btc_15m_not_following:9`; `signal_first` side_sources=`signal_first_fallback:12` with top skips `outside_entry_window:55, btc_15m_not_following:8`. No closed ETH trades yet in this epoch.
- **Status:** `inconclusive ⚠️`

### 2026-05-02 — Inherited SOL entry-window timing fix + YAML band alignment

- **What changed:** Inherits [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) entry-window bound resolution (no fixed 15m cap on auto-align upper bound) and latency-adjusted `mins_left` before the outside-window check. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro`: `entry_window_15m_max` **45.0**, `entry_window_15m_min` **2.0**, `entry_window_latency_buffer_sec` **12**, `ai_entry_window_15m_min` **2.0**.
- **Why:** ETH uses the shared macro scan loop; without aligned YAML and inherited code fixes, ETH could remain timing-excluded before BTC-follow or oracle gates run.
- **Hypothesis:** ETH sees fewer spurious `outside_entry_window` skips with unchanged edge/oracle/BTC-follow intent.
- **Expected outcome:** Timing eligibility matches SOL macro family fixes; economics gates unchanged.
- **Actual outcome:** `pending` (minimum ~15 closed ETH macro trades after deploy).
- **Status:** `pending`

### 2026-04-30 — Shared lag staleness and explicit inherited price band

- **What changed:** ETH inherits the shared [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) lag staleness fix and the shared [src/strategies/sol_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) up/down price-band fix.
- **Why:** ETH uses the shared macro service/loop; stale lag timestamps and symmetric-by-accident price-band math affected inherited behavior.
- **Hypothesis:** ETH should avoid old BTC impulse carryover and respect future asymmetric `entry_price_min` / `entry_price_max` tuning.
- **Expected outcome:** No default behavior change while ETH remains `0.46–0.54`; safer behavior if the entry band is tuned asymmetrically.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed ETH macro trades after deploy).
- **Status:** `pending`

### 2026-04-29 — Fixed-cycle scheduler + wider ETH timing windows

- **What changed:** [src/main.py](/Users/mainfolder/Documents/psb-main%201/src/main.py) now maintains fixed scan cadence by subtracting cycle runtime before sleeping. [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) widened ETH up/down entry windows to 15m `8.0–15.0` and 5m `0.75–5.0` minutes remaining.
- **Why:** Live ops showed ETH markets arriving but repeatedly skipping as `outside_window`. ETH’s stricter BTC-follow and oracle gates cannot be evaluated if timing excludes every market first.
- **Hypothesis:** ETH should reach its actual BTC-follow, RSI, oracle-basis, edge, and price-band gates more often without weakening those gates.
- **Expected outcome:** ETH `outside_window` should drop in ops; any continued no-fire behavior should be attributable to BTC continuation, ETH confirmation, oracle basis, edge, or price-band checks.
- **Actual outcome:** `pending` (need live ops pulse and ≥15 closed ETH trades after deploy in this epoch).
- **Status:** `pending`

### 2026-04-27 — ETH Chainlink oracle verification + basis veto

- **What changed:** [src/analysis/sol_btc_service.py](/Users/mainfolder/Documents/psb-main%201/src/analysis/sol_btc_service.py) now fetches ETH/USD from the asset-specific Chainlink reference feed on Polygon for the ETH leg, records oracle price/network/basis on the analysis object, and [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py) now blocks entries when `oracle_max_basis_bps` is exceeded. Added `strategies.eth_macro.oracle_max_basis_bps: 10.0` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml).
- **Why:** ETH up/down markets resolve against an oracle source, so exchange spot drifting too far from the reference feed is a real basis risk for short-window binaries.
- **Hypothesis:** Blocking ETH entries when Binance-vs-Chainlink basis is too wide should remove a class of false edge where exchange momentum does not match the likely resolution source.
- **Expected outcome:** Fewer ETH trades during basis dislocations; cleaner ETH live sample when exchange and oracle are aligned.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after deploy in this epoch).
- **Status:** `pending`

### 2026-04-27 — ETH AI timing window + shared marginal-AI path

- **What changed:** ETH now uses the same style of marginal up/down AI assist as the other crypto strategies, but only inside an explicit AI timing band. Added `ai_entry_window_15m_min/max` and `ai_entry_window_5m_min/max` to `strategies.eth_macro` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml), and wired [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py) to consult AI on marginal edges only when that timing window is open. Also made ETH read its own `ai_hold_veto_ttl_sec` / `min_edge_5m_ai_override` config instead of inheriting the shared SOL defaults accidentally.
- **Why:** ETH’s custom BTC-follow path was still quant-only while BTC/SOL/HYPE already had LLM tie-break support. The goal of this pass is to let AI weigh in after some candle data has formed, without turning ETH into an AI-first strategy or moving the whole quant entry framework.
- **Hypothesis:** AI should help most on borderline ETH up/down entries when there is enough intra-window information to reason about continuation versus exhaustion. The timing window should reduce low-information AI calls near candle open.
- **Expected outcome:** ETH keeps its BTC-follow quant core, but marginal 5m/15m up/down decisions can now be assisted by AI when timing is favorable; future journal data should show whether AI reduces borderline ETH misses.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after deploy in this epoch).
- **Status:** `pending`

### 2026-04-27 — ETH 15m hardening + explicit oversold SELL block (new evaluation epoch)

- **What changed:** Tightened the 15m BTC-follow confirm path in [src/strategies/eth_macro.py](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py): weak `ETH15m above signal` / `below signal` states no longer score positively; 15m confirmation now requires either a crossover or same-direction histogram magnitude with in-direction momentum. BTC 15m follow gating also no longer accepts candle drift by itself; it now requires MACD impulse or MACD-plus-candle agreement. Added `strategies.eth_macro.rsi_sell_block_below: 40.0`, `eth_follow_15m_hist_min: 0.03`, and raised `eth_follow_15m_min_adj` to `0.05` in [config/settings.yaml](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml).
- **Why:** ETH 5m improved materially after the BTC-follow/backtest-fidelity fix, but ETH 15m remained negative. Recent bad `SELL_YES` entries were also showing oversold RSI, which should be blocked explicitly instead of only being implied by softer scoring.
- **Hypothesis:** Hardening 15m confirmation should remove weak continuation entries without materially hurting ETH 5m. The explicit oversold SELL block should filter the bounce-prone `SELL_YES` setups seen in recent ETH paper losses.
- **Expected outcome:** ETH 15m improves versus pre-change baseline while ETH 5m stays directionally similar to its strong 2026-04-27 baseline.
- **Actual outcome:** `pending` (need ≥15 closed ETH trades after the change; backtest comparison also pending for this epoch).
- **Status:** `pending`
- **Epoch note:** Treat this as a new ETH evaluation epoch starting **2026-04-27**. Keep prior ETH paper trades in the journal, but do not combine pre- and post-redesign/fidelity-fix ETH samples into a single performance judgement.
- **Pre-change baselines:** 5m [backtest_crypto_ETH_5m_20260427_183330.json](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_ETH_5m_20260427_183330.json); 15m [backtest_crypto_ETH_15m_20260427_183516.json](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_ETH_15m_20260427_183516.json)

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

### 2026-05-04 — Paper `test_20260504_034719`

- **Headline:** Strong slice: **9** closes, **77.8%** WR, **+$13.45** — all `BUY_YES` in this session parse.
- **Artifact:** [`docs/session_reports/session_parse_test_20260504_034719.json`](docs/session_reports/session_parse_test_20260504_034719.json); heatmap [`docs/session_reports/hourly_heatmap_20260504_exit_pt.txt`](docs/session_reports/hourly_heatmap_20260504_exit_pt.txt).

## Lessons learned

_(none yet — add only after data)_
