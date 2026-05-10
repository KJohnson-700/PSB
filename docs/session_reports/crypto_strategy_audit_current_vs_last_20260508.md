## Strategy test review — crypto current vs last sessions — 2026-05-08

### Summary

- Current paper session `test_20260508_050455`: `78` closed exits, `55.1%` WR, `+$5.20` realized PnL.
- Last comparable session `test_20260507_215616`: `62` closed exits, `59.7%` WR, `+$24.00` realized PnL.
- Prior failure baseline `test_20260507_035930`: `69` closed exits, `49.3%` WR, `-$50.47` realized PnL.
- The global stop change is directionally working: prior failure had `31` `updown_time_stop` exits for `-$89.32`; current session has only `1` `updown_time_stop`, with losses mostly capped by `updown_stop_loss`.
- Remaining drag is concentrated in `eth_macro`, `xrp_macro`, and flat `bitcoin`; `hype_macro` and `sol_macro` are carrying the current session.

### Findings

| ID | Severity | Area | Evidence | Notes |
|----|----------|------|----------|-------|
| F1 | high | exit logic | `data/paper_trades/test_20260507_035930/summary.json` shows `-$50.47`; current parsed exits show only `1` `updown_time_stop` vs `31` in the failure run | The new global `updown_stop_loss_pct: 0.20` is doing its job; do not revert it. |
| F2 | high | `bitcoin` | Current `bitcoin`: `32` trades, `50.0%` WR, `-$0.44`; last session: `31` trades, `61.3%` WR, `+$23.20` | BTC lost the edge despite similar volume; strongest filter clue is RSI band and centered entry price. |
| F3 | high | `eth_macro` | Current `eth_macro`: `16` trades, `43.8%` WR, `-$3.59`; last session: `18` trades, `50.0%` WR, `-$0.78` | ETH is no longer starved, but added flow is negative. |
| F4 | medium | `xrp_macro` | Current `xrp_macro`: `8` trades, `37.5%` WR, `-$1.95`; last session only `2` trades, both winners | XRP participation expanded before the lane proved stable. |
| F5 | medium | `hype_macro` | Current `hype_macro`: `18` trades, `72.2%` WR, `+$3.30`; last session: `11` trades, `63.6%` WR, `-$0.15` | HYPE improved, but losses still cluster in low-correlation / low-RSI exceptions. |
| F6 | medium | backtest/live drift | `src/backtest/updown_engine.py` documents backtests assume YES `0.50` and intentionally skip live `max_edge_updown`; current live outcomes are highly entry-price sensitive | Backtest results should not be used as direct live tuning proof without entry-price cohort replay. |
| F7 | low | `sol_macro` | Current `sol_macro`: `4` trades, `100%` WR, `+$7.88` | Good signal, but sample is too small to expand risk. |

### Current vs last session deltas

| Strategy | Current WR / PnL | Last WR / PnL | Change | Read |
|----------|------------------|---------------|--------|------|
| `bitcoin` | `50.0%`, `-$0.44` | `61.3%`, `+$23.20` | `-11.3pp`, `-$23.64` | Main regression from last test. |
| `eth_macro` | `43.8%`, `-$3.59` | `50.0%`, `-$0.78` | `-6.2pp`, `-$2.81` | Active but still negative. |
| `hype_macro` | `72.2%`, `+$3.30` | `63.6%`, `-$0.15` | `+8.6pp`, `+$3.45` | Improved; keep active. |
| `xrp_macro` | `37.5%`, `-$1.95` | `100%`, `+$1.73` | sample expanded from `2` to `8` | Needs tighter calibration mode. |
| `sol_macro` | `100%`, `+$7.88` | `0 trades` | new contribution | Do not infer durability yet. |

### Cohort observations

- `bitcoin` current session:
  - RSI `<45`: `16` trades, `75%` WR, `+$13.68`.
  - RSI `45-55`: `16` trades, `25%` WR, `-$14.12`.
  - Entry `.49-.51`: `9` trades, `77.8%` WR, `+$7.12`.
  - Entry `.45-.49`: `16` trades, `37.5%` WR, `-$5.13`.
  - Edge `.08-.10`: `18` trades, `61.1%` WR, `+$9.77`.
  - Edge `.10-.12`: `14` trades, `35.7%` WR, `-$10.22`.
