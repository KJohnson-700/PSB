## Ghost Session Assessment — `test_20260528_042826`

### Scope

- **Paper session:** `test_20260528_042826`
- **Window:** `2026-05-28T11:30:44.830168+00:00` to `2026-05-28T21:03:51.349823+00:00`
- **Paper summary:** `126` entries, `124` exits, realized PnL `+38.81`, total PnL `+39.73`
- **Ghost slice:** `15,625` settled rejected candidates whose `ts` fell inside the same session window

### Session Read

- The session made money live, but the ghost log still shows **net protective value** overall: `protected_loss_pct 7810.0` vs `missed_ev_pct 7393.2256`, for **`net_gate_value_pct +416.7744`**.
- That means the answer is **not** “loosen everything.” The profitable changes are concentrated in a few **long-side** rejection families.
- Strategy-level ghost net:
  - **Most missed upside:** `xrp_macro -322.791`, `hype_macro -231.493`
  - **Mixed / slightly too strict on some longs:** `sol_macro -231.255`
  - **Net protective / do not broadly loosen:** `bnb_macro +386.382`, `doge_macro +253.609`, `eth_macro +71.635`, `bitcoin +28.178`

### Live Loss Diagnosis For `bitcoin`, `eth_macro`, `sol_macro`

The ghost slice only explains rejected trades. The actual session losers came from **filled** trades with repeated structural patterns:

- **`bitcoin` live session PnL:** `-10.68`
- **`eth_macro` live session PnL:** `-18.34`
- **`sol_macro` live session PnL:** `+3.17` overall, but its **15m and 1h shorts lost** while 5m native shorts carried the strategy positive

#### `bitcoin`

- Window split:
  - `5m`: `n=21`, PnL `-8.95`
  - `15m`: `n=21`, PnL `-0.73`
  - `1h`: `n=7`, PnL `-1.00`
- Side-source split:
  - `btc_quant_disagree_flip`: `n=15`, PnL `-25.31`
  - `btc_htf_bias`: `n=34`, PnL `+14.64`
- Conflict split:
  - `long_to_short_quant_disagree`: `n=14`, PnL `-35.63`
  - `none`: `n=34`, PnL `+14.64`
- Worst lane:
  - `bitcoin|5m|down|bearish|htf_bearish_side_long_quant_short`: `n=10`, PnL `-25.60`

**Interpretation:** the BTC bleed was primarily the **quant-disagree short path**, especially 5m. The clean HTF-bias short path was actually positive.

#### `eth_macro`

- Window split:
  - `5m`: `n=6`, PnL `-10.76`
  - `15m`: `n=12`, PnL `-7.58`
- Action split:
  - `BUY_NO`: `n=9`, PnL `-22.63`
  - `BUY_YES`: `n=9`, PnL `+4.29`
- Worst lane:
  - `eth_macro|5m|down|bearish__bearish__bull|eth_5m_native`: `n=3`, PnL `-13.28`
- Repeated loser pattern:
  - bearish ETH short entries with `btc_1h_regime=BULL`, often at **very cheap NO-side prices** (`entry_price ~0.24–0.31`)
- Additional long-side loser pattern:
  - 15m `BUY_YES` entries with `primary_htf_bias=BULLISH`, `alt_htf_bias=NEUTRAL`, **`btc_htf_bias=BEARISH`**, and **RSI > 70**

**Interpretation:** ETH was not simply “too tight.” It was entering both:
1. low-priced 5m bearish fades that kept stopping out, and
2. overbought 15m longs with bearish BTC context and only neutral ETH higher-timeframe support.

#### `sol_macro`

- Overall session PnL was positive, but by window:
  - `15m`: `n=4`, PnL `-6.76`
  - `1h`: `n=1`, PnL `-3.59`
  - `5m`: `n=7`, PnL `+13.51`
- Side-source split:
  - `sol_15m_native`: `n=4`, PnL `-6.76`
  - `sol_1h_native`: `n=1`, PnL `-3.59`
  - `sol_5m_native`: `n=3`, PnL `+20.04`
  - `sol_5m_vs_slower`: `n=2`, PnL `-4.62`
- Conflict split:
  - `alt_macro_quant_disagree`: `n=3`, PnL `-2.76`
  - `aligned`: `n=9`, PnL `+5.93`

**Interpretation:** SOL’s issue in this session was **not the whole strategy**. The damage came from 15m/1h bearish shorts and from the `sol_5m_vs_slower` disagreement path, while `sol_5m_native` was the profitable engine.

