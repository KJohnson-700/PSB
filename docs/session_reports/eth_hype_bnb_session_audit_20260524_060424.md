## Strategy test review — ETH/HYPE/BNB session `test_20260524_060424` — 2026-05-24

### Summary

- Raw journal has `200` entries and `200` exits. The saved summary reported `199/199` because one valid `hype_macro` BUY_NO winner was filtered as a phantom.
- `eth_macro` really was broken in this session: `3` closed, `0` wins, `-$20.84`, all `5m` BUY_NO, all hour `11` PT, same lane `eth_macro|5m|down|bearish__bearish__bull|standard`.
- `hype_macro` was bad but not as broken as the saved summary showed: corrected raw stats are `3` closed, `1` win, `-$6.00`. All were `15m` BUY_NO in the same hour/lane, with low correlation tags.
- `bnb_macro` was not structurally broken from this sample: `28` closed, `9` wins, `-$0.79`. The weak spot was early hour PT `7`: `12` trades, `1` win, `-$35.06`; later BNB trades recovered most of it.
- Recommendation: fix the journal accounting bug now; do not tighten ETH/HYPE/BNB strategy gates from this session alone. ETH/HYPE need more data because the closed samples are only `3` each.

### Findings

| ID | Severity | Area | Evidence | Notes |
|----|----------|------|----------|-------|
| F1 | high | Journal accounting | `summary.json` reports HYPE `2` trades / `0` wins / `-$11.19`; raw `entries.jsonl` has HYPE winner `dry_1779646562.235316` at entry line `1328`, exit line `1335`, `take_profit`, `+$5.1899`. | Legacy phantom filter dropped a valid long-NO close because `0.395 + 0.600 ~= 1.0`. |
| F2 | medium | ETH sample quality | ETH closed trades: `3`, all losses, all `5m`, all hour `11` PT, all `updown_stop_loss`. | This is a real bad cluster, but too small for parameter tuning. |
| F3 | medium | HYPE sample quality | Corrected HYPE closed trades: `3`, `1` win, `-$6.00`, all `15m`, all hour `11` PT, all low-correlation. | Low-corr HYPE is suspicious, but sample is too small to tighten. |
| F4 | low | BNB regime/time cluster | BNB hour `7` PT: `12` trades, `1` win, `-$35.06`; hours `8-11` combined were positive. | Monitor hour-of-day clustering before changing gates. |

### Likely Bugs / Miscalculations

1. **Fixed:** phantom-exit filtering was too broad in session parsing and journal reload/list summaries. Token-flip detection now requires a YES entry leg; BUY_NO complementary prices are retained.
2. **Fixed broadly:** older attribution/learning scripts now call the same leg-aware phantom helper instead of duplicating the unconditional complement-price heuristic.

### Strategy Observations

- ETH/HYPE results are dominated by one small cluster around `18:16Z-18:52Z` / `11 PT`. This is not enough evidence for a durable strategy adjustment.
- BNB's headline looked worse than its actual expectancy: average win `+$7.46`, average loss `-$3.58`, near-flat total PnL. The problem was path clustering, not a broad lane failure.
- BTC-secondary context was not clearly protective or harmful here. ETH/HYPE both traded against BTC bullish HTF, but the sample size is too small; BNB had many `diag_flat_btc` entries and still ended near breakeven.

### Suggested Improvements

1. Fixed measurement: BUY_NO phantom filtering is now leg-aware.
2. Fixed coverage posture: paper daily cap is `2000`; ETH/HYPE/BNB lanes now use `0.3x` calibration sizing; HYPE and BNB oracle basis caps were widened to cover the observed block clusters.
3. Track ETH/HYPE/BNB by candidate events, unique markets, entry rate, hour PT, oracle basis bucket, and lane id in the next session.
4. Do not treat ETH/HYPE 15m/1h quality as proven: cached replay after an aggressive edge-loosen attempt produced high trade count but bad WR, so edge floors were kept unchanged.

### Applied Coverage Fix

- `eth_macro`: kept edge floors unchanged; widened oracle basis cap from `10/15` bps to `20/30` bps; set all 5m/15m/1h lane size multipliers to `0.3x`.
- `hype_macro`: kept edge floors unchanged; widened oracle basis cap from `25/30` bps to `40/60` bps; set all 5m/15m/1h lane size multipliers to `0.3x`.
- `bnb_macro`: kept edge floors unchanged; widened oracle basis cap from `18/22` bps to `30/40` bps; set 15m/1h lane size multipliers to `0.3x` to match existing 5m calibration sizing.
- Backtest note: BNB is not supported by `scripts/run_backtest_crypto.py`; ETH/HYPE cached replays were used only as a coverage/quality sanity check, not as validation.

### Metadata / Summary

**Tags:** #PSB #StrategyAudit #JournalAccounting #ETHMacro #HYPEMacro #BNBMacro

**Related Concepts:** [[Journal Phantom Filter]], [[BUY_NO Accounting]], [[ETH Macro]], [[HYPE Macro]], [[BNB Macro]], [[Paper Session Audit]]

**Summary:** Session `test_20260524_060424` contained one real measurement bug: valid HYPE BUY_NO take-profit was excluded by an overbroad phantom filter. ETH and HYPE need more data before strategy adjustment; BNB was near breakeven overall, with one early time cluster causing most drawdown.