- `eth_macro` current session:
  - `5m`: `9` trades, `33.3%` WR, `-$2.23`.
  - `15m`: `7` trades, `57.1%` WR, `-$1.36` because one real resolution loss dominated.
  - Corr `.25-.5`: `9` trades, `33.3%` WR, `-$7.87`.
  - Edge `.08-.10`: `2` trades, `0%` WR, `-$3.19`.
- `hype_macro` current session:
  - `15m` only: `18` trades, `72.2%` WR, `+$3.30`.
  - Corr `.25-.5`: `9` trades, `77.8%` WR, `+$4.89`.
  - Corr `0-.25`: `8` trades, `62.5%` WR, `-$2.48`.
  - Entry `<.45`: `1` trade, `-$2.77`; avoid treating discounted YES as automatically attractive.
- `xrp_macro` current session:
  - `5m`: `2` trades, `0%` WR, `-$1.98`.
  - `15m`: `6` trades, `50%` WR, about flat.
  - RSI `55-65`: `6` trades, `16.7%` WR, `-$3.01`.
  - Corr `<0`: `2` trades, `0%` WR, `-$2.52`.

### Likely bugs / miscalculations

1. **Backtest/live comparability is still weak.** The backtest engine explicitly assumes entry at `YES=0.50` and does not apply the live `max_edge_updown` cap. Current live cohorts show entry price and edge band matter materially, so current JSON backtests can mislead if used as direct threshold proof.
2. **BTC high-edge live trades are not actually better.** In the current paper data, `.10-.12` edge trades underperformed `.08-.10`, which suggests the edge model may be overestimating when market price is discounted or RSI is neutral.
3. **ETH loosened from zero-trade starvation into negative calibration flow.** The change recovered participation, but the current evidence says to limit ETH 5m and low-edge entries until the cohort improves.
4. **XRP 5m should remain diagnostic only.** Current 5m XRP was `0/2` and the latest stored `XRP 5m` backtest is strongly negative.

### Suggested improvements (prioritized)

1. **Keep the new stop-loss regime.** `updown_stop_loss_pct: 0.20` plus earlier stop handling reduced the old time-stop bleed. Do not revert to the wider/death-window-only stop model.
2. **Tighten BTC neutral-RSI entries.** Add or test a BTC 5m/15m gate that requires extra confirmation when RSI is `45-55`; current cohort was `25%` WR and `-$14.12`.
3. **Do not raise BTC edge blindly.** Current `.10-.12` edge trades were worse than `.08-.10`; tune by RSI/entry-price context, not just higher `min_edge`.
4. **Make ETH 5m calibration smaller or stricter.** ETH 5m is active but negative; raise `eth_macro.min_edge_5m` from `0.085` to `0.10` or lower `calibration_size_multiplier_5m` until it has a positive 20-trade cohort.
5. **Constrain ETH moderate-correlation entries.** Current ETH corr `.25-.5` was `9` trades, `-$7.87`; require stronger BTC/ETH follow or higher edge in that corr band.
6. **Freeze or reduce XRP 5m.** Keep `require_btc_catalyst_5m: true`, but also consider raising `xrp_macro.min_edge_5m` back toward `0.085-0.09` or setting 5m size multiplier lower than ETH.
7. **Keep HYPE 15m active, but block the weakest low-corr exception.** Current HYPE improved; the adjustment should be surgical: suppress corr `0-.25` unless entry is centered or edge is meaningfully above `0.12`.
8. **Do not expand SOL risk yet.** Four wins are useful, but not enough for a sizing change. Keep collecting until at least 15 closed trades.
9. **Add entry-price cohort replay to the backtest report.** Stored backtests should emit performance by simulated/live fill band, or replay against `data/entry_prices/updown_fills.jsonl`, before strategy thresholds are tuned from backtest JSON.

### Metadata / Summary

- **Tags:** #PSB #PolymarketBot #CryptoStrategies #BacktestAudit #PaperTrading
- **Related Concepts:** [[Strategy Evaluation]], [[Updown Exit Logic]], [[BTC RSI Gate]], [[ETH Macro]], [[XRP Macro]], [[HYPE Macro]], [[Backtest Live Drift]]
- **Summary:** Current crypto paper performance is much better than the prior failure run because the stop-loss regime capped the old `updown_time_stop` bleed. The highest-leverage next adjustments are BTC neutral-RSI filtering, ETH/XRP 5m risk reduction, and backtest/live cohort alignment around entry price.