### Best Improvement Candidates

#### 1. Relax `lane_entry_window` for 15m `BUY_YES` on `xrp_macro`

- Base rejected sample: `n=1190`, WR `67.1%`, total ghost PnL `+386.973`
- Probe-backed relaxes were all positive:
  - `+1m`: `n=27`, WR `74.1%`, total `+6.498`
  - `+2m`: `n=49`, WR `79.6%`, total `+8.148`
  - `+5m`: `n=93`, WR `73.1%`, total `+22.129`
- **Interpretation:** XRP 15m long timing is the cleanest “too conservative” setting in this session.
- **Suggested change:** test a **small** extension first, likely `+1m` or `+2m`, before a broader `+5m`.

#### 2. Relax `lane_entry_window` for 15m `BUY_YES` on `hype_macro`

- Base rejected sample: `n=851`, WR `67.1%`, total `+288.289`
- Probe-backed relaxes:
  - `+1m`: `n=14`, WR `64.3%`, total `+3.005`
  - `+2m`: `n=21`, WR `71.4%`, total `+6.289`
  - `+5m`: `n=53`, WR `67.9%`, total `+16.289`
- **Interpretation:** HYPE 15m longs were also late-blocked too often.
- **Suggested change:** same pattern as XRP, start with `+2m`.

#### 3. Relax `lane_entry_window` for 15m `BUY_YES` on `doge_macro`

- Base rejected sample: `n=683`, WR `67.1%`, total `+218.862`
- Probe-backed relaxes:
  - `+1m`: `n=16`, WR `87.5%`, total `+7.228`
  - `+2m`: `n=30`, WR `86.7%`, total `+7.802`
  - `+5m`: `n=51`, WR `76.5%`, total `+12.802`
- **Interpretation:** DOGE 15m long-side timing was meaningfully too strict.
- **Caveat:** DOGE short-side `lane_entry_window` was strongly protective, so this should stay **long-side only**.

#### 4. Relax `lane_entry_window` for 15m `BUY_YES` on `eth_macro`

- Base rejected sample: `n=486`, WR `66.5%`, total `+161.214`
- Probe-backed relaxes:
  - `+1m`: `n=4`, WR `75.0%`, total `+2.25`
  - `+2m`: `n=10`, WR `70.0%`, total `+4.628`
  - `+5m`: `n=21`, WR `71.4%`, total `+10.297`
- **Interpretation:** ETH 15m long timing looks loosenable.
- **Important constraint:** do **not** loosen `eth_15m_weak_confirm` globally from this sample. That gate stayed protective overall.

#### 5. Review `sol_macro` 15m long IQL reject logic

- `sol_macro|15m|BUY_YES|iql_15m_reject`: `n=631`, WR `60.1%`, total `+107.376`
- Margin study against the histogram floor:
  - Rows within `-0.03` of the floor: `n=578`, WR `58.7%`, total `+89.385`
  - Rows within `-0.01` of the floor: `n=432`, WR `53.2%`, total `+20.189`
  - Rows at or above the floor: `n=366`, WR `51.6%`, total `+2.003`
- **Interpretation:** the profitable misses are mostly the **slightly-below-floor** bullish 15m long setups, not a broad failure of the filter.
- **Suggested change:** do **not** cut the floor aggressively. Instead test a **small bullish-long-only IQL floor relaxation** for marginal misses below the threshold.

### Secondary Candidate

#### `xrp_macro` 15m `BUY_YES` `price_too_far_from_even`

- Base rejected sample: `n=30`, WR `33.3%`, total `+82.429`
- Probe-backed relaxes:
  - `-0.02` min price: `n=1`, total `+4.128`
  - `-0.05` min price: `n=5`, WR `100%`, total `+24.648`
  - `-0.10` min price: `n=13`, WR `53.8%`, total `+31.544`
- **Interpretation:** there is real upside here, but the sample is much thinner and the edge likely comes from a few high-payout tails.
- **Suggested change:** optional and lower priority than entry-window changes.

### Do Not Loosen From This Session

