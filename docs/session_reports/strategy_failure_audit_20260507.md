## Strategy Failure Audit — test_20260507_035930 — 2026-05-07

### Summary
- Active session: [`data/paper_trades/test_20260507_035930/summary.json`](/Users/mainfolder/Documents/psb-main%201/data/paper_trades/test_20260507_035930/summary.json)
- Session result: `-59.04` PnL in the session summary, `-56.34` to `-56.35` in the parsed/ongoing log snapshots depending on exact cutoff.
- Main damage:
  - `bitcoin`: roughly `-37.2`
  - `hype_macro`: `-17.52`
  - `xrp_macro`: `-6.55`
  - `eth_macro`: `0 trades`
  - `sol_macro`: `+2.23`
- Main conclusion: this does **not** currently look like a post-restart scanner collapse. The stronger evidence is strategy-specific failure, with `bitcoin` bleeding from the start, `hype_macro` degrading later, `eth_macro` fully gated out, and `sol_macro` appearing okay mainly because it stayed mostly suppressed.

### Findings

| ID | Severity | Area | Evidence | Notes |
|----|----------|------|----------|-------|
| F1 | high | `bitcoin` | active session journal + fresh backtests | BTC is the main loss engine and is diverging sharply from its latest strong 5m backtest, while 15m remains effectively unvalidated. |
| F2 | high | `hype_macro` | early-vs-late split | HYPE is the clearest lane that actually deteriorates as the session goes on. |
| F3 | medium | scanner vs strategy timing | active session log | Scanner looks materially healthier after restart; strategy phase remains slow. |
| F4 | medium | dead-zone counterfactual | matched `DEAD_ZONE_SKIP` vs live entries | Dead-zone-disabled trades account for meaningful BTC damage, but only part of the session loss. |
| F5 | medium | `eth_macro` gating | repeated log evidence | ETH is not failing by trading badly; it is fully gated out. |
| F6 | medium | `sol_macro` interpretation | session PnL plus skip diagnostics | SOL looks okay in this session mainly because it stayed mostly suppressed. |
| F7 | medium | `xrp_macro` | small-sample exit audit | XRP is small-sample negative and still exposed to the same `updown_time_stop` damage. |

## 1. Scanner Audit

### What looks fixed
- No active-session `Too many open files` lines after restart.
- Active-session market counts stayed stable in `OPS_JSON`:
  - `updown_15m_count`: `45`
  - `updown_5m_count`: `20`
  - `updown_hype_alt_count`: `3`
- Slug fetch stats stayed stable through the session:
  - `updown_5m`: `20/20` hit
  - `updown_15m`: `45/45` hit
  - `updown_hype_alt`: consistently `4/8` hit, `4` empty responses

### What still looks concerning
- Scanner phase itself is fine:
  - median `sync network phase`: about `3279ms`
  - range: `2943ms` to `19306ms`
- Strategy phase is much slower:
  - median `Crypto parallel scan phase complete`: about `30464ms`
  - range: `17120ms` to `81193ms`
- There were a few scanner-gamma warnings in the session window, but they were timeout retries, not FD exhaustion, and counts remained stable.

### Scanner conclusion
The active failure run is **not best explained by scanner degradation**. The stronger operational concern now is that **strategy evaluation is slow**, not that market discovery is collapsing.

## 2. Bitcoin Audit

### Session result
- `36` parsed exits
- Total parsed PnL: about `-34.50`
- `5m`: `24` exits, `-21.375`
- `15m`: `12` exits, `-13.123`

### Core failure shape
- `5m`:
  - win rate `50%`
  - avg win `+1.49`
  - avg loss `-3.27`
  - reasons: `12 take_profit`, `12 updown_time_stop`
- `15m`:
  - win rate `50%`
  - avg win `+1.45`
  - avg loss `-3.64`
  - reasons: `6 take_profit`, `4 updown_time_stop`, `2 RESOLVED:NO`

### Most important point
BTC did **not** mainly “start good then go bad” on realized exits. It was ugly from the start:
- early half of BTC exits: `-28.748`, `38.9%` WR
- late half of BTC exits: `-5.75`, `61.1%` WR

