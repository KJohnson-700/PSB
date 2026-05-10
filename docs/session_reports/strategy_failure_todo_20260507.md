## Strategy Failure To-Do — test_20260507_035930 — 2026-05-07

### Scope
- Session: [`data/paper_trades/test_20260507_035930/summary.json`](/Users/mainfolder/Documents/psb-main%201/data/paper_trades/test_20260507_035930/summary.json)
- Current damage: `-59.04` PnL
- Strategy breakdown:
  - `bitcoin`: `34` trades, `-37.2`
  - `hype_macro`: `18` trades, `-17.52`
  - `xrp_macro`: `4` trades, `-6.55`
  - `eth_macro`: `0` trades
  - `sol_macro`: `6` trades, `+2.23`

### Working Hypothesis
The failure does **not** currently look like scanner collapse after the restart. The stronger pattern is that `bitcoin` and `hype_macro` kept trading into a bad regime, `xrp_macro` had a smaller but negative sample, `eth_macro` was fully gated out, and `sol_macro` was mostly suppressed.

## 1. Scanner

### 1.1 Confirm scanner is not the primary cause post-restart
- Evidence to keep in mind:
  - No fresh `Too many open files` lines in the active session log after restart.
  - Scanner network phase stays around `3.2s-4.1s` late in session.
  - Market counts remain stable: `updown_15m_count≈45`, `updown_5m_count=20`, `updown_hype_alt_count=3`.
- Next checks:
  1. Pull the exact restart timestamp and prove the active process started after scanner commit `32cb718`.
  2. Verify no scanner warnings/errors exist anywhere in the active-session time window.
  3. Build a cycle timeline comparing `Scanner: sync network phase finished` vs `Crypto parallel scan phase complete` to isolate whether strategy evaluation, not scanning, is what remained slow.

### 1.2 Check if strategy phase, not scanner phase, degraded through the session
- Evidence:
  - Scanner phase late-session: roughly `3-4s`
  - Crypto parallel strategy phase late-session: often `30s-52s`
- Next checks:
  1. Measure early vs late `crypto parallel scan phase` duration.
  2. Correlate slower strategy phases with losing entries and `updown_time_stop` exits.
  3. Check whether long cycle times caused stale admission or stale management windows.

## 2. Bitcoin

### 2.1 Audit why BTC is the main blowup
- Current evidence:
  - `bitcoin` is the biggest drag: `-37.2`
  - Exit mix is the problem: many `updown_time_stop` losses outweigh smaller `take_profit` wins.
- Early vs late within BTC exits:
  - Early half: `18` exits, `-28.748`, `38.9%` WR
  - Late half: `18` exits, `-5.75`, `61.1%` WR
- Interpretation:
  - BTC did **not** simply “start good then get bad” on realized exits. It started badly and stayed net bad, with the worst damage early.
- Next checks:
  1. Rebuild BTC trades chronologically with entry reason, edge, and minutes-to-end.
  2. Separate `5m` vs `15m` BTC losses.
  3. Count how many BTC losses occurred in dead-zone-disabled hours.
  4. Check whether `updown_time_stop` losses cluster around specific edge bands like `0.095-0.115`.
  5. Compare BTC live losers against the latest `BTC 5m` backtest, which is very strong, to identify spec drift between live and backtest.

### 2.2 Check whether BTC 15m is under-validated
- Evidence:
  - Latest `BTC 15m` test-window report has `0` test trades in split mode.
- Next checks:
  1. Treat BTC 15m live behavior as effectively unvalidated by the current artifact.
  2. Re-run BTC 15m backtest on the exact active configuration without the split artifact issue.
  3. Verify current live 15m thresholds match the backtest path.

## 3. SOL

### 3.1 Verify whether SOL was genuinely okay or just suppressed
- Current evidence:
  - `sol_macro`: `6` trades, `+2.23`
  - Late cycles show repeated skips like `buy_yes_suppressed_bearish_1h` and later `price_too_far_from_even`.
- Interpretation:
  - SOL may look fine only because it mostly stopped trading when conditions worsened.
- Next checks:
  1. Split SOL into pre-suppression vs post-suppression windows.
  2. Count how many hypothetical valid SOL setups were blocked later by `buy_yes_suppressed_bearish_1h`.
  3. Compare active-session SOL skip reasons against the previous bad SOL session to determine whether suppression is actually protective.

### 3.2 Keep structural SOL weakness front and center
- Evidence:
  - Latest SOL backtests are still deeply negative on both `15m` and `5m`.
- Next checks:
  1. Do not treat this single positive session slice as vindication.
  2. Verify whether the few SOL winners came from unusually favorable entry prices rather than improved model quality.

## 4. ETH

### 4.1 Explain zero firing exactly
- Current evidence:
  - `eth_macro`: `0` trades
  - Active log repeatedly shows `abort_reason="btc_follow_1h_blocked"` or `ETH Macro strategy: BTC 1H continuation not strong enough`.
- Interpretation:
  - ETH was not positive and not active. It was hard-gated out.
- Next checks:
  1. Quantify how many ETH opportunities were considered but blocked.
  2. Compare ETH gating strictness to BTC/HYPE/XRP under the same regime.
  3. Decide whether ETH gate protected capital or incorrectly starved a valid lane.

### 4.2 Check ETH-specific over-gating
- Next checks:
  1. Audit `btc_follow_1h_blocked` and `eth_1h_bearish` frequency.
  2. Compare ETH gating thresholds to the live thresholds that still allowed BTC and HYPE to trade badly.
  3. Determine if ETH is the only lane behaving defensively while others overtrade.

## 5. HYPE