- `doge_macro|15m|BUY_NO|lane_entry_window`: `n=745`, total `-291.163` for the missed trades, so the current block protected substantial downside.
- `sol_macro|15m|BUY_NO|iql_15m_reject`: `n=921`, total `-248.636`, also strongly protective.
- `bnb_macro|15m|BUY_NO|lane_entry_window`: `n=857`, total `-221.685`, protective.
- `eth_macro|15m|BUY_YES|eth_15m_weak_confirm`: `n=414`, total `-73.978`, protective.
- `eth_macro|15m|BUY_NO|eth_15m_weak_confirm`: `n=826`, total `-82.626`, protective.
- Broad `price_too_far_from_even` loosening remains dangerous for several names; many probe variants stayed negative, especially BNB and multiple low-price 5m cases.

### BTC-Secondary Check

Per the recurring audit requirement, BTC-secondary context was checked explicitly for `sol_macro`, `eth_macro`, `hype_macro`, and `xrp_macro`.

- There were **no explicit BTC-family rejection reasons** in this session slice (`btc_*` gate families were absent).
- Regime split still mattered:
  - `sol_macro`: `BULL`-tagged rows `n=1143`, total `-29.649`; `NONE` rows `n=1736`, total `-201.605`
  - `eth_macro`: `BULL` `n=898`, total `+140.345`; `NONE` `n=1710`, total `-211.98`
  - `hype_macro`: `BULL` `n=2300`, total `+247.939`; `NONE` `n=95`, total `-16.446`
  - `xrp_macro`: `BULL` `n=2278`, total `+327.385`; `NONE` `n=87`, total `-4.593`
- **Interpretation:** BTC-secondary context helped ETH, HYPE, and XRP materially in this session. SOL improved under BTC bull context too, but still stayed slightly negative net.

### Priority Order

1. **Disable or hard-gate BTC `btc_quant_disagree_flip` shorts**, especially the 5m lane.
2. **Raise the BTC short bar when `btc_1h_regime=BULL`**, especially for 5m `BUY_NO` and very-late entries.
3. **Suppress ETH 5m bearish shorts at cheap NO prices** under `btc_1h_regime=BULL`; this was the main ETH loss engine.
4. **Block ETH 15m longs when `alt_htf_bias=NEUTRAL`, `btc_htf_bias=BEARISH`, and RSI is already stretched**; those were poor-quality continuation longs.
5. **De-emphasize SOL 15m/1h shorts and the `sol_5m_vs_slower` path**, while preserving `sol_5m_native`.
6. Then test the ghost-backed long-side loosening candidates for XRP/HYPE/DOGE/ETH only after the loss engines above are controlled.

### Concrete Calibration Changes

#### `bitcoin`

- Turn off `btc_quant_disagree_flip` for `BUY_NO`, or require a much higher threshold before it can override side selection.
- Raise `min_edge` for BTC `BUY_NO` when `btc_1h_regime=BULL`, especially on `5m`.
- Add a stricter low-price block for BTC short entries on `5m` when `entry_price <= 0.35`.
- Tighten very-late BTC short entries (`minutes_to_market_end <= 2`) because several worst losses came from that pattern.

#### `eth_macro`

- Disable or sharply restrict `eth_macro` `5m BUY_NO` under `btc_1h_regime=BULL`.
- Add a price-floor restriction to avoid cheap NO entries on ETH 5m fades.
- Block `15m BUY_YES` when:
  - `alt_htf_bias=NEUTRAL`
  - `btc_htf_bias=BEARISH`
  - RSI is already in the low-70s or above

#### `sol_macro`

- Reduce or temporarily disable `sol_15m_native` bearish shorts until they re-prove themselves.
- Disable `sol_5m_vs_slower` when `alt_htf_bias=BULLISH`; that disagreement path was negative.
- Keep `sol_5m_native` active; it was the only clearly positive SOL path in this session.

### Limits

- This ghost review supports **entry/gate** changes only.
- It does **not** answer exit, sizing, bankroll interaction, or brand-new feature questions.

### Metadata / Summary

- **Tags:** `#psb` `#ghost-lab` `#session-review` `#xrp_macro` `#hype_macro` `#sol_macro` `#eth_macro`
- **Related Concepts:** [[Ghost Lab]], [[rejected_candidates_settled.jsonl]], [[lane_entry_window]], [[iql_15m_reject]], [[price_too_far_from_even]], [[BTC-secondary context]]
- **Summary:** The 126-trade session was profitable live, and the aligned ghost slice shows the bot was still net selective overall. The best improvements are concentrated in 15m long-side timing for XRP, HYPE, DOGE, and ETH, plus a narrow bullish-long relaxation around SOL’s `iql_15m_reject` threshold.