### Fresh backtest check
- Fresh `BTC 5m` rerun on `2026-04-01 -> 2026-04-20`:
  - report: [`backtest_crypto_BTC_5m_20260507_131604.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_BTC_5m_20260507_131604.json)
  - result: `63` trades, `82.5%` WR, `+316.57`
- Fresh `BTC 15m` rerun on the same range:
  - report: [`backtest_crypto_BTC_15m_20260507_131550.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_BTC_15m_20260507_131550.json)
  - result: `0` trades

### BTC conclusion
This is the clearest current spec-drift problem in the repo:
- live `BTC 5m` loses badly on payoff shape
- fresh `BTC 5m` backtest remains strongly positive
- live `BTC 15m` trades and loses, while fresh `BTC 15m` still does not validate any test-window entries

That points to a mismatch between live and backtest admission/exit behavior, not just “bad luck.”

## 3. HYPE Audit

### Session result
- `18` exits
- Total PnL: `-17.525`

### Early vs late
- early half:
  - `9` exits
  - `-2.5`
  - `55.6%` WR
- late half:
  - `9` exits
  - `-15.025`
  - `33.3%` WR

This is the lane that most clearly matches the intuition that the bot **starts okay then trades badly later**.

### Window breakdown
- `5m`: `7` exits, `-9.925`, `28.6%` WR
- `15m`: `11` exits, `-7.6`, `54.5%` WR

### Failure shape
- late HYPE was dominated by `updown_time_stop`
- average late win fell to about `+0.95`
- average late loss stayed near `-2.98`
- low-corr logic did not protect it from deterioration

### HYPE conclusion
HYPE is the clearest “degrades later” strategy in this session. The lane seems especially vulnerable once the regime shifts from cleaner early low-corr continuation into weaker/more marginal late setups.

## 4. Dead-Zone Counterfactual

### What was measured
Matched `DEAD_ZONE_SKIP` hypothetical events to the corresponding live entries in the active session without changing config.

### Result
- matched trades: `4`
- all matched trades were `bitcoin`
- combined realized PnL: `-11.05`

### Detail
- `Bitcoin Up or Down - May 7, 7:10AM-7:15AM ET`: `-3.75`
- `Bitcoin Up or Down - May 7, 7:15AM-7:30AM ET`: `+1.6`
- `Bitcoin Up or Down - May 7, 7:45AM-8:00AM ET`: `-5.1`
- `Bitcoin Up or Down - May 7, 7:45AM-7:50AM ET`: `-3.8`

### Dead-zone conclusion
Dead-zone-disabled trades are materially relevant in this session, but they are **not** the whole story:
- they explain about `-11.05`
- the session damage is much larger than that
- most of the broader failure still comes from strategy behavior outside this subset

This supports keeping dead zones **off for now** if the purpose is diagnosis, while still acknowledging that the current data is building evidence that the blocked hours matter.

## 5. ETH Audit

### Session result
- `0` trades

### Why it never fired
The logs show ETH was consistently blocked by regime gates:
- repeated `ETH Macro strategy: BTC 1H continuation not strong enough`
- repeated `ETH Macro SCAN_DIAG ... skips_top6={'outside_entry_window': ..., 'eth_1h_bearish': ...}`
- `OPS_JSON` shows `cumulative_signal_counts.eth_macro: 0`

### ETH conclusion
ETH is not part of the loss stack because it never traded. The open question is whether ETH’s stricter gating is **correctly defensive** or whether it is **too strict relative to the looser lanes that were still allowed to lose heavily**.

## 6. SOL Audit

### Session result
- `6` exits
- Total PnL: `+2.225`

### Why this is not a clean win
- early half of SOL exits: `+4.75`
- late half of SOL exits: `-2.525`
- later skip diagnostics increasingly show suppression:
  - `buy_yes_suppressed_bearish_1h`
  - `price_too_far_from_even`
  - `iql_15m_reject`

### SOL conclusion
SOL does not look “fixed.” It looks **less exposed**. This positive session slice is likely explained more by reduced participation than by a trustworthy edge recovery.

## 7. XRP Audit

### Session result
- `4` exits
- Total PnL: `-6.548`

### Shape
- early half: `-4.95`, `0%` WR
- late half: `-1.597`, `50%` WR
- reasons: `3 updown_time_stop`, `1 take_profit`

### XRP conclusion
XRP is still showing the same family of problem as BTC/HYPE, just on a tiny sample: weak payoff asymmetry with `updown_time_stop` doing the real damage.