### 5.1 Audit HYPE deterioration
- Current evidence:
  - `hype_macro`: `18` trades, `-17.52`
  - Early half: `9` exits, `-2.5`, `55.6%` WR
  - Late half: `9` exits, `-15.025`, `33.3%` WR
- Interpretation:
  - HYPE really did deteriorate as the session went on.
- Next checks:
  1. Rebuild HYPE trade chronology around the point performance rolled over.
  2. Compare early profitable HYPE entries vs late losing ones by edge, correlation, and entry price.
  3. Determine whether low-corr logic or entry-price-band logic admitted too many bad late longs.

### 5.2 Check HYPE signal-quality drift
- Evidence from late-cycle diagnostics:
  - late skips include `edge_below_min`, `entry_price_band_updown`, `ai_hold_marginal_updown`, and heavy liquidity skips.
- Next checks:
  1. Determine whether actual losing HYPE trades were placed before these filters tightened.
  2. Check if HYPE late-session market quality worsened faster than the strategy reacted.

## 6. XRP

### 6.1 Audit XRP small-sample failure
- Current evidence:
  - `xrp_macro`: `4` trades, `-6.55`
  - Early half: `2` exits, `-4.95`, `0%` WR
  - Late half: `2` exits, `-1.597`, `50%` WR
- Interpretation:
  - Small sample, but still clearly negative.
- Next checks:
  1. Inspect every XRP entry for lag magnitude, BTC move, and minutes-to-end.
  2. Check whether the few entries that fired were exactly the wrong exceptions to `flat_btc_no_lag` gating.
  3. Compare active-session XRP entries against the latest positive `XRP 15m` backtest for threshold/spec drift.

## 7. Cross-Strategy Turning Point

### 7.1 Build a true session timeline
- Session-wide exits:
  - First half of exits: `-40.045`, `34.4%` WR
  - Second half of exits: `-16.3`, `59.4%` WR
- Interpretation:
  - The session did **not** simply begin well and then turn bad on realized exits. It began badly, then partially stabilized, but the damage was already done.
- Next checks:
  1. Plot cumulative PnL by exit index and by clock time.
  2. Mark regime changes on that curve.
  3. Identify whether your “started good then went bad” impression came from unrealized PnL, a specific lane, or an early small-sample streak before the first major losses printed.

### 7.2 Check dead-zone-disabled damage
- Evidence:
  - The active session contains `DEAD_ZONE_SKIP` hypothetical lines immediately before some real entries.
- Next checks:
  1. Count all trades that would have been skipped if dead-zone gating were enabled.
  2. Compute their realized PnL.
  3. Separate that from strategy logic failure so dead-zone does not become an all-purpose excuse.

## 8. Immediate Execution Order

1. Build scanner-vs-strategy timing timeline for the active session.
2. Reconstruct BTC trade chronology and split by `5m` vs `15m`.
3. Reconstruct HYPE early-vs-late trade deterioration.
4. Audit XRP trade exceptions against `flat_btc_no_lag` gating.
5. Quantify ETH gating suppression and decide if it is protective or over-strict.
6. Verify whether SOL only looks okay because it stopped firing.
7. Re-run the exact backtests most relevant to active damage:
   - `BTC 15m`
   - `BTC 5m`
   - `HYPE 15m`
   - `XRP 15m`

## Most Likely Themes
- **Theme 1:** `bitcoin` live path is diverging from the backtest, especially on `15m` and on `updown_time_stop` handling.
- **Theme 2:** `hype_macro` degrades materially later in session; this is the clearest “starts okay, ends bad” lane.
- **Theme 3:** `eth_macro` is not failing by losing trades; it is failing by being fully gated out.
- **Theme 4:** scanner health after restart looks materially better than before, so primary blame should currently stay on strategy behavior unless a timing audit disproves that.

## 9. Future Work — Scanner and Cycle Optimization

### 9.1 Scanner verdict as of 2026-05-07 21:35 PDT
- Scanner is **mostly optimized** and is not the current primary failure source.
- Active-session timing after `2026-05-07 19:41:37`:
  - Scanner network sync: `p50=3.98s`, `p90=4.55s`, `max=5.85s`
  - Full scanner including CLOB price hydration: `p50=11.42s`, `p90=12.05s`, `max=13.43s`
  - Crypto strategy phase: `p50=32.50s`, `p90=46.02s`, `max=65.69s`
- Active-session scanner health:
  - `updown_15m_count=45`
  - `updown_5m_count=20`
  - `updown_hype_alt_count=3`
  - no post-restart `Too many open files`
  - no post-restart scanner sync timeouts

### 9.2 Scanner optimizations to consider later
- Reduce CLOB price hydration cost; this is the main remaining scanner cost because network sync is about `4s` but full scanner is about `11s`.
- Consider a per-cycle price cache keyed by token ID so repeated hydration across scanner, exits, and strategy prep does not refetch identical prices.
- Consider using CLOB batch/orderbook endpoints if available and reliable enough to replace many `/midpoint` requests.
- Keep HYPE alt and weather discovery bounded/off-critical-path; do not reintroduce broad event crawls into the main cycle.

### 9.3 Higher-priority optimization
- Prioritize [[Strategy Evaluation]] latency before deeper scanner rewrites.
- The biggest cycle overrun source is repeated analysis/service calls inside BTC/SOL/ETH/HYPE/XRP `scan_and_analyze`.
- Future structural fix: build a shared per-cycle [[Market Context Cache]] for BTC spot/HTF, alt analysis, oracle basis, exposure state, and recent CLOB prices, then pass snapshots into strategies instead of letting each strategy refetch.
