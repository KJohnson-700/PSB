# 1H asset audit — session `test_20260528_042826`

Scope: live exits from [summary.json](/Users/mainfolder/Documents/psb-main%201/data/paper_trades/test_20260528_042826/summary.json) plus settled-ghost rejects from the matching session window in `data/calibration/rejected_candidates_settled.jsonl`.

## Live 1H exits

| Strategy | 1H exits | 1H PnL | Read |
|---|---:|---:|---|
| `bitcoin` | 7 | `-1.00` | weak/mixed; not dead, but stop-loss heavy |
| `sol_macro` | 1 | `-3.59` | bad sample |
| `xrp_macro` | 1 | `+6.02` | positive sample |
| `bnb_macro` | 0 | `0` | starved |
| `eth_macro` | 0 | `0` | starved |
| `hype_macro` | 0 | `0` | starved |
| `doge_macro` | 0 | `0` | starved |

## Ghost standouts by asset

### `bitcoin`

- Live: `7` exits, `-1.00` net.
- Ghost: `1h lane_min_edge_bias_quant_disagree` on `bitcoin|1h|down|bearish|htf_bearish_side_short` was strongly positive: `21/28`, `75.0%`.
- Ghost: plain `1h lane_min_edge` on the same live lane was poor: `1/9`, `11.1%`.
- Read: the 1h BTC issue is not broad starvation. It is branch quality. Some skipped disagreement trades were good, but the plain admitted 1h short path was not reliably good.

### `bnb_macro`

- Live: no 1h exits in this session.
- Ghost:
  - `lane_entry_window` on `bnb_macro|1h|down|bearish__bearish__bull|standard`: `92/182`, `50.5%`
  - `lane_min_edge` on `bnb_macro|1h|down|bearish__bearish__bull|bnb_1h_native`: `14/22`, `63.6%`
  - `liquidity` on the same lane family: `11/18`, `61.1%`
- Read: BNB 1h is starved more than broken. Recent ghosts do not support treating BNB 1h as a terrible lane.

### `doge_macro`

- Live: no 1h exits in this session.
- Ghost:
  - `lane_entry_window`: `21/139`, `15.1%`
  - `lane_min_edge` on `doge_1h_native`: `2/13`, `15.4%`
  - `oracle_basis_block`: `2/35`, `5.7%`
- Read: DOGE 1h down looks genuinely poor. This is not just starvation.

### `eth_macro`

- Live: no 1h exits in this session.
- Ghost:
  - `eth_1h_weak_confirm` on `eth_macro|1h|down|bearish__bearish|standard`: `71/197`, `36.0%`
- Read: ETH 1h looks structurally weak in the current confirm model. Starvation alone is not the explanation.

### `hype_macro`

- Live: no 1h exits in this session.
- Ghost:
  - `1h down lane_entry_window`: `27/136`, `19.9%`
  - `1h down liquidity`: `3/19`, `15.8%`
  - `1h up liquidity`: `17/17`, `100%`
  - `1h up lane_entry_window`: `10/10`, `100%`
- Read: HYPE 1h is bifurcated. Downside 1h looks bad; upside 1h looks badly starved.

### `sol_macro`

- Live: `1` exit, `-3.59`, side source `sol_1h_native`.
- Ghost:
  - `iql_15m_reject` on `sol_macro|1h|down|bearish__bearish|standard`: `68/182`, `37.4%`
  - `lane_entry_window`: `11/28`, `39.3%`
  - `lane_min_edge` on `sol_1h_native`: `1/5`, `20.0%`
- Read: SOL 1h looks weak in both live and ghosts. This does not look like healthy starvation.

### `xrp_macro`

- Live: `1` exit, `+6.02`, side source `xrp_1h_native`.
- Ghost:
  - `1h up lane_min_edge` on `xrp_1h_native`: `14/14`, `100%`
  - `1h up oracle_basis_block`: `2/3`, `66.7%`
  - `1h up lane_entry_window`: `10/30`, `33.3%`
  - `1h down lane_entry_window`: `0/56`, `0.0%`
  - `1h down lane_min_edge`: `1/6`, `16.7%`
- Read: XRP 1h is asymmetric. Upside looks undertraded; downside looks poor.

## Ranked 1H verdict

1. Best starvation candidate: `xrp_macro` 1h up
2. Next starvation candidate: `bnb_macro` 1h down
3. Mixed / branch-quality problem: `bitcoin` 1h down
4. Weak model problem: `eth_macro` 1h down
5. Weak lane problem: `sol_macro` 1h down
6. Weak lane problem: `doge_macro` 1h down
7. Split regime: `hype_macro` 1h up starved, `1h` down poor

## Practical takeaway

- Do **not** treat all `1h` lanes as one problem.
- `bnb_macro` and `xrp_macro` have the strongest current evidence for undertraded `1h` opportunity.
- `eth_macro`, `sol_macro`, and `doge_macro` look more like true `1h` quality problems than mere starvation.