## 8. Timing Correlation Check

### What was measured
Linked exits to the latest prior `Crypto parallel scan phase complete` event when the lag was within 15 minutes.

### Result
- linked sample: `24` exits
- exits after very slow phases (`>=45s`): `3` exits, combined PnL `-0.45`
- exits after fast phases (`<25s`): `7` exits, combined PnL `-11.55`

### Timing conclusion
There is **not yet a clean direct correlation** showing that the worst realized losses happened only after the slowest strategy phases. Slow phase time is still a risk factor, but the active losses are better explained by strategy behavior than by latency alone.

## Most Likely Bugs / Miscalculations / Spec Drift

1. **BTC live path is diverging from its recent strong 5m backtest.**
   This is now stronger than a hunch; the fresh rerun still looks good while live 5m loses badly.

2. **BTC 15m is effectively unvalidated while still trading live.**
   Fresh rerun still returns `0` trades in the test window, but live 15m is clearly active.

3. **HYPE admission quality deteriorates materially later in the session.**
   This looks like a lane-specific regime/admission problem, especially on 5m and late-session 15m.

4. **ETH may be over-gated while weaker lanes remain under-gated.**
   ETH is fully suppressed by 1H continuation logic while BTC/HYPE are still allowed to lose repeatedly.

5. **SOL’s apparent improvement may be a false positive caused by suppression.**
   Reduced firing can improve a session without proving the lane is healthy.

## Prioritized Next Steps
1. Audit BTC live vs backtest parity line-by-line, especially:
   - entry price bands
   - time-window logic
   - `updown_time_stop` exit handling
2. Audit HYPE live entry quality drift by time of day and correlation regime.
3. Keep dead zones off for now, but continue tagging every matched hypothetical/live pair and accumulate that evidence.
4. Decide whether ETH’s stricter gate should become the template for the weaker long lanes.

## 9. BTC Live vs Backtest Parity

### Main parity break
The current `BTC` backtest is still not testing the same exit regime as live.

- Live `bitcoin` is dominated by `updown_time_stop` losses:
  - `5m`: `12` `updown_time_stop` exits for `-39.25`
  - `15m`: `4` `updown_time_stop` exits for `-11.523`
- The backtest engine still documents and implements `hold to settlement`:
  - [`src/backtest/updown_engine.py:45`](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py:45)
  - [`src/backtest/updown_engine.py:1716`](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py:1716)

That means the strong fresh `BTC 5m` backtest is not a like-for-like validation of the live path that is actually losing money.

### Admission mismatch
Live `bitcoin` applies updown-specific market-price filters that the backtest does not mirror in the same way:

- live edge cap:
  - [`src/strategies/bitcoin.py:1692`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py:1692)
- live updown entry-price band:
  - [`src/strategies/bitcoin.py:1720`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py:1720)
- backtest explicitly skips the live edge cap because it assumes perfect `YES=0.50` pricing:
  - [`src/backtest/updown_engine.py:72`](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py:72)

This is not a small detail. Live is trading against real market prices around `0.445` to `0.525`, while backtest edge is still a signal-strength score against synthetic centered pricing.

### Active-session BTC trade shape
- `BTC 5m`: `24` exits, `-21.375`
  - `edge<=0.095`: `7` exits, `-4.95`
  - `0.095<edge<=0.105`: `13` exits, `-10.3`
  - `edge>0.105`: `4` exits, `-6.125`
  - `price<0.47`: `6` exits, `-15.0`
  - `time_stop`: `12` exits, `-39.25`
  - `minutes_to_market_end<=5`: `9` exits, `-14.7`
- `BTC 15m`: `13` exits, `-11.923`
  - `0.095<edge<=0.105`: `2` exits, `-8.3`
  - `edge>0.105`: `11` exits, `-3.622`
  - `price>0.50`: `5` exits, `-9.05`
  - `time_stop`: `4` exits, `-11.523`

### BTC conclusion
The strongest current BTC problem is not just bad threshold tuning. It is parity drift:

1. live exits are path-dependent and lose heavily on `updown_time_stop`
2. backtest exits are still mostly expiry-settlement simulations
3. live trades around ordinary-looking edges still lose badly
4. fresh `BTC 15m` backtest still has `0` validation-window trades while live `15m` trades and loses

## 10. HYPE Admission Drift

### Core result
HYPE does deteriorate later, but not because later entries have obviously lower nominal edge.

- early half: `9` exits, `-2.5`, `55.6%` WR
- late half: `9` exits, `-15.025`, `33.3%` WR

### Early vs late by window
- early `5m`: `3` exits, `-3.6`, avg edge `0.1183`, avg corr `0.2973`, `2` time-stops
- late `5m`: `4` exits, `-6.325`, avg edge `0.1231`, avg corr `0.5509`, `3` time-stops
- early `15m`: `6` exits, `+1.1`, avg edge `0.1418`, avg corr `0.1162`, `1` time-stop
- late `15m`: `5` exits, `-8.7`, avg edge `0.1355`, avg corr `0.1325`, `3` time-stops

### What changed
Late HYPE did not get obviously weaker on quoted edge. It got worse on outcome path:

- more `updown_time_stop`
- worse realized payoff despite similar entry prices
- no clear protection from higher `corr_1h`

The early winning HYPE sample also includes very low-correlation entries:
- example winner:
  - `15m`, `entry_price=0.45`, `edge=0.1423`, `corr_1h=0.154`
- later losses still clear hard edge bars but fail on path/timing rather than admission math alone

### HYPE conclusion
HYPE currently looks like a regime/path problem more than a simple min-edge problem. The lane can still admit trades with healthy-looking edges and then degrade into time-stop losses later in the session.

## 11. ETH Gate Review

### Product reality
Your objection is correct: a lane that runs all night and never trades is not a viable answer.

In this session:
- `eth_macro`: `0` trades
- journal rows: `0`
- cumulative signal count stays `0`

### What blocked ETH
There are two different ETH blockers in the active session:

1. whole-scan abort when BTC 1H continuation is not strong enough
   - [`src/strategies/eth_macro.py:500`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py:500)
   - active-session log count: `btc_follow_1h_blocked = 327`
2. per-market rejection when `BUY_YES` conflicts with ETH 1H bearish trend
   - [`src/strategies/eth_macro.py:687`](/Users/mainfolder/Documents/psb-main%201/src/strategies/eth_macro.py:687)
   - active-session aggregated skip count across `OPS_JSON`: `eth_1h_bearish = 331`

Aggregated `OPS_JSON` top ETH skips for `test_20260507_035930`:
- `outside_entry_window`: `2347`
- `eth_1h_bearish`: `331`
- `price_too_far`: `189`
- `eth_5m_weak_confirm`: `31`
- `btc_min_move_dollars`: `21`

### ETH conclusion
ETH is not proving itself safe. It is proving itself over-gated.

The current gate stack can suppress the strategy at two levels:
1. abort the whole scan on BTC 1H continuation
2. reject the remaining long setups on ETH 1H bearish alignment

That may be defensible as a forensic safety layer, but it is not acceptable as a live product behavior if the lane stays silent for an entire overnight session.

## 12. Follow-Up Execution

### BTC backtest parity patch
- Implemented a crypto up/down backtest change in [`src/backtest/updown_engine.py`](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py) so the backtest now replays approximate near-expiry `updown_time_stop` exits from 1m bars before falling back to settlement.
- Added focused regressions in [`tests/test_updown_backtest_parity.py`](/Users/mainfolder/Documents/psb-main%201/tests/test_updown_backtest_parity.py): `15 passed`.

### BTC reruns after the patch
- `BTC 5m` rerun:
  - [`backtest_crypto_BTC_5m_20260507_133311.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_BTC_5m_20260507_133311.json)
  - `62` trades, `83.9%` WR, `+$360.81`
- `BTC 15m` rerun:
  - [`backtest_crypto_BTC_15m_20260507_133010.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_BTC_15m_20260507_133010.json)
  - `0` trades

### What that means
The BTC backtest is less unrealistic than the first broken TP proxy pass, but it is still much stronger than live. So the time-stop parity patch was necessary, not sufficient.

### HYPE regime cut
HYPE regime attribution from the active session:
- `BEAR 5m`: `7` exits, `-9.925`, `28.6%` WR, `5` time-stops
- `BEAR 15m`: `10` exits, `-3.6`, `60%` WR, `3` time-stops
- `RANGE 15m`: `1` exit, `-4.0`, `0%` WR, `1` time-stop

This confirms the earlier read that the HYPE damage is concentrated in `5m` path behavior under the prevailing bearish BTC regime rather than in obviously low quoted edge.

### ETH gate loosening
- Live config change applied:
  - [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml) `strategies.eth_macro.btc_follow_1h_required: false`
- Backtest parity updated so ETH backtest now respects that same flag in [`src/backtest/updown_engine.py`](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py).
- Short validation reruns after the change:
  - [`backtest_crypto_ETH_5m_20260507_133409.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_ETH_5m_20260507_133409.json): `0` trades
  - [`backtest_crypto_ETH_15m_20260507_133407.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_ETH_15m_20260507_133407.json): `0` trades

### ETH meaning
Removing the whole-scan BTC 1H abort was still the right change for live silence, but the short backtest slice staying at `0` trades says the lane is also starved by other downstream gates or by the test-window setup itself. So ETH silence is broader than one abort switch.

## 13. Deeper Follow-Up Audits

### BTC 15m parity gap is larger than just exits

The backtest still under-models the live `BTC 15m` admission path in two important ways:

1. **Backtest 15m edge has no timing bonus**
   - live `bitcoin.py` 15m entries include timing reasons like `15m early DRIFT_UP`, `15m predict window`, and sometimes `5m predict window`
   - backtest [`src/backtest/updown_engine.py:973`](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py:973) still documents `timing_bonus = 0 in backtest`

2. **Backtest 15m uses a stricter histogram gate than live**
   - backtest hard-gates out LONG when 4H histogram is not rising:
     - [`src/backtest/updown_engine.py:950`](/Users/mainfolder/Documents/psb-main%201/src/backtest/updown_engine.py:950)
   - live allows a 1H recovery pass when 4H is falling but 1H is rising:
     - [`src/strategies/bitcoin.py:1254`](/Users/mainfolder/Documents/psb-main%201/src/strategies/bitcoin.py:1254)

This is a direct explanation for why live `BTC 15m` can trade while the backtest still shows `0` trades.

### BTC 15m active-session loser shape
The live `BTC 15m` losers were admitted on ordinary-looking bullish continuation reasons, not on extreme edge:

- repeated `edge` cluster: about `0.10-0.115`
- repeated reasons:
  - `15m hist rising`
  - `15m MACD>signal`
  - `15m early DRIFT_UP`
  - `15m predict window`
- some losers were admitted with negative 15m MACD histogram but “rising” direction only

That means the live 15m issue is not just one bad tail trade. It is the actual continuation logic and/or timing bonus stack.

### HYPE 5m bearish-regime path

The failing HYPE 5m trades are highly consistent:

- all `BUY_YES`
- all under `btc_1h_regime = BEAR`
- many still allowed with `ALT_HTF=NEUTRAL` or `ALT_HTF=BEARISH`
- several specifically tagged `low_corr_5m(...)`
- losing entries still clear edge:
  - `0.1075`
  - `0.1125`
  - `0.1275`
  - `0.1350`
  - `0.1450`

The weak path is therefore:
1. BTC macro says bullish enough to trade LONG
2. HYPE 5m allows the trade even in bearish or neutral alt regime
3. low-correlation conditions do not suppress entry
4. trade bleeds into `updown_time_stop`

The strongest concrete candidate gate behind this is still HYPE running with:
- `enforce_alt_1h_alignment: false`
- no hard low-correlation suppression

### ETH downstream suppression after removing scan-abort

After disabling the whole-scan BTC 1H abort, ETH is still being suppressed mainly by:

- `outside_entry_window`: `2489`
- `eth_1h_bearish`: `365`
- `price_too_far`: `195`
- `eth_5m_weak_confirm`: `31`

Interpretation:
- `outside_entry_window` is the biggest raw count, but that is also common across lanes because most scanned markets are simply not tradable on that cycle
- the **real ETH-specific suppressor** is still `eth_1h_bearish`
- after that, `price_too_far` is the next meaningful structural filter

So ETH is not just being killed by the old abort. It is still mostly blocked by its per-market bearish 1H alignment rule.

## 14. Next Intervention Pass Results

### BTC 15m parity patch result
- New BTC 15m rerun after adding timing bonus + 1H recovery parity:
  - [`backtest_crypto_BTC_15m_20260507_134222.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_BTC_15m_20260507_134222.json)
  - `82` trades, `68.3%` WR, `+$325.05`

This confirms the earlier BTC 15m backtest was materially under-modeling the live admission path. The backtest is now at least generating the same class of entries.

### HYPE 5m tightening applied
- Config change:
  - [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml)
  - `strategies.hype_macro.require_btc_catalyst_5m: true`

This is intentionally narrow: it targets the failing HYPE 5m lane only.

### ETH downstream loosening applied
- Config change:
  - [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml)
  - `strategies.eth_macro.enforce_alt_1h_alignment: false`

### ETH post-loosening replay result
- [`backtest_crypto_ETH_15m_20260507_134220.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_ETH_15m_20260507_134220.json): `0` trades
- [`backtest_crypto_ETH_5m_20260507_134222.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_ETH_5m_20260507_134222.json): `0` trades

### Meaning
ETH remains starved even after removing both:
1. whole-scan BTC 1H abort
2. per-market ETH 1H bearish suppression

That means the next ETH suppressors are likely entry-window timing and ETH-specific admission quality filters like `price_too_far`, `eth_5m_weak_confirm`, and centered-price/catalyst logic.

## 15. Live Process Status Check

The current live bot has **not** loaded the latest ETH config changes yet.

Evidence from the active log:
- [`data/logs/polybot_20260507.log`](/Users/mainfolder/Documents/psb-main%201/data/logs/polybot_20260507.log)
- Repeated live `ETH Macro SCAN_DIAG` lines at `13:38-13:47 PT` still print `enforce_alt_1h=True`
- Current config on disk is now `strategies.eth_macro.enforce_alt_1h_alignment: false` in [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml)

Meaning:
- the ETH all-night silence in the still-running process is measuring the **old** gate stack
- the HYPE 5m `require_btc_catalyst_5m: true` tightening is also not yet proven live until restart
- any further live attribution without restart will mix strategy diagnosis with stale in-memory config

This does **not** change the earlier strategy findings, but it does mean the most recent ETH/HYPE config interventions have not been operationally tested in the live paper process yet.

## 16. Full Intervention: SOL and XRP

### SOL intervention
- Config change:
  - [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml)
  - `strategies.sol_macro.require_btc_catalyst_5m: true`

### SOL why
- In `test_20260507_035930`, every closed SOL trade was `5m` `BUY_YES`
- Session result was only `+$1.00` on `7` closes, with:
  - `3x take_profit`
  - `3x updown_time_stop`
  - `1x RESOLVED:NO`
- Broader evidence remains structurally bad:
  - [`backtest_crypto_SOL_5m_20260504_134700.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_SOL_5m_20260504_134700.json): `933` trades, `-486.225`
  - [`backtest_crypto_SOL_15m_20260505_034745.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_SOL_15m_20260505_034745.json): `462` trades, `-403.2`

### SOL meaning
This is not a lane that earned more freedom. The intervention is deliberately narrow: tighten the failing `5m` path without using dead zone as the explanation.

### XRP intervention
- Config changes:
  - [`config/settings.yaml`](/Users/mainfolder/Documents/psb-main%201/config/settings.yaml)
  - `strategies.xrp_macro.enforce_alt_1h_alignment: true`
  - `strategies.xrp_macro.require_btc_catalyst_5m: true`

### XRP why
- In `test_20260507_035930`, all `4` XRP closes were `15m` `BUY_YES`
- Net PnL: `-6.55`
- Exit mix:
  - `3x updown_time_stop`
  - `1x take_profit`
- Every live XRP entry in the bad run had `ALT_HTF=BEARISH`
- Shared strategy code in [`src/strategies/sol_macro.py`](/Users/mainfolder/Documents/psb-main%201/src/strategies/sol_macro.py) confirms `enforce_alt_1h_alignment=true` suppresses bearish-1H `BUY_YES` longs while still allowing `BUY_NO` diagnostically
- XRP `5m` still does not deserve a relaxed path:
  - [`backtest_crypto_XRP_5m_20260506_170551.json`](/Users/mainfolder/Documents/psb-main%201/data/backtest/reports/backtest_crypto_XRP_5m_20260506_170551.json): `770` trades, `-492.825`

### XRP meaning
The live problem is not broad XRP randomness. It is specifically `15m` long-side participation against bearish alt context. Restoring bearish-1H long suppression is the cleaner intervention than squeezing price bands again.
